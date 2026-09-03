"""外部平台离线库存与事件的导入服务。"""

from __future__ import annotations

from dataclasses import dataclass
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors import CanonicalEvent, InventorySnapshotRecord
from app.db import ExternalEventInbox, ExternalInventorySnapshot


@dataclass
class ImportStats:
    inserted: int = 0
    no_op: int = 0
    conflict: int = 0
    total: int = 0
    snapshot_ids: list[int] | None = None


def ingest_inventory(db: Session, records: list[InventorySnapshotRecord]) -> ImportStats:
    stats = ImportStats(total=len(records), snapshot_ids=[])
    try:
        for record in records:
            existing = db.scalar(select(ExternalInventorySnapshot).where(
                ExternalInventorySnapshot.platform == record.platform,
                ExternalInventorySnapshot.account_ref == record.account_ref,
                ExternalInventorySnapshot.idempotency_key == record.idempotency_key,
            ))
            if existing is not None:
                if existing.payload_hash == record.payload_hash:
                    stats.no_op += 1
                else:
                    stats.conflict += 1
                continue
            snapshot = ExternalInventorySnapshot(
                platform=record.platform,
                account_ref=record.account_ref,
                store_ref=record.store_ref,
                marketplace=record.marketplace,
                warehouse_ref=record.warehouse_ref,
                external_sku=record.external_sku,
                internal_sku_code=record.internal_sku_code,
                asin=record.asin,
                available_qty=record.available_qty,
                reserved_qty=record.reserved_qty,
                inbound_qty=record.inbound_qty,
                as_of=record.as_of,
                received_at=record.received_at,
                payload_hash=record.payload_hash,
                idempotency_key=record.idempotency_key,
                raw_ref=record.raw_ref,
                source_mode=record.source_mode,
                simulated=record.simulated,
            )
            db.add(snapshot)
            db.flush()
            stats.snapshot_ids.append(snapshot.id)
            stats.inserted += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return stats


def ingest_events(db: Session, events: list[CanonicalEvent]) -> ImportStats:
    stats = ImportStats(total=len(events))
    try:
        for event in events:
            existing = db.scalar(select(ExternalEventInbox).where(
                ExternalEventInbox.platform == event.platform,
                ExternalEventInbox.account_ref == event.account_ref,
                ExternalEventInbox.idempotency_key == event.idempotency_key,
            ))
            if existing is not None:
                if existing.payload_hash == event.payload_hash:
                    stats.no_op += 1
                else:
                    existing.status = "conflict"
                    existing.error_code = "PAYLOAD_HASH_CONFLICT"
                    stats.conflict += 1
                continue
            db.add(ExternalEventInbox(
                platform=event.platform,
                account_ref=event.account_ref,
                external_event_id=event.external_event_id,
                event_type=event.event_type,
                event_version=event.event_version,
                external_object_no=event.external_object_no,
                occurred_at=event.occurred_at,
                received_at=event.received_at,
                payload_hash=event.payload_hash,
                idempotency_key=event.idempotency_key,
                payload_json=json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
                raw_ref=event.raw_ref,
                source_mode=event.source_mode,
                simulated=event.simulated,
            ))
            stats.inserted += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return stats


def reconcile_inventory(
    db: Session,
    snapshot_id: int,
    *,
    sku_mapping: dict[str, int] | None = None,
    warehouse_mapping: dict[str, int] | None = None,
) -> list[dict]:
    """只读对账：返回结果，不写入内部库存。"""
    from app.db import InventoryBalance, ProductSku, Warehouse

    snapshot = db.get(ExternalInventorySnapshot, snapshot_id)
    if snapshot is None:
        raise ValueError("EXTERNAL_SNAPSHOT_NOT_FOUND")
    sku_mapping = sku_mapping or {}
    warehouse_mapping = warehouse_mapping or {}
    sku_id = sku_mapping.get(snapshot.external_sku)
    if sku_id is None and snapshot.internal_sku_code:
        sku = db.scalar(select(ProductSku).where(ProductSku.sku_code == snapshot.internal_sku_code))
        sku_id = sku.id if sku else None
    warehouse_id = warehouse_mapping.get(snapshot.warehouse_ref or "")
    if warehouse_id is None and snapshot.warehouse_ref:
        warehouse = db.scalar(select(Warehouse).where(Warehouse.code == snapshot.warehouse_ref))
        warehouse_id = warehouse.id if warehouse else None
    balance = db.scalar(select(InventoryBalance).where(
        InventoryBalance.sku_id == sku_id,
        InventoryBalance.warehouse_id == warehouse_id,
    )) if sku_id and warehouse_id else None
    internal_qty = balance.on_hand_qty if balance else None
    if sku_id is None or warehouse_id is None:
        classification = "unmapped"
        reason = "未找到内部 SKU 或仓库映射"
    elif internal_qty is None:
        classification = "internal_missing"
        reason = "内部没有对应库存余额"
    elif internal_qty == snapshot.available_qty:
        classification = "matched"
        reason = None
    else:
        classification = "mismatch"
        reason = "外部可售数量与内部在库数量不同"
    return [{
        "snapshot_id": snapshot.id,
        "platform": snapshot.platform,
        "external_sku": snapshot.external_sku,
        "internal_sku_code": snapshot.internal_sku_code,
        "sku_id": sku_id,
        "warehouse_id": warehouse_id,
        "external_available_qty": snapshot.available_qty,
        "internal_on_hand_qty": internal_qty,
        "delta": snapshot.available_qty - internal_qty if internal_qty is not None else None,
        "classification": classification,
        "reason": reason,
    }]
