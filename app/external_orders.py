"""统一外部订单事实和每日销量聚合服务。

订单事实只更新外部域；不会调用任何内部库存写入路径。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import json

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.connectors import ExternalOrderRecord
from app.db import (
    DailySkuSale,
    ExternalAccount,
    ExternalOrder,
    ExternalOrderLine,
    ExternalProductMapping,
    ProductSku,
)


@dataclass
class SalesImportStats:
    total: int = 0
    inserted: int = 0
    updated: int = 0
    no_op: int = 0
    conflict: int = 0
    stale: int = 0
    affected_dates: list[str] | None = None
    sync_run_id: str | None = None


def ensure_external_account(
    db: Session,
    *,
    workspace_id: int,
    platform: str,
    account_ref: str,
    store_ref: str = "default",
    source_mode: str = "mock",
    simulated: bool = True,
) -> ExternalAccount:
    if not workspace_id:
        raise ValueError("WORKSPACE_CONTEXT_REQUIRED")
    store_ref = (store_ref or "default").strip() or "default"
    account = db.scalar(select(ExternalAccount).where(
        ExternalAccount.workspace_id == workspace_id,
        ExternalAccount.platform == platform,
        ExternalAccount.account_ref == account_ref,
        ExternalAccount.store_ref == store_ref,
    ))
    if account is None:
        account = ExternalAccount(
            workspace_id=workspace_id, platform=platform, account_ref=account_ref,
            store_ref=store_ref, source_mode=source_mode, simulated=simulated,
        )
        db.add(account)
        db.flush()
    elif account.status != "active":
        raise ValueError("EXTERNAL_ACCOUNT_INACTIVE")
    return account


def set_product_mapping(
    db: Session,
    *,
    workspace_id: int,
    external_account_id: int,
    external_sku: str,
    internal_sku_id: int | None,
) -> ExternalProductMapping:
    account = db.scalar(select(ExternalAccount).where(
        ExternalAccount.id == external_account_id,
        ExternalAccount.workspace_id == workspace_id,
    ))
    if account is None:
        raise ValueError("EXTERNAL_ACCOUNT_NOT_FOUND")
    sku = None
    if internal_sku_id is not None:
        sku = db.scalar(select(ProductSku).where(
            ProductSku.id == internal_sku_id,
            ProductSku.workspace_id == workspace_id,
            ProductSku.is_active.is_(True),
        ))
        if sku is None:
            raise ValueError("SKU_NOT_FOUND")
    mapping = db.scalar(select(ExternalProductMapping).where(
        ExternalProductMapping.workspace_id == workspace_id,
        ExternalProductMapping.external_account_id == external_account_id,
        ExternalProductMapping.external_sku == external_sku,
    ))
    if mapping is None:
        mapping = ExternalProductMapping(
            workspace_id=workspace_id, external_account_id=external_account_id,
            external_sku=external_sku, internal_sku_id=internal_sku_id,
            mapping_status="mapped" if sku else "unmapped", source="manual",
        )
        db.add(mapping)
    else:
        mapping.internal_sku_id = internal_sku_id
        mapping.mapping_status = "mapped" if sku else "unmapped"
    db.commit()
    db.refresh(mapping)
    rebuild_daily_sales(db, workspace_id=workspace_id, external_account_id=external_account_id, external_sku=external_sku)
    return mapping


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(__import__("datetime").timezone.utc).replace(tzinfo=None)
    return value


def _is_sale(order: ExternalOrder) -> bool:
    return order.order_status in {"paid", "fulfilled", "completed"}


def _line_net(line: ExternalOrderLine) -> tuple[int, Decimal]:
    qty = max(line.ordered_qty - line.cancelled_qty - line.refunded_qty, 0)
    amount = max(Decimal(line.gross_amount or 0) - Decimal(line.refund_amount or 0), Decimal("0"))
    return qty, amount


def _rebuild_key(
    db: Session,
    *,
    workspace_id: int,
    external_account_id: int,
    external_sku: str,
    sales_date: date | None = None,
) -> None:
    account = db.get(ExternalAccount, external_account_id)
    if account is None or account.workspace_id != workspace_id:
        raise ValueError("EXTERNAL_ACCOUNT_NOT_FOUND")
    mapping = db.scalar(select(ExternalProductMapping).where(
        ExternalProductMapping.workspace_id == workspace_id,
        ExternalProductMapping.external_account_id == external_account_id,
        ExternalProductMapping.external_sku == external_sku,
    ))
    internal_sku_id = mapping.internal_sku_id if mapping and mapping.mapping_status == "mapped" else None
    order_query = select(ExternalOrder).where(
        ExternalOrder.workspace_id == workspace_id,
        ExternalOrder.external_account_id == external_account_id,
        ExternalOrder.store_ref == account.store_ref,
    )
    orders = db.scalars(order_query).all()
    groups: dict[date, dict[str, object]] = defaultdict(lambda: {
        "gross_qty": 0, "cancelled_qty": 0, "refunded_qty": 0, "net_qty": 0,
        "gross_amount": Decimal("0"), "refund_amount": Decimal("0"), "net_amount": Decimal("0"),
        "orders": set(), "complete": True,
    })
    for order in orders:
        day = order.external_created_at.date()
        if sales_date is not None and day != sales_date:
            continue
        for line in db.scalars(select(ExternalOrderLine).where(
            ExternalOrderLine.workspace_id == workspace_id,
            ExternalOrderLine.external_order_id == order.id,
            ExternalOrderLine.external_sku == external_sku,
        )).all():
            item = groups[day]
            item["gross_qty"] = int(item["gross_qty"]) + (line.ordered_qty if _is_sale(order) else 0)
            item["cancelled_qty"] = int(item["cancelled_qty"]) + (line.cancelled_qty if _is_sale(order) else line.ordered_qty)
            item["refunded_qty"] = int(item["refunded_qty"]) + (line.refunded_qty if _is_sale(order) else 0)
            net_qty, net_amount = _line_net(line) if _is_sale(order) else (0, Decimal("0"))
            item["net_qty"] = int(item["net_qty"]) + net_qty
            item["gross_amount"] = Decimal(item["gross_amount"]) + (Decimal(line.gross_amount or 0) if _is_sale(order) else Decimal("0"))
            item["refund_amount"] = Decimal(item["refund_amount"]) + (Decimal(line.refund_amount or 0) if _is_sale(order) else Decimal("0"))
            item["net_amount"] = Decimal(item["net_amount"]) + net_amount
            item["orders"].add(order.id)
            if order.data_completeness != "complete" or line.data_completeness != "complete":
                item["complete"] = False
    existing_filters = [
        DailySkuSale.workspace_id == workspace_id,
        DailySkuSale.external_account_id == external_account_id,
        DailySkuSale.external_sku == external_sku,
    ]
    if sales_date is not None:
        existing_filters.append(DailySkuSale.sales_date == sales_date)
    db.execute(delete(DailySkuSale).where(*existing_filters))
    for day, item in groups.items():
        db.add(DailySkuSale(
            workspace_id=workspace_id, external_account_id=external_account_id,
            platform=account.platform, account_ref=account.account_ref, store_ref=account.store_ref,
            external_sku=external_sku, internal_sku_id=internal_sku_id, sales_date=day,
            gross_qty=int(item["gross_qty"]), cancelled_qty=int(item["cancelled_qty"]),
            refunded_qty=int(item["refunded_qty"]), net_qty=int(item["net_qty"]),
            gross_amount=Decimal(item["gross_amount"]), refund_amount=Decimal(item["refund_amount"]),
            net_amount=Decimal(item["net_amount"]), order_count=len(item["orders"]),
            data_completeness="complete" if item["complete"] else "partial",
        ))


def rebuild_daily_sales(
    db: Session,
    *,
    workspace_id: int,
    external_account_id: int,
    external_sku: str,
    sales_date: date | None = None,
) -> None:
    _rebuild_key(db, workspace_id=workspace_id, external_account_id=external_account_id, external_sku=external_sku, sales_date=sales_date)
    db.commit()


def ingest_orders(
    db: Session,
    records: list[ExternalOrderRecord],
    *,
    workspace_id: int,
    sync_run_id: str | None = None,
) -> SalesImportStats:
    if not workspace_id:
        raise ValueError("WORKSPACE_CONTEXT_REQUIRED")
    stats = SalesImportStats(total=len(records), sync_run_id=sync_run_id, affected_dates=[])
    touched: set[tuple[int, str]] = set()
    try:
        for record in records:
            account = ensure_external_account(
                db, workspace_id=workspace_id, platform=record.platform,
                account_ref=record.account_ref, store_ref=record.store_ref,
                source_mode=record.source_mode, simulated=record.simulated,
            )
            existing = db.scalar(select(ExternalOrder).where(
                ExternalOrder.workspace_id == workspace_id,
                ExternalOrder.external_account_id == account.id,
                ExternalOrder.external_order_no == record.external_order_no,
            ))
            if existing is not None:
                newer = (record.event_version is not None and (existing.event_version is None or record.event_version > existing.event_version)) or (
                    record.event_version is None and _utc_naive(record.external_updated_at) > _utc_naive(existing.external_updated_at)
                )
                if existing.payload_hash == record.payload_hash:
                    stats.no_op += 1
                    continue
                if not newer:
                    stats.stale += 1
                    continue
                old_day = existing.external_created_at.date()
                for old_line in db.scalars(select(ExternalOrderLine).where(ExternalOrderLine.external_order_id == existing.id)).all():
                    touched.add((account.id, old_line.external_sku))
                db.execute(delete(ExternalOrderLine).where(ExternalOrderLine.external_order_id == existing.id))
                order = existing
                stats.updated += 1
                touched.update((account.id, line.external_sku) for line in record.lines)
            else:
                order = ExternalOrder(
                    workspace_id=workspace_id, external_account_id=account.id,
                    platform=record.platform, account_ref=record.account_ref, store_ref=record.store_ref,
                    external_order_no=record.external_order_no, order_status=record.order_status,
                    external_created_at=record.external_created_at, paid_at=record.paid_at,
                    external_updated_at=record.external_updated_at, event_version=record.event_version,
                    gross_amount=record.gross_amount, refund_amount=record.refund_amount, currency=record.currency,
                    payload_json=json.dumps(record.as_dict(), ensure_ascii=False, sort_keys=True),
                    payload_hash=record.payload_hash, idempotency_key=record.idempotency_key,
                    source_mode=record.source_mode, simulated=record.simulated, sync_run_id=sync_run_id,
                    data_completeness=record.data_completeness, status_reason=record.status_reason,
                )
                db.add(order)
                db.flush()
                stats.inserted += 1
            order.order_status = record.order_status
            order.external_created_at = record.external_created_at
            order.paid_at = record.paid_at
            order.external_updated_at = record.external_updated_at
            order.event_version = record.event_version
            order.gross_amount = record.gross_amount
            order.refund_amount = record.refund_amount
            order.payload_json = json.dumps(record.as_dict(), ensure_ascii=False, sort_keys=True)
            order.payload_hash = record.payload_hash
            order.idempotency_key = record.idempotency_key
            order.sync_run_id = sync_run_id
            order.data_completeness = record.data_completeness
            order.status_reason = record.status_reason
            for line in record.lines:
                mapping = db.scalar(select(ExternalProductMapping).where(
                    ExternalProductMapping.workspace_id == workspace_id,
                    ExternalProductMapping.external_account_id == account.id,
                    ExternalProductMapping.external_sku == line.external_sku,
                ))
                internal_id = mapping.internal_sku_id if mapping and mapping.mapping_status == "mapped" else None
                db.add(ExternalOrderLine(
                    workspace_id=workspace_id, external_order_id=order.id,
                    external_line_id=line.external_line_id, external_sku=line.external_sku,
                    internal_sku_id=internal_id, ordered_qty=line.ordered_qty,
                    cancelled_qty=line.cancelled_qty, refunded_qty=line.refunded_qty,
                    gross_amount=line.gross_amount, refund_amount=line.refund_amount,
                    currency=line.currency, mapping_status="mapped" if internal_id else "unmapped",
                    data_completeness=record.data_completeness, payload_hash=line.payload_hash,
                ))
                touched.add((account.id, line.external_sku))
            stats.affected_dates.append(record.external_created_at.date().isoformat())
        db.flush()
        for account_id, external_sku in touched:
            _rebuild_key(db, workspace_id=workspace_id, external_account_id=account_id, external_sku=external_sku)
        db.commit()
    except Exception:
        db.rollback()
        raise
    stats.affected_dates = sorted(set(stats.affected_dates or []))
    return stats


def list_daily_sales(
    db: Session,
    *,
    workspace_id: int,
    platform: str | None = None,
    account_ref: str | None = None,
    store_ref: str | None = None,
    external_sku: str | None = None,
    internal_sku_id: int | None = None,
    start: date | None = None,
    end: date | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[DailySkuSale], int]:
    filters = [DailySkuSale.workspace_id == workspace_id]
    for condition in (
        DailySkuSale.platform == platform if platform else None,
        DailySkuSale.account_ref == account_ref if account_ref else None,
        DailySkuSale.store_ref == store_ref if store_ref else None,
        DailySkuSale.external_sku == external_sku if external_sku else None,
        DailySkuSale.internal_sku_id == internal_sku_id if internal_sku_id else None,
        DailySkuSale.sales_date >= start if start else None,
        DailySkuSale.sales_date <= end if end else None,
    ):
        if condition is not None:
            filters.append(condition)
    from sqlalchemy import func
    total = db.scalar(select(func.count(DailySkuSale.id)).where(*filters)) or 0
    rows = db.scalars(select(DailySkuSale).where(*filters).order_by(DailySkuSale.sales_date.desc(), DailySkuSale.id.desc()).offset(offset).limit(limit)).all()
    return rows, int(total)
