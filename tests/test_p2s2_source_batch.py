"""P2-2 来源批次生产化（归因四件套 + 授权引用实体化）回归测试。

用例编号与 `dev_task.p2s2-source-batch.md` §6 一一对应：

| 编号 | 场景 | 测试函数 |
|---|---|---|
| S-1 | migration 0020 up/down（逐列核对） | `test_s1_migration_0020_up_and_down_restores_0019_shape` |
| S-2 | 带 principal 触发 sync（fixture） | `test_s2_http_sync_records_actor_and_request_id` |
| S-3 | 无 principal 的系统路径（demo seed） | `test_s3_system_path_actor_is_seed_demo_constant` |
| S-4 | 多页同步（<50 页） | `test_s4_multi_page_sync_records_upstream_request_ids` |
| S-5 | credential ref 生命周期 | `test_s5_credential_ref_lifecycle_and_unique_name` |
| S-6 | **明文零落库** | `test_s6_plaintext_credential_never_persisted` |
| S-7 | live 触发但无匹配 ref | `test_s7_live_without_matching_ref_returns_409_without_run_row` |
| S-8 | live 触发且 ref 指纹匹配 | `test_s8_live_with_matching_ref_links_credential_ref_id` |
| S-9 | resume 归因 | `test_s9_resume_uses_current_actor_and_source_credential_ref` |
| S-10 | run 详情响应追加字段 | `test_s10_run_response_appends_attribution_fields_only` |
| S-11 | 既有测试零回归 | `test_s11_existing_live_path_tests_stay_green` |
| S-12 | 归因赋值点唯一 | `test_s12_attribution_assignment_point_is_unique_and_isolated` |
| S-13 | **非 HTTP 路径 request_id 降级** | `test_s13_background_request_id_prefix_is_unique` |
| S-14 | **上游 id 封顶截断** | `test_s14_upstream_ids_capped_at_50_keeping_latest` |

口径说明（读测试前先读）：

- **归因四件套**：actor / request_id / credential_ref_id / upstream_request_ids_json。
  HTTP 路径的 actor 取 `principal.subject`、request_id 复用中间件的 `X-Request-ID`；
  非 HTTP 路径（CLI / 后台）actor 走 `system:<来源>` 常量、request_id 走 `req-bg-<uuid12>`。
- **授权引用红线**：明文凭据任何形态都不入库——只存 env 键名 + sha256 前 8 位指纹。
  已登记的引用若与当前凭据指纹失配，live 路径按既有
  `409 SHOPIFY_LIVE_NOT_CONFIGURED` 语义拒绝（不新增错误码）。
- 同步走真实路由 `/api/external/shopify/sync` 与真实 `ShopifyReadOnlyAdapter._pages()`，
  只把 `ShopifyLiveTransport` 换成假 transport（不触网）。
"""

from __future__ import annotations

import hashlib
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

# 任务文件写死的常量：系统路径 actor。
SYSTEM_ACTOR_SEED_DEMO = "system:seed-demo"
# 任务文件写死的常量：非 HTTP 路径 request_id 前缀。
BACKGROUND_REQUEST_ID_PREFIX = "req-bg-"


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

    `request_ids` 记录每次上游调用的 request_id——适配器每页生成一个
    `shopify-<uuid12>`，P2-2 用它验证上游 id 的落库形态与封顶。
    """

    def __init__(self, *, pages: int = 1, page_hook=None, store_domain: str = "probe.myshopify.com"):
        self.pages = pages
        self.page_hook = page_hook
        self.store_domain = store_domain
        self.calls: list[str] = []
        self.request_ids: list[str] = []

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
        self.request_ids.append(request_id)
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


class ResumeFakeTransport(FakeShopifyTransport):
    """续跑专用假 transport：按 `after` 游标定位续跑起点。"""

    def __init__(self, *, total_pages: int = 1, store_domain: str = "probe.myshopify.com"):
        super().__init__(pages=total_pages, store_domain=store_domain)
        self.total_pages = total_pages

    def query(self, *, query: str, variables: dict, request_id: str) -> dict[str, Any]:
        if "inventoryItems(" in query:
            data_key = "inventoryItems"
        elif "orders(" in query:
            data_key = "orders"
        else:
            data_key = "products"
        after = (variables or {}).get("after")
        if after:
            try:
                index = int(str(after).rsplit("-", 1)[-1]) + 1
            except ValueError:
                index = 0
        else:
            index = 0
        self.calls.append(data_key)
        self.request_ids.append(request_id)
        has_next = (index + 1) < self.total_pages
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
    db_path: Path


def _login(client: TestClient, login: str, password: str = "pw") -> TestClient:
    response = client.post("/login", data={"login": login, "password": password, "next": "/"}, follow_redirects=False)
    assert response.status_code in (302, 303), response.text
    csrf = client.cookies.get("dianshang_csrf")
    assert csrf, "缺少 CSRF cookie"
    client.headers.update({"X-CSRF-Token": csrf})
    return client


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """构造一个已登录 admin 的外部同步测试环境（与 P2-0a / R6 用例同构）。"""
    db_path = tmp_path / "p2s2.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IMPORTS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEMO_MODE_ENABLED", "true")
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
        workspace = db_mod.Workspace(tenant_key="p2s2", name="P2-2 来源批次商家")
        db.add(workspace)
        db.flush()
        admin = db_mod.UserAccount(login="admin", password_hash=hash_password("pw"), display_name="批次管理员")
        db.add(admin)
        db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=admin.id, role="admin"))
        db.commit()
        workspace_id = workspace.id

    import app.main as main_mod

    importlib.reload(main_mod)

    client = TestClient(main_mod.app)
    _login(client, "admin")

    yield Harness(client=client, main=main_mod, db_mod=db_mod, workspace_id=workspace_id, db_path=db_path)

    runtime_flags.set_live_enabled(True)
    bg_mod.reset_executor()
    cfg_mod.get_settings.cache_clear()


def _install_fake_transport(monkeypatch, transport) -> Any:
    """把 `app.platform_adapters.shopify.ShopifyLiveTransport` 换成假 transport。"""
    import app.platform_adapters.shopify as shopify_mod

    monkeypatch.setattr(shopify_mod, "ShopifyLiveTransport", transport)
    return transport


def _run_row(db_mod, run_id: str) -> dict[str, Any]:
    with db_mod.SessionLocal() as db:
        run = db.query(db_mod.ExternalSyncRun).filter(db_mod.ExternalSyncRun.run_id == run_id).one()
        return {
            "run_id": run.run_id,
            "status": run.status,
            "actor": run.actor,
            "request_id": run.request_id,
            "credential_ref_id": run.credential_ref_id,
            "upstream_request_ids_json": run.upstream_request_ids_json,
            "source_mode": run.source_mode,
            "simulated": run.simulated,
            "attempt": run.attempt,
            "retry_of_run_id": run.retry_of_run_id,
        }


def _orders_payload(account: str = "a", store: str = "s") -> str:
    return json.dumps({"records": [{
        "account_ref": account,
        "store_ref": store,
        "external_order_no": "O-P2S2-1",
        "status": "paid",
        "created_at": "2026-09-01T10:00:00Z",
        "updated_at": "2026-09-01T10:01:00Z",
        "lines": [{"external_sku": "S-P2S2-1", "ordered_qty": 2, "gross_amount": "20.00"}],
    }]})


def _run_alembic(args: list[str], *, database_url: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(REPO_ROOT),
    )


def _table_snapshot(eng, table: str) -> dict:
    """逐列快照：列名/类型/可空性/默认值/列顺序 + 索引 + 唯一约束。"""
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


# ---------------------------------------------------------------------------
# S-1 migration 0020 up/down
# ---------------------------------------------------------------------------


def test_s1_migration_0020_up_and_down_restores_0019_shape(tmp_path):
    """S-1：up 加四列 + 建新表；down 后逐列断言与 0019 基线相等（R6-4 同款快照）。"""
    db_path = tmp_path / "p2s2_alembic.db"
    db_url = f"sqlite:///{db_path}"

    # 0019 基线。
    upgrade_0019 = _run_alembic(["upgrade", "0019_external_sync_run_cursor"], database_url=db_url)
    assert upgrade_0019.returncode == 0, upgrade_0019.stdout + upgrade_0019.stderr

    eng = create_engine(db_url)
    before = _table_snapshot(eng, "external_sync_runs")
    before_names = [item["name"] for item in before["columns"]]
    assert "external_credential_refs" not in set(inspect(eng).get_table_names())

    # 既有行：up 之后归因四列必须是 NULL（可空列，不回填、不改既有行语义）。
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO external_sync_runs "
            "(run_id, platform, sync_type, started_at, created_at, resource_status_json, retryable_resources_json) "
            "VALUES ('legacy-run-1', 'shopify', 'shopify_live', '2026-01-01 00:00:00', '2026-01-01 00:00:00', "
            "'{\"schema_version\": 1}', '[]')"
        ))

    # 被测对象钉死 **0020**（P2-3 新增 0021 之后 head 已推进；
    # 若继续用 `upgrade head`，本用例断言的"四列追加"会被后续切片的新列冲掉——
    # 与 R6-4 同款处置：把目标钉死，断言与 0020 语义零变化）。
    upgrade_0020 = _run_alembic(["upgrade", "0020_external_sync_attribution"], database_url=db_url)
    assert upgrade_0020.returncode == 0, upgrade_0020.stdout + upgrade_0020.stderr

    after_up = _table_snapshot(eng, "external_sync_runs")
    after_up_names = [item["name"] for item in after_up["columns"]]
    # 四个可空列追加在既有列之后，顺序固定。
    assert after_up_names == before_names + [
        "actor",
        "request_id",
        "credential_ref_id",
        "upstream_request_ids_json",
    ]
    new_columns = {item["name"]: item for item in after_up["columns"]}
    assert new_columns["actor"]["type"] == "VARCHAR(128)"
    assert new_columns["actor"]["nullable"] is True
    assert new_columns["request_id"]["type"] == "VARCHAR(64)"
    assert new_columns["request_id"]["nullable"] is True
    assert new_columns["credential_ref_id"]["type"] == "INTEGER"
    assert new_columns["credential_ref_id"]["nullable"] is True
    assert new_columns["upstream_request_ids_json"]["type"] == "TEXT"
    assert new_columns["upstream_request_ids_json"]["nullable"] is True
    # 其余列零变化。
    assert after_up["columns"][: len(before["columns"])] == before["columns"]
    assert after_up["indexes"] == before["indexes"]
    assert after_up["uniques"] == before["uniques"]

    # 新表在位，且无任何默认数据。
    assert "external_credential_refs" in set(inspect(eng).get_table_names())
    ref_snapshot = _table_snapshot(eng, "external_credential_refs")
    assert [item["name"] for item in ref_snapshot["columns"]] == [
        "id",
        "workspace_id",
        "platform",
        "ref_name",
        "env_key",
        "secret_fingerprint",
        "created_by",
        "created_at",
    ]
    with eng.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM external_credential_refs")).scalar() == 0

    # 既有行的归因四列全是 NULL。
    with eng.connect() as conn:
        row = conn.execute(text(
            "SELECT actor, request_id, credential_ref_id, upstream_request_ids_json "
            "FROM external_sync_runs WHERE run_id = 'legacy-run-1'"
        )).one()
    assert tuple(row) == (None, None, None, None)

    # down：两张表回到 0019 形态。
    downgrade = _run_alembic(["downgrade", "-1"], database_url=db_url)
    assert downgrade.returncode == 0, downgrade.stdout + downgrade.stderr

    after_down = _table_snapshot(eng, "external_sync_runs")
    assert after_down["columns"] == before["columns"]
    assert after_down["indexes"] == before["indexes"]
    assert after_down["uniques"] == before["uniques"]
    assert "external_credential_refs" not in set(inspect(eng).get_table_names())

    # 既有行的其余数据不因 down 丢失。
    with eng.connect() as conn:
        row = conn.execute(text(
            "SELECT run_id, platform, resource_status_json, retryable_resources_json "
            "FROM external_sync_runs WHERE run_id = 'legacy-run-1'"
        )).one()
    assert tuple(row) == ("legacy-run-1", "shopify", '{"schema_version": 1}', "[]")


# ---------------------------------------------------------------------------
# S-2 HTTP 路径归因
# ---------------------------------------------------------------------------


def test_s2_http_sync_records_actor_and_request_id(harness):
    """S-2：带 principal 的同步 → actor == principal.subject；request_id == 响应头 X-Request-ID。"""
    response = harness.client.post(
        "/api/external/sync/fixture",
        json={
            "platform": "jd",
            "account_ref": "a",
            "store_ref": "s",
            "source_mode": "json",
            "orders_content": _orders_payload(),
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    run = _run_row(harness.db_mod, body["sync_run_id"])
    # actor 取登录用户的 subject（登录名）。
    assert run["actor"] == "admin"
    # request_id 复用中间件的请求 id（与响应头逐字节一致）。
    assert run["request_id"] == response.headers["X-Request-ID"]
    assert run["request_id"].startswith("req-")
    # mock / fixture 路径没有授权概念，引用允许为空。
    assert run["credential_ref_id"] is None


# ---------------------------------------------------------------------------
# S-3 系统路径 actor 常量
# ---------------------------------------------------------------------------


def test_s3_system_path_actor_is_seed_demo_constant(harness, monkeypatch):
    """S-3：无 principal 的系统路径（demo seed）→ run.actor == `system:seed-demo`（字面值锁定）。"""
    import app.cli as cli_mod
    import app.demo_seed as demo_seed_mod

    # CLI 的 schema 自检在测试里没有意义（库表已由 create_all 建好）。
    monkeypatch.setattr(cli_mod, "_ensure_schema", lambda: None)
    # 重新加载 CLI 模块，让它绑定到本次测试的 SessionLocal（db 模块已被 reload 过）。
    importlib.reload(cli_mod)
    monkeypatch.setattr(cli_mod, "_ensure_schema", lambda: None)
    # app.db 被 reload 过之后，app.demo_seed 里绑定的 ORM 类是旧对象，
    # 与新 app.db 的同名表会拼成笛卡尔积（SQLite 报 ambiguous column name）。
    # 这里一并 reload，使演示种子与当前 ORM 模型一致（仅测试侧处理，不改产品代码）。
    importlib.reload(demo_seed_mod)

    assert cli_mod.cmd_seed_demo(["--workspace", "demo-p2s2"]) == 0

    with harness.db_mod.SessionLocal() as db:
        run = (
            db.query(harness.db_mod.ExternalSyncRun)
            .filter(harness.db_mod.ExternalSyncRun.sync_type == "demo_seed")
            .one()
        )
        assert run.actor == SYSTEM_ACTOR_SEED_DEMO
        assert run.actor == "system:seed-demo"
        assert run.request_id.startswith(BACKGROUND_REQUEST_ID_PREFIX)


# ---------------------------------------------------------------------------
# S-4 上游请求 id 落库形态
# ---------------------------------------------------------------------------


def test_s4_multi_page_sync_records_upstream_request_ids(harness, monkeypatch):
    """S-4：多页同步 → upstream_request_ids_json == {ids, truncated, total}，ids 长度 == 页数。"""
    transport = _install_fake_transport(monkeypatch, FakeShopifyTransport(pages=3))

    response = harness.client.post("/api/external/shopify/sync?resources=orders")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded", body
    assert transport.calls.count("orders") == 3

    run = _run_row(harness.db_mod, body["run_id"])
    payload = json.loads(run["upstream_request_ids_json"])
    assert set(payload) == {"ids", "truncated", "total"}
    assert payload["truncated"] is False
    assert payload["total"] == 3
    assert len(payload["ids"]) == 3
    assert all(item for item in payload["ids"])
    # 每页一个上游请求 id，形如 shopify-<uuid12>。
    assert payload["ids"] == transport.request_ids
    assert all(item.startswith("shopify-") for item in payload["ids"])


# ---------------------------------------------------------------------------
# S-5 授权引用生命周期
# ---------------------------------------------------------------------------


def test_s5_credential_ref_lifecycle_and_unique_name(harness, monkeypatch):
    """S-5：add → list → remove 全通过；ref_name 唯一；指纹 == sha256 前 8 位。"""
    import app.cli as cli_mod
    from app.credential_refs import (
        CredentialRefError,
        add_credential_ref,
        fingerprint_of,
        list_credential_refs,
        remove_credential_ref,
    )

    secret = "shpat_fixture_secret_value"
    expected = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]

    with harness.db_mod.SessionLocal() as db:
        ref = add_credential_ref(
            db,
            platform="shopify",
            ref_name="shopify-dev-store",
            env_key="SHOPIFY_ACCESS_TOKEN",
            secret=secret,
            workspace_id=harness.workspace_id,
            created_by="tester",
        )
        assert ref.secret_fingerprint == expected
        assert fingerprint_of(secret) == expected
        assert len(ref.secret_fingerprint) == 8

        # list 能看到这条引用。
        refs = list_credential_refs(db)
        assert [item.ref_name for item in refs] == ["shopify-dev-store"]
        assert refs[0].secret_fingerprint == expected

        # ref_name 唯一：同名重复登记被拒。
        with pytest.raises(CredentialRefError) as excinfo:
            add_credential_ref(
                db,
                platform="shopify",
                ref_name="shopify-dev-store",
                env_key="SHOPIFY_ACCESS_TOKEN",
                secret=secret,
            )
        assert excinfo.value.code == "CREDENTIAL_REF_NAME_EXISTS"

        # 数据库层的唯一约束同样生效（绕过应用层直接插入也会被拒）。
        import sqlalchemy.exc

        with pytest.raises(sqlalchemy.exc.IntegrityError):
            db.add(harness.db_mod.ExternalCredentialRef(
                platform="shopify",
                ref_name="shopify-dev-store",
                env_key="SHOPIFY_ACCESS_TOKEN",
                secret_fingerprint=expected,
            ))
            db.commit()
        db.rollback()

        # remove 是硬删：行没了，且再列不出来。
        removed = remove_credential_ref(db, ref_name="shopify-dev-store")
        assert removed is not None
        assert list_credential_refs(db) == []
        assert remove_credential_ref(db, ref_name="shopify-dev-store") is None

    # CLI 管理面：add 只收键名（指纹自动算）、list 可见、remove 可删。
    importlib.reload(cli_mod)
    monkeypatch.setattr(cli_mod, "_ensure_schema", lambda: None)
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", secret)

    import io
    import contextlib

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli_mod.cmd_credential_ref([
            "add", "--platform", "shopify", "--ref-name", "cli-store", "--env-key", "SHOPIFY_ACCESS_TOKEN",
        ])
    assert code == 0, buffer.getvalue()
    payload = json.loads(buffer.getvalue())
    assert payload["ok"] is True
    assert payload["secret_fingerprint"] == expected
    assert secret not in buffer.getvalue()

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli_mod.cmd_credential_ref(["list"])
    assert code == 0, buffer.getvalue()
    listed = json.loads(buffer.getvalue())
    assert [item["ref_name"] for item in listed["refs"]] == ["cli-store"]

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli_mod.cmd_credential_ref(["remove", "--ref-name", "cli-store"])
    assert code == 0, buffer.getvalue()
    assert json.loads(buffer.getvalue())["removed"] is True

    # 管理面不收明文：任何形如 --secret 的入参一律拒绝。
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli_mod.cmd_credential_ref([
            "add", "--platform", "shopify", "--ref-name", "bad", "--env-key", "SHOPIFY_ACCESS_TOKEN",
            "--secret", secret,
        ])
    assert code == 2
    assert json.loads(buffer.getvalue())["error_code"] == "CREDENTIAL_REF_PLAINTEXT_FORBIDDEN"


# ---------------------------------------------------------------------------
# S-6 明文零落库
# ---------------------------------------------------------------------------


def test_s6_plaintext_credential_never_persisted(harness):
    """S-6：三层 grep（引用表 / run 全行 / SQLite 文件字节）明文与全文 hash 零命中。"""
    from app.credential_refs import add_credential_ref

    secret = "shpat_LIVE_plaintext_9f3a_DO_NOT_STORE"
    full_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()

    with harness.db_mod.SessionLocal() as db:
        ref = add_credential_ref(
            db,
            platform="shopify",
            ref_name="shopify-dev-store",
            env_key="SHOPIFY_ACCESS_TOKEN",
            secret=secret,
            workspace_id=harness.workspace_id,
            created_by="tester",
        )
        ref_id = ref.id

        # run 行带上引用（模拟一次 live 运行的归因）。
        run = harness.db_mod.ExternalSyncRun(
            workspace_id=harness.workspace_id,
            run_id="p2s2-plaintext-probe",
            platform="shopify",
            sync_type="shopify_live",
            source_mode="shopify_live",
            simulated=False,
            status="succeeded",
            actor="admin",
            request_id="req-plaintext-probe",
            credential_ref_id=ref_id,
        )
        db.add(run)
        db.commit()

        # 第一层：引用表逐列。
        rows = db.execute(text(
            "SELECT id, workspace_id, platform, ref_name, env_key, secret_fingerprint, created_by "
            "FROM external_credential_refs"
        )).all()
        assert rows, "引用行不应为空"
        for row in rows:
            for value in row:
                text_value = str(value)
                assert secret not in text_value
                assert full_hash not in text_value
        # 指纹是 8 位截断，不是全文 hash。
        assert all(len(str(row[5])) == 8 for row in rows)

        # 第二层：run 表全行。
        run_rows = db.execute(text("SELECT * FROM external_sync_runs")).all()
        assert run_rows
        for row in run_rows:
            for value in row:
                text_value = str(value)
                assert secret not in text_value
                assert full_hash not in text_value

    # 第三层：SQLite 文件字节。
    raw = harness.db_path.read_bytes()
    assert secret.encode("utf-8") not in raw
    assert full_hash.encode("utf-8") not in raw


# ---------------------------------------------------------------------------
# S-7 live 无匹配 ref
# ---------------------------------------------------------------------------


def test_s7_live_without_matching_ref_returns_409_without_run_row(harness, monkeypatch):
    """S-7：登记的引用与当前凭据指纹失配 → 409 SHOPIFY_LIVE_NOT_CONFIGURED，且不写 run 行。"""
    from app.credential_refs import add_credential_ref

    with harness.db_mod.SessionLocal() as db:
        add_credential_ref(
            db,
            platform="shopify",
            ref_name="shopify-dev-store",
            env_key="SHOPIFY_ACCESS_TOKEN",
            secret="stale-key-value-not-the-current-one",
            workspace_id=harness.workspace_id,
            created_by="tester",
        )
        before = db.query(harness.db_mod.ExternalSyncRun).count()

    _install_fake_transport(monkeypatch, FakeShopifyTransport(pages=1))

    response = harness.client.post("/api/external/shopify/sync?resources=orders")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    # 错误码不新增：仍是既有语义。
    assert detail["code"] == "SHOPIFY_LIVE_NOT_CONFIGURED"

    with harness.db_mod.SessionLocal() as db:
        after = db.query(harness.db_mod.ExternalSyncRun).count()
    assert after == before, "前置失败不得留下 run 行"


# ---------------------------------------------------------------------------
# S-8 live 指纹匹配
# ---------------------------------------------------------------------------


def test_s8_live_with_matching_ref_links_credential_ref_id(harness, monkeypatch):
    """S-8：ref 指纹匹配 → run.credential_ref_id == ref id；source_mode / simulated 既有值不变。"""
    from app.credential_refs import add_credential_ref

    with harness.db_mod.SessionLocal() as db:
        ref = add_credential_ref(
            db,
            platform="shopify",
            ref_name="shopify-dev-store",
            env_key="SHOPIFY_ACCESS_TOKEN",
            secret="probe-token",  # 与 harness 里的 SHOPIFY_ACCESS_TOKEN 一致
            workspace_id=harness.workspace_id,
            created_by="tester",
        )
        ref_id = ref.id

    _install_fake_transport(monkeypatch, FakeShopifyTransport(pages=1))

    response = harness.client.post("/api/external/shopify/sync?resources=orders")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded", body

    run = _run_row(harness.db_mod, body["run_id"])
    assert run["credential_ref_id"] == ref_id
    # 既有取值零变化。
    assert run["source_mode"] == "shopify_live"
    assert run["simulated"] is False


# ---------------------------------------------------------------------------
# S-9 resume 归因
# ---------------------------------------------------------------------------


def test_s9_resume_uses_current_actor_and_source_credential_ref(harness, monkeypatch):
    """S-9：续跑的新 run → actor 为**当前**触发者、credential_ref_id 与源 run 同引用。"""
    from app.credential_refs import add_credential_ref

    with harness.db_mod.SessionLocal() as db:
        ref = add_credential_ref(
            db,
            platform="shopify",
            ref_name="shopify-dev-store",
            env_key="SHOPIFY_ACCESS_TOKEN",
            secret="probe-token",
            workspace_id=harness.workspace_id,
            created_by="tester",
        )
        ref_id = ref.id
        # 源 run：带归因、orders 失败、已取 1 页未取完（可续跑现场）。
        source = harness.db_mod.ExternalSyncRun(
            workspace_id=harness.workspace_id,
            run_id="p2s2-source-run",
            platform="shopify",
            sync_type="shopify_live",
            account_ref="probe.myshopify.com",
            store_ref="probe.myshopify.com",
            source_mode="shopify_live",
            simulated=False,
            status="failed",
            attempt=1,
            actor="someone-else",
            request_id="req-source-run",
            credential_ref_id=ref_id,
            resource_status_json=json.dumps(
                {"schema_version": 1, "orders": {"status": "failed", "execution": "attempted"}}
            ),
            retryable_resources_json="[]",
            cursor_json=json.dumps({
                "schema_version": 1,
                "resources": {"orders": {"cursor": "orders-0", "pages": 1, "fetched": 1, "complete": False}},
            }),
        )
        db.add(source)
        db.commit()
        source_run_id = source.run_id

    _install_fake_transport(monkeypatch, ResumeFakeTransport(total_pages=2))

    response = harness.client.post(f"/api/external/sync/runs/{source_run_id}/resume")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded", body

    run = _run_row(harness.db_mod, body["run_id"])
    # 链路语义不变。
    assert run["attempt"] == 2
    assert run["retry_of_run_id"] == source_run_id
    # actor 是**当前**触发者，不继承源 run。
    assert run["actor"] == "admin"
    assert run["request_id"] == response.headers["X-Request-ID"]
    # 引用与源 run 同一条。
    assert run["credential_ref_id"] == ref_id

    source_row = _run_row(harness.db_mod, source_run_id)
    assert source_row["actor"] == "someone-else"


# ---------------------------------------------------------------------------
# S-10 run 响应追加字段
# ---------------------------------------------------------------------------


def test_s10_run_response_appends_attribution_fields_only(harness, monkeypatch):
    """S-10：run 详情响应只新增归因三键（+ P2-3 的 quality_status），既有键值逐字节不变。"""
    _install_fake_transport(monkeypatch, FakeShopifyTransport(pages=2))

    response = harness.client.post("/api/external/shopify/sync?resources=orders")
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    sync_request_id = response.headers["X-Request-ID"]

    detail = harness.client.get(f"/api/external/sync/runs/{run_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()

    legacy_keys = {
        "run_id", "workspace_id", "platform", "account_ref", "store_ref", "sync_type",
        "source_mode", "simulated", "status", "attempt", "retry_of_run_id", "started_at",
        "finished_at", "heartbeat_at", "duration_seconds", "heartbeat_age_seconds", "total",
        "inserted", "updated", "no_op", "conflict", "stale", "error_code", "error_message",
        "resource_status", "retryable_resources",
    }
    # 只增不改：既有键一个不多一个不少，新增键为归因三键。
    # P2-3 必要连带：run 详情响应追加 `quality_status`（观察态质量结论），键集断言同步跟进。
    assert set(body) == legacy_keys | {"actor", "request_id", "upstream_request_count", "quality_status"}
    # 既有键值不变。
    assert body["run_id"] == run_id
    assert body["platform"] == "shopify"
    assert body["source_mode"] == "shopify_live"
    assert body["simulated"] is False
    assert body["status"] == "succeeded"
    assert body["attempt"] == 1
    assert body["retry_of_run_id"] is None
    assert body["error_code"] is None
    # 新增键的取值正确（run 的 request_id 是**那次同步**的请求 id）。
    assert body["actor"] == "admin"
    assert body["request_id"] == sync_request_id
    assert body["upstream_request_count"] == 2

    # 列表端点同样追加。
    listing = harness.client.get("/api/external/sync/runs")
    assert listing.status_code == 200, listing.text
    items = listing.json()
    assert items
    first = items[0]
    assert set(first) == legacy_keys | {"actor", "request_id", "upstream_request_count", "quality_status"}


# ---------------------------------------------------------------------------
# S-11 既有测试零回归
# ---------------------------------------------------------------------------


def test_s11_existing_live_path_tests_stay_green():
    """S-11：preview/sync/dry-run/retention/cursor/resume 相关既有测试零改动全绿。"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q",
            "tests/test_p2a_sync_guardrails.py",
            "tests/test_r4_resume.py",
            "tests/test_r6_cursor_persistence.py",
            "tests/test_p2b_retention.py",
            "-p", "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# S-12 归因赋值点唯一
# ---------------------------------------------------------------------------


def test_s12_attribution_assignment_point_is_unique_and_isolated(harness):
    """S-12：actor 只在单一位置赋值；两个不同用户并发跑 sync，actor/request_id 互不串。"""
    import app.db as db_mod
    from app.employee_auth import hash_password

    # 第二个操作者：并发时用来区分 actor。
    with db_mod.SessionLocal() as db:
        other = db_mod.UserAccount(login="operator", password_hash=hash_password("pw"), display_name="批次运营")
        db.add(other)
        db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=harness.workspace_id, user_id=other.id, role="admin"))
        db.commit()

    # 代码审查项：归因 actor 的赋值点唯一。
    # 注意：`actor=` 在 main.py 的**工单域**本就有既有用法（与本片无关），
    # 因此这里精确到"归因赋值点"：取 principal.subject 的那一处，
    # 且 run 的 actor 不允许在 begin_sync_run 之外被直接赋值。
    main_text = (REPO_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    sync_text = (REPO_ROOT / "app" / "external_sync.py").read_text(encoding="utf-8")
    main_actor_lines = [
        line for line in main_text.splitlines()
        # 归因赋值点：从主体取 subject 交给上下文；响应回读那一行取的是 run.actor，不含 subject。
        if "actor=getattr(" in line and "subject" in line
    ]
    sync_actor_lines = [line for line in sync_text.splitlines() if "actor=" in line]
    assert len(main_actor_lines) == 1, main_actor_lines
    assert len(sync_actor_lines) == 1, sync_actor_lines
    # 归因只经 begin_sync_run 落库：不得出现 run.actor = 这类直接赋值。
    for path in sorted((REPO_ROOT / "app").glob("*.py")):
        content = path.read_text(encoding="utf-8")
        assert "run.actor =" not in content, f"{path.name} 存在绕过 begin_sync_run 的 actor 赋值"

    other_client = TestClient(harness.main.app)
    _login(other_client, "operator")

    results: dict[str, Any] = {}

    def _do(client: TestClient, key: str, request_id: str) -> None:
        # SQLite 单写者：两个并发写可能撞上写锁，允许重试（最多 3 次）。
        # 归因断言只看"最终成功的那次"的落库结果，重试不影响归因语义。
        last: Any = None
        for _attempt in range(3):
            response = client.post(
                "/api/external/sync/fixture",
                json={
                    "platform": "jd",
                    "account_ref": "a",
                    "store_ref": "s",
                    "source_mode": "json",
                    "orders_content": _orders_payload(),
                },
                headers={"X-Request-ID": request_id},
            )
            last = response
            if response.status_code == 200:
                break
            time.sleep(0.2)
        results[key] = (last, request_id)

    threads = [
        threading.Thread(target=_do, args=(harness.client, "admin", "req-concurrent-admin")),
        threading.Thread(target=_do, args=(other_client, "operator", "req-concurrent-operator")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    admin_response, admin_request_id = results["admin"]
    operator_response, operator_request_id = results["operator"]
    assert admin_response.status_code == 200, admin_response.text
    assert operator_response.status_code == 200, operator_response.text

    admin_run = _run_row(harness.db_mod, admin_response.json()["sync_run_id"])
    operator_run = _run_row(harness.db_mod, operator_response.json()["sync_run_id"])
    # actor 互不串。
    assert admin_run["actor"] == "admin"
    assert operator_run["actor"] == "operator"
    # request_id 互不串：各自等于自己那次请求的头。
    assert admin_run["request_id"] == admin_request_id
    assert operator_run["request_id"] == operator_request_id
    assert admin_run["request_id"] != operator_run["request_id"]


# ---------------------------------------------------------------------------
# S-13 非 HTTP 路径 request_id 降级
# ---------------------------------------------------------------------------


def test_s13_background_request_id_prefix_is_unique(harness, monkeypatch):
    """S-13：非 HTTP 路径的 run → request_id 形如 `req-bg-<uuid12>` 且唯一；actor 为 `system:*`。"""
    import app.cli as cli_mod
    import app.demo_seed as demo_seed_mod
    from app.external_sync import background_request_id, system_actor

    assert background_request_id().startswith(BACKGROUND_REQUEST_ID_PREFIX)
    assert background_request_id() != background_request_id()
    assert system_actor("seed-demo") == SYSTEM_ACTOR_SEED_DEMO

    importlib.reload(cli_mod)
    monkeypatch.setattr(cli_mod, "_ensure_schema", lambda: None)
    # 与 S-3 同理：app.db 被 reload 后要同步 reload 演示种子模块（仅测试侧处理）。
    importlib.reload(demo_seed_mod)

    assert cli_mod.cmd_seed_demo(["--workspace", "demo-p2s2-a"]) == 0
    assert cli_mod.cmd_seed_demo(["--workspace", "demo-p2s2-b"]) == 0

    with harness.db_mod.SessionLocal() as db:
        runs = db.query(harness.db_mod.ExternalSyncRun).filter(
            harness.db_mod.ExternalSyncRun.sync_type == "demo_seed"
        ).all()
    assert len(runs) == 2
    request_ids = [item.request_id for item in runs]
    assert all(value.startswith(BACKGROUND_REQUEST_ID_PREFIX) for value in request_ids)
    # 后缀是 12 位 uuid 十六进制。
    assert all(len(value) == len(BACKGROUND_REQUEST_ID_PREFIX) + 12 for value in request_ids)
    assert len(set(request_ids)) == 2
    assert all(item.actor == SYSTEM_ACTOR_SEED_DEMO for item in runs)


# ---------------------------------------------------------------------------
# S-14 上游 id 封顶截断
# ---------------------------------------------------------------------------


def test_s14_upstream_ids_capped_at_50_keeping_latest(harness, monkeypatch):
    """S-14：51 页 → ids 只留最近 50 个、truncated=True、total=51（不丢最新页）。"""
    transport = _install_fake_transport(monkeypatch, FakeShopifyTransport(pages=51))

    response = harness.client.post("/api/external/shopify/sync?resources=orders")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded", body
    assert transport.calls.count("orders") == 51

    run = _run_row(harness.db_mod, body["run_id"])
    payload = json.loads(run["upstream_request_ids_json"])
    assert payload["truncated"] is True
    assert payload["total"] == 51
    assert len(payload["ids"]) == 50
    # 保留的是**最近 50 个**：首元素是第 2 页的 id，末元素是第 51 页的 id。
    assert payload["ids"][0] == transport.request_ids[1]
    assert payload["ids"][-1] == transport.request_ids[50]
