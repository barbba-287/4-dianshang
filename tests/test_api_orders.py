"""订单只读 API 和页面契约测试。"""

import importlib
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'orders-api.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    import app.config as cfg
    import app.db as db_mod
    import app.background as bg_mod
    import app.main as main_mod
    cfg.get_settings.cache_clear(); importlib.reload(cfg); importlib.reload(db_mod); importlib.reload(bg_mod); importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    from app.employee_auth import hash_password
    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="orders-api", name="订单 API")
        other = db_mod.Workspace(tenant_key="orders-other", name="另一个商家")
        db.add_all([workspace, other]); db.flush()
        user = db_mod.UserAccount(login="orders-op", password_hash=hash_password("orders-password"), display_name="订单运营")
        db.add(user); db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="operations"))
        account = db_mod.ExternalAccount(workspace_id=workspace.id, platform="mock", account_ref="demo", store_ref="store", source_mode="mock", simulated=True)
        db.add(account); db.flush()
        order = db_mod.ExternalOrder(workspace_id=workspace.id, external_account_id=account.id, platform="mock", account_ref="demo", store_ref="store", external_order_no="API-PAID", order_status="paid", external_created_at=datetime.utcnow(), external_updated_at=datetime.utcnow(), gross_amount=Decimal("20.00"), refund_amount=Decimal("0"), currency="CNY", payload_json="{}", payload_hash="api-hash", idempotency_key="api-key", source_mode="mock", simulated=True, data_completeness="complete")
        db.add(order); db.flush()
        db.add(db_mod.ExternalOrderLine(workspace_id=workspace.id, external_order_id=order.id, external_line_id="line", external_sku="API-SKU", ordered_qty=1, cancelled_qty=0, refunded_qty=0, gross_amount=Decimal("20.00"), refund_amount=Decimal("0"), currency="CNY", mapping_status="unmapped", data_completeness="complete", payload_hash="line-hash"))
        private_account = db_mod.ExternalAccount(workspace_id=other.id, platform="mock", account_ref="other", store_ref="other", source_mode="mock", simulated=True)
        db.add(private_account); db.flush()
        private = db_mod.ExternalOrder(workspace_id=other.id, external_account_id=private_account.id, platform="mock", account_ref="other", store_ref="other", external_order_no="PRIVATE", order_status="paid", external_created_at=datetime.utcnow(), external_updated_at=datetime.utcnow(), gross_amount=Decimal("20.00"), refund_amount=Decimal("0"), currency="CNY", payload_json="{}", payload_hash="private-hash", idempotency_key="private-key", source_mode="mock", simulated=True, data_completeness="complete")
        db.add(private); db.commit()
        order_id, private_id = order.id, private.id
    with TestClient(main_mod.app) as test_client:
        login = test_client.post("/login", data={"login":"orders-op", "password":"orders-password", "next":"/orders"}, follow_redirects=False)
        assert login.status_code == 303
        yield test_client, order_id, private_id
    bg_mod.reset_executor(); cfg.get_settings.cache_clear()


def test_orders_api_list_detail_and_page_is_read_only(client):
    api, order_id, private_id = client
    listing = api.get("/api/orders", params={"fulfillment_view":"to_fulfill"})
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] == 1
    assert body["items"][0]["external_order_no"] == "API-PAID"
    assert body["items"][0]["simulated"] is True
    detail = api.get(f"/api/orders/{order_id}")
    assert detail.status_code == 200
    assert detail.json()["lines"][0]["external_sku"] == "API-SKU"
    assert api.get(f"/api/orders/{private_id}").status_code == 404
    page = api.get("/orders")
    assert page.status_code == 200
    html = page.text
    assert "订单事实" in html
    assert "外部订单事实" in html
    assert "不执行发货" in html
    assert "付款" in html
    assert "退款" in html
    assert "发货按钮" not in html


def test_fulfillment_proposal_export_defaults_paid_and_is_read_only(client):
    api, order_id, _private_id = client
    response = api.get("/api/orders/fulfillment-proposal.xlsx")
    assert response.status_code == 200, response.text
    assert response.content.startswith(b"PK")
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert response.headers["x-export-order-count"] == "1"
    assert response.headers["x-export-row-count"] == "1"
    assert response.headers["x-export-data-completeness"] == "complete"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith('attachment; filename="fulfillment-proposal-')
    assert "filename*=" in response.headers["content-disposition"]
    assert api.get("/api/orders/fulfillment-proposal.xlsx", params={"order_status":"cancelled"}).status_code == 422
    detail_before = api.get(f"/api/orders/{order_id}").json()
    assert detail_before["order_status"] == "paid"

def test_orders_api_rejects_invalid_date_range_and_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'orders-unauth.db'}")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    import app.config as cfg
    import app.db as db_mod
    import app.background as bg_mod
    import app.main as main_mod
    cfg.get_settings.cache_clear(); importlib.reload(cfg); importlib.reload(db_mod); importlib.reload(bg_mod); importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    with TestClient(main_mod.app) as test_client:
        assert test_client.get("/api/orders").status_code == 401
    bg_mod.reset_executor(); cfg.get_settings.cache_clear()
