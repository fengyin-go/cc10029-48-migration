"""测试公共夹具：每个测试用例使用独立临时 SQLite 数据库与回执目录。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import database
from app.config import settings


@pytest.fixture(autouse=True)
def isolated_sqlite(tmp_path: Path):
    db_file = tmp_path / "test-workbench.sqlite3"
    receipt_dir = tmp_path / "migration"
    # settings 是 frozen dataclass，用 object.__setattr__ 临时改写回执目录
    original_receipt_dir = settings.receipt_dir
    object.__setattr__(settings, "receipt_dir", str(receipt_dir))
    database.configure(str(db_file))
    # 重新初始化内存仓库（会在空库上灌入示例数据，与旧行为一致）
    from app import store as store_module
    store_module.store.bootstrap()
    yield
    object.__setattr__(settings, "receipt_dir", original_receipt_dir)
    database.configure(None)
