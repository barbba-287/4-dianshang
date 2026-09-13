"""库存看板 API 合同测试。"""

import importlib
from datetime import date, datetime, timedelta
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


def test_dashboard_uses_latest_sales_business_date(client):
    import app.db as db_mod

    cutoff = date(2026, 9, 7)
    with db_mod.SessionLocal() as db:
        warehouse = db_mod.Warehouse(workspace_id=1, code="W-DATE", name="日期仓", warehouse_type="own")
        db.add(warehouse)
        db.flush()
        sku = db_mod.ProductSku(workspace_id=1, product_id=1, sku_code="DATE-SKU")
        db.add(sku)
        db.flush()
        db.add(db_mod.InventoryBalance(workspace_id=1, warehouse_id=warehouse.id, sku_id=sku.id, on_hand_qty=28))
        account = db_mod.ExternalAccount(workspace_id=1, platform="mock", account_ref="date-account", store_ref="date-store")
        db.add(account)
        db.flush()
        for offset in range(14):
            sales_date = cutoff - timedelta(days=13 - offset)
            db.add(db_mod.DailySkuSale(
                workspace_id=1, external_account_id=account.id, platform="mock",
                account_ref="date-account", store_ref="date-store", external_sku="DATE-SKU",
                internal_sku_id=sku.id, sales_date=sales_date, gross_qty=2,
                cancelled_qty=0, refunded_qty=0, net_qty=2,
                gross_amount=Decimal("20.00"), refund_amount=Decimal("0"),
                net_amount=Decimal("20.00"), order_count=1, data_completeness="complete",
            ))
        db.commit()

    response = client.get("/api/dashboard/summary")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sales_summary"]["as_of"] == cutoff.isoformat()
    item = next(row for row in body["sku_health"] if row["sku_code"] == "DATE-SKU")
    assert item["sales"]["14"]["daily_avg_qty"] == 2.0
    assert item["days_of_inventory"] == 14.0

    explicit = client.get("/api/dashboard/summary", params={"as_of": cutoff.isoformat()})
    assert explicit.status_code == 200, explicit.text
    assert explicit.json()["sales_summary"]["as_of"] == cutoff.isoformat()


def test_dashboard_summary_empty(client):
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
    assert '/api/analytics/product-quadrant' in response.text
    assert 'id="product-quadrant-panel"' in response.text
    assert 'id="quadrant-chart"' in response.text
    assert 'id="quadrant-summary"' in response.text
    assert 'id="quadrant-insufficient"' in response.text
    assert 'id="quadrant-category-tables"' in response.text
    assert 'id="quadrant-table-focal_supplement"' in response.text
    assert 'id="quadrant-table-healthy"' in response.text
    assert 'id="quadrant-table-watch"' in response.text
    assert 'id="quadrant-table-slow_risk"' in response.text
    assert 'function quadrantIsInsufficient' in response.text
    assert 'item.growth_rate!=null&&item.days_of_inventory!=null' not in response.text
    assert 'quadrantIsInsufficient(item)' in response.text
    assert 'growth_window:\'7\'' in response.text
    assert 'baseline_window:\'14\'' in response.text
    assert 'page_size:\'100\'' in response.text
    assert '库存/可售天数按当前选择仓库范围统计，销量未按仓库过滤' in response.text
    assert 'quadrantChart?.resize()' in response.text
    assert '<title>运营驾驶舱</title>' in response.text
    assert '<h1>运营驾驶舱</h1>' in response.text
    for marker in ('sales-trend-panel','inventory-risk-panel','product-quadrant-panel','sync-health-panel','sku-health-panel','alerts-panel','recent-inbounds-panel','metric-definitions-panel'):
        assert f'id="{marker}"' in response.text
    assert response.text.count('id="logout"') == 1
    assert 'id="logout-link"' not in response.text
    assert '商品与补货' not in response.text
    assert 'id="sync-health-panel"' in response.text
    assert '/api/external/sync/runs?limit=20' in response.text
    assert '/api/external/sync/runs/${encodeURIComponent(runId)}/retry' in response.text
    assert 'Idempotency-Key' in response.text
    assert 'X-CSRF-Token' in response.text
    assert 'retryable_resources' in response.text
    assert 'orders_content' in response.text
    assert 'inventory_content' in response.text


def test_workspace_sidebar_is_fixed_and_scrollable(client):
    response = client.get('/dashboard')
    assert response.status_code == 200
    css = client.get('/static/workspace-shell.css').text
    assert 'position:fixed' in css
    assert 'height:100vh' in css
    assert 'overflow-y:auto' in css
    assert 'margin-left:232px' in css
    assert 'margin-left:76px' in css
