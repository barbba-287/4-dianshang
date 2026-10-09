"""订单事实页面契约测试。"""

import importlib


def test_order_page_contains_read_only_boundary_and_filters(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'orders-page.db'}")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    import app.config as cfg
    import app.db as db_mod
    import app.background as bg_mod
    import app.main as main_mod
    cfg.get_settings.cache_clear(); importlib.reload(cfg); importlib.reload(db_mod); importlib.reload(bg_mod); importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    from app.employee_auth import hash_password
    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="orders-page", name="订单页面")
        db.add(workspace); db.flush()
        user = db_mod.UserAccount(login="orders-page", password_hash=hash_password("orders-page-password"), display_name="订单页面")
        db.add(user); db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="operations")); db.commit()
    from fastapi.testclient import TestClient
    with TestClient(main_mod.app) as client:
        login = client.post("/login", data={"login":"orders-page", "password":"orders-page-password", "next":"/orders"}, follow_redirects=False)
        assert login.status_code == 303
        response = client.get("/orders")
    assert response.status_code == 200
    html = response.text
    for marker in ("订单事实", "外部订单事实", "待人工处理", "待发（外部已支付）", "fulfillment_view", "/api/orders", "async function load()", "async function loadDetail", "body=await api(`/api/orders?", "render();", "load();", "导出待发清单（人工履约）", "filename\\*=UTF-8", "decodeURIComponent", "待发清单-人工履约.xlsx", "人工履约 Proposal", "不执行发货", "付款", "退款"):
        assert marker in html
    assert "confirm-shipment" not in html
    assert "自动发货" not in html
    bg_mod.reset_executor(); cfg.get_settings.cache_clear()


def test_navigation_exposes_orders_to_operations_only():
    from app.employee_auth import Principal
    import app.main as main_mod
    operations = main_mod._visible_navigation(Principal(subject="op", tenant_id="test", roles=("operations",), auth_type="session"), "/orders")
    warehouse = main_mod._visible_navigation(Principal(subject="wh", tenant_id="test", roles=("warehouse",), auth_type="session"), "/warehouse")
    assert 'href="/orders"' in operations
    assert "订单事实" in operations
    assert 'href="/orders"' not in warehouse
