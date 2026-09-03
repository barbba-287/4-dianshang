"""账户、角色和仓库授权工作台回归测试。"""

import importlib
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
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

    from app.employee_auth import hash_password
    from app.schemas import ProductRecord

    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="admin-test", name="账户测试商家")
        db.add(workspace)
        db.flush()
        db.add(
            db_mod.UserAccount(
                login="admin",
                password_hash=hash_password("admin-password"),
                display_name="测试管理员",
            )
        )
        db.flush()
        db.add(
            db_mod.WorkspaceMembership(
                workspace_id=workspace.id, user_id=1, role="admin"
            )
        )
        db.add(
            db_mod.Warehouse(
                code="OWN-ADMIN",
                name="账户测试仓",
                warehouse_type="own",
                workspace_id=workspace.id,
            )
        )
        repo_mod.upsert_product(
            db,
            ProductRecord(
                source="fixture",
                external_product_id="admin-product",
                title="账户测试商品",
                url="https://fixture.local/admin-product",
                current_price=Decimal("10.00"),
                observed_at=datetime.utcnow(),
            ),
        )
        db.commit()

    with TestClient(main_mod.app) as test_client:
        login = test_client.post(
            "/login",
            data={"login": "admin", "password": "admin-password", "next": "/admin/users"},
            follow_redirects=False,
        )
        assert login.status_code == 303, login.text
        csrf = test_client.cookies.get("dianshang_csrf")
        assert csrf
        test_client.headers.update({"X-CSRF-Token": csrf})
        yield test_client

    bg_mod.reset_executor()
    cfg.get_settings.cache_clear()


def test_admin_user_lifecycle_and_warehouse_scope(client):
    warehouses = client.get("/api/admin/warehouses")
    assert warehouses.status_code == 200, warehouses.text
    warehouse = warehouses.json()[0]
    assert warehouse["workspace_id"] == 1

    created = client.post(
        "/api/admin/users",
        json={
            "login": "operator",
            "display_name": "运营成员",
            "password": "operator-password",
            "role": "operations",
            "warehouse_ids": [],
        },
    )
    assert created.status_code == 201, created.text
    user = created.json()
    assert user["role"] == "operations"
    user_id = user["id"]

    granted = client.post(f"/api/admin/users/{user_id}/warehouses/{warehouse['id']}", json={})
    assert granted.status_code == 204, granted.text

    changed = client.patch(
        f"/api/admin/users/{user_id}",
        json={"display_name": "仓库成员", "role": "warehouse"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["role"] == "warehouse"
    assert changed.json()["warehouse_ids"] == [warehouse["id"]]

    reset = client.post(
        f"/api/admin/users/{user_id}/reset-password",
        json={"password": "operator-password-2"},
    )
    assert reset.status_code == 204, reset.text

    deactivated = client.post(f"/api/admin/users/{user_id}/deactivate", json={})
    assert deactivated.status_code == 204, deactivated.text
    listed = client.get("/api/admin/users")
    assert listed.status_code == 200
    inactive = next(item for item in listed.json() if item["id"] == user_id)
    assert inactive["status"] == "inactive"

    activated = client.post(f"/api/admin/users/{user_id}/activate", json={})
    assert activated.status_code == 204, activated.text
    active = next(item for item in client.get("/api/admin/users").json() if item["id"] == user_id)
    assert active["status"] == "active"
    assert active["warehouse_ids"] == [warehouse["id"]]

    revoked = client.delete(f"/api/admin/users/{user_id}/warehouses/{warehouse['id']}")
    assert revoked.status_code == 204, revoked.text
    assert next(item for item in client.get("/api/admin/users").json() if item["id"] == user_id)["warehouse_ids"] == []


def test_admin_csrf_and_page_contract(client):
    missing_csrf = client.post(
        "/api/admin/users",
        headers={"X-CSRF-Token": ""},
        json={
            "login": "blocked",
            "display_name": "应被拦截",
            "password": "blocked-password",
            "role": "readonly",
            "warehouse_ids": [],
        },
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["detail"]["code"] == "CSRF_INVALID"

    page = client.get("/admin/users")
    assert page.status_code == 200
    for marker in (
        "/api/admin/users",
        "/reset-password",
        "data-action=\"grant\"",
        "data-action=\"revoke\"",
        "/api/admin/warehouses/",
        "退出登录",
    ):
        assert marker in page.text
