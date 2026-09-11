"""商品表现四象限 API 回归测试。"""
from __future__ import annotations

import importlib
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'quadrant.db'}")
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
        workspace = db_mod.Workspace(tenant_key="quadrant", name="四象限测试")
        db.add(workspace); db.flush()
        user = db_mod.UserAccount(login="operator", password_hash=hash_password("operator-password"), display_name="运营")
        db.add(user); db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="operations"))
        warehouse = db_mod.Warehouse(workspace_id=workspace.id, code="W-Q", name="四象限仓", warehouse_type="own")
        db.add(warehouse); db.flush()
        product = upsert_product(
            db, ProductRecord(source="fixture", external_product_id="p-q", title="商品Q", url="https://e/p-q", current_price=Decimal("10.00"), observed_at=date.today()),
            workspace_id=workspace.id,
        )
        db.flush()
        for offset in range(3):
            sku = db_mod.ProductSku(workspace_id=workspace.id, product_id=product.id, sku_code=f"S-Q-{offset}")
            db.add(sku); db.flush()
            db.add(db_mod.InventoryPolicy(workspace_id=workspace.id, warehouse_id=warehouse.id, sku_id=sku.id, safety_stock_qty=5, reorder_point_qty=8))
            db.add(db_mod.InventoryBalance(workspace_id=workspace.id, warehouse_id=warehouse.id, sku_id=sku.id, on_hand_qty=20))
        db.commit()
        sku_ids = [sku.id for sku in db.scalars(select(db_mod.ProductSku).where(db_mod.ProductSku.workspace_id == workspace.id)).all()]
    client_ctx = TestClient(main_mod.app)
    login = client_ctx.post("/login", data={"login": "operator", "password": "operator-password", "next": "/dashboard"}, follow_redirects=False)
    assert login.status_code == 303
    csrf = client_ctx.cookies.get("dianshang_csrf")
    assert csrf
    client_ctx.headers.update({"X-CSRF-Token": csrf})
    try:
        yield client_ctx, db_mod, workspace.id, warehouse.id, sku_ids
    finally:
        bg_mod.reset_executor(); cfg.get_settings.cache_clear()


def _seed_sales(db_mod, *, workspace_id, sku_id, account_ref, daily_qty, days, end):
    with db_mod.SessionLocal() as db:
        account = db_mod.ExternalAccount(workspace_id=workspace_id, platform="jd", account_ref=account_ref, store_ref="s")
        db.add(account); db.flush()
        for offset in range(days):
            db.add(db_mod.DailySkuSale(
                workspace_id=workspace_id, external_account_id=account.id, platform="jd",
                account_ref=account_ref, store_ref="s", external_sku=f"S-{account_ref}",
                internal_sku_id=sku_id, sales_date=end - timedelta(days=offset),
                gross_qty=daily_qty, cancelled_qty=0, refunded_qty=0, net_qty=daily_qty,
                gross_amount=Decimal("10.00"), refund_amount=Decimal("0"), net_amount=Decimal("10.00"),
                order_count=1, data_completeness="complete",
            ))
        db.commit()


def test_product_quadrant_returns_summary(client):
    c, _db, _ws, _wh, _ids = client
    response = c.get("/api/analytics/product-quadrant")
    assert response.status_code == 200
    body = response.json()
    assert body["business_timezone"] == "UTC"
    assert body["summary"]["total_scored"] == 3
    assert body["items"]


def test_product_quadrant_invalid_window_rejected(client):
    c, _db, _ws, _wh, _ids = client
    response = c.get("/api/analytics/product-quadrant", params={"growth_window": 5})
    assert response.status_code == 422


def test_product_quadrant_classifies_focal_supplement(client):
    c, db_mod, workspace_id, _wh, sku_ids = client
    _seed_sales(db_mod, workspace_id=workspace_id, sku_id=sku_ids[0], account_ref="focal", daily_qty=10, days=21, end=date.today())
    body = c.get("/api/analytics/product-quadrant").json()
    item = next(item for item in body["items"] if item["sku_id"] == sku_ids[0])
    assert item["growth_rate"] is not None
    assert item["days_of_inventory"] is not None
    assert item["quadrant"] in {"focal_supplement", "healthy", "watch", "slow_risk"}


def test_product_quadrant_marks_insufficient_when_window_missing(client):
    c, db_mod, workspace_id, _wh, sku_ids = client
    _seed_sales(db_mod, workspace_id=workspace_id, sku_id=sku_ids[1], account_ref="partial", daily_qty=1, days=3, end=date.today())
    body = c.get("/api/analytics/product-quadrant").json()
    item = next(item for item in body["items"] if item["sku_id"] == sku_ids[1])
    assert item["quadrant"] == "insufficient"
    assert "growth_data_insufficient" in body["unsupported_metrics"]
    assert "days_data_insufficient" in body["unsupported_metrics"]


def test_warehouse_id_outside_workspace_returns_404(client):
    c, db_mod, _ws, _wh, _ids = client
    with db_mod.SessionLocal() as db:
        other = db_mod.Workspace(tenant_key="out", name="外部")
        db.add(other); db.flush()
        warehouse = db_mod.Warehouse(workspace_id=other.id, code="W-OUT", name="外部仓", warehouse_type="own")
        db.add(warehouse); db.commit()
        other_id = warehouse.id
    response = c.get("/api/analytics/product-quadrant", params={"warehouse_id": other_id})
    assert response.status_code == 404
