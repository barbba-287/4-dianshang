"""R6 同步游标持久化（cursor_json + Alembic migration）回归测试。

用例编号与 `dev_task.r6-cursor-persistence.md` §6 一一对应：

| 编号 | 场景 | 测试函数 |
|---|---|---|
| R6-1 | 正常 3 页同步（每页落库一次） | `test_r6_1_three_page_sync_persists_cursor_after_each_page` |
| R6-2 | 中途 fail（第 2 页抛错） | `test_r6_2_midrun_failure_keeps_last_persisted_cursor` |
| R6-3 | 中途取消（P2-0a/S4 语义） | `test_r6_3_cancel_keeps_last_persisted_cursor` |
| R6-4 | migration up/down（逐列核对） | `test_r6_4_migration_up_and_down_keeps_other_columns` |
| R6-5 | 双资源（orders+inventory） | `test_r6_5_two_resources_keep_independent_cursors` |
| R6-6 | cursor_json schema 形状 | `test_r6_6_cursor_json_shape_is_frozen` |
| R6-7 | `resume_plan` 全分支 | `test_r6_7_resume_plan_branches`（参数化，每行一个 id） |
| R6-8 | 新建 run 初始值 | `test_r6_8_new_run_cursor_json_is_empty_object` |
| R6-9 | 回退验证（A3 教训） | **非自动化**：交付前的回退实验，记录见 `.testtmp/delivery.r6.md` |

口径说明（读测试前先读）：

- 「取完」时持久化的 cursor 是 `null`（`complete=True`），依据是任务文件 §2.1
  「取完（hasNextPage=false）→ cursor=null 且 complete=true」与 R6-6
  「取完资源 cursor is None and complete is True」。因此末页之前的每一页持久化的是
  **该页的 endCursor**（即下一页的 `after` 值），末页持久化 null。
- 同步走真实路由 `/api/external/shopify/sync` + 真实 `ShopifyReadOnlyAdapter._pages()`，
  只把 `ShopifyLiveTransport` 换成假 transport（不触网）。
"""

import importlib
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text


REPO_ROOT = Path(__file__).resolve().parents[1]


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

    `pages` 为每个资源要返回的页数；末页 hasNextPage=false，但 endCursor 仍非空
    （真实 Shopify 也会给末页 endCursor）——用于验证"取完 → cursor=null"。
    """

    def __init__(self, *, pages: int = 1, page_hook=None, store_domain: str = "probe.myshopify.com"):
        self.pages = pages
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
            self.page_hook(data_key, index, variables)
        has_next = index < self.pages - 1
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
    workspace_id: int


def _login(client: TestClient, login: str, password: str = "pw") -> TestClient:
    response = client.post("/login", data={"login": login, "password": password, "next": "/"}, follow_redirects=False)
    assert response.status_code in (302, 303), response.text
    csrf = client.cookies.get("dianshang_csrf")
    assert csrf, "缺少 CSRF cookie"
    client.headers.update({"X-CSRF-Token": csrf})
    return client


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """构造一个已登录 admin 的 Shopify live 同步测试环境（与 P2-0a 用例同构）。

    不用 TestClient 的 context manager：lifespan 会拉起后台 worker 线程，
    与 R6-3 的并发取消场景争抢同一个 SQLite 库。
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'r6.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IMPORTS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("SHOPIFY_LIVE_ENABLED", "true")
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "probe.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "probe-token")
    monkeypatch.setenv("SHOPIFY_SYNC_BUDGET_SECONDS", "300")

    import app.background as bg_mod
    import app.config as cfg_mod
    import app.db as db_mod
    import app.repository as repo_mod

    cfg_mod.get_settings.cache_clear()
    importlib.reload(cfg_mod)
    importlib.reload(db_mod)
    importlib.reload(repo_mod)
    importlib.reload(bg_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    bg_mod.reset_executor()

    import app.runtime_flags as runtime_flags

    runtime_flags.set_live_enabled(True)

    from app.employee_auth import hash_password

    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="r6", name="R6 游标商家")
        db.add(workspace)
        db.flush()
        admin = db_mod.UserAccount(login="admin", password_hash=hash_password("pw"), display_name="游标管理员")
        db.add(admin)
        db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=admin.id, role="admin"))
        db.commit()
        workspace_id = workspace.id

    import app.main as main_mod

    importlib.reload(main_mod)

    client = TestClient(main_mod.app)
    _login(client, "admin")

    yield Harness(client=client, main=main_mod, db_mod=db_mod, workspace_id=workspace_id)

    runtime_flags.set_live_enabled(True)
    bg_mod.reset_executor()
    cfg_mod.get_settings.cache_clear()


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
            "cursor_json_raw": run.cursor_json,
            "cursor_json": json.loads(run.cursor_json or "{}"),
        }


# ---------------------------------------------------------------------------
# R6-1 正常 3 页同步
# ---------------------------------------------------------------------------


def test_r6_1_three_page_sync_persists_cursor_after_each_page(harness, monkeypatch):
    """R6-1：每页成功后落库一次，pages 逐页递增到 3，cursor 为该页 endCursor。"""
    observed: list[tuple[int, dict]] = []

    def page_hook(data_key: str, index: int, variables: dict) -> None:
        # 每次请求第 index 页时，前 index 页已经落库（用于观察"每页一次"的中间态）。
        if index == 0:
            return
        with harness.db_mod.SessionLocal() as db:
            run = (
                db.query(harness.db_mod.ExternalSyncRun)
                .order_by(harness.db_mod.ExternalSyncRun.id.desc())
                .first()
            )
            payload = json.loads(run.cursor_json or "{}")
        observed.append((index, payload))

    transport = _install_fake_transport(monkeypatch, FakeShopifyTransport(pages=3, page_hook=page_hook))

    response = harness.client.post("/api/external/shopify/sync?resources=orders")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded", body
    assert transport.calls.count("orders") == 3

    # 中间态：第 2 页被请求时，第 1 页已落库（pages=1）；第 3 页被请求时 pages=2。
    assert [index for index, _ in observed] == [1, 2]
    assert observed[0][1]["resources"]["orders"]["pages"] == 1
    assert observed[0][1]["resources"]["orders"]["cursor"] == "orders-0"
    assert observed[1][1]["resources"]["orders"]["pages"] == 2
    assert observed[1][1]["resources"]["orders"]["cursor"] == "orders-1"

    run = _run_row(harness.db_mod, body["run_id"])
    entry = run["cursor_json"]["resources"]["orders"]
    assert entry["pages"] == 3
    assert entry["fetched"] == 3
    # 末页 hasNextPage=false → 取完：cursor=null 且 complete=true（§2.1）。
    assert entry["cursor"] is None
    assert entry["complete"] is True
    assert run["cursor_json"]["updated_at"]


# ---------------------------------------------------------------------------
# R6-2 中途 fail
# ---------------------------------------------------------------------------


def test_r6_2_midrun_failure_keeps_last_persisted_cursor(harness, monkeypatch):
    """R6-2：第 2 页抛错时，已落库的第 1 页游标保留（不因失败清空）。"""
    def page_hook(data_key: str, index: int, variables: dict) -> None:
        if data_key == "orders" and index == 1:
            raise RuntimeError("上游第 2 页炸了")

    transport = _install_fake_transport(monkeypatch, FakeShopifyTransport(pages=3, page_hook=page_hook))

    response = harness.client.post("/api/external/shopify/sync?resources=orders")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed", body
    assert transport.calls.count("orders") == 2

    run = _run_row(harness.db_mod, body["run_id"])
    entry = run["cursor_json"]["resources"]["orders"]
    # 第 1 页的游标保留；第 2 页没成功，不进计数。
    assert entry["cursor"] == "orders-0"
    assert entry["pages"] == 1
    assert entry["fetched"] == 1
    assert entry["complete"] is False


# ---------------------------------------------------------------------------
# R6-3 中途取消
# ---------------------------------------------------------------------------


def test_r6_3_cancel_keeps_last_persisted_cursor(harness, monkeypatch):
    """R6-3：同步被取消时游标保留（取消也是可续跑的）。"""
    first_page = threading.Event()

    def page_hook(data_key: str, index: int, variables: dict) -> None:
        if data_key == "orders" and index == 0:
            first_page.set()
        # 每页留窗口，保证取消请求能在下一个分页检查点前送达。
        time.sleep(0.2)

    transport = _install_fake_transport(monkeypatch, FakeShopifyTransport(pages=50, page_hook=page_hook))

    box: dict[str, Any] = {}

    def worker() -> None:
        box["response"] = harness.client.post("/api/external/shopify/sync?resources=orders")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert first_page.wait(10), "同步线程没有进入第一页"

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
        if run_id is None:
            time.sleep(0.05)
    assert run_id, "没有找到运行中的 sync run"

    # 等到"至少有一页已落库"再发取消：避免取消早于第一页（那样就没有游标可保留）。
    pages_seen = 0
    deadline = time.monotonic() + 10
    while pages_seen < 1 and time.monotonic() < deadline:
        with harness.db_mod.SessionLocal() as db:
            run = (
                db.query(harness.db_mod.ExternalSyncRun)
                .filter(harness.db_mod.ExternalSyncRun.run_id == run_id)
                .one()
            )
            payload = json.loads(run.cursor_json or "{}")
        entry = (payload.get("resources") or {}).get("orders") or {}
        pages_seen = int(entry.get("pages") or 0)
        if pages_seen < 1:
            time.sleep(0.02)
    assert pages_seen >= 1, "取消前没有任何一页落库，无法验证游标保留"

    cancel = harness.client.post(f"/api/external/sync/runs/{run_id}/cancel")
    assert cancel.status_code == 202, cancel.text

    thread.join(30)
    assert not thread.is_alive(), "同步线程没有在取消后退出"

    response = box["response"]
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resources"]["orders"]["error_code"] == "SHOPIFY_SYNC_CANCELLED", body
    assert body["status"] == "failed", body
    # 取消在下一个分页检查点生效：不会把 50 页跑满。
    assert transport.calls.count("orders") <= 5

    run = _run_row(harness.db_mod, run_id)
    entry = run["cursor_json"]["resources"]["orders"]
    # 取消前取到的页的游标保留（不因取消清空），且与已取页数自洽。
    assert entry["pages"] >= 1
    assert entry["cursor"] == f"orders-{entry['pages'] - 1}"
    assert entry["complete"] is False


# ---------------------------------------------------------------------------
# R6-5 双资源
# ---------------------------------------------------------------------------


def test_r6_5_two_resources_keep_independent_cursors(harness, monkeypatch):
    """R6-5：orders 与 inventory 两个资源键互不覆盖。"""
    transport = _install_fake_transport(monkeypatch, FakeShopifyTransport(pages=2))

    response = harness.client.post("/api/external/shopify/sync?resources=orders,inventory")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded", body

    run = _run_row(harness.db_mod, body["run_id"])
    resources = run["cursor_json"]["resources"]
    assert set(resources) == {"orders", "inventory"}
    assert resources["orders"]["pages"] == 2
    assert resources["inventory"]["pages"] == 2
    assert resources["orders"]["complete"] is True
    assert resources["inventory"]["complete"] is True
    assert resources["orders"]["cursor"] is None
    assert resources["inventory"]["cursor"] is None
    assert transport.calls.count("orders") == 2
    assert transport.calls.count("inventoryItems") == 2


# ---------------------------------------------------------------------------
# R6-6 schema 形状
# ---------------------------------------------------------------------------


def test_r6_6_cursor_json_shape_is_frozen(harness, monkeypatch):
    """R6-6：顶层键恰为 schema_version/resources/updated_at；资源条目形状固定。"""
    _install_fake_transport(monkeypatch, FakeShopifyTransport(pages=2))

    response = harness.client.post("/api/external/shopify/sync?resources=orders,inventory")
    assert response.status_code == 200, response.text
    body = response.json()

    run = _run_row(harness.db_mod, body["run_id"])
    payload = run["cursor_json"]
    assert set(payload) == {"schema_version", "resources", "updated_at"}
    assert payload["schema_version"] == 1
    assert isinstance(payload["updated_at"], str) and payload["updated_at"]
    for name, entry in payload["resources"].items():
        assert set(entry) == {"cursor", "pages", "fetched", "complete"}, name
        assert isinstance(entry["pages"], int)
        assert isinstance(entry["fetched"], int)
        assert isinstance(entry["complete"], bool)
        # 取完的资源：cursor is None and complete is True
        assert entry["cursor"] is None
        assert entry["complete"] is True


# ---------------------------------------------------------------------------
# R6-8 新建 run 初始值
# ---------------------------------------------------------------------------


def test_r6_8_new_run_cursor_json_is_empty_object(harness):
    """R6-8：新建 run 的 cursor_json 读出来是字面 "{}"（不是 NULL、不是空壳 JSON）。"""
    from app.external_sync import begin_sync_run

    with harness.db_mod.SessionLocal() as db:
        run = begin_sync_run(
            db,
            workspace_id=harness.workspace_id,
            platform="shopify",
            sync_type="shopify_live",
            account_ref="probe.myshopify.com",
            store_ref="probe.myshopify.com",
            source_mode="shopify_live",
            simulated=False,
        )
        run_id = run.run_id

    with harness.db_mod.SessionLocal() as db:
        stored = (
            db.query(harness.db_mod.ExternalSyncRun)
            .filter(harness.db_mod.ExternalSyncRun.run_id == run_id)
            .one()
        )
        assert stored.cursor_json is not None
        assert stored.cursor_json == "{}"
        assert json.loads(stored.cursor_json) == {}


# ---------------------------------------------------------------------------
# R6-7 resume_plan 全分支
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case_id, cursor_json_str, targets, expected",
    [
        (
            "A_未开始_字面空对象_允许全量重拉",
            "{}",
            ["orders"],
            {"allowed": True, "reason": None, "full_restart": True, "action": "restart", "cursor": None},
        ),
        (
            "B_schema在但资源无条目_允许全量重拉",
            json.dumps({"schema_version": 1, "resources": {}}),
            ["orders"],
            {"allowed": True, "reason": None, "full_restart": True, "action": "restart", "cursor": None},
        ),
        (
            "B2_缺目标资源键_该资源从第1页开始",
            json.dumps({"schema_version": 1, "resources": {"inventory": {"cursor": "c9", "pages": 3, "fetched": 9, "complete": False}}}),
            ["orders"],
            {"allowed": True, "reason": None, "full_restart": True, "action": "restart", "cursor": None},
        ),
        (
            "C1_目标资源未取完_从游标继续",
            json.dumps({"schema_version": 1, "resources": {"orders": {"cursor": "cursor-1", "pages": 2, "fetched": 10, "complete": False}}}),
            ["orders"],
            {"allowed": True, "reason": None, "full_restart": False, "action": "resume", "cursor": "cursor-1"},
        ),
        (
            "C2_目标资源已取完_跳过",
            json.dumps({"schema_version": 1, "resources": {"orders": {"cursor": None, "pages": 3, "fetched": 150, "complete": True}}}),
            ["orders"],
            {"allowed": True, "reason": None, "full_restart": False, "action": "skip", "cursor": None},
        ),
        (
            "D_schema_version非1_拒绝",
            json.dumps({"schema_version": 99, "resources": {}}),
            ["orders"],
            {"allowed": False, "reason": "UNSUPPORTED_CURSOR_SCHEMA", "full_restart": False, "action": None, "cursor": None},
        ),
        (
            "E_缺schema_version键_拒绝",
            json.dumps({"resources": {}}),
            ["orders"],
            {"allowed": False, "reason": "UNSUPPORTED_CURSOR_SCHEMA", "full_restart": False, "action": None, "cursor": None},
        ),
        (
            "F_列值为NULL_拒绝",
            None,
            ["orders"],
            {"allowed": False, "reason": "NO_CURSOR", "full_restart": False, "action": None, "cursor": None},
        ),
        (
            "G_非JSON字符串_拒绝",
            "not-json",
            ["orders"],
            {"allowed": False, "reason": "NO_CURSOR", "full_restart": False, "action": None, "cursor": None},
        ),
    ],
)
def test_r6_7_resume_plan_branches(case_id, cursor_json_str, targets, expected):
    """R6-7：resume_plan 覆盖 §2.2 判定表的每一行。"""
    from app.external_sync import resume_plan

    plan = resume_plan(cursor_json_str, targets)

    assert plan.allowed is expected["allowed"], case_id
    assert plan.reason == expected["reason"], case_id
    assert plan.full_restart is expected["full_restart"], case_id
    assert plan.empty is expected["full_restart"], case_id
    if expected["allowed"]:
        assert plan.http_status is None, case_id
        entry = plan.resources[targets[0]]
        assert entry.action == expected["action"], case_id
        assert entry.cursor == expected["cursor"], case_id
    else:
        assert plan.http_status == 409, case_id


def test_r6_7_resume_plan_mixed_targets_decide_per_resource():
    """R6-7 附加：多目标资源时逐资源判定（完成的跳过、未完成的续跑）。"""
    from app.external_sync import resume_plan

    payload = json.dumps({
        "schema_version": 1,
        "resources": {
            "orders": {"cursor": "cursor-1", "pages": 2, "fetched": 10, "complete": False},
            "inventory": {"cursor": None, "pages": 1, "fetched": 28, "complete": True},
        },
        "updated_at": "2026-10-27T00:00:00+00:00",
    })

    plan = resume_plan(payload, ["orders", "inventory"])

    assert plan.allowed is True
    assert plan.full_restart is False
    assert plan.resources["orders"].action == "resume"
    assert plan.resources["orders"].cursor == "cursor-1"
    assert plan.resources["inventory"].action == "skip"


# ---------------------------------------------------------------------------
# R6-4 migration up/down（逐列核对）
# ---------------------------------------------------------------------------


def _run_alembic(args: list[str], *, database_url: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(REPO_ROOT),
    )


def _table_snapshot(eng, table: str = "external_sync_runs") -> dict:
    """逐列快照：列名/类型/可空性/默认值表达式/**列顺序** + 索引 + 唯一约束。"""
    insp = inspect(eng)
    columns = [
        {
            "name": item["name"],
            "type": str(item["type"]),
            "nullable": item["nullable"],
            "default": item["default"],
            "position": position,
        }
        for position, item in enumerate(insp.get_columns(table))
    ]
    indexes = sorted(
        (item["name"], tuple(item["column_names"]), bool(item["unique"]))
        for item in insp.get_indexes(table)
    )
    uniques = sorted(
        (item["name"], tuple(item["column_names"]))
        for item in insp.get_unique_constraints(table)
    )
    return {"columns": columns, "indexes": indexes, "uniques": uniques}


def test_r6_4_migration_up_and_down_keeps_other_columns(tmp_path):
    """R6-4：up 加列并回填既有行；down 只丢 cursor_json，其余逐列零变化。"""
    db_path = tmp_path / "r6_alembic.db"
    db_url = f"sqlite:///{db_path}"

    # 先停在 0018（R6 之前），记下基线表结构。
    upgrade_0018 = _run_alembic(["upgrade", "0018_repair_service_ticket_schema"], database_url=db_url)
    assert upgrade_0018.returncode == 0, upgrade_0018.stdout + upgrade_0018.stderr

    eng = create_engine(db_url)
    before = _table_snapshot(eng)
    assert "cursor_json" not in {item["name"] for item in before["columns"]}

    # 既有行（R6 之前写入）：up 之后 cursor_json 必须是字面 "{}"。
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO external_sync_runs "
            "(run_id, platform, sync_type, started_at, created_at, resource_status_json, retryable_resources_json) "
            "VALUES ('legacy-run-1', 'shopify', 'shopify_live', '2026-01-01 00:00:00', '2026-01-01 00:00:00', "
            "'{\"schema_version\": 1}', '[]')"
        ))

    # up 到 0019（R6 迁移本身）。
    # 说明（P2-2）：本测试针对 0019 迁移；仓库 head 已推进到 0020（归因四件套），
    # 这里由 "head" 改为显式指向 0019 revision——**断言与 R6 语义零变化**，
    # 只是把被测对象钉死在 0019，避免后续迁移改变列集合。
    upgrade_head = _run_alembic(["upgrade", "0019_external_sync_run_cursor"], database_url=db_url)
    assert upgrade_head.returncode == 0, upgrade_head.stdout + upgrade_head.stderr

    after_up = _table_snapshot(eng)
    cursor_columns = [item for item in after_up["columns"] if item["name"] == "cursor_json"]
    assert len(cursor_columns) == 1
    cursor_column = cursor_columns[0]
    assert cursor_column["type"] == "TEXT"
    assert cursor_column["nullable"] is False
    assert cursor_column["default"] in ("'{}'", '"{}"', "{}")

    with eng.connect() as conn:
        legacy_cursor = conn.execute(text(
            "SELECT cursor_json FROM external_sync_runs WHERE run_id = 'legacy-run-1'"
        )).scalar()
    assert legacy_cursor == "{}"

    # 其余列在 up 之后零变化（只多一列）。
    before_names = [item["name"] for item in before["columns"]]
    after_up_names = [item["name"] for item in after_up["columns"]]
    assert after_up_names == before_names + ["cursor_json"]
    assert after_up["indexes"] == before["indexes"]
    assert after_up["uniques"] == before["uniques"]

    # down：只丢 cursor_json（回到 0018；见上方说明）。
    downgrade = _run_alembic(["downgrade", "0018_repair_service_ticket_schema"], database_url=db_url)
    assert downgrade.returncode == 0, downgrade.stdout + downgrade.stderr

    after_down = _table_snapshot(eng)
    assert after_down["columns"] == before["columns"]
    assert after_down["indexes"] == before["indexes"]
    assert after_down["uniques"] == before["uniques"]

    # 既有行的其余数据不因 down 丢失（只丢 cursor 列本身）。
    with eng.connect() as conn:
        row = conn.execute(text(
            "SELECT run_id, platform, resource_status_json, retryable_resources_json "
            "FROM external_sync_runs WHERE run_id = 'legacy-run-1'"
        )).one()
    assert tuple(row) == ("legacy-run-1", "shopify", '{"schema_version": 1}', "[]")

    # down → up 可重复（列可再次加回，既有行仍回填 "{}"）。
    upgrade_again = _run_alembic(["upgrade", "0019_external_sync_run_cursor"], database_url=db_url)
    assert upgrade_again.returncode == 0, upgrade_again.stdout + upgrade_again.stderr
    with eng.connect() as conn:
        legacy_cursor = conn.execute(text(
            "SELECT cursor_json FROM external_sync_runs WHERE run_id = 'legacy-run-1'"
        )).scalar()
    assert legacy_cursor == "{}"
