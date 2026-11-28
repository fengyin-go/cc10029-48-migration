"""运行配置：端口、跨域、运行环境与持久化数据库位置。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_db_path() -> str:
    """数据库文件位置：优先环境变量，默认放在 backend/data/ 下，便于挂载持久卷。"""
    env_path = os.environ.get("APP_DB_PATH", "").strip()
    if env_path:
        return env_path
    # backend/app/config.py -> backend/data/workbench.sqlite3
    return str(Path(__file__).resolve().parent.parent / "data" / "workbench.sqlite3")


@dataclass(frozen=True)
class Settings:
    app_name: str = "教学诊改协同台"
    env: str = field(default_factory=lambda: os.environ.get("APP_ENV", "local"))
    port: int = 8000
    db_path: str = field(default_factory=_default_db_path)
    # 切换回执与备份文件统一放在数据库同级目录，便于核对与回滚。
    receipt_dir: str = field(
        default_factory=lambda: os.environ.get(
            "APP_RECEIPT_DIR",
            str(Path(_default_db_path()).resolve().parent / "migration"),
        )
    )
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200


settings = Settings()
