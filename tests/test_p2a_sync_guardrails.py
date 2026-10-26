"""P2-0a 同步护栏（热开关 + 双预算 + 可取消）回归测试。

用例编号与 `dev_task.p2a-sync-guardrails.md` §5 一一对应：

| 编号 | 用例 | 测试函数 |
|---|---|---|
| T1  | 预算超限终止（起始即超限 / 取到部分页后超限） | `test_t1_budget_exceeded_terminates_run_before_any_page`、`test_t1_budget_exceeded_midrun_does_not_write_fetched_pages` |
| T2  | 页数预算仍生效（既有行为回归） | `test_t2_page_limit_still_enforced` |
| T3  | 取消终止 | `test_t3_cancel_request_stops_running_sync` |
| T4  | 取消 API 对不存在 / 已结束 run | `test_t4_cancel_unknown_run_returns_404`、`test_t4b_cancel_finished_run_returns_409` |
| T5  | 热开关关闭后 sync 拒绝 | `test_t5_live_switch_off_blocks_sync_and_capabilities` |
| T6  | 热开关需要 admin | `test_t6_live_switch_requires_admin_role` |
| T7  | 正常路径不受影响 | `test_t7_normal_sync_path_succeeds`、`test_t7b_adapter_without_guardrails_is_unchanged` |

说明（与实现的一致性记录）：

- 本文件不改动任何既有测试文件与产品代码，只新增本文件；
- Shopify live 通道由 `FakeShopifyTransport` 替换 `ShopifyLiveTransport`，
  从而**不触碰真实 Shopify API**，但仍然走真实的
  `ShopifyReadOnlyAdapter._pages()` 护栏检查点与真实的同步路由/落库路径；
- 取消语义按实现固定为：run 不存在 → 404 `SYNC_RUN_NOT_FOUND`；
  run 已终态 → 409 `SYNC_RUN_NOT_CANCELLABLE`；运行中 → 202。
"""

import importlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

# Settings 读取走 `app.config.get_settings` 的 lru_cache；用例内改环境变量后
# 必须清缓存，否则路由仍读到 harness fixture 建好的旧配置。
import app.config as cfg


# ---------------------------------------------------------------------------
# 假 transport 与 fixture 数据
# ---------------------------------------------------------------------------

ORDER_NODE = {
    "id": "gid://shopify/Order/1001",
    "name": "#1001",
    "createdAt": "2026-10-01T10:00:00Z",
    "updatedAt": "2026-10-02T10:00:00Z",
    "processedAt": "2026-10-01T10:05:00Z",
    "displayFinancialStatus": "PAID",
    "displayFulfillmentStatus": "FULFILLED",
    "currentTotalPriceSet": {"shopMoney": {"amount": "99.00", "currencyCode": "USD"}},
    "lineItems": {
        "nodes": [
            {
                "id": "line-1",
                "sku": "SKU-1",
                "quantity": 2,
                "originalTotalSet": {"shopMoney": {"amount": "99.00", "currencyCode": "USD"}},
                "variant": {"id": "variant-1", "title": "高山绿茶"},
            }
        ]
    },
}

INVENTORY_NODE = {
    "id": "gid://shopify/InventoryItem/2001",
    "sku": "SKU-1",
    "inventoryLevels": {
        "edges": [
            {
                "node": {
                    "id": "level-1",
                    "quantities": [{"name": "available", "quantity": 7}],
                    "location": {"id": "location-1"},
                }
            }
        ]
    },
}

_NODE_BY_KEY = {"orders": ORDER_NODE, "inventoryItems": INVENTORY_NODE, "products": ORDER_NODE}


class FakeShopifyTransport:
    """替换 `ShopifyLiveTransport` 的假 transport（不发起任何真实网络请求）。

    路由按位置参数构造 transport：`ShopifyLiveTransport(domain, token, version, timeout, retries)`，
    因此这里用可调用实例承接构造调用，`query()` 按页返回 fixture 数据。
    """

    def __init__(self, *, pages: int = 1, always_next: bool = False, page_hook=None, store_domain: str = "probe.myshopify.com"):
        self.pages = pages
        self.always_next = always_next
        self.page_hook = page_hook
        self.store_domain = store_domain
        self.calls: list[str] = []

    def __call__(self, *args, **kwargs):
        return self

    def query(self, *, query: str, variables: dict, request_id: str) -> dict[str, Any]:
        if "inventoryItems(" in query:
            data_key = "inventoryItems"
        elif "orders(" in query:
            data_key = "orders"
        else:
            data_key = "products"
        index = sum(1 for item in self.calls if item == data_key)
        self.calls.append(data_key)
        if self.page_hook is not None:
            self.page_hook(data_key, index)
        has_next = self.always_next or index < self.pages - 1
        return {
            "data": {
                data_key: {
                    "edges": [{"cursor": f"{data_key}-{index}", "node": _NODE_BY_KEY[data_key]}],
                    "pageInfo": {"hasNextPage": has_next, "endCursor": f"{data_key}-{index}"},
                }
            },
            "request_id": request_id,
        }


@dataclass
class Harness:
    client: TestClient
    main: Any
    db_mod: Any


def _login(client: TestClient, login: str, password: str = "pw") -> TestClient:
    response = client.post("/login", data={"login": login, "password": password, "next": "/"}, follow_redirects=False)
    assert response.status_code in (302, 303), response.text
    csrf = client.cookies.get("dianshang_csrf")
    assert csrf, "缺少 CSRF cookie"
    client.headers.update({"X-CSRF-Token": csrf})
    return client


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """构造一个已登录 admin 的 Shopify live 同步测试环境。

    不用 TestClient 的 context manager：lifespan 会拉起后台 worker 线程，
    与 T3 的并发取消场景争抢同一个 SQLite 库；这里显式建表并跳过 lifespan。
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'p2a.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IMPORTS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    # Shopify live 通道开启（真实调用被 FakeShopifyTransport 顶替）。
    monkeypatch.setenv("SHOPIFY_LIVE_ENABLED", "true")
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "probe.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "probe-token")
    monkeypatch.setenv("SHOPIFY_SYNC_BUDGET_SECONDS", "300")

    import app.background as bg_mod
    import app.config as cfg
    import app.db as db_mod
    import app.repository as repo_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(repo_mod)
    importlib.reload(bg_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    bg_mod.reset_executor()

    import app.runtime_flags as runtime_flags

    # 运行期热开关是进程内全局状态：用例之间必须复位，避免互相污染。
    runtime_flags.set_live_enabled(True)

    from app.employee_auth import hash_password

    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="p2a", name="P2-0a 护栏商家")
        db.add(workspace)
        db.flush()
        admin = db_mod.UserAccount(login="admin", password_hash=hash_password("pw"), display_name="护栏管理员")
        operator = db_mod.UserAccount(login="ops", password_hash=hash_password("pw"), display_name="护栏运营")
        db.add_all([admin, operator])
        db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=admin.id, role="admin"))
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=operator.id, role="operations"))
        db.commit()

    import app.main as main_mod

    importlib.reload(main_mod)

    client = TestClient(main_mod.app)
    _login(client, "admin")

    yield Harness(client=client, main=main_mod, db_mod=db_mod)

    runtime_flags.set_live_enabled(True)
    bg_mod.reset_executor()
    cfg.get_settings.cache_clear()


def _install_fake_transport(monkeypatch, transport: FakeShopifyTransport) -> FakeShopifyTransport:
    """把 `app.platform_adapters.shopify.ShopifyLiveTransport` 换成假 transport。

    同步路由在函数体内 `from app.platform_adapters.shopify import ShopifyLiveTransport`，
    因此替换模块属性即可生效，无需改动产品代码。
    """
    import app.platform_adapters.shopify as shopify_mod

    monkeypatch.setattr(shopify_mod, "ShopifyLiveTransport", transport)
    return transport


def _run_row(db_mod, run_id: str) -> dict[str, Any]:
    with db_mod.SessionLocal() as db:
        run = db.query(db_mod.ExternalSyncRun).filter(db_mod.ExternalSyncRun.run_id == run_id).one()
        return {
            "run_id": run.run_id,
            "status": run.status,
            "error_code": run.error_code,
            "total": run.total,
            "inserted": run.inserted,
            "resource_status": json.loads(run.resource_status_json or "{}"),
        }


def _row_counts(db_mod) -> tuple[int, int]:
    with db_mod.SessionLocal() as db:
        return (
            db.query(db_mod.ExternalOrder).count(),
            db.query(db_mod.ExternalInventorySnapshot).count(),
        )


# ---------------------------------------------------------------------------
# T1 预算超限终止
# ---------------------------------------------------------------------------


def test_t1_budget_exceeded_terminates_run_before_any_page(harness, monkeypatch):
    """T1：wall-clock 预算为 0 时，同步在第一个检查点即终止。"""
    monkeypatch.setenv("SHOPIFY_SYNC_BUDGET_SECONDS", "0")
    cfg.get_settings.cache_clear()
    transport = _install_fake_transport(monkeypatch, FakeShopifyTransport(always_next=True))

    response = harness.client.post("/api/external/shopify/sync?resources=orders")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resources"]["orders"]["error_code"] == "SHOPIFY_SYNC_BUDGET_EXCEEDED"
    assert body["status"] == "failed"
    assert body["error_code"] == "SHOPIFY_SYNC_BUDGET_EXCEEDED"
    # 预算在取第一页之前就超限：一个页都没取。
    assert transport.calls == []
    run = _run_row(harness.db_mod, body["run_id"])
    assert run["status"] == "failed"
    assert run["error_code"] == "SHOPIFY_SYNC_BUDGET_EXCEEDED"
    assert run["resource_status"]["orders"]["error_code"] == "SHOPIFY_SYNC_BUDGET_EXCEEDED"
    assert run["resource_status"]["orders"]["pages_used"] == 0
    assert run["resource_status"]["orders"]["budget_seconds"] == 0


def test_t1_budget_exceeded_midrun_does_not_write_fetched_pages(harness, monkeypatch):
    """T1：预算在取到若干页之后超限时，run 失败且已取到的页不写入内部事实层。"""
    monkeypatch.setenv("SHOPIFY_SYNC_BUDGET_SECONDS", "1")
    cfg.get_settings.cache_clear()

    def page_hook(data_key: str, index: int) -> None:
        # 每页耗时超过 1 秒预算：第二页的检查点必然超限。
        time.sleep(1.2)

    transport = _install_fake_transport(monkeypatch, FakeShopifyTransport(always_next=True, page_hook=page_hook))

    response = harness.client.post("/api/external/shopify/sync?resources=orders,inventory")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resources"]["orders"]["error_code"] == "SHOPIFY_SYNC_BUDGET_EXCEEDED"
    assert body["resources"]["orders"]["pages_used"] == 1
    assert body["status"] == "failed"
    assert body["error_code"] == "SHOPIFY_SYNC_BUDGET_EXCEEDED"
    # 只取到 1 页就停住（没有把 max_pages 跑满）。
    assert transport.calls.count("orders") == 1
    run = _run_row(harness.db_mod, body["run_id"])
    assert run["status"] == "failed"
    assert run["error_code"] == "SHOPIFY_SYNC_BUDGET_EXCEEDED"
    assert run["inserted"] == 0
    # 已取到的页不写入：订单与库存台账都为空。
    assert _row_counts(harness.db_mod) == (0, 0)


# ---------------------------------------------------------------------------
# T2 页数预算仍生效
# ---------------------------------------------------------------------------


def test_t2_page_limit_still_enforced(harness, monkeypatch):
    """T2：页数预算（max_pages）仍是既有行为——持续 hasNextPage 时报 SHOPIFY_PAGE_LIMIT。"""
    monkeypatch.setenv("SHOPIFY_MAX_PAGES", "2")
    cfg.get_settings.cache_clear()
    transport = _install_fake_transport(monkeypatch, FakeShopifyTransport(always_next=True))

    response = harness.client.post("/api/external/shopify/sync?resources=orders")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resources"]["orders"]["error_code"] == "SHOPIFY_PAGE_LIMIT"
    assert body["resources"]["orders"]["pages_used"] == 2
    assert transport.calls.count("orders") == 2
    run = _run_row(harness.db_mod, body["run_id"])
    assert run["resource_status"]["orders"]["error_code"] == "SHOPIFY_PAGE_LIMIT"
    assert run["resource_status"]["orders"]["pages_used"] == 2


# ---------------------------------------------------------------------------
# T3 取消终止
# ---------------------------------------------------------------------------


def test_t3_cancel_request_stops_running_sync(harness, monkeypatch):
    """T3：运行中的 sync 被取消后，run 终态为 failed + SHOPIFY_SYNC_CANCELLED。"""
    first_page = threading.Event()

    def page_hook(data_key: str, index: int) -> None:
        if data_key == "orders" and index == 0:
            first_page.set()
        # 每页留出窗口，保证取消请求一定能在后续检查点之前送达。
        time.sleep(0.2)

    transport = _install_fake_transport(monkeypatch, FakeShopifyTransport(always_next=True, page_hook=page_hook))

    box: dict[str, Any] = {}

    def worker() -> None:
        box["response"] = harness.client.post("/api/external/shopify/sync?resources=orders")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert first_page.wait(10), "同步线程没有进入第一页"

    # 从台账里取运行中的 run_id（run 在 begin_sync_run 时已落库）。
    run_id = None
    deadline = time.monotonic() + 10
    while run_id is None and time.monotonic() < deadline:
        with harness.db_mod.SessionLocal() as db:
            run = (
                db.query(harness.db_mod.ExternalSyncRun)
                .filter(harness.db_mod.ExternalSyncRun.status == "running")
                .order_by(harness.db_mod.ExternalSyncRun.id.desc())
                .first()
            )
        if run is not None:
            run_id = run.run_id
        else:
            time.sleep(0.05)
    assert run_id, "没有找到运行中的 sync run"

    cancel = harness.client.post(f"/api/external/sync/runs/{run_id}/cancel")
    assert cancel.status_code == 202, cancel.text
    assert cancel.json()["run_id"] == run_id
    assert cancel.json()["cancel_requested"] is True

    thread.join(30)
    assert not thread.is_alive(), "同步线程没有在取消后退出"

    response = box["response"]
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_id"] == run_id
    assert body["resources"]["orders"]["error_code"] == "SHOPIFY_SYNC_CANCELLED"
    assert body["status"] == "failed"
    assert body["error_code"] == "SHOPIFY_SYNC_CANCELLED"
    # 取消在下一个分页检查点生效：不会把 100 页跑满。
    assert transport.calls.count("orders") <= 5
    run = _run_row(harness.db_mod, run_id)
    assert run["status"] == "failed"
    assert run["error_code"] == "SHOPIFY_SYNC_CANCELLED"
    assert run["resource_status"]["orders"]["error_code"] == "SHOPIFY_SYNC_CANCELLED"
    # 已取到的页不写入内部事实层。
    assert _row_counts(harness.db_mod) == (0, 0)
    # 取消 token 在 run 结束后注销，不泄漏。
    assert run_id not in harness.main._sync_cancel_tokens


# ---------------------------------------------------------------------------
# T4 取消 API 语义
# ---------------------------------------------------------------------------


def test_t4_cancel_unknown_run_returns_404(harness):
    """T4：取消一个不存在的 run → 404 SYNC_RUN_NOT_FOUND。"""
    response = harness.client.post("/api/external/sync/runs/run-does-not-exist/cancel")

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "SYNC_RUN_NOT_FOUND"


def test_t4b_cancel_finished_run_returns_409(harness, monkeypatch):
    """T4：取消一个已结束的 run → 409 SYNC_RUN_NOT_CANCELLABLE。"""
    _install_fake_transport(monkeypatch, FakeShopifyTransport(pages=1))

    response = harness.client.post("/api/external/shopify/sync?resources=orders")
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    assert response.json()["status"] == "succeeded"

    cancel = harness.client.post(f"/api/external/sync/runs/{run_id}/cancel")

    assert cancel.status_code == 409, cancel.text
    assert cancel.json()["detail"]["code"] == "SYNC_RUN_NOT_CANCELLABLE"


# ---------------------------------------------------------------------------
# T5 热开关
# ---------------------------------------------------------------------------


def test_t5_live_switch_off_blocks_sync_and_capabilities(harness, monkeypatch):
    """T5：admin 关掉热开关后，sync 被拒（409），capabilities 报告 live_enabled=false。"""
    _install_fake_transport(monkeypatch, FakeShopifyTransport(always_next=True))

    before = harness.client.get("/api/external/shopify/capabilities")
    assert before.status_code == 200, before.text
    assert before.json()["live_enabled"] is True

    switch = harness.client.post("/api/admin/live-switch", json={"live_enabled": False})
    assert switch.status_code == 200, switch.text
    assert switch.json() == {"live_enabled": False, "source": "runtime_flag"}

    caps = harness.client.get("/api/external/shopify/capabilities")
    assert caps.status_code == 200, caps.text
    assert caps.json()["live_enabled"] is False
    assert caps.json()["simulated"] is True

    sync = harness.client.post("/api/external/shopify/sync?resources=orders")
    assert sync.status_code == 409, sync.text
    assert sync.json()["detail"]["code"] == "SHOPIFY_LIVE_NOT_CONFIGURED"

    # 重新打开后恢复放行（热开关是可逆的运行期开关）。
    reopen = harness.client.post("/api/admin/live-switch", json={"live_enabled": True})
    assert reopen.status_code == 200, reopen.text
    assert reopen.json() == {"live_enabled": True, "source": "runtime_flag"}
    assert harness.client.get("/api/external/shopify/capabilities").json()["live_enabled"] is True


def test_t6_live_switch_requires_admin_role(harness, monkeypatch):
    """T6：非 admin 调用 live-switch → 403，且开关状态不受影响。"""
    _install_fake_transport(monkeypatch, FakeShopifyTransport(always_next=True))

    operator = TestClient(harness.main.app)
    _login(operator, "ops")

    response = operator.post("/api/admin/live-switch", json={"live_enabled": False})

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "FORBIDDEN"
    # 被拒绝的调用没有改动运行期开关。
    assert harness.client.get("/api/external/shopify/capabilities").json()["live_enabled"] is True


# ---------------------------------------------------------------------------
# T7 正常路径不受影响
# ---------------------------------------------------------------------------


def test_t7_normal_sync_path_succeeds(harness, monkeypatch):
    """T7：护栏不改变正常路径——同步成功、页数留痕写入台账。"""
    transport = _install_fake_transport(monkeypatch, FakeShopifyTransport(pages=1))

    response = harness.client.post("/api/external/shopify/sync?resources=orders,inventory")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["error_code"] is None
    assert body["resources"]["orders"]["inserted"] == 1
    assert body["resources"]["inventory"]["inserted"] == 1
    assert body["resources"]["orders"]["pages_used"] == 1
    assert body["resources"]["inventory"]["pages_used"] == 1
    assert transport.calls.count("orders") == 1
    assert transport.calls.count("inventoryItems") == 1

    run = _run_row(harness.db_mod, body["run_id"])
    assert run["status"] == "succeeded"
    assert run["error_code"] is None
    assert run["resource_status"]["orders"]["status"] == "succeeded"
    assert run["resource_status"]["orders"]["pages_used"] == 1
    assert run["resource_status"]["orders"]["budget_seconds"] == 300
    assert run["resource_status"]["inventory"]["status"] == "succeeded"
    assert run["resource_status"]["inventory"]["pages_used"] == 1
    assert _row_counts(harness.db_mod) == (1, 1)


def test_t7b_adapter_without_guardrails_is_unchanged(harness):
    """T7：护栏参数缺省时，adapter 的行为与引入护栏前一致（fixture 路径不受影响）。"""
    from app.platform_adapters.shopify import ShopifyFixtureTransport, ShopifyReadOnlyAdapter

    responses = {
        "orders": {
            "data": {
                "orders": {
                    "edges": [{"cursor": "c1", "node": ORDER_NODE}],
                    "pageInfo": {"hasNextPage": False, "endCursor": "c1"},
                }
            }
        }
    }
    adapter = ShopifyReadOnlyAdapter(ShopifyFixtureTransport(responses))

    records = adapter.fetch_orders()

    assert len(records) == 1
    assert records[0]["id"] == ORDER_NODE["id"]
    assert adapter.pages_used == 1
    assert adapter.simulated is True
    assert adapter.live_enabled is False
