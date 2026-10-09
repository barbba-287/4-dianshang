"""电商客服业务表单页面契约测试。"""
import importlib
from fastapi.testclient import TestClient

def test_customer_service_page_has_business_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'customer-page.db'}")
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
        ws = db_mod.Workspace(tenant_key="customer-page", name="客服页面")
        db.add(ws); db.flush()
        user = db_mod.UserAccount(login="customer-page", password_hash=hash_password("customer-password"), display_name="客服")
        db.add(user); db.flush(); db.add(db_mod.WorkspaceMembership(workspace_id=ws.id, user_id=user.id, role="customer_service")); db.commit()
    with TestClient(main_mod.app) as client:
        login = client.post('/login', data={'login':'customer-page','password':'customer-password','next':'/customer-service'}, follow_redirects=False)
        assert login.status_code == 303
        response = client.get('/customer-service')
    assert response.status_code == 200
    html = response.text
    for marker in ('售前咨询', '订单支付', '物流配送', '退货退款', '商品质量', '投诉升级', '客户/客户编号', '订单号', '商品/SKU', '问题描述', '内部备注', '输入片段，如 001', '当前订单事实暂无客户检索字段', 'setupOrderSuggest', 'setupSkuSuggest', '跨人协作', '只有需要跟进'):
        assert marker in html
    assert '不自动回复' in html or 'AI 建议' in html
    assert '不自动关闭工单' in html or '不自动关闭' in html
    bg_mod.reset_executor(); cfg.get_settings.cache_clear()
