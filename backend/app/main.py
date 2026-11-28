"""教学诊改协同台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import database
from app.config import settings
from app.routers import ROUTERS
from app.store import store


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动：确保表结构就绪、内存视图与 SQLite 一致；业务入口保持不变。
    database.init_schema()
    store.bootstrap()
    yield


app = FastAPI(title="教学诊改协同台", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、持久化数据库与业务模块就绪。"""
    return {
        "ok": True,
        "app": settings.app_name,
        "version": "2.0.0",
        "storage": "sqlite",
        "db_path": database.get_db_path(),
        "modules": len(store.module_names()),
        "entries": database.count_entries(),
    }


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()


# 资源路由含 /api/{module}，必须放在 health / overview 之后注册，
# 否则固定路径会被动态路径抢先匹配。
for module in ROUTERS:
    app.include_router(module.router)
