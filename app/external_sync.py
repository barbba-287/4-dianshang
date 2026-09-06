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


RESOURCE_SCHEMA_VERSION = 1


@dataclass
class ImportStats:
    inserted: int = 0
    no_op: int = 0
    conflict: int = 0
    total: int = 0
    updated: int = 0
    stale: int = 0
    snapshot_ids: list[int] | None = None
    sync_run_id: str | None = None
    sync_status: str | None = None
    affected_dates: list[str] | None = None


def _resource_entry(*, status: str = "pending", execution: str = "attempted") -> dict:
    return {
        "status": status,
        "execution": execution,
        "total": 0,
        "inserted": 0,
        "updated": 0,
        "no_op": 0,
        "conflict": 0,
        "stale": 0,
        "snapshot_ids": [],
        "error_code": None,
        "error_message": None,
        "retryable": False,
    }


def resource_status(run: ExternalSyncRun) -> dict:
    try:
        value = json.loads(run.resource_status_json or "{}")
    except (TypeError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    value.setdefault("schema_version", RESOURCE_SCHEMA_VERSION)
    return value


def retryable_resources(run: ExternalSyncRun) -> list[str]:
    try:
        value = json.loads(run.retryable_resources_json or "[]")
    except (TypeError, ValueError):
        value = []
    names = {item for item in value if isinstance(item, str)} if isinstance(value, list) else set()
    # A carried-forward failure remains actionable on a later retry.  It is
    # deliberately kept out of the child's attempted-resource status, but it
    # must not disappear from the compensation chain.
    for name, item in resource_status(run).items():
        if name == "schema_version" or not isinstance(item, dict):
            continue
        if item.get("retryable") and (
            item.get("status") == "failed"
            or item.get("prior_status") == "failed"
        ):
            names.add(name)
    return sorted(names)


def _save_resource_status(db: Session, run: ExternalSyncRun, statuses: dict) -> None:
    statuses["schema_version"] = RESOURCE_SCHEMA_VERSION
    run.resource_status_json = json.dumps(statuses, ensure_ascii=False, sort_keys=True)
    retryable = [name for name, item in statuses.items() if name != "schema_version" and isinstance(item, dict) and item.get("status") == "failed" and item.get("retryable")]
    run.retryable_resources_json = json.dumps(sorted(retryable), ensure_ascii=False)
    run.heartbeat_at = datetime.utcnow()
    db.commit()
    db.refresh(run)


def initialize_resources(db: Session, run: ExternalSyncRun, resources: list[str], *, carried_forward: dict | None = None) -> ExternalSyncRun:
    statuses = {"schema_version": RESOURCE_SCHEMA_VERSION}
    if carried_forward:
        for name, item in carried_forward.items():
            if name == "schema_version" or not isinstance(item, dict):
                continue
            copied = dict(item)
            copied["prior_status"] = copied.get("status")
            copied["status"] = "carried_forward"
            copied["execution"] = "carried_forward"
            statuses[name] = copied
    for name in resources:
        statuses[name] = _resource_entry()
    _save_resource_status(db, run, statuses)
    return run


def mark_resource_running(db: Session, run: ExternalSyncRun, resource: str) -> ExternalSyncRun:
    statuses = resource_status(run)
    entry = dict(statuses.get(resource) or _resource_entry())
    entry["status"] = "running"
    entry["execution"] = "attempted"
    entry["error_code"] = None
    entry["error_message"] = None
    statuses[resource] = entry
    _save_resource_status(db, run, statuses)
    return run


def _stats_dict(stats: ImportStats) -> dict:
    return {
        "status": "succeeded",
        "execution": "attempted",
        "total": stats.total,
        "inserted": stats.inserted,
        "updated": getattr(stats, "updated", 0),
        "no_op": stats.no_op,
        "conflict": stats.conflict,
        "stale": getattr(stats, "stale", 0),
        "snapshot_ids": getattr(stats, "snapshot_ids", None) or [],
        "error_code": None,
        "error_message": None,
        "retryable": False,
    }


def mark_resource_succeeded(db: Session, run: ExternalSyncRun, resource: str, stats: ImportStats) -> ExternalSyncRun:
    statuses = resource_status(run)
    statuses[resource] = _stats_dict(stats)
    _save_resource_status(db, run, statuses)
    return run


def is_retryable_error(exc: Exception) -> bool:
    if hasattr(exc, "retryable"):
        return bool(getattr(exc, "retryable"))
    code = str(getattr(exc, "code", ""))
    return code in {"TEMPORARY_IO_ERROR", "TIMEOUT", "RATE_LIMITED", "UPSTREAM_5XX"}


def mark_resource_failed(db: Session, run: ExternalSyncRun, resource: str, exc: Exception, *, retryable: bool | None = None) -> ExternalSyncRun:
    statuses = resource_status(run)
    entry = dict(statuses.get(resource) or _resource_entry())
    entry.update({
        "status": "failed",
        "execution": "attempted",
        "error_code": str(getattr(exc, "code", type(exc).__name__))[:64],
        "error_message": str(getattr(exc, "message", exc))[:500],
        "retryable": is_retryable_error(exc) if retryable is None else retryable,
    })
    statuses[resource] = entry
    _save_resource_status(db, run, statuses)
    return run


def calculate_overall_status(run: ExternalSyncRun) -> str:
    entries = [item for name, item in resource_status(run).items() if name != "schema_version" and isinstance(item, dict)]
    statuses = []
    for item in entries:
        status = item.get("status")
        if item.get("execution") == "carried_forward":
            status = item.get("prior_status", status)
        statuses.append(status)
    if not statuses:
        return run.status
    if any(status in {"running", "pending"} for status in statuses):
        return "running"
    successful = sum(status == "succeeded" for status in statuses)
    failed = sum(status == "failed" for status in statuses)
    if failed and successful:
        return "partial"
    if failed:
        return "failed"
    return "succeeded" if successful == len(statuses) else "failed"


def finalize_resource_run(db: Session, run: ExternalSyncRun, *, error: Exception | None = None) -> ExternalSyncRun:
    statuses = resource_status(run)
    totals = {key: 0 for key in ("total", "inserted", "updated", "no_op", "conflict", "stale")}
    for name, item in statuses.items():
        if name == "schema_version" or not isinstance(item, dict):
            continue
        status = item.get("status")
        if item.get("execution") == "carried_forward":
            status = item.get("prior_status", status)
        for key in totals:
            totals[key] += int(item.get(key) or 0)
    run.status = calculate_overall_status(run)
    run.finished_at = datetime.utcnow()
    run.heartbeat_at = run.finished_at
    for key, value in totals.items():
        setattr(run, key, value)
    if error is not None:
        run.error_code = str(getattr(error, "code", type(error).__name__))[:64]
        run.error_message = str(getattr(error, "message", error))[:500]
    db.commit()
    db.refresh(run)
    return run


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
    attempt: int = 1,
    retry_of_run_id: str | None = None,
    retry_idempotency_key: str | None = None,
    retry_payload_hash: str | None = None,
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
        attempt=attempt,
        retry_of_run_id=retry_of_run_id,
        retry_idempotency_key=retry_idempotency_key,
        retry_payload_hash=retry_payload_hash,
        resource_status_json=json.dumps({"schema_version": RESOURCE_SCHEMA_VERSION}),
        retryable_resources_json="[]",
        heartbeat_at=datetime.utcnow(),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def complete_sync_run(db: Session, run: ExternalSyncRun, stats: ImportStats) -> ExternalSyncRun:
    statuses = resource_status(run)
    resource_names = [name for name in statuses if name != "schema_version"]
    if resource_names:
        # Resource-aware callers already recorded per-resource statistics.
        return finalize_resource_run(db, run)
    run.status = "succeeded"
    run.finished_at = datetime.utcnow()
    run.heartbeat_at = run.finished_at
    run.total = stats.total
    run.inserted = stats.inserted
    run.updated = stats.updated
    run.no_op = stats.no_op
    run.conflict = stats.conflict
    run.stale = stats.stale
    db.commit()
    db.refresh(run)
    return run


def fail_sync_run(db: Session, run: ExternalSyncRun, exc: Exception) -> ExternalSyncRun:
    if resource_status(run).keys() - {"schema_version"}:
        return finalize_resource_run(db, run, error=exc)
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


def validate_inventory_batch_scope(records: list[InventorySnapshotRecord]) -> tuple[str, str | None, str | None]:
    if not records:
        raise ValueError("EXTERNAL_RECORDS_EMPTY")
    first = records[0]
    account_ref = first.account_ref
    store_ref = first.store_ref
    warehouse_ref = first.warehouse_ref
    for record in records:
        if (
            record.account_ref != account_ref
            or (record.store_ref or "default") != (store_ref or "default")
            or (record.warehouse_ref or "__default_warehouse__") != (warehouse_ref or "__default_warehouse__")
        ):
            raise ValueError("MIXED_EXTERNAL_SCOPE")
    return account_ref, store_ref, warehouse_ref


def validate_event_batch_scope(events: list[CanonicalEvent]) -> str:
    if not events:
        raise ValueError("EXTERNAL_RECORDS_EMPTY")
    account_ref = events[0].account_ref
    if any(event.account_ref != account_ref for event in events):
        raise ValueError("MIXED_EXTERNAL_ACCOUNT")
    return account_ref


def ingest_inventory(
    db: Session,
    records: list[InventorySnapshotRecord],
    *,
    workspace_id: int | None = None,
    sync_run_id: str | None = None,
) -> ImportStats:
    stats = ImportStats(total=len(records), snapshot_ids=[], sync_run_id=sync_run_id, sync_status="running" if sync_run_id else None)
    validate_inventory_batch_scope(records)
    if sync_run_id:
        _set_run_metadata(db, ExternalInventorySnapshot, sync_run_id, workspace_id)
    try:
        for record in records:
            store_ref_key = record.store_ref or "__default_store__"
            warehouse_ref_key = record.warehouse_ref or "__default_warehouse__"
            existing = db.scalar(select(ExternalInventorySnapshot).where(
                ExternalInventorySnapshot.platform == record.platform,
                ExternalInventorySnapshot.account_ref == record.account_ref,
                ExternalInventorySnapshot.store_ref_key == store_ref_key,
                ExternalInventorySnapshot.warehouse_ref_key == warehouse_ref_key,
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
                store_ref_key=store_ref_key,
                warehouse_ref_key=warehouse_ref_key,
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
    validate_event_batch_scope(events)
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
