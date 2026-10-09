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


def test_analytics_contract_exposes_metadata_and_unknown_decision_metrics(client):
    c, _db, workspace_id, warehouse_id = client

    sales = c.get("/api/analytics/sales")
    assert sales.status_code == 200
    sales_body = sales.json()
    assert sales_body["meta"]["workspace_id"] == workspace_id
    assert sales_body["meta"]["timezone"] == "UTC"
    assert sales_body["meta"]["metric_version"] == "1"
    assert sales_body["meta"]["metrics"][0]["source"] == "DailySkuSale"

    health = c.get("/api/analytics/inventory-health", params={"warehouse_id": warehouse_id})
    assert health.status_code == 200
    assert health.json()["meta"]["workspace_id"] == workspace_id
    assert health.json()["meta"]["metrics"][0]["warehouse_id"] == warehouse_id
    assert health.json()["meta"]["data_completeness"] == "insufficient"

    quadrant = c.get("/api/analytics/product-quadrant", params={"warehouse_id": warehouse_id})
    assert quadrant.status_code == 200
    assert quadrant.json()["meta"]["workspace_id"] == workspace_id

    decisions = c.get("/api/analytics/replenishment-decisions")
    assert decisions.status_code == 200
    decision_body = decisions.json()
    assert decision_body["meta"]["data_completeness"] == "unknown"
    assert decision_body["metrics"]["suggestion_adoption_rate"]["value"] is None
    assert decision_body["metrics"]["generation_to_inbound_confirmation_seconds"]["reason"] == "PURCHASE_REQUEST_INBOUND_LINK_MISSING"



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


def test_partial_sales_are_excluded_from_daily_average(client):
    c, db_mod, workspace_id, warehouse_id = client
    cutoff = date.today()
    complete_date = cutoff - __import__("datetime").timedelta(days=1)
    with db_mod.SessionLocal() as db:
        complete_account = db_mod.ExternalAccount(workspace_id=workspace_id, platform="jd", account_ref="complete", store_ref="store-a")
        partial_account = db_mod.ExternalAccount(workspace_id=workspace_id, platform="jd", account_ref="partial", store_ref="store-b")
        sku = db.scalar(__import__("sqlalchemy").select(db_mod.ProductSku).where(db_mod.ProductSku.workspace_id == workspace_id))
        db.add_all([complete_account, partial_account]); db.flush()
        complete_date = cutoff - __import__("datetime").timedelta(days=1)
        db.add_all([
            db_mod.DailySkuSale(workspace_id=workspace_id, external_account_id=complete_account.id, platform="jd", account_ref="complete", store_ref="store-a", external_sku="SKU-A", internal_sku_id=sku.id, sales_date=complete_date, gross_qty=1, cancelled_qty=0, refunded_qty=0, net_qty=1, gross_amount=Decimal("10.00"), refund_amount=Decimal("0"), net_amount=Decimal("10.00"), order_count=1, data_completeness="complete"),
            db_mod.DailySkuSale(workspace_id=workspace_id, external_account_id=partial_account.id, platform="jd", account_ref="partial", store_ref="store-b", external_sku="SKU-A", internal_sku_id=sku.id, sales_date=cutoff, gross_qty=99, cancelled_qty=0, refunded_qty=0, net_qty=99, gross_amount=Decimal("990.00"), refund_amount=Decimal("0"), net_amount=Decimal("990.00"), order_count=1, data_completeness="partial"),
        ])
        db.commit()

    health = c.get("/api/analytics/inventory-health", params={"as_of": cutoff.isoformat()})
    assert health.status_code == 200, health.text
    item = health.json()["items"][0]
    assert item["sales"]["14"]["daily_avg_qty"] == 1.0
    assert item["days_of_inventory"] == 20.0
    assert item["sales"]["14"]["sales_qty"] == 100

    sales = c.get("/api/analytics/sales", params={"as_of": cutoff.isoformat()})
    assert sales.status_code == 200, sales.text
    assert sales.json()["windows"]["7"]["net_qty"] == 100
    assert sales.json()["windows"]["7"]["daily_avg_qty"] is None


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
