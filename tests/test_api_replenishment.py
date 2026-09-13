"""补货 API 合同与权限回归测试。"""
from __future__ import annotations

import importlib
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api-replenishment.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IMPORTS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    import app.config as cfg
    import app.db as db_mod
    import app.repository as repo_mod
    import app.background as bg_mod
    import app.main as main_mod
    cfg.get_settings.cache_clear(); importlib.reload(cfg); importlib.reload(db_mod); importlib.reload(repo_mod); importlib.reload(bg_mod); importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    from app.employee_auth import hash_password
    from app.schemas import ProductRecord
    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="api-replenishment", name="补货测试")
        db.add(workspace); db.flush()
        user = db_mod.UserAccount(login="operator", password_hash=hash_password("operator-password"), display_name="运营")
        db.add(user); db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="operations"))
        warehouse = db_mod.Warehouse(workspace_id=workspace.id, code="W-API", name="API仓", warehouse_type="own")
        db.add(warehouse); db.flush()
        product = repo_mod.upsert_product(db, ProductRecord(source="fixture", external_product_id="api-p", title="商品", url="https://fixture.local/api-p", current_price=Decimal("10.00"), observed_at=__import__("datetime").datetime.utcnow()), workspace_id=workspace.id)
        db.flush()
        sku = db_mod.ProductSku(workspace_id=workspace.id, product_id=product.id, sku_code="API-SKU")
        db.add(sku); db.flush()
        db.add(db_mod.InventoryPolicy(workspace_id=workspace.id, warehouse_id=warehouse.id, sku_id=sku.id, safety_stock_qty=5, reorder_point_qty=8))
        db.add(db_mod.InventoryBalance(workspace_id=workspace.id, warehouse_id=warehouse.id, sku_id=sku.id, on_hand_qty=4))
        account = db_mod.ExternalAccount(workspace_id=workspace.id, platform="jd", account_ref="api", store_ref="store")
        db.add(account); db.flush()
        for offset in range(14):
            db.add(db_mod.DailySkuSale(workspace_id=workspace.id, external_account_id=account.id, platform="jd", account_ref="api", store_ref="store", external_sku="API-SKU", internal_sku_id=sku.id, sales_date=date.today() - timedelta(days=13-offset), gross_qty=3, cancelled_qty=0, refunded_qty=0, net_qty=3, gross_amount=Decimal("30.00"), refund_amount=Decimal("0"), net_amount=Decimal("30.00"), order_count=1, data_completeness="complete"))
        db.commit()
    return cfg, db_mod, bg_mod, main_mod, workspace.id, warehouse.id, sku.id


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg, db_mod, bg_mod, main_mod, workspace_id, warehouse_id, sku_id = _setup(monkeypatch, tmp_path)
    with TestClient(main_mod.app) as client:
        login = client.post("/login", data={"login":"operator", "password":"operator-password", "next":"/dashboard"}, follow_redirects=False)
        assert login.status_code == 303, login.text
        csrf = client.cookies.get("dianshang_csrf")
        assert csrf
        client.headers.update({"X-CSRF-Token": csrf})
        yield client, workspace_id, warehouse_id, sku_id
    bg_mod.reset_executor(); cfg.get_settings.cache_clear()


def test_replenishment_api_flow_and_inventory_unchanged(client):
    client, _workspace_id, warehouse_id, sku_id = client
    generated = client.post("/api/replenishment/suggestions/generate", json={"warehouse_id": warehouse_id, "sku_id": sku_id, "coverage_days": 14}, headers={"Idempotency-Key":"generate-1"})
    assert generated.status_code == 201, generated.text
    suggestion = generated.json()
    assert suggestion["status"] == "suggested"
    assert suggestion["suggested_qty"] == 43
    suggestion_id = suggestion["id"]
    decision = client.post(f"/api/replenishment/suggestions/{suggestion_id}/decision", json={"action":"confirm", "expected_version":1}, headers={"Idempotency-Key":"decision-api-1"})
    assert decision.status_code == 200, decision.text
    assert decision.json()["status"] == "confirmed"
    purchase = client.post("/api/purchase-requests", json={"suggestion_ids":[suggestion_id], "note":"补货申请"}, headers={"Idempotency-Key":"purchase-api-1"})
    assert purchase.status_code == 201, purchase.text
    assert purchase.json()["status"] == "submitted"
    replay = client.post("/api/purchase-requests", json={"suggestion_ids":[suggestion_id], "note":"补货申请"}, headers={"Idempotency-Key":"purchase-api-1"})
    assert replay.status_code == 201
    assert replay.json()["id"] == purchase.json()["id"]
    from app.db import SessionLocal, InventoryBalance, InventoryTransaction
    with SessionLocal() as db:
        assert db.scalar(__import__("sqlalchemy").select(InventoryBalance.on_hand_qty).where(InventoryBalance.warehouse_id == warehouse_id, InventoryBalance.sku_id == sku_id)) == 4
        assert (db.scalar(__import__("sqlalchemy").select(__import__("sqlalchemy").func.count(InventoryTransaction.id)).where(InventoryTransaction.warehouse_id == warehouse_id, InventoryTransaction.sku_id == sku_id)) or 0) == 0


def test_purchase_draft_api_lifecycle_and_idempotency(client):
    client, _workspace_id, warehouse_id, sku_id = client
    generated = client.post(
        "/api/replenishment/suggestions/generate",
        json={"warehouse_id": warehouse_id, "sku_id": sku_id, "coverage_days": 14},
        headers={"Idempotency-Key": "draft-generate-api"},
    )
    assert generated.status_code == 201, generated.text
    suggestion_id = generated.json()["id"]
    confirmed = client.post(
        f"/api/replenishment/suggestions/{suggestion_id}/decision",
        json={"action": "confirm", "expected_version": 1},
        headers={"Idempotency-Key": "draft-decision-api"},
    )
    assert confirmed.status_code == 200, confirmed.text

    created = client.post(
        "/api/purchase-requests/drafts",
        json={"suggestion_ids": [suggestion_id], "note": "待人工复核", "supplier_ref": "supplier-a"},
        headers={"Idempotency-Key": "draft-create-api"},
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["status"] == "draft"
    assert draft["version"] == 1
    assert draft["submitted_by"] is None

    replay = client.post(
        "/api/purchase-requests/drafts",
        json={"suggestion_ids": [suggestion_id], "note": "待人工复核", "supplier_ref": "supplier-a"},
        headers={"Idempotency-Key": "draft-create-api"},
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == draft["id"]

    edited = client.patch(
        f"/api/purchase-requests/{draft['id']}",
        json={"expected_version": 1, "note": "已复核", "supplier_ref": "supplier-b"},
        headers={"Idempotency-Key": "draft-edit-api"},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["version"] == 2
    assert edited.json()["note"] == "已复核"

    submitted = client.post(
        f"/api/purchase-requests/{draft['id']}/submit",
        json={"expected_version": 2},
        headers={"Idempotency-Key": "draft-submit-api"},
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "submitted"
    assert submitted.json()["version"] == 3

    submit_replay = client.post(
        f"/api/purchase-requests/{draft['id']}/submit",
        json={"expected_version": 2},
        headers={"Idempotency-Key": "draft-submit-api"},
    )
    assert submit_replay.status_code == 200
    assert submit_replay.json()["id"] == draft["id"]


def test_purchase_draft_api_rejects_idempotency_reuse_and_stale_version(client):
    client, _workspace_id, warehouse_id, sku_id = client
    generated = client.post(
        "/api/replenishment/suggestions/generate",
        json={"warehouse_id": warehouse_id, "sku_id": sku_id, "coverage_days": 14},
        headers={"Idempotency-Key": "draft-reuse-generate"},
    )
    suggestion_id = generated.json()["id"]
    assert client.post(
        f"/api/replenishment/suggestions/{suggestion_id}/decision",
        json={"action": "confirm", "expected_version": 1},
        headers={"Idempotency-Key": "draft-reuse-decision"},
    ).status_code == 200
    created = client.post(
        "/api/purchase-requests/drafts",
        json={"suggestion_ids": [suggestion_id], "note": "原始备注"},
        headers={"Idempotency-Key": "draft-reuse-create"},
    )
    assert created.status_code == 201
    draft_id = created.json()["id"]
    reused = client.post(
        "/api/purchase-requests/drafts",
        json={"suggestion_ids": [suggestion_id], "note": "不同备注"},
        headers={"Idempotency-Key": "draft-reuse-create"},
    )
    assert reused.status_code == 409
    assert reused.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSE"

    stale = client.patch(
        f"/api/purchase-requests/{draft_id}",
        json={"expected_version": 2, "note": "不应写入"},
        headers={"Idempotency-Key": "draft-stale-edit"},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "PURCHASE_REQUEST_VERSION_CONFLICT"
    current = client.get(f"/api/purchase-requests/{draft_id}")
    assert current.status_code == 200
    assert current.json()["version"] == 1
    assert current.json()["note"] == "原始备注"


def test_replenishment_suggestions_list_route_and_filter(client):
    client, _workspace_id, warehouse_id, sku_id = client
    generated = client.post(
        "/api/replenishment/suggestions/generate",
        json={"warehouse_id": warehouse_id, "sku_id": sku_id, "coverage_days": 14},
        headers={"Idempotency-Key": "list-route-generate"},
    )
    assert generated.status_code == 201, generated.text
    suggestion_id = generated.json()["id"]
    confirmed = client.post(
        f"/api/replenishment/suggestions/{suggestion_id}/decision",
        json={"action": "confirm", "expected_version": 1},
        headers={"Idempotency-Key": "list-route-decision"},
    )
    assert confirmed.status_code == 200, confirmed.text

    response = client.get(
        "/api/replenishment/suggestions",
        params={"status": "confirmed", "warehouse_id": warehouse_id, "page_size": 100},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["page"] == 1
    assert body["page_size"] == 100
    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [suggestion_id]


def test_replenishment_suggestions_route_is_in_openapi(client):
    client, _workspace_id, _warehouse_id, _sku_id = client
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/replenishment/suggestions" in paths
    assert "get" in paths["/api/replenishment/suggestions"]


def test_replenishment_api_requires_idempotency(client):
    client, _workspace_id, warehouse_id, sku_id = client
    missing = client.post(
        "/api/replenishment/suggestions/generate",
        json={"warehouse_id": warehouse_id, "sku_id": sku_id, "coverage_days": 14},
    )
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

def test_replenishment_evaluation_records_observation_window(client):
    client, _workspace_id, warehouse_id, sku_id = client
    suggested = client.post(
        "/api/replenishment/suggestions/generate",
        json={"warehouse_id": warehouse_id, "sku_id": sku_id, "coverage_days": 7, "as_of_date": "2026-09-10"},
        headers={"Idempotency-Key": "eval-gen-001"},
    )
    assert suggested.status_code == 201, suggested.text
    suggestion_id = suggested.json()["id"]
    evaluated = client.post(
        f"/api/analytics/replenishment-evaluation?suggestion_id={suggestion_id}&window_start=2026-09-11&window_end=2026-09-17",
    )
    assert evaluated.status_code == 201, evaluated.text
    body = evaluated.json()
    assert body["evaluation_status"] in {"evaluated", "insufficient"}
    assert body["suggestion_id"] == suggestion_id
    listing = client.get(f"/api/analytics/replenishment-evaluation?suggestion_id={suggestion_id}")
    assert listing.status_code == 200
    assert any(item["id"] == body["id"] for item in listing.json()["items"])
    inventory = client.get("/api/inventory")
    assert inventory.status_code == 200

