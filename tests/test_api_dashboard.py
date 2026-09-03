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


def test_dashboard_page_contract(client):
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert 'id="kpis"' in response.text
    assert 'api/dashboard/summary' in response.text
