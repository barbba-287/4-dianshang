"""安全库存策略与低库存告警接口测试。"""

import importlib
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient


def _reload_app(monkeypatch, db_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(db_path.parent / "artifacts"))
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
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
    return cfg, db_mod, repo_mod, bg_mod, main_mod


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg, db_mod, repo_mod, bg_mod, main_mod = _reload_app(monkeypatch, tmp_path / "alerts.db")
    from app.employee_auth import hash_password
    from app.schemas import ProductRecord

    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="alerts-test", name="告警测试商家")
        db.add(workspace)
        db.flush()
        product = repo_mod.upsert_product(
            db,
            ProductRecord(
                source="fixture",
                external_product_id="alert-tea",
                title="告警测试茶",
                url="https://fixture.local/alert-tea",
                current_price=Decimal("10.00"),
                observed_at=datetime.utcnow(),
            ),
            workspace_id=workspace.id,
        )
        user = db_mod.UserAccount(
            login="alert-admin",
            password_hash=hash_password("password-123"),
            display_name="告警管理员",
        )
        db.add(user)
        db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="admin"))
        db.commit()
        product_id = product.id

    with TestClient(main_mod.app) as test_client:
        login = test_client.post(
            "/login",
            data={"login": "alert-admin", "password": "password-123", "next": "/"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        csrf = test_client.cookies.get("dianshang_csrf")
        assert csrf
        test_client.headers.update({"X-CSRF-Token": csrf})
        yield test_client, product_id

    bg_mod.reset_executor()
    cfg.get_settings.cache_clear()


def test_low_stock_alert_lifecycle_and_deduplication(client):
    api, product_id = client
    warehouse = api.post(
        "/api/warehouses",
        json={"code": "ALERT-WH", "name": "告警仓", "warehouse_type": "own"},
    )
    assert warehouse.status_code == 201, warehouse.text
    warehouse_id = warehouse.json()["id"]
    sku = api.post(
        "/api/skus",
        json={"product_id": product_id, "sku_code": "ALERT-SKU"},
    )
    assert sku.status_code == 201, sku.text
    sku_id = sku.json()["id"]

    policy = api.put(
        "/api/inventory/policies",
        json={
            "warehouse_id": warehouse_id,
            "sku_id": sku_id,
            "safety_stock_qty": 2,
            "reorder_point_qty": 5,
        },
    )
    assert policy.status_code == 200, policy.text

    first = api.post(f"/api/inventory/alerts/refresh?warehouse_id={warehouse_id}")
    assert first.status_code == 200, first.text
    assert len(first.json()) == 1
    assert first.json()[0]["kind"] == "low_stock"
    assert first.json()[0]["severity"] == "critical"

    second = api.post(f"/api/inventory/alerts/refresh?warehouse_id={warehouse_id}")
    assert second.status_code == 200, second.text
    assert len(second.json()) == 1
    alerts = api.get("/api/inventory/alerts?status=open")
    assert alerts.status_code == 200
    assert len(alerts.json()) == 1

    alert_id = alerts.json()[0]["id"]
    acknowledged = api.post(f"/api/inventory/alerts/{alert_id}/ack")
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["status"] == "acknowledged"
