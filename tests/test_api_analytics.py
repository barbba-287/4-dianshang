"""BI 指标层 API 回归测试。"""
from __future__ import annotations

import importlib
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'analytics.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IMPORTS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    import app.config as cfg
    import app.db as db_mod
    import app.background as bg_mod
    import app.main as main_mod
    cfg.get_settings.cache_clear()
    importlib.reload(cfg); importlib.reload(db_mod); importlib.reload(bg_mod); importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    from app.employee_auth import hash_password
    from app.schemas import ProductRecord
    from app.repository import upsert_product
    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="analytics", name="分析测试")
        db.add(workspace); db.flush()
        user = db_mod.UserAccount(login="operator", password_hash=hash_password("operator-password"), display_name="运营")
        db.add(user); db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="operations"))
        warehouse = db_mod.Warehouse(workspace_id=workspace.id, code="W-A", name="分析仓", warehouse_type="own")
        db.add(warehouse); db.flush()
        product = upsert_product(
            db, ProductRecord(source="fixture", external_product_id="a-p", title="分析商品", url="https://e/a", current_price=Decimal("10.00"), observed_at=date.today()),
            workspace_id=workspace.id,
        )
        db.flush()
        sku = db_mod.ProductSku(workspace_id=workspace.id, product_id=product.id, sku_code="SKU-A")
        db.add(sku); db.flush()
        db.add(db_mod.InventoryPolicy(workspace_id=workspace.id, warehouse_id=warehouse.id, sku_id=sku.id, safety_stock_qty=5, reorder_point_qty=8))
        db.add(db_mod.InventoryBalance(workspace_id=workspace.id, warehouse_id=warehouse.id, sku_id=sku.id, on_hand_qty=20))
        db.commit()
    client_ctx = TestClient(main_mod.app)
    login = client_ctx.post("/login", data={"login": "operator", "password": "operator-password", "next": "/dashboard"}, follow_redirects=False)
    assert login.status_code == 303
    csrf = client_ctx.cookies.get("dianshang_csrf")
    assert csrf
    client_ctx.headers.update({"X-CSRF-Token": csrf})
    try:
        yield client_ctx, db_mod, workspace.id, warehouse.id
    finally:
        bg_mod.reset_executor(); cfg.get_settings.cache_clear()


def test_analytics_sales_returns_three_windows(client):
    c, _db, _ws, _wh = client
    response = c.get("/api/analytics/sales")
    assert response.status_code == 200
    body = response.json()
    assert set(body["windows"].keys()) == {"7", "14", "30"}
    assert body["as_of"]
    assert body["limitations"]


def test_analytics_inventory_health_summary_with_healthy_sku(client):
    c, _db, _ws, _wh = client
    response = c.get("/api/analytics/inventory-health")
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["total"] == 1
    assert body["page"] == 1
    assert body["page_size"] == 20
    assert body["items"][0]["on_hand_qty"] == 20


def test_analytics_inventory_health_warehouse_outside_returns_404(client):
    c, db_mod, _ws, _wh = client
    with db_mod.SessionLocal() as db:
        other = db_mod.Workspace(tenant_key="out", name="外部")
        db.add(other); db.flush()
        warehouse = db_mod.Warehouse(workspace_id=other.id, code="W-OUT-A", name="外部仓", warehouse_type="own")
        db.add(warehouse); db.commit()
        other_id = warehouse.id
    response = c.get("/api/analytics/inventory-health", params={"warehouse_id": other_id})
    assert response.status_code == 404
