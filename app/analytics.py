"""只读 BI 指标层：商品表现四象限、销量、库存健康等。"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import (
    DailySkuSale,
    InventoryBalance,
    InventoryPolicy,
    Product,
    ProductSku,
    Warehouse,
)

QUADRANT_FOCAL_SUPPLEMENT = "focal_supplement"
QUADRANT_HEALTHY = "healthy"
QUADRANT_WATCH = "watch"
QUADRANT_SLOW_RISK = "slow_risk"
QUADRANT_INSUFFICIENT = "insufficient"

DEFAULT_GROWTH_HIGH = 0.10
DEFAULT_DAYS_LOW = 14.0
ALLOWED_WINDOWS = (7, 14, 30)


@dataclass
class QuadrantThresholds:
    growth_high: float = DEFAULT_GROWTH_HIGH
    days_low: float = DEFAULT_DAYS_LOW


def _resolve_thresholds(growth_high: float | None, days_low: float | None) -> QuadrantThresholds:
    thresholds = QuadrantThresholds()
    if growth_high is not None:
        thresholds.growth_high = float(growth_high)
    if days_low is not None:
        thresholds.days_low = float(days_low)
    return thresholds


def _sales_window(db: Session, *, workspace_id: int, internal_sku_id: int | None, start: date, end: date) -> dict:
    filters = [
        DailySkuSale.workspace_id == workspace_id,
        DailySkuSale.sales_date >= start,
        DailySkuSale.sales_date <= end,
    ]
    if internal_sku_id is not None:
        filters.append(DailySkuSale.internal_sku_id == internal_sku_id)
    rows = db.scalars(select(DailySkuSale).where(*filters)).all()
    total_net = sum(row.net_qty for row in rows)
    days = (end - start).days + 1
    complete_days = {row.sales_date for row in rows if row.data_completeness == "complete"}
    complete = len(complete_days) == days and days > 0
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "required_days": days,
        "effective_sale_days": len(complete_days),
        "missing_days": max(0, days - len(complete_days)),
        "net_qty": total_net,
        "daily_avg_qty": float(Decimal(total_net) / Decimal(len(complete_days))) if complete and complete_days else None,
        "data_completeness": "complete" if complete else "insufficient",
        "quality_reason": None if complete else "INCOMPLETE_COVERAGE",
    }


def _classify(growth_rate: float | None, days_value: float | None, thresholds: QuadrantThresholds) -> tuple[str, str | None, str]:
    if growth_rate is None or days_value is None:
        return QUADRANT_INSUFFICIENT, "INCOMPLETE_COVERAGE", "insufficient"
    high_growth = growth_rate >= thresholds.growth_high
    low_inventory = days_value < thresholds.days_low
    if high_growth and low_inventory:
        return QUADRANT_FOCAL_SUPPLEMENT, None, "complete"
    if high_growth and not low_inventory:
        return QUADRANT_HEALTHY, None, "complete"
    if not high_growth and low_inventory:
        return QUADRANT_WATCH, None, "complete"
    return QUADRANT_SLOW_RISK, None, "complete"


def build_product_quadrant(
    db: Session,
    *,
    workspace_id: int,
    as_of: date | None = None,
    growth_window: int = 7,
    baseline_window: int = 14,
    days_window: int = 14,
    warehouse_id: int | None = None,
    growth_high: float | None = None,
    days_low: float | None = None,
    limit: int = 500,
) -> dict:
    if growth_window not in ALLOWED_WINDOWS:
        raise ValueError("INVALID_GROWTH_WINDOW")
    if baseline_window not in ALLOWED_WINDOWS:
        raise ValueError("INVALID_BASELINE_WINDOW")
    if days_window not in ALLOWED_WINDOWS:
        raise ValueError("INVALID_DAYS_WINDOW")
    if growth_window == baseline_window:
        raise ValueError("INVALID_GROWTH_WINDOW")
    as_of = as_of or __import__("datetime").datetime.utcnow().date()
    thresholds = _resolve_thresholds(growth_high, days_low)

    sku_filters = [ProductSku.workspace_id == workspace_id, ProductSku.is_active.is_(True)]
    sku_query = select(ProductSku).where(*sku_filters).order_by(ProductSku.id)
    skus = db.scalars(sku_query).all()

    short_start = as_of - timedelta(days=growth_window - 1)
    baseline_start = as_of - timedelta(days=growth_window + baseline_window - 1)
    baseline_end = as_of - timedelta(days=growth_window)
    days_start = as_of - timedelta(days=days_window - 1)

    sales_filters = [
        DailySkuSale.workspace_id == workspace_id,
        DailySkuSale.sales_date >= baseline_start,
        DailySkuSale.sales_date <= as_of,
    ]
    sales_rows = db.scalars(select(DailySkuSale).where(*sales_filters)).all()
    sku_sales: dict[int, list[DailySkuSale]] = defaultdict(list)
    for row in sales_rows:
        if row.internal_sku_id is not None:
            sku_sales[row.internal_sku_id].append(row)

    balance_filters = [InventoryBalance.workspace_id == workspace_id]
    if warehouse_id is not None:
        balance_filters.append(InventoryBalance.warehouse_id == warehouse_id)
    balances = db.scalars(select(InventoryBalance).where(*balance_filters)).all()
    on_hand_by_sku: dict[int, int] = defaultdict(int)
    for balance in balances:
        on_hand_by_sku[balance.sku_id] += balance.on_hand_qty

    products = {row.id: row for row in db.scalars(select(Product).where(Product.workspace_id == workspace_id)).all()}

    items: list[dict] = []
    summary = {key: 0 for key in (QUADRANT_FOCAL_SUPPLEMENT, QUADRANT_HEALTHY, QUADRANT_WATCH, QUADRANT_SLOW_RISK, QUADRANT_INSUFFICIENT)}
    unsupported: list[str] = []
    for sku in skus:
        rows = sku_sales.get(sku.id, [])
        short_rows = [row for row in rows if short_start <= row.sales_date <= as_of]
        baseline_rows = [row for row in rows if baseline_start <= row.sales_date <= baseline_end]
        days_rows = [row for row in rows if days_start <= row.sales_date <= as_of]

        short_complete = len(short_rows) >= growth_window and all(row.data_completeness == "complete" for row in short_rows)
        baseline_complete = len(baseline_rows) >= baseline_window and all(row.data_completeness == "complete" for row in baseline_rows)
        days_complete = len(days_rows) >= days_window and all(row.data_completeness == "complete" for row in days_rows)

        short_window = _aggregate(short_rows, growth_window, short_complete)
        baseline_window_dict = _aggregate(baseline_rows, baseline_window, baseline_complete)
        days_window_dict = _aggregate(days_rows, days_window, days_complete)

        if short_complete and baseline_complete:
            numerator = short_window["net_qty"] - baseline_window_dict["net_qty"]
            denominator = baseline_window_dict["net_qty"]
            growth_rate = (numerator / denominator) if denominator else None
        else:
            numerator = short_window["net_qty"] if short_complete else None
            denominator = baseline_window_dict["net_qty"] if baseline_complete else None
            growth_rate = None
        days_of_inventory = (on_hand_by_sku.get(sku.id, 0) / days_window_dict["daily_avg_qty"]) if days_complete and days_window_dict["daily_avg_qty"] else None
        quadrant, reason, completeness = _classify(growth_rate, days_of_inventory, thresholds)
        product = products.get(sku.product_id)
        if not short_complete and not baseline_complete:
            unsupported.append("growth_data_insufficient")
        if not days_complete:
            unsupported.append("days_data_insufficient")
        item = {
            "sku_id": sku.id,
            "sku_code": sku.sku_code,
            "product_id": sku.product_id,
            "product_title": product.title if product else None,
            "category": product.category if product else None,
            "platform": short_rows[0].platform if short_rows else None,
            "on_hand_qty": int(on_hand_by_sku.get(sku.id, 0)),
            "days_of_inventory": days_of_inventory,
            "growth_rate": growth_rate,
            "growth_numerator": numerator,
            "growth_denominator": denominator,
            "quadrant": quadrant,
            "data_completeness": completeness,
            "reason": reason,
            "windows": {
                "short": short_window,
                "long": short_window,
                "baseline": baseline_window_dict,
                "days": days_window_dict,
                "as_of": as_of.isoformat(),
                "business_timezone": "UTC",
            },
        }
        items.append(item)
        summary[quadrant] += 1
    items.sort(key=lambda item: (-(item["growth_rate"] or float("-inf")), -(item["days_of_inventory"] or float("-inf"))))
    return {
        "as_of": as_of.isoformat(),
        "business_timezone": "UTC",
        "growth_window": growth_window,
        "baseline_window": baseline_window,
        "days_window": days_window,
        "growth_high": thresholds.growth_high,
        "days_low": thresholds.days_low,
        "summary": {**summary, "total_scored": len(items)},
        "items": items[:limit],
        "unsupported_metrics": sorted(set(unsupported)),
        "limitations": [
            "增长率与覆盖天数使用 DailySkuSale.data_completeness=complete 的日期；缺失日期不补 0。",
            "覆盖天数仅使用已确认的内部 InventoryBalance.on_hand_qty，不混入外部观察值。",
            "当前 sales_date 为 UTC naive 投影，P0-4b 后将按 IANA 时区重做业务日。",
        ],
    }


def _aggregate(rows: list[DailySkuSale], window_days: int, complete: bool) -> dict:
    net_qty = sum(row.net_qty for row in rows)
    complete_days = len({row.sales_date for row in rows if row.data_completeness == "complete"})
    daily_avg_qty = float(Decimal(net_qty) / Decimal(complete_days)) if complete and complete_days else None
    return {
        "from": (min((row.sales_date for row in rows), default=None).isoformat() if rows else None),
        "to": (max((row.sales_date for row in rows), default=None).isoformat() if rows else None),
        "required_days": window_days,
        "effective_sale_days": complete_days,
        "missing_days": max(0, window_days - complete_days),
        "net_qty": net_qty,
        "daily_avg_qty": daily_avg_qty,
        "data_completeness": "complete" if complete else "insufficient",
        "quality_reason": None if complete else "INCOMPLETE_COVERAGE",
    }