"""运营工作台今日待办 API 与页面契约测试。"""

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'operations-api.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
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
    from app.employee_auth import hash_password

    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="api-operations", name="运营待办测试")
        db.add(workspace)
        db.flush()
        user = db_mod.UserAccount(login="ops", password_hash=hash_password("operations-password"), display_name="运营")
        db.add(user)
        db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="operations"))
        warehouse = db_mod.Warehouse(workspace_id=workspace.id, code="OPS-WH", name="运营仓", warehouse_type="own")
        db.add(warehouse)
        db.commit()
        workspace_id, warehouse_id = workspace.id, warehouse.id

    with TestClient(main_mod.app) as test_client:
        login = test_client.post("/login", data={"login": "ops", "password": "operations-password", "next": "/ops"}, follow_redirects=False)
        assert login.status_code == 303, login.text
        csrf = test_client.cookies.get("dianshang_csrf")
        assert csrf
        test_client.headers.update({"X-CSRF-Token": csrf})
        yield test_client, workspace_id, warehouse_id
    bg_mod.reset_executor()
    cfg.get_settings.cache_clear()


def test_operations_today_requires_employee_login(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'unauth.db'}")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    import app.config as cfg
    import app.db as db_mod
    import app.background as bg_mod
    import app.main as main_mod
    cfg.get_settings.cache_clear(); importlib.reload(cfg); importlib.reload(db_mod); importlib.reload(bg_mod); importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    with TestClient(main_mod.app) as test_client:
        response = test_client.get("/api/operations/today")
        assert response.status_code == 401
    bg_mod.reset_executor(); cfg.get_settings.cache_clear()


def test_operations_today_export_has_scope_headers_and_filters(client):
    api, workspace_id, warehouse_id = client
    import app.db as db_mod
    from datetime import datetime

    with db_mod.SessionLocal() as db:
        db.add(db_mod.InventoryAlert(
            workspace_id=workspace_id, kind="low_stock", severity="high", status="open",
            dedupe_key="export-alert", title="库存告警", message="需要导出",
            warehouse_id=warehouse_id, created_at=datetime.utcnow(), last_seen_at=datetime.utcnow(),
        ))
        db.commit()

    all_rows = api.get("/api/operations/today/export", params={"warehouse_id": warehouse_id})
    assert all_rows.status_code == 200, all_rows.text
    assert all_rows.content.startswith(b"\xef\xbb\xbf")
    assert all_rows.headers["x-export-workspace-id"] == str(workspace_id)
    assert all_rows.headers["x-export-source"] == "workspace"
    assert all_rows.headers["x-export-row-count"] == "1"
    assert all_rows.headers["x-export-sha256"]
    assert "attachment" in all_rows.headers["content-disposition"]

    filtered = api.get("/api/operations/today/export", params={"warehouse_id": warehouse_id, "todo_ids": "missing:999"})
    assert filtered.status_code == 200
    assert filtered.headers["x-export-row-count"] == "0"
    assert filtered.content.startswith(b"\xef\xbb\xbf")


def test_operations_today_xlsx_export_has_formatted_content_type(client):
    api, _workspace_id, _warehouse_id = client
    response = api.get("/api/operations/today/export.xlsx")
    assert response.status_code == 200, response.text
    assert response.content.startswith(b"PK")
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert response.headers["x-export-row-count"] == "0"
    assert response.headers["content-disposition"].startswith('attachment; filename="operations-today-')
    assert "filename*=" in response.headers["content-disposition"]
    assert "content-disposition" in response.headers["access-control-expose-headers"].lower()
    assert response.headers["cache-control"] == "no-store"


def test_operations_today_xlsx_unauthenticated_response_is_json(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'unauth-xlsx.db'}")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    import app.config as cfg
    import app.db as db_mod
    import app.background as bg_mod
    import app.main as main_mod
    cfg.get_settings.cache_clear(); importlib.reload(cfg); importlib.reload(db_mod); importlib.reload(bg_mod); importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    with TestClient(main_mod.app) as test_client:
        response = test_client.get("/api/operations/today/export.xlsx")
        assert response.status_code == 401
        assert response.headers["content-type"].startswith("application/json")
        assert not response.content.startswith(b"PK")
    bg_mod.reset_executor(); cfg.get_settings.cache_clear()


def test_operations_today_export_is_not_available_without_login(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'unauth-export.db'}")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    import app.config as cfg
    import app.db as db_mod
    import app.background as bg_mod
    import app.main as main_mod
    cfg.get_settings.cache_clear(); importlib.reload(cfg); importlib.reload(db_mod); importlib.reload(bg_mod); importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    with TestClient(main_mod.app) as test_client:
        response = test_client.get("/api/operations/today/export")
        assert response.status_code == 401
    bg_mod.reset_executor(); cfg.get_settings.cache_clear()

def test_operations_today_contract_and_ops_page(client):
    api, workspace_id, warehouse_id = client
    response = api.get("/api/operations/today", params={"warehouse_id": warehouse_id})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workspace_id"] == workspace_id
    assert body["summary"]["total"] == 0
    assert body["simulated"] is False
    assert body["meta"]["data_completeness"] == "partial"
    page = api.get("/ops")
    assert page.status_code == 200
    html = page.text
    for marker in ("operations-today-panel", "operations-today-summary", "operations-today-list", "/api/operations/today", "/api/operations/today/export", "/api/operations/today/export.xlsx", "fetch(`/api/operations/today/export.xlsx?", "credentials: 'same-origin'", "URL.createObjectURL", "Excel 导出失败", "导出 Excel（已选）", "导出 Excel（全部）", "仅导出，不改变业务状态", "演示数据", "去处理"):
        assert marker in html
