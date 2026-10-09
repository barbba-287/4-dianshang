"""Workspace-scoped read-only order inbox queries."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import ExternalOrder, ExternalOrderLine, Product, ProductSku


FULFILLMENT_VIEWS = {"needs_attention", "to_fulfill", "fulfilled", "cancelled"}


def _filters(*, workspace_id: int, platform: str | None, account_ref: str | None, store_ref: str | None, order_status: str | None, fulfillment_view: str | None, keyword: str | None, date_from: date | None, date_to: date | None) -> list:
    filters = [ExternalOrder.workspace_id == workspace_id]
    if platform:
        filters.append(ExternalOrder.platform == platform)
    if account_ref:
        filters.append(ExternalOrder.account_ref == account_ref)
    if store_ref:
        filters.append(ExternalOrder.store_ref == store_ref)
    if order_status:
        filters.append(ExternalOrder.order_status == order_status)
    if fulfillment_view == "needs_attention":
        filters.append(or_(ExternalOrder.order_status.in_(("pending", "unknown")), ExternalOrder.data_completeness != "complete"))
    elif fulfillment_view == "to_fulfill":
        filters.append(ExternalOrder.order_status == "paid")
    elif fulfillment_view == "fulfilled":
        filters.append(ExternalOrder.order_status.in_(("fulfilled", "completed")))
    elif fulfillment_view == "cancelled":
        filters.append(ExternalOrder.order_status.in_(("cancelled", "closed")))
    if keyword:
        pattern = f"%{keyword.strip()}%"
        filters.append(or_(ExternalOrder.external_order_no.ilike(pattern), ExternalOrder.platform.ilike(pattern), ExternalOrder.store_ref.ilike(pattern)))
    if date_from:
        filters.append(ExternalOrder.external_created_at >= datetime.combine(date_from, time.min))
    if date_to:
        filters.append(ExternalOrder.external_created_at < datetime.combine(date_to, time.max))
    return filters


def _item(order: ExternalOrder, *, line_count: int, net_item_qty: int) -> dict:
    return {
        "id": order.id,
        "platform": order.platform,
        "account_ref": order.account_ref,
        "store_ref": order.store_ref,
        "external_order_no": order.external_order_no,
        "order_status": order.order_status,
        "external_created_at": order.external_created_at,
        "paid_at": order.paid_at,
        "external_updated_at": order.external_updated_at,
        "gross_amount": order.gross_amount,
        "refund_amount": order.refund_amount,
        "currency": order.currency,
        "data_completeness": order.data_completeness,
        "status_reason": order.status_reason,
        "source_mode": order.source_mode,
        "simulated": order.simulated,
        "line_count": line_count,
        "net_item_qty": net_item_qty,
    }


def _line_summary(db: Session, *, workspace_id: int, order_ids: list[int]) -> dict[int, tuple[int, int]]:
    if not order_ids:
        return {}
    rows = db.execute(
        select(ExternalOrderLine.external_order_id, func.count(ExternalOrderLine.id), func.coalesce(func.sum(ExternalOrderLine.ordered_qty - ExternalOrderLine.cancelled_qty - ExternalOrderLine.refunded_qty), 0))
        .where(ExternalOrderLine.workspace_id == workspace_id, ExternalOrderLine.external_order_id.in_(order_ids))
        .group_by(ExternalOrderLine.external_order_id)
    ).all()
    return {int(order_id): (int(count), int(qty or 0)) for order_id, count, qty in rows}


def list_orders(db: Session, *, workspace_id: int, page: int = 1, page_size: int = 20, platform: str | None = None, account_ref: str | None = None, store_ref: str | None = None, order_status: str | None = None, fulfillment_view: str | None = None, keyword: str | None = None, date_from: date | None = None, date_to: date | None = None) -> dict:
    if page < 1 or page_size < 1 or page_size > 100:
        raise ValueError("INVALID_PAGINATION")
    if date_from and date_to and date_from > date_to:
        raise ValueError("INVALID_DATE_RANGE")
    if fulfillment_view and fulfillment_view not in FULFILLMENT_VIEWS:
        raise ValueError("INVALID_FULFILLMENT_VIEW")
    filters = _filters(workspace_id=workspace_id, platform=platform, account_ref=account_ref, store_ref=store_ref, order_status=order_status, fulfillment_view=fulfillment_view, keyword=keyword, date_from=date_from, date_to=date_to)
    total = int(db.scalar(select(func.count(ExternalOrder.id)).where(*filters)) or 0)
    orders = db.scalars(select(ExternalOrder).where(*filters).order_by(ExternalOrder.external_created_at.desc(), ExternalOrder.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    summaries = _line_summary(db, workspace_id=workspace_id, order_ids=[order.id for order in orders])
    items = [_item(order, line_count=summaries.get(order.id, (0, 0))[0], net_item_qty=summaries.get(order.id, (0, 0))[1]) for order in orders]
    complete = sum(1 for order in orders if order.data_completeness == "complete")
    statuses = {status: sum(1 for order in orders if order.order_status == status) for status in ("pending", "paid", "fulfilled", "completed", "cancelled", "closed", "unknown")}
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "summary": {"returned_count": len(items), "complete_count": complete, "data_incomplete_count": len(items) - complete, "by_status": statuses},
        "meta": {"workspace_id": workspace_id, "as_of": datetime.utcnow(), "source": "ExternalOrder", "data_completeness": "complete" if items and complete == len(items) else "partial" if items else "unknown", "limitations": ["订单状态是外部平台事实，不等于内部履约完成", "当前只读，不执行发货、面单、付款、退款或平台写回"]},
    }


def get_orders_with_lines(db: Session, *, workspace_id: int, order_status: str = "paid", platform: str | None = None, account_ref: str | None = None, store_ref: str | None = None, keyword: str | None = None, date_from: date | None = None, date_to: date | None = None, limit: int = 200) -> dict:
    if limit < 1 or limit > 200:
        raise ValueError("INVALID_EXPORT_LIMIT")
    filters = _filters(workspace_id=workspace_id, platform=platform, account_ref=account_ref, store_ref=store_ref, order_status=order_status, fulfillment_view=None, keyword=keyword, date_from=date_from, date_to=date_to)
    total = int(db.scalar(select(func.count(ExternalOrder.id)).where(*filters)) or 0)
    orders = db.scalars(select(ExternalOrder).where(*filters).order_by(ExternalOrder.external_created_at.desc(), ExternalOrder.id.desc()).limit(limit)).all()
    line_rows = db.scalars(select(ExternalOrderLine).where(ExternalOrderLine.workspace_id == workspace_id, ExternalOrderLine.external_order_id.in_([order.id for order in orders])).order_by(ExternalOrderLine.external_order_id, ExternalOrderLine.id)).all() if orders else []
    sku_ids = {line.internal_sku_id for line in line_rows if line.internal_sku_id is not None}
    sku_labels = {}
    if sku_ids:
        for sku, product in db.execute(select(ProductSku, Product).join(Product, Product.id == ProductSku.product_id).where(ProductSku.workspace_id == workspace_id, ProductSku.id.in_(sku_ids))).all():
            sku_labels[sku.id] = {"internal_product_title": product.title, "internal_variant_label": sku.variant_label}
    lines_by_order: dict[int, list] = {}
    for line in line_rows:
        lines_by_order.setdefault(line.external_order_id, []).append(line)
    items = []
    for order in orders:
        lines = lines_by_order.get(order.id, [])
        item = _item(order, line_count=len(lines), net_item_qty=sum(line.ordered_qty - line.cancelled_qty - line.refunded_qty for line in lines))
        item["lines"] = [{
            "external_line_id": line.external_line_id,
            "external_sku": line.external_sku,
            "internal_sku_id": line.internal_sku_id,
            "ordered_qty": line.ordered_qty,
            "cancelled_qty": line.cancelled_qty,
            "refunded_qty": line.refunded_qty,
            "gross_amount": line.gross_amount,
            "refund_amount": line.refund_amount,
            "currency": line.currency,
            "mapping_status": line.mapping_status,
            "data_completeness": line.data_completeness,
            **sku_labels.get(line.internal_sku_id, {}),
        } for line in lines]
        items.append(item)
    return {"items": items, "total": total}
def get_order(db: Session, *, workspace_id: int, order_id: int) -> dict | None:
    order = db.scalar(select(ExternalOrder).where(ExternalOrder.id == order_id, ExternalOrder.workspace_id == workspace_id))
    if order is None:
        return None
    lines = db.scalars(select(ExternalOrderLine).where(ExternalOrderLine.external_order_id == order.id, ExternalOrderLine.workspace_id == workspace_id).order_by(ExternalOrderLine.id)).all()
    return {**_item(order, line_count=len(lines), net_item_qty=sum(line.ordered_qty - line.cancelled_qty - line.refunded_qty for line in lines)), "lines": lines, "meta": {"workspace_id": workspace_id, "source": "ExternalOrder", "data_completeness": order.data_completeness, "limitations": ["外部订单事实只读；paid 仅表示外部支付状态，不代表已发货"]}}
