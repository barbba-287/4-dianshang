"""Alembic 迁移测试。

覆盖：
- 全新 SQLite 通过 alembic upgrade head 创建全部 7 张表；
- alembic current / history 命令可用；
- alembic_version 表存在并指向 head。
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from app.config import get_settings
from app.db import engine


def _run_alembic(args: list[str], *, database_url: str) -> subprocess.CompletedProcess:
    """在临时数据库上调用 alembic 子命令。"""
    import os

    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd="D:\\ai\\dianshang",
    )


def _new_temp_sqlite(tmp_path: Path) -> str:
    db_path = tmp_path / "alembic.db"
    return f"sqlite:///{db_path}"


def test_alembic_upgrade_head_creates_all_tables(tmp_path):
    db_url = _new_temp_sqlite(tmp_path)
    result = _run_alembic(["upgrade", "head"], database_url=db_url)
    assert result.returncode == 0, result.stdout + result.stderr

    # 直接连这个临时数据库检查
    from sqlalchemy import create_engine

    eng = create_engine(db_url)
    inspector = inspect(eng)
    tables = set(inspector.get_table_names())
    expected = {
        "products",
        "product_price_history",
        "crawl_jobs",
        "documents",
        "document_versions",
        "document_chunks",
        "document_product_links",
        "alembic_version",
    }
    assert expected.issubset(tables), f"missing: {expected - tables}"


def test_alembic_current_shows_head(tmp_path):
    db_url = _new_temp_sqlite(tmp_path)
    # 先升级一次
    upgrade = _run_alembic(["upgrade", "head"], database_url=db_url)
    assert upgrade.returncode == 0

    current = _run_alembic(["current"], database_url=db_url)
    assert current.returncode == 0
    assert "0001_baseline" in current.stdout
    assert "head" in current.stdout


def test_alembic_upgrade_is_idempotent(tmp_path):
    db_url = _new_temp_sqlite(tmp_path)
    first = _run_alembic(["upgrade", "head"], database_url=db_url)
    assert first.returncode == 0
    second = _run_alembic(["upgrade", "head"], database_url=db_url)
    assert second.returncode == 0


def test_alembic_stamp_head_marks_existing_db(tmp_path):
    """已有表但 alembic_version 不存在的场景：stamp head 不重建表。"""
    db_url = _new_temp_sqlite(tmp_path)
    # 手工建一张表模拟 week1 legacy
    from sqlalchemy import create_engine

    eng = create_engine(db_url)
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE products (id INTEGER PRIMARY KEY)"))
    stamp = _run_alembic(["stamp", "head"], database_url=db_url)
    assert stamp.returncode == 0, stamp.stdout + stamp.stderr

    # 表还在（没被删）
    with eng.connect() as conn:
        rows = list(conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")))
    assert any("products" in r[0] for r in rows)


def test_init_db_py_uses_alembic(tmp_path, monkeypatch):
    """python init_db.py 通过 alembic upgrade head 工作。"""
    db_path = tmp_path / "init.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)

    import subprocess

    result = subprocess.run(
        [sys.executable, "init_db.py"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd="D:\\ai\\dianshang",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "head" in result.stdout.lower() or "升级" in result.stdout or "upgrade" in result.stdout.lower()

    eng = __import__("sqlalchemy").create_engine(db_url)
    tables = set(inspect(eng).get_table_names())
    assert "products" in tables
    assert "documents" in tables


def test_init_db_py_on_legacy_db_uses_stamp(tmp_path, monkeypatch):
    """legacy 库（业务表存在但无 alembic_version）→ init_db.py 走 stamp head。"""
    db_path = tmp_path / "legacy.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)

    from sqlalchemy import create_engine

    eng = create_engine(db_url)
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE products (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE crawl_jobs (id INTEGER PRIMARY KEY)"))
    assert "alembic_version" not in set(inspect(eng).get_table_names())

    result = subprocess.run(
        [sys.executable, "init_db.py"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd="D:\\ai\\dianshang",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "legacy" in result.stdout.lower() or "stamp" in result.stdout.lower()

    # 表还在（没被删）+ alembic_version 被创建
    insp = inspect(eng)
    tables = set(insp.get_table_names())
    assert "products" in tables
    assert "alembic_version" in tables


def test_init_db_py_on_migrated_db_is_idempotent(tmp_path, monkeypatch):
    """已迁移库 → init_db.py 走 upgrade head，幂等。"""
    db_path = tmp_path / "migrated.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)

    # 先跑一次升级
    first = _run_alembic(["upgrade", "head"], database_url=db_url)
    assert first.returncode == 0

    # 再用 init_db.py 跑一次
    result = subprocess.run(
        [sys.executable, "init_db.py"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd="D:\\ai\\dianshang",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "已迁移" in result.stdout or "migrated" in result.stdout.lower() or "幂等" in result.stdout