"""库存看板聚合查询。"""

from __future__ import annotations

from datetime import datetime, timedelta
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import CrawlJob, InboundLine, InboundOrder, InventoryBalance, Product, ProductSku, Warehouse


def build_dashboard_summary(
    db: Session,
    *,
    workspace_id: int,
    days: int = 7,
    warehouse_id: int | None = None,
    recent_limit: int = 10,
) -> dict:
    now = datetime.utcnow()
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
    failed_jobs = db.scalar(select(func.count(CrawlJob.id)).where(CrawlJob.workspace_id == workspace_id, CrawlJob.status == "failed", CrawlJob.finished_at >= start, CrawlJob.finished_at < now)) or 0
    from app.db import InventoryAlert
    alert_rows = db.scalars(select(InventoryAlert).where(InventoryAlert.workspace_id == workspace_id, InventoryAlert.warehouse_id == warehouse_id if warehouse_id is not None else True)).all()
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
        {"id": alert.id, "kind": alert.kind, "severity": alert.severity, "status": alert.status, "title": alert.title, "message": alert.message}
        for alert in sorted(alert_rows, key=lambda item: (item.last_seen_at, item.id), reverse=True)[:10]
    ]
    from app.external_sync import snapshot_freshness
    freshness = snapshot_freshness(db, workspace_id=workspace_id)
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
        },
        "recent_inbounds": [{"id": order.id, "reference_no": order.reference_no, "warehouse_id": order.warehouse_id, "status": order.status, "created_at": order.created_at.isoformat(), **line_by_order.get(order.id, {"expected_qty": 0, "received_qty": 0})} for order in recent],
        "alert_summary": alert_summary,
        "snapshot_freshness": freshness,
        "unsupported_metrics": ["sales_qty", "gmv", "inventory_turnover", "days_of_inventory", "forecast", "dynamic_replenishment_qty"],
        "limitations": ["库存指标仅代表已确认的内部 on_hand；外部平台快照不在本汇总中跨来源相加", "入库数量按入库单创建时间统计"],
    }
