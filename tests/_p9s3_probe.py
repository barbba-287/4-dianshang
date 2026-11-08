# -*- coding: utf-8 -*-
"""P9-S3 探针与对拍基建（`dev_task.p9s3-order-upsert.md` §4 A-4、§5）。

**下划线开头 → pytest 不收集本文件**（只被 `tests/test_p9s3_order_upsert.py` 显式 import）。
探针**不进产品代码**，只活在 tests 下（§5.2 第 4 条）。

三条硬要求（§5.2）：

1. **两个独立库/会话**：legacy 与 new 各跑一次必须互不污染 → 这里起两个 SQLite 文件，
   各自建一份**完全相同**的 fixture 数据（插入顺序相同 → 主键、成本快照 id 也相同，
   `cost_snapshot_id` 才具备可比性）。
2. **快照必须是全字段**：`ExternalOrderLine` 的 12 个字段一个都不能漏
   （只比金额会漏掉 R-68 本身）。
3. **`external_line_id` 绝不做字面比对**：两侧都归一化成 `(external_sku, 同 SKU 内序号)`
   再比，并强制断言**双射**（集合大小相等 + 一一对应）。
   归一化用的是 `app.order_line_identity.legacy_seq_map` —— 与 A-3 的旧 seq 解析
   **同一个函数**（不许各写一份）。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import (
    Base,
    CostSnapshot,
    DailySkuSale,
    ExternalOrder,
    ExternalOrderLine,
    ExternalProductMapping,
    Product,
    ProductSku,
    Workspace,
)
from app.order_line_identity import legacy_seq_map

# 快照字段口径（§5.2 第 2 条）：一个都不能少。
ORDER_FIELDS = (
    "external_order_no",
    "platform",
    "account_ref",
    "store_ref",
    "order_status",
    "external_created_at",
    "paid_at",
    "external_updated_at",
    "event_version",
    "gross_amount",
    "refund_amount",
    "currency",
    "payload_hash",
    "idempotency_key",
    "source_mode",
    "simulated",
    "data_completeness",
    "status_reason",
    "payload_json",
    "sync_run_id",
)

LINE_FIELDS = (
    "external_sku",
    "ordered_qty",
    "cancelled_qty",
    "refunded_qty",
    "gross_amount",
    "refund_amount",
    "cost_status",
    "cost_snapshot_id",
    "unit_cost",
    "fee_rate",
)

DAILY_FIELDS = (
    "platform",
    "account_ref",
    "store_ref",
    "external_sku",
    "internal_sku_id",
    "sales_date",
    "gross_qty",
    "cancelled_qty",
    "refunded_qty",
    "net_qty",
    "gross_amount",
    "refund_amount",
    "net_amount",
    "order_count",
    "data_completeness",
)

STATS_FIELDS = (
    "total",
    "inserted",
    "updated",
    "no_op",
    "conflict",
    "stale",
    "merged_orders",
    "merged_rows",
)


def _plain(value):
    """把 Decimal / datetime 变成可比较、可打印的标量。"""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


# ---------------------------------------------------------------------------
# 建库与 fixture
# ---------------------------------------------------------------------------

def create_probe_db(path, *, tenant_key: str, skus, costs):
    """建一个独立 SQLite 库并灌入 fixture。返回 (engine, sessionmaker, ids)。

    `costs`: {sku_code: (unit_cost_str, effective_time)}
    """
    engine = create_engine("sqlite:///%s" % path)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    from app.external_orders import ensure_external_account

    db = session_factory()
    try:
        workspace = Workspace(tenant_key=tenant_key, name="P9-S3 探针工作区")
        db.add(workspace)
        db.flush()
        product = Product(
            workspace_id=workspace.id,
            source="fixture",
            external_product_id="%s-p" % tenant_key,
            title="P9-S3 探针商品",
            url="https://fixture.local/%s" % tenant_key,
            current_price=Decimal("40.00"),
        )
        db.add(product)
        db.flush()
        sku_ids = {}
        for code in skus:
            sku = ProductSku(
                workspace_id=workspace.id,
                product_id=product.id,
                sku_code="SKU-%s" % code,
            )
            db.add(sku)
            db.flush()
            sku_ids[code] = sku.id
        external = ensure_external_account(
            db,
            workspace_id=workspace.id,
            platform="taobao",
            account_ref="account-a",
            store_ref="store-a",
        )
        for code in skus:
            db.add(ExternalProductMapping(
                workspace_id=workspace.id,
                external_account_id=external.id,
                external_sku="EXT-SKU-%s" % code,
                internal_sku_id=sku_ids[code],
                mapping_status="mapped",
            ))
        for code, (unit_cost, effective_time) in costs.items():
            db.add(CostSnapshot(
                workspace_id=workspace.id,
                sku_id=sku_ids[code],
                unit_cost=Decimal(unit_cost),
                effective_time=effective_time,
                source="manual",
                # **两侧库必须完全相同**（含 source_ref）——否则成本快照表比对会
                # 因 fixture 差异而假红，掩盖真正的不一致。
                source_ref="probe-%s" % code,
                currency="CNY",
            ))
        db.commit()
        ids = {
            "workspace_id": workspace.id,
            "account_id": external.id,
            "sku_ids": sku_ids,
        }
    finally:
        db.close()
    return engine, session_factory, ids


# ---------------------------------------------------------------------------
# 跑一遍 ingest
# ---------------------------------------------------------------------------

def run_ingest(session_factory, records, *, workspace_id: int, legacy: bool):
    """在给定库里跑一遍 legacy 或 new 的 ingest_orders，返回 (stats, session)。"""
    from app.external_orders import _ingest_orders_legacy, ingest_orders

    db = session_factory()
    func = _ingest_orders_legacy if legacy else ingest_orders
    try:
        stats = func(db, records, workspace_id=workspace_id)
        return stats, db
    except Exception:
        db.close()
        raise


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------

def snapshot(db) -> dict:
    """五张表的可比较快照：订单头、订单行（含五件套）、日销、成本快照引用、统计。"""
    orders = db.scalars(select(ExternalOrder)).all()
    all_lines = db.scalars(select(ExternalOrderLine)).all()
    daily = db.scalars(select(DailySkuSale)).all()
    snapshots = db.scalars(select(CostSnapshot)).all()

    order_by_id = {order.id: order for order in orders}

    # 行 → 归一化键 (external_sku, seq)：用 legacy_seq_map（与 A-3 同一套规则）
    lines_by_order = {}
    for line in all_lines:
        lines_by_order.setdefault(line.external_order_id, []).append(line)

    line_rows = []
    for order_id, lines in lines_by_order.items():
        seq_map = legacy_seq_map(lines)
        order = order_by_id.get(order_id)
        for line in lines:
            key = seq_map.get(line.external_line_id)
            row = {
                "order_no": order.external_order_no if order else None,
                "line_key": None if key is None else "%s#%d" % (key[0], key[1]),
                "raw_line_id": line.external_line_id,
                "anchored_at": _plain(line.anchored_at),
            }
            for field in LINE_FIELDS:
                row[field] = _plain(getattr(line, field))
            line_rows.append(row)
    line_rows.sort(key=lambda item: (str(item["order_no"]), str(item["line_key"])))

    order_rows = []
    for order in orders:
        row = {"order_no": order.external_order_no}
        for field in ORDER_FIELDS:
            row[field] = _plain(getattr(order, field))
        order_rows.append(row)
    order_rows.sort(key=lambda item: str(item["order_no"]))

    daily_rows = []
    for item in daily:
        row = {}
        for field in DAILY_FIELDS:
            row[field] = _plain(getattr(item, field))
        row["calculated_at_present"] = item.calculated_at is not None
        daily_rows.append(row)
    daily_rows.sort(key=lambda item: (str(item["external_sku"]), str(item["sales_date"])))

    cost_rows = []
    for item in snapshots:
        cost_rows.append({
            "id": item.id,
            "sku_id": item.sku_id,
            "unit_cost": _plain(item.unit_cost),
            "effective_time": _plain(item.effective_time),
            "source": item.source,
            "source_ref": item.source_ref,
            "currency": item.currency,
        })
    cost_rows.sort(key=lambda item: item["id"])

    return {
        "orders": order_rows,
        "lines": line_rows,
        "daily": daily_rows,
        "cost_snapshots": cost_rows,
    }


def stats_snapshot(stats) -> dict:
    """导入统计的可比较快照。"""
    out = {field: _plain(getattr(stats, field)) for field in STATS_FIELDS}
    out["affected_dates"] = list(stats.affected_dates or [])
    return out


# ---------------------------------------------------------------------------
# 比对
# ---------------------------------------------------------------------------

def _anchored_state(snapshot_rows, *, prev_rows=None):
    """把 anchored_at 归一化成「是否为空 + 是否等于旧值」。

    §5.2 第 3 条：两次运行的时间戳天然不同，比绝对值必然假红。
    """
    prev_by_key = {}
    if prev_rows is not None:
        prev_by_key = {(r["order_no"], r["line_key"]): r for r in prev_rows}
    out = {}
    for row in snapshot_rows:
        key = (row["order_no"], row["line_key"])
        state = {"present": row["anchored_at"] is not None}
        if prev_rows is not None:
            prev = prev_by_key.get(key)
            state["preserved"] = (
                prev is not None
                and prev["anchored_at"] is not None
                and row["anchored_at"] == prev["anchored_at"]
            )
        out[key] = state
    return out


def compare(left: dict, right: dict, *, left_prev=None, right_prev=None) -> list:
    """逐组比对两份快照，返回差异描述列表（空列表 = 完全一致）。

    - `external_line_id` **不做字面比对**（两侧归一化成 `(sku, seq)`，并强制断言双射）；
    - `anchored_at` 比「是否为空 + 是否等于旧值」，不比绝对时间；
    - 其余字段全比（§5.2 第 2 条 / §7 红线 8：不许为了通过而放宽比对字段）。
    """
    diffs: list = []

    # --- 订单头 ---
    left_orders = {row["order_no"]: row for row in left["orders"]}
    right_orders = {row["order_no"]: row for row in right["orders"]}
    if set(left_orders) != set(right_orders):
        diffs.append("订单号集合不同：legacy=%s new=%s" % (
            sorted(left_orders), sorted(right_orders)))
    for order_no in sorted(set(left_orders) & set(right_orders)):
        a, b = left_orders[order_no], right_orders[order_no]
        for field in ORDER_FIELDS:
            if a[field] != b[field]:
                diffs.append("订单 %s 的 %s：legacy=%r new=%r" % (order_no, field, a[field], b[field]))

    # --- 订单行（归一化键 + 双射断言）---
    left_lines = {(row["order_no"], row["line_key"]): row for row in left["lines"]}
    right_lines = {(row["order_no"], row["line_key"]): row for row in right["lines"]}
    if len(left_lines) != len(left["lines"]) or len(right_lines) != len(right["lines"]):
        diffs.append("行键存在重复（归一化后撞键）：legacy=%d/%d new=%d/%d" % (
            len(left_lines), len(left["lines"]), len(right_lines), len(right["lines"])))
    if set(left_lines) != set(right_lines):
        diffs.append("归一化后的行键集合不同（双射不成立）：legacy=%s new=%s" % (
            sorted(str(k) for k in left_lines), sorted(str(k) for k in right_lines)))
    left_anchor = _anchored_state(left["lines"], prev_rows=left_prev)
    right_anchor = _anchored_state(right["lines"], prev_rows=right_prev)
    for key in sorted(set(left_lines) & set(right_lines), key=str):
        a, b = left_lines[key], right_lines[key]
        for field in LINE_FIELDS:
            if a[field] != b[field]:
                diffs.append("行 %s 的 %s：legacy=%r new=%r" % (key, field, a[field], b[field]))
        if left_anchor[key] != right_anchor[key]:
            diffs.append("行 %s 的 anchored_at 状态：legacy=%r new=%r" % (
                key, left_anchor[key], right_anchor[key]))

    # --- 日销 ---
    left_daily = {(r["external_sku"], str(r["sales_date"])): r for r in left["daily"]}
    right_daily = {(r["external_sku"], str(r["sales_date"])): r for r in right["daily"]}
    if set(left_daily) != set(right_daily):
        diffs.append("日销键集合不同：legacy=%s new=%s" % (
            sorted(str(k) for k in left_daily), sorted(str(k) for k in right_daily)))
    for key in sorted(set(left_daily) & set(right_daily), key=str):
        a, b = left_daily[key], right_daily[key]
        for field in DAILY_FIELDS:
            if a[field] != b[field]:
                diffs.append("日销 %s 的 %s：legacy=%r new=%r" % (key, field, a[field], b[field]))
        if a["calculated_at_present"] != b["calculated_at_present"]:
            diffs.append("日销 %s 的 calculated_at 存在性不同" % (key,))

    # --- 成本快照引用 ---
    if left["cost_snapshots"] != right["cost_snapshots"]:
        diffs.append("成本快照表不同：legacy=%r new=%r" % (
            left["cost_snapshots"], right["cost_snapshots"]))

    return diffs
