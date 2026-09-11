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
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")

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

    from app.employee_auth import hash_password

    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="test", name="测试商家")
        db.add(workspace)
        db.flush()
        user = db_mod.UserAccount(
            login="admin",
            password_hash=hash_password("test-password"),
            display_name="测试管理员",
        )
        db.add(user)
        db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="admin"))
        db.commit()

    with TestClient(main_mod.app) as test_client:
        login = test_client.post(
            "/login",
            data={"login": "admin", "password": "test-password", "next": "/"},
            follow_redirects=False,
        )
        assert login.status_code in (302, 303), login.text
        csrf = test_client.cookies.get("dianshang_csrf")
        assert csrf
        test_client.headers.update({"X-CSRF-Token": csrf})
        yield test_client

    reset_executor()
    cfg.get_settings.cache_clear()


def _create_sku(client, code="TEA-250-GREEN"):
    response = client.post(
        "/api/skus",
        json={"product_id": 1, "sku_code": code, "variant_label": "绿茶 250g"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_warehouse(client, code="OWN-01"):
    response = client.post(
        "/api/warehouses",
        json={"code": code, "name": f"仓库 {code}", "warehouse_type": "own"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_inbound(client, warehouse_id, sku_id, reference_no="IN-001", expected_qty=10):
    response = client.post(
        "/api/inbounds",
        json={
            "warehouse_id": warehouse_id,
            "reference_no": reference_no,
            "lines": [{"sku_id": sku_id, "expected_qty": expected_qty}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_receive_requires_idempotency_key(client):
    sku = _create_sku(client, "TEA-NO-KEY")
    warehouse = _create_warehouse(client, "OWN-NO-KEY")
    inbound = _create_inbound(client, warehouse["id"], sku["id"], "IN-NO-KEY", 2)
    response = client.post(
        f"/api/inbounds/{inbound['id']}/receive",
        json={"lines": [{"sku_id": sku["id"], "received_qty": 2, "damaged_qty": 0}]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


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
        headers={"Idempotency-Key": "receive-001"},
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
        headers={"Idempotency-Key": "receive-invalid-001"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "DAMAGED_QTY_INVALID"


def test_creation_errors_and_duplicate_reference(client):
    missing_product = client.post(
        "/api/skus", json={"product_id": 9999, "sku_code": "MISSING"}
    )
    assert missing_product.status_code == 404
    assert missing_product.json()["detail"]["code"] == "PRODUCT_NOT_FOUND"

    sku = _create_sku(client)
    duplicate_sku = client.post(
        "/api/skus", json={"product_id": 1, "sku_code": sku["sku_code"]}
    )
    assert duplicate_sku.status_code == 409
    assert duplicate_sku.json()["detail"]["code"] == "SKU_CODE_EXISTS"

    warehouse = _create_warehouse(client)
    duplicate_warehouse = client.post(
        "/api/warehouses", json={"code": warehouse["code"], "name": "重复仓库"}
    )
    assert duplicate_warehouse.status_code == 409
    assert duplicate_warehouse.json()["detail"]["code"] == "WAREHOUSE_CODE_EXISTS"

    first = _create_inbound(client, warehouse["id"], sku["id"], "IN-DUP")
    duplicate_reference = client.post(
        "/api/inbounds",
        json={
            "warehouse_id": warehouse["id"],
            "reference_no": first["reference_no"],
            "lines": [{"sku_id": sku["id"], "expected_qty": 1}],
        },
    )
    assert duplicate_reference.status_code == 409
    assert duplicate_reference.json()["detail"]["code"] == "INBOUND_REFERENCE_EXISTS"


def test_receive_idempotency_and_line_validation(client):
    sku = _create_sku(client)
    warehouse = _create_warehouse(client)
    inbound = _create_inbound(client, warehouse["id"], sku["id"], "IN-IDEMP", 10)
    path = f"/api/inbounds/{inbound['id']}/receive"
    payload = {"lines": [{"sku_id": sku["id"], "received_qty": 8, "damaged_qty": 1}]}
    headers = {"Idempotency-Key": "receive-1"}

    first = client.post(path, json=payload, headers=headers)
    assert first.status_code == 200, first.text
    replay = client.post(path, json=payload, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["received_at"] == first.json()["received_at"]
    assert replay.json()["lines"] == first.json()["lines"]

    changed = client.post(
        path,
        json={"lines": [{"sku_id": sku["id"], "received_qty": 7, "damaged_qty": 0}]},
        headers=headers,
    )
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "INBOUND_ALREADY_RECEIVED"

    other_key = client.post(path, json=payload, headers={"Idempotency-Key": "receive-2"})
    assert other_key.status_code == 409
    assert other_key.json()["detail"]["code"] == "INBOUND_ALREADY_RECEIVED"

    missing_line = _create_inbound(client, warehouse["id"], sku["id"], "IN-MISSING-LINE")
    mismatch = client.post(
        f"/api/inbounds/{missing_line['id']}/receive",
        json={"lines": [{"sku_id": 9999, "received_qty": 1, "damaged_qty": 0}]},
        headers={"Idempotency-Key": "receive-mismatch-001"},
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["detail"]["code"] == "INBOUND_LINES_MISMATCH"

    not_found = client.post(
        "/api/inbounds/9999/receive",
        json={"lines": [{"sku_id": sku["id"], "received_qty": 1, "damaged_qty": 0}]},
        headers={"Idempotency-Key": "receive-not-found-001"},
    )
    assert not_found.status_code == 404
    assert not_found.json()["detail"]["code"] == "INBOUND_NOT_FOUND"


def test_inventory_filters_pagination_and_confirmed_fields(client):
    sku_one = _create_sku(client, "TEA-GREEN")
    sku_two = _create_sku(client, "TEA-RED")
    warehouse_one = _create_warehouse(client, "OWN-A")
    warehouse_two = _create_warehouse(client, "OWN-B")

    first = _create_inbound(client, warehouse_one["id"], sku_one["id"], "IN-FILTER-1", 5)
    client.post(
        f"/api/inbounds/{first['id']}/receive",
        json={"lines": [{"sku_id": sku_one["id"], "received_qty": 5, "damaged_qty": 1}]},
        headers={"Idempotency-Key": "receive-filter-001"},
    )
    confirmed = client.post(f"/api/inbounds/{first['id']}/confirm", json={})
    assert confirmed.status_code == 200
    assert confirmed.json()["lines"][0]["accepted_qty"] == 4

    second = _create_inbound(client, warehouse_two["id"], sku_two["id"], "IN-FILTER-2", 3)
    client.post(
        f"/api/inbounds/{second['id']}/receive",
        json={"lines": [{"sku_id": sku_two["id"], "received_qty": 3, "damaged_qty": 0}]},
        headers={"Idempotency-Key": "receive-filter-002"},
    )
    client.post(f"/api/inbounds/{second['id']}/confirm", json={})

    page = client.get("/api/inventory?page=1&page_size=1")
    assert page.status_code == 200
    assert page.json()["total"] == 2
    assert len(page.json()["items"]) == 1
    assert page.json()["page"] == 1
    assert page.json()["page_size"] == 1

    warehouse_filter = client.get(f"/api/inventory?warehouse_id={warehouse_one['id']}")
    assert warehouse_filter.json()["total"] == 1
    assert warehouse_filter.json()["items"][0]["on_hand_qty"] == 4

    sku_filter = client.get(f"/api/inventory?sku_id={sku_two['id']}")
    assert sku_filter.json()["total"] == 1
    assert sku_filter.json()["items"][0]["warehouse_id"] == warehouse_two["id"]


def test_confirm_rolls_back_inventory_when_write_fails(client, monkeypatch):
    import app.db as db_mod
    import app.repository as repo_mod

    sku = _create_sku(client)
    warehouse = _create_warehouse(client)
    inbound = _create_inbound(client, warehouse["id"], sku["id"], "IN-ROLLBACK", 4)
    path = f"/api/inbounds/{inbound['id']}/receive"
    received = client.post(
        path,
        json={"lines": [{"sku_id": sku["id"], "received_qty": 4, "damaged_qty": 1}]},
        headers={"Idempotency-Key": "receive-rollback-001"},
    )
    assert received.status_code == 200

    def fail_after_staging(db, *, inbound_id, confirmed_by=None, idempotency_key=None):
        order = db.get(db_mod.InboundOrder, inbound_id)
        assert order is not None
        line = db.query(db_mod.InboundLine).filter_by(inbound_order_id=inbound_id).one()
        db.add(
            db_mod.InventoryTransaction(
                inbound_order_id=inbound_id,
                warehouse_id=order.warehouse_id,
                sku_id=line.sku_id,
                quantity_delta=3,
                movement_type="inbound_confirm",
                idempotency_key=f"rollback:{inbound_id}",
            )
        )
        raise RuntimeError("injected inventory write failure")

    monkeypatch.setattr(repo_mod, "confirm_inbound", fail_after_staging)
    failed = client.post(f"/api/inbounds/{inbound['id']}/confirm", json={})
    assert failed.status_code == 500

    detail = client.get(f"/api/inbounds/{inbound['id']}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "received"
    inventory = client.get(f"/api/inventory?warehouse_id={warehouse['id']}")
    assert inventory.status_code == 200
    assert inventory.json()["total"] == 0

    from sqlalchemy import select

    with db_mod.SessionLocal() as db:
        assert db.scalar(select(db_mod.InventoryTransaction.id)) is None
        assert db.scalar(select(db_mod.InventoryBalance.id)) is None

    monkeypatch.undo()
    recovered = client.post(f"/api/inbounds/{inbound['id']}/confirm", json={})
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "confirmed"
