"""S5 Agent API 端到端测试。

覆盖：
- GET /api/agent/tools 返回只读工具列表；
- POST /api/agent/invoke 调用 search_products / get_product_detail / get_price_history；
- 负向：未知工具 / 错误参数 / 写入型工具被拒绝。
"""

import importlib
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.background import reset_executor


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IMPORTS_DIR", str(tmp_path / "uploads"))

    import app.config as cfg
    import app.db as db_mod
    import app.repository as repo_mod
    import app.background as bg_mod
    import app.agent as agent_mod
    import app.agent.impl as agent_impl_mod
    import app.main as main_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(repo_mod)
    importlib.reload(bg_mod)
    importlib.reload(agent_impl_mod)
    importlib.reload(agent_mod)
    importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    reset_executor()

    from app.schemas import ProductRecord

    record = ProductRecord(
        source="fixture",
        external_product_id="tea-001",
        title="高山绿茶 250g",
        url="https://fixture.local/tea-001",
        category="茶饮",
        description="清香回甘",
        rating=Decimal("4.8"),
        current_price=Decimal("89.90"),
        observed_at=datetime(2026, 8, 30, 12, 0, 0),
    )
    with db_mod.SessionLocal() as db:
        repo_mod.upsert_product(db, record)
        db.commit()

    with TestClient(main_mod.app) as c:
        yield c

    reset_executor()
    cfg.get_settings.cache_clear()


def test_list_agent_tools_returns_three_readonly(client):
    response = client.get("/api/agent/tools")
    assert response.status_code == 200
    tools = response.json()
    names = {t["name"] for t in tools}
    assert {"search_products", "get_product_detail", "get_price_history"}.issubset(names)
    for tool in tools:
        assert tool["is_readonly"] is True


def test_invoke_search_products_returns_product(client):
    # SQL LIKE 在 SQLite 上对中英混合可行；这里使用 category 精确匹配
    response = client.post(
        "/api/agent/invoke",
        json={"tool": "search_products", "input": {"category": "茶饮"}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["output"]["total"] >= 1
    assert any(item["external_product_id"] == "tea-001" for item in body["output"]["items"])


def test_invoke_get_product_detail_success(client):
    response = client.post(
        "/api/agent/invoke",
        json={"tool": "get_product_detail", "input": {"product_id": 1}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["output"]["title"] == "高山绿茶 250g"


def test_invoke_get_price_history_returns_items(client):
    response = client.post(
        "/api/agent/invoke",
        json={"tool": "get_price_history", "input": {"product_id": 1}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["output"]["items"]


def test_invoke_unknown_tool_returns_error(client):
    response = client.post(
        "/api/agent/invoke",
        json={"tool": "delete_inventory", "input": {}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] == "TOOL_NOT_FOUND"


def test_invoke_wrong_argument_type_returns_validation_error(client):
    response = client.post(
        "/api/agent/invoke",
        json={"tool": "get_product_detail", "input": {"product_id": "not-an-int"}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] == "TYPE_MISMATCH"


def test_invoke_missing_required_field(client):
    response = client.post(
        "/api/agent/invoke",
        json={"tool": "get_product_detail", "input": {}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] == "MISSING_FIELD"