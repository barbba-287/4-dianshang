"""淘宝 Adapter capability and fixture preview API tests."""
from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'taobao-api.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("TAOBAO_ADAPTER_ENABLED", "false")
    import app.config as cfg
    import app.db as db_mod
    import app.background as bg_mod
    import app.main as main_mod
    cfg.get_settings.cache_clear(); importlib.reload(cfg); importlib.reload(db_mod); importlib.reload(bg_mod); importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    from app.employee_auth import hash_password
    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="taobao-api", name="淘宝 API 测试")
        db.add(workspace); db.flush()
        user = db_mod.UserAccount(login="viewer", password_hash=hash_password("viewer-password"), display_name="查看者")
        db.add(user); db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="operations")); db.commit()
    client = TestClient(main_mod.app)
    login = client.post("/login", data={"login":"viewer", "password":"viewer-password", "next":"/"}, follow_redirects=False)
    assert login.status_code == 303
    client.headers.update({"X-CSRF-Token": client.cookies.get("dianshang_csrf")})
    return client, db_mod, bg_mod


def test_capabilities_and_preview_are_read_only(tmp_path, monkeypatch):
    client, db_mod, bg_mod = _client(tmp_path, monkeypatch)
    try:
        capabilities = client.get("/api/external/taobao/capabilities")
        assert capabilities.status_code == 200
        body = capabilities.json()
        assert body["read_only"] is True
        assert body["live_enabled"] is False
        assert body["simulated"] is True
        assert "secret" not in capabilities.text.lower()
        content = json.dumps({"records":[{"external_order_no":"TB-API-1","status":"paid","created_at":"2026-09-01T10:00:00Z","updated_at":"2026-09-01T10:01:00Z","lines":[{"external_sku":"TB-SKU","ordered_qty":2,"gross_amount":"20.00"}]}],"has_more":False})
        preview = client.post("/api/external/taobao/preview", json={"resource":"orders","account_ref":"account","store_ref":"store","content":content})
        assert preview.status_code == 200, preview.text
        assert preview.json()["normalized_rows"][0]["external_order_no"] == "TB-API-1"
        with db_mod.SessionLocal() as db:
            assert db.query(db_mod.ExternalOrder).count() == 0
            assert db.query(db_mod.ExternalInventorySnapshot).count() == 0
    finally:
        bg_mod.reset_executor()


def test_preview_rejects_invalid_fixture(tmp_path, monkeypatch):
    client, _db_mod, bg_mod = _client(tmp_path, monkeypatch)
    try:
        response = client.post("/api/external/taobao/preview", json={"resource":"orders","account_ref":"account","store_ref":"store","content":"not-json"})
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "TAOBAO_BAD_RESPONSE"
    finally:
        bg_mod.reset_executor()
