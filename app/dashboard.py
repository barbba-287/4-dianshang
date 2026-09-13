"""库存看板聚合查询。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import math

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.business_dates import resolve_sales_as_of
from app.db import (
    CrawlJob,
    InboundLine,
    InboundOrder,
    InventoryBalance,
    InventoryPolicy,
    Product,
    ProductSku,
    Warehouse,
)


def _sales_window(db: Session, *, workspace_id: int, start: date, end: date) -> dict:
    from app.db import DailySkuSale
    rows = db.scalars(select(DailySkuSale).where(
        DailySkuSale.workspace_id == workspace_id,
        DailySkuSale.sales_date >= start,
        DailySkuSale.sales_date <= end,
    )).all()
    total_net = sum(row.net_qty for row in rows)
    total_gross = sum(row.gross_qty for row in rows)
    total_refund = sum(row.refunded_qty for row in rows)
    days = (end - start).days + 1
    observed_days = len({row.sales_date for row in rows})
    complete_days = {row.sales_date for row in rows if row.data_completeness == "complete"}
    complete = len(complete_days) >= days if rows else False
    return {
        "from": start.isoformat(), "to": end.isoformat(), "required_days": days,
        "observed_days": observed_days, "missing_days": max(0, days - len(complete_days)),
        "gross_qty": total_gross,
        "refunded_qty": total_refund, "net_qty": total_net,
        "effective_sale_days": len(complete_days),
        "daily_avg_qty": float(Decimal(total_net) / len(complete_days)) if complete and complete_days else None,
        "data_completeness": "complete" if complete else "insufficient",
        "quality_reason": None if complete else "INCOMPLETE_COVERAGE",
    }


def build_sales_summary(db: Session, *, workspace_id: int, as_of: date | None = None) -> dict:
    as_of = resolve_sales_as_of(db, workspace_id=workspace_id, explicit_as_of=as_of)
    return {
        "windows": {
            "7": _sales_window(db, workspace_id=workspace_id, start=as_of - timedelta(days=6), end=as_of),
            "14": _sales_window(db, workspace_id=workspace_id, start=as_of - timedelta(days=13), end=as_of),
            "30": _sales_window(db, workspace_id=workspace_id, start=as_of - timedelta(days=29), end=as_of),
        },
        "limitations": ["销量按外部订单创建日统计；取消和退款修订创建日", "缺少完整覆盖日期时不把缺失日期当作零销量"],
        "as_of": as_of.isoformat(),
    }


def build_sku_health(
    db: Session,
    *,
    workspace_id: int,
    coverage_days: int = 14,
    as_of: date | None = None,
    warehouse_ids: tuple[int, ...] | list[int] | None = None,
) -> list[dict]:
    """Build an explainable SKU health view from internal stock and external sales."""
    from app.db import DailySkuSale

    as_of = resolve_sales_as_of(db, workspace_id=workspace_id, explicit_as_of=as_of)
    if coverage_days <= 0:
        raise ValueError("INVALID_COVERAGE_DAYS")
    sku_query = select(ProductSku).where(
        ProductSku.workspace_id == workspace_id,
        ProductSku.is_active.is_(True),
    ).order_by(ProductSku.id)
    if warehouse_ids is not None:
        if not warehouse_ids:
            return []
        scoped_skus = select(InventoryBalance.sku_id).where(
            InventoryBalance.workspace_id == workspace_id,
            InventoryBalance.warehouse_id.in_(warehouse_ids),
        )
        sku_query = sku_query.where(ProductSku.id.in_(scoped_skus))
    skus = db.scalars(sku_query).all()
    start = as_of - timedelta(days=29)
    sales_rows = db.scalars(select(DailySkuSale).where(
        DailySkuSale.workspace_id == workspace_id,
        DailySkuSale.sales_date >= start,
        DailySkuSale.sales_date <= as_of,
    )).all()
    sales_by_sku: dict[int, list] = {}
    for row in sales_rows:
        if row.internal_sku_id is not None:
            sales_by_sku.setdefault(row.internal_sku_id, []).append(row)
    balances_query = select(InventoryBalance).where(InventoryBalance.workspace_id == workspace_id)
    if warehouse_ids is not None:
        balances_query = balances_query.where(InventoryBalance.warehouse_id.in_(warehouse_ids))
    balances = db.scalars(balances_query).all()
    balance_by_sku: dict[int, int] = {}
    for balance in balances:
        balance_by_sku[balance.sku_id] = balance_by_sku.get(balance.sku_id, 0) + balance.on_hand_qty
    policies = db.scalars(select(InventoryPolicy).where(InventoryPolicy.workspace_id == workspace_id)).all()
    policy_by_sku: dict[int, InventoryPolicy] = {}
    for policy in policies:
        if warehouse_ids is None or policy.warehouse_id in warehouse_ids:
            policy_by_sku.setdefault(policy.sku_id, policy)
    result = []
    for sku in skus:
        rows = sales_by_sku.get(sku.id, [])
        metrics = {}
        for window in (7, 14, 30):
            window_start = as_of - timedelta(days=window - 1)
            matching = [row for row in rows if window_start <= row.sales_date <= as_of]
            observed_days = len({row.sales_date for row in matching})
            complete_days = len({row.sales_date for row in matching if row.data_completeness == "complete"})
            net_qty = sum(row.net_qty for row in matching)
            complete = complete_days == window
            avg = (Decimal(net_qty) / complete_days) if complete and complete_days else None
            metrics[str(window)] = {
                "sales_qty": net_qty,
                "effective_sale_days": complete_days,
                "daily_avg_qty": float(avg) if avg is not None else None,
                "data_completeness": "complete" if complete else "insufficient",
                "quality_reason": None if complete else "INCOMPLETE_COVERAGE",
                "observed_days": observed_days,
            }
        policy = policy_by_sku.get(sku.id)
        on_hand = balance_by_sku.get(sku.id, 0)
        avg = metrics[str(coverage_days if coverage_days in (7, 14, 30) else 14)]["daily_avg_qty"]
        if avg is None:
            status = "data_insufficient"
            suggested = None
            days_of_inventory = None
            reason = "INCOMPLETE_COVERAGE"
        elif avg == 0:
            status = "no_sales"
            suggested = 0
            days_of_inventory = None
            reason = "NO_SALES"
        else:
            safety = policy.safety_stock_qty if policy else 0
            target = math.ceil(avg * coverage_days + safety)
            suggested = max(0, target - on_hand)
            days_of_inventory = round(on_hand / avg, 2)
            status = "urgent" if days_of_inventory < 3 else "reorder" if days_of_inventory < 7 else "healthy"
            reason = "LOW_STOCK" if suggested else "STOCK_SUFFICIENT"
        result.append({
            "sku_id": sku.id,
            "sku_code": sku.sku_code,
            "product_id": sku.product_id,
            "on_hand_qty": on_hand,
            "sales": metrics,
            "safety_stock_qty": policy.safety_stock_qty if policy else 0,
            "reorder_point_qty": policy.reorder_point_qty if policy else 0,
            "days_of_inventory": days_of_inventory,
            "suggested_replenishment_qty": suggested,
            "status": status,
            "reason": reason,
        })
    return result


def _sales_trend(db: Session, *, workspace_id: int, as_of: date, days: int, sku_id: int | None = None) -> dict:
    """Return a sparse daily trend; missing dates stay null, never become zero."""
    from app.db import DailySkuSale

    start = as_of - timedelta(days=days - 1)
    filters = [
        DailySkuSale.workspace_id == workspace_id,
        DailySkuSale.sales_date >= start,
        DailySkuSale.sales_date <= as_of,
    ]
    if sku_id is not None:
        filters.append(DailySkuSale.internal_sku_id == sku_id)
    rows = db.scalars(select(DailySkuSale).where(*filters)).all()
    by_day = {}
    for row in rows:
        item = by_day.setdefault(row.sales_date.isoformat(), {"qty": 0, "complete": True})
        item["qty"] += row.net_qty
        item["complete"] = item["complete"] and row.data_completeness == "complete"
    dates = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    values = [by_day.get(day, {}).get("qty") if by_day.get(day, {}).get("complete", False) else None for day in dates]
    return {"days": days, "from": start.isoformat(), "to": as_of.isoformat(), "sku_id": sku_id, "dates": dates, "values": values, "complete_days": sum(value is not None for value in values), "missing_days": sum(value is None for value in values)}


def _inventory_chart(db: Session, *, workspace_id: int, warehouse_id: int | None = None, limit: int = 10) -> dict:
    filters = [ProductSku.workspace_id == workspace_id, InventoryBalance.workspace_id == workspace_id]
    if warehouse_id is not None:
        filters.append(InventoryBalance.warehouse_id == warehouse_id)
    rows = db.execute(
        select(ProductSku.sku_code, Warehouse.code, func.coalesce(InventoryBalance.on_hand_qty, 0))
        .join(InventoryBalance, InventoryBalance.sku_id == ProductSku.id)
        .join(Warehouse, Warehouse.id == InventoryBalance.warehouse_id)
        .where(*filters)
        .order_by(InventoryBalance.on_hand_qty.asc(), ProductSku.id, Warehouse.id)
    ).all()
    totals = {}
    quantities = {}
    for sku_code, warehouse_code, quantity in rows:
        value = int(quantity or 0)
        totals[sku_code] = totals.get(sku_code, 0) + value
        quantities[(sku_code, warehouse_code)] = value
    selected = [sku for sku, _ in sorted(totals.items(), key=lambda item: (item[1], item[0]))[:limit]]
    warehouses = sorted({warehouse_code for _, warehouse_code, _ in rows})
    series = [{"name": warehouse, "values": [quantities.get((code, warehouse), 0) for code in selected]} for warehouse in warehouses]
    return {"skus": selected, "warehouses": warehouses, "series": series, "limit": limit}


def build_sync_health_summary(db: Session, *, workspace_id: int, stale_after_seconds: int = 900, now: datetime | None = None) -> dict:
    """汇总外部同步运行健康；只读取当前 workspace 的运行记录。"""
    from app.db import ExternalSyncRun
    from app.external_sync import resource_status, retryable_resources

    now = now or datetime.utcnow()
    rows = db.scalars(select(ExternalSyncRun).where(ExternalSyncRun.workspace_id == workspace_id).order_by(ExternalSyncRun.id.desc()).limit(5000)).all()
    counts = {key: 0 for key in ("running", "succeeded", "failed", "partial", "stalled", "retryable")}
    latest: dict[tuple, ExternalSyncRun] = {}
    recent_failures = []
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
        heartbeat = row.heartbeat_at or row.started_at
        age = max(0, int((now - heartbeat).total_seconds())) if heartbeat else None
        stalled = row.status == "running" and age is not None and age > stale_after_seconds
        if stalled:
            counts["stalled"] += 1
        retryable = retryable_resources(row)
        key = (row.platform, row.account_ref, row.store_ref, row.sync_type)
        latest.setdefault(key, row)
        if row.status in {"failed", "partial"}:
            recent_failures.append({"run_id": row.run_id, "platform": row.platform, "sync_type": row.sync_type, "status": row.status, "error_code": row.error_code, "retryable_resources": retryable})
    def item(row: ExternalSyncRun) -> dict:
        heartbeat = row.heartbeat_at or row.started_at
        return {"run_id": row.run_id, "platform": row.platform, "account_ref": row.account_ref, "store_ref": row.store_ref, "sync_type": row.sync_type, "status": row.status, "attempt": row.attempt or 1, "retry_of_run_id": row.retry_of_run_id, "resource_status": resource_status(row), "retryable_resources": retryable_resources(row), "duration_seconds": int(((row.finished_at or now) - row.started_at).total_seconds()) if row.started_at else None, "heartbeat_age_seconds": max(0, int((now - heartbeat).total_seconds())) if heartbeat else None}
    latest_rows = list(latest.values())
    counts["retryable"] = sum(len(retryable_resources(row)) for row in latest_rows)
    return {"as_of": now.isoformat(), "stale_after_seconds": stale_after_seconds, "counts": counts, "latest": [item(row) for row in latest_rows], "recent_failures": recent_failures[:20]}


def build_dashboard_summary(
    db: Session,
    *,
    workspace_id: int,
    days: int = 7,
    as_of: date | None = None,
    warehouse_id: int | None = None,
    sku_id: int | None = None,
    recent_limit: int = 10,
) -> dict:
    now = datetime.utcnow()
    as_of = resolve_sales_as_of(db, workspace_id=workspace_id, explicit_as_of=as_of)
    start = now - timedelta(days=days)
    warehouse_filter = [InboundOrder.workspace_id == workspace_id]
    if warehouse_id is not None:
        warehouse_filter.append(InboundOrder.warehouse_id == warehouse_id)
    balance_filter = [InventoryBalance.workspace_id == workspace_id]
    if warehouse_id is not None:
        balance_filter.append(InventoryBalance.warehouse_id == warehouse_id)
    inbound_query = select(InboundOrder).where(*warehouse_filter, InboundOrder.created_at >= start, InboundOrder.created_at < now)
    orders = db.scalars(inbound_query).all()
    lines = db.scalars(
        select(InboundLine)
        .where(
            InboundLine.inbound_order_id.in_([order.id for order in orders]),
            InboundLine.workspace_id == workspace_id,
        )
    ).all() if orders else []
    statuses = {status: sum(1 for order in orders if order.status == status) for status in ("expected", "received", "confirmed")}
    expected_qty = sum(line.expected_qty for line in lines)
    received_qty = sum(line.received_qty or 0 for line in lines)
    damaged_qty = sum(line.damaged_qty or 0 for line in lines)
    balance_total = db.scalar(select(func.coalesce(func.sum(InventoryBalance.on_hand_qty), 0)).where(*balance_filter)) or 0
    balance_count = db.scalar(select(func.count(InventoryBalance.id)).where(*balance_filter)) or 0
    current_in_transit_filter = [InboundOrder.workspace_id == workspace_id, InboundOrder.status == "expected"]
    if warehouse_id is not None:
        current_in_transit_filter.append(InboundOrder.warehouse_id == warehouse_id)
    current_in_transit_orders = db.scalars(select(InboundOrder).where(*current_in_transit_filter)).all()
    current_in_transit_ids = [order.id for order in current_in_transit_orders]
    current_in_transit_qty = db.scalar(select(func.coalesce(func.sum(InboundLine.expected_qty), 0)).where(InboundLine.workspace_id == workspace_id, InboundLine.inbound_order_id.in_(current_in_transit_ids))) if current_in_transit_ids else 0
    failed_jobs = db.scalar(select(func.count(CrawlJob.id)).where(CrawlJob.workspace_id == workspace_id, CrawlJob.status == "failed", CrawlJob.finished_at >= start, CrawlJob.finished_at < now)) or 0
    from app.db import InventoryAlert
    alert_rows = db.scalars(select(InventoryAlert).where(InventoryAlert.workspace_id == workspace_id, InventoryAlert.warehouse_id == warehouse_id if warehouse_id is not None else True)).all()
    sku_ids = {alert.sku_id for alert in alert_rows if alert.sku_id is not None}
    sku_labels = {}
    if sku_ids:
        for sku, product in db.execute(select(ProductSku, Product).join(Product, Product.id == ProductSku.product_id).where(ProductSku.workspace_id == workspace_id, ProductSku.id.in_(sku_ids))).all():
            sku_labels[sku.id] = {"product_id": product.id, "product_title": product.title, "sku_code": sku.sku_code, "variant_label": sku.variant_label}
    alert_summary = {
        "total": len(alert_rows),
        "active": sum(1 for alert in alert_rows if alert.status != "resolved"),
        "open": sum(1 for alert in alert_rows if alert.status == "open"),
        "acknowledged": sum(1 for alert in alert_rows if alert.status == "acknowledged"),
        "resolved": sum(1 for alert in alert_rows if alert.status == "resolved"),
        "by_kind": {},
        "by_severity": {},
        "recent": [],
    }
    for alert in alert_rows:
        alert_summary["by_kind"][alert.kind] = alert_summary["by_kind"].get(alert.kind, 0) + 1
        alert_summary["by_severity"][alert.severity] = alert_summary["by_severity"].get(alert.severity, 0) + 1
    alert_summary["recent"] = [
        {"id": alert.id, "kind": alert.kind, "severity": alert.severity, "status": alert.status, "title": alert.title, "message": alert.message, "warehouse_id": alert.warehouse_id, "sku_id": alert.sku_id, **sku_labels.get(alert.sku_id, {}), "platform": alert.platform, "last_seen_at": alert.last_seen_at.isoformat() if alert.last_seen_at else None}
        for alert in sorted(alert_rows, key=lambda item: (item.last_seen_at, item.id), reverse=True)[:10]
    ]
    alert_summary["details"] = []
    grouped_alerts = {}
    for alert in alert_rows:
        if alert.sku_id not in sku_labels:
            continue
        grouped_alerts.setdefault(alert.sku_id, []).append({"id": alert.id, "kind": alert.kind, "severity": alert.severity, "status": alert.status, "title": alert.title, "message": alert.message, "warehouse_id": alert.warehouse_id, "sku_id": alert.sku_id, "platform": alert.platform, "last_seen_at": alert.last_seen_at.isoformat() if alert.last_seen_at else None})
    for sku_id, alerts in grouped_alerts.items():
        label = sku_labels[sku_id]
        alert_summary["details"].append({**label, "sku_id": sku_id, "alerts": alerts})
    alert_summary["unlinked"] = [item for item in alert_summary["recent"] if item.get("sku_id") not in sku_labels]
    from app.external_sync import snapshot_freshness
    freshness = snapshot_freshness(db, workspace_id=workspace_id)
    sync_health = build_sync_health_summary(
        db,
        workspace_id=workspace_id,
        stale_after_seconds=get_settings().external_sync_run_stale_after_seconds,
        now=now,
    )
    sku_health = build_sku_health(
        db,
        workspace_id=workspace_id,
        as_of=as_of,
        warehouse_ids=None if warehouse_id is None else (warehouse_id,),
    )
    product_skus = {row.id: row for row in db.scalars(select(ProductSku).where(ProductSku.workspace_id == workspace_id)).all()}
    products = {row.id: row for row in db.scalars(select(Product).where(Product.workspace_id == workspace_id)).all()}
    alerts_by_sku = {}
    for alert in alert_rows:
        if alert.sku_id is not None:
            alerts_by_sku.setdefault(alert.sku_id, []).append({"id": alert.id, "kind": alert.kind, "severity": alert.severity, "status": alert.status, "title": alert.title, "message": alert.message, "warehouse_id": alert.warehouse_id, "sku_id": alert.sku_id, "platform": alert.platform})
    for row in sku_health:
        sku = product_skus.get(row["sku_id"])
        product = products.get(sku.product_id) if sku else None
        row["product_title"] = product.title if product else None
        row["variant_label"] = sku.variant_label if sku else None
        row["alerts"] = alerts_by_sku.get(row["sku_id"], [])
    recent = sorted(orders, key=lambda order: (order.created_at, order.id), reverse=True)[:recent_limit]
    line_by_order = {}
    for line in lines:
        item = line_by_order.setdefault(line.inbound_order_id, {"expected_qty": 0, "received_qty": 0})
        item["expected_qty"] += line.expected_qty
        item["received_qty"] += line.received_qty or 0
    return {
        "range": {"days": days, "from": start.isoformat(), "to": now.isoformat()},
        "kpis": {
            "product_count": db.scalar(select(func.count(Product.id)).where(Product.workspace_id == workspace_id)) or 0,
            "active_sku_count": db.scalar(select(func.count(ProductSku.id)).where(ProductSku.workspace_id == workspace_id, ProductSku.is_active.is_(True))) or 0,
            "active_warehouse_count": db.scalar(select(func.count(Warehouse.id)).where(Warehouse.workspace_id == workspace_id, Warehouse.is_active.is_(True))) or 0,
            "on_hand_qty": int(balance_total), "inventory_balance_count": int(balance_count),
            "expected_inbound_count": statuses["expected"], "received_inbound_count": statuses["received"], "confirmed_inbound_count": statuses["confirmed"],
            "expected_qty": expected_qty, "received_qty": received_qty, "damaged_qty": damaged_qty,
            "accepted_qty": received_qty - damaged_qty, "difference_qty": received_qty - expected_qty, "failed_job_count": int(failed_jobs),
            "current_in_transit_expected_qty": int(current_in_transit_qty or 0), "current_in_transit_inbound_count": len(current_in_transit_orders),
            "current_in_transit_expected_qty": int(current_in_transit_qty or 0), "current_in_transit_inbound_count": len(current_in_transit_orders),
        },
        "recent_inbounds": [{"id": order.id, "reference_no": order.reference_no, "warehouse_id": order.warehouse_id, "status": order.status, "created_at": order.created_at.isoformat(), **line_by_order.get(order.id, {"expected_qty": 0, "received_qty": 0})} for order in recent],
        "alert_summary": alert_summary,
        "snapshot_freshness": freshness,
        "sync_health": sync_health,
        "sales_summary": {
            **build_sales_summary(db, workspace_id=workspace_id, as_of=as_of),
            "as_of": as_of.isoformat(),
        },
        "sales_trend": _sales_trend(db, workspace_id=workspace_id, as_of=as_of, days=min(max(days, 7), 30), sku_id=sku_id),
        "inventory_chart": _inventory_chart(db, workspace_id=workspace_id, warehouse_id=warehouse_id),
        "sku_health": sku_health,
        "unsupported_metrics": ["gmv", "inventory_turnover", "forecast", "dynamic_replenishment_qty"],
        "limitations": ["库存指标仅代表已确认的内部 on_hand；外部平台快照不在本汇总中跨来源相加", "入库数量按入库单创建时间统计", "销量按外部订单创建日统计，覆盖不完整时不计算日均"],
    }
