"""P7-S2 成本锚定与回填测试：F-11..F-16。

编号口径按用户 2026-11-04 裁定重排（v1.1 里 F-11 是 as-of，此处让位于验收红线）：

- F-11 锚定固化（★核心红线）：锚定后再插新快照 → 订单行 unit_cost 与利润输入**完全不变**
- F-12 重建继承（★必踩坑）：同一订单二次 ingest → 锚定五件套原样保留，不退回 missing
- F-13 as-of 时间对齐：T-10/T-2/未来 三条快照，命中 T-10
- F-14 回填：dry_run 零写库 / 只补 missing / 重复幂等 / resolved 不动 / 日志可捕获
- F-15 归因分离：unmapped 与 missing 分开，前者不计入成本缺口
- F-16 查询预算：一批 N 行只发 1 条 cost_snapshots 查询（红线 R-2）
"""
from __future__ import annotations

import importlib
import json
import logging
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

SALE_TIME = "2026-09-10T10:00:00Z"


class Ctx:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - 直通
        self.records.append(record)


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'p7s2.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("PROFIT_PLATFORM_FEE_RATE", "0.05")
    monkeypatch.setenv("PROFIT_PLATFORM_FEE_OVERRIDES", "")

    import app.config as cfg
    import app.costs as costs_mod
    import app.db as db_mod
    import app.external_orders as ext_mod
    import app.main as main_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    # external_orders / costs 必须先于 main 重载：它们 `from app.db import ...` 绑定的
    # 是模块对象，不重载就会抓住上一个 db 模块的模型类与 engine（两套 metadata 互不可见）。
    importlib.reload(ext_mod)
    importlib.reload(costs_mod)
    importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)

    from app.employee_auth import hash_password

    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="p7s2", name="P7-S2 锚定测试")
        db.add(workspace)
        db.flush()
        admin = db_mod.UserAccount(
            login="admin", password_hash=hash_password("admin-pw"), display_name="管理员"
        )
        viewer = db_mod.UserAccount(
            login="viewer", password_hash=hash_password("viewer-pw"), display_name="只读"
        )
        db.add_all([admin, viewer])
        db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=admin.id, role="admin"))
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=viewer.id, role="readonly"))
        product = db_mod.Product(
            workspace_id=workspace.id, source="fixture", external_product_id="p7s2-p",
            title="P7-S2 商品", url="https://fixture.local/p7s2-p", current_price=Decimal("30.00"),
        )
        db.add(product)
        db.flush()
        sku_a = db_mod.ProductSku(workspace_id=workspace.id, product_id=product.id, sku_code="SKU-A")
        sku_b = db_mod.ProductSku(workspace_id=workspace.id, product_id=product.id, sku_code="SKU-B")
        db.add_all([sku_a, sku_b])
        db.flush()
        account = ext_mod.ensure_external_account(
            db, workspace_id=workspace.id, platform="jd", account_ref="account-a", store_ref="store-a",
        )
        db.add_all([
            db_mod.ExternalProductMapping(
                workspace_id=workspace.id, external_account_id=account.id,
                external_sku="EXT-SKU-A", internal_sku_id=sku_a.id, mapping_status="mapped",
            ),
            db_mod.ExternalProductMapping(
                workspace_id=workspace.id, external_account_id=account.id,
                external_sku="EXT-SKU-B", internal_sku_id=sku_b.id, mapping_status="mapped",
            ),
            # EXT-SKU-C 刻意不给映射 → 走 unmapped 分支
        ])
        db.commit()
        ids = {
            "workspace_id": workspace.id, "account_id": account.id,
            "sku_a": sku_a.id, "sku_b": sku_b.id,
        }

    clients: dict[str, TestClient] = {}
    for login, password in (("admin", "admin-pw"), ("viewer", "viewer-pw")):
        client = TestClient(main_mod.app)
        response = client.post(
            "/login", data={"login": login, "password": password, "next": "/"}, follow_redirects=False,
        )
        assert response.status_code == 303
        client.headers.update({"X-CSRF-Token": client.cookies.get("dianshang_csrf")})
        clients[login] = client

    yield Ctx(
        db_mod=db_mod, main_mod=main_mod, ext_mod=ext_mod, cfg=cfg,
        clients=clients, **ids,
    )
    cfg.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 夹具辅助
# ---------------------------------------------------------------------------


def _order(
    *,
    order_no: str = "ORD-1",
    lines: list[dict] | None = None,
    created: str = SALE_TIME,
    updated: str = "2026-09-10T10:01:00Z",
    version: int | None = 1,
    status: str = "paid",
):
    if lines is None:
        lines = [{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 2, "gross": "100.00"}]
    row = {
        "account_ref": "account-a", "store_ref": "store-a",
        "external_order_no": order_no, "status": status,
        "created_at": created, "updated_at": updated,
        "lines": [
            {
                "external_line_id": item["external_line_id"],
                "external_sku": item["external_sku"],
                "ordered_qty": item.get("qty", 1),
                "cancelled_qty": item.get("cancelled", 0),
                "refunded_qty": item.get("refunded", 0),
                "gross_amount": item.get("gross", "50.00"),
                "refund_amount": item.get("refund", "0.00"),
            }
            for item in lines
        ],
    }
    if version is not None:
        row["event_version"] = version
    from app.connectors import load_orders

    return load_orders(json.dumps({"records": [row]}), platform="jd")[0]


def _snapshot(ctx, sku_id: int, cost: str, when: str, ref: str) -> int:
    with ctx.db_mod.SessionLocal() as db:
        snapshot = ctx.db_mod.CostSnapshot(
            workspace_id=ctx.workspace_id, sku_id=sku_id,
            unit_cost=Decimal(cost), effective_time=datetime.fromisoformat(when),
            source="manual", source_ref=ref, currency="CNY",
        )
        db.add(snapshot)
        db.commit()
        return snapshot.id


def _ingest(ctx, *records):
    with ctx.db_mod.SessionLocal() as db:
        stats = ctx.ext_mod.ingest_orders(db, list(records), workspace_id=ctx.workspace_id)
    return stats


def _lines(ctx, order_no: str) -> list[dict]:
    with ctx.db_mod.SessionLocal() as db:
        order = db.scalar(select(ctx.db_mod.ExternalOrder).where(
            ctx.db_mod.ExternalOrder.workspace_id == ctx.workspace_id,
            ctx.db_mod.ExternalOrder.external_order_no == order_no,
        ))
        assert order is not None, f"订单 {order_no} 不存在"
        rows = db.scalars(select(ctx.db_mod.ExternalOrderLine).where(
            ctx.db_mod.ExternalOrderLine.external_order_id == order.id,
        ).order_by(ctx.db_mod.ExternalOrderLine.external_line_id)).all()
        return [
            {
                "line_id": row.external_line_id,
                "sku": row.external_sku,
                "internal_sku_id": row.internal_sku_id,
                "cost_snapshot_id": row.cost_snapshot_id,
                "unit_cost": row.unit_cost,
                "fee_rate": row.fee_rate,
                "cost_status": row.cost_status,
                "anchored_at": row.anchored_at,
                "qty": row.ordered_qty,
            }
            for row in rows
        ]


def _backfill(ctx, payload: dict, login: str = "admin"):
    return ctx.clients[login].post("/api/finance/cost-backfill", json=payload)


# ---------------------------------------------------------------------------
# F-13 as-of 时间对齐（先跑，给后面几条打底）
# ---------------------------------------------------------------------------


def test_f13_as_of_picks_latest_snapshot_not_after_sale(ctx):
    """T-10天 / T-2天 / 未来 三条快照，销售在中间 → 必须命中 T-10 那条。

    「未来」那条虽然更贵更近，但它是销售之后才进货的成本，不能用于这笔销售。
    """
    _snapshot(ctx, ctx.sku_a, "10.00", "2026-08-31T00:00:00", "t-10")
    _snapshot(ctx, ctx.sku_a, "20.00", "2026-09-12T00:00:00", "t+2")     # 销售之后才进货
    _snapshot(ctx, ctx.sku_a, "30.00", "2026-09-20T00:00:00", "future")  # 更晚

    _ingest(ctx, _order(order_no="ORD-ASOF"))
    line = _lines(ctx, "ORD-ASOF")[0]
    assert line["cost_status"] == "resolved"
    assert line["unit_cost"] == Decimal("10.00")
    assert line["fee_rate"] == Decimal("0.0500")
    assert line["anchored_at"] is not None


# ---------------------------------------------------------------------------
# F-11 锚定固化（核心红线）
# ---------------------------------------------------------------------------


def test_f11_anchored_cost_never_drifts_when_new_snapshot_lands(ctx):
    """★核心红线：锚定之后再进货（插入新快照），该订单的成本与利润输入必须完全不变。

    这是「历史利润不因今天的进货而改变」的最小可验证形式——
    利润 = 实付 − 单价×数量 − 费率×实付，单价与费率不变则利润不变。
    """
    _snapshot(ctx, ctx.sku_a, "10.00", "2026-08-31T00:00:00", "before")
    _ingest(ctx, _order(order_no="ORD-FREEZE"))
    before = _lines(ctx, "ORD-FREEZE")[0]
    assert before["unit_cost"] == Decimal("10.00")

    # 销售之后补了一笔更便宜（也更贵）的进货——绝不能反向影响这笔历史订单
    _snapshot(ctx, ctx.sku_a, "99.00", "2026-09-12T00:00:00", "after")
    after_new_snapshot = _lines(ctx, "ORD-FREEZE")[0]

    assert after_new_snapshot["unit_cost"] == before["unit_cost"]
    assert after_new_snapshot["cost_snapshot_id"] == before["cost_snapshot_id"]
    assert after_new_snapshot["fee_rate"] == before["fee_rate"]
    assert after_new_snapshot["anchored_at"] == before["anchored_at"]
    assert after_new_snapshot["cost_status"] == "resolved"


# ---------------------------------------------------------------------------
# F-12 重建继承（必踩坑）
# ---------------------------------------------------------------------------


def test_f12_reingest_keeps_anchor_instead_of_refetching(ctx):
    """★必踩坑：`ingest_orders` 更新订单时会 DELETE 订单行再重建。

    若重建不继承锚定值，同一订单每被平台更新一次（改价/改状态/补退款）成本就丢一次——
    现象是「上线当天正常，几天后订单神秘变 Unknown」，极难复现。
    """
    _snapshot(ctx, ctx.sku_a, "10.00", "2026-08-31T00:00:00", "v1-cost")
    _ingest(ctx, _order(order_no="ORD-REBUILD", lines=[
        {"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 2, "gross": "100.00"},
    ]))
    first = _lines(ctx, "ORD-REBUILD")
    assert first[0]["cost_status"] == "resolved"
    assert first[0]["unit_cost"] == Decimal("10.00")

    # 关键：在两次 ingest 之间补一笔"今天的成本"。如果没有继承，重建会重新 as-of 抓到 99。
    _snapshot(ctx, ctx.sku_a, "99.00", "2026-09-11T00:00:00", "v2-cost")

    stats = _ingest(ctx, _order(
        order_no="ORD-REBUILD",
        lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 2, "gross": "88.00"}],
        updated="2026-09-12T10:00:00Z", version=2,
    ))
    assert stats.updated == 1
    rebuilt = _lines(ctx, "ORD-REBUILD")
    assert len(rebuilt) == 1, "更新不该产生重复行"
    assert rebuilt[0]["cost_status"] == "resolved", "锚定状态不许退回 missing"
    assert rebuilt[0]["unit_cost"] == Decimal("10.00"), "重建后必须继承旧锚定，不得按新快照重抓"
    assert rebuilt[0]["cost_snapshot_id"] == first[0]["cost_snapshot_id"]
    assert rebuilt[0]["fee_rate"] == first[0]["fee_rate"]
    assert rebuilt[0]["anchored_at"] == first[0]["anchored_at"]


def test_f12b_missing_status_also_survives_reingest(ctx):
    """首次锚定就没成本（missing）的行，重建时同样继承 missing，不因事后补成本而自动变 resolved。

    自动变 resolved 等于静默改写历史利润；要补只能走显式回填（F-14）。
    """
    _ingest(ctx, _order(order_no="ORD-MISSING-REBUILD"))
    assert _lines(ctx, "ORD-MISSING-REBUILD")[0]["cost_status"] == "missing"

    _snapshot(ctx, ctx.sku_a, "12.00", "2026-09-11T00:00:00", "late-cost")
    _ingest(ctx, _order(
        order_no="ORD-MISSING-REBUILD",
        lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 1, "gross": "50.00"}],
        updated="2026-09-12T10:00:00Z", version=2,
    ))
    rebuilt = _lines(ctx, "ORD-MISSING-REBUILD")[0]
    assert rebuilt["cost_status"] == "missing"
    assert rebuilt["unit_cost"] is None
    assert rebuilt["fee_rate"] is not None, "费率仍应保留（首次锚定已固化）"


# ---------------------------------------------------------------------------
# F-15 unmapped / missing 归因分离
# ---------------------------------------------------------------------------


def test_f15_unmapped_is_distinct_from_missing(ctx):
    """没有内部 SKU 映射 ≠ 缺成本。前者是映射问题，不该被算进「缺成本」的黄色提示。"""
    _ingest(ctx, _order(order_no="ORD-SPLIT", lines=[
        {"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 1, "gross": "50.00"},
        {"external_line_id": "L-2", "external_sku": "EXT-SKU-C", "qty": 1, "gross": "50.00"},
    ]))
    lines = {item["line_id"]: item for item in _lines(ctx, "ORD-SPLIT")}
    assert lines["L-1"]["cost_status"] == "missing"
    assert lines["L-1"]["internal_sku_id"] == ctx.sku_a
    assert lines["L-2"]["cost_status"] == "unmapped"
    assert lines["L-2"]["internal_sku_id"] is None
    assert lines["L-2"]["unit_cost"] is None


# ---------------------------------------------------------------------------
# F-14 回填
# ---------------------------------------------------------------------------


def test_f14_backfill_dry_run_then_apply_then_idempotent(ctx):
    """回填四件事一次验完：dry_run 零写库 / 只补 missing / resolved 不动 / 重复幂等 + 日志。"""
    # 一笔"先销售后成本"的订单：销售时无任何快照 → missing
    _ingest(ctx, _order(order_no="ORD-BACKFILL"))
    assert _lines(ctx, "ORD-BACKFILL")[0]["cost_status"] == "missing"

    # 另造一笔「销售前就有成本」的订单 → resolved，回填绝不能碰它
    _snapshot(ctx, ctx.sku_a, "15.00", "2026-09-01T00:00:00", "pre-sale")
    _ingest(ctx, _order(
        order_no="ORD-RESOLVED", created="2026-09-05T10:00:00Z",
        lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 1, "gross": "60.00"}],
        updated="2026-09-05T10:01:00Z",
    ))
    resolved_before = _lines(ctx, "ORD-RESOLVED")[0]
    assert resolved_before["cost_status"] == "resolved"
    assert resolved_before["unit_cost"] == Decimal("15.00")

    # 事后补货：09-20 的成本，晚于两笔销售
    later_snapshot = _snapshot(ctx, ctx.sku_a, "18.00", "2026-09-20T00:00:00", "late")

    handler = _ListHandler()
    ctx.main_mod.logger.addHandler(handler)
    ctx.main_mod.logger.setLevel(logging.INFO)
    try:
        # 1) dry_run 默认 true → 零写库
        dry = _backfill(ctx, {})
        assert dry.status_code == 200, dry.text
        body = dry.json()
        assert body["dry_run"] is True
        assert body["scanned"] == 1, "只应扫到 missing 那一笔"
        assert body["updated"] == 1
        assert body["skipped"] == 0
        assert body["samples"][0]["unit_cost"] == "18.00"
        assert _lines(ctx, "ORD-BACKFILL")[0]["cost_status"] == "missing", "dry_run 不许写库"

        # 2) 显式 dry_run=false → 落库，状态 backfilled（与 resolved 区分）
        applied = _backfill(ctx, {"dry_run": False})
        assert applied.status_code == 200, applied.text
        assert applied.json()["updated"] == 1
        filled = _lines(ctx, "ORD-BACKFILL")[0]
        assert filled["cost_status"] == "backfilled"
        assert filled["unit_cost"] == Decimal("18.00")
        assert filled["cost_snapshot_id"] == later_snapshot

        # 3) 重复回填幂等：不再是 missing，扫不到
        again = _backfill(ctx, {"dry_run": False})
        assert again.json()["scanned"] == 0
        assert again.json()["updated"] == 0

        # 4) resolved 行全程未被改写
        resolved_after = _lines(ctx, "ORD-RESOLVED")[0]
        assert resolved_after["cost_status"] == "resolved"
        assert resolved_after["unit_cost"] == resolved_before["unit_cost"]
        assert resolved_after["cost_snapshot_id"] == resolved_before["cost_snapshot_id"]
        assert resolved_after["anchored_at"] == resolved_before["anchored_at"]
    finally:
        ctx.main_mod.logger.removeHandler(handler)

    messages = [record.getMessage() for record in handler.records]
    assert any("利润成本回填" in message and "dry_run=True" in message for message in messages)
    assert any("利润成本回填" in message and "dry_run=False" in message for message in messages)


def test_f14b_backfill_requires_write_permission(ctx):
    assert _backfill(ctx, {}, login="viewer").status_code == 403


def test_f14c_backfill_without_candidate_stays_missing(ctx):
    """补了成本也补不到（时间上完全没有候选）→ 保持 missing，不动。"""
    _ingest(ctx, _order(order_no="ORD-NOCAND"))
    _snapshot(ctx, ctx.sku_a, "9.00", "2026-10-01T00:00:00", "way-later")
    # next_after_sale 会命中 10-01 那条（晚于销售即候选），故这里用 nearest 亦命中。
    # 真正的"无候选"用 sku_ids 过滤到一个没有快照的 SKU 来构造。
    body = _backfill(ctx, {"dry_run": False, "sku_ids": [ctx.sku_b]}).json()
    assert body["scanned"] == 0, "sku_b 上没有 missing 行，不该被扫到"
    assert _lines(ctx, "ORD-NOCAND")[0]["cost_status"] == "missing"


# ---------------------------------------------------------------------------
# F-16 同步路径查询预算（红线 R-2）
# ---------------------------------------------------------------------------


def test_f16_ingest_issues_at_most_one_snapshot_query(ctx):
    """一批多行只发 1 条 cost_snapshots 查询；已锚定行根本不查成本表。"""
    from sqlalchemy import event as sa_event

    _snapshot(ctx, ctx.sku_a, "10.00", "2026-08-01T00:00:00", "a")
    _snapshot(ctx, ctx.sku_b, "20.00", "2026-08-01T00:00:00", "b")

    statements: list[str] = []

    def _capture(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
        statements.append(statement)

    sa_event.listen(ctx.db_mod.engine, "before_cursor_execute", _capture)
    try:
        _ingest(
            ctx,
            _order(order_no="ORD-BUDGET-1", lines=[
                {"external_line_id": "L-1", "external_sku": "EXT-SKU-A"},
                {"external_line_id": "L-2", "external_sku": "EXT-SKU-B"},
            ]),
            _order(order_no="ORD-BUDGET-2", lines=[
                {"external_line_id": "L-1", "external_sku": "EXT-SKU-A"},
            ]),
        )
    finally:
        sa_event.remove(ctx.db_mod.engine, "before_cursor_execute", _capture)

    snapshot_queries = [sql for sql in statements if "cost_snapshots" in sql.lower()]
    assert len(snapshot_queries) == 1, f"期望 1 条快照查询，实际 {len(snapshot_queries)}"

    # 已锚定的行重建时不得查成本表
    statements.clear()
    sa_event.listen(ctx.db_mod.engine, "before_cursor_execute", _capture)
    try:
        _ingest(ctx, _order(
            order_no="ORD-BUDGET-1",
            lines=[
                {"external_line_id": "L-1", "external_sku": "EXT-SKU-A"},
                {"external_line_id": "L-2", "external_sku": "EXT-SKU-B"},
            ],
            updated="2026-09-12T10:00:00Z", version=2,
        ))
    finally:
        sa_event.remove(ctx.db_mod.engine, "before_cursor_execute", _capture)
    assert not [sql for sql in statements if "cost_snapshots" in sql.lower()], (
        "已锚定行继承了旧值，就不该再查成本表"
    )


# ---------------------------------------------------------------------------
# 附带：费率锚定不随配置漂移
# ---------------------------------------------------------------------------


def test_fee_rate_anchored_does_not_follow_config_change(ctx, monkeypatch):
    """改了全局费率，已锚定行不受影响；新订单才用新费率（F-18 的最小前置）。"""
    _snapshot(ctx, ctx.sku_a, "10.00", "2026-08-01T00:00:00", "a")
    _ingest(ctx, _order(order_no="ORD-FEE-OLD"))
    old = _lines(ctx, "ORD-FEE-OLD")[0]
    assert old["fee_rate"] == Decimal("0.0500")

    monkeypatch.setenv("PROFIT_PLATFORM_FEE_RATE", "0.08")
    ctx.cfg.get_settings.cache_clear()

    assert _lines(ctx, "ORD-FEE-OLD")[0]["fee_rate"] == Decimal("0.0500"), "历史行费率必须冻结"

    _ingest(ctx, _order(
        order_no="ORD-FEE-NEW", created="2026-09-12T10:00:00Z",
        updated="2026-09-12T10:01:00Z",
        lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 1, "gross": "50.00"}],
    ))
    assert _lines(ctx, "ORD-FEE-NEW")[0]["fee_rate"] == Decimal("0.0800")
