"""持久化切换与工作流接口。

沿用原业务入口（/api/{module} 资源路由不变），本路由只新增切换日所需能力：
- 存量迁移导入、回执查询、复核、回滚；
- 幂等分发（分发者中断重试取回原结果）；
- 待办收件箱、认领资格、幂等受理。
固定路径必须在动态资源路由之前注册（见 app/main.py）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.migration import migration_service
from app.services.workflow import workflow_service

router = APIRouter(prefix="/api", tags=["持久化切换与工作流"])


class ImportPayload(BaseModel):
    source: str | None = None
    exported_at: str | None = None
    records: dict[str, list[dict[str, Any]]] | list[dict[str, Any]] = Field(
        default_factory=dict
    )
    todos: list[dict[str, Any]] = Field(default_factory=list)


class ImportRequest(BaseModel):
    payload: ImportPayload
    batch_id: str | None = None
    dry_run: bool = False


class DispatchRequest(BaseModel):
    module: str
    entry_id: int
    action: str
    dispatch_key: str | None = None
    actor: str | None = None


class ClaimRequest(BaseModel):
    module: str
    entry_id: int
    teacher: str | None = None
    action: str | None = None
    todo_code: str | None = None


@router.post("/migration/imports")
def create_import(request: ImportRequest) -> dict[str, Any]:
    """导入旧工作台存量记录并出具切换回执；验证不通过自动回滚。"""
    try:
        receipt = migration_service.import_export(
            request.payload.model_dump(),
            batch_id=request.batch_id,
            source=request.payload.source,
            dry_run=request.dry_run,
        )
    except Exception as exc:  # 事务已回滚，把失败原因返回给操作员
        raise HTTPException(status_code=400, detail=f"迁移失败并已回滚：{exc}") from exc
    return receipt


@router.get("/migration/batches")
def list_batches() -> dict[str, Any]:
    return {"items": migration_service.list_batches()}


@router.get("/migration/batches/{batch_id}")
def get_batch_receipt(batch_id: str) -> dict[str, Any]:
    try:
        return migration_service.get_receipt(batch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/migration/batches/{batch_id}/verify")
def verify_batch(batch_id: str) -> dict[str, Any]:
    try:
        return migration_service.verify_batch(batch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/migration/batches/{batch_id}/rollback")
def rollback_batch(batch_id: str) -> dict[str, Any]:
    try:
        return migration_service.rollback(batch_id, reason="操作员发起回滚")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/dispatch")
def dispatch(request: DispatchRequest) -> dict[str, Any]:
    """幂等动作分发：同一 dispatch_key 重试只取回首次结果。"""
    return workflow_service.dispatch(
        request.module, request.entry_id, request.action,
        dispatch_key=request.dispatch_key, actor=request.actor,
    )


@router.get("/dispatch")
def list_dispatch(module: str | None = None) -> dict[str, Any]:
    from app.store import store
    return {"items": store.list_dispatch(module)}


@router.get("/todos")
def list_todos(module: str | None = None, status: str | None = None) -> dict[str, Any]:
    return {"items": workflow_service.list_todos(module=module, status=status)}


@router.get("/{module}/{entry_id}/eligibility")
def entry_eligibility(module: str, entry_id: int) -> dict[str, Any]:
    return workflow_service.eligibility(module, entry_id)


@router.post("/claims")
def claim(request: ClaimRequest) -> dict[str, Any]:
    """幂等受理：已受理的待办重试返回原结果，两边入口状态一致。"""
    return workflow_service.claim(
        request.module, request.entry_id,
        teacher=request.teacher, action=request.action,
        todo_code=request.todo_code,
    )


@router.get("/claims")
def list_claims(module: str | None = None) -> dict[str, Any]:
    from app.store import store
    return {"items": store.list_claims(module)}
