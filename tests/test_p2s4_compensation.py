"""P2-4 补偿机制（死信隔离 + 人工重放）回归测试。

用例编号与 `dev_task.p2s4-compensation.md` §6 一一对应：

| 编号 | 场景 | 测试函数 |
|---|---|---|
| D-1 | migration 0022 up/down（逐列核对） | `test_d1_migration_0022_up_and_down_restores_0021_shape` |
| D-2 | failed run 自动登记（含重复 finalize 幂等） | `test_d2_failed_run_registers_one_dead_letter_row` |
| D-3 | succeeded 不登记 | `test_d3_succeeded_run_registers_no_dead_letter_row` |
| D-4 | 分类表锁死 + 死信行无凭据形态字符串 | `test_d4_retry_class_table_is_locked_and_no_credential_in_row` |
| D-5 | 退避计算（attempt=1/2/3 区间，manual 为 NULL） | `test_d5_backoff_window_is_computed_once_per_attempt` |
| D-6 | exhausted 拒绝重放 | `test_d6_exhausted_dead_letter_cannot_be_replayed` |
| D-7 | 退避闸未到期 → 409，到期后放行 | `test_d7_backoff_gate_blocks_replay_until_due` |
| D-7b | 并发闸（抢 `_sync_gate_lock`） | `test_d7b_concurrency_gate_blocks_replay_while_sync_running` |
| D-8 | replay 成功链（新 run attempt=源+1 / 死信 replayed） | `test_d8_replay_creates_new_run_and_marks_dead_letter_replayed` |
| D-9 | replay 幂等（同键 200 同一 run，不产生第二行） | `test_d9_replay_is_idempotent_on_same_key` |
| D-10 | ignore 归档 + 二次 ignore 409 | `test_d10_ignore_archives_and_second_ignore_is_rejected` |
| D-11 | manual 类拒绝重放 | `test_d11_manual_class_dead_letter_cannot_be_replayed` |
| D-12 | 质量警告不进死信（只读视图可见） | `test_d12_quality_failed_run_never_enters_dead_letter_table` |
| D-13 | 僵尸判定（只读可见，run 状态不变） | `test_d13_zombie_run_is_visible_readonly` |
| D-14 | 权限与隔离（跨 workspace 404 / 403 / 422） | `test_d14_permission_and_tenant_isolation` |

口径说明（读测试前先读）：

- **零 live**：Shopify live 通道由假 transport 顶替 `ShopifyLiveTransport`，
  不触碰任何真实 Shopify API；但走真实的 `/api/external/sync/dead-letters/*` 路由、
  真实的死信登记（`finalize_resource_run` 单点挂接）与真实的 R4 resume 执行体。
- **四道闸门顺序**：状态 → 分类 → 退避 → 并发，不得颠倒（任务书 §4 T4）。
- **幂等键命中检查**排在四闸之前：否则「同幂等键二次 → 200 返回同一新 run」
  与状态闸「非 open 一律 409」互斥（第一次重放后死信已是 replayed 终态）。
- 退避用**写入时一次算死**的 `next_replay_at` 断言区间；测试里用"把门槛前推"
  模拟时间流逝（不引入假时钟）。
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

import app.config as cfg


REPO_ROOT = Path(__file__).resolve().parents[1]

# 分类表里恰好 3 个 auto 码（任务书 §4 T3①，测试锁死）。
AUTO_CODES = {"SHOPIFY_TIMEOUT", "SHOPIFY_RATE_LIMITED", "SHOPIFY_UPSTREAM_ERROR"}
# 退避基数与区间（base=30s，jitter ∈ [0, 0.25]）。
BACKOFF_BASE_SECONDS = 30
BACKOFF_JITTER_RATIO = 0.25

# 源 run 的游标前置态（第 1 页取到一半失败 → 可续跑）。
DEFAULT_ORDERS_CURSOR = {
    "schema_version": 1,
    "resources": {
        "orders": {"cursor": "orders-0", "pages": 1, "fetched": 1, "complete": False},
    },
}


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


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


def _login(client: TestClient, login: str, password: str = "pw") -> TestClient:
    response = client.post("/login", data={"login": login, "password": password, "next": "/"}, follow_redirects=False)
    assert response.status_code in (302, 303), response.text
    csrf = client.cookies.get("dianshang_csrf")
    assert csrf, "缺少 CSRF cookie"
    client.headers.update({"X-CSRF-Token": csrf})
    return client


def _run_cli(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    """CLI 走真实子进程（与应用进程同库、不同进程）：验证真实命令行口径。"""
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-m", "app.cli", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(REPO_ROOT),
    )


# ---------------------------------------------------------------------------
# 假 transport（照抄 R4 口径：不发起任何真实网络请求）
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

_NODE_BY_KEY = {"orders": ORDER_NODE, "inventoryItems": ORDER_NODE, "products": ORDER_NODE}


class ResumeFakeTransport:
    """假 transport：按 `variables["after"]` 游标定位续跑起点（零真实网络请求）。"""

    def __init__(self, *, total_pages: int = 1):
        self.total_pages = total_pages
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
        after = (variables or {}).get("after")
        if after:
            try:
                index = int(str(after).rsplit("-", 1)[-1]) + 1
            except ValueError:
                index = 0
        else:
            index = 0
        self.calls.append(data_key)
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


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


@dataclass
class Harness:
    client: TestClient
    main: Any
    db_mod: Any
    es: Any
    dl: Any
    workspace_id: int
    other_workspace_id: int
    db_path: Path


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """构造 Shopify live 同步 + 死信补偿的测试环境（零真实网络请求）。"""
    db_path = tmp_path / "p2s4_compensation.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "p2s4-artifacts"))
    monkeypatch.setenv("IMPORTS_DIR", str(tmp_path / "p2s4-uploads"))
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("SHOPIFY_LIVE_ENABLED", "true")
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "probe.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "probe-token")
    monkeypatch.setenv("SHOPIFY_SYNC_BUDGET_SECONDS", "300")

    import app.background as bg_mod
    import app.db as db_mod
    import app.repository as repo_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(repo_mod)
    importlib.reload(bg_mod)
    # 死信模块 → 外部同步模块的加载顺序必须跟上 db（否则 ORM 类与 engine 错配）。
    import app.dead_letters as dl_mod
    import app.external_sync as es_mod

    importlib.reload(dl_mod)
    importlib.reload(es_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    bg_mod.reset_executor()

    import app.runtime_flags as runtime_flags

    runtime_flags.set_live_enabled(True)

    from app.employee_auth import hash_password

    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="p2s4", name="P2-4 补偿商家")
        other = db_mod.Workspace(tenant_key="p2s4-other", name="P2-4 别的商家")
        db.add_all([workspace, other])
        db.flush()
        admin = db_mod.UserAccount(login="admin", password_hash=hash_password("pw"), display_name="补偿管理员")
        operator = db_mod.UserAccount(login="ops", password_hash=hash_password("pw"), display_name="补偿运营")
        reader = db_mod.UserAccount(login="reader", password_hash=hash_password("pw"), display_name="补偿只读")
        other_admin = db_mod.UserAccount(login="other-admin", password_hash=hash_password("pw"), display_name="别家管理员")
        db.add_all([admin, operator, reader, other_admin])
        db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=admin.id, role="admin"))
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=operator.id, role="operations"))
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=reader.id, role="readonly"))
        db.add(db_mod.WorkspaceMembership(workspace_id=other.id, user_id=other_admin.id, role="admin"))
        db.commit()
        workspace_id = workspace.id
        other_workspace_id = other.id

    import app.main as main_mod

    importlib.reload(main_mod)

    client = TestClient(main_mod.app)
    _login(client, "admin")

    yield Harness(
        client=client,
        main=main_mod,
        db_mod=db_mod,
        es=es_mod,
        dl=dl_mod,
        workspace_id=workspace_id,
        other_workspace_id=other_workspace_id,
        db_path=db_path,
    )

    runtime_flags.set_live_enabled(True)
    bg_mod.reset_executor()
    cfg.get_settings.cache_clear()


def _install_fake_transport(monkeypatch, transport: ResumeFakeTransport) -> ResumeFakeTransport:
    """把 `ShopifyLiveTransport` 换成假 transport（零真实网络请求）。"""
    import app.platform_adapters.shopify as shopify_mod

    monkeypatch.setattr(shopify_mod, "ShopifyLiveTransport", transport)
    return transport


def _seed_failed_live_run(
    harness: Harness,
    *,
    error_code: str = "SHOPIFY_TIMEOUT",
    message: str = "上游暂时不可用",
    attempt: int = 1,
    resources: tuple[str, ...] = ("orders",),
    cursor_json: str | None = None,
    workspace_id: int | None = None,
    retryable: bool = True,
) -> str:
    """造一个"失败且已 finalize"的 live run（死信由真实 finalize 路径登记）。"""
    from app.platform_adapters.base import AdapterError

    es = harness.es
    db_mod = harness.db_mod
    with db_mod.SessionLocal() as db:
        run = es.begin_sync_run(
            db,
            workspace_id=workspace_id if workspace_id is not None else harness.workspace_id,
            platform="shopify",
            sync_type="shopify_live",
            account_ref="probe.myshopify.com",
            store_ref="probe.myshopify.com",
            source_mode="shopify_live",
            simulated=False,
            attempt=attempt,
        )
        es.initialize_resources(db, run, list(resources))
        for resource in resources:
            es.mark_resource_running(db, run, resource)
            es.mark_resource_failed(
                db, run, resource, AdapterError(error_code, message, retryable=retryable)
            )
        if cursor_json is not None:
            run.cursor_json = cursor_json
            db.commit()
        es.finalize_resource_run(
            db, run, error=AdapterError(error_code, message, retryable=retryable)
        )
        db.refresh(run)
        return run.run_id


def _seed_succeeded_run(harness: Harness, *, quality_status: str | None = None) -> str:
    """造一个成功的 run（可选带上质量结论快照）。"""
    es = harness.es
    db_mod = harness.db_mod
    with db_mod.SessionLocal() as db:
        run = es.begin_sync_run(
            db,
            workspace_id=harness.workspace_id,
            platform="shopify",
            sync_type="shopify_live",
            account_ref="probe.myshopify.com",
            store_ref="probe.myshopify.com",
            source_mode="shopify_live",
            simulated=False,
            attempt=1,
        )
        es.initialize_resources(db, run, ["orders"])
        es.mark_resource_running(db, run, "orders")
        es.mark_resource_succeeded(db, run, "orders", es.ImportStats(total=1, inserted=1))
        es.finalize_resource_run(db, run)
        if quality_status is not None:
            run.quality_status = quality_status
            db.commit()
        db.refresh(run)
        return run.run_id


def _seed_zombie_run(harness: Harness, *, stale_seconds: int = 1000) -> str:
    """造一个僵尸 run：running + 无 finished_at + 心跳过期（默认阈值 900s）。"""
    db_mod = harness.db_mod
    with db_mod.SessionLocal() as db:
        run = db_mod.ExternalSyncRun(
            workspace_id=harness.workspace_id,
            run_id=uuid4().hex,
            platform="shopify",
            sync_type="shopify_live",
            account_ref="probe.myshopify.com",
            store_ref="probe.myshopify.com",
            source_mode="shopify_live",
            simulated=False,
            status="running",
            attempt=1,
            resource_status_json=json.dumps({"schema_version": 1, "orders": {"status": "running"}}),
            retryable_resources_json="[]",
            cursor_json="{}",
            started_at=datetime.utcnow() - timedelta(seconds=stale_seconds + 60),
            heartbeat_at=datetime.utcnow() - timedelta(seconds=stale_seconds),
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run.run_id


def _dead_letter_row(harness: Harness, run_id: str) -> dict[str, Any] | None:
    """读一条死信行（跨表读，只读）。"""
    db_mod = harness.db_mod
    with db_mod.SessionLocal() as db:
        row = db.query(db_mod.ExternalSyncDeadLetter).filter(
            db_mod.ExternalSyncDeadLetter.run_id == run_id
        ).one_or_none()
        if row is None:
            return None
        return {
            "id": row.id,
            "workspace_id": row.workspace_id,
            "run_id": row.run_id,
            "platform": row.platform,
            "sync_type": row.sync_type,
            "source_mode": row.source_mode,
            "error_code": row.error_code,
            "error_message": row.error_message,
            "retry_class": row.retry_class,
            "attempt": row.attempt,
            "next_replay_at": row.next_replay_at,
            "exhausted": bool(row.exhausted),
            "quality_status": row.quality_status,
            "status": row.status,
            "replayed_run_id": row.replayed_run_id,
            "actor": row.actor,
            "request_id": row.request_id,
            "ignore_reason": row.ignore_reason,
        }


def _dead_letter_count(harness: Harness, run_id: str | None = None) -> int:
    db_mod = harness.db_mod
    with db_mod.SessionLocal() as db:
        query = db.query(db_mod.ExternalSyncDeadLetter)
        if run_id is not None:
            query = query.filter(db_mod.ExternalSyncDeadLetter.run_id == run_id)
        return query.count()


def _move_replay_deadline(harness: Harness, run_id: str, *, seconds_ago: int = 5) -> None:
    """把退避门槛前推（模拟时间流逝；不做假时钟，不改配置）。"""
    db_mod = harness.db_mod
    with db_mod.SessionLocal() as db:
        row = db.query(db_mod.ExternalSyncDeadLetter).filter(
            db_mod.ExternalSyncDeadLetter.run_id == run_id
        ).one()
        row.next_replay_at = datetime.utcnow() - timedelta(seconds=seconds_ago)
        db.commit()


def _run_detail(harness: Harness, run_id: str) -> dict[str, Any]:
    db_mod = harness.db_mod
    with db_mod.SessionLocal() as db:
        run = db.query(db_mod.ExternalSyncRun).filter(db_mod.ExternalSyncRun.run_id == run_id).one()
        return {
            "run_id": run.run_id,
            "status": run.status,
            "attempt": run.attempt,
            "retry_of_run_id": run.retry_of_run_id,
            "retry_idempotency_key": run.retry_idempotency_key,
            "error_code": run.error_code,
        }


def _replay(client: TestClient, run_id: str, *, key: str | None = "dl-test-1"):
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.post(
        f"/api/external/sync/dead-letters/{run_id}/replay",
        headers=headers,
    )


# ---------------------------------------------------------------------------
# D-1 migration 0022 up/down
# ---------------------------------------------------------------------------


def test_d1_migration_0022_up_and_down_restores_0021_shape(tmp_path):
    """D-1：up 建新表（含唯一约束 + 索引）；down 整表删除 = 还原 0021 形态。"""
    db_url = f"sqlite:///{tmp_path / 'd1.db'}"
    upgrade_0021 = _run_alembic(["upgrade", "0021_external_sync_quality"], database_url=db_url)
    assert upgrade_0021.returncode == 0, upgrade_0021.stdout + upgrade_0021.stderr

    eng = create_engine(db_url)
    tables_before = set(inspect(eng).get_table_names())
    assert "external_sync_dead_letters" not in tables_before
    runs_before = _table_snapshot(eng, "external_sync_runs")

    upgrade_head = _run_alembic(["upgrade", "head"], database_url=db_url)
    assert upgrade_head.returncode == 0, upgrade_head.stdout + upgrade_head.stderr
    tables_after = set(inspect(eng).get_table_names())
    assert "external_sync_dead_letters" in tables_after

    snapshot = _table_snapshot(eng, "external_sync_dead_letters")
    columns = {item["name"]: item for item in snapshot["columns"]}
    # 列集合与顺序 = 任务书 §4 T1 的 18 列 + 主键。
    assert list(columns) == [
        "id",
        "workspace_id",
        "run_id",
        "platform",
        "sync_type",
        "source_mode",
        "error_code",
        "error_message",
        "retry_class",
        "attempt",
        "next_replay_at",
        "exhausted",
        "quality_status",
        "status",
        "replayed_run_id",
        "actor",
        "request_id",
        "ignore_reason",
        "created_at",
        "updated_at",
    ]
    # 全列 nullable 或带默认（无 NOT NULL 无默认列）。
    for name, item in columns.items():
        if name == "id":
            continue
        assert item["nullable"] or item["default"] is not None, (name, item)
    # 唯一约束：(workspace_id, run_id)。
    assert ("uq_external_sync_dead_letter_workspace_run", ("workspace_id", "run_id")) in snapshot["uniques"]
    # 索引：workspace_id / run_id。
    index_columns = {item[1] for item in snapshot["indexes"]}
    assert ("workspace_id",) in index_columns
    assert ("run_id",) in index_columns

    downgrade = _run_alembic(["downgrade", "0021_external_sync_quality"], database_url=db_url)
    assert downgrade.returncode == 0, downgrade.stdout + downgrade.stderr
    tables_down = set(inspect(eng).get_table_names())
    assert "external_sync_dead_letters" not in tables_down
    assert tables_down == tables_before
    assert _table_snapshot(eng, "external_sync_runs") == runs_before

    # 再升一次仍可成功（幂等可重复执行）。
    again = _run_alembic(["upgrade", "head"], database_url=db_url)
    assert again.returncode == 0, again.stdout + again.stderr


# ---------------------------------------------------------------------------
# D-2 / D-3 登记触发与范围
# ---------------------------------------------------------------------------


def test_d2_failed_run_registers_one_dead_letter_row(harness):
    """D-2：failed run 自动登记；快照正确；重复 finalize 只有一行。"""
    run_id = _seed_failed_live_run(harness, error_code="SHOPIFY_TIMEOUT", attempt=1)
    row = _dead_letter_row(harness, run_id)
    assert row is not None, "失败 run 必须自动登记死信"
    assert row["error_code"] == "SHOPIFY_TIMEOUT"
    assert row["retry_class"] == "auto"
    assert row["attempt"] == 1
    assert row["status"] == "open"
    assert row["exhausted"] is False
    # 质量结论快照：失败 run 不跑质量 → NULL（上下文快照，不参与判定）。
    assert row["quality_status"] is None
    assert row["workspace_id"] == harness.workspace_id
    assert row["next_replay_at"] is not None

    # 重复 finalize（finalize 重入安全）：仍然只有一行。
    from app.platform_adapters.base import AdapterError

    with harness.db_mod.SessionLocal() as db:
        run = db.query(harness.db_mod.ExternalSyncRun).filter(
            harness.db_mod.ExternalSyncRun.run_id == run_id
        ).one()
        harness.es.finalize_resource_run(
            db, run, error=AdapterError("SHOPIFY_TIMEOUT", "上游暂时不可用", retryable=True)
        )
    assert _dead_letter_count(harness, run_id) == 1


def test_d3_succeeded_run_registers_no_dead_letter_row(harness):
    """D-3：成功的 run 不进死信表（含资源级全成功的 finalize）。"""
    run_id = _seed_succeeded_run(harness)
    assert _dead_letter_count(harness, run_id) == 0
    assert _dead_letter_count(harness) == 0


# ---------------------------------------------------------------------------
# D-4 分类表锁死 + 凭据红线
# ---------------------------------------------------------------------------


def test_d4_retry_class_table_is_locked_and_no_credential_in_row(harness):
    """D-4：auto 恰好 3 码；预算/取消/未知码 → manual；死信行无凭据形态字符串。"""
    table = harness.dl.RETRY_CLASS_BY_CODE
    auto_codes = {code for code, value in table.items() if value == "auto"}
    assert auto_codes == AUTO_CODES, auto_codes
    assert table["SHOPIFY_SYNC_BUDGET_EXCEEDED"] == "manual"
    assert table["SHOPIFY_SYNC_CANCELLED"] == "manual"
    # 未知码 → manual（缺数据不硬给结论）。
    assert harness.dl.classify_retry_class("MYSTERY_CODE_9000") == "manual"
    assert harness.dl.classify_retry_class(None) == "manual"
    assert harness.dl.classify_retry_class("SHOPIFY_LIVE_NOT_CONFIGURED") == "manual"
    assert harness.dl.classify_retry_class("PAYLOAD_HASH_CONFLICT") == "manual"

    # 预算耗尽 → manual 类死信（登记口径）。
    budget_run = _seed_failed_live_run(harness, error_code="SHOPIFY_SYNC_BUDGET_EXCEEDED")
    budget_row = _dead_letter_row(harness, budget_run)
    assert budget_row["retry_class"] == "manual"
    # manual 类 next_replay_at 恒 NULL（退避不参与 manual）。
    assert budget_row["next_replay_at"] is None

    # 凭据红线：错误信息里的 token 形态字符串不得进死信表。
    secret_run = _seed_failed_live_run(
        harness,
        error_code="SHOPIFY_TIMEOUT",
        message="upstream rejected token=shpat_0123456789abcdef",
    )
    secret_row = _dead_letter_row(harness, secret_run)
    assert secret_row is not None
    dumped = json.dumps(secret_row, ensure_ascii=False, default=str)
    assert "shpat_" not in dumped, dumped
    assert "probe-token" not in dumped, dumped


# ---------------------------------------------------------------------------
# D-5 退避计算
# ---------------------------------------------------------------------------


def test_d5_backoff_window_is_computed_once_per_attempt(harness):
    """D-5：next_replay_at ∈ [base×2^(n−1), base×2^(n−1)×1.25]；manual 类恒 NULL。"""
    for attempt in (1, 2, 3):
        run_id = _seed_failed_live_run(harness, error_code="SHOPIFY_TIMEOUT", attempt=attempt)
        row = _dead_letter_row(harness, run_id)
        assert row["retry_class"] == "auto"
        assert row["next_replay_at"] is not None
        with harness.db_mod.SessionLocal() as db:
            run = db.query(harness.db_mod.ExternalSyncRun).filter(
                harness.db_mod.ExternalSyncRun.run_id == run_id
            ).one()
            failed_at = run.finished_at
        base = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        delta = (row["next_replay_at"] - failed_at).total_seconds()
        assert base <= delta <= base * (1 + BACKOFF_JITTER_RATIO), (attempt, delta)

    # manual 类：退避字段恒 NULL。
    manual_run = _seed_failed_live_run(harness, error_code="SHOPIFY_SYNC_CANCELLED")
    assert _dead_letter_row(harness, manual_run)["next_replay_at"] is None


# ---------------------------------------------------------------------------
# D-6 / D-7 / D-7b 三道闸门
# ---------------------------------------------------------------------------


def test_d6_exhausted_dead_letter_cannot_be_replayed(harness):
    """D-6：attempt 达上限 → 409 DEAD_LETTER_EXHAUSTED，且 exhausted=True。"""
    run_id = _seed_failed_live_run(harness, error_code="SHOPIFY_TIMEOUT", attempt=3)
    row = _dead_letter_row(harness, run_id)
    assert row["exhausted"] is True
    _move_replay_deadline(harness, run_id)
    response = _replay(harness.client, run_id)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "DEAD_LETTER_EXHAUSTED", response.text


def test_d7_backoff_gate_blocks_replay_until_due(harness, monkeypatch):
    """D-7：退避未到 → 409 DEAD_LETTER_BACKOFF_NOT_REACHED；到期后放行。"""
    transport = _install_fake_transport(monkeypatch, ResumeFakeTransport(total_pages=1))
    run_id = _seed_failed_live_run(
        harness,
        error_code="SHOPIFY_TIMEOUT",
        cursor_json=json.dumps(DEFAULT_ORDERS_CURSOR),
    )
    response = _replay(harness.client, run_id)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "DEAD_LETTER_BACKOFF_NOT_REACHED", response.text
    # 闸门拦下时不得建新 run。
    assert _run_detail(harness, run_id)["status"] == "failed"

    # 到期后放行（把门槛前推模拟时间流逝）。
    _move_replay_deadline(harness, run_id)
    ok = _replay(harness.client, run_id)
    assert ok.status_code == 201, ok.text
    assert transport.calls == ["orders"]


def test_d7b_concurrency_gate_blocks_replay_while_sync_running(harness, monkeypatch):
    """D-7b：先占 `_sync_gate_lock` 再调 replay → 409 SHOPIFY_SYNC_IN_PROGRESS；释放后可重放。"""
    _install_fake_transport(monkeypatch, ResumeFakeTransport(total_pages=1))
    run_id = _seed_failed_live_run(
        harness,
        error_code="SHOPIFY_TIMEOUT",
        cursor_json=json.dumps(DEFAULT_ORDERS_CURSOR),
    )
    _move_replay_deadline(harness, run_id)
    lock = harness.main._sync_gate_lock
    assert lock.acquire(blocking=False), "测试自身应拿到单飞闸门"
    try:
        blocked = _replay(harness.client, run_id)
        assert blocked.status_code == 409, blocked.text
        detail = blocked.json()["detail"]
        assert detail["code"] == "SHOPIFY_SYNC_IN_PROGRESS", blocked.text
    finally:
        lock.release()
    ok = _replay(harness.client, run_id)
    assert ok.status_code == 201, ok.text


# ---------------------------------------------------------------------------
# D-8 / D-9 重放链与幂等
# ---------------------------------------------------------------------------


def test_d8_replay_creates_new_run_and_marks_dead_letter_replayed(harness, monkeypatch):
    """D-8：新 run attempt=源+1、retry_of_run_id=源；死信 status=replayed + replayed_run_id。"""
    _install_fake_transport(monkeypatch, ResumeFakeTransport(total_pages=1))
    run_id = _seed_failed_live_run(
        harness,
        error_code="SHOPIFY_TIMEOUT",
        cursor_json=json.dumps(DEFAULT_ORDERS_CURSOR),
    )
    _move_replay_deadline(harness, run_id)
    response = _replay(harness.client, run_id, key="dl-d8-1")
    assert response.status_code == 201, response.text
    payload = response.json()
    new_run_id = payload["run"]["run_id"]
    assert new_run_id != run_id

    new_run = _run_detail(harness, new_run_id)
    assert new_run["attempt"] == 2, new_run
    assert new_run["retry_of_run_id"] == run_id, new_run
    assert new_run["retry_idempotency_key"] == "dl-d8-1", new_run

    row = _dead_letter_row(harness, run_id)
    assert row["status"] == "replayed", row
    assert row["replayed_run_id"] == new_run_id, row
    # 源 run 不被改动（补偿是"再跑一次"，不是"改历史"）。
    assert _run_detail(harness, run_id)["status"] == "failed"


def test_d9_replay_is_idempotent_on_same_key(harness, monkeypatch):
    """D-9：同幂等键二次 → 200 返回同一新 run；不同键不再产生第二 run 行。"""
    _install_fake_transport(monkeypatch, ResumeFakeTransport(total_pages=1))
    run_id = _seed_failed_live_run(
        harness,
        error_code="SHOPIFY_TIMEOUT",
        cursor_json=json.dumps(DEFAULT_ORDERS_CURSOR),
    )
    _move_replay_deadline(harness, run_id)
    first = _replay(harness.client, run_id, key="dl-d9-1")
    assert first.status_code == 201, first.text
    first_run_id = first.json()["run"]["run_id"]

    second = _replay(harness.client, run_id, key="dl-d9-1")
    assert second.status_code == 200, second.text
    assert second.json()["run"]["run_id"] == first_run_id

    # 不同键：死信已是 replayed 终态 → 409，且不产生第二个重放 run 行。
    other = _replay(harness.client, run_id, key="dl-d9-2")
    assert other.status_code == 409, other.text
    assert other.json()["detail"]["code"] == "DEAD_LETTER_NOT_OPEN", other.text

    with harness.db_mod.SessionLocal() as db:
        replays = (
            db.query(harness.db_mod.ExternalSyncRun)
            .filter(harness.db_mod.ExternalSyncRun.retry_of_run_id == run_id)
            .count()
        )
    assert replays == 1, "同源 run 不得产生第二个重放 run 行"


# ---------------------------------------------------------------------------
# D-10 / D-11 归档与人工类
# ---------------------------------------------------------------------------


def test_d10_ignore_archives_and_second_ignore_is_rejected(harness):
    """D-10：ignore → status=ignored + reason 落库；二次 ignore → 409 DEAD_LETTER_NOT_OPEN。"""
    run_id = _seed_failed_live_run(harness, error_code="SHOPIFY_TIMEOUT")
    response = harness.client.post(
        f"/api/external/sync/dead-letters/{run_id}/ignore",
        json={"reason": "已人工排查并修复上游"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ignored"
    row = _dead_letter_row(harness, run_id)
    assert row["status"] == "ignored", row
    assert row["ignore_reason"] == "已人工排查并修复上游", row

    again = harness.client.post(
        f"/api/external/sync/dead-letters/{run_id}/ignore",
        json={"reason": "重复归档"},
    )
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["code"] == "DEAD_LETTER_NOT_OPEN", again.text


def test_d11_manual_class_dead_letter_cannot_be_replayed(harness):
    """D-11：CANCELLED 死信 → 409 DEAD_LETTER_MANUAL_ONLY（分类闸在退避闸之前）。"""
    run_id = _seed_failed_live_run(harness, error_code="SHOPIFY_SYNC_CANCELLED")
    row = _dead_letter_row(harness, run_id)
    assert row["retry_class"] == "manual"
    response = _replay(harness.client, run_id)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "DEAD_LETTER_MANUAL_ONLY", response.text


# ---------------------------------------------------------------------------
# D-12 / D-13 质量只读视图与僵尸只读判定
# ---------------------------------------------------------------------------


def test_d12_quality_failed_run_never_enters_dead_letter_table(harness):
    """D-12：quality failed 但 run succeeded → 死信零行；`--quality failed` 只读视图可见。"""
    run_id = _seed_succeeded_run(harness, quality_status="failed")
    assert _dead_letter_count(harness, run_id) == 0
    assert _dead_letter_count(harness) == 0

    result = _run_cli(harness.db_path, "dead-letter", "list", "--quality", "failed")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["mode"] == "quality_view"
    run_ids = [item["run_id"] for item in payload["items"]]
    assert run_id in run_ids, payload
    entry = next(item for item in payload["items"] if item["run_id"] == run_id)
    assert entry["replayable"] is False, "质量只读视图不可重放"


def test_d13_zombie_run_is_visible_readonly(harness):
    """D-13：running + 心跳过期 → `--zombie` 可见；run.status 仍是 running（不被改动）。"""
    run_id = _seed_zombie_run(harness)
    result = _run_cli(harness.db_path, "dead-letter", "list", "--zombie")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["mode"] == "zombie"
    run_ids = [item["run_id"] for item in payload["items"]]
    assert run_id in run_ids, payload
    entry = next(item for item in payload["items"] if item["run_id"] == run_id)
    assert entry["heartbeat_age_seconds"] >= 900, entry
    # 只读判定：run 状态不被改动。
    assert _run_detail(harness, run_id)["status"] == "running"


# ---------------------------------------------------------------------------
# D-14 权限与隔离
# ---------------------------------------------------------------------------


def test_d14_permission_and_tenant_isolation(harness):
    """D-14：跨 workspace → 404；无 catalog.write → 403；无 Idempotency-Key → 422。"""
    # 跨 workspace：别家的死信不可见、不可操作。
    other_run_id = _seed_failed_live_run(
        harness,
        error_code="SHOPIFY_TIMEOUT",
        workspace_id=harness.other_workspace_id,
    )
    listing = harness.client.get("/api/external/sync/dead-letters")
    assert listing.status_code == 200, listing.text
    visible = [item["run_id"] for item in listing.json()["items"]]
    assert other_run_id not in visible, listing.text

    cross = _replay(harness.client, other_run_id)
    assert cross.status_code == 404, cross.text
    assert cross.json()["detail"]["code"] == "DEAD_LETTER_NOT_FOUND", cross.text

    cross_ignore = harness.client.post(
        f"/api/external/sync/dead-letters/{other_run_id}/ignore",
        json={"reason": "越权尝试"},
    )
    assert cross_ignore.status_code == 404, cross_ignore.text

    # 只读角色：无 catalog.write → 403（replay 与 ignore 都是写操作）。
    reader = TestClient(harness.main.app)
    _login(reader, "reader")
    own_run_id = _seed_failed_live_run(harness, error_code="SHOPIFY_TIMEOUT")
    forbidden = _replay(reader, own_run_id)
    assert forbidden.status_code == 403, forbidden.text
    forbidden_ignore = reader.post(
        f"/api/external/sync/dead-letters/{own_run_id}/ignore",
        json={"reason": "只读角色不该能归档"},
    )
    assert forbidden_ignore.status_code == 403, forbidden_ignore.text

    # 缺 Idempotency-Key → 422（复用既有校验语义）。
    missing_key = harness.client.post(
        f"/api/external/sync/dead-letters/{own_run_id}/replay",
    )
    assert missing_key.status_code == 422, missing_key.text
    assert missing_key.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED", missing_key.text
