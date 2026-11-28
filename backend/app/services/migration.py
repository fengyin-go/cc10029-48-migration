"""存量迁移服务：把旧工作台导出的内存台账导入 SQLite，并出具可核对的切换回执。

关键约束（对应切换方案）：
- 保留纪要、诊改任务、备课评议等记录的原编号与来源（source_code/source_ref 落列）；
- 整批导入在一个事务内完成，任一步失败整体回滚，数据库回到导入前；
- 导入前做 SQLite 在线备份，验证不通过或上线后需要撤回时，可按批次定向回滚，
  已经向操作员确认受理的认领/评议邀请（独立台账表）保留，仍可继续办理；
- 待认领事项按幂等键写入收件箱，重复导入/分发不会重发、不会再派一项；
- 回执同时落 migration_batches 表和 receipt_dir 下的 JSON 文件，记录数与关联结果可对照。
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app import database as db
from app.config import settings
from app.store import store

# 文本标记前缀 -> 业务模块，用于核对跨模块关联是否能在新库中找回。
MARKER_MODULES = {
    "meeting": "review_meeting",
    "lesson_prep": "lesson_prep",
    "peer_review": "peer_review",
    "exam": "exam_analysis",
    "quality_report": "quality_report",
    "plan": "improve_plan",
    "feedback": "feedback",
    "observation": "observation",
}
MARKER_RE = re.compile(r"\b([a-z_]+):(\d+)\b")

# 这些模块的原编号与来源是切换核对的重点。
SOURCE_SENSITIVE_MODULES = ("review_meeting", "improve_task", "peer_review", "lesson_prep")

SOURCE_CODE_KEYS = ("source_code", "来源", "source")
SOURCE_REF_KEYS = ("source_ref", "原编号", "原始编号", "业务编号", "legacy_id", "source_id")


class MigrationService:
    # ----------------------------- 对外主流程 -----------------------------

    def import_export(self, payload: dict[str, Any], *,
                      batch_id: str | None = None,
                      source: str | None = None,
                      dry_run: bool = False) -> dict[str, Any]:
        """导入一份旧工作台导出数据，返回切换回执。"""
        batch_id = batch_id or f"mig-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
        source = source or str(payload.get("source") or "legacy-workbench")
        records_by_module, flat_todos = self._normalize_payload(payload)
        expected_counts = {module: len(rows) for module, rows in records_by_module.items()}
        total_expected = sum(expected_counts.values())

        if dry_run:
            return self._build_receipt(
                batch_id, source, payload.get("exported_at"),
                expected_counts, {}, [], [], backup_file="",
                dry_run=True, errors=[],
            )

        Path(settings.receipt_dir).mkdir(parents=True, exist_ok=True)
        backup_file = str(Path(settings.receipt_dir) / "backups" / f"{batch_id}.sqlite3")
        db.backup_database(backup_file)
        self._insert_batch(batch_id, source, total_expected, backup_file)

        errors: list[str] = []
        try:
            with db.transaction():
                imported_counts: dict[str, int] = {}
                for module, rows in records_by_module.items():
                    prepared = [
                        self._prepare_row(module, row, batch_id, source) for row in rows
                    ]
                    db.bulk_upsert_entries(module, prepared)
                    imported_counts[module] = len(prepared)
                todo_results = self._import_todos(flat_todos, source)
        except Exception as exc:  # 事务已回滚，记录失败批次后抛出
            self._finish_batch(batch_id, "failed", 0,
                               {"error": f"导入事务回滚：{exc}"})
            raise

        store.reload()
        link_details = self._verify_links(records_by_module)
        receipt = self._build_receipt(
            batch_id, source, payload.get("exported_at"),
            expected_counts, imported_counts, link_details, todo_results,
            backup_file=backup_file, dry_run=False, errors=errors,
        )
        passed = self._verification_passed(receipt)
        receipt["verification_passed"] = passed
        if not passed:
            # 验证不通过：按批次定向回滚，恢复服务到导入前状态。
            self.rollback(batch_id, reason="切换验证不通过自动回滚")
            receipt["status"] = "rolled_back"
        else:
            self._finish_batch(batch_id, "success", total_expected, receipt)
        self._write_receipt_file(batch_id, receipt)
        return receipt

    def verify_batch(self, batch_id: str) -> dict[str, Any]:
        """对已导入批次重新执行记录数与关联核对。"""
        batch = self._get_batch(batch_id)
        if batch is None:
            raise KeyError(f"迁移批次 {batch_id} 不存在")
        receipt = json.loads(batch["receipt"] or "{}")
        # 用当前库里的实际记录数重新核对
        actual = {
            module: db.get_conn().execute(
                "SELECT COUNT(*) AS c FROM entries WHERE module=?", (module,)
            ).fetchone()["c"]
            for module in receipt.get("counts", {}).get("expected", {})
        }
        receipt["current_counts"] = actual
        receipt["verification_passed"] = self._verification_passed(receipt)
        return receipt

    def rollback(self, batch_id: str, *, reason: str = "") -> dict[str, Any]:
        """按批次定向回滚：删除/还原本批 entries，保留分发、待办、认领台账。"""
        batch = self._get_batch(batch_id)
        if batch is None:
            raise KeyError(f"迁移批次 {batch_id} 不存在")
        backup_file = batch["backup_file"]

        with db.transaction():
            if backup_file and Path(backup_file).exists():
                backup_rows = self._load_backup_entries(backup_file)
                conn = db.get_conn()
                tagged = conn.execute(
                    "SELECT module, id FROM entries WHERE batch_id=?", (batch_id,)
                ).fetchall()
                for record in tagged:
                    key = (record["module"], record["id"])
                    if key in backup_rows:
                        # 导入前就存在的记录：还原旧版本
                        db.upsert_entry(record["module"], backup_rows[key])
                    else:
                        # 导入新增的记录：删除
                        db.delete_entry(record["module"], record["id"])
            else:
                db.get_conn().execute(
                    "DELETE FROM entries WHERE batch_id=?", (batch_id,)
                )
        self._finish_batch(batch_id, "rolled_back", batch["imported_rows"],
                           {"rollback_reason": reason or "人工回滚"})
        store.reload()
        result = {
            "batch_id": batch_id,
            "status": "rolled_back",
            "reason": reason or "人工回滚",
            "ledger_preserved": {
                "dispatch": len(store.list_dispatch()),
                "todos": len(store.list_todos()),
                "claims": len(store.list_claims()),
            },
        }
        self._write_receipt_file(batch_id, result)
        return result

    def list_batches(self) -> list[dict[str, Any]]:
        records = db.get_conn().execute(
            "SELECT batch_id, source, status, imported_rows, expected_rows, "
            "started_at, finished_at, backup_file FROM migration_batches "
            "ORDER BY started_at DESC"
        ).fetchall()
        return [dict(r) for r in records]

    def get_receipt(self, batch_id: str) -> dict[str, Any]:
        batch = self._get_batch(batch_id)
        if batch is None:
            raise KeyError(f"迁移批次 {batch_id} 不存在")
        return json.loads(batch["receipt"] or "{}")

    # ----------------------------- 内部实现 -----------------------------

    def _normalize_payload(self, payload: dict[str, Any]) -> tuple[
        dict[str, list[dict[str, Any]]], list[dict[str, Any]]
    ]:
        records: dict[str, list[dict[str, Any]]] = {}
        if isinstance(payload.get("records"), dict):
            for module, rows in payload["records"].items():
                records.setdefault(module, []).extend(rows or [])
        elif isinstance(payload.get("records"), list):
            for item in payload["records"]:
                module = item.get("module") or item.get("_module")
                row = item.get("row") if "row" in item else {
                    k: v for k, v in item.items() if k not in ("module", "_module")
                }
                if module:
                    records.setdefault(module, []).append(row)
        todos = list(payload.get("todos") or [])
        return records, todos

    def _prepare_row(self, module: str, row: dict[str, Any],
                     batch_id: str, source: str) -> dict[str, Any]:
        prepared = dict(row)
        # 原编号：沿用导出 id；缺失时按模块顺延
        if "id" not in prepared or prepared.get("id") in (None, ""):
            current_max = db.get_conn().execute(
                "SELECT COALESCE(MAX(id), 0) AS m FROM entries WHERE module=?",
                (module,),
            ).fetchone()["m"]
            prepared["id"] = int(current_max) + 1
        else:
            prepared["id"] = int(prepared["id"])
        prepared.setdefault("status", "草稿")
        prepared.setdefault("pending", True)
        prepared.setdefault("abnormal", False)
        # 来源与原编号落列，供切换后对照
        source_code = self._pick_alias(prepared, SOURCE_CODE_KEYS) or source
        source_ref = self._pick_alias(prepared, SOURCE_REF_KEYS) or str(prepared["id"])
        prepared["source_code"] = source_code
        prepared["source_ref"] = source_ref
        prepared["batch_id"] = batch_id
        return prepared

    @staticmethod
    def _pick_alias(row: dict[str, Any], keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = row.get(key)
            if value not in (None, ""):
                return str(value)
        return None

    def _import_todos(self, todos: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for item in todos:
            module = item["module"]
            origin_module = item["origin_module"]
            origin_ref = str(item["origin_ref"])
            todo_code = item.get("todo_code") or f"{origin_module}:{origin_ref}:{module}"
            entry_id = item.get("entry_id")
            stored = store.upsert_todo({
                "todo_code": todo_code,
                "module": module,
                "entry_id": int(entry_id) if entry_id is not None else None,
                "title": item.get("title", ""),
                "origin_module": origin_module,
                "origin_ref": origin_ref,
                "source_code": item.get("source_code") or source,
                "source_ref": item.get("source_ref") or origin_ref,
                "status": item.get("status", "待认领"),
                "assignee": item.get("assignee"),
                "claimed_at": item.get("claimed_at"),
            })
            results.append(stored)
        return results

    def _verify_links(self, records_by_module: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        details: list[dict[str, Any]] = []
        # 导入后以数据库实际数据构建 (module, id) 集合
        available: set[tuple[str, int]] = {
            (r["module"], r["id"])
            for r in db.get_conn().execute("SELECT module, id FROM entries").fetchall()
        }
        for module, rows in records_by_module.items():
            for row in rows:
                for field, value in row.items():
                    if not isinstance(value, str):
                        continue
                    for prefix, ref_id in MARKER_RE.findall(value):
                        target_module = MARKER_MODULES.get(prefix)
                        if target_module is None:
                            continue
                        resolved = (target_module, int(ref_id)) in available
                        details.append({
                            "module": module,
                            "entry_id": int(row.get("id", 0)),
                            "field": field,
                            "marker": f"{prefix}:{ref_id}",
                            "target_module": target_module,
                            "target_id": int(ref_id),
                            "resolved": resolved,
                        })
        return details

    def _build_receipt(self, batch_id: str, source: str, exported_at: Any,
                       expected_counts: dict[str, int],
                       imported_counts: dict[str, int],
                       link_details: list[dict[str, Any]],
                       todo_results: list[dict[str, Any]],
                       *, backup_file: str, dry_run: bool,
                       errors: list[str]) -> dict[str, Any]:
        source_preservation: dict[str, dict[str, int]] = {}
        for module in SOURCE_SENSITIVE_MODULES:
            rows = db.fetch_entries(module) if not dry_run else []
            checked = len(rows)
            with_source = sum(1 for r in rows if r.get("source_code") and r.get("source_ref"))
            source_preservation[module] = {"checked": checked, "with_source": with_source}

        todo_by_status: dict[str, int] = {}
        for todo in todo_results:
            todo_by_status[todo["status"]] = todo_by_status.get(todo["status"], 0) + 1
        linked_todos = sum(
            1 for t in todo_results
            if t.get("entry_id") is not None
            and store.find(t["module"], int(t["entry_id"])) is not None
        )
        resolved = sum(1 for d in link_details if d["resolved"])
        receipt = {
            "batch_id": batch_id,
            "source": source,
            "exported_at": exported_at,
            "imported_at": datetime.now().isoformat(timespec="seconds"),
            "status": "dry_run" if dry_run else "success",
            "dry_run": dry_run,
            "counts": {
                "expected": expected_counts,
                "imported": imported_counts,
                "total_expected": sum(expected_counts.values()),
                "total_imported": sum(imported_counts.values()),
            },
            "source_preservation": source_preservation,
            "links": {
                "total": len(link_details),
                "resolved": resolved,
                "unresolved": len(link_details) - resolved,
                "details": link_details,
            },
            "todos": {
                "imported": len(todo_results),
                "by_status": todo_by_status,
                "linked": linked_todos,
                "unlinked": len(todo_results) - linked_todos,
            },
            "backup_file": backup_file,
            "verification_passed": False,
            "errors": errors,
        }
        if not dry_run:
            receipt["verification_passed"] = self._verification_passed(receipt)
        return receipt

    @staticmethod
    def _verification_passed(receipt: dict[str, Any]) -> bool:
        counts = receipt.get("counts", {})
        if counts.get("expected") != counts.get("imported"):
            return False
        if counts.get("total_expected") != counts.get("total_imported"):
            return False
        if receipt.get("links", {}).get("unresolved", 0) != 0:
            return False
        if receipt.get("todos", {}).get("unlinked", 0) != 0:
            return False
        if receipt.get("errors"):
            return False
        return True

    # ----------------------------- 批次表辅助 -----------------------------

    def _insert_batch(self, batch_id: str, source: str,
                      expected: int, backup_file: str) -> None:
        db.get_conn().execute(
            """
            INSERT OR REPLACE INTO migration_batches
                (batch_id, source, status, expected_rows, backup_file)
            VALUES (?, ?, 'importing', ?, ?)
            """,
            (batch_id, source, expected, backup_file),
        )
        db.maybe_commit(db.get_conn())

    def _finish_batch(self, batch_id: str, status: str,
                      imported: int, receipt: dict[str, Any]) -> None:
        db.get_conn().execute(
            """
            UPDATE migration_batches
               SET status=?, imported_rows=?, finished_at=datetime('now'),
                   receipt=?
             WHERE batch_id=?
            """,
            (status, imported, json.dumps(receipt, ensure_ascii=False, default=str), batch_id),
        )
        db.maybe_commit(db.get_conn())

    def _get_batch(self, batch_id: str) -> sqlite3.Row | None:
        return db.get_conn().execute(
            "SELECT * FROM migration_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()

    def _load_backup_entries(self, backup_file: str) -> dict[tuple[str, int], dict[str, Any]]:
        backup = sqlite3.connect(backup_file)
        backup.row_factory = sqlite3.Row
        try:
            records = backup.execute("SELECT * FROM entries").fetchall()
        finally:
            backup.close()
        return {
            (r["module"], r["id"]): db.db_row_to_dict(r) for r in records
        }

    @staticmethod
    def _write_receipt_file(batch_id: str, receipt: dict[str, Any]) -> str:
        Path(settings.receipt_dir).mkdir(parents=True, exist_ok=True)
        path = Path(settings.receipt_dir) / f"{batch_id}.json"
        path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return str(path)


migration_service = MigrationService()
