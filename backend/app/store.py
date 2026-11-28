"""数据仓库：业务记录与切换台账的统一入口。

历史实现是进程内字典（内存台账），现迁移到 SQLite 持久化。为了让 20 个领域服务
及其业务入口（同一套 /api/{module} 资源路由）零改动继续工作，这里做两件事：

1. ``TrackedRow`` / ``TrackedList``：写回式容器。服务里既有的 ``row["status"] = ...``、
   ``rows.append({...})``、``rows.clear()`` 等原地修改会自动同步到 SQLite，
   读路径仍像普通 dict/list 一样使用；
2. 新增迁移批次、分发台账、待办收件箱、认领台账的读写方法，供切换与派单流程使用。

首次启动且数据库为空时，用内置示例数据初始化，保证“克隆即可运行”的旧体验不变。
"""
from __future__ import annotations

from typing import Any

from app import database as db
from app.seed import SEED_ROWS


class TrackedRow(dict):
    """普通 dict 的写回子类：任何键变更都会持久化所属模块记录。"""

    def __init__(self, module: str, data: dict[str, Any]) -> None:
        super().__init__(data)
        # 用 object.__setattr__ 避免与 dict 键空间混淆
        object.__setattr__(self, "_module", module)

    def _persist(self) -> None:
        db.upsert_entry(object.__getattribute__(self, "_module"), dict(self))

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        if key != "id":  # 主键不允许改，避免无谓写入
            self._persist()

    def __delitem__(self, key: str) -> None:
        super().__delitem__(key)
        self._persist()

    def update(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        super().update(*args, **kwargs)
        self._persist()

    def setdefault(self, key: str, default: Any = None) -> Any:
        if key not in self:
            self[key] = default
        return super().get(key)

    def pop(self, key: str, default: Any = ...) -> Any:  # type: ignore[override]
        value = super().pop(key) if default is ... else super().pop(key, default)
        self._persist()
        return value


class TrackedList(list):
    """普通 list 的写回子类：append/clear 等结构变更同步到 SQLite。

    从数据库加载进来的是普通 dict，通过 __getitem__/迭代时按需包成 TrackedRow，
    保证服务拿到的每一行都是可写回对象，reload 之后也不例外。
    """

    def __init__(self, module: str, rows: list[dict[str, Any]] | None = None) -> None:
        super().__init__(rows or [])
        object.__setattr__(self, "_module", module)

    def _wrap(self, row: dict[str, Any]) -> TrackedRow:
        if isinstance(row, TrackedRow):
            return row
        return TrackedRow(object.__getattribute__(self, "_module"), row)

    def __getitem__(self, index: int) -> Any:
        row = super().__getitem__(index)
        if not isinstance(row, TrackedRow):
            wrapped = self._wrap(row)
            super().__setitem__(index, wrapped)
            return wrapped
        return row

    def __iter__(self) -> Any:
        for index in range(len(self)):
            yield self[index]

    def append(self, row: dict[str, Any]) -> None:
        tracked = self._wrap(row)
        super().append(tracked)
        db.upsert_entry(object.__getattribute__(self, "_module"), dict(tracked))

    def insert(self, index: int, row: dict[str, Any]) -> None:
        tracked = self._wrap(row)
        super().insert(index, tracked)
        db.upsert_entry(object.__getattribute__(self, "_module"), dict(tracked))

    def extend(self, rows: Any) -> None:
        for row in rows:
            self.append(row)

    def clear(self) -> None:
        super().clear()
        db.clear_module(object.__getattribute__(self, "_module"))

    def remove(self, row: Any) -> None:
        super().remove(row)
        db.delete_entry(
            object.__getattribute__(self, "_module"), int(row.get("id", 0))
        )

    def pop(self, index: int = -1) -> Any:
        row = super().pop(index)
        db.delete_entry(
            object.__getattribute__(self, "_module"), int(row.get("id", 0))
        )
        return row


class Store:
    """兼容旧内存仓库接口，并在底层落 SQLite。"""

    def __init__(self) -> None:
        self._tables: dict[str, TrackedList] = {}
        self.bootstrap()

    # --------------------------- 初始化与内存视图 ---------------------------

    def bootstrap(self) -> None:
        """建表并加载数据；空库时灌入示例数据。"""
        db.init_schema()
        db.reset_conn()
        self._tables = {}
        loaded = db.load_all_entries()
        if not loaded and not db.get_conn().execute(
            "SELECT 1 FROM migration_batches LIMIT 1"
        ).fetchone():
            # 全新环境：保留原有示例数据体验
            for module, rows in SEED_ROWS.items():
                db.bulk_upsert_entries(module, [dict(r) for r in rows])
            loaded = db.load_all_entries()
        for module, rows in loaded.items():
            self._tables[module] = TrackedList(module, rows)

    def reload(self) -> None:
        """从数据库重新装载内存视图（迁移/回滚后调用）。"""
        db.reset_conn()
        loaded = db.load_all_entries()
        self._tables = {
            module: TrackedList(module, rows) for module, rows in loaded.items()
        }

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> TrackedList:
        if module not in self._tables:
            self._tables[module] = TrackedList(module, db.fetch_entries(module))
        return self._tables[module]

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ----------------------------- 待办收件箱 -----------------------------

    def upsert_todo(self, todo: dict[str, Any]) -> dict[str, Any]:
        """登记待办（幂等）：同一 todo_code 重复写入只更新可空字段，不重复派单。"""
        conn = db.get_conn()
        conn.execute(
            """
            INSERT INTO todo_inbox (todo_code, module, entry_id, title,
                                    origin_module, origin_ref, source_code,
                                    source_ref, status, assignee, claimed_at)
            VALUES (:todo_code, :module, :entry_id, :title,
                    :origin_module, :origin_ref, :source_code,
                    :source_ref, :status, :assignee, :claimed_at)
            ON CONFLICT(todo_code) DO UPDATE SET
                title=excluded.title,
                entry_id=COALESCE(excluded.entry_id, todo_inbox.entry_id),
                source_code=COALESCE(excluded.source_code, todo_inbox.source_code),
                source_ref=COALESCE(excluded.source_ref, todo_inbox.source_ref)
            """,
            {
                "todo_code": todo["todo_code"],
                "module": todo["module"],
                "entry_id": todo.get("entry_id"),
                "title": todo.get("title", ""),
                "origin_module": todo["origin_module"],
                "origin_ref": str(todo["origin_ref"]),
                "source_code": todo.get("source_code"),
                "source_ref": todo.get("source_ref"),
                "status": todo.get("status", "待认领"),
                "assignee": todo.get("assignee"),
                "claimed_at": todo.get("claimed_at"),
            },
        )
        return self.get_todo(todo["todo_code"]) or todo

    def get_todo(self, todo_code: str) -> dict[str, Any] | None:
        record = db.get_conn().execute(
            "SELECT * FROM todo_inbox WHERE todo_code=?", (todo_code,)
        ).fetchone()
        return dict(record) if record else None

    def find_todo(self, module: str, entry_id: int, origin_module: str) -> dict[str, Any] | None:
        record = db.get_conn().execute(
            "SELECT * FROM todo_inbox WHERE module=? AND entry_id=? AND origin_module=?",
            (module, entry_id, origin_module),
        ).fetchone()
        return dict(record) if record else None

    def list_todos(self, module: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM todo_inbox WHERE 1=1"
        params: list[Any] = []
        if module:
            sql += " AND module=?"
            params.append(module)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC, rowid DESC"
        return [dict(r) for r in db.get_conn().execute(sql, params).fetchall()]

    def mark_todo(self, todo_code: str, status: str, assignee: str | None = None) -> None:
        db.get_conn().execute(
            """
            UPDATE todo_inbox
               SET status=?, assignee=COALESCE(?, assignee),
                   claimed_at=CASE WHEN ?='已认领' THEN datetime('now') ELSE claimed_at END
             WHERE todo_code=?
            """,
            (status, assignee, status, todo_code),
        )

    # ----------------------------- 分发 / 认领台账 -----------------------------

    def record_dispatch(self, dispatch_key: str, module: str, entry_id: int,
                        action: str, result: dict[str, Any]) -> None:
        db.get_conn().execute(
            """
            INSERT OR IGNORE INTO dispatch_ledger
                (dispatch_key, module, entry_id, action, result)
            VALUES (?, ?, ?, ?, ?)
            """,
            (dispatch_key, module, entry_id, action,
             db.json.dumps(result, ensure_ascii=False, default=str)),
        )

    def get_dispatch(self, dispatch_key: str) -> dict[str, Any] | None:
        record = db.get_conn().execute(
            "SELECT * FROM dispatch_ledger WHERE dispatch_key=?", (dispatch_key,)
        ).fetchone()
        if not record:
            return None
        payload = dict(record)
        payload["result"] = db.json.loads(record["result"] or "{}")
        return payload

    def list_dispatch(self, module: str | None = None) -> list[dict[str, Any]]:
        if module:
            records = db.get_conn().execute(
                "SELECT * FROM dispatch_ledger WHERE module=? ORDER BY created_at DESC",
                (module,),
            ).fetchall()
        else:
            records = db.get_conn().execute(
                "SELECT * FROM dispatch_ledger ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in records]

    def record_claim(self, claim_key: str, module: str, entry_id: int,
                     action: str, result: dict[str, Any],
                     todo_code: str | None = None, teacher: str | None = None) -> None:
        db.get_conn().execute(
            """
            INSERT OR IGNORE INTO claim_ledger
                (claim_key, module, entry_id, todo_code, teacher, action, result)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (claim_key, module, entry_id, todo_code, teacher, action,
             db.json.dumps(result, ensure_ascii=False, default=str)),
        )

    def get_claim(self, claim_key: str) -> dict[str, Any] | None:
        record = db.get_conn().execute(
            "SELECT * FROM claim_ledger WHERE claim_key=?", (claim_key,)
        ).fetchone()
        if not record:
            return None
        payload = dict(record)
        payload["result"] = db.json.loads(record["result"] or "{}")
        return payload

    def find_claim_for_entry(self, module: str, entry_id: int) -> dict[str, Any] | None:
        return self.get_claim(f"{module}:{entry_id}")

    def list_claims(self, module: str | None = None) -> list[dict[str, Any]]:
        if module:
            records = db.get_conn().execute(
                "SELECT * FROM claim_ledger WHERE module=? ORDER BY created_at DESC",
                (module,),
            ).fetchall()
        else:
            records = db.get_conn().execute(
                "SELECT * FROM claim_ledger ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in records]


store = Store()
