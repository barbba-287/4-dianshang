"""P2-3 数据质量门禁（六维校验 + 留存回放 + 观察态门禁）回归测试。

用例编号与 `dev_task.p2s3-quality.md` §5 一一对应：

| 编号 | 场景 | 测试函数 |
|---|---|---|
| Q-1 | migration 0021 up/down（逐列核对） | `test_q1_migration_0021_up_and_down_restores_0020_shape` |
| Q-2 | structural：response 含 `errors` 节点 | `test_q2_structural_errors_node_makes_run_failed` |
| Q-3 | integrity：混入 3 行脏行 | `test_q3_integrity_counts_three_dirty_rows` |
| Q-4 | uniqueness：同批幂等键重复 | `test_q4_uniqueness_counts_duplicate_keys` |
| Q-5 | consistency：同幂等键 payload_hash 与既有行不一致 | `test_q5_consistency_flags_payload_hash_conflict` |
| Q-6 | freshness：as_of 超阈（soft，不推高到 failed） | `test_q6_freshness_is_soft_finding_only` |
| Q-7 | truncated 留存件 → warn（不是 failed，也不是 unknown） | `test_q7_truncated_retention_is_warn_not_failed_not_unknown` |
| Q-8 | 三态落库（ok/warn/failed） | `test_q8_three_states_land_in_db` |
| Q-9 | 观察态门禁：quality failed 不拦截 | `test_q9_observe_only_gate_does_not_block_run` / `test_q9b_http_run_response_appends_quality_status_only` |
| Q-10 | 回放零网络（出站 HTTP 客户端打桩） | `test_q10_replay_is_offline_and_never_calls_outbound_http` |
| Q-11 | 留存缺失 → unknown（与 warn 严格区分） | `test_q11_missing_retention_is_unknown` |
| Q-12 | failed/cancelled run 不跑质量（两列 NULL） | `test_q12_failed_and_cancelled_runs_keep_quality_columns_null` |

口径说明（读测试前先读）：

- **六维**：`structural`/`integrity` 是 hard，`uniqueness`/`consistency`/`freshness`/`completeness`
  是 soft；hard 有发现 → `failed`，仅 soft → `warn`，全零 → `ok`。
- **unknown（根本没得校验）**：留存目录完全缺失 / meta 找不到 / 既无留存页又无入库行。
  unknown 时**六维不评估**（dims 全零），避免"status=unknown 却 findings>0"的自相矛盾读数。
- **truncated（跳过校验）**：留存文件存在且 `meta.truncated=true` → 该页跳过结构校验，
  记 `completeness` finding → `warn`。**truncated ≠ unknown，两者不得互相冒充**。
- **integrity 口径**：走 `app/connectors.py` 的 normalize 预检（质量层在外围计数，
  不改 normalize 抛错语义）；`app/connectors.py` 本轮零改动。
- **零 live**：本文件不构造任何真实域名 / 真实 transport；留存件全部程序化构造；
  回放路径用 `httpx.Client.send` 打桩（**不是** `socket.socket`）。
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text


REPO_ROOT = Path(__file__).resolve().parents[1]

# 六维 id（写死，与 app/source_quality.py 的 QUALITY_DIMENSIONS 一致）。
DIMENSIONS = ("structural", "integrity", "uniqueness", "consistency", "freshness", "completeness")

# 过时阈值：复用既有配置 SHOPIFY_SYNC_BUDGET_SECONDS（conftest 固定 300 秒）。
STALE_SECONDS = 300


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


# ---------------------------------------------------------------------------
# 留存件构造（程序化，不依赖任何本地演练证据）
# ---------------------------------------------------------------------------


def _orders_node(*, order_no: str = "1001", updated_at: str | None = None) -> dict:
    stamp = updated_at or datetime.now(timezone.utc).isoformat()
    return {
        "id": f"gid://shopify/Order/{order_no}",
        "name": f"#{order_no}",
        "createdAt": stamp,
        "updatedAt": stamp,
        "processedAt": stamp,
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


def _orders_page(*, error: bool = False) -> dict:
    """正常页 / 含 GraphQL errors 节点的页。"""
    if error:
        return {"errors": [{"message": "Throttled"}], "data": None}
    return {
        "data": {
            "orders": {
                "edges": [{"cursor": "orders-0", "node": _orders_node()}],
                "pageInfo": {"hasNextPage": False, "endCursor": "orders-0"},
            }
        }
    }


def _write_retention(
    artifacts_dir: Path,
    run_id: str,
    request_id: str,
    *,
    body: Any,
    truncated: bool = False,
    resource: str = "orders",
    captured_at: str | None = None,
) -> Path:
    """写一份留存件（正文 + meta 侧车），布局与 P2-0b 一致。"""
    directory = Path(artifacts_dir) / "shopify_sync" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if truncated:
        # 超限件：只写前一半字节，且用 .json.truncated 后缀（不会被误当成完整响应）。
        (directory / f"{request_id}.json.truncated").write_bytes(raw[: max(1, len(raw) // 2)])
    else:
        (directory / f"{request_id}.json").write_bytes(raw)
    meta = {
        "request_id": request_id,
        "attempt": 1,
        "http_status": 200,
        "elapsed_ms": 12,
        "query": "query Orders { orders { edges { node { id } } } }",
        "variables": {"first": 50},
        "captured_at": captured_at or datetime.now(timezone.utc).isoformat(),
        "truncated": truncated,
        "stored_bytes": len(raw),
        "original_size": len(raw),
        "resource": resource,
    }
    (directory / f"{request_id}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    return directory


# ---------------------------------------------------------------------------
# 库内夹具
# ---------------------------------------------------------------------------


@dataclass
class Env:
    db_mod: Any
    workspace_id: int
    artifacts_dir: Path
    db_path: Path


@dataclass
class HttpEnv:
    db_mod: Any
    workspace_id: int
    artifacts_dir: Path
    client: TestClient


def _resource_status(resources: tuple[str, ...], *, status: str = "succeeded") -> str:
    entry = {
        "status": status,
        "execution": "attempted",
        "total": 1,
        "inserted": 1,
        "updated": 0,
        "no_op": 0,
        "conflict": 0,
        "stale": 0,
        "snapshot_ids": [],
        "error_code": None,
        "error_message": None,
        "retryable": False,
    }
    payload: dict[str, Any] = {"schema_version": 1}
    for name in resources:
        payload[name] = dict(entry)
    return json.dumps(payload, ensure_ascii=False)


def _create_run(
    env: Env,
    *,
    run_id: str,
    resources: tuple[str, ...] = ("inventory",),
    resource_status: str = "succeeded",
    source_mode: str = "mock",
    account_ref: str = "acct-q",
) -> str:
    with env.db_mod.SessionLocal() as db:
        run = env.db_mod.ExternalSyncRun(
            workspace_id=env.workspace_id,
            run_id=run_id,
            platform="shopify",
            account_ref=account_ref,
            store_ref="default",
            sync_type="inventory",
            source_mode=source_mode,
            simulated=source_mode != "shopify_live",
            status="running",
            resource_status_json=_resource_status(resources, status=resource_status),
            retryable_resources_json="[]",
            cursor_json="{}",
        )
        db.add(run)
        db.commit()
    return run_id


def _add_snapshot(
    env: Env,
    *,
    run_id: str,
    key: str,
    sku: str = "SKU-1",
    available: int = 5,
    reserved: int = 0,
    inbound: int = 0,
    warehouse: str = "wh-1",
    account_ref: str = "acct-q",
    as_of: datetime | None = None,
    payload_hash: str = "sha256:aaa",
    sync_run_id: str | None = None,
    source_mode: str = "mock",
) -> None:
    now = datetime.utcnow()
    with env.db_mod.SessionLocal() as db:
        row = env.db_mod.ExternalInventorySnapshot(
            workspace_id=env.workspace_id,
            sync_run_id=sync_run_id or run_id,
            platform="shopify",
            account_ref=account_ref,
            store_ref="default",
            # *_ref_key 由写入路径按 ref 派生；直插时必须显式给出，
            # 否则会落到 "__default_warehouse__" 默认值上，把唯一约束口径搅乱。
            store_ref_key="default",
            warehouse_ref=warehouse,
            warehouse_ref_key=warehouse,
            external_sku=sku,
            available_qty=available,
            reserved_qty=reserved,
            inbound_qty=inbound,
            as_of=as_of or now,
            received_at=now,
            payload_hash=payload_hash,
            idempotency_key=key,
            source_mode=source_mode,
            simulated=True,
        )
        db.add(row)
        db.commit()


def _run_row(env: Env, run_id: str) -> dict[str, Any]:
    with env.db_mod.SessionLocal() as db:
        run = (
            db.query(env.db_mod.ExternalSyncRun)
            .filter(env.db_mod.ExternalSyncRun.run_id == run_id)
            .one()
        )
        return {
            "run_id": run.run_id,
            "status": run.status,
            "error_code": run.error_code,
            "error_message": run.error_message,
            "quality_status": run.quality_status,
            "quality_json": run.quality_json,
            "total": run.total,
            "inserted": run.inserted,
        }


def _finalize(env: Env, run_id: str):
    """走真实收口路径（finalize_resource_run）→ 返回质量报告。"""
    import app.external_sync as es_mod

    with env.db_mod.SessionLocal() as db:
        run = (
            db.query(env.db_mod.ExternalSyncRun)
            .filter(env.db_mod.ExternalSyncRun.run_id == run_id)
            .one()
        )
        es_mod.finalize_resource_run(db, run)
        return es_mod.quality_report_for_run(db, run)


def _replay(env: Env, run_id: str):
    """只读本地留存重算（零网络）。"""
    import app.source_quality as sq_mod

    return sq_mod.replay_quality(run_id)


def _reload_runtime(monkeypatch, tmp_path, *, artifacts_dir: Path) -> Any:
    """重绑 app.db / app.external_sync / app.source_quality 到本次测试的库与目录。

    重绑顺序有讲究：`app.db` 先重载（换 engine + 换 ORM 类），再重载依赖它的
    `app.external_sync`（否则它手里的 ORM 类是旧对象，与新库拼表会出错）。
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'p2s3.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(artifacts_dir))
    monkeypatch.setenv("IMPORTS_DIR", str(tmp_path / "uploads"))

    import app.config as cfg_mod

    cfg_mod.get_settings.cache_clear()

    import app.db as db_mod

    importlib.reload(db_mod)

    import app.external_sync as es_mod

    importlib.reload(es_mod)

    import app.source_quality as sq_mod

    importlib.reload(sq_mod)

    db_mod.Base.metadata.create_all(db_mod.engine)
    return db_mod


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    """隔离的库 + 留存目录（不依赖 HTTP）。"""
    artifacts_dir = tmp_path / "artifacts"
    db_mod = _reload_runtime(monkeypatch, tmp_path, artifacts_dir=artifacts_dir)
    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="p2s3", name="P2-3 质量商家")
        db.add(workspace)
        db.flush()
        db.commit()
        workspace_id = workspace.id
    return Env(
        db_mod=db_mod,
        workspace_id=workspace_id,
        artifacts_dir=artifacts_dir,
        db_path=tmp_path / "p2s3.db",
    )


@pytest.fixture
def http_env(tmp_path, monkeypatch) -> HttpEnv:
    """已登录 admin 的 HTTP 环境（同步走真实路由，零 live）。"""
    artifacts_dir = tmp_path / "artifacts"
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    monkeypatch.setenv("DEMO_MODE_ENABLED", "true")
    monkeypatch.setenv("SHOPIFY_LIVE_ENABLED", "false")

    import app.config as cfg_mod

    cfg_mod.get_settings.cache_clear()

    import app.db as db_mod

    importlib.reload(db_mod)

    import app.repository as repo_mod

    importlib.reload(repo_mod)

    import app.background as bg_mod

    importlib.reload(bg_mod)

    import app.external_sync as es_mod

    importlib.reload(es_mod)

    import app.source_quality as sq_mod

    importlib.reload(sq_mod)

    db_mod.Base.metadata.create_all(db_mod.engine)
    bg_mod.reset_executor()

    from app.employee_auth import hash_password

    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="p2s3-http", name="P2-3 质量商家")
        db.add(workspace)
        db.flush()
        admin = db_mod.UserAccount(login="admin", password_hash=hash_password("pw"), display_name="质量管理员")
        db.add(admin)
        db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=admin.id, role="admin"))
        db.commit()
        workspace_id = workspace.id

    import app.main as main_mod

    importlib.reload(main_mod)

    client = TestClient(main_mod.app)
    _login(client, "admin")

    yield HttpEnv(db_mod=db_mod, workspace_id=workspace_id, artifacts_dir=artifacts_dir, client=client)

    bg_mod.reset_executor()
    cfg_mod.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Q-1 migration 0021 up/down
# ---------------------------------------------------------------------------


def test_q1_migration_0021_up_and_down_restores_0020_shape(tmp_path):
    """Q-1：up 加两列（可空、末尾追加）；down 逐列还原到 0020 形态。"""
    db_path = tmp_path / "p2s3_alembic.db"
    db_url = f"sqlite:///{db_path}"

    upgrade_0020 = _run_alembic(["upgrade", "0020_external_sync_attribution"], database_url=db_url)
    assert upgrade_0020.returncode == 0, upgrade_0020.stdout + upgrade_0020.stderr

    eng = create_engine(db_url)
    before = _table_snapshot(eng, "external_sync_runs")
    before_names = [item["name"] for item in before["columns"]]

    # 既有行：up 之后两列必须是 NULL（可空列，不回填）。
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO external_sync_runs "
            "(run_id, platform, sync_type, started_at, created_at, resource_status_json, retryable_resources_json) "
            "VALUES ('legacy-run-1', 'shopify', 'shopify_live', '2026-01-01 00:00:00', '2026-01-01 00:00:00', "
            "'{\"schema_version\": 1}', '[]')"
        ))

    upgrade_head = _run_alembic(["upgrade", "head"], database_url=db_url)
    assert upgrade_head.returncode == 0, upgrade_head.stdout + upgrade_head.stderr

    after_up = _table_snapshot(eng, "external_sync_runs")
    after_names = [item["name"] for item in after_up["columns"]]
    # 两个可空列追加在既有列之后，顺序固定。
    assert after_names == before_names + ["quality_status", "quality_json"]
    new_columns = {item["name"]: item for item in after_up["columns"]}
    assert new_columns["quality_status"]["type"] == "VARCHAR(16)"
    assert new_columns["quality_status"]["nullable"] is True
    assert new_columns["quality_json"]["type"] == "TEXT"
    assert new_columns["quality_json"]["nullable"] is True
    # 其余列/索引/唯一约束零变化。
    assert after_up["columns"][: len(before["columns"])] == before["columns"]
    assert after_up["indexes"] == before["indexes"]
    assert after_up["uniques"] == before["uniques"]

    with eng.connect() as conn:
        row = conn.execute(text(
            "SELECT quality_status, quality_json FROM external_sync_runs WHERE run_id = 'legacy-run-1'"
        )).one()
    assert tuple(row) == (None, None)

    # P2-4 连带（连带许可：钉死版本号目标行，断言语义零变化）：
    # 仓库 head 已推进到 0022（死信表），`downgrade -1` 只回退一步（0022 → 0021），
    # 0021 的两列仍在——这里把目标**钉死**为 0020（回到 0020 形态），断言语义与 P2-3 一致。
    downgrade = _run_alembic(["downgrade", "0020_external_sync_attribution"], database_url=db_url)
    assert downgrade.returncode == 0, downgrade.stdout + downgrade.stderr

    after_down = _table_snapshot(eng, "external_sync_runs")
    assert after_down["columns"] == before["columns"]
    assert after_down["indexes"] == before["indexes"]
    assert after_down["uniques"] == before["uniques"]


# ---------------------------------------------------------------------------
# Q-2 structural
# ---------------------------------------------------------------------------


def test_q2_structural_errors_node_makes_run_failed(env):
    """Q-2：留存响应含 GraphQL errors 节点 → 该页 structural findings≥1 → run 级 failed。"""
    run_id = "q2-structural-run"
    _create_run(env, run_id=run_id, resources=("orders",), source_mode="shopify_live")
    _write_retention(env.artifacts_dir, run_id, "shopify-q2-1", body=_orders_page(error=True))

    report = _finalize(env, run_id)
    assert report.dims["structural"]["level"] == "hard"
    assert report.dims["structural"]["findings"] >= 1
    assert report.status == "failed"

    row = _run_row(env, run_id)
    assert row["quality_status"] == "failed"
    assert row["status"] == "succeeded"  # 观察态：不因质量结论改 run 状态
    payload = json.loads(row["quality_json"])
    assert payload["status"] == "failed"
    assert payload["dims"]["structural"]["findings"] >= 1


# ---------------------------------------------------------------------------
# Q-3 integrity
# ---------------------------------------------------------------------------


def test_q3_integrity_counts_three_dirty_rows(env):
    """Q-3：三行脏行（缺 sku / 负 available / 负 reserved）→ integrity findings==3。"""
    run_id = "q3-integrity-run"
    _create_run(env, run_id=run_id, resources=("inventory",))
    _add_snapshot(env, run_id=run_id, key="q3-key-1", sku="", available=5)
    _add_snapshot(env, run_id=run_id, key="q3-key-2", sku="SKU-2", available=-1)
    _add_snapshot(env, run_id=run_id, key="q3-key-3", sku="SKU-3", available=5, reserved=-5)

    report = _finalize(env, run_id)
    assert report.dims["integrity"]["level"] == "hard"
    assert report.dims["integrity"]["findings"] == 3
    assert len(report.dims["integrity"]["samples"]) <= 3
    assert report.status == "failed"

    row = _run_row(env, run_id)
    assert row["quality_status"] == "failed"
    payload = json.loads(row["quality_json"])
    assert payload["dims"]["integrity"]["findings"] == 3


# ---------------------------------------------------------------------------
# Q-4 uniqueness
# ---------------------------------------------------------------------------


def test_q4_uniqueness_counts_duplicate_keys(env):
    """Q-4：同批内同一幂等键出现两次 → uniqueness findings==1（soft → warn）。"""
    run_id = "q4-uniqueness-run"
    _create_run(env, run_id=run_id, resources=("inventory",))
    _add_snapshot(env, run_id=run_id, key="q4-key", warehouse="wh-1", payload_hash="sha256:same")
    _add_snapshot(env, run_id=run_id, key="q4-key", warehouse="wh-2", payload_hash="sha256:same")

    report = _finalize(env, run_id)
    assert report.dims["uniqueness"]["level"] == "soft"
    assert report.dims["uniqueness"]["findings"] == 1
    assert report.status == "warn"

    row = _run_row(env, run_id)
    assert row["quality_status"] == "warn"


# ---------------------------------------------------------------------------
# Q-5 consistency
# ---------------------------------------------------------------------------


def test_q5_consistency_flags_payload_hash_conflict(env):
    """Q-5：同幂等键与库中既有行 payload_hash 不一致 → consistency findings≥1。"""
    run_id = "q5-consistency-run"
    _create_run(env, run_id=run_id, resources=("inventory",))
    # 既有行（属于别的 run）：同幂等键、不同 payload_hash。
    _add_snapshot(
        env, run_id=run_id, key="q5-key", warehouse="wh-9",
        payload_hash="sha256:old", sync_run_id="q5-older-run",
    )
    # 本次 run 的行：同幂等键、不同 payload_hash → 冲突（R-31 conflict 的质量视角）。
    _add_snapshot(env, run_id=run_id, key="q5-key", warehouse="wh-1", payload_hash="sha256:new")

    report = _finalize(env, run_id)
    assert report.dims["consistency"]["level"] == "soft"
    assert report.dims["consistency"]["findings"] >= 1
    assert report.status == "warn"

    row = _run_row(env, run_id)
    assert row["quality_status"] == "warn"


# ---------------------------------------------------------------------------
# Q-6 freshness
# ---------------------------------------------------------------------------


def test_q6_freshness_is_soft_finding_only(env):
    """Q-6：as_of 超过阈值（复用 SHOPIFY_SYNC_BUDGET_SECONDS）→ soft finding，不推高到 failed。"""
    run_id = "q6-freshness-run"
    _create_run(env, run_id=run_id, resources=("inventory",))
    stale_as_of = datetime.utcnow() - timedelta(seconds=STALE_SECONDS + 300)
    _add_snapshot(env, run_id=run_id, key="q6-key", as_of=stale_as_of)

    report = _finalize(env, run_id)
    assert report.dims["freshness"]["level"] == "soft"
    assert report.dims["freshness"]["findings"] >= 1
    assert report.status != "failed"
    assert report.status == "warn"

    row = _run_row(env, run_id)
    assert row["quality_status"] == "warn"


# ---------------------------------------------------------------------------
# Q-7 truncated
# ---------------------------------------------------------------------------


def test_q7_truncated_retention_is_warn_not_failed_not_unknown(env):
    """Q-7：truncated 留存件 → 跳过该页校验 + completeness finding → warn（不是 failed/unknown）。"""
    run_id = "q7-truncated-run"
    _create_run(env, run_id=run_id, resources=("orders",), source_mode="shopify_live")
    _write_retention(env.artifacts_dir, run_id, "shopify-q7-1", body=_orders_page(), truncated=True)

    report = _finalize(env, run_id)
    # 该页跳过校验 → structural 无发现。
    assert report.dims["structural"]["findings"] == 0
    # 缺口记在 completeness。
    assert report.dims["completeness"]["findings"] >= 1
    # 状态是 warn：既不是 failed，也不是 unknown。
    assert report.status == "warn"
    assert report.status not in {"failed", "unknown"}

    row = _run_row(env, run_id)
    assert row["quality_status"] == "warn"

    # 回放同款口径：truncated → warn（非 failed、非 unknown、不抛异常）。
    replayed = _replay(env, run_id)
    assert replayed.status == "warn"
    assert replayed.dims["structural"]["findings"] == 0
    assert replayed.dims["completeness"]["findings"] >= 1


# ---------------------------------------------------------------------------
# Q-8 三态落库
# ---------------------------------------------------------------------------


def test_q8_three_states_land_in_db(env):
    """Q-8：ok / warn / failed 三态分别正确落 quality_status + quality_json（可反解析、dims 齐全）。"""
    ok_run = "q8-ok-run"
    _create_run(env, run_id=ok_run, resources=("inventory",))
    _add_snapshot(env, run_id=ok_run, key="q8-ok-key", as_of=datetime.utcnow())

    warn_run = "q8-warn-run"
    _create_run(env, run_id=warn_run, resources=("inventory",))
    _add_snapshot(
        env, run_id=warn_run, key="q8-warn-key",
        as_of=datetime.utcnow() - timedelta(seconds=STALE_SECONDS + 300),
    )

    failed_run = "q8-failed-run"
    _create_run(env, run_id=failed_run, resources=("inventory",))
    _add_snapshot(env, run_id=failed_run, key="q8-failed-key", sku="")

    for run_id, expected in ((ok_run, "ok"), (warn_run, "warn"), (failed_run, "failed")):
        report = _finalize(env, run_id)
        assert report.status == expected, (run_id, report.status)
        row = _run_row(env, run_id)
        assert row["quality_status"] == expected
        # 报告全文：四个顶层键 + 六维齐全 + 每维三字段。
        payload = json.loads(row["quality_json"])
        assert set(payload) == {"run_id", "status", "dims", "checked_at"}
        assert payload["run_id"] == run_id
        assert payload["status"] == expected
        assert set(payload["dims"]) == set(DIMENSIONS)
        for dim in payload["dims"].values():
            assert set(dim) == {"level", "findings", "samples"}
            assert isinstance(dim["findings"], int)
            assert isinstance(dim["samples"], list)
            assert len(dim["samples"]) <= 3


# ---------------------------------------------------------------------------
# Q-9 观察态门禁
# ---------------------------------------------------------------------------


def test_q9_observe_only_gate_does_not_block_run(env):
    """Q-9：quality failed 的 run 仍然 succeeded、无新 error_code、数据照常入库。"""
    run_id = "q9-observe-run"
    _create_run(env, run_id=run_id, resources=("inventory",))
    _add_snapshot(env, run_id=run_id, key="q9-key", sku="")  # 脏行

    report = _finalize(env, run_id)
    assert report.status == "failed"

    row = _run_row(env, run_id)
    # 拦截未发生：run 状态不变、没有新错误码。
    assert row["status"] == "succeeded"
    assert row["error_code"] is None
    assert row["error_message"] is None
    # 质量结论照常落库（观察态）。
    assert row["quality_status"] == "failed"
    # 数据照常入库（脏行仍在库里，没有被门禁拦下）。
    with env.db_mod.SessionLocal() as db:
        stored = (
            db.query(env.db_mod.ExternalInventorySnapshot)
            .filter(env.db_mod.ExternalInventorySnapshot.sync_run_id == run_id)
            .count()
        )
    assert stored == 1


def test_q9b_http_run_response_appends_quality_status_only(http_env):
    """Q-9（HTTP 面）：run 详情响应只新增 quality_status，既有键一个不多一个不少。"""
    payload = json.dumps({
        "records": [{
            "account_ref": "a",
            "store_ref": "s",
            "external_order_no": "O-P2S3-1",
            "status": "paid",
            "created_at": "2026-09-01T10:00:00Z",
            "updated_at": "2026-09-01T10:01:00Z",
            "lines": [{"external_sku": "S-P2S3-1", "ordered_qty": 2, "gross_amount": "20.00"}],
        }]
    })
    response = http_env.client.post(
        "/api/external/sync/fixture",
        json={
            "platform": "jd",
            "account_ref": "a",
            "store_ref": "s",
            "source_mode": "json",
            "orders_content": payload,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    run_id = body["sync_run_id"]

    detail = http_env.client.get(f"/api/external/sync/runs/{run_id}")
    assert detail.status_code == 200, detail.text
    run_body = detail.json()

    legacy_keys = {
        "run_id", "workspace_id", "platform", "account_ref", "store_ref", "sync_type",
        "source_mode", "simulated", "status", "attempt", "retry_of_run_id", "started_at",
        "finished_at", "heartbeat_at", "duration_seconds", "heartbeat_age_seconds", "total",
        "inserted", "updated", "no_op", "conflict", "stale", "error_code", "error_message",
        "resource_status", "retryable_resources",
    }
    # P2-2 的三个归因键 + P2-3 的一个质量键；其余键一个不多一个不少。
    assert set(run_body) == legacy_keys | {
        "actor", "request_id", "upstream_request_count", "quality_status",
    }
    # 既有键值不变。
    assert run_body["status"] == "succeeded"
    assert run_body["error_code"] is None
    # 新增键取值合法（fixture 路径同样跑了质量）。
    assert run_body["quality_status"] in {"ok", "warn", "failed", "unknown"}

    with http_env.db_mod.SessionLocal() as db:
        run = (
            db.query(http_env.db_mod.ExternalSyncRun)
            .filter(http_env.db_mod.ExternalSyncRun.run_id == run_id)
            .one()
        )
        assert run.quality_status == run_body["quality_status"]
        assert run.quality_json


# ---------------------------------------------------------------------------
# Q-10 回放零网络
# ---------------------------------------------------------------------------


def test_q10_replay_is_offline_and_never_calls_outbound_http(env, monkeypatch):
    """Q-10：回放只读本地留存；出站 HTTP 客户端（httpx.Client.send）打桩后仍成功且零命中。"""
    import httpx

    calls: list[Any] = []

    def _boom(self, request, **kwargs):  # noqa: ANN001 - 测试桩
        calls.append(request)
        raise AssertionError("回放路径不得发起任何出站 HTTP 请求")

    monkeypatch.setattr(httpx.Client, "send", _boom)

    run_id = "q10-replay-run"
    _create_run(env, run_id=run_id, resources=("orders",), source_mode="shopify_live")
    _write_retention(env.artifacts_dir, run_id, "shopify-q10-1", body=_orders_page())

    report = _replay(env, run_id)
    assert calls == [], "回放路径命中了出站 HTTP 客户端"
    assert report.status == "ok"
    assert set(report.dims) == set(DIMENSIONS)
    assert report.dims["structural"]["findings"] == 0
    assert report.dims["integrity"]["findings"] == 0

    # 模块自身不引入任何网络库（静态口径：不用 socket 打桩，也不用 urllib 打桩）。
    module_text = (REPO_ROOT / "app" / "source_quality.py").read_text(encoding="utf-8")
    for forbidden in ("import httpx", "import requests", "import socket", "urllib.request"):
        assert forbidden not in module_text, forbidden

    # CLI `quality replay <run_id>` 同样只读本地留存、零网络。
    cli_env = os.environ.copy()
    cli_env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-m", "app.cli", "quality", "replay", run_id],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=cli_env,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    cli_payload = json.loads(result.stdout)
    assert cli_payload["ok"] is True
    assert cli_payload["report"]["status"] == "ok"
    assert set(cli_payload["report"]) == {"run_id", "status", "dims", "checked_at"}


# ---------------------------------------------------------------------------
# Q-11 留存缺失 → unknown
# ---------------------------------------------------------------------------


def test_q11_missing_retention_is_unknown(env):
    """Q-11：留存目录不存在 / 空 / meta 缺失 → unknown（不是 warn），且不抛异常。"""
    # (a) 目录不存在。
    missing_run = "q11-missing-run"
    _create_run(env, run_id=missing_run, resources=("orders",), source_mode="shopify_live")
    report = _finalize(env, missing_run)
    assert report.status == "unknown"
    # unknown 时六维不评估（dims 全零），不与"全零→ok"混淆。
    assert all(dim["findings"] == 0 for dim in report.dims.values())

    row = _run_row(env, missing_run)
    # 落库形态：**字面 unknown**（不是 NULL——NULL 表示"本次运行没跑质量"）。
    assert row["quality_status"] == "unknown"
    assert row["quality_json"] is not None
    payload = json.loads(row["quality_json"])
    assert payload["status"] == "unknown"

    # (b) 目录存在但为空。
    empty_run = "q11-empty-run"
    _create_run(env, run_id=empty_run, resources=("orders",), source_mode="shopify_live")
    (env.artifacts_dir / "shopify_sync" / empty_run).mkdir(parents=True, exist_ok=True)
    assert _finalize(env, empty_run).status == "unknown"

    # (c) 只有正文、没有 meta 侧车 → 同样 unknown。
    nometa_run = "q11-nometa-run"
    _create_run(env, run_id=nometa_run, resources=("orders",), source_mode="shopify_live")
    directory = env.artifacts_dir / "shopify_sync" / nometa_run
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "shopify-orphan.json").write_text(
        json.dumps(_orders_page(), ensure_ascii=False), encoding="utf-8"
    )
    assert _finalize(env, nometa_run).status == "unknown"

    # 回放同样 unknown，不抛异常。
    assert _replay(env, missing_run).status == "unknown"


# ---------------------------------------------------------------------------
# Q-12 failed / cancelled run 不跑质量
# ---------------------------------------------------------------------------


def test_q12_failed_and_cancelled_runs_keep_quality_columns_null(env):
    """Q-12：failed / cancelled 的 run 两列保持 NULL（不跑质量，避免误报）。"""
    import app.external_sync as es_mod

    # failed：台账里资源失败 → 收口后 run 状态 failed → 质量层不介入。
    failed_run = "q12-failed-run"
    _create_run(env, run_id=failed_run, resources=("inventory",), resource_status="failed")
    _finalize(env, failed_run)
    row = _run_row(env, failed_run)
    assert row["status"] == "failed"
    assert row["quality_status"] is None
    assert row["quality_json"] is None

    # cancelled：状态非 succeeded → 门禁直接跳过（两列保持 NULL）。
    cancelled_run = "q12-cancelled-run"
    _create_run(env, run_id=cancelled_run, resources=("inventory",))
    with env.db_mod.SessionLocal() as db:
        run = (
            db.query(env.db_mod.ExternalSyncRun)
            .filter(env.db_mod.ExternalSyncRun.run_id == cancelled_run)
            .one()
        )
        run.status = "cancelled"
        db.commit()
        db.refresh(run)
        assert es_mod.apply_quality_gate(db, run) is None
    row = _run_row(env, cancelled_run)
    assert row["status"] == "cancelled"
    assert row["quality_status"] is None
    assert row["quality_json"] is None
