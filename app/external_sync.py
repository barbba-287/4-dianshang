"""外部平台离线库存与事件的导入服务。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from uuid import uuid4
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors import CanonicalEvent, InventorySnapshotRecord
from app.db import ExternalEventInbox, ExternalInventorySnapshot, ExternalSyncRun

@dataclass
class ImportStats:
    inserted: int = 0
    no_op: int = 0
    conflict: int = 0
    total: int = 0
    snapshot_ids: list[int] | None = None
    sync_run_id: str | None = None
    sync_status: str | None = None


def begin_sync_run(
    db: Session,
    *,
    workspace_id: int,
    platform: str,
    sync_type: str,
    account_ref: str | None = None,
    store_ref: str | None = None,
    source_mode: str = "mock",
    simulated: bool = True,
) -> ExternalSyncRun:
    run = ExternalSyncRun(
        workspace_id=workspace_id,
        run_id=uuid4().hex,
        platform=platform,
        account_ref=account_ref,
        store_ref=store_ref,
        sync_type=sync_type,
        source_mode=source_mode,
        simulated=simulated,
        status="running",
        heartbeat_at=datetime.utcnow(),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def complete_sync_run(db: Session, run: ExternalSyncRun, stats: ImportStats) -> ExternalSyncRun:
    run.status = "succeeded"
    run.finished_at = datetime.utcnow()
    run.heartbeat_at = run.finished_at
    run.total = stats.total
    run.inserted = stats.inserted
    run.no_op = stats.no_op
    run.conflict = stats.conflict
    db.commit()
    db.refresh(run)
    return run


def fail_sync_run(db: Session, run: ExternalSyncRun, exc: Exception) -> ExternalSyncRun:
    run.status = "failed"
    run.finished_at = datetime.utcnow()
    run.heartbeat_at = run.finished_at
    run.error_code = str(getattr(exc, "code", type(exc).__name__))[:64]
    run.error_message = str(getattr(exc, "message", exc))[:500]
    db.commit()
    db.refresh(run)
    return run


def _set_run_metadata(db: Session, model, sync_run_id: str | None, workspace_id: int | None):
    if sync_run_id is None:
        return
    run = db.scalar(select(ExternalSyncRun).where(
        ExternalSyncRun.run_id == sync_run_id,
        ExternalSyncRun.workspace_id == workspace_id,
    ))
    if run is None:
        raise ValueError("SYNC_RUN_NOT_FOUND")

def ingest_inventory(
    db: Session,
    records: list[InventorySnapshotRecord],
    *,
    workspace_id: int | None = None,
    sync_run_id: str | None = None,
) -> ImportStats:
    stats = ImportStats(total=len(records), snapshot_ids=[], sync_run_id=sync_run_id, sync_status="running" if sync_run_id else None)
    if sync_run_id:
        _set_run_metadata(db, ExternalInventorySnapshot, sync_run_id, workspace_id)
    try:
        for record in records:
            existing = db.scalar(select(ExternalInventorySnapshot).where(
                ExternalInventorySnapshot.platform == record.platform,
                ExternalInventorySnapshot.account_ref == record.account_ref,
                ExternalInventorySnapshot.idempotency_key == record.idempotency_key,
                ExternalInventorySnapshot.workspace_id == workspace_id,
            ))
            if existing is not None:
                if existing.payload_hash == record.payload_hash:
                    stats.no_op += 1
                else:
                    stats.conflict += 1
                continue
            snapshot = ExternalInventorySnapshot(
                workspace_id=workspace_id,
                sync_run_id=sync_run_id,
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
    if sync_run_id:
        stats.sync_status = "succeeded"
    return stats


def ingest_events(
    db: Session,
    events: list[CanonicalEvent],
    *,
    workspace_id: int | None = None,
    sync_run_id: str | None = None,
) -> ImportStats:
    stats = ImportStats(total=len(events), sync_run_id=sync_run_id, sync_status="running" if sync_run_id else None)
    if sync_run_id:
        _set_run_metadata(db, ExternalEventInbox, sync_run_id, workspace_id)
    try:
        for event in events:
            existing = db.scalar(select(ExternalEventInbox).where(
                ExternalEventInbox.platform == event.platform,
                ExternalEventInbox.account_ref == event.account_ref,
                ExternalEventInbox.idempotency_key == event.idempotency_key,
                ExternalEventInbox.workspace_id == workspace_id,
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
                workspace_id=workspace_id,
                sync_run_id=sync_run_id,
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
    if sync_run_id:
        stats.sync_status = "succeeded"
    return stats


def snapshot_freshness(
    db: Session,
    *,
    workspace_id: int,
    now: datetime | None = None,
    stale_after_seconds: int = 86400,
    platform: str | None = None,
) -> list[dict]:
    """按平台/账户/店铺/外部仓库来源计算最新快照新鲜度。"""
    from datetime import timezone

    now = now or datetime.utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    filters = [ExternalInventorySnapshot.workspace_id == workspace_id]
    if platform is not None:
        filters.append(ExternalInventorySnapshot.platform == platform)
    rows = db.scalars(
        select(ExternalInventorySnapshot)
        .where(*filters)
        .order_by(
            ExternalInventorySnapshot.platform,
            ExternalInventorySnapshot.account_ref,
            ExternalInventorySnapshot.store_ref,
            ExternalInventorySnapshot.warehouse_ref,
            ExternalInventorySnapshot.as_of.desc(),
            ExternalInventorySnapshot.received_at.desc(),
            ExternalInventorySnapshot.id.desc(),
        )
    ).all()
    groups: dict[tuple, list] = {}
    for row in rows:
        key = (row.platform, row.account_ref, row.store_ref, row.marketplace, row.warehouse_ref)
        groups.setdefault(key, []).append(row)
    result = []
    for (item_platform, account_ref, store_ref, marketplace, warehouse_ref), items in groups.items():
        latest = items[0]
        observed = latest.as_of
        received = latest.received_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        age = (now - observed).total_seconds()
        state = "clock_skew" if age < 0 else "fresh" if age <= stale_after_seconds else "stale"
        result.append({
            "platform": item_platform,
            "account_ref": account_ref,
            "store_ref": store_ref,
            "marketplace": marketplace,
            "warehouse_ref": warehouse_ref,
            "latest_snapshot_id": latest.id,
            "latest_as_of": latest.as_of,
            "latest_received_at": latest.received_at,
            "age_seconds": max(0, int(age)),
            "ingest_delay_seconds": int((received - observed).total_seconds()),
            "threshold_seconds": stale_after_seconds,
            "state": state,
            "snapshot_count": len(items),
            "simulated": latest.simulated,
            "source_mode": latest.source_mode,
        })
    return result


def reconcile_inventory(
    db: Session,
    snapshot_id: int,
    *,
    workspace_id: int | None = None,
    sku_mapping: dict[str, int] | None = None,
    warehouse_mapping: dict[str, int] | None = None,
) -> list[dict]:
    """只读对账：返回结果，不写入内部库存。"""
    from app.db import InventoryBalance, ProductSku, Warehouse

    snapshot = db.scalar(
        select(ExternalInventorySnapshot).where(
            ExternalInventorySnapshot.id == snapshot_id,
            ExternalInventorySnapshot.workspace_id == workspace_id,
        )
    )
    if snapshot is None:
        raise ValueError("EXTERNAL_SNAPSHOT_NOT_FOUND")
    sku_mapping = sku_mapping or {}
    warehouse_mapping = warehouse_mapping or {}
    sku_id = sku_mapping.get(snapshot.external_sku)
    if sku_id is None and snapshot.internal_sku_code:
        sku = db.scalar(select(ProductSku).where(ProductSku.sku_code == snapshot.internal_sku_code, ProductSku.workspace_id == workspace_id))
        sku_id = sku.id if sku else None
    warehouse_id = warehouse_mapping.get(snapshot.warehouse_ref or "")
    if warehouse_id is None and snapshot.warehouse_ref:
        warehouse = db.scalar(select(Warehouse).where(Warehouse.code == snapshot.warehouse_ref, Warehouse.workspace_id == workspace_id))
        warehouse_id = warehouse.id if warehouse else None
    balance = db.scalar(select(InventoryBalance).where(
        InventoryBalance.workspace_id == workspace_id,
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
