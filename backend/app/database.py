"""SQLite 持久层：连接管理、表结构与底层读写。

设计要点：
- 只依赖标准库 sqlite3，沿用原项目“克隆即可运行”的约定；
- 每条业务记录保存在 entries 表（按 module 分表逻辑等价），业务字段以 JSON 存储，
  原编号 id / status / pending / abnormal 提升为列，便于筛选、统计与迁移核对；
- 切换台账（迁移批次、分发、待办、认领）单独建表，承载幂等键和可回滚信息；
- 写操作统一串行化（_write_lock），保证“动作 + 跨模块派单 + 台账”在同一事务内完成，
  分发者中断重试时凭幂等键拿回原处理结果，不会再派一项。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.config import settings

# 业务记录的固定列；其余中文字段进入 data JSON。
_BASE_COLUMNS = ("id", "status", "pending", "abnormal")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    module      TEXT NOT NULL,
    id          INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT '',
    pending     INTEGER NOT NULL DEFAULT 1,
    abnormal    INTEGER NOT NULL DEFAULT 0,
    data        TEXT NOT NULL DEFAULT '{}',
    source_code TEXT,
    source_ref  TEXT,
    batch_id    TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (module, id)
);
CREATE INDEX IF NOT EXISTS idx_entries_module ON entries(module);
CREATE INDEX IF NOT EXISTS idx_entries_source ON entries(source_code, source_ref);
CREATE INDEX IF NOT EXISTS idx_entries_batch ON entries(batch_id);

CREATE TABLE IF NOT EXISTS migration_batches (
    batch_id       TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    started_at     TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at    TEXT,
    status         TEXT NOT NULL,            -- importing / success / failed / rolled_back
    imported_rows  INTEGER NOT NULL DEFAULT 0,
    expected_rows  INTEGER NOT NULL DEFAULT 0,
    receipt        TEXT NOT NULL DEFAULT '{}',
    backup_file    TEXT
);

CREATE TABLE IF NOT EXISTS dispatch_ledger (
    dispatch_key TEXT PRIMARY KEY,            -- 幂等键：分发类型:来源模块:来源编号
    module       TEXT NOT NULL,
    entry_id     INTEGER NOT NULL,
    action       TEXT NOT NULL,
    result       TEXT NOT NULL DEFAULT '{}',  -- 首次处理结果快照
    status       TEXT NOT NULL DEFAULT 'done',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS todo_inbox (
    todo_code     TEXT PRIMARY KEY,           -- 幂等键：来源模块:来源编号:目标模块
    module        TEXT NOT NULL,              -- 待办所在业务模块（如 improve_task / peer_review）
    entry_id      INTEGER,                    -- 关联的业务记录编号
    title         TEXT NOT NULL,
    origin_module TEXT NOT NULL,              -- 待办出处（review_meeting / lesson_prep ...）
    origin_ref    TEXT NOT NULL,              -- 出处原编号/原编码
    source_code   TEXT,                       -- 旧工作台来源（保留来源用）
    source_ref    TEXT,
    status        TEXT NOT NULL DEFAULT '待认领',  -- 待认领 / 已认领 / 已处理 / 已撤回
    assignee      TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    claimed_at    TEXT,
    UNIQUE (module, entry_id, origin_module)
);

CREATE TABLE IF NOT EXISTS claim_ledger (
    claim_key   TEXT PRIMARY KEY,             -- 幂等键：module:entry_id（同一待办只认一次）
    module      TEXT NOT NULL,
    entry_id    INTEGER NOT NULL,
    todo_code   TEXT,
    teacher     TEXT,
    action      TEXT NOT NULL,
    result      TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_write_lock = threading.RLock()
_local = threading.local()
# 测试可通过 configure() 把数据库指到临时文件；None 表示使用 settings.db_path。
_path_override: str | None = None


def configure(path: str | None) -> None:
    """切换数据库文件（主要给测试使用），并关闭旧连接、重建表结构。"""
    global _path_override
    _path_override = path
    reset_conn()
    init_schema()


def get_db_path() -> str:
    return _path_override or settings.db_path


def _connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None：交给我们自己的 transaction() 管理显式事务，
    # 避免 sqlite3 模块隐式事务与 BEGIN IMMEDIATE 嵌套报错。
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def init_schema(path: str | None = None) -> None:
    """建表（幂等）。测试可通过 path 指向临时数据库。"""
    target = path or get_db_path()
    with _connect(target) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


def get_conn() -> sqlite3.Connection:
    """每线程一个连接；服务为单进程，WAL 下读写并发足够。"""
    if not hasattr(_local, "conn") or _local.conn is None:
        init_schema()
        _local.conn = _connect(get_db_path())
    return _local.conn


def reset_conn() -> None:
    """关闭并清理线程连接（测试切换数据库路径后调用）。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
    _local.conn = None


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """写事务上下文：支持嵌套（内层用 SAVEPOINT），提交/回滚统一处理。

    autocommit 连接下，TrackedRow 的即时写回会自动加入当前显式事务，
    保证“动作 + 跨模块派单 + 台账”原子提交，分发/认领重试不会留下半成品。
    """
    conn = get_conn()
    with _write_lock:
        if getattr(_local, "tx_depth", 0) > 0:
            savepoint = f"sp_{_local.tx_depth}_{id(conn)}"
            conn.execute(f"SAVEPOINT {savepoint};")
            _local.tx_depth += 1
            try:
                yield conn
                conn.execute(f"RELEASE SAVEPOINT {savepoint};")
            except Exception:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint};")
                conn.execute(f"RELEASE SAVEPOINT {savepoint};")
                raise
            return

        _local.tx_depth = 1
        try:
            conn.execute("BEGIN IMMEDIATE;")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            _local.tx_depth = 0


def in_transaction() -> bool:
    return getattr(_local, "tx_depth", 0) > 0


def maybe_commit(conn: sqlite3.Connection) -> None:
    """仅在没有外层显式事务时提交，避免台账辅助方法提前提交业务事务。"""
    if not in_transaction():
        conn.commit()


# ----------------------------- entries 表读写 -----------------------------

def row_to_db_tuple(module: str, row: dict[str, Any]) -> tuple[Any, ...]:
    extra = {
        key: value
        for key, value in row.items()
        if key not in _BASE_COLUMNS and key not in ("source_code", "source_ref", "batch_id")
    }
    return (
        module,
        int(row.get("id", 0)),
        str(row.get("status", "")),
        1 if row.get("pending") else 0,
        1 if row.get("abnormal") else 0,
        json.dumps(extra, ensure_ascii=False, default=str),
        row.get("source_code"),
        row.get("source_ref"),
        row.get("batch_id"),
    )


def db_row_to_dict(record: sqlite3.Row) -> dict[str, Any]:
    data = json.loads(record["data"] or "{}")
    row: dict[str, Any] = dict(data)
    row["id"] = record["id"]
    row["status"] = record["status"]
    row["pending"] = bool(record["pending"])
    row["abnormal"] = bool(record["abnormal"])
    if record["source_code"]:
        row["source_code"] = record["source_code"]
    if record["source_ref"]:
        row["source_ref"] = record["source_ref"]
    if record["batch_id"]:
        row["batch_id"] = record["batch_id"]
    return row


def load_all_entries() -> dict[str, list[dict[str, Any]]]:
    conn = get_conn()
    tables: dict[str, list[dict[str, Any]]] = {}
    records = conn.execute(
        "SELECT * FROM entries ORDER BY module, id"
    ).fetchall()
    for record in records:
        tables.setdefault(record["module"], []).append(db_row_to_dict(record))
    return tables


def upsert_entry(module: str, row: dict[str, Any]) -> None:
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO entries (module, id, status, pending, abnormal, data,
                             source_code, source_ref, batch_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(module, id) DO UPDATE SET
            status=excluded.status,
            pending=excluded.pending,
            abnormal=excluded.abnormal,
            data=excluded.data,
            source_code=COALESCE(excluded.source_code, entries.source_code),
            source_ref=COALESCE(excluded.source_ref, entries.source_ref),
            batch_id=COALESCE(excluded.batch_id, entries.batch_id),
            updated_at=datetime('now')
        """,
        row_to_db_tuple(module, row),
    )


def bulk_upsert_entries(module: str, rows: list[dict[str, Any]]) -> None:
    conn = get_conn()
    conn.executemany(
        """
        INSERT INTO entries (module, id, status, pending, abnormal, data,
                             source_code, source_ref, batch_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(module, id) DO UPDATE SET
            status=excluded.status,
            pending=excluded.pending,
            abnormal=excluded.abnormal,
            data=excluded.data,
            source_code=COALESCE(excluded.source_code, entries.source_code),
            source_ref=COALESCE(excluded.source_ref, entries.source_ref),
            batch_id=COALESCE(excluded.batch_id, entries.batch_id),
            updated_at=datetime('now')
        """,
        [row_to_db_tuple(module, row) for row in rows],
    )


def delete_entry(module: str, entry_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM entries WHERE module=? AND id=?", (module, entry_id))


def clear_module(module: str) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM entries WHERE module=?", (module,))


def count_entries(module: str | None = None) -> int:
    conn = get_conn()
    if module is None:
        return int(conn.execute("SELECT COUNT(*) AS c FROM entries").fetchone()["c"])
    return int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM entries WHERE module=?", (module,)
        ).fetchone()["c"]
    )


def fetch_entries(module: str) -> list[dict[str, Any]]:
    conn = get_conn()
    records = conn.execute(
        "SELECT * FROM entries WHERE module=? ORDER BY id", (module,)
    ).fetchall()
    return [db_row_to_dict(record) for record in records]


def backup_database(backup_path: str) -> str:
    """SQLite 在线备份（含台账表），供切换失败/回滚时恢复服务。"""
    Path(backup_path).parent.mkdir(parents=True, exist_ok=True)
    source = get_conn()
    source.commit()
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
    return backup_path
