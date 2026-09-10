"""首页仓储工作台的 HTML 合同测试。"""

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'frontend.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
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
    from app.employee_auth import hash_password

    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="test", name="测试商家")
        db.add(workspace)
        db.flush()
        user = db_mod.UserAccount(login="admin", password_hash=hash_password("test-password"), display_name="测试管理员")
        db.add(user)
        db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="admin"))
        db.commit()
    with TestClient(main_mod.app) as test_client:
        login = test_client.post("/login", data={"login": "admin", "password": "test-password", "next": "/"}, follow_redirects=False)
        assert login.status_code in (302, 303)
        yield test_client
    bg_mod.reset_executor()
    cfg.get_settings.cache_clear()


def test_home_contains_product_and_warehouse_workflow(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    for marker in (
        'id="products"',
        'id="sku-product"',
        'id="create-sku"',
        'id="warehouse-select"',
        'id="create-inbound"',
        'id="received-inbound-select"',
        'id="load-received-inbound"',
        "status=received",
    ):
        assert marker in html
    assert 'id="confirm-inbound"' in html
    assert 'id="receive-inbound"' not in html
    assert 'id="logout"' in html
    assert 'class="sidebar"></aside>' not in html
    assert '<title>运营工作台</title>' in html
    assert '<h1>运营工作台</h1>' in html
    assert '返回工作台' not in html
    assert '返回工作台' not in html
    assert '切换账户' not in html


def test_navigation_is_filtered_by_employee_role():
    from app.employee_auth import Principal
    import app.main as main_mod

    cases = {
        "admin": {"/dashboard", "/ops", "/warehouse", "/admin/users", "/customer-service"},
        "operations": {"/dashboard", "/ops", "/warehouse", "/customer-service"},
        "warehouse": {"/warehouse"},
        "customer_service": {"/customer-service"},
        "readonly": {"/customer-service"},
    }
    labels = {
        "/dashboard": "运营驾驶舱",
        "/ops": "运营工作台",
        "/warehouse": "仓库收货",
        "/admin/users": "成员权限",
        "/customer-service": "客服知识",
    }
    for role, visible_paths in cases.items():
        principal = Principal(subject=role, tenant_id="test", roles=(role,), auth_type="session")
        html = main_mod._visible_navigation(principal, "/ops")
        for path, label in labels.items():
            if path in visible_paths:
                assert f'href="{path}"' in html
                assert label in html
            else:
                if path == "/ops":
                    continue
                assert f'href="{path}"' not in html
