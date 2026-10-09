"""运营工作台的只读待办聚合查询。

待办首版从已有领域事实派生，不持久化新的 Todo 状态；所有结果都限定在
当前 workspace 和可见仓库范围内，并保留 synthetic/live 边界。
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import (
    ExternalSyncRun,
    InboundOrder,
    InventoryAlert,
    Product,
    ProductContentRevision,
    ProductSku,
    PurchaseRequest,
    ReplenishmentSuggestion,
    Workspace,
)


_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _visible_filter(column, workspace_id: int, warehouse_ids: tuple[int, ...] | None):
    filters = [column.workspace_id == workspace_id]
    if warehouse_ids is not None:
        if not warehouse_ids:
            # An always-false SQL expression keeps callers from accidentally
            # treating an empty authorization scope as full access.
            filters.append(column.id == -1)
        elif hasattr(column, "warehouse_id"):
            filters.append(column.warehouse_id.in_(warehouse_ids))
    return filters


def _item(
    *,
    todo_id: str,
    todo_type: str,
    priority: str,
    status: str,
    title: str,
    reason: str,
    entity_type: str,
    entity_id: int | str | None,
    source: str,
    updated_at: datetime | None,
    allowed_actions: list[str],
    target_path: str | None,
    warehouse_id: int | None = None,
    due_at: datetime | None = None,
) -> dict:
    return {
        "todo_id": todo_id,
        "type": todo_type,
        "priority": priority,
        "status": status,
        "title": title,
        "reason": reason,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "source": source,
        "warehouse_id": warehouse_id,
        "due_at": due_at,
        "updated_at": updated_at,
        "allowed_actions": allowed_actions,
        "target_path": target_path,
    }


def _sku_labels(db: Session, *, workspace_id: int, sku_ids: Iterable[int]) -> dict[int, dict]:
    ids = {int(value) for value in sku_ids if value is not None}
    if not ids:
        return {}
    rows = db.execute(
        select(ProductSku, Product)
        .join(Product, Product.id == ProductSku.product_id)
        .where(ProductSku.workspace_id == workspace_id, ProductSku.id.in_(ids))
    ).all()
    return {
        sku.id: {
            "sku_code": sku.sku_code,
            "variant_label": sku.variant_label,
            "product_title": product.title,
        }
        for sku, product in rows
    }


def _scope_filters(model, *, workspace_id: int, warehouse_ids: tuple[int, ...] | None) -> list:
    filters = [model.workspace_id == workspace_id]
    if warehouse_ids is not None:
        if not warehouse_ids:
            filters.append(model.id == -1)
        elif hasattr(model, "warehouse_id"):
            filters.append(model.warehouse_id.in_(warehouse_ids))
    return filters


def build_operations_today(
    db: Session,
    *,
    workspace_id: int,
    warehouse_ids: tuple[int, ...] | list[int] | None = None,
    limit: int = 100,
    now: datetime | None = None,
) -> dict:
    """Build a stable, read-only operational todo view for one workspace."""
    if limit <= 0:
        raise ValueError("INVALID_TODO_LIMIT")
    if limit > 200:
        raise ValueError("TODO_LIMIT_TOO_LARGE")

    workspace = db.get(Workspace, workspace_id)
    if workspace is None or workspace.status != "active":
        raise ValueError("WORKSPACE_NOT_FOUND")
    scope = None if warehouse_ids is None else tuple(sorted({int(value) for value in warehouse_ids}))
    now = now or datetime.utcnow()
    items: list[dict] = []

    alerts = db.scalars(
        select(InventoryAlert)
        .where(*_scope_filters(InventoryAlert, workspace_id=workspace_id, warehouse_ids=scope), InventoryAlert.status != "resolved")
        .order_by(InventoryAlert.last_seen_at.desc(), InventoryAlert.id.desc())
    ).all()
    alert_labels = _sku_labels(db, workspace_id=workspace_id, sku_ids=[row.sku_id for row in alerts])
    for alert in alerts:
        label = alert_labels.get(alert.sku_id or -1, {})
        sku_text = f" · {label['sku_code']}" if label.get("sku_code") else ""
        items.append(_item(
            todo_id=f"alert:{alert.id}",
            todo_type="inventory_alert",
            priority="critical" if alert.severity == "critical" else "high",
            status=alert.status,
            title=f"{alert.title}{sku_text}",
            reason=alert.message,
            entity_type="inventory_alert",
            entity_id=alert.id,
            source="InventoryAlert",
            warehouse_id=alert.warehouse_id,
            updated_at=alert.last_seen_at,
            allowed_actions=["view", "acknowledge"],
            target_path="/dashboard#alerts-panel",
        ))

    suggestions = db.scalars(
        select(ReplenishmentSuggestion)
        .where(*_scope_filters(ReplenishmentSuggestion, workspace_id=workspace_id, warehouse_ids=scope), ReplenishmentSuggestion.status == "suggested")
        .order_by(ReplenishmentSuggestion.updated_at.desc(), ReplenishmentSuggestion.id.desc())
    ).all()
    suggestion_labels = _sku_labels(db, workspace_id=workspace_id, sku_ids=[row.sku_id for row in suggestions])
    for suggestion in suggestions:
        label = suggestion_labels.get(suggestion.sku_id, {})
        sku_text = label.get("sku_code") or f"SKU #{suggestion.sku_id}"
        qty = suggestion.suggested_qty if suggestion.suggested_qty is not None else "数据不足"
        items.append(_item(
            todo_id=f"replenishment:{suggestion.id}",
            todo_type="replenishment_review",
            priority="critical" if suggestion.reason in {"LOW_STOCK", "STOCKOUT_RISK"} else "high",
            status=suggestion.status,
            title=f"补货建议待确认 · {sku_text}",
            reason=f"建议补货 {qty} 件；{suggestion.reason or '需要人工复核'}",
            entity_type="replenishment_suggestion",
            entity_id=suggestion.id,
            source="ReplenishmentSuggestion",
            warehouse_id=suggestion.warehouse_id,
            updated_at=suggestion.updated_at,
            allowed_actions=["view", "confirm", "modify", "ignore"],
            target_path="/ops#purchase-workflow",
        ))

    requests = db.scalars(
        select(PurchaseRequest)
        .where(*_scope_filters(PurchaseRequest, workspace_id=workspace_id, warehouse_ids=scope), PurchaseRequest.status == "draft")
        .order_by(PurchaseRequest.updated_at.desc(), PurchaseRequest.id.desc())
    ).all()
    for purchase in requests:
        items.append(_item(
            todo_id=f"purchase_request:{purchase.id}",
            todo_type="purchase_draft",
            priority="high",
            status=purchase.status,
            title=f"采购草稿待人工处理 · {purchase.request_no}",
            reason=purchase.note or "请复核数量、供应商和预计到货日期后再提交人工审批",
            entity_type="purchase_request",
            entity_id=purchase.id,
            source="PurchaseRequest",
            warehouse_id=purchase.warehouse_id,
            updated_at=purchase.updated_at,
            allowed_actions=["view", "edit", "submit"],
            target_path="/ops#purchase-workflow",
        ))

    # Keep only the latest run for each external source, matching dashboard
    # semantics; a failed old run must not remain a duplicate daily task.
    runs = db.scalars(
        select(ExternalSyncRun)
        .where(ExternalSyncRun.workspace_id == workspace_id)
        .order_by(ExternalSyncRun.id.desc())
        .limit(5000)
    ).all()
    latest_runs: dict[tuple[str, str | None, str | None, str], ExternalSyncRun] = {}
    stale_after = get_settings().external_sync_run_stale_after_seconds
    for run in runs:
        key = (run.platform, run.account_ref, run.store_ref, run.sync_type)
        latest_runs.setdefault(key, run)
    for run in latest_runs.values():
        heartbeat = run.heartbeat_at or run.started_at
        is_stalled = bool(
            run.status == "running"
            and heartbeat is not None
            and (now - heartbeat).total_seconds() > stale_after
        )
        if run.status not in {"failed", "partial"} and not is_stalled:
            continue
        status = "stalled" if is_stalled else run.status
        reason = "同步运行超过心跳阈值，需停止或人工检查" if is_stalled else (
            "同步存在失败资源，请人工补偿" if run.status == "partial" else f"最近一次同步失败：{run.error_code or '未知错误'}"
        )
        items.append(_item(
            todo_id=f"sync:{run.run_id}",
            todo_type="sync_failure",
            priority="critical" if status in {"failed", "stalled"} else "high",
            status=status,
            title=f"外部同步需处理 · {run.platform}/{run.sync_type}",
            reason=reason,
            entity_type="external_sync_run",
            entity_id=run.run_id,
            source="ExternalSyncRun",
            updated_at=run.finished_at or run.heartbeat_at or run.started_at,
            allowed_actions=["view", "retry"] if run.retryable_resources_json not in (None, "[]") else ["view"],
            target_path="/dashboard#sync-health-panel",
        ))

    revisions = db.scalars(
        select(ProductContentRevision)
        .where(ProductContentRevision.workspace_id == workspace_id)
        .where(
            (ProductContentRevision.status.in_(("review_required", "quality_failed")))
            | (ProductContentRevision.quality_status.in_(("warn", "blocked")))
        )
        .order_by(ProductContentRevision.updated_at.desc(), ProductContentRevision.id.desc())
    ).all()
    for revision in revisions:
        blocked = revision.quality_status == "blocked" or revision.status == "quality_failed"
        items.append(_item(
            todo_id=f"content_revision:{revision.id}",
            todo_type="content_review",
            priority="high" if blocked else "medium",
            status=revision.status,
            title=f"商品页内容待审核 · 商品 #{revision.product_id}",
            reason="质量门禁阻断，需人工处理" if blocked else "候选内容或质量警告待人工审核",
            entity_type="content_revision",
            entity_id=revision.id,
            source="ProductContentRevision",
            updated_at=revision.updated_at,
            allowed_actions=["view", "review"],
            target_path="/content",
        ))

    inbounds = db.scalars(
        select(InboundOrder)
        .where(*_scope_filters(InboundOrder, workspace_id=workspace_id, warehouse_ids=scope), InboundOrder.status == "received")
        .order_by(InboundOrder.updated_at.desc(), InboundOrder.id.desc())
    ).all()
    for inbound in inbounds:
        items.append(_item(
            todo_id=f"inbound:{inbound.id}",
            todo_type="inbound_confirmation",
            priority="high",
            status=inbound.status,
            title=f"入库单待运营确认 · {inbound.reference_no}",
            reason="仓库已反馈实收，确认前不会改变内部库存台账",
            entity_type="inbound_order",
            entity_id=inbound.id,
            source="InboundOrder",
            warehouse_id=inbound.warehouse_id,
            updated_at=inbound.updated_at,
            allowed_actions=["view", "confirm"],
            target_path="/ops#warehouse-step-confirmation",
        ))

    def sort_key(item: dict) -> tuple:
        updated_at = item["updated_at"] or datetime.min
        return (_PRIORITY_RANK.get(item["priority"], 9), -updated_at.timestamp(), str(item["todo_id"]))

    items.sort(key=sort_key)
    visible_items = items[:limit]
    by_type: dict[str, int] = {}
    for item in visible_items:
        by_type[item["type"]] = by_type.get(item["type"], 0) + 1

    is_demo = workspace.tenant_key.startswith("demo-")
    return {
        "workspace_id": workspace_id,
        "as_of": now,
        "timezone": "UTC",
        "source_mode": "synthetic" if is_demo else "workspace",
        "simulated": is_demo,
        "evidence_level": "E2" if is_demo else "E3",
        "summary": {
            "total": len(visible_items),
            "matched_total": len(items),
            "by_type": by_type,
            "unknown_types": {"customer_service": None},
        },
        "items": [{key: value for key, value in item.items() if key != "updated_at"} for item in visible_items],
        "meta": {
            "data_completeness": "partial",
            "limitations": [
                "当前待办从已有领域事实派生，尚未持久化认领、完成或跨日状态",
                "客服工单和客户跟进事实尚未纳入，未知数量不按 0 计",
                "待办只能证明模拟或现有数据流程可运行，不能证明真实业务收益",
            ],
            "scope": {"warehouse_ids": list(scope) if scope is not None else "all_visible"},
            "limit": limit,
        },
    }
