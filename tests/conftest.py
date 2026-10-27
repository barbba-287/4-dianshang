"""S1+ 测试全局配置。

模块顶层强制设置 SQLite 数据库 URL，绕过 .env 的 MySQL 配置，避免
`app.db` import 时尝试创建 MySQL engine。

每个测试自动隔离 env 与清缓存；具体的模块 reload 由各 test 文件的
fixture 控制，避免与显式 fixture 冲突。

同时为测试用的 SQLite 连接开启 WAL 日志模式（见 `_sqlite_wal_mode`）：
本机（Windows）默认的 `journal_mode=delete` 会为每次 DDL 创建/删除一个
`-journal` 回滚日志文件，建 43 张表约需 10.5s；改为 WAL 后约 0.04s。
全量测试每个用例都会建库，因此该差异会把整套测试从 ~39 分钟压到 ~4 分钟。
该设置仅作用于测试进程，不影响生产库配置。
"""

import os
import sqlite3
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["ARTIFACTS_DIR"] = "artifacts"
os.environ["WORKER_LEASE_SECONDS"] = "30"
os.environ["EMPLOYEE_AUTH_ENABLED"] = "false"
os.environ["DEMO_MODE_ENABLED"] = "true"

# Shopify live 通道固定为「关闭」基线，防止测试结果随仓库外 `.env` 漂移。
# `Settings` 用 `env_file=".env"`（app/config.py），仓库外的本地 `.env` 若写了
# SHOPIFY_LIVE_ENABLED=true / 真 token，测试进程就会读到它 —— 环境变量优先级高于
# dotenv，这里统一压死。取值以 `app/config.py` 的既有默认值为准，不另行发明。
# 需要非默认值的测试（如 tests/test_p2a_sync_guardrails.py 的 harness）自行
# monkeypatch 覆盖，既有做法不变。
os.environ["SHOPIFY_LIVE_ENABLED"] = "false"
os.environ["SHOPIFY_STORE_DOMAIN"] = ""
os.environ["SHOPIFY_ACCESS_TOKEN"] = ""
os.environ["SHOPIFY_SYNC_BUDGET_SECONDS"] = "300"
os.environ["SHOPIFY_MAX_PAGES"] = "100"
os.environ["SHOPIFY_API_VERSION"] = "2026-07"

# 让 subprocess 启动的子进程（test_alembic.py、部分 CLI 测试）也拿到 WAL 设置：
# Python 启动时会自动导入 sys.path 上第一个 sitecustomize，故把本目录前置。
_WAL_BOOTSTRAP_DIR = str(Path(__file__).parent / "_wal_bootstrap")
_existing_pythonpath = os.environ.get("PYTHONPATH", "")
if _WAL_BOOTSTRAP_DIR not in _existing_pythonpath.split(os.pathsep):
    os.environ["PYTHONPATH"] = (
        f"{_WAL_BOOTSTRAP_DIR}{os.pathsep}{_existing_pythonpath}"
        if _existing_pythonpath
        else _WAL_BOOTSTRAP_DIR
    )

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine


@event.listens_for(Engine, "connect")
def _enable_sqlite_wal(dbapi_connection, _connection_record):
    """给测试进程内所有 SQLite 连接开启 WAL。

    挂在 ``Engine`` 基类上而非某个 engine 实例：各测试文件会用
    ``importlib.reload(app.db)`` 重建 engine，实例级监听会随之失效。
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    uploads_dir = tmp_path / "uploads"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IMPORTS_DIR", str(uploads_dir))
    monkeypatch.setenv("WORKER_LEASE_SECONDS", "30")
    # 与模块级保持同一套 Shopify 关闭基线：monkeypatch 会连同仓库外 `.env`
    # 可能带来的真值一起覆盖，保证单个用例也读不到本地 `.env`。
    monkeypatch.setenv("SHOPIFY_LIVE_ENABLED", "false")
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "")
    monkeypatch.setenv("SHOPIFY_SYNC_BUDGET_SECONDS", "300")
    monkeypatch.setenv("SHOPIFY_MAX_PAGES", "100")
    monkeypatch.setenv("SHOPIFY_API_VERSION", "2026-07")
    yield


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """每次测试前清缓存，让 env 在下一次 get_settings() 调用时生效。"""
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()