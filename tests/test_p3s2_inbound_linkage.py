"""P3-S2 入库单关联采购单回归测试（R-1 ~ R-8）。

用例编号与 `dev_task.p3s2-inbound-linkage.md` §6 一一对应：

| 编号 | 场景 | 测试函数 |
|---|---|---|
| R-1 | 关联单正常实收 | `test_r1_linked_inbound_normal_receive_succeeds` |
| R-2 | 超收被拦（U4 主用例） | `test_r2_over_receiving_is_blocked_and_writes_nothing` |
| R-3 | 无边单不受影响 | `test_r3_unlinked_inbound_is_not_gated` |
| R-4 | 进度派生三态 | `test_r4_receipt_progress_three_states` |
| R-5 | 破损计入口径（口径 A） | `test_r5_damaged_is_subset_of_received` |
| R-6 | 幂等重放先于 U4 闸 | `test_r6_idempotent_replay_precedes_u4_gate` |
| R-7 | 来源键契约 | `test_r7_source_keys_contract` |
| R-8 | 进度端点边界 | `test_r8_receipt_progress_edge_cases` |

口径说明（读测试前先读）：

- **零 live**：全部走本地 SQLite + 真实 HTTP 路由，不触碰任何外部平台。
- **双账号夹具红线继续适用**：`operations` 账号负责制单/审批/生成入库单/查进度，
  `warehouse` 账号负责实收（`inbound.receive` 只属于 ROLE_WAREHOUSE）。两个**独立**
  `TestClient` 各自登录。本文件不含任何 monkeypatch 自审/绕过开关。
- **超收口径 A**（已裁定）：本仓库语义是 `damaged ⊆ received`（`damaged ≤ received`
  由 `DAMAGED_QTY_INVALID` 强制），因此 U4 闸**只比对 `received_qty`**，
  "received + damaged" 的旧口径会把破损算两遍，已排除。
- **R-4 三态**用三条同构构造的采购单各造一个状态：`received` 是**一次性**动作
  （成功后 status 转 `received`，不可再次实收），因此同一张单无法在同一时刻
  呈现 pending/partial/completed 三态。
"""

from __future__ import annotations

import importlib
import os
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

REPO_ROOT = Path(__file__).resolve().parents[1]

# 既有 9 键（逐字节不变）与 P3-S2 新增 2 键。
LEGACY_INBOUND_KEYS = (
    "id", "warehouse_id", "reference_no", "status", "note",
    "lines", "created_at", "received_at", "confirmed_at",
)
NEW_INBOUND_KEYS = ("purchase_request_id", "purchase_request_no")

# 历史数据构造用的"不存在的 login"（与 P3-S1 同款等价构造，不是绕过）。
FIXTURE_MAKER = "system:p3s2-fixture-maker"

REQUESTED_QTY = 10


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


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


def _key(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _error_code(response) -> str:
    return response.json()["detail"]["code"]


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
    workspace_id: int
    other_workspace_id: int
    warehouse_id: int
    sku_id: int
    accounts: dict = field(default_factory=dict)
    clients: dict = field(default_factory=dict)

    def client(self, login: str) -> TestClient:
        return self.clients[login]


@pytest.fixture
def p3s2_fixture(tmp_path, monkeypatch):
    """制单/审批（operations）+ 实收（warehouse）两个真实账号、两个独立 client。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'p3s2.db'}")
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
        workspace = db_mod.Workspace(tenant_key="p3s2", name="P3-S2 商家")
        other = db_mod.Workspace(tenant_key="p3s2-other", name="P3-S2 别家商家")
        db.add_all([workspace, other])
        db.flush()
        users = [
            ("maker", "operations"),
            ("checker", "operations"),
            ("wh", "warehouse"),
            ("reader", "readonly"),
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
            workspace_id=workspace.id, code="W-P3S2", name="P3-S2 仓", warehouse_type="own",
        )
        db.add(warehouse)
        db.flush()
        # 仓库角色需要显式的 active 仓库授权（空授权集合一律拒绝）。
        db.add(db_mod.WarehouseAccess(
            user_id=created["wh"][0].id, warehouse_id=warehouse.id, status="active",
        ))
        product = repo_mod.upsert_product(
            db,
            ProductRecord(
                source="fixture", external_product_id="p3s2-p", title="P3-S2 商品",
                url="https://fixture.local/p3s2-p", current_price=Decimal("10.00"),
                observed_at=__import__("datetime").datetime.utcnow(),
            ),
            workspace_id=workspace.id,
        )
        db.flush()
        sku = db_mod.ProductSku(workspace_id=workspace.id, product_id=product.id, sku_code="P3S2-SKU")
        db.add(sku)
        db.flush()
        db.add(db_mod.InventoryBalance(
            workspace_id=workspace.id, warehouse_id=warehouse.id, sku_id=sku.id, on_hand_qty=4,
        ))
        db.commit()
        ctx = Ctx(
            db_mod=db_mod, main_mod=main_mod, cfg=cfg,
            workspace_id=workspace.id, other_workspace_id=other.id,
            warehouse_id=warehouse.id, sku_id=sku.id,
            accounts={
                login: Account(login=login, user_id=account.id, role=role)
                for login, (account, role) in created.items()
            },
        )

    for login in ("maker", "checker", "wh", "reader", "other-admin"):
        client = TestClient(main_mod.app)
        _login(client, login)
        ctx.clients[login] = client

    yield ctx

    bg_mod.reset_executor()
    cfg.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def _seed_suggestion(ctx: Ctx, *, qty: int = REQUESTED_QTY) -> int:
    with ctx.db_mod.SessionLocal() as db:
        suggestion = ctx.db_mod.ReplenishmentSuggestion(
            workspace_id=ctx.workspace_id, warehouse_id=ctx.warehouse_id,
            sku_id=ctx.sku_id, status="confirmed", suggested_qty=qty, decision_qty=qty,
            source_hash=f"p3s2-{uuid.uuid4().hex}", source_snapshot_json="{}",
            as_of_date=date.today(), version=1, created_by="system:seed",
        )
        db.add(suggestion)
        db.commit()
        db.refresh(suggestion)
        return suggestion.id


def _create_submitted(ctx: Ctx) -> dict:
    """构造一张 submitted 采购单（制单人是"不存在的 login"，审批者可自由切换）。"""
    suggestion_id = _seed_suggestion(ctx)
    with ctx.db_mod.SessionLocal() as db:
        request = ctx.db_mod.PurchaseRequest(
            workspace_id=ctx.workspace_id, warehouse_id=ctx.warehouse_id,
            request_no=f"PR-{uuid.uuid4().hex[:12].upper()}", status="submitted",
            note="P3-S2 历史数据构造", submitted_by=FIXTURE_MAKER, created_by=FIXTURE_MAKER,
            submitted_at=__import__("datetime").datetime.utcnow(),
            idempotency_key=_key("hist"), payload_hash="sha256:" + "0" * 64,
            version=1,
        )
        db.add(request)
        db.flush()
        db.add(ctx.db_mod.PurchaseRequestLine(
            workspace_id=ctx.workspace_id, purchase_request_id=request.id,
            warehouse_id=ctx.warehouse_id, sku_id=ctx.sku_id,
            suggestion_id=suggestion_id, requested_qty=REQUESTED_QTY,
            source_suggestion_version=1,
        ))
        db.commit()
        return {"id": request.id, "version": request.version, "status": request.status}


def _approve_and_generate(ctx: Ctx, request_id: int) -> dict:
    """审批（checker）→ 生成入库单（checker）；返回入库单 id 与关联单号。"""
    checker = ctx.client("checker")
    approved = checker.post(
        f"/api/purchase-requests/{request_id}/approve",
        json={"expected_version": 1, "note": "同意"},
        headers={"Idempotency-Key": _key("approve")},
    )
    assert approved.status_code == 200, approved.text
    generated = checker.post(
        f"/api/purchase-requests/{request_id}/inbound-orders",
        json={"note": "生成入库单"},
        headers={"Idempotency-Key": _key("inbound")},
    )
    assert generated.status_code == 201, generated.text
    body = generated.json()
    assert body["purchase_request_id"] == request_id, generated.text
    with ctx.db_mod.SessionLocal() as db:
        request_no = db.get(ctx.db_mod.PurchaseRequest, request_id).request_no
    return {"inbound_id": body["id"], "request_no": request_no, "body": body}


def _receive(ctx: Ctx, inbound_id: int, *, received: int, damaged: int = 0, key: str | None = None):
    return ctx.client("wh").post(
        f"/api/inbounds/{inbound_id}/receive",
        json={"lines": [{
            "sku_id": ctx.sku_id, "received_qty": received, "damaged_qty": damaged,
        }]},
        headers={"Idempotency-Key": key or _key("recv")},
    )


def _inbound_row(ctx: Ctx, inbound_id: int):
    with ctx.db_mod.SessionLocal() as db:
        return db.get(ctx.db_mod.InboundOrder, inbound_id)


def _inbound_line_row(ctx: Ctx, inbound_id: int):
    with ctx.db_mod.SessionLocal() as db:
        return db.scalar(
            select(ctx.db_mod.InboundLine).where(
                ctx.db_mod.InboundLine.inbound_order_id == inbound_id
            )
        )


def _inventory_counts(ctx: Ctx) -> dict:
    with ctx.db_mod.SessionLocal() as db:
        return {
            "transactions": db.scalar(
                select(func.count(ctx.db_mod.InventoryTransaction.id))
            ) or 0,
            "balances": db.scalar(
                select(func.count(ctx.db_mod.InventoryBalance.id))
            ) or 0,
        }


def _balance_qty(ctx: Ctx) -> int:
    with ctx.db_mod.SessionLocal() as db:
        return db.scalar(
            select(ctx.db_mod.InventoryBalance.on_hand_qty).where(
                ctx.db_mod.InventoryBalance.warehouse_id == ctx.warehouse_id,
                ctx.db_mod.InventoryBalance.sku_id == ctx.sku_id,
            )
        )


def _create_manual_inbound(ctx: Ctx, *, expected_qty: int = REQUESTED_QTY) -> dict:
    """手工单（公开 POST /api/inbounds 不透传 purchase_request_id → 必然无边）。"""
    response = ctx.client("checker").post(
        "/api/inbounds",
        json={
            "warehouse_id": ctx.warehouse_id,
            "reference_no": f"IN-MANUAL-{uuid.uuid4().hex[:8]}",
            "lines": [{"sku_id": ctx.sku_id, "expected_qty": expected_qty}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _progress(ctx: Ctx, request_id: int):
    return ctx.client("checker").get(f"/api/purchase-requests/{request_id}/receipt-progress")


# ---------------------------------------------------------------------------
# R-1 ~ R-3 实收链路（含 U4 超收闸）
# ---------------------------------------------------------------------------


def test_r1_linked_inbound_normal_receive_succeeds(p3s2_fixture):
    """R-1：received == requested → 200；status="received"；received_at 落值。"""
    ctx = p3s2_fixture
    request = _create_submitted(ctx)
    linked = _approve_and_generate(ctx, request["id"])

    response = _receive(ctx, linked["inbound_id"], received=REQUESTED_QTY)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "received", response.text
    assert body["received_at"] is not None, response.text
    assert body["lines"][0]["received_qty"] == REQUESTED_QTY, response.text
    assert body["purchase_request_id"] == request["id"], response.text

    row = _inbound_row(ctx, linked["inbound_id"])
    assert row.status == "received"
    assert row.received_at is not None


def test_r2_over_receiving_is_blocked_and_writes_nothing(p3s2_fixture):
    """R-2：received = requested+1 → 409 OVER_RECEIVING_FORBIDDEN；零写入。"""
    ctx = p3s2_fixture
    request = _create_submitted(ctx)
    linked = _approve_and_generate(ctx, request["id"])
    counts_before = _inventory_counts(ctx)
    balance_before = _balance_qty(ctx)

    response = _receive(ctx, linked["inbound_id"], received=REQUESTED_QTY + 1)
    assert response.status_code == 409, response.text
    assert _error_code(response) == "OVER_RECEIVING_FORBIDDEN", response.text

    # 状态仍 expected，实收字段未落库（保持建单时的默认 0）。
    row = _inbound_row(ctx, linked["inbound_id"])
    assert row.status == "expected"
    assert row.received_at is None
    line = _inbound_line_row(ctx, linked["inbound_id"])
    assert line.received_qty == 0
    assert line.damaged_qty == 0

    # 库存零副作用。
    assert _inventory_counts(ctx) == counts_before
    assert _balance_qty(ctx) == balance_before

    # 超收被拒后仍可用合法数量实收（闸不锁死单据）。
    ok = _receive(ctx, linked["inbound_id"], received=REQUESTED_QTY)
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "received"


def test_r3_unlinked_inbound_is_not_gated(p3s2_fixture):
    """R-3：手工单（无边）实收超 expected → 200（现状回归，逐字节旧行为）。"""
    ctx = p3s2_fixture
    manual = _create_manual_inbound(ctx, expected_qty=5)
    assert manual["purchase_request_id"] is None

    response = _receive(ctx, manual["id"], received=8, damaged=1)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "received", response.text
    assert body["lines"][0]["received_qty"] == 8, response.text
    assert body["lines"][0]["damaged_qty"] == 1, response.text
    assert body["purchase_request_id"] is None
    assert body["purchase_request_no"] is None


# ---------------------------------------------------------------------------
# R-4 ~ R-6 进度与幂等
# ---------------------------------------------------------------------------


def test_r4_receipt_progress_three_states(p3s2_fixture):
    """R-4：pending（未报）→ partial（短收）→ completed（足额），数值与 D3 公式一致。"""
    ctx = p3s2_fixture

    # pending：已生成入库单但尚未报实收（expected 单不计入 received）。
    pending_request = _create_submitted(ctx)
    pending_linked = _approve_and_generate(ctx, pending_request["id"])
    pending = _progress(ctx, pending_request["id"])
    assert pending.status_code == 200, pending.text
    pending_body = pending.json()
    assert pending_body["progress_status"] == "pending", pending.text
    assert pending_body["totals"] == {
        "requested_qty": REQUESTED_QTY, "received_qty": 0, "damaged_qty": 0,
        "accepted_qty": 0, "remaining_qty": REQUESTED_QTY,
    }, pending.text
    assert pending_body["lines"][0]["received_qty"] is None, pending.text
    assert pending_body["lines"][0]["remaining_qty"] == REQUESTED_QTY, pending.text
    assert pending_body["inbound_order_id"] == pending_linked["inbound_id"], pending.text
    assert pending_body["inbound_status"] == "expected", pending.text

    # partial：短收 4 件（其中 1 件破损）。
    partial_request = _create_submitted(ctx)
    partial_linked = _approve_and_generate(ctx, partial_request["id"])
    assert _receive(ctx, partial_linked["inbound_id"], received=4, damaged=1).status_code == 200
    partial = _progress(ctx, partial_request["id"])
    assert partial.status_code == 200, partial.text
    partial_body = partial.json()
    assert partial_body["progress_status"] == "partial", partial.text
    assert partial_body["totals"] == {
        "requested_qty": REQUESTED_QTY, "received_qty": 4, "damaged_qty": 1,
        "accepted_qty": 3, "remaining_qty": REQUESTED_QTY - 4,
    }, partial.text
    assert partial_body["lines"][0]["accepted_qty"] == 3, partial.text
    assert partial_body["lines"][0]["remaining_qty"] == REQUESTED_QTY - 4, partial.text

    # completed：足额 10 件。
    completed_request = _create_submitted(ctx)
    completed_linked = _approve_and_generate(ctx, completed_request["id"])
    assert _receive(ctx, completed_linked["inbound_id"], received=REQUESTED_QTY).status_code == 200
    completed = _progress(ctx, completed_request["id"])
    assert completed.status_code == 200, completed.text
    completed_body = completed.json()
    assert completed_body["progress_status"] == "completed", completed.text
    assert completed_body["totals"]["remaining_qty"] == 0, completed.text
    assert completed_body["inbound_status"] == "received", completed.text


def test_r5_damaged_is_subset_of_received(p3s2_fixture):
    """R-5：received == requested 且 damaged > 0 → 200（口径 A：破损是 received 的子集）。"""
    ctx = p3s2_fixture
    request = _create_submitted(ctx)
    linked = _approve_and_generate(ctx, request["id"])

    response = _receive(ctx, linked["inbound_id"], received=REQUESTED_QTY, damaged=3)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "received", response.text
    assert body["lines"][0]["received_qty"] == REQUESTED_QTY, response.text
    assert body["lines"][0]["damaged_qty"] == 3, response.text

    progress = _progress(ctx, request["id"])
    assert progress.status_code == 200, progress.text
    assert progress.json()["progress_status"] == "completed", progress.text
    assert progress.json()["totals"]["accepted_qty"] == REQUESTED_QTY - 3, progress.text


def test_r6_idempotent_replay_precedes_u4_gate(p3s2_fixture):
    """R-6：合法实收后同 key+hash 重放 → 200 原样；不同 key → 409 INBOUND_ALREADY_RECEIVED。"""
    ctx = p3s2_fixture
    request = _create_submitted(ctx)
    linked = _approve_and_generate(ctx, request["id"])
    key = _key("recv")

    first = _receive(ctx, linked["inbound_id"], received=REQUESTED_QTY, key=key)
    assert first.status_code == 200, first.text

    # 同 key 同载荷 → 同 payload_hash → 幂等原样返回。
    replay = _receive(ctx, linked["inbound_id"], received=REQUESTED_QTY, key=key)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()

    # 同 key 换载荷（且这次是超收）→ 命中幂等重放闸而非 U4 闸：
    # 若 U4 闸排在幂等闸之前，这里会是 OVER_RECEIVING_FORBIDDEN。
    reuse = _receive(ctx, linked["inbound_id"], received=REQUESTED_QTY + 5, key=key)
    assert reuse.status_code == 409, reuse.text
    assert _error_code(reuse) == "INBOUND_ALREADY_RECEIVED", reuse.text

    # 换 key → 已实收 → 409 INBOUND_ALREADY_RECEIVED。
    other = _receive(ctx, linked["inbound_id"], received=REQUESTED_QTY, key=_key("recv"))
    assert other.status_code == 409, other.text
    assert _error_code(other) == "INBOUND_ALREADY_RECEIVED", other.text


# ---------------------------------------------------------------------------
# R-7 ~ R-8 响应契约与端点边界
# ---------------------------------------------------------------------------


def test_r7_source_keys_contract(p3s2_fixture):
    """R-7：关联单含两个来源键；无边单两键为 null；既有 9 键逐字节不变。"""
    ctx = p3s2_fixture
    request = _create_submitted(ctx)
    linked = _approve_and_generate(ctx, request["id"])

    detail = ctx.client("checker").get(f"/api/inbounds/{linked['inbound_id']}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert set(body) == set(LEGACY_INBOUND_KEYS) | set(NEW_INBOUND_KEYS), sorted(body)
    assert body["purchase_request_id"] == request["id"], detail.text
    assert body["purchase_request_no"] == linked["request_no"], detail.text

    # 既有 9 键的取值与库里事实一致（未被本片改写）。
    row = _inbound_row(ctx, linked["inbound_id"])
    assert body["id"] == row.id
    assert body["warehouse_id"] == row.warehouse_id
    assert body["reference_no"] == row.reference_no
    assert body["status"] == row.status
    assert body["note"] == row.note
    assert body["created_at"] == row.created_at.isoformat()
    assert body["received_at"] is None
    assert body["confirmed_at"] is None
    line = _inbound_line_row(ctx, linked["inbound_id"])
    assert body["lines"][0]["sku_id"] == line.sku_id
    assert body["lines"][0]["expected_qty"] == line.expected_qty
    assert body["lines"][0]["received_qty"] is None  # expected 态仍旧为 null

    # 无边单：两键都是 null，既有键同样成立。
    manual = _create_manual_inbound(ctx)
    manual_detail = ctx.client("checker").get(f"/api/inbounds/{manual['id']}")
    assert manual_detail.status_code == 200, manual_detail.text
    manual_body = manual_detail.json()
    assert set(manual_body) == set(LEGACY_INBOUND_KEYS) | set(NEW_INBOUND_KEYS)
    assert manual_body["purchase_request_id"] is None
    assert manual_body["purchase_request_no"] is None

    # 列表接口不动（无来源键）。
    listing = ctx.client("checker").get("/api/inbounds")
    assert listing.status_code == 200, listing.text
    item = next(row for row in listing.json()["items"] if row["id"] == linked["inbound_id"])
    assert set(item) == {
        "id", "warehouse_id", "reference_no", "status", "created_at",
        "received_at", "confirmed_at",
    }, sorted(item)


def test_r8_receipt_progress_edge_cases(p3s2_fixture):
    """R-8：① PR 无明细 → 200 空数组 + totals 全 0 + pending；② 不存在 → 404；③ expected 单不计入。"""
    ctx = p3s2_fixture

    # ① PR 无明细（draft 态，历史数据构造）。
    with ctx.db_mod.SessionLocal() as db:
        empty_request = ctx.db_mod.PurchaseRequest(
            workspace_id=ctx.workspace_id, warehouse_id=ctx.warehouse_id,
            request_no=f"PR-{uuid.uuid4().hex[:12].upper()}", status="draft",
            created_by=FIXTURE_MAKER,
            idempotency_key=_key("empty"), payload_hash="sha256:" + "1" * 64,
            version=1,
        )
        db.add(empty_request)
        db.commit()
        db.refresh(empty_request)
        empty_request_id = empty_request.id
    empty = _progress(ctx, empty_request_id)
    assert empty.status_code == 200, empty.text
    empty_body = empty.json()
    assert empty_body["lines"] == [], empty.text
    assert empty_body["totals"] == {
        "requested_qty": 0, "received_qty": 0, "damaged_qty": 0,
        "accepted_qty": 0, "remaining_qty": 0,
    }, empty.text
    assert empty_body["progress_status"] == "pending", empty.text
    assert empty_body["inbound_order_id"] is None, empty.text

    # ② 不存在的 PR → 404 PURCHASE_REQUEST_NOT_FOUND。
    missing = _progress(ctx, 99999999)
    assert missing.status_code == 404, missing.text
    assert _error_code(missing) == "PURCHASE_REQUEST_NOT_FOUND", missing.text

    # ③ expected 态入库单不计入 received（进度仍 pending，实收量为 0）。
    request = _create_submitted(ctx)
    linked = _approve_and_generate(ctx, request["id"])
    progress = _progress(ctx, request["id"])
    assert progress.status_code == 200, progress.text
    body = progress.json()
    assert body["inbound_status"] == "expected", progress.text
    assert body["totals"]["received_qty"] == 0, progress.text
    assert body["progress_status"] == "pending", progress.text
    assert body["lines"][0]["received_qty"] is None, progress.text
    assert body["lines"][0]["accepted_qty"] is None, progress.text

    # 跨租户 → 404（工作空间范围校验复用 S1 的定位逻辑）。
    cross = ctx.client("other-admin").get(
        f"/api/purchase-requests/{request['id']}/receipt-progress"
    )
    assert cross.status_code == 404, cross.text
    assert _error_code(cross) == "PURCHASE_REQUEST_NOT_FOUND", cross.text
