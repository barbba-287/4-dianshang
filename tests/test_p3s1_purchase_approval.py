"""P3-S1 采购单审批回归测试（A-1 ~ A-20）。

用例编号与 `dev_task.p3s1-purchase-approval.md` §6 一一对应：

| 编号 | 场景 | 测试函数 |
|---|---|---|
| A-1 | migration 0023 up/down（8 列 + 唯一索引，逐列快照） | `test_a1_migration_0023_up_and_down_restores_0022_shape` |
| A-2 | approve 成功落库 + 审计行 | `test_a2_approve_success_persists_approver_and_action` |
| A-3 | approve 幂等（同键重放） | `test_a3_approve_is_idempotent_on_same_key` |
| A-4 | 幂等键复用冲突 | `test_a4_idempotency_key_reuse_returns_409` |
| A-5 | 版本冲突（同一单） | `test_a5_version_conflict_returns_409` |
| A-6 | 并发 CAS（两用户同 expected_version） | `test_a6_concurrent_cas_allows_exactly_one_winner` |
| A-7 | 自审红线（U3） | `test_a7_self_approval_is_forbidden_and_writes_nothing` |
| A-8 | 权限闸（U2 + Q1，角色收窄证明互不替代） | `test_a8_permission_gate_blocks_non_approvers` |
| A-9 | reject 理由必填 | `test_a9_reject_reason_is_required_and_writes_nothing` |
| A-10 | reject 成功 | `test_a10_reject_success_persists_reason_and_action` |
| A-11 | 状态机负向（四分支） | `test_a11_state_machine_rejects_invalid_transitions` |
| A-12 | cancel 成功（U5，零 DELETE） | `test_a12_cancel_marks_cancelled_without_deleting_row` |
| A-13 | cancel 归属 + 已生成入库单 | `test_a13_cancel_enforces_ownership_and_inbound_guard` |
| A-14 | 生成入库单成功 + 库存零副作用 | `test_a14_generate_inbound_order_has_zero_inventory_side_effect` |
| A-15 | 生成幂等 + 唯一冲突直接构造 | `test_a15_generate_inbound_order_is_idempotent_on_unique_conflict` |
| A-16 | 生成前置与越权 | `test_a16_generate_inbound_order_preconditions_and_authorization` |
| A-17 | 响应契约（16 旧键 + 7 新键，detail=list） | `test_a17_response_contract_keeps_legacy_keys_and_adds_seven` |
| A-18 | 前端与静态红线 | `test_a18_frontend_bridge_and_static_redlines` |
| A-19 | Q2 终态与重办血缘（含 F11 同键重放） | `test_a19_terminal_states_and_supersedes_lineage` |
| A-20 | Q1b 双账号与禁绕过 | `test_a20_dual_account_fixture_and_no_self_approval_bypass` |

口径说明（读测试前先读）：

- **零 live**：全部走本地 SQLite + 真实 HTTP 路由，不触碰任何外部平台。
- **§6.1 夹具红线**：本文件顶部提供 `maker_checker_fixture`——同工作空间两个**真实**账号
  （`maker` / `checker`，均为 `operations`），两个**独立** `TestClient` 各自登录（各自 cookie jar
  与 CSRF token），禁止同一 client 反复切换身份。**没有任何一条用例为跑通而削弱 U3**：
  产品代码里自审判定是唯一无豁免分支，`app/purchase_approval.py` 的 `SELF_APPROVAL_FORBIDDEN`
  命中数恒为 2（判定 + 抛错），本文件不含任何针对自审校验的 monkeypatch / 短路
  （A-20 ④ 对本文件自身源码做静态断言）。
- **历史数据构造**：A-2 ~ A-14 中与状态机有关的用例，把采购单的 `created_by`/`submitted_by`
  写成不存在的 login（如 `system:p3s1-fixture-maker`）或由 `maker` 真实提交，
  保证审批者与制单者**不是同一人**——这是 §6.1 第 4 条允许的等价构造，不是绕过。
- **CAS 判定顺序**：状态先于版本。因此"版本冲突"用例必须让单子**仍处于可决策状态**
  （`submitted`）而 `version` 落后；并发用例里失败方看到的是状态已变
  （`INVALID_PURCHASE_REQUEST_STATE`）。
"""

from __future__ import annotations

import ast
import importlib
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILE = Path(__file__).resolve()

# 既有 16 键契约（逐字节不变）与 P3-S1 新增 7 键。
LEGACY_KEYS = (
    "id", "workspace_id", "warehouse_id", "request_no", "status", "note",
    "submitted_by", "submitted_at", "created_by", "version", "supplier_ref",
    "expected_arrival_date", "idempotency_key", "created_at", "updated_at", "lines",
)
NEW_KEYS = (
    "approver_id", "approver_user_id", "decided_at", "reject_reason",
    "cancelled_by", "cancelled_at", "supersedes_request_id",
)

# 历史数据构造用的"不存在的 login"（§6.1 第 4 条允许的等价写法）。
FIXTURE_MAKER = "system:p3s1-fixture-maker"


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _run_alembic(args: list[str], *, database_url: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(REPO_ROOT),
    )


def _table_snapshot(eng, table: str) -> dict:
    """逐列快照：列名/类型/可空性/默认值/列顺序 + 索引 + 唯一约束。"""
    insp = inspect(eng)
    columns = [
        {
            "name": item["name"], "type": str(item["type"]),
            "nullable": item["nullable"], "default": item["default"],
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
    response = client.post(
        "/login",
        data={"login": login, "password": password, "next": "/ops"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303), response.text
    csrf = client.cookies.get("dianshang_csrf")
    assert csrf, "缺少 CSRF cookie"
    client.headers.update({"X-CSRF-Token": csrf})
    return client


def _canonical_hash(value: dict) -> str:
    """与 `app/replenishment.py` / `app/purchase_approval.py` 同口径的规范化哈希。"""
    import hashlib
    import json

    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _key(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@dataclass
class Account:
    login: str
    user_id: int
    role: str


@dataclass
class Ctx:
    db_mod: object
    main_mod: object
    cfg: object
    bg_mod: object
    workspace_id: int
    other_workspace_id: int
    warehouse_id: int
    sku_id: int
    accounts: dict = field(default_factory=dict)
    clients: dict = field(default_factory=dict)

    def account(self, login: str) -> Account:
        return self.accounts[login]

    def client(self, login: str) -> TestClient:
        return self.clients[login]


@pytest.fixture
def maker_checker_fixture(tmp_path, monkeypatch):
    """§6.1 合规夹具：maker（制单/提交）+ checker（审批），两个真实账号、两个独立 client。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'p3s1.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IMPORTS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")

    import app.config as cfg
    import app.db as db_mod
    import app.repository as repo_mod
    import app.background as bg_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(repo_mod)
    importlib.reload(bg_mod)

    import app.main as main_mod

    importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)

    from app.employee_auth import hash_password
    from app.schemas import ProductRecord

    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="p3s1", name="P3-S1 审批商家")
        other = db_mod.Workspace(tenant_key="p3s1-other", name="P3-S1 别家商家")
        db.add_all([workspace, other])
        db.flush()
        users = [
            ("maker", "operations"),
            ("checker", "operations"),
            ("admin", "admin"),
            ("reader", "readonly"),
            ("wh", "warehouse"),
            ("other-admin", "admin"),
        ]
        created: dict = {}
        for login, role in users:
            account = db_mod.UserAccount(
                login=login, password_hash=hash_password("pw"), display_name=login,
            )
            db.add(account)
            db.flush()
            created[login] = (account, role)
        for login, (account, role) in created.items():
            target = other if login == "other-admin" else workspace
            db.add(db_mod.WorkspaceMembership(
                workspace_id=target.id, user_id=account.id, role=role,
            ))
        warehouse = db_mod.Warehouse(
            workspace_id=workspace.id, code="W-P3S1", name="P3-S1 仓", warehouse_type="own",
        )
        db.add(warehouse)
        db.flush()
        product = repo_mod.upsert_product(
            db,
            ProductRecord(
                source="fixture", external_product_id="p3s1-p", title="P3-S1 商品",
                url="https://fixture.local/p3s1-p", current_price=Decimal("10.00"),
                observed_at=__import__("datetime").datetime.utcnow(),
            ),
            workspace_id=workspace.id,
        )
        db.flush()
        sku = db_mod.ProductSku(workspace_id=workspace.id, product_id=product.id, sku_code="P3S1-SKU")
        db.add(sku)
        db.flush()
        db.add(db_mod.InventoryPolicy(
            workspace_id=workspace.id, warehouse_id=warehouse.id, sku_id=sku.id,
            safety_stock_qty=5, reorder_point_qty=8,
        ))
        db.add(db_mod.InventoryBalance(
            workspace_id=workspace.id, warehouse_id=warehouse.id, sku_id=sku.id, on_hand_qty=4,
        ))
        db.commit()
        workspace_id = workspace.id
        other_workspace_id = other.id
        warehouse_id = warehouse.id
        sku_id = sku.id
        accounts = {
            login: Account(login=login, user_id=account.id, role=role)
            for login, (account, role) in created.items()
        }

    ctx = Ctx(
        db_mod=db_mod, main_mod=main_mod, cfg=cfg, bg_mod=bg_mod,
        workspace_id=workspace_id, other_workspace_id=other_workspace_id,
        warehouse_id=warehouse_id, sku_id=sku_id, accounts=accounts,
    )
    for login in ("maker", "checker", "admin", "reader", "wh", "other-admin"):
        client = TestClient(main_mod.app)
        _login(client, login)
        ctx.clients[login] = client

    yield ctx

    bg_mod.reset_executor()
    cfg.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 数据构造辅助（走真实 API 或历史数据构造，均不改产品代码）
# ---------------------------------------------------------------------------


def _seed_suggestion(ctx: Ctx, *, qty: int = 10, status: str = "confirmed") -> int:
    """种一条可用的补货建议（confirmed + decision_qty），供采购单明细引用。"""
    with ctx.db_mod.SessionLocal() as db:
        suggestion = ctx.db_mod.ReplenishmentSuggestion(
            workspace_id=ctx.workspace_id, warehouse_id=ctx.warehouse_id,
            sku_id=ctx.sku_id, status=status, suggested_qty=qty, decision_qty=qty,
            source_hash=f"p3s1-{uuid4().hex}", source_snapshot_json="{}",
            as_of_date=date.today(), version=1, created_by="system:seed",
        )
        db.add(suggestion)
        db.commit()
        db.refresh(suggestion)
        return suggestion.id


def _create_submitted(ctx: Ctx, *, client: TestClient | None = None, note: str | None = None) -> dict:
    """走真实 API 建一张 submitted 采购单（默认由 maker 提交）。"""
    client = client or ctx.client("maker")
    suggestion_id = _seed_suggestion(ctx)
    response = client.post(
        "/api/purchase-requests",
        json={"suggestion_ids": [suggestion_id], "note": note},
        headers={"Idempotency-Key": _key("pr")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_submitted_via_draft(ctx: Ctx, *, client: TestClient | None = None) -> dict:
    """走 draft → submit 真实路径，得到 version=2 的 submitted 采购单。"""
    client = client or ctx.client("maker")
    suggestion_id = _seed_suggestion(ctx)
    created = client.post(
        "/api/purchase-requests/drafts",
        json={"suggestion_ids": [suggestion_id]},
        headers={"Idempotency-Key": _key("draft")},
    )
    assert created.status_code == 201, created.text
    submitted = client.post(
        f"/api/purchase-requests/{created.json()['id']}/submit",
        json={"expected_version": 1},
        headers={"Idempotency-Key": _key("submit")},
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["version"] == 2, submitted.text
    return submitted.json()


def _create_historical_submitted(ctx: Ctx, *, created_by: str = FIXTURE_MAKER) -> dict:
    """历史数据构造：制单人/提交人写成不存在的 login，审批者可自由切换。"""
    suggestion_id = _seed_suggestion(ctx)
    with ctx.db_mod.SessionLocal() as db:
        request = ctx.db_mod.PurchaseRequest(
            workspace_id=ctx.workspace_id, warehouse_id=ctx.warehouse_id,
            request_no=f"PR-{uuid4().hex[:12].upper()}", status="submitted",
            note="历史数据构造", submitted_by=created_by, created_by=created_by,
            submitted_at=__import__("datetime").datetime.utcnow(),
            idempotency_key=_key("hist"), payload_hash=_canonical_hash({"history": True}),
            version=1,
        )
        db.add(request)
        db.flush()
        db.add(ctx.db_mod.PurchaseRequestLine(
            workspace_id=ctx.workspace_id, purchase_request_id=request.id,
            warehouse_id=ctx.warehouse_id, sku_id=ctx.sku_id,
            suggestion_id=suggestion_id, requested_qty=10, source_suggestion_version=1,
        ))
        db.commit()
        return {"id": request.id, "version": request.version, "status": request.status}


def _row(ctx: Ctx, request_id: int):
    with ctx.db_mod.SessionLocal() as db:
        return db.get(ctx.db_mod.PurchaseRequest, request_id)


def _action_rows(ctx: Ctx, request_id: int) -> list:
    with ctx.db_mod.SessionLocal() as db:
        return db.scalars(
            select(ctx.db_mod.PurchaseRequestAction)
            .where(ctx.db_mod.PurchaseRequestAction.purchase_request_id == request_id)
            .order_by(ctx.db_mod.PurchaseRequestAction.id)
        ).all()


def _counts(ctx: Ctx) -> dict:
    with ctx.db_mod.SessionLocal() as db:
        return {
            "requests": db.scalar(select(func.count(ctx.db_mod.PurchaseRequest.id))) or 0,
            "actions": db.scalar(select(func.count(ctx.db_mod.PurchaseRequestAction.id))) or 0,
            "inbound": db.scalar(select(func.count(ctx.db_mod.InboundOrder.id))) or 0,
            "balances": db.scalar(select(func.count(ctx.db_mod.InventoryBalance.id))) or 0,
            "transactions": db.scalar(select(func.count(ctx.db_mod.InventoryTransaction.id))) or 0,
        }


def _error_code(response) -> str:
    return response.json()["detail"]["code"]


# ---------------------------------------------------------------------------
# A-1 migration 0023 up/down
# ---------------------------------------------------------------------------


def test_a1_migration_0023_up_and_down_restores_0022_shape(tmp_path):
    """A-1：up 加 8 列（7+1）+ 唯一索引；down 后两表快照与 0022 形态整体相等；未用 batch。"""
    db_url = f"sqlite:///{tmp_path / 'a1.db'}"
    upgrade_0022 = _run_alembic(["upgrade", "0022_external_sync_dead_letters"], database_url=db_url)
    assert upgrade_0022.returncode == 0, upgrade_0022.stdout + upgrade_0022.stderr

    eng = create_engine(db_url)
    requests_before = _table_snapshot(eng, "purchase_requests")
    inbound_before = _table_snapshot(eng, "inbound_orders")
    before_request_columns = [item["name"] for item in requests_before["columns"]]
    before_inbound_columns = [item["name"] for item in inbound_before["columns"]]
    assert "approver_id" not in before_request_columns
    assert "purchase_request_id" not in before_inbound_columns

    upgrade_head = _run_alembic(["upgrade", "head"], database_url=db_url)
    assert upgrade_head.returncode == 0, upgrade_head.stdout + upgrade_head.stderr

    requests_after = _table_snapshot(eng, "purchase_requests")
    inbound_after = _table_snapshot(eng, "inbound_orders")
    after_request_columns = [item["name"] for item in requests_after["columns"]]
    after_inbound_columns = [item["name"] for item in inbound_after["columns"]]

    # 8 列：purchase_requests 追加 7，inbound_orders 追加 1（物理顺序在末尾）。
    assert after_request_columns == before_request_columns + [
        "approver_id", "approver_user_id", "decided_at", "reject_reason",
        "cancelled_by", "cancelled_at", "supersedes_request_id",
    ], after_request_columns
    assert after_inbound_columns == before_inbound_columns + ["purchase_request_id"], after_inbound_columns
    # 全列可空、无默认值（无回填、无 server_default）。
    for item in requests_after["columns"][len(before_request_columns):]:
        assert item["nullable"] is True, item
        assert item["default"] is None, item
    for item in inbound_after["columns"][len(before_inbound_columns):]:
        assert item["nullable"] is True, item
        assert item["default"] is None, item
    # 唯一索引。
    assert (
        "uq_inbound_order_purchase_request",
        ("workspace_id", "purchase_request_id"),
        True,
    ) in inbound_after["indexes"], inbound_after["indexes"]

    downgrade = _run_alembic(["downgrade", "0022_external_sync_dead_letters"], database_url=db_url)
    assert downgrade.returncode == 0, downgrade.stdout + downgrade.stderr
    assert _table_snapshot(eng, "purchase_requests") == requests_before
    assert _table_snapshot(eng, "inbound_orders") == inbound_before

    # 再升一次仍可成功（迁移幂等可重复执行）。
    again = _run_alembic(["upgrade", "head"], database_url=db_url)
    assert again.returncode == 0, again.stdout + again.stderr

    # 未使用 SQLite batch（batch 会重建成表，本片红线禁止）。
    migration_source = (REPO_ROOT / "migrations" / "versions" / "0023_purchase_approval.py").read_text(encoding="utf-8")
    assert "op.batch_alter_table" not in migration_source


# ---------------------------------------------------------------------------
# A-2 ~ A-6 approve 主干
# ---------------------------------------------------------------------------


def test_a2_approve_success_persists_approver_and_action(maker_checker_fixture):
    """A-2：status=approved，approver_id/approver_user_id/decided_at 落库，version+1，审计行齐备。"""
    ctx = maker_checker_fixture
    request = _create_historical_submitted(ctx)
    checker = ctx.client("checker")
    checker_id = ctx.account("checker").user_id

    response = checker.post(
        f"/api/purchase-requests/{request['id']}/approve",
        json={"expected_version": 1, "note": "同意补货"},
        headers={"Idempotency-Key": _key("approve"), "X-Request-ID": "req-a2"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["approver_id"] == "checker"
    assert body["approver_user_id"] == checker_id
    assert body["decided_at"] is not None
    assert body["version"] == 2

    row = _row(ctx, request["id"])
    assert row.status == "approved"
    assert row.approver_id == "checker"
    assert row.approver_user_id == checker_id
    assert row.decided_at is not None
    assert row.version == 2

    actions = _action_rows(ctx, request["id"])
    approve_actions = [item for item in actions if item.action_type == "approve"]
    assert len(approve_actions) == 1
    action = approve_actions[0]
    assert action.actor == "checker"
    assert action.request_id == "req-a2"
    assert action.payload_hash.startswith("sha256:")
    assert action.from_status == "submitted"
    assert action.to_status == "approved"
    assert action.expected_version == 1


def test_a3_approve_is_idempotent_on_same_key(maker_checker_fixture):
    """A-3：同键同载荷二次 approve → 200 同结果；version 不再增；action 行恰好 1 条。"""
    ctx = maker_checker_fixture
    request = _create_historical_submitted(ctx)
    checker = ctx.client("checker")
    key = _key("approve")
    payload = {"expected_version": 1, "note": "同键重放"}

    first = checker.post(
        f"/api/purchase-requests/{request['id']}/approve", json=payload,
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 200, first.text
    second = checker.post(
        f"/api/purchase-requests/{request['id']}/approve", json=payload,
        headers={"Idempotency-Key": key},
    )
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert second.json()["version"] == 2
    assert len([item for item in _action_rows(ctx, request["id"]) if item.action_type == "approve"]) == 1


def test_a4_idempotency_key_reuse_returns_409(maker_checker_fixture):
    """A-4：同 key 不同载荷 → 409 IDEMPOTENCY_KEY_REUSE，状态不变。"""
    ctx = maker_checker_fixture
    request = _create_historical_submitted(ctx)
    checker = ctx.client("checker")
    key = _key("approve")

    first = checker.post(
        f"/api/purchase-requests/{request['id']}/approve",
        json={"expected_version": 1, "note": "原始载荷"},
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 200, first.text
    reuse = checker.post(
        f"/api/purchase-requests/{request['id']}/approve",
        json={"expected_version": 1, "note": "换载荷"},
        headers={"Idempotency-Key": key},
    )
    assert reuse.status_code == 409, reuse.text
    assert _error_code(reuse) == "IDEMPOTENCY_KEY_REUSE"
    row = _row(ctx, request["id"])
    assert row.status == "approved"
    assert row.version == 2
    assert len([item for item in _action_rows(ctx, request["id"]) if item.action_type == "approve"]) == 1


def test_a5_version_conflict_returns_409(maker_checker_fixture):
    """A-5：同一单版本落后（状态仍可决策）→ 409 PURCHASE_REQUEST_VERSION_CONFLICT。

    判定顺序为"状态先于版本"（并发方看到状态已变 → INVALID_PURCHASE_REQUEST_STATE，见 A-6），
    因此版本冲突用例必须让单子**仍为 submitted** 而 `version` 落后：这里用真实
    draft → submit 路径得到 version=2 的 submitted 单，再以 version=1 调 approve。
    """
    ctx = maker_checker_fixture
    request = _create_submitted_via_draft(ctx)
    assert request["version"] == 2
    checker = ctx.client("checker")

    conflict = checker.post(
        f"/api/purchase-requests/{request['id']}/approve",
        json={"expected_version": 1},
        headers={"Idempotency-Key": _key("approve")},
    )
    assert conflict.status_code == 409, conflict.text
    assert _error_code(conflict) == "PURCHASE_REQUEST_VERSION_CONFLICT"
    row = _row(ctx, request["id"])
    assert row.status == "submitted"
    assert row.version == 2
    assert row.approver_id is None


def test_a6_concurrent_cas_allows_exactly_one_winner(maker_checker_fixture):
    """A-6：两个不同用户用同一 expected_version 各调一次 approve → 恰好 1 个 200、1 个 409。"""
    ctx = maker_checker_fixture
    request = _create_historical_submitted(ctx)
    responses = []
    for login in ("checker", "admin"):
        responses.append(ctx.client(login).post(
            f"/api/purchase-requests/{request['id']}/approve",
            json={"expected_version": 1},
            headers={"Idempotency-Key": _key("approve")},
        ))
    codes = sorted(response.status_code for response in responses)
    assert codes == [200, 409], [response.text for response in responses]
    loser = next(response for response in responses if response.status_code == 409)
    assert _error_code(loser) == "INVALID_PURCHASE_REQUEST_STATE"

    winner = next(response for response in responses if response.status_code == 200)
    row = _row(ctx, request["id"])
    assert row.approver_id == winner.json()["approver_id"]
    assert row.version == 2
    assert len([item for item in _action_rows(ctx, request["id"]) if item.action_type == "approve"]) == 1


# ---------------------------------------------------------------------------
# A-7 ~ A-13 自审、权限、reject、cancel
# ---------------------------------------------------------------------------


def test_a7_self_approval_is_forbidden_and_writes_nothing(maker_checker_fixture):
    """A-7：制单人本人 approve / reject → 409 SELF_APPROVAL_FORBIDDEN，数据库零变化。"""
    ctx = maker_checker_fixture
    request = _create_historical_submitted(ctx, created_by="maker")
    maker = ctx.client("maker")
    before = _counts(ctx)
    row_before = _row(ctx, request["id"])

    approve = maker.post(
        f"/api/purchase-requests/{request['id']}/approve",
        json={"expected_version": 1},
        headers={"Idempotency-Key": _key("approve")},
    )
    assert approve.status_code == 409, approve.text
    assert _error_code(approve) == "SELF_APPROVAL_FORBIDDEN"

    reject = maker.post(
        f"/api/purchase-requests/{request['id']}/reject",
        json={"expected_version": 1, "reason": "自审应当被拒"},
        headers={"Idempotency-Key": _key("reject")},
    )
    assert reject.status_code == 409, reject.text
    assert _error_code(reject) == "SELF_APPROVAL_FORBIDDEN"

    after = _counts(ctx)
    assert after == before, (before, after)
    row_after = _row(ctx, request["id"])
    assert row_after.status == row_before.status
    assert row_after.version == row_before.version
    assert row_after.approver_id is None
    assert row_after.decided_at is None
    assert row_after.reject_reason is None


def test_a8_permission_gate_blocks_non_approvers(maker_checker_fixture, monkeypatch):
    """A-8：warehouse / readonly → 403；operations → 通过；收窄 operations 后 approve/reject 403、cancel 200。"""
    ctx = maker_checker_fixture
    request = _create_historical_submitted(ctx)

    # 角色闸：warehouse 与 readonly 都没有 purchasing.approve。
    for login in ("wh", "reader"):
        response = ctx.client(login).post(
            f"/api/purchase-requests/{request['id']}/approve",
            json={"expected_version": 1},
            headers={"Idempotency-Key": _key("approve")},
        )
        assert response.status_code == 403, (login, response.text)
        assert _error_code(response) == "FORBIDDEN"

    # operations 有 purchasing.approve → 通过。
    ok = ctx.client("checker").post(
        f"/api/purchase-requests/{request['id']}/approve",
        json={"expected_version": 1},
        headers={"Idempotency-Key": _key("approve")},
    )
    assert ok.status_code == 200, ok.text

    # "互不替代"的证明：临时收窄 operations 的权限集（先例 tests/test_p2s1_connector.py:187）。
    # 只收窄 ROLE_PERMISSIONS 的字典项，不改 app/identity.py 的表定义本身。
    import app.identity as identity_mod

    owned = _create_historical_submitted(ctx, created_by="maker")
    monkeypatch.setitem(
        identity_mod.ROLE_PERMISSIONS, "operations",
        frozenset({"purchasing.submit", "purchase.read"}),
    )
    maker = ctx.client("maker")
    approve = maker.post(
        f"/api/purchase-requests/{owned['id']}/approve",
        json={"expected_version": 1},
        headers={"Idempotency-Key": _key("approve")},
    )
    assert approve.status_code == 403, approve.text
    assert _error_code(approve) == "FORBIDDEN"
    reject = maker.post(
        f"/api/purchase-requests/{owned['id']}/reject",
        json={"expected_version": 1, "reason": "无审批权"},
        headers={"Idempotency-Key": _key("reject")},
    )
    assert reject.status_code == 403, reject.text
    cancel = maker.post(
        f"/api/purchase-requests/{owned['id']}/cancel",
        json={"expected_version": 1},
        headers={"Idempotency-Key": _key("cancel")},
    )
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "cancelled"


def test_a9_reject_reason_is_required_and_writes_nothing(maker_checker_fixture):
    """A-9：缺 reason / 空串 / 全空格 → 422 REJECT_REASON_REQUIRED，零写入。"""
    ctx = maker_checker_fixture
    request = _create_historical_submitted(ctx)
    checker = ctx.client("checker")
    before = _counts(ctx)

    for payload in ({"expected_version": 1}, {"expected_version": 1, "reason": ""}, {"expected_version": 1, "reason": "   "}):
        response = checker.post(
            f"/api/purchase-requests/{request['id']}/reject",
            json=payload,
            headers={"Idempotency-Key": _key("reject")},
        )
        assert response.status_code == 422, (payload, response.text)
        assert _error_code(response) == "REJECT_REASON_REQUIRED"

    assert _counts(ctx) == before, (before, _counts(ctx))
    row = _row(ctx, request["id"])
    assert row.status == "submitted"
    assert row.version == 1
    assert row.reject_reason is None


def test_a10_reject_success_persists_reason_and_action(maker_checker_fixture):
    """A-10：status=rejected，reject_reason 原文落库，decided_at 非空，approver_id=审批人。"""
    ctx = maker_checker_fixture
    request = _create_historical_submitted(ctx)
    checker = ctx.client("checker")

    response = checker.post(
        f"/api/purchase-requests/{request['id']}/reject",
        json={"expected_version": 1, "reason": "供应商缺货，改期再报"},
        headers={"Idempotency-Key": _key("reject"), "X-Request-ID": "req-a10"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "rejected"
    assert body["reject_reason"] == "供应商缺货，改期再报"
    assert body["decided_at"] is not None
    assert body["approver_id"] == "checker"
    assert body["version"] == 2

    row = _row(ctx, request["id"])
    assert row.status == "rejected"
    assert row.reject_reason == "供应商缺货，改期再报"
    assert row.decided_at is not None
    assert row.approver_id == "checker"

    reject_actions = [item for item in _action_rows(ctx, request["id"]) if item.action_type == "reject"]
    assert len(reject_actions) == 1
    assert reject_actions[0].actor == "checker"
    assert reject_actions[0].request_id == "req-a10"
    assert reject_actions[0].to_status == "rejected"


def test_a11_state_machine_rejects_invalid_transitions(maker_checker_fixture):
    """A-11：draft→approve / rejected→approve / cancelled→approve / approved→approve 均 409。"""
    ctx = maker_checker_fixture
    checker = ctx.client("checker")

    def expect_invalid_state(request_id: int, label: str) -> None:
        response = checker.post(
            f"/api/purchase-requests/{request_id}/approve",
            json={"expected_version": 1},
            headers={"Idempotency-Key": _key("approve")},
        )
        assert response.status_code == 409, (label, response.text)
        assert _error_code(response) == "INVALID_PURCHASE_REQUEST_STATE", label

    # draft → approve（草稿由 maker 建、checker 审批，避免自审判定抢先短路状态判定）
    suggestion_id = _seed_suggestion(ctx)
    draft = ctx.client("maker").post(
        "/api/purchase-requests/drafts",
        json={"suggestion_ids": [suggestion_id]},
        headers={"Idempotency-Key": _key("draft")},
    )
    assert draft.status_code == 201, draft.text
    expect_invalid_state(draft.json()["id"], "draft")

    # rejected → approve
    rejected = _create_historical_submitted(ctx)
    reject = checker.post(
        f"/api/purchase-requests/{rejected['id']}/reject",
        json={"expected_version": 1, "reason": "不批"},
        headers={"Idempotency-Key": _key("reject")},
    )
    assert reject.status_code == 200, reject.text
    expect_invalid_state(rejected["id"], "rejected")

    # cancelled → approve（撤回由制单人 maker 发起，审批用 checker）
    cancelled = _create_historical_submitted(ctx, created_by="maker")
    cancel = ctx.client("maker").post(
        f"/api/purchase-requests/{cancelled['id']}/cancel",
        json={"expected_version": 1},
        headers={"Idempotency-Key": _key("cancel")},
    )
    assert cancel.status_code == 200, cancel.text
    expect_invalid_state(cancelled["id"], "cancelled")

    # approved → approve
    approved = _create_historical_submitted(ctx)
    approve = checker.post(
        f"/api/purchase-requests/{approved['id']}/approve",
        json={"expected_version": 1},
        headers={"Idempotency-Key": _key("approve")},
    )
    assert approve.status_code == 200, approve.text
    expect_invalid_state(approved["id"], "approved")


def test_a12_cancel_marks_cancelled_without_deleting_row(maker_checker_fixture):
    """A-12：submitted→cancelled，cancelled_by/cancelled_at 落库，行仍在（零 DELETE），version+1。"""
    ctx = maker_checker_fixture
    request = _create_historical_submitted(ctx, created_by="maker")
    maker = ctx.client("maker")

    response = maker.post(
        f"/api/purchase-requests/{request['id']}/cancel",
        json={"expected_version": 1, "reason": "改走别的供应商"},
        headers={"Idempotency-Key": _key("cancel")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["cancelled_by"] == "maker"
    assert body["cancelled_at"] is not None
    assert body["version"] == 2

    row = _row(ctx, request["id"])
    assert row is not None, "撤回不得物理删除行"
    assert row.status == "cancelled"
    assert row.cancelled_by == "maker"
    assert row.cancelled_at is not None
    assert row.version == 2

    cancel_actions = [item for item in _action_rows(ctx, request["id"]) if item.action_type == "cancel"]
    assert len(cancel_actions) == 1
    assert cancel_actions[0].actor == "maker"
    assert cancel_actions[0].to_status == "cancelled"


def test_a13_cancel_enforces_ownership_and_inbound_guard(maker_checker_fixture):
    """A-13：非制单人 cancel → 403 PURCHASE_REQUEST_NOT_OWNER；admin → 通过；已生成入库单 → 409。"""
    ctx = maker_checker_fixture
    request = _create_historical_submitted(ctx, created_by="maker")

    stranger = ctx.client("checker")
    denied = stranger.post(
        f"/api/purchase-requests/{request['id']}/cancel",
        json={"expected_version": 1},
        headers={"Idempotency-Key": _key("cancel")},
    )
    assert denied.status_code == 403, denied.text
    assert _error_code(denied) == "PURCHASE_REQUEST_NOT_OWNER"

    by_admin = ctx.client("admin").post(
        f"/api/purchase-requests/{request['id']}/cancel",
        json={"expected_version": 1},
        headers={"Idempotency-Key": _key("cancel")},
    )
    assert by_admin.status_code == 200, by_admin.text
    assert by_admin.json()["cancelled_by"] == "admin"

    # approved 且已生成入库单 → 409 PURCHASE_REQUEST_ALREADY_INBOUND
    guarded = _create_historical_submitted(ctx, created_by="maker")
    checker = ctx.client("checker")
    approved = checker.post(
        f"/api/purchase-requests/{guarded['id']}/approve",
        json={"expected_version": 1},
        headers={"Idempotency-Key": _key("approve")},
    )
    assert approved.status_code == 200, approved.text
    generated = checker.post(
        f"/api/purchase-requests/{guarded['id']}/inbound-orders",
        json={"note": "生成入库单"},
        headers={"Idempotency-Key": _key("inbound")},
    )
    assert generated.status_code == 201, generated.text
    blocked = ctx.client("maker").post(
        f"/api/purchase-requests/{guarded['id']}/cancel",
        json={"expected_version": 2},
        headers={"Idempotency-Key": _key("cancel")},
    )
    assert blocked.status_code == 409, blocked.text
    assert _error_code(blocked) == "PURCHASE_REQUEST_ALREADY_INBOUND"


# ---------------------------------------------------------------------------
# A-14 ~ A-16 生成入库单
# ---------------------------------------------------------------------------


def test_a14_generate_inbound_order_has_zero_inventory_side_effect(maker_checker_fixture):
    """A-14：201；purchase_request_id 落库；expected_qty 与 requested_qty 逐 SKU 对齐；库存零副作用。"""
    ctx = maker_checker_fixture
    request = _create_historical_submitted(ctx)
    checker = ctx.client("checker")
    approved = checker.post(
        f"/api/purchase-requests/{request['id']}/approve",
        json={"expected_version": 1},
        headers={"Idempotency-Key": _key("approve")},
    )
    assert approved.status_code == 200, approved.text

    before = _counts(ctx)
    with ctx.db_mod.SessionLocal() as db:
        balance_before = db.scalar(
            select(ctx.db_mod.InventoryBalance.on_hand_qty).where(
                ctx.db_mod.InventoryBalance.warehouse_id == ctx.warehouse_id,
                ctx.db_mod.InventoryBalance.sku_id == ctx.sku_id,
            )
        )

    response = checker.post(
        f"/api/purchase-requests/{request['id']}/inbound-orders",
        json={"note": "生成入库单"},
        headers={"Idempotency-Key": _key("inbound")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["reference_no"] == f"INB-{_request_no(ctx, request['id'])}"
    assert body["status"] == "expected"
    assert len(body["lines"]) == 1
    assert body["lines"][0]["sku_id"] == ctx.sku_id
    assert body["lines"][0]["expected_qty"] == 10

    with ctx.db_mod.SessionLocal() as db:
        order = db.scalar(
            select(ctx.db_mod.InboundOrder).where(
                ctx.db_mod.InboundOrder.workspace_id == ctx.workspace_id,
                ctx.db_mod.InboundOrder.purchase_request_id == request["id"],
            )
        )
        assert order is not None
        assert order.purchase_request_id == request["id"]
        assert order.created_by == "checker"
        assert order.reference_no == f"INB-{order.reference_no.split('INB-', 1)[1]}"
        lines = db.scalars(
            select(ctx.db_mod.InboundLine).where(ctx.db_mod.InboundLine.inbound_order_id == order.id)
        ).all()
        assert len(lines) == 1
        assert lines[0].sku_id == ctx.sku_id
        assert lines[0].expected_qty == 10
        balance_after = db.scalar(
            select(ctx.db_mod.InventoryBalance.on_hand_qty).where(
                ctx.db_mod.InventoryBalance.warehouse_id == ctx.warehouse_id,
                ctx.db_mod.InventoryBalance.sku_id == ctx.sku_id,
            )
        )
        assert balance_after == balance_before

    after = _counts(ctx)
    assert after["transactions"] == before["transactions"], "生成入库单不得产生库存流水"
    assert after["balances"] == before["balances"], "生成入库单不得新增库存余额行"


def _request_no(ctx: Ctx, request_id: int) -> str:
    with ctx.db_mod.SessionLocal() as db:
        return db.get(ctx.db_mod.PurchaseRequest, request_id).request_no


def test_a15_generate_inbound_order_is_idempotent_on_unique_conflict(maker_checker_fixture):
    """A-15：二次生成 → 200 同 id，库中该 purchase_request_id 只有 1 行；唯一冲突直接构造亦返回 200。"""
    ctx = maker_checker_fixture
    checker = ctx.client("checker")
    request = _create_historical_submitted(ctx)
    checker.post(
        f"/api/purchase-requests/{request['id']}/approve",
        json={"expected_version": 1},
        headers={"Idempotency-Key": _key("approve")},
    )
    first = checker.post(
        f"/api/purchase-requests/{request['id']}/inbound-orders",
        json={"note": "第一次"},
        headers={"Idempotency-Key": _key("inbound")},
    )
    assert first.status_code == 201, first.text
    second = checker.post(
        f"/api/purchase-requests/{request['id']}/inbound-orders",
        json={"note": "第二次"},
        headers={"Idempotency-Key": _key("inbound")},
    )
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    with ctx.db_mod.SessionLocal() as db:
        rows = db.scalars(
            select(ctx.db_mod.InboundOrder).where(
                ctx.db_mod.InboundOrder.workspace_id == ctx.workspace_id,
                ctx.db_mod.InboundOrder.purchase_request_id == request["id"],
            )
        ).all()
        assert len(rows) == 1

    # 直接构造唯一冲突：手工插入一行同 (workspace_id, purchase_request_id) 的入库单，
    # 再调接口必须返回 200（命中幂等），且唯一索引在库层面确实拦得住重复插入。
    other = _create_historical_submitted(ctx)
    checker.post(
        f"/api/purchase-requests/{other['id']}/approve",
        json={"expected_version": 1},
        headers={"Idempotency-Key": _key("approve")},
    )
    with ctx.db_mod.SessionLocal() as db:
        conflict = ctx.db_mod.InboundOrder(
            workspace_id=ctx.workspace_id, warehouse_id=ctx.warehouse_id,
            reference_no=f"INB-CONFLICT-{uuid4().hex[:8]}", status="expected",
            created_by="system:conflict", purchase_request_id=other["id"],
        )
        db.add(conflict)
        db.commit()
        db.refresh(conflict)
        conflict_id = conflict.id
        with ctx.db_mod.engine.raw_connection() as raw:
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(
                    "INSERT INTO inbound_orders (workspace_id, warehouse_id, reference_no, status, "
                    "created_by, created_at, updated_at, purchase_request_id) "
                    "VALUES (?, ?, ?, 'expected', 'system:dup', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)",
                    (ctx.workspace_id, ctx.warehouse_id, f"INB-DUP-{uuid4().hex[:8]}", other["id"]),
                )
            raw.rollback()

    conflict_response = checker.post(
        f"/api/purchase-requests/{other['id']}/inbound-orders",
        json={"note": "唯一冲突"},
        headers={"Idempotency-Key": _key("inbound")},
    )
    assert conflict_response.status_code == 200, conflict_response.text
    assert conflict_response.json()["id"] == conflict_id
    with ctx.db_mod.SessionLocal() as db:
        rows = db.scalars(
            select(ctx.db_mod.InboundOrder).where(
                ctx.db_mod.InboundOrder.workspace_id == ctx.workspace_id,
                ctx.db_mod.InboundOrder.purchase_request_id == other["id"],
            )
        ).all()
        assert len(rows) == 1


def test_a16_generate_inbound_order_preconditions_and_authorization(maker_checker_fixture):
    """A-16：draft/submitted/rejected/cancelled → 409；无 inbound.create → 403；跨 workspace → 404。"""
    ctx = maker_checker_fixture
    checker = ctx.client("checker")

    # draft
    suggestion_id = _seed_suggestion(ctx)
    draft = checker.post(
        "/api/purchase-requests/drafts",
        json={"suggestion_ids": [suggestion_id]},
        headers={"Idempotency-Key": _key("draft")},
    )
    assert draft.status_code == 201, draft.text
    # submitted
    submitted = _create_historical_submitted(ctx)
    # rejected
    rejected = _create_historical_submitted(ctx)
    checker.post(
        f"/api/purchase-requests/{rejected['id']}/reject",
        json={"expected_version": 1, "reason": "不批"},
        headers={"Idempotency-Key": _key("reject")},
    )
    # cancelled
    cancelled = _create_historical_submitted(ctx)
    checker.post(
        f"/api/purchase-requests/{cancelled['id']}/cancel",
        json={"expected_version": 1},
        headers={"Idempotency-Key": _key("cancel")},
    )

    for request_id, label in (
        (draft.json()["id"], "draft"),
        (submitted["id"], "submitted"),
        (rejected["id"], "rejected"),
        (cancelled["id"], "cancelled"),
    ):
        response = checker.post(
            f"/api/purchase-requests/{request_id}/inbound-orders",
            json={"note": "不该生成"},
            headers={"Idempotency-Key": _key("inbound")},
        )
        assert response.status_code == 409, (label, response.text)
        assert _error_code(response) == "INVALID_PURCHASE_REQUEST_STATE", label

    # 无 inbound.create → 403（readonly 角色无该权限点）
    forbidden = ctx.client("reader").post(
        f"/api/purchase-requests/{submitted['id']}/inbound-orders",
        json={"note": "越权"},
        headers={"Idempotency-Key": _key("inbound")},
    )
    assert forbidden.status_code == 403, forbidden.text
    assert _error_code(forbidden) == "FORBIDDEN"

    # 跨租户 id → 404：拿工作空间 1 的单号，用工作空间 2 的账号去调
    cross = ctx.client("other-admin").post(
        f"/api/purchase-requests/{submitted['id']}/inbound-orders",
        json={"note": "跨租户"},
        headers={"Idempotency-Key": _key("inbound")},
    )
    assert cross.status_code == 404, cross.text
    assert _error_code(cross) == "PURCHASE_REQUEST_NOT_FOUND"

    # 工作空间 2 的单挂到工作空间 1 的仓库 → 404 WAREHOUSE_NOT_FOUND（同样不得写库）
    with ctx.db_mod.SessionLocal() as db:
        other_request = ctx.db_mod.PurchaseRequest(
            workspace_id=ctx.other_workspace_id, warehouse_id=ctx.warehouse_id,
            request_no=f"PR-{uuid4().hex[:12].upper()}", status="approved",
            idempotency_key=_key("other"), payload_hash=_canonical_hash({"other": True}),
            version=1,
        )
        db.add(other_request)
        db.commit()
        db.refresh(other_request)
        other_request_id = other_request.id
    cross_warehouse = ctx.client("other-admin").post(
        f"/api/purchase-requests/{other_request_id}/inbound-orders",
        json={"note": "跨租户仓库"},
        headers={"Idempotency-Key": _key("inbound")},
    )
    assert cross_warehouse.status_code == 404, cross_warehouse.text
    assert _error_code(cross_warehouse) == "WAREHOUSE_NOT_FOUND"
    with ctx.db_mod.SessionLocal() as db:
        orders = db.scalars(
            select(ctx.db_mod.InboundOrder).where(
                ctx.db_mod.InboundOrder.purchase_request_id == other_request_id,
            )
        ).all()
        assert len(orders) == 0


# ---------------------------------------------------------------------------
# A-17 响应契约
# ---------------------------------------------------------------------------


def test_a17_response_contract_keeps_legacy_keys_and_adds_seven(maker_checker_fixture):
    """A-17：detail 与 list 的既有 16 键逐字节不变 + 新增 7 键；inbound-order 两分支。"""
    ctx = maker_checker_fixture
    checker = ctx.client("checker")
    request = _create_historical_submitted(ctx)
    checker.post(
        f"/api/purchase-requests/{request['id']}/approve",
        json={"expected_version": 1},
        headers={"Idempotency-Key": _key("approve")},
    )

    detail = checker.get(f"/api/purchase-requests/{request['id']}")
    assert detail.status_code == 200, detail.text
    detail_body = detail.json()
    listing = checker.get("/api/purchase-requests")
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    item = next(row for row in items if row["id"] == request["id"])

    assert set(detail_body) == set(LEGACY_KEYS) | set(NEW_KEYS)
    assert set(item) == set(LEGACY_KEYS) | set(NEW_KEYS)
    for key in LEGACY_KEYS:
        assert detail_body[key] == item[key], key
    for key in NEW_KEYS:
        assert detail_body[key] == item[key], key
    assert detail_body["supersedes_request_id"] is None
    assert detail_body["approver_id"] == "checker"
    assert detail_body["approver_user_id"] == ctx.account("checker").user_id

    # inbound-order 两分支
    before = checker.get(f"/api/purchase-requests/{request['id']}/inbound-order")
    assert before.status_code == 200, before.text
    assert before.json() == {"inbound_order": None}

    generated = checker.post(
        f"/api/purchase-requests/{request['id']}/inbound-orders",
        json={"note": "生成"},
        headers={"Idempotency-Key": _key("inbound")},
    )
    assert generated.status_code == 201, generated.text
    after = checker.get(f"/api/purchase-requests/{request['id']}/inbound-order")
    assert after.status_code == 200, after.text
    inbound = after.json()["inbound_order"]
    assert inbound is not None
    assert inbound["reference_no"] == f"INB-{_request_no(ctx, request['id'])}"
    assert inbound["status"] == "expected"
    assert len(inbound["lines"]) == 1


# ---------------------------------------------------------------------------
# A-18 前端与静态红线
# ---------------------------------------------------------------------------


def test_a18_frontend_bridge_and_static_redlines(maker_checker_fixture):
    """A-18：/ops 含四个动作按钮；purchase-workflow 仍在；purchase_approval.py 无 DELETE / 不 import fastapi。"""
    ctx = maker_checker_fixture
    page = ctx.client("maker").get("/ops")
    assert page.status_code == 200, page.text
    html = page.text
    assert 'id="purchase-workflow"' in html
    for action in ("approve", "reject", "cancel", "create-inbound"):
        assert f'data-purchase-action="{action}"' in html, action

    source = (REPO_ROOT / "app" / "purchase_approval.py").read_text(encoding="utf-8")
    assert "DELETE" not in source
    assert ".delete(" not in source
    # 不 import fastapi：用 AST 判定（模块 docstring 里会提到这四个字，不能用裸字符串）
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "fastapi" not in imported, sorted(imported)


# ---------------------------------------------------------------------------
# A-19 Q2 终态与重办血缘
# ---------------------------------------------------------------------------


def test_a19_terminal_states_and_supersedes_lineage(maker_checker_fixture):
    """A-19：① 终态三动作皆 409；② 新建 draft 带 supersedes 落值且旧单零变化；③ 非法指向 404；④ 不传字段零变化；⑤ 改造前形态同键重放成功。"""
    ctx = maker_checker_fixture
    checker = ctx.client("checker")
    maker = ctx.client("maker")

    # ① reject 成功后 approve / reject / cancel 三者均 409 INVALID_PURCHASE_REQUEST_STATE
    # （cancel 走归属校验，所以由制单人 maker 发起；制单人 ≠ 审批人 checker）
    old = _create_historical_submitted(ctx, created_by="maker")
    rejected = checker.post(
        f"/api/purchase-requests/{old['id']}/reject",
        json={"expected_version": 1, "reason": "不批，重办"},
        headers={"Idempotency-Key": _key("reject")},
    )
    assert rejected.status_code == 200, rejected.text
    for action, payload, actor in (
        ("approve", {"expected_version": 2}, checker),
        ("reject", {"expected_version": 2, "reason": "再批一次"}, checker),
        ("cancel", {"expected_version": 2}, maker),
    ):
        response = actor.post(
            f"/api/purchase-requests/{old['id']}/{action}",
            json=payload,
            headers={"Idempotency-Key": _key(action)},
        )
        assert response.status_code == 409, (action, response.text)
        assert _error_code(response) == "INVALID_PURCHASE_REQUEST_STATE", action

    # ② 新建 draft 带 supersedes_request_id → 成功且响应落值；旧单状态/版本/审计行零变化
    old_before = _row(ctx, old["id"])
    actions_before = len(_action_rows(ctx, old["id"]))
    suggestion_id = _seed_suggestion(ctx)
    replacement = maker.post(
        "/api/purchase-requests/drafts",
        json={"suggestion_ids": [suggestion_id], "supersedes_request_id": old["id"]},
        headers={"Idempotency-Key": _key("draft")},
    )
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()["supersedes_request_id"] == old["id"]
    assert replacement.json()["status"] == "draft"
    old_after = _row(ctx, old["id"])
    assert old_after.status == "rejected"
    assert old_after.version == old_before.version
    assert len(_action_rows(ctx, old["id"])) == actions_before

    # ③ 指向不存在 id / 跨 workspace → 404 且零写入
    counts_before = _counts(ctx)
    missing = maker.post(
        "/api/purchase-requests/drafts",
        json={"suggestion_ids": [_seed_suggestion(ctx)], "supersedes_request_id": 99999999},
        headers={"Idempotency-Key": _key("draft")},
    )
    assert missing.status_code == 404, missing.text
    assert _error_code(missing) == "PURCHASE_REQUEST_NOT_FOUND"
    assert _counts(ctx) == counts_before, (counts_before, _counts(ctx))

    # ④ 不传该字段的既有 draft 创建路径行为零变化
    plain = maker.post(
        "/api/purchase-requests/drafts",
        json={"suggestion_ids": [_seed_suggestion(ctx)]},
        headers={"Idempotency-Key": _key("draft")},
    )
    assert plain.status_code == 201, plain.text
    assert plain.json()["supersedes_request_id"] is None
    assert plain.json()["status"] == "draft"
    assert plain.json()["version"] == 1

    # ⑤ 改造前形态的行（payload_hash 为既有 4 键 hash）做同键重放 → 成功，不是 409
    legacy_suggestion_id = _seed_suggestion(ctx)
    legacy_key = _key("legacy")
    legacy_hash = _canonical_hash({
        "suggestion_ids": [legacy_suggestion_id],
        "note": "改造前的载荷",
        "supplier_ref": None,
        "expected_arrival_date": None,
    })
    with ctx.db_mod.SessionLocal() as db:
        legacy = ctx.db_mod.PurchaseRequest(
            workspace_id=ctx.workspace_id, warehouse_id=ctx.warehouse_id,
            request_no=f"PR-{uuid4().hex[:12].upper()}", status="draft",
            note="改造前的载荷", created_by="system:legacy",
            idempotency_key=legacy_key, payload_hash=legacy_hash, version=1,
        )
        db.add(legacy)
        db.commit()
        db.refresh(legacy)
        legacy_id = legacy.id
    replay = maker.post(
        "/api/purchase-requests/drafts",
        json={"suggestion_ids": [legacy_suggestion_id], "note": "改造前的载荷"},
        headers={"Idempotency-Key": legacy_key},
    )
    assert replay.status_code in (200, 201), replay.text
    assert replay.json()["id"] == legacy_id
    assert "IDEMPOTENCY_KEY_REUSE" not in replay.text
    with ctx.db_mod.SessionLocal() as db:
        rows = db.scalars(
            select(ctx.db_mod.PurchaseRequest).where(
                ctx.db_mod.PurchaseRequest.workspace_id == ctx.workspace_id,
                ctx.db_mod.PurchaseRequest.idempotency_key == legacy_key,
            )
        ).all()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# A-20 Q1b 双账号与禁绕过
# ---------------------------------------------------------------------------


def test_a20_dual_account_fixture_and_no_self_approval_bypass(maker_checker_fixture):
    """A-20：双账号审批链路、自审被拒、角色权限表未被放宽、无 env/settings 开关、无自审 monkeypatch。"""
    ctx = maker_checker_fixture
    maker_account = ctx.account("maker")
    checker_account = ctx.account("checker")
    assert checker_account.user_id != maker_account.user_id
    assert maker_account.role == "operations" and checker_account.role == "operations"

    # ① maker 提交 → checker 审批 → approver_id / approver_user_id 指向 checker
    request = _create_historical_submitted(ctx, created_by="maker")
    approved = ctx.client("checker").post(
        f"/api/purchase-requests/{request['id']}/approve",
        json={"expected_version": 1},
        headers={"Idempotency-Key": _key("approve")},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["approver_id"] == "checker"
    assert approved.json()["approver_user_id"] == checker_account.user_id

    # ② maker 自审 → 409 SELF_APPROVAL_FORBIDDEN，库零变化
    own = _create_historical_submitted(ctx, created_by="maker")
    before = _counts(ctx)
    denied = ctx.client("maker").post(
        f"/api/purchase-requests/{own['id']}/approve",
        json={"expected_version": 1},
        headers={"Idempotency-Key": _key("approve")},
    )
    assert denied.status_code == 409, denied.text
    assert _error_code(denied) == "SELF_APPROVAL_FORBIDDEN"
    assert _counts(ctx) == before, (before, _counts(ctx))

    # ③ 角色权限表：operations 有 purchasing.approve，warehouse 没有，admin 仍是空集
    import app.identity as identity_mod

    assert "purchasing.approve" in identity_mod.ROLE_PERMISSIONS[identity_mod.ROLE_OPERATIONS]
    assert "purchasing.approve" not in identity_mod.ROLE_PERMISSIONS[identity_mod.ROLE_WAREHOUSE]
    assert identity_mod.ROLE_PERMISSIONS[identity_mod.ROLE_ADMIN] == frozenset()
    identity_source = (REPO_ROOT / "app" / "identity.py").read_text(encoding="utf-8")
    assert 'ROLE_ADMIN: frozenset()' in identity_source
    identity_test_source = (REPO_ROOT / "tests" / "test_identity.py").read_text(encoding="utf-8")
    assert "ROLE_PERMISSIONS[ROLE_ADMIN] == frozenset()" in identity_test_source

    # ④ 静态：SELF_APPROVAL_FORBIDDEN 命中数恒为 2（判定 + 抛错），无 env/settings 开关
    approval_source = (REPO_ROOT / "app" / "purchase_approval.py").read_text(encoding="utf-8")
    assert approval_source.count("SELF_APPROVAL_FORBIDDEN") == 2
    assert "os.environ" not in approval_source
    assert "get_settings" not in approval_source
    assert "settings" not in approval_source

    # 本测试文件自身不得出现针对自审校验的 monkeypatch / 短路
    own_source = SOURCE_FILE.read_text(encoding="utf-8")
    needle = "_check_self_" + "approval"
    assert needle not in own_source
    assert not re.search(r"setattr\([^)]*purchase_approval", own_source)
