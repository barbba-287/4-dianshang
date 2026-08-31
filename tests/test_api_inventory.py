"""轻量仓储协同 API 回归测试。"""

import importlib
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.background import reset_executor


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "inventory.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IMPORTS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("API_AUTH_ENABLED", "false")

    import app.config as cfg
    import app.db as db_mod
    import app.repository as repo_mod
    import app.background as bg_mod
    import app.main as main_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(repo_mod)
    importlib.reload(bg_mod)
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
        observed_at=datetime(2026, 8, 31, 12, 0, 0),
    )
    with db_mod.SessionLocal() as db:
        repo_mod.upsert_product(db, record)
        db.commit()

    with TestClient(main_mod.app) as test_client:
        yield test_client

    reset_executor()
    cfg.get_settings.cache_clear()


def test_inbound_receive_confirm_updates_inventory_once(client):
    sku = client.post(
        "/api/skus",
        json={"product_id": 1, "sku_code": "TEA-250-GREEN", "variant_label": "绿茶 250g"},
    )
    assert sku.status_code == 201, sku.text
    sku_id = sku.json()["id"]

    warehouse = client.post(
        "/api/warehouses",
        json={
            "code": "OWN-01",
            "name": "自有小仓",
            "warehouse_type": "own",
            "integration_mode": "manual",
        },
    )
    assert warehouse.status_code == 201, warehouse.text
    warehouse_id = warehouse.json()["id"]

    inbound = client.post(
        "/api/inbounds",
        json={
            "warehouse_id": warehouse_id,
            "reference_no": "IN-001",
            "lines": [{"sku_id": sku_id, "expected_qty": 100}],
        },
    )
    assert inbound.status_code == 201, inbound.text
    inbound_id = inbound.json()["id"]
    assert inbound.json()["status"] == "expected"

    before = client.get(f"/api/inventory?warehouse_id={warehouse_id}")
    assert before.status_code == 200
    assert before.json()["total"] == 0

    received = client.post(
        f"/api/inbounds/{inbound_id}/receive",
        json={"lines": [{"sku_id": sku_id, "received_qty": 98, "damaged_qty": 3}]},
    )
    assert received.status_code == 200, received.text
    received_body = received.json()
    assert received_body["status"] == "received"
    assert received_body["lines"][0]["difference"] == -2

    after_receive = client.get(f"/api/inventory?warehouse_id={warehouse_id}")
    assert after_receive.json()["total"] == 0

    confirmed = client.post(f"/api/inbounds/{inbound_id}/confirm", json={})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"

    inventory = client.get(f"/api/inventory?warehouse_id={warehouse_id}").json()
    assert inventory["total"] == 1
    assert inventory["items"][0]["on_hand_qty"] == 95

    repeated = client.post(f"/api/inbounds/{inbound_id}/confirm", json={})
    assert repeated.status_code == 200, repeated.text
    inventory_again = client.get(f"/api/inventory?warehouse_id={warehouse_id}").json()
    assert inventory_again["items"][0]["on_hand_qty"] == 95


def test_inbound_rejects_duplicate_sku_and_invalid_state(client):
    sku = client.post(
        "/api/skus",
        json={"product_id": 1, "sku_code": "TEA-250-GREEN"},
    ).json()
    warehouse = client.post(
        "/api/warehouses",
        json={"code": "3PL-01", "name": "第三方仓", "warehouse_type": "third_party"},
    ).json()
    payload = {
        "warehouse_id": warehouse["id"],
        "reference_no": "IN-002",
        "lines": [
            {"sku_id": sku["id"], "expected_qty": 1},
            {"sku_id": sku["id"], "expected_qty": 2},
        ],
    }
    duplicate = client.post("/api/inbounds", json=payload)
    assert duplicate.status_code == 422
    assert duplicate.json()["detail"]["code"] == "DUPLICATE_SKU"

    inbound = client.post(
        "/api/inbounds",
        json={
            "warehouse_id": warehouse["id"],
            "reference_no": "IN-003",
            "lines": [{"sku_id": sku["id"], "expected_qty": 2}],
        },
    ).json()
    response = client.post(f"/api/inbounds/{inbound['id']}/confirm", json={})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "INVALID_INBOUND_STATE"


def test_inbound_validates_damaged_quantity(client):
    sku = client.post(
        "/api/skus",
        json={"product_id": 1, "sku_code": "TEA-250-GREEN"},
    ).json()
    warehouse = client.post(
        "/api/warehouses",
        json={"code": "OWN-02", "name": "备用仓"},
    ).json()
    inbound = client.post(
        "/api/inbounds",
        json={
            "warehouse_id": warehouse["id"],
            "reference_no": "IN-004",
            "lines": [{"sku_id": sku["id"], "expected_qty": 2}],
        },
    ).json()
    response = client.post(
        f"/api/inbounds/{inbound['id']}/receive",
        json={"lines": [{"sku_id": sku["id"], "received_qty": 1, "damaged_qty": 2}]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "DAMAGED_QTY_INVALID"
