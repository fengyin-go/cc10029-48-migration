"""工作流服务：幂等分发、待办收件箱同步、认领资格与受理。

解决切换日的在途问题：
- 分发（如纪要分发给教师、备课签阅触发评议）走分发台账，带幂等键；
  分发者中断后用同一幂等键重试，直接拿回首次处理结果，不会再派一项；
- 派生出的待办写入 todo_inbox，与业务记录通过 (module, entry_id, origin_module) 关联；
  重复导入/重复触发只更新不新增，待认领事项不会因停用旧内存服务而被全部重发；
- 认领统一收敛到 claim_ledger：诊改任务入口和待办入口看到的是同一份认领资格，
  已受理的待办在任务入口同步变为不可再领，重试受理返回原结果。
"""
from __future__ import annotations

from typing import Any

from app import database as db
from app.store import store

# 来源模块 -> 派生待办所在模块、文本标记、待办标题取值字段
ORIGIN_CHAINS: dict[str, dict[str, Any]] = {
    "review_meeting": {
        "target_module": "improve_task",
        "marker": "meeting",
        "scan_fields": ("任务描述", "备注说明"),
        "title_field": "任务描述",
        "title": "会议决议待认领诊改任务",
    },
    "lesson_prep": {
        "target_module": "peer_review",
        "marker": "lesson_prep",
        "scan_fields": ("评议意见", "备注说明"),
        "title_field": "评议意见",
        "title": "备课出处的同行评议邀请",
    },
}

# 各模块“受理/认领”默认执行的动作；None 表示只登记受理，不改业务状态（邀请仍可继续办理）。
DEFAULT_CLAIM_ACTION = {
    "improve_task": "认领任务",
}
# 各模块可受理的状态；终端状态视为已处理、不可再领。
CLAIMABLE_STATUS = {
    "improve_task": ("草稿", "待认领"),
    "peer_review": ("草稿", "待汇总", "已汇总"),
}
TERMINAL_STATUS = {
    "improve_task": ("已验收",),
    "peer_review": ("已公示",),
}


class WorkflowService:
    # ------------------------------- 幂等分发 -------------------------------

    def dispatch(self, module: str, entry_id: int, action: str, *,
                 dispatch_key: str | None = None,
                 actor: str | None = None) -> dict[str, Any]:
        """带台账的动作分发；同幂等键重试返回首次结果，不重复执行。"""
        key = dispatch_key or f"{module}:{entry_id}:{action}"
        existing = store.get_dispatch(key)
        if existing is not None:
            result = existing["result"]
            return {
                "ok": True,
                "idempotent_replay": True,
                "dispatch_key": key,
                "module": module,
                "entry_id": entry_id,
                "action": action,
                "message": f"分发处理结果已存在，直接取回原结果（{key}）",
                "entry": result.get("entry"),
                "first_created_at": existing["created_at"],
            }

        from app.services import service_for
        service = service_for(module)
        if service is None:
            return {"ok": False, "message": f"未知业务模块：{module}", "dispatch_key": key}

        with db.transaction():
            entry, message = service.run_action(entry_id, action)
            if entry is None:
                # 业务校验未通过：不记台账，调用方修正后可重试。
                return {"ok": False, "dispatch_key": key, "message": message}
            synced = self.sync_todos(module, entry_id)
            store.record_dispatch(key, module, entry_id, action,
                                  {"entry": entry, "message": message, "actor": actor})

        return {
            "ok": True,
            "idempotent_replay": False,
            "dispatch_key": key,
            "module": module,
            "entry_id": entry_id,
            "action": action,
            "message": message,
            "entry": entry,
            "todos": synced,
        }

    # ------------------------------- 待办同步 -------------------------------

    def ensure_todo(self, *, origin_module: str, origin_ref: str,
                    target_module: str, entry_id: int, title: str,
                    source_code: str | None = None,
                    source_ref: str | None = None) -> dict[str, Any]:
        """按确定性幂等键登记待办；已存在则取回，不重复派单。"""
        todo_code = f"{origin_module}:{origin_ref}:{target_module}"
        return store.upsert_todo({
            "todo_code": todo_code,
            "module": target_module,
            "entry_id": int(entry_id),
            "title": title,
            "origin_module": origin_module,
            "origin_ref": str(origin_ref),
            "source_code": source_code,
            "source_ref": source_ref or str(origin_ref),
            "status": "待认领",
        })

    def sync_todos(self, origin_module: str, origin_id: int) -> list[dict[str, Any]]:
        """扫描某来源动作派生的业务记录，补齐对应待办（幂等）。"""
        chain = ORIGIN_CHAINS.get(origin_module)
        if chain is None:
            return []
        marker = f"{chain['marker']}:{origin_id}"
        target_rows = store.rows(chain["target_module"])
        synced: list[dict[str, Any]] = []
        for row in target_rows:
            if not any(marker in str(row.get(field, "")) for field in chain["scan_fields"]):
                continue
            origin_entry = store.find(origin_module, origin_id)
            todo = self.ensure_todo(
                origin_module=origin_module,
                origin_ref=origin_id,
                target_module=chain["target_module"],
                entry_id=int(row["id"]),
                title=str(row.get(chain["title_field"], "")) or chain["title"],
                source_code=origin_entry.get("source_code") if origin_entry else None,
                source_ref=origin_entry.get("source_ref") if origin_entry else None,
            )
            synced.append(todo)
        return synced

    # ------------------------------- 认领资格 -------------------------------

    def eligibility(self, module: str, entry_id: int) -> dict[str, Any]:
        """任务入口与待办入口共用的认领资格视图。"""
        entry = store.find(module, entry_id)
        if entry is None:
            return {"module": module, "entry_id": entry_id, "can_claim": False,
                    "reason": "记录不存在或已归档"}
        claim = store.find_claim_for_entry(module, entry_id)
        if claim is not None:
            return {"module": module, "entry_id": entry_id, "can_claim": False,
                    "reason": "该待办已受理，认领资格已占用",
                    "status": entry.get("status"), "claim": claim}
        current = str(entry.get("status", ""))
        if current in TERMINAL_STATUS.get(module, ()):
            return {"module": module, "entry_id": entry_id, "can_claim": False,
                    "reason": f"当前状态为{current}，流程已结束", "status": current}
        allowed = CLAIMABLE_STATUS.get(module)
        if allowed is not None and current not in allowed:
            return {"module": module, "entry_id": entry_id, "can_claim": False,
                    "reason": f"当前状态为{current}，不在可认领状态{ '/'.join(allowed) }",
                    "status": current}
        return {"module": module, "entry_id": entry_id, "can_claim": True,
                "reason": "可认领", "status": current}

    def claim(self, module: str, entry_id: int, *,
              teacher: str | None = None,
              action: str | None = None,
              todo_code: str | None = None) -> dict[str, Any]:
        """受理待办（幂等）。已受理时重试取回原结果，不再派一项。"""
        claim_key = f"{module}:{entry_id}"
        existing = store.get_claim(claim_key)
        if existing is not None:
            return {
                "ok": True,
                "idempotent_replay": True,
                "claim_key": claim_key,
                "module": module,
                "entry_id": entry_id,
                "message": "该待办此前已受理，取回原受理结果，未重复认领",
                "entry": existing["result"].get("entry"),
                "first_created_at": existing["created_at"],
            }

        eligibility = self.eligibility(module, entry_id)
        if not eligibility["can_claim"]:
            return {"ok": False, "claim_key": claim_key,
                    "message": eligibility["reason"], **eligibility}

        from app.services import service_for
        service = service_for(module)
        target_action = action if action is not None else DEFAULT_CLAIM_ACTION.get(module)

        with db.transaction():
            result_entry: dict[str, Any] | None = None
            message = "受理成功"
            if target_action and service is not None:
                result_entry, message = service.run_action(entry_id, target_action)
                if result_entry is None:
                    return {"ok": False, "claim_key": claim_key, "message": message}
            else:
                result_entry = store.find(module, entry_id)
            # 同一业务记录关联的全部待办同步为已认领（任务入口与待办入口一致）
            for todo in store.list_todos(module=module):
                if todo.get("entry_id") == entry_id and todo["status"] == "待认领":
                    store.mark_todo(todo["todo_code"], "已认领", teacher)
            if todo_code:
                store.mark_todo(todo_code, "已认领", teacher)
            store.record_claim(
                claim_key, module, entry_id,
                target_action or "受理登记",
                {"entry": result_entry, "message": message, "teacher": teacher},
                todo_code=todo_code, teacher=teacher,
            )

        return {
            "ok": True,
            "idempotent_replay": False,
            "claim_key": claim_key,
            "module": module,
            "entry_id": entry_id,
            "action": target_action,
            "message": message,
            "entry": result_entry,
        }

    def list_todos(self, module: str | None = None,
                   status: str | None = None) -> list[dict[str, Any]]:
        """待办列表：附带认领资格、业务状态和出处，供两个入口对照。"""
        todos = store.list_todos(module=module, status=status)
        enriched: list[dict[str, Any]] = []
        for todo in todos:
            item = dict(todo)
            entry_id = todo.get("entry_id")
            if entry_id is not None:
                entry = store.find(todo["module"], int(entry_id))
                item["entry_status"] = entry.get("status") if entry else None
                item["can_claim"] = self.eligibility(
                    todo["module"], int(entry_id)
                )["can_claim"]
                item["claim"] = store.find_claim_for_entry(
                    todo["module"], int(entry_id)
                )
            else:
                item["can_claim"] = False
                item["claim"] = None
            enriched.append(item)
        return enriched


workflow_service = WorkflowService()
