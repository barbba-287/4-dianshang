"""库存看板 API 合同测试。"""

import importlib
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'dashboard.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
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
        db.add(db_mod.Workspace(tenant_key="dashboard-test", name="看板测试商家")); db.flush()
        product = repo_mod.upsert_product(db, ProductRecord(source="fixture", external_product_id="dash-1", title="看板商品", url="https://fixture.local/dash-1", current_price=Decimal("10.00"), observed_at=datetime.utcnow()))
        user = db_mod.UserAccount(login="admin", password_hash=hash_password("password"), display_name="管理员"); db.add(user); db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=1, user_id=user.id, role="admin")); db.commit()
    with TestClient(main_mod.app) as test_client:
        response = test_client.post("/login", data={"login":"admin","password":"password","next":"/"}, follow_redirects=False)
        assert response.status_code == 303
        yield test_client
    bg_mod.reset_executor(); cfg.get_settings.cache_clear()


def test_dashboard_summary_empty(client):
    response = client.get("/api/dashboard/summary")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kpis"]["product_count"] == 1
    assert body["kpis"]["on_hand_qty"] == 0
    assert "gmv" in body["unsupported_metrics"]
    assert body["recent_inbounds"] == []


def test_dashboard_summary_includes_sync_health_and_workspace_scope(client):
    import json
    import app.db as db_mod

    now = datetime.utcnow()
    with db_mod.SessionLocal() as db:
        db.add_all([
            db_mod.ExternalSyncRun(
                workspace_id=1,
                run_id="sync-success-1",
                platform="jd",
                account_ref="account-a",
                store_ref="store-a",
                sync_type="fixture_bundle",
                source_mode="json",
                status="succeeded",
                started_at=now - timedelta(seconds=8),
                finished_at=now - timedelta(seconds=2),
                heartbeat_at=now - timedelta(seconds=2),
                resource_status_json=json.dumps({"schema_version": 1, "orders": {"status": "succeeded", "execution": "attempted"}}),
            ),
            db_mod.ExternalSyncRun(
                workspace_id=1,
                run_id="sync-partial-1",
                platform="jd",
                account_ref="account-b",
                store_ref="store-b",
                sync_type="fixture_bundle",
                source_mode="json",
                status="partial",
                started_at=now - timedelta(seconds=30),
                finished_at=now - timedelta(seconds=20),
                heartbeat_at=now - timedelta(seconds=20),
                resource_status_json=json.dumps({"schema_version": 1, "inventory": {"status": "failed", "execution": "attempted", "retryable": True, "error_code": "TIMEOUT"}}),
                retryable_resources_json=json.dumps(["inventory"]),
                error_code="TIMEOUT",
            ),
            db_mod.ExternalSyncRun(
                workspace_id=1,
                run_id="sync-running-stale",
                platform="pdd",
                account_ref="account-c",
                store_ref="store-c",
                sync_type="inventory",
                source_mode="json",
                status="running",
                started_at=now - timedelta(seconds=2000),
                heartbeat_at=now - timedelta(seconds=2000),
            ),
        ])
        db.commit()
    response = client.get("/api/dashboard/summary")
    assert response.status_code == 200, response.text
    health = response.json()["sync_health"]
    assert health["counts"]["succeeded"] == 1
    assert health["counts"]["partial"] == 1
    assert health["counts"]["running"] == 1
    assert health["counts"]["stalled"] == 1
    assert health["counts"]["retryable"] == 1
    assert len(health["latest"]) == 3
    assert any(item["run_id"] == "sync-partial-1" for item in health["recent_failures"])


def test_dashboard_sync_health_latest_is_deduplicated(client):
    import app.db as db_mod

    now = datetime.utcnow()
    with db_mod.SessionLocal() as db:
        for run_id, seconds_ago, status in (("sync-old", 20, "failed"), ("sync-new", 5, "succeeded")):
            db.add(db_mod.ExternalSyncRun(
                workspace_id=1,
                run_id=run_id,
                platform="taobao",
                account_ref="same-account",
                store_ref="same-store",
                sync_type="inventory",
                source_mode="json",
                status=status,
                started_at=now - timedelta(seconds=seconds_ago + 1),
                finished_at=now - timedelta(seconds=seconds_ago) if status != "running" else None,
                heartbeat_at=now - timedelta(seconds=seconds_ago),
            ))
        db.commit()
    response = client.get("/api/dashboard/summary")
    assert response.status_code == 200, response.text
    latest = response.json()["sync_health"]["latest"]
    same_source = [item for item in latest if item["platform"] == "taobao" and item["account_ref"] == "same-account"]
    assert len(same_source) == 1
    assert same_source[0]["run_id"] == "sync-new"


    response = client.get("/dashboard")
    assert response.status_code == 200
    assert 'id="kpis"' in response.text
    assert 'api/dashboard/summary' in response.text
    assert '<title>运营驾驶舱</title>' in response.text
    assert '<h1>运营驾驶舱</h1>' in response.text
    assert response.text.count('id="logout"') == 1
    assert 'id="logout-link"' not in response.text
    assert '商品与补货' not in response.text
