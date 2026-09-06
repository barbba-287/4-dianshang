"""库存策略与告警生命周期服务。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import InventoryAlert, InventoryBalance, InventoryPolicy, ProductSku, Warehouse


def upsert_inventory_policy(
    db: Session,
    *,
    workspace_id: int,
    warehouse_id: int,
    sku_id: int,
    safety_stock_qty: int,
    reorder_point_qty: int,
) -> InventoryPolicy:
    if safety_stock_qty < 0 or reorder_point_qty < safety_stock_qty:
        raise ValueError("INVALID_INVENTORY_POLICY")
    warehouse = db.scalar(select(Warehouse).where(Warehouse.id == warehouse_id, Warehouse.workspace_id == workspace_id, Warehouse.is_active.is_(True)))
    sku = db.scalar(select(ProductSku).where(ProductSku.id == sku_id, ProductSku.workspace_id == workspace_id, ProductSku.is_active.is_(True)))
    if warehouse is None:
        raise ValueError("WAREHOUSE_NOT_FOUND")
    if sku is None:
        raise ValueError("SKU_NOT_FOUND")
    policy = db.scalar(select(InventoryPolicy).where(InventoryPolicy.workspace_id == workspace_id, InventoryPolicy.warehouse_id == warehouse_id, InventoryPolicy.sku_id == sku_id))
    if policy is None:
        policy = InventoryPolicy(workspace_id=workspace_id, warehouse_id=warehouse_id, sku_id=sku_id, safety_stock_qty=safety_stock_qty, reorder_point_qty=reorder_point_qty)
        db.add(policy)
    else:
        policy.safety_stock_qty = safety_stock_qty
        policy.reorder_point_qty = reorder_point_qty
    db.commit()
    db.refresh(policy)
    return policy


def refresh_low_stock_alerts(db: Session, *, workspace_id: int, warehouse_id: int | None = None) -> list[InventoryAlert]:
    filters = [InventoryPolicy.workspace_id == workspace_id]
    if warehouse_id is not None:
        filters.append(InventoryPolicy.warehouse_id == warehouse_id)
    policies = db.scalars(select(InventoryPolicy).where(*filters)).all()
    now = datetime.utcnow()
    for policy in policies:
        balance = db.scalar(select(InventoryBalance).where(InventoryBalance.workspace_id == workspace_id, InventoryBalance.warehouse_id == policy.warehouse_id, InventoryBalance.sku_id == policy.sku_id))
        on_hand = int(balance.on_hand_qty) if balance is not None else 0
        key = f"low_stock:{policy.warehouse_id}:{policy.sku_id}"
        alert = db.scalar(select(InventoryAlert).where(InventoryAlert.workspace_id == workspace_id, InventoryAlert.dedupe_key == key))
        if on_hand <= policy.reorder_point_qty:
            severity = "critical" if on_hand <= policy.safety_stock_qty else "warning"
            message = f"当前库存 {on_hand}，安全库存 {policy.safety_stock_qty}，补货点 {policy.reorder_point_qty}"
            if alert is None:
                db.add(InventoryAlert(workspace_id=workspace_id, kind="low_stock", severity=severity, status="open", dedupe_key=key, title="库存低于补货点", message=message, warehouse_id=policy.warehouse_id, sku_id=policy.sku_id, created_at=now, last_seen_at=now))
            else:
                alert.severity = severity
                alert.message = message
                alert.last_seen_at = now
                if alert.status == "resolved":
                    alert.status = "open"
                    alert.acknowledged_at = None
        elif alert is not None and alert.status != "resolved":
            alert.status = "resolved"
            alert.last_seen_at = now
    db.commit()
    return list_alerts(db, workspace_id=workspace_id, warehouse_id=warehouse_id, status="open")


def _upsert_external_alert(db: Session, *, workspace_id: int, dedupe_key: str, kind: str, severity: str, title: str, message: str, platform: str | None = None, active: bool = True, now: datetime | None = None) -> InventoryAlert:
    now = now or datetime.utcnow()
    alert = db.scalar(select(InventoryAlert).where(InventoryAlert.workspace_id == workspace_id, InventoryAlert.dedupe_key == dedupe_key))
    if alert is None:
        alert = InventoryAlert(workspace_id=workspace_id, kind=kind, severity=severity, status="open" if active else "resolved", dedupe_key=dedupe_key, title=title, message=message[:1000], platform=platform, created_at=now, last_seen_at=now)
        db.add(alert)
    else:
        alert.severity = severity
        alert.title = title
        alert.message = message[:1000]
        alert.last_seen_at = now
        if active and alert.status == "resolved":
            alert.status = "open"
            alert.acknowledged_at = None
        elif not active and alert.status != "resolved":
            alert.status = "resolved"
    return alert


def refresh_external_alerts(db: Session, *, workspace_id: int, stale_after_seconds: int = 86400, now: datetime | None = None, sync_stale_after_seconds: int = 900) -> list[InventoryAlert]:
    from app.db import ExternalSyncRun
    from app.external_sync import snapshot_freshness

    now = now or datetime.utcnow()
    runs = db.scalars(select(ExternalSyncRun).where(ExternalSyncRun.workspace_id == workspace_id).order_by(ExternalSyncRun.id.desc())).all()
    latest_runs: dict[tuple[str, str | None, str | None, str], ExternalSyncRun] = {}
    for run in runs:
        latest_runs.setdefault((run.platform, run.account_ref, run.store_ref, run.sync_type), run)
    for (platform, account_ref, store_ref, sync_type), run in latest_runs.items():
        source_key = f"{platform}:{account_ref or ''}:{store_ref or ''}:{sync_type}"
        _upsert_external_alert(db, workspace_id=workspace_id, dedupe_key=f"external_sync_failed:{source_key}", kind="external_sync_failed", severity="critical", title="外部同步失败", message=f"{platform}/{sync_type} 最近一次同步失败：{run.error_code or 'UNKNOWN'}", platform=platform, active=run.status == "failed", now=now)
        _upsert_external_alert(db, workspace_id=workspace_id, dedupe_key=f"external_sync_partial:{source_key}", kind="external_sync_partial", severity="warning", title="外部同步部分成功", message=f"{platform}/{sync_type} 存在资源失败，需人工补偿", platform=platform, active=run.status == "partial", now=now)
        heartbeat = run.heartbeat_at or run.started_at
        stalled = run.status == "running" and heartbeat is not None and (now - heartbeat).total_seconds() > sync_stale_after_seconds
        _upsert_external_alert(db, workspace_id=workspace_id, dedupe_key=f"external_sync_stalled:{source_key}", kind="external_sync_stalled", severity="critical", title="外部同步运行停滞", message=f"{platform}/{sync_type} heartbeat 已超过 {sync_stale_after_seconds} 秒", platform=platform, active=stalled, now=now)
    for item in snapshot_freshness(db, workspace_id=workspace_id, now=now, stale_after_seconds=stale_after_seconds):
        source_key = ":".join(str(item.get(field) or "") for field in ("platform", "account_ref", "store_ref", "marketplace", "warehouse_ref"))
        _upsert_external_alert(db, workspace_id=workspace_id, dedupe_key=f"external_snapshot_stale:{source_key}", kind="external_snapshot_stale", severity="critical", title="外部库存快照过期", message=f"外部快照数据年龄 {item['age_seconds']} 秒，阈值 {item['threshold_seconds']} 秒", platform=item.get("platform"), active=item.get("state") == "stale", now=now)
        _upsert_external_alert(db, workspace_id=workspace_id, dedupe_key=f"external_snapshot_clock_skew:{source_key}", kind="external_snapshot_clock_skew", severity="warning", title="外部库存快照时钟偏差", message="外部快照观测时间晚于当前时间，需检查平台时钟或数据时间", platform=item.get("platform"), active=item.get("state") == "clock_skew", now=now)
    db.commit()
    return list_alerts(db, workspace_id=workspace_id)


def list_alerts(db: Session, *, workspace_id: int, warehouse_id: int | None = None, status: str | None = None, limit: int = 100) -> list[InventoryAlert]:
    filters = [InventoryAlert.workspace_id == workspace_id]
    if warehouse_id is not None:
        filters.append(InventoryAlert.warehouse_id == warehouse_id)
    if status is not None:
        filters.append(InventoryAlert.status == status)
    return db.scalars(select(InventoryAlert).where(*filters).order_by(InventoryAlert.last_seen_at.desc(), InventoryAlert.id.desc()).limit(limit)).all()


def acknowledge_alert(db: Session, *, workspace_id: int, alert_id: int) -> InventoryAlert | None:
    alert = db.scalar(select(InventoryAlert).where(InventoryAlert.id == alert_id, InventoryAlert.workspace_id == workspace_id))
    if alert is None:
        return None
    alert.status = "acknowledged"
    alert.acknowledged_at = datetime.utcnow()
    db.commit()
    db.refresh(alert)
    return alert
