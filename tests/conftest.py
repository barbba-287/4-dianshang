"""S1+ 测试全局配置。

模块顶层强制设置 SQLite 数据库 URL，绕过 .env 的 MySQL 配置，避免
`app.db` import 时尝试创建 MySQL engine。

每个测试自动隔离 env 与清缓存；具体的模块 reload 由各 test 文件的
fixture 控制，避免与显式 fixture 冲突。
"""

import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["ARTIFACTS_DIR"] = "artifacts"
os.environ["WORKER_LEASE_SECONDS"] = "30"
os.environ["EMPLOYEE_AUTH_ENABLED"] = "false"
os.environ["DEMO_MODE_ENABLED"] = "true"

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    uploads_dir = tmp_path / "uploads"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IMPORTS_DIR", str(uploads_dir))
    monkeypatch.setenv("WORKER_LEASE_SECONDS", "30")
    yield


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """每次测试前清缓存，让 env 在下一次 get_settings() 调用时生效。"""
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()