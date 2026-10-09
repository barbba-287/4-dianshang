"""客服工单业务字段与 API 契约测试。"""
import importlib
from datetime import datetime
from fastapi.testclient import TestClient

def test_ticket_list_api_returns_empty_page_for_customer_service(tmp_path, monkeypatch):
    """客服角色可加载空待处理工单列表。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ticket-list-empty.db'}")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEMO_MODE_ENABLED", "true")
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    import app.config as cfg
    import app.db as db_mod
    import app.background as bg_mod
    import app.main as main_mod
    cfg.get_settings.cache_clear(); importlib.reload(cfg); importlib.reload(db_mod); importlib.reload(bg_mod); importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    from app.employee_auth import hash_password
    with db_mod.SessionLocal() as db:
        ws = db_mod.Workspace(tenant_key="ticket-list-empty", name="空工单")
        db.add(ws); db.flush()
        user = db_mod.UserAccount(login="ticket-list-empty", password_hash=hash_password("ticket-password"), display_name="客服")
        db.add(user); db.flush(); db.add(db_mod.WorkspaceMembership(workspace_id=ws.id, user_id=user.id, role="customer_service")); db.commit()
    with TestClient(main_mod.app) as client:
        login = client.post('/login', data={'login':'ticket-list-empty','password':'ticket-password','next':'/customer-service'}, follow_redirects=False)
        assert login.status_code == 303
        response = client.get('/api/tickets?page_size=100')
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []
    bg_mod.reset_executor(); cfg.get_settings.cache_clear()


def test_ticket_list_api_repairs_legacy_ticket_columns(tmp_path, monkeypatch):
    """客服列表可读取 0017 版本记录下的旧结构工单。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ticket-list-legacy.db'}")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEMO_MODE_ENABLED", "true")
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    import app.config as cfg
    import app.db as db_mod
    import app.background as bg_mod
    import app.main as main_mod
    cfg.get_settings.cache_clear(); importlib.reload(cfg); importlib.reload(db_mod); importlib.reload(bg_mod); importlib.reload(main_mod)
    from sqlalchemy import text
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    migration_cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    migration_cfg.set_main_option("sqlalchemy.url", db_mod.database_url)
    command.upgrade(migration_cfg, "0017_service_tickets")
    with db_mod.engine.begin() as conn:
        conn.execute(text("ALTER TABLE service_tickets DROP COLUMN issue_type"))
        conn.execute(text("ALTER TABLE service_tickets DROP COLUMN description"))
        conn.execute(text("ALTER TABLE service_tickets DROP COLUMN sku_ref"))
        conn.execute(text("INSERT INTO workspaces (tenant_key, name, status, created_at, updated_at) VALUES ('ticket-list-legacy', '旧工单', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO service_tickets (workspace_id, ticket_no, subject, status, priority, channel, source_mode, simulated, created_by, version, created_at, updated_at) VALUES (1, 'T-0001-000001', '迁移旧工单', 'open', 'normal', 'manual', 'manual', 0, 'legacy-agent', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
    from app.employee_auth import hash_password
    with db_mod.SessionLocal() as db:
        user = db_mod.UserAccount(login="ticket-list-legacy", password_hash=hash_password("ticket-password"), display_name="客服")
        db.add(user); db.flush(); db.add(db_mod.WorkspaceMembership(workspace_id=1, user_id=user.id, role="customer_service")); db.commit()
    with TestClient(main_mod.app) as client:
        login = client.post('/login', data={'login':'ticket-list-legacy','password':'ticket-password','next':'/customer-service'}, follow_redirects=False)
        assert login.status_code == 303
        response = client.get('/api/tickets?page_size=100')
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["subject"] == "迁移旧工单"
    bg_mod.reset_executor(); cfg.get_settings.cache_clear()


def test_ticket_create_accepts_business_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ticket-api.db'}")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEMO_MODE_ENABLED", "true")
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    import app.config as cfg
    import app.db as db_mod
    import app.background as bg_mod
    import app.main as main_mod
    cfg.get_settings.cache_clear(); importlib.reload(cfg); importlib.reload(db_mod); importlib.reload(bg_mod); importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    from app.employee_auth import hash_password
    with db_mod.SessionLocal() as db:
        ws = db_mod.Workspace(tenant_key="ticket-api", name="工单 API")
        db.add(ws); db.flush()
        user = db_mod.UserAccount(login="ticket-api", password_hash=hash_password("ticket-password"), display_name="工单客服")
        db.add(user); db.flush(); db.add(db_mod.WorkspaceMembership(workspace_id=ws.id, user_id=user.id, role="customer_service")); db.commit()
    with TestClient(main_mod.app) as client:
        login = client.post('/login', data={'login':'ticket-api','password':'ticket-password','next':'/customer-service'}, follow_redirects=False)
        assert login.status_code == 303
        response = client.post('/api/tickets', json={'subject':'包裹三天没有物流更新','issue_type':'logistics','description':'平台显示已发货但没有轨迹','customer_ref':'C-001','external_order_ref':'DEMO-001','sku_ref':'SKU-1','priority':'high'}, headers={'Idempotency-Key':'ticket-business-1','X-CSRF-Token':client.cookies.get('dianshang_csrf')})
    assert response.status_code == 201, response.text
    assert response.json()['subject'] == '包裹三天没有物流更新'
    bg_mod.reset_executor(); cfg.get_settings.cache_clear()
