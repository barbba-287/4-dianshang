"""R4 live 同步断点续跑（`POST /api/external/sync/runs/{run_id}/resume`）回归测试。

用例编号与 `dev_task.r4-resume.md` §4 一一对应：

| 编号 | 场景 | 测试函数 |
|---|---|---|
| R4-1 | status=succeeded 的 run 调 resume | `test_r4_1_succeeded_run_cannot_be_resumed` |
| R4-2 | 失败的 orders-only run（cursor=orders-0，complete=false）续跑 | `test_r4_2_resume_continues_from_source_cursor` |
| R4-3 | inventory 已 succeeded（complete=true）+ orders 失败 | `test_r4_3_completed_resource_is_skipped_without_upstream_call` |
| R4-4 | 续跑新 run 超预算（预算调 0） | `test_r4_4_budget_exceeded_leaves_source_run_untouched` |
| R4-5 | 源 run 运行中（S4 卡页法）调 resume | `test_r4_5_resume_rejected_while_sync_is_running` |
| R4-6 | 源 run 没有可用游标（列 NOT NULL，NULL 形态不可达；用损坏串/空串） | `test_r4_6_null_or_corrupted_cursor_is_rejected` |
| R4-6b | 源 run `cursor_json == "{}"`（未开始分页） | `test_r4_6b_empty_cursor_restarts_full_sync` |
| R4-7 | `cursor_json.schema_version=99` | `test_r4_7_unsupported_schema_version_is_rejected` |
| R4-8 | `cursor_json` 缺 `schema_version` 键 | `test_r4_8_missing_schema_version_is_rejected` |
| R4-9 | 续跑途中取消 | `test_r4_9_cancel_during_resume_keeps_cursor` |
| R4-10 | 链式续跑（A → B → C） | `test_r4_10_chained_resume_links_to_direct_predecessor` |
| R4-11 | 闸门反方向：resume 在跑时发 `/sync` | `test_r4_11_sync_rejected_while_resume_is_running` |
| R4-12 | TOCTOU：前置校验通过后源 run 状态被改写 | `test_r4_12_toctou_reread_rejects_concurrent_state_change` |
| R4-13 | R-31 去重兜底复核（双维度）：重入重复拉取只多翻页不重复写数 | `test_r4_13_reentry_duplicates_absorbed_after_resume` |

harness（假 transport / fixture / 登录 / 库）整体照抄 `tests/test_p2a_sync_guardrails.py`，
只做 4 处改名（库名 `r4_resume.db`、留存目录前缀、workspace `tenant_key="r4"`、
商家名），保证与既有护栏测试同一套语义。

口径说明：

- Shopify live 通道由假 transport 顶替 `ShopifyLiveTransport`，**不触碰真实 Shopify API**，
  但仍走真实的 `ShopifyReadOnlyAdapter._pages()` 与真实的 `/resume` 路由、落库路径；
- 假 transport 的 `page_hook(data_key, index)` 里 `index` 是**绝对页序**（0 基，含源 run
  已取走的页），因此"续跑从第 N+1 页开始"可以被确定性地断言；
- 409 一律断言 `detail.code` + `detail.message` 里的 `reason=<XXX>`（reason 字符串与
  任务文件 §2.2 逐字一致；code 只作辅助断言）。
"""

import importlib
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

# Settings 读取走 `app.config.get_settings` 的 lru_cache；用例内改环境变量后
# 必须清缓存，否则路由仍读到 harness fixture 建好的旧配置。
import app.config as cfg


# ---------------------------------------------------------------------------
# 假 transport 与 fixture 数据（照抄 p2a）
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


class ResumeFakeTransport(FakeShopifyTransport):
    """续跑专用假 transport：**按传入的 `after` 游标定位续跑起点**。

    - 游标形如 `orders-K` 表示"第 K 页的 endCursor"→ 下一段从第 K+1 页开始；
    - `after` 为 None（RESTART / 全量重拉）→ 从第 0 页开始；
    - `total_pages` 是该资源在上游的总页数（`hasNextPage` 判定：页序+1 < total_pages）；
    - `page_hook(data_key, index)` 的 `index` 是**绝对页序**（0 基）。

    与 p2a 假 transport 的唯一差别：**起点由 `variables["after"]` 决定**。
    因此"续跑（RESUME，从源游标继续）"与"全量重拉（RESTART，从第 1 页开始）"
    在上游调用次数上可区分——R4-2 断言"调用次数 == 总页数−1"正是回退实验
    （把 RESUME 退化成 RESTART）的红点。
    """

    def __init__(self, *, total_pages: int = 1, always_next: bool = False, page_hook=None, store_domain: str = "probe.myshopify.com"):
        super().__init__(pages=total_pages, always_next=always_next, page_hook=page_hook, store_domain=store_domain)
        self.total_pages = total_pages

    def query(self, *, query: str, variables: dict, request_id: str) -> dict[str, Any]:
        if "inventoryItems(" in query:
            data_key = "inventoryItems"
        elif "orders(" in query:
            data_key = "orders"
        else:
            data_key = "products"
        # 续跑起点由游标决定：after="orders-K" → 下一段从第 K+1 页开始。
        after = (variables or {}).get("after")
        if after:
            try:
                index = int(str(after).rsplit("-", 1)[-1]) + 1
            except ValueError:
                index = 0
        else:
            index = 0
        self.calls.append(data_key)
        if self.page_hook is not None:
            self.page_hook(data_key, index)
        has_next = self.always_next or (index + 1) < self.total_pages
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
    与并发卡页场景争抢同一个 SQLite 库；这里显式建表并跳过 lifespan。
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'r4_resume.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "r4-artifacts"))
    monkeypatch.setenv("IMPORTS_DIR", str(tmp_path / "r4-uploads"))
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    # Shopify live 通道开启（真实调用被假 transport 顶替）。
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
        workspace = db_mod.Workspace(tenant_key="r4", name="R4 续跑商家")
        db.add(workspace)
        db.flush()
        admin = db_mod.UserAccount(login="admin", password_hash=hash_password("pw"), display_name="续跑管理员")
        operator = db_mod.UserAccount(login="ops", password_hash=hash_password("pw"), display_name="续跑运营")
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

    同步/续跑路由在函数体内 `from app.platform_adapters.shopify import ShopifyLiveTransport`，
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
# R4 专用 helper
# ---------------------------------------------------------------------------

DEFAULT_ORDERS_CURSOR = {
    "schema_version": 1,
    "resources": {
        "orders": {"cursor": "orders-0", "pages": 1, "fetched": 1, "complete": False},
    },
}


def _seed_source_run(
    harness,
    *,
    status: str = "failed",
    cursor_json: str | None = None,
    attempt: int = 1,
    platform: str = "shopify",
    source_mode: str = "shopify_live",
    resource_status_json: str | None = None,
) -> str:
    """直接在库里插一条源 run（R4 的前置态），返回 run_id。

    默认前置态：orders 失败、游标 `orders-0`（1 页，未取完）——即"第 1 页取到一半失败"
    的典型可续跑现场。
    """
    db_mod = harness.db_mod
    if cursor_json is None:
        cursor_json = json.dumps(DEFAULT_ORDERS_CURSOR)
    if resource_status_json is None:
        resource_status_json = json.dumps(
            {"schema_version": 1, "orders": {"status": "failed", "execution": "attempted"}}
        )
    with db_mod.SessionLocal() as db:
        workspace = db.query(db_mod.Workspace).first()
        run = db_mod.ExternalSyncRun(
            workspace_id=workspace.id,
            run_id=uuid4().hex,
            platform=platform,
            sync_type="shopify_live",
            account_ref="probe.myshopify.com",
            store_ref="probe.myshopify.com",
            source_mode=source_mode,
            simulated=False,
            status=status,
            attempt=attempt,
            resource_status_json=resource_status_json,
            retryable_resources_json="[]",
            cursor_json=cursor_json,
            heartbeat_at=datetime.utcnow(),
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run.run_id


def _run_detail(db_mod, run_id: str) -> dict[str, Any]:
    """读出一个 run 的链路元数据（attempt / retry_of / 游标 / 台账）。"""
    with db_mod.SessionLocal() as db:
        run = db.query(db_mod.ExternalSyncRun).filter(db_mod.ExternalSyncRun.run_id == run_id).one()
        return {
            "run_id": run.run_id,
            "status": run.status,
            "error_code": run.error_code,
            "attempt": run.attempt,
            "retry_of_run_id": run.retry_of_run_id,
            "cursor_json_raw": run.cursor_json,
            "cursor_json": json.loads(run.cursor_json or "{}"),
            "resource_status": json.loads(run.resource_status_json or "{}"),
        }


def _wait_for_run(db_mod, predicate, timeout: float = 10.0) -> str | None:
    """轮询库直到出现满足 predicate 的 run（不用 sleep 赌时序，用超时兜底）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with db_mod.SessionLocal() as db:
            for run in db.query(db_mod.ExternalSyncRun).all():
                if predicate(run):
                    return run.run_id
        time.sleep(0.05)
    return None


def _assert_reason(response, reason: str) -> None:
    """409 断言铁律：reason 串（`reason=<XXX>`）为准，code 只作辅助断言。"""
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "SYNC_RUN_NOT_RESUMABLE", response.text
    assert f"reason={reason}" in detail["message"], response.text


# ---------------------------------------------------------------------------
# R4-1 终态 run 不可续跑
# ---------------------------------------------------------------------------


def test_r4_1_succeeded_run_cannot_be_resumed(harness, monkeypatch):
    """R4-1：status=succeeded 的 run 调 resume → 409，reason=NOT_RESUMABLE_STATE。"""
    transport = _install_fake_transport(monkeypatch, ResumeFakeTransport(total_pages=1))
    run_id = _seed_source_run(harness, status="succeeded")

    response = harness.client.post(f"/api/external/sync/runs/{run_id}/resume")

    _assert_reason(response, "NOT_RESUMABLE_STATE")
    # 拒绝发生在闸门之前：一个上游请求都不该发出，也不该建新 run。
    assert transport.calls == []
    assert _run_detail(harness.db_mod, run_id)["status"] == "succeeded"


# ---------------------------------------------------------------------------
# R4-2 从源游标续跑
# ---------------------------------------------------------------------------


def test_r4_2_resume_continues_from_source_cursor(harness, monkeypatch):
    """R4-2：源 run 已取 1 页（orders-0，未取完）→ 续跑只拉剩下的 1 页。"""
    transport = _install_fake_transport(
        monkeypatch, ResumeFakeTransport(total_pages=2)
    )
    run_id = _seed_source_run(harness)

    response = harness.client.post(f"/api/external/sync/runs/{run_id}/resume")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resumed_from"] == run_id
    assert body["status"] == "succeeded", body
    # 总页数 2、源 run 已取 1 页 → 本次只拉 1 页（总页数−1）。
    assert transport.calls.count("orders") == 1

    detail = _run_detail(harness.db_mod, body["run_id"])
    assert detail["attempt"] == 2
    assert detail["retry_of_run_id"] == run_id
    assert detail["resource_status"]["resumed_from"] == run_id
    # 累计口径：源 1 页 + 本次 1 页 = 2 页（不归零、不回退）。
    orders_cursor = detail["cursor_json"]["resources"]["orders"]
    assert orders_cursor["pages"] == 2
    assert orders_cursor["fetched"] == 2
    # 单 run 口径：本次 run 自己只消耗 1 页。
    assert detail["resource_status"]["orders"]["pages_used"] == 1


# ---------------------------------------------------------------------------
# R4-3 已取完的资源跳过（零上游调用）
# ---------------------------------------------------------------------------


def test_r4_3_completed_resource_is_skipped_without_upstream_call(harness, monkeypatch):
    """R4-3：inventory 已 complete=true → SKIP（零上游调用）；orders 照常续跑。"""
    transport = _install_fake_transport(
        monkeypatch, ResumeFakeTransport(total_pages=2)
    )
    cursor = {
        "schema_version": 1,
        "resources": {
            "orders": {"cursor": "orders-0", "pages": 1, "fetched": 1, "complete": False},
            "inventory": {"cursor": None, "pages": 1, "fetched": 1, "complete": True},
        },
    }
    statuses = {
        "schema_version": 1,
        "orders": {"status": "failed", "execution": "attempted"},
        "inventory": {"status": "succeeded", "execution": "attempted"},
    }
    run_id = _seed_source_run(
        harness, cursor_json=json.dumps(cursor), resource_status_json=json.dumps(statuses)
    )

    response = harness.client.post(f"/api/external/sync/runs/{run_id}/resume")

    assert response.status_code == 200, response.text
    body = response.json()
    # inventory：零上游调用。
    assert "inventoryItems" not in transport.calls
    # orders：正常续跑 1 页。
    assert transport.calls.count("orders") == 1
    # 响应体标注 SKIP 与源基数。
    assert body["resources"]["inventory"]["resumed"] == "skip"
    assert body["resources"]["inventory"]["source_pages"] == 1
    assert body["resources"]["inventory"]["source_fetched"] == 1

    detail = _run_detail(harness.db_mod, body["run_id"])
    inventory_status = detail["resource_status"]["inventory"]
    assert inventory_status["resumed"] == "skip"
    assert inventory_status["resumed_from_run"] == run_id
    assert inventory_status["source_pages"] == 1
    assert inventory_status["source_fetched"] == 1
    # SKIP 不写 pages_used（没拉页）。
    assert "pages_used" not in inventory_status


# ---------------------------------------------------------------------------
# R4-4 续跑超预算，源 run 不受影响
# ---------------------------------------------------------------------------


def test_r4_4_budget_exceeded_leaves_source_run_untouched(harness, monkeypatch):
    """R4-4：续跑新 run 超预算 → 新 run failed + SHOPIFY_SYNC_BUDGET_EXCEEDED；源 run 不动。"""
    monkeypatch.setenv("SHOPIFY_SYNC_BUDGET_SECONDS", "0")
    cfg.get_settings.cache_clear()
    transport = _install_fake_transport(
        monkeypatch, ResumeFakeTransport(total_pages=2)
    )
    run_id = _seed_source_run(harness)
    before = _run_detail(harness.db_mod, run_id)

    response = harness.client.post(f"/api/external/sync/runs/{run_id}/resume")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed", body
    assert body["error_code"] == "SHOPIFY_SYNC_BUDGET_EXCEEDED", body
    assert body["resources"]["orders"]["error_code"] == "SHOPIFY_SYNC_BUDGET_EXCEEDED"
    # 预算在第一页之前就超限：一个页都没取。
    assert transport.calls == []

    detail = _run_detail(harness.db_mod, body["run_id"])
    assert detail["status"] == "failed"
    assert detail["error_code"] == "SHOPIFY_SYNC_BUDGET_EXCEEDED"

    # 源 run 的状态与游标逐字节不变（续跑不得回写源 run）。
    after = _run_detail(harness.db_mod, run_id)
    assert after["status"] == before["status"] == "failed"
    assert after["cursor_json_raw"] == before["cursor_json_raw"]


# ---------------------------------------------------------------------------
# R4-5 闸门共享：sync 在跑时 resume 被拒
# ---------------------------------------------------------------------------


def test_r4_5_resume_rejected_while_sync_is_running(harness, monkeypatch):
    """R4-5：live sync 在跑（S4 卡页法）时调 resume → 409 SHOPIFY_SYNC_IN_PROGRESS。"""
    first_page = threading.Event()
    release = threading.Event()

    def page_hook(data_key: str, index: int) -> None:
        if data_key == "orders" and index == 0:
            first_page.set()
            release.wait(30)

    transport = _install_fake_transport(
        monkeypatch,
        ResumeFakeTransport(total_pages=3, page_hook=page_hook),
    )
    run_id = _seed_source_run(harness)

    box: dict[str, Any] = {}

    def worker() -> None:
        box["response"] = harness.client.post("/api/external/shopify/sync?resources=orders")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert first_page.wait(10), "同步线程没有进入第一页"

    # 闸门被 sync 占着：resume 必须被拒（证明两者共用同一把单飞锁）。
    response = harness.client.post(f"/api/external/sync/runs/{run_id}/resume")

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "SHOPIFY_SYNC_IN_PROGRESS", response.text
    # 被拒的 resume 没有发出上游请求、也没有建新 run。
    assert transport.calls.count("orders") == 1

    release.set()
    thread.join(30)
    assert not thread.is_alive(), "同步线程没有退出"
    assert box["response"].status_code == 200, box["response"].text


# ---------------------------------------------------------------------------
# R4-6 / R4-6b 游标列的两种极端态
# ---------------------------------------------------------------------------


def test_r4_6_null_or_corrupted_cursor_is_rejected(harness, monkeypatch):
    """R4-6：源 run 没有可用游标 → 409，reason=NO_CURSOR。

    口径说明（与任务文件 §4 的字面写法有出入，如实记录）：
    `cursor_json` 列在迁移 `0019_external_sync_run_cursor`（`nullable=False`）与
    `app/db.py` 的模型（`Mapped[str]` → nullable=False）里都是 **NOT NULL**，
    因此"手工 UPDATE 置 NULL"会被数据库拒绝（IntegrityError），该字面形态**不可达**。
    本用例改用 `resume_plan` 同一分支（reason=NO_CURSOR）的**可达输入**：
    损坏的非 JSON 串与空串；并顺带把"NULL 形态不可达"这一事实钉住。
    """
    transport = _install_fake_transport(
        monkeypatch, ResumeFakeTransport(total_pages=2)
    )
    run_id = _seed_source_run(harness)

    def _set_cursor(value):
        with harness.db_mod.SessionLocal() as db:
            db.query(harness.db_mod.ExternalSyncRun).filter(
                harness.db_mod.ExternalSyncRun.run_id == run_id
            ).update({"cursor_json": value}, synchronize_session=False)
            db.commit()

    # ① 损坏的非 JSON 串 → NO_CURSOR
    _set_cursor("not-a-json-cursor")
    response = harness.client.post(f"/api/external/sync/runs/{run_id}/resume")
    _assert_reason(response, "NO_CURSOR")

    # ② 空串 → NO_CURSOR
    _set_cursor("")
    response = harness.client.post(f"/api/external/sync/runs/{run_id}/resume")
    _assert_reason(response, "NO_CURSOR")

    # ③ 字面 NULL 形态不可达：列是 NOT NULL，数据库直接拒绝。
    with pytest.raises(Exception) as excinfo:
        _set_cursor(None)
    assert "NOT NULL" in str(excinfo.value), str(excinfo.value)

    assert transport.calls == []


def test_r4_6b_empty_cursor_restarts_full_sync(harness, monkeypatch):
    """R4-6b：源 run 的 cursor_json == "{}"（未开始分页）→ 不是 409，全量重拉。"""
    transport = _install_fake_transport(
        monkeypatch, ResumeFakeTransport(total_pages=2)
    )
    run_id = _seed_source_run(harness, cursor_json="{}")

    response = harness.client.post(f"/api/external/sync/runs/{run_id}/resume")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded", body
    assert body["resumed_from"] == run_id
    # RESTART：从第 1 页开始，全量重拉（2 页都取）。
    assert transport.calls.count("orders") == 2

    detail = _run_detail(harness.db_mod, body["run_id"])
    assert detail["attempt"] == 2
    assert detail["retry_of_run_id"] == run_id
    assert detail["cursor_json"]["resources"]["orders"]["pages"] == 2


# ---------------------------------------------------------------------------
# R4-7 / R4-8 游标 schema 不可识别
# ---------------------------------------------------------------------------


def test_r4_7_unsupported_schema_version_is_rejected(harness, monkeypatch):
    """R4-7：cursor_json.schema_version=99 → 409，reason=UNSUPPORTED_CURSOR_SCHEMA。"""
    transport = _install_fake_transport(
        monkeypatch, ResumeFakeTransport(total_pages=2)
    )
    cursor = {
        "schema_version": 99,
        "resources": {"orders": {"cursor": "orders-0", "pages": 1, "fetched": 1, "complete": False}},
    }
    run_id = _seed_source_run(harness, cursor_json=json.dumps(cursor))

    response = harness.client.post(f"/api/external/sync/runs/{run_id}/resume")

    _assert_reason(response, "UNSUPPORTED_CURSOR_SCHEMA")
    assert transport.calls == []


def test_r4_8_missing_schema_version_is_rejected(harness, monkeypatch):
    """R4-8：cursor_json 缺 schema_version 键 → 409，reason=UNSUPPORTED_CURSOR_SCHEMA。"""
    transport = _install_fake_transport(
        monkeypatch, ResumeFakeTransport(total_pages=2)
    )
    cursor = {
        "resources": {"orders": {"cursor": "orders-0", "pages": 1, "fetched": 1, "complete": False}}
    }
    run_id = _seed_source_run(harness, cursor_json=json.dumps(cursor))

    response = harness.client.post(f"/api/external/sync/runs/{run_id}/resume")

    _assert_reason(response, "UNSUPPORTED_CURSOR_SCHEMA")
    assert transport.calls == []


# ---------------------------------------------------------------------------
# R4-9 续跑途中取消：游标保留
# ---------------------------------------------------------------------------


def test_r4_9_cancel_during_resume_keeps_cursor(harness, monkeypatch):
    """R4-9：续跑途中取消 → 新 run failed + SHOPIFY_SYNC_CANCELLED；游标保留（可再续）。"""
    blocked = threading.Event()
    release = threading.Event()

    def page_hook(data_key: str, index: int) -> None:
        # 源 run 已取 1 页（游标 orders-0）→ 续跑的第 1 页是绝对页序 1。
        if data_key == "orders" and index == 1:
            blocked.set()
            release.wait(30)

    transport = _install_fake_transport(
        monkeypatch,
        ResumeFakeTransport(total_pages=3, page_hook=page_hook),
    )
    run_id = _seed_source_run(harness)

    box: dict[str, Any] = {}

    def worker() -> None:
        box["response"] = harness.client.post(f"/api/external/sync/runs/{run_id}/resume")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert blocked.wait(10), "续跑线程没有进入第一页"

    # 新 run 已落库（begin_sync_run 在拉取之前），取它的 run_id 打取消。
    new_run_id = _wait_for_run(
        harness.db_mod, lambda run: run.retry_of_run_id == run_id and run.status == "running"
    )
    assert new_run_id, "没有找到续跑产生的新 run"

    cancel = harness.client.post(f"/api/external/sync/runs/{new_run_id}/cancel")
    assert cancel.status_code == 202, cancel.text
    assert cancel.json()["run_id"] == new_run_id
    assert cancel.json()["cancel_requested"] is True

    release.set()
    thread.join(30)
    assert not thread.is_alive(), "续跑线程没有在取消后退出"

    response = box["response"]
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_id"] == new_run_id
    assert body["status"] == "failed", body
    assert body["error_code"] == "SHOPIFY_SYNC_CANCELLED", body

    detail = _run_detail(harness.db_mod, new_run_id)
    assert detail["status"] == "failed"
    assert detail["error_code"] == "SHOPIFY_SYNC_CANCELLED"
    assert detail["resource_status"]["orders"]["error_code"] == "SHOPIFY_SYNC_CANCELLED"
    # 游标保留：已取到的那一页已经落库（可以再续）。
    orders_cursor = detail["cursor_json"]["resources"]["orders"]
    assert orders_cursor["cursor"] == "orders-1"
    assert orders_cursor["pages"] == 2  # 源 1 页 + 本次 1 页
    assert transport.calls.count("orders") == 1


# ---------------------------------------------------------------------------
# R4-10 链式续跑
# ---------------------------------------------------------------------------


def test_r4_10_chained_resume_links_to_direct_predecessor(harness, monkeypatch):
    """R4-10：A 失败 → resume 得 B，B 也失败 → 再 resume B 得 C。

    ① C 的 retry_of_run_id = B（直接前驱），attempt = 3；
    ② C 的 pages 基数 = B 落库的累计值（严格递增，不回退不重置）；
    ③ C 的 resumed_from = B 的 run_id。
    """
    run_a = _seed_source_run(harness, attempt=1)

    # --- A → B：B 取到 1 页后在下一页失败 ---
    def fail_on_third_page(data_key: str, index: int) -> None:
        if data_key == "orders" and index == 2:
            raise RuntimeError("upstream boom")

    transport_b = _install_fake_transport(
        monkeypatch,
        ResumeFakeTransport(total_pages=3, page_hook=fail_on_third_page),
    )
    response_b = harness.client.post(f"/api/external/sync/runs/{run_a}/resume")
    assert response_b.status_code == 200, response_b.text
    body_b = response_b.json()
    run_b = body_b["run_id"]
    assert body_b["status"] == "failed", body_b
    assert transport_b.calls.count("orders") == 2

    detail_b = _run_detail(harness.db_mod, run_b)
    assert detail_b["attempt"] == 2
    assert detail_b["retry_of_run_id"] == run_a
    assert detail_b["resource_status"]["resumed_from"] == run_a
    # B 落库的累计值：源 1 页 + B 自己 1 页 = 2 页。
    assert detail_b["cursor_json"]["resources"]["orders"]["pages"] == 2
    assert detail_b["cursor_json"]["resources"]["orders"]["cursor"] == "orders-1"

    # --- B → C：再续跑 B ---
    transport_c = _install_fake_transport(
        monkeypatch, ResumeFakeTransport(total_pages=3)
    )
    response_c = harness.client.post(f"/api/external/sync/runs/{run_b}/resume")
    assert response_c.status_code == 200, response_c.text
    body_c = response_c.json()
    run_c = body_c["run_id"]

    detail_c = _run_detail(harness.db_mod, run_c)
    # ① 直接前驱是 B（不是最初的 A），attempt 单调 +1。
    assert detail_c["retry_of_run_id"] == run_b
    assert detail_c["retry_of_run_id"] != run_a
    assert detail_c["attempt"] == 3
    # ③ resumed_from 指向 B。
    assert body_c["resumed_from"] == run_b
    assert detail_c["resource_status"]["resumed_from"] == run_b
    # ② pages 以 B 的累计值为基数，严格递增。
    pages_c = detail_c["cursor_json"]["resources"]["orders"]["pages"]
    assert pages_c >= detail_b["cursor_json"]["resources"]["orders"]["pages"]
    assert pages_c > detail_b["cursor_json"]["resources"]["orders"]["pages"]
    assert pages_c == 3
    assert transport_c.calls.count("orders") == 1


# ---------------------------------------------------------------------------
# R4-11 闸门反方向：resume 在跑时 /sync 被拒
# ---------------------------------------------------------------------------


def test_r4_11_sync_rejected_while_resume_is_running(harness, monkeypatch):
    """R4-11：续跑在跑（卡页法）时发 `/sync` → 409 SHOPIFY_SYNC_IN_PROGRESS（闸门同一把、方向对称）。"""
    blocked = threading.Event()
    release = threading.Event()

    def page_hook(data_key: str, index: int) -> None:
        if data_key == "orders" and index == 1:
            blocked.set()
            release.wait(30)

    transport = _install_fake_transport(
        monkeypatch,
        ResumeFakeTransport(total_pages=3, page_hook=page_hook),
    )
    run_id = _seed_source_run(harness)

    box: dict[str, Any] = {}

    def worker() -> None:
        box["response"] = harness.client.post(f"/api/external/sync/runs/{run_id}/resume")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert blocked.wait(10), "续跑线程没有进入第一页"

    sync = harness.client.post("/api/external/shopify/sync?resources=orders")

    assert sync.status_code == 409, sync.text
    assert sync.json()["detail"]["code"] == "SHOPIFY_SYNC_IN_PROGRESS", sync.text

    release.set()
    thread.join(30)
    assert not thread.is_alive(), "续跑线程没有退出"
    assert box["response"].status_code == 200, box["response"].text


# ---------------------------------------------------------------------------
# R4-12 TOCTOU：抢锁后重读源 run
# ---------------------------------------------------------------------------


def test_r4_12_toctou_reread_rejects_concurrent_state_change(harness, monkeypatch):
    """R4-12：前置校验通过后、抢锁后重读之前，源 run 被并发改成终态。

    构造法：在单飞闸门的 `acquire()` 成功之后（= 前置校验段已过、TOCTOU 重读尚未发生）
    把源 run 的 status 改成 succeeded，并让请求会话的 identity map 失效，
    使重读真正看到并发改写后的行。

    期望：409 reason=NOT_RESUMABLE_STATE，且**不建新 run**（库里不留 running 孤儿）。
    """
    db_mod = harness.db_mod
    run_id = _seed_source_run(harness)

    real_session_local = db_mod.SessionLocal
    captured_sessions: list[Any] = []

    def _capturing_session_local(*args, **kwargs):
        session = real_session_local(*args, **kwargs)
        captured_sessions.append(session)
        return session

    monkeypatch.setattr(db_mod, "SessionLocal", _capturing_session_local)

    def _mutate_source_run() -> None:
        # 并发改写：源 run 进入终态（与 cancel 竞态同一形态）。
        with real_session_local() as db:
            db.query(db_mod.ExternalSyncRun).filter(
                db_mod.ExternalSyncRun.run_id == run_id
            ).update({"status": "succeeded"}, synchronize_session=False)
            db.commit()
        # 让请求会话丢弃缓存的源 run（否则重读会命中 identity map 的旧值）。
        for session in captured_sessions:
            session.expire_all()

    real_lock = harness.main._sync_gate_lock

    class _GateWithMutation:
        def acquire(self, blocking: bool = True) -> bool:
            acquired = real_lock.acquire(blocking=blocking)
            if acquired:
                _mutate_source_run()
            return acquired

        def release(self) -> None:
            real_lock.release()

    monkeypatch.setattr(harness.main, "_sync_gate_lock", _GateWithMutation())

    response = harness.client.post(f"/api/external/sync/runs/{run_id}/resume")

    _assert_reason(response, "NOT_RESUMABLE_STATE")
    # 不建新 run、不留 running 孤儿。
    with real_session_local() as db:
        runs = db.query(db_mod.ExternalSyncRun).all()
        assert [run.run_id for run in runs] == [run_id]


# ---------------------------------------------------------------------------
# R4-13 / R-31 去重兜底复核（双维度）
# ---------------------------------------------------------------------------


def _sql_rows(db_mod, sql: str) -> list[tuple]:
    """跑一条只读 SQL，返回行元组列表（去重断言直接看库，不看应用层统计）。"""
    from sqlalchemy import text

    with db_mod.SessionLocal() as db:
        return [tuple(row) for row in db.execute(text(sql)).all()]


def test_r4_13_reentry_duplicates_absorbed_after_resume(harness, monkeypatch):
    """R4-13（R-31 复核，双维度）：重入重复拉取只多翻页，不重复写数。

    场景构造：
    - 源 run 已失败，orders 游标 `orders-0`（第 1 页已取、未取完）、
      inventory 游标 `inventory-0`（同样未取完）——两个资源都是 complete=false。
    - 假 transport 的**每一页都返回同一份 fixture**（`ORDER_NODE` / `INVENTORY_NODE`），
      所以续跑第 2 页交付的订单/库存与第 1 页完全相同（同订单 id、同行 id、同 sku+仓位）
      → 人为制造"重入重复"。
    - 对**同一个源 run 连续 resume 两次**：第一次写入这批记录，第二次把同一批记录
      再交付一次（上游确实多翻了一页）。

    期望（数据层 payload_hash / idempotency 去重兜底成立）：
    - orders：按 external_order_no 与 external_line_id 分组，HAVING COUNT(*)>1 为空集；
      external_orders 总行数 == 1、external_order_lines 总行数 == 1；
    - inventory：按 external_sku+warehouse_ref_key 与 idempotency_key 分组，
      HAVING COUNT(*)>1 为空集；快照总行数 == 1；
    - 第二次 resume 的上游调用数确实增加（证明"多翻页"真的发生了），
      但插入数 == 0（重复被吸收，不是静默没跑）。
    """
    transport = _install_fake_transport(
        monkeypatch, ResumeFakeTransport(total_pages=2)
    )
    cursor = {
        "schema_version": 1,
        "resources": {
            "orders": {"cursor": "orders-0", "pages": 1, "fetched": 1, "complete": False},
            "inventory": {"cursor": "inventory-0", "pages": 1, "fetched": 1, "complete": False},
        },
    }
    statuses = {
        "schema_version": 1,
        "orders": {"status": "failed", "execution": "attempted"},
        "inventory": {"status": "failed", "execution": "attempted"},
    }
    run_id = _seed_source_run(
        harness, cursor_json=json.dumps(cursor), resource_status_json=json.dumps(statuses)
    )

    # --- 第一次 resume：把这批记录写进库 ---
    first = harness.client.post(f"/api/external/sync/runs/{run_id}/resume")
    assert first.status_code == 200, first.text
    body1 = first.json()
    assert body1["status"] == "succeeded", body1
    assert body1["resources"]["orders"]["inserted"] == 1, body1
    assert body1["resources"]["inventory"]["inserted"] == 1, body1
    calls_after_first = list(transport.calls)
    assert calls_after_first.count("orders") == 1
    assert calls_after_first.count("inventoryItems") == 1

    # --- 第二次 resume（重入）：同一批上游记录再交付一次 ---
    second = harness.client.post(f"/api/external/sync/runs/{run_id}/resume")
    assert second.status_code == 200, second.text
    body2 = second.json()
    assert body2["status"] == "succeeded", body2

    # "只多翻页"：上游确实又被调了一次（orders / inventory 各再一次）。
    assert transport.calls.count("orders") == calls_after_first.count("orders") + 1
    assert transport.calls.count("inventoryItems") == calls_after_first.count("inventoryItems") + 1
    # "不重复写数"：第二次没有任何新插入，重复被数据层去重吸收。
    assert body2["resources"]["orders"]["inserted"] == 0, body2
    assert body2["resources"]["inventory"]["inserted"] == 0, body2
    # 重复被**处理过**而不是被静默跳过：统计里能看到这批记录的去向。
    # orders：payload 完全相同 → 命中 payload_hash 去重（no_op）。
    orders_stats = body2["resources"]["orders"]
    assert orders_stats["no_op"] == 1, body2
    assert orders_stats["updated"] == 0 and orders_stats["conflict"] == 0, body2
    # inventory：幂等键稳定（shopify:<item>:<location>:<payload_hash>）但 payload_hash
    # 含 as_of 时间戳 → 命中幂等键去重并被记为 conflict，同样不写第二行。
    inventory_stats = body2["resources"]["inventory"]
    assert inventory_stats["conflict"] == 1, body2
    assert inventory_stats["no_op"] == 0, body2
    assert inventory_stats["snapshot_ids"] == [], body2

    db_mod = harness.db_mod

    # --- orders 维度：零重复 ---
    assert _sql_rows(
        db_mod,
        "SELECT external_order_no, COUNT(*) FROM external_orders "
        "GROUP BY external_order_no HAVING COUNT(*) > 1",
    ) == []
    assert _sql_rows(
        db_mod,
        "SELECT external_line_id, COUNT(*) FROM external_order_lines "
        "GROUP BY external_line_id HAVING COUNT(*) > 1",
    ) == []
    assert _sql_rows(db_mod, "SELECT COUNT(*) FROM external_orders")[0][0] == 1
    assert _sql_rows(db_mod, "SELECT COUNT(*) FROM external_order_lines")[0][0] == 1

    # --- inventory 维度：零重复（按 sku+仓位 与幂等键两个口径） ---
    assert _sql_rows(
        db_mod,
        "SELECT external_sku, warehouse_ref_key, COUNT(*) FROM external_inventory_snapshots "
        "GROUP BY external_sku, warehouse_ref_key HAVING COUNT(*) > 1",
    ) == []
    assert _sql_rows(
        db_mod,
        "SELECT idempotency_key, COUNT(*) FROM external_inventory_snapshots "
        "GROUP BY idempotency_key HAVING COUNT(*) > 1",
    ) == []
    assert _sql_rows(db_mod, "SELECT COUNT(*) FROM external_inventory_snapshots")[0][0] == 1
