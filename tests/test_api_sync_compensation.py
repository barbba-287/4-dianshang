"""同步运行详情与人工补偿 API 测试。"""
from __future__ import annotations

import importlib
import json

from fastapi.testclient import TestClient


class RetryableFixtureError(RuntimeError):
    code = "TIMEOUT"
    retryable = True


def _orders(account: str = "a", store: str = "s", order_no: str = "O-API-1") -> str:
    return json.dumps({"records": [{
        "account_ref": account,
        "store_ref": store,
        "external_order_no": order_no,
        "status": "paid",
        "created_at": "2026-09-01T10:00:00Z",
        "updated_at": "2026-09-01T10:01:00Z",
        "lines": [{"external_sku": "S-API-1", "ordered_qty": 2, "gross_amount": "20.00"}],
    }]})


def _inventory(account: str = "a", store: str = "s") -> str:
    return json.dumps({"records": [{
        "account_ref": account,
        "store_ref": store,
        "external_sku": "S-API-1",
        "available_qty": 7,
        "as_of": "2026-09-01T10:00:00Z",
        "received_at": "2026-09-01T10:01:00Z",
    }]})


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'sync-api.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    import app.config as cfg
    import app.db as db_mod
    import app.background as bg_mod
    import app.main as main_mod
    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(bg_mod)
    importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    from app.employee_auth import hash_password
    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="sync-api", name="同步 API 测试")
        db.add(workspace)
        db.flush()
        user = db_mod.UserAccount(
            login="operator",
            password_hash=hash_password("operator-password"),
            display_name="运营",
        )
        db.add(user)
        db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="operations"))
        db.commit()
    client = TestClient(main_mod.app)
    login = client.post(
        "/login",
        data={"login": "operator", "password": "operator-password", "next": "/"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    client.headers.update({"X-CSRF-Token": client.cookies.get("dianshang_csrf")})
    return client, db_mod, bg_mod


def _create_partial(client, monkeypatch):
    import app.platform_sync as platform_sync

    original = platform_sync.load_records

    def fail_inventory(*args, **kwargs):
        raise RetryableFixtureError("库存 fixture 暂时不可读")

    monkeypatch.setattr(platform_sync, "load_records", fail_inventory)
    response = client.post(
        "/api/external/sync/fixture",
        json={
            "platform": "jd",
            "account_ref": "a",
            "store_ref": "s",
            "source_mode": "json",
            "orders_content": _orders(),
            "inventory_content": _inventory(),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["sync_status"] == "partial"
    return response.json()["sync_run_id"], platform_sync, original


def test_sync_detail_and_retry_idempotency(tmp_path, monkeypatch):
    client, db_mod, bg_mod = _client(tmp_path, monkeypatch)
    try:
        run_id, platform_sync, original_loader = _create_partial(client, monkeypatch)
        detail = client.get(f"/api/external/sync/runs/{run_id}")
        assert detail.status_code == 200, detail.text
        parent = detail.json()
        assert parent["status"] == "partial"
        assert parent["attempt"] == 1
        assert parent["retryable_resources"] == ["inventory"]
        assert parent["resource_status"]["orders"]["status"] == "succeeded"
        assert parent["resource_status"]["inventory"]["error_code"] == "TIMEOUT"

        monkeypatch.setattr(platform_sync, "load_records", original_loader)
        retry_payload = {"resources": ["inventory"], "inventory_content": _inventory()}
        retry = client.post(
            f"/api/external/sync/runs/{run_id}/retry",
            json=retry_payload,
            headers={"Idempotency-Key": "sync-retry-1"},
        )
        assert retry.status_code == 201, retry.text
        child = retry.json()
        assert child["status"] == "succeeded"
        assert child["attempt"] == 2
        assert child["retry_of_run_id"] == run_id
        assert child["resource_status"]["orders"]["execution"] == "carried_forward"
        assert child["resource_status"]["inventory"]["execution"] == "attempted"

        replay = client.post(
            f"/api/external/sync/runs/{run_id}/retry",
            json=retry_payload,
            headers={"Idempotency-Key": "sync-retry-1"},
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["run_id"] == child["run_id"]
        with db_mod.SessionLocal() as db:
            assert db.query(db_mod.ExternalSyncRun).filter(
                db_mod.ExternalSyncRun.retry_of_run_id == run_id,
            ).count() == 1
            assert db.query(db_mod.ExternalOrder).count() == 1
            assert db.query(db_mod.ExternalInventorySnapshot).count() == 1
            assert db.query(db_mod.InventoryBalance).count() == 0
    finally:
        bg_mod.reset_executor()


def test_sync_retry_rejects_invalid_requests_and_workspace_scope(tmp_path, monkeypatch):
    client, _db_mod, bg_mod = _client(tmp_path, monkeypatch)
    try:
        run_id, platform_sync, original_loader = _create_partial(client, monkeypatch)
        monkeypatch.setattr(platform_sync, "load_records", original_loader)
        missing_key = client.post(
            f"/api/external/sync/runs/{run_id}/retry",
            json={"resources": ["inventory"], "inventory_content": _inventory()},
        )
        assert missing_key.status_code == 422
        assert missing_key.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

        not_retryable = client.post(
            f"/api/external/sync/runs/{run_id}/retry",
            json={"resources": ["orders"], "orders_content": _orders()},
            headers={"Idempotency-Key": "sync-retry-invalid-resource"},
        )
        assert not_retryable.status_code == 409
        assert not_retryable.json()["detail"]["code"] == "SYNC_RESOURCE_NOT_RETRYABLE"

        missing_content = client.post(
            f"/api/external/sync/runs/{run_id}/retry",
            json={"resources": ["inventory"]},
            headers={"Idempotency-Key": "sync-retry-missing-content"},
        )
        assert missing_content.status_code == 422
        assert missing_content.json()["detail"]["code"] == "FIXTURE_CONTENT_REQUIRED"

        first_retry = client.post(
            f"/api/external/sync/runs/{run_id}/retry",
            json={"resources": ["inventory"], "inventory_content": _inventory()},
            headers={"Idempotency-Key": "sync-retry-1"},
        )
        assert first_retry.status_code == 201, first_retry.text

        changed = client.post(
            f"/api/external/sync/runs/{run_id}/retry",
            json={"resources": ["inventory"], "inventory_content": _inventory() + " "},
            headers={"Idempotency-Key": "sync-retry-1"},
        )
        assert changed.status_code == 409
        assert changed.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSE"

        other = client.get("/api/external/sync/runs/not-a-run")
        assert other.status_code == 404
        assert other.json()["detail"]["code"] == "SYNC_RUN_NOT_FOUND"
    finally:
        bg_mod.reset_executor()
