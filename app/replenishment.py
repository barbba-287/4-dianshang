"""补货建议和内部采购申请领域服务。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import (
    DailySkuSale,
    InventoryBalance,
    InventoryPolicy,
    ProductSku,
    PurchaseRequest,
    PurchaseRequestAction,
    PurchaseRequestLine,
    ReplenishmentSuggestion,
    ReplenishmentSuggestionAction,
    ReplenishmentEvaluation,
    Warehouse,
)
from app.business_dates import resolve_sales_as_of

FORMULA_VERSION = "replenishment.v1"
OPEN_SLOT = "open"


def _canonical_hash(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _require_warehouse(db: Session, *, workspace_id: int, warehouse_id: int) -> Warehouse:
    warehouse = db.scalar(select(Warehouse).where(
        Warehouse.id == warehouse_id,
        Warehouse.workspace_id == workspace_id,
        Warehouse.is_active.is_(True),
    ))
    if warehouse is None:
        raise ValueError("WAREHOUSE_NOT_FOUND")
    return warehouse


def _require_sku(db: Session, *, workspace_id: int, sku_id: int) -> ProductSku:
    sku = db.scalar(select(ProductSku).where(
        ProductSku.id == sku_id,
        ProductSku.workspace_id == workspace_id,
        ProductSku.is_active.is_(True),
    ))
    if sku is None:
        raise ValueError("SKU_NOT_FOUND")
    return sku


def _sales_inputs(db: Session, *, workspace_id: int, sku_id: int, as_of: date, window_days: int) -> dict:
    start = as_of - timedelta(days=window_days - 1)
    rows = db.scalars(select(DailySkuSale).where(
        DailySkuSale.workspace_id == workspace_id,
        DailySkuSale.internal_sku_id == sku_id,
        DailySkuSale.sales_date >= start,
        DailySkuSale.sales_date <= as_of,
    )).all()
    complete_days = {row.sales_date for row in rows if row.data_completeness == "complete"}
    net_qty = sum(row.net_qty for row in rows)
    complete = len(complete_days) == window_days
    average = (Decimal(net_qty) / Decimal(window_days)) if complete else None
    return {
        "from": start.isoformat(),
        "to": as_of.isoformat(),
        "net_qty": net_qty,
        "effective_sale_days": len(complete_days),
        "daily_avg_qty": str(average) if average is not None else None,
        "data_completeness": "complete" if complete else "insufficient",
    }


def calculate_replenishment(
    db: Session,
    *,
    workspace_id: int,
    warehouse_id: int,
    sku_id: int,
    coverage_days: int = 14,
    as_of: date | None = None,
) -> dict:
    if coverage_days not in (7, 14, 30):
        raise ValueError("INVALID_COVERAGE_DAYS")
    as_of = resolve_sales_as_of(db, workspace_id=workspace_id, explicit_as_of=as_of)
    _require_warehouse(db, workspace_id=workspace_id, warehouse_id=warehouse_id)
    _require_sku(db, workspace_id=workspace_id, sku_id=sku_id)
    balance = db.scalar(select(InventoryBalance).where(
        InventoryBalance.workspace_id == workspace_id,
        InventoryBalance.warehouse_id == warehouse_id,
        InventoryBalance.sku_id == sku_id,
    ))
    policy = db.scalar(select(InventoryPolicy).where(
        InventoryPolicy.workspace_id == workspace_id,
        InventoryPolicy.warehouse_id == warehouse_id,
        InventoryPolicy.sku_id == sku_id,
    ))
    sales = _sales_inputs(db, workspace_id=workspace_id, sku_id=sku_id, as_of=as_of, window_days=coverage_days)
    average = Decimal(sales["daily_avg_qty"]) if sales["daily_avg_qty"] is not None else None
    safety = policy.safety_stock_qty if policy else 0
    reorder = policy.reorder_point_qty if policy else 0
    on_hand = balance.on_hand_qty if balance else 0
    target = int((average * coverage_days + safety).to_integral_value(rounding=ROUND_CEILING)) if average is not None else None
    suggested = max(0, target - on_hand) if target is not None else None
    reason = "LOW_STOCK" if suggested and suggested > 0 else "STOCK_SUFFICIENT" if suggested == 0 else "INCOMPLETE_COVERAGE"
    snapshot = {
        "formula_version": FORMULA_VERSION,
        "workspace_id": workspace_id,
        "warehouse_id": warehouse_id,
        "sku_id": sku_id,
        "as_of_date": as_of.isoformat(),
        "coverage_days": coverage_days,
        "sales_scope": "workspace",
        "sales": sales,
        "on_hand_qty": on_hand,
        "safety_stock_qty": safety,
        "reorder_point_qty": reorder,
        "target_qty": target,
        "suggested_qty": suggested,
        "data_completeness": sales["data_completeness"],
        "policy_id": policy.id if policy else None,
    }
    return {
        "workspace_id": workspace_id,
        "warehouse_id": warehouse_id,
        "sku_id": sku_id,
        "coverage_days": coverage_days,
        "as_of_date": as_of,
        "daily_avg_qty": average,
        "on_hand_qty": on_hand,
        "safety_stock_qty": safety,
        "reorder_point_qty": reorder,
        "sales_qty": sales["net_qty"],
        "effective_sale_days": sales["effective_sale_days"],
        "data_completeness": sales["data_completeness"],
        "suggested_qty": suggested,
        "reason": reason,
        "source_snapshot": snapshot,
        "source_hash": _canonical_hash(snapshot),
    }


def generate_suggestion(
    db: Session,
    *,
    workspace_id: int,
    warehouse_id: int,
    sku_id: int,
    actor: str | None,
    coverage_days: int = 14,
    as_of: date | None = None,
) -> ReplenishmentSuggestion:
    values = calculate_replenishment(
        db, workspace_id=workspace_id, warehouse_id=warehouse_id,
        sku_id=sku_id, coverage_days=coverage_days, as_of=as_of,
    )
    if values["suggested_qty"] is None or values["suggested_qty"] <= 0:
        raise ValueError(values["reason"])
    existing = db.scalar(select(ReplenishmentSuggestion).where(
        ReplenishmentSuggestion.workspace_id == workspace_id,
        ReplenishmentSuggestion.warehouse_id == warehouse_id,
        ReplenishmentSuggestion.sku_id == sku_id,
        ReplenishmentSuggestion.source_hash == values["source_hash"],
    ))
    if existing is not None:
        return existing
    open_suggestion = db.scalar(select(ReplenishmentSuggestion).where(
        ReplenishmentSuggestion.workspace_id == workspace_id,
        ReplenishmentSuggestion.warehouse_id == warehouse_id,
        ReplenishmentSuggestion.sku_id == sku_id,
        ReplenishmentSuggestion.active_slot == OPEN_SLOT,
    ))
    if open_suggestion is not None:
        raise ValueError("OPEN_SUGGESTION_EXISTS")
    suggestion = ReplenishmentSuggestion(
        workspace_id=workspace_id, warehouse_id=warehouse_id, sku_id=sku_id,
        status="suggested", suggested_qty=values["suggested_qty"],
        formula_version=FORMULA_VERSION, coverage_days=coverage_days,
        daily_avg_qty=values["daily_avg_qty"], on_hand_qty=values["on_hand_qty"],
        safety_stock_qty=values["safety_stock_qty"], reorder_point_qty=values["reorder_point_qty"],
        sales_window_days=coverage_days, sales_qty=values["sales_qty"],
        effective_sale_days=values["effective_sale_days"], data_completeness=values["data_completeness"],
        reason=values["reason"], source_hash=values["source_hash"],
        source_snapshot_json=json.dumps(values["source_snapshot"], ensure_ascii=False, sort_keys=True),
        as_of_date=values["as_of_date"], active_slot=OPEN_SLOT, version=1, created_by=actor,
    )
    db.add(suggestion)
    try:
        db.commit()
        db.refresh(suggestion)
    except Exception:
        db.rollback()
        duplicate = db.scalar(select(ReplenishmentSuggestion).where(
            ReplenishmentSuggestion.workspace_id == workspace_id,
            ReplenishmentSuggestion.warehouse_id == warehouse_id,
            ReplenishmentSuggestion.sku_id == sku_id,
            ReplenishmentSuggestion.source_hash == values["source_hash"],
        ))
        if duplicate is not None:
            return duplicate
        raise
    return suggestion


def evaluate_replenishment(
    db: Session,
    *,
    workspace_id: int,
    suggestion_id: int,
    window_start: date,
    window_end: date,
    source_mode: str = "mock",
    simulated: bool = True,
):
    if workspace_id is None or window_start > window_end:
        raise ValueError("INVALID_EVALUATION_WINDOW")
    suggestion = db.scalar(select(ReplenishmentSuggestion).where(
        ReplenishmentSuggestion.id == suggestion_id,
        ReplenishmentSuggestion.workspace_id == workspace_id,
    ))
    if suggestion is None:
        raise ValueError("SUGGESTION_NOT_FOUND")
    if suggestion.suggested_qty is None:
        raise ValueError("SUGGESTION_DATA_INCOMPLETE")
    if suggestion.as_of_date >= window_end:
        raise ValueError("EVALUATION_WINDOW_BEFORE_SUGGESTION")
    days = (window_end - window_start).days + 1
    rows = db.scalars(select(DailySkuSale).where(
        DailySkuSale.workspace_id == workspace_id,
        DailySkuSale.internal_sku_id == suggestion.sku_id,
        DailySkuSale.sales_date >= window_start,
        DailySkuSale.sales_date <= window_end,
    )).all()
    complete_days = {row.sales_date for row in rows if row.data_completeness == "complete"}
    snapshot = {
        "suggestion_id": suggestion.id,
        "suggested_qty": suggestion.suggested_qty,
        "formula_version": suggestion.formula_version,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "sales": [{"date": row.sales_date.isoformat(), "net_qty": row.net_qty, "data_completeness": row.data_completeness} for row in sorted(rows, key=lambda item: item.sales_date)],
    }
    source_hash = _canonical_hash(snapshot)
    existing = db.scalar(select(ReplenishmentEvaluation).where(
        ReplenishmentEvaluation.workspace_id == workspace_id,
        ReplenishmentEvaluation.suggestion_id == suggestion.id,
        ReplenishmentEvaluation.window_start == window_start,
        ReplenishmentEvaluation.window_end == window_end,
        ReplenishmentEvaluation.formula_version == suggestion.formula_version,
        ReplenishmentEvaluation.source_snapshot_hash == source_hash,
    ))
    if existing is not None:
        return existing
    complete = len(complete_days) == days
    actual = sum(row.net_qty for row in rows) if complete else None
    status = "evaluated" if complete else "insufficient"
    evaluation = ReplenishmentEvaluation(
        workspace_id=workspace_id, suggestion_id=suggestion.id, sku_id=suggestion.sku_id, warehouse_id=suggestion.warehouse_id,
        formula_version=suggestion.formula_version, suggested_qty=suggestion.suggested_qty,
        window_start=window_start, window_end=window_end, actual_sales_qty=actual,
        absolute_error=abs(actual - suggestion.suggested_qty) if actual is not None else None,
        evaluation_status=status, data_completeness="complete" if complete else "insufficient",
        source_snapshot_hash=source_hash, source_snapshot_json=json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
        source_mode=source_mode, simulated=simulated,
    )
    db.add(evaluation)
    db.commit()
    db.refresh(evaluation)
    return evaluation


def list_replenishment_evaluations(db: Session, *, workspace_id: int, suggestion_id: int | None = None, warehouse_id: int | None = None, sku_id: int | None = None, status: str | None = None, limit: int = 20, offset: int = 0):
    from app.db import ReplenishmentEvaluation
    filters = [ReplenishmentEvaluation.workspace_id == workspace_id]
    if suggestion_id is not None: filters.append(ReplenishmentEvaluation.suggestion_id == suggestion_id)
    if warehouse_id is not None: filters.append(ReplenishmentEvaluation.warehouse_id == warehouse_id)
    if sku_id is not None: filters.append(ReplenishmentEvaluation.sku_id == sku_id)
    if status: filters.append(ReplenishmentEvaluation.evaluation_status == status)
    from sqlalchemy import func
    total = db.scalar(select(func.count(ReplenishmentEvaluation.id)).where(*filters)) or 0
    rows = db.scalars(select(ReplenishmentEvaluation).where(*filters).order_by(ReplenishmentEvaluation.id.desc()).offset(offset).limit(limit)).all()
    return rows, int(total)


def list_suggestions(db: Session, *, workspace_id: int, warehouse_id: int | None = None, warehouse_ids: list[int] | tuple[int, ...] | None = None, status: str | None = None, sku_id: int | None = None, limit: int = 100, offset: int = 0):
    filters = [ReplenishmentSuggestion.workspace_id == workspace_id]
    if warehouse_id is not None:
        filters.append(ReplenishmentSuggestion.warehouse_id == warehouse_id)
    elif warehouse_ids is not None:
        filters.append(ReplenishmentSuggestion.warehouse_id.in_(warehouse_ids))
    if status:
        filters.append(ReplenishmentSuggestion.status == status)
    if sku_id is not None:
        filters.append(ReplenishmentSuggestion.sku_id == sku_id)
    from sqlalchemy import func
    total = db.scalar(select(func.count(ReplenishmentSuggestion.id)).where(*filters)) or 0
    rows = db.scalars(select(ReplenishmentSuggestion).where(*filters).order_by(ReplenishmentSuggestion.id.desc()).offset(offset).limit(limit)).all()
    return rows, int(total)


def decide_suggestion(
    db: Session,
    *,
    workspace_id: int,
    suggestion_id: int,
    action: str,
    actor: str,
    idempotency_key: str,
    decision_qty: int | None = None,
    reason: str | None = None,
    expected_version: int,
) -> ReplenishmentSuggestion:
    if not idempotency_key or len(idempotency_key) > 128:
        raise ValueError("IDEMPOTENCY_KEY_REQUIRED")
    if action not in {"confirm", "modify", "ignore"}:
        raise ValueError("INVALID_ACTION")
    payload = {"action": action, "decision_qty": decision_qty, "reason": reason, "expected_version": expected_version}
    payload_hash = _canonical_hash(payload)
    previous_action = db.scalar(select(ReplenishmentSuggestionAction).where(
        ReplenishmentSuggestionAction.workspace_id == workspace_id,
        ReplenishmentSuggestionAction.suggestion_id == suggestion_id,
        ReplenishmentSuggestionAction.idempotency_key == idempotency_key,
    ))
    if previous_action is not None:
        if previous_action.payload_hash != payload_hash:
            raise ValueError("IDEMPOTENCY_KEY_REUSE")
        suggestion = db.get(ReplenishmentSuggestion, suggestion_id)
        if suggestion is None:
            raise ValueError("SUGGESTION_NOT_FOUND")
        return suggestion
    suggestion = db.scalar(select(ReplenishmentSuggestion).where(
        ReplenishmentSuggestion.id == suggestion_id,
        ReplenishmentSuggestion.workspace_id == workspace_id,
    ))
    if suggestion is None:
        raise ValueError("SUGGESTION_NOT_FOUND")
    if suggestion.version != expected_version:
        raise ValueError("SUGGESTION_VERSION_CONFLICT")
    if suggestion.status not in {"suggested", "modified"}:
        raise ValueError("INVALID_SUGGESTION_STATE")
    if action == "modify":
        if decision_qty is None or decision_qty < 0 or not reason or not reason.strip():
            raise ValueError("MODIFY_REASON_AND_QTY_REQUIRED")
        to_status, final_qty = "modified", decision_qty
    elif action == "confirm":
        if suggestion.suggested_qty is None:
            raise ValueError("SUGGESTION_DATA_INCOMPLETE")
        to_status, final_qty = "confirmed", suggestion.suggested_qty
    else:
        if not reason or not reason.strip():
            raise ValueError("IGNORE_REASON_REQUIRED")
        to_status, final_qty = "ignored", None
    old_status = suggestion.status
    now = datetime.utcnow()
    suggestion.status = to_status
    suggestion.decision_qty = final_qty
    suggestion.decision_reason = reason
    suggestion.decision_by = actor
    suggestion.decision_at = now
    suggestion.updated_at = now
    suggestion.version += 1
    if to_status == "ignored":
        suggestion.active_slot = None
    db.add(ReplenishmentSuggestionAction(
        workspace_id=workspace_id, suggestion_id=suggestion_id,
        idempotency_key=idempotency_key, payload_hash=payload_hash,
        from_status=old_status, to_status=to_status, decision_qty=final_qty,
        reason=reason, actor=actor, expected_version=expected_version,
    ))
    db.commit()
    db.refresh(suggestion)
    return suggestion


def create_purchase_request_draft(
    db: Session,
    *,
    workspace_id: int,
    suggestion_ids: list[int],
    actor: str,
    idempotency_key: str,
    note: str | None = None,
    supplier_ref: str | None = None,
    expected_arrival_date: date | None = None,
) -> PurchaseRequest:
    """Create an editable purchase draft without submitting or changing stock."""
    if not idempotency_key or len(idempotency_key) > 128:
        raise ValueError("IDEMPOTENCY_KEY_REQUIRED")
    if not suggestion_ids:
        raise ValueError("SUGGESTIONS_REQUIRED")
    unique_ids = sorted(set(suggestion_ids))
    payload = {
        "suggestion_ids": unique_ids,
        "note": note,
        "supplier_ref": supplier_ref,
        "expected_arrival_date": expected_arrival_date.isoformat() if expected_arrival_date else None,
    }
    payload_hash = _canonical_hash(payload)
    existing = db.scalar(select(PurchaseRequest).where(
        PurchaseRequest.workspace_id == workspace_id,
        PurchaseRequest.idempotency_key == idempotency_key,
    ))
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise ValueError("IDEMPOTENCY_KEY_REUSE")
        return existing
    suggestions = db.scalars(select(ReplenishmentSuggestion).where(
        ReplenishmentSuggestion.workspace_id == workspace_id,
        ReplenishmentSuggestion.id.in_(unique_ids),
    ).order_by(ReplenishmentSuggestion.id)).all()
    if len(suggestions) != len(unique_ids):
        raise ValueError("SUGGESTION_NOT_FOUND")
    if any(item.status not in {"confirmed", "modified"} or item.decision_qty is None or item.decision_qty <= 0 for item in suggestions):
        raise ValueError("SUGGESTION_NOT_READY")
    already_linked = db.scalar(select(PurchaseRequestLine.suggestion_id).where(
        PurchaseRequestLine.workspace_id == workspace_id,
        PurchaseRequestLine.suggestion_id.in_(unique_ids),
    ))
    if already_linked is not None:
        raise ValueError("SUGGESTION_ALREADY_IN_PURCHASE_REQUEST")
    warehouses = {item.warehouse_id for item in suggestions}
    if len(warehouses) != 1:
        raise ValueError("MIXED_WAREHOUSE")
    warehouse_id = next(iter(warehouses))
    request = PurchaseRequest(
        workspace_id=workspace_id, warehouse_id=warehouse_id,
        request_no=f"PR-{uuid4().hex[:12].upper()}", status="draft", note=note,
        created_by=actor, idempotency_key=idempotency_key, payload_hash=payload_hash,
        supplier_ref=supplier_ref, expected_arrival_date=expected_arrival_date, version=1,
    )
    db.add(request)
    db.flush()
    for item in suggestions:
        db.add(PurchaseRequestLine(
            workspace_id=workspace_id, purchase_request_id=request.id,
            warehouse_id=item.warehouse_id, sku_id=item.sku_id, suggestion_id=item.id,
            requested_qty=item.decision_qty, source_suggestion_version=item.version,
        ))
    db.add(PurchaseRequestAction(
        workspace_id=workspace_id, purchase_request_id=request.id, action_type="create",
        from_status=None, to_status="draft", idempotency_key=idempotency_key,
        payload_hash=payload_hash, actor=actor,
    ))
    try:
        db.commit()
        db.refresh(request)
    except Exception:
        db.rollback()
        duplicate = db.scalar(select(PurchaseRequest).where(
            PurchaseRequest.workspace_id == workspace_id,
            PurchaseRequest.idempotency_key == idempotency_key,
        ))
        if duplicate is not None and duplicate.payload_hash == payload_hash:
            return duplicate
        raise
    return request


def edit_purchase_request_draft(
    db: Session,
    *,
    workspace_id: int,
    request_id: int,
    actor: str,
    idempotency_key: str,
    expected_version: int,
    note: str | None = None,
    supplier_ref: str | None = None,
    expected_arrival_date: date | None = None,
) -> PurchaseRequest:
    """Edit draft metadata with optimistic version protection."""
    if not idempotency_key or len(idempotency_key) > 128:
        raise ValueError("IDEMPOTENCY_KEY_REQUIRED")
    request = db.scalar(select(PurchaseRequest).where(PurchaseRequest.id == request_id, PurchaseRequest.workspace_id == workspace_id))
    if request is None:
        raise ValueError("PURCHASE_REQUEST_NOT_FOUND")
    payload_hash = _canonical_hash({"request_id": request_id, "note": note, "supplier_ref": supplier_ref, "expected_arrival_date": expected_arrival_date.isoformat() if expected_arrival_date else None, "expected_version": expected_version})
    action = db.scalar(select(PurchaseRequestAction).where(PurchaseRequestAction.workspace_id == workspace_id, PurchaseRequestAction.purchase_request_id == request_id, PurchaseRequestAction.action_type == "edit", PurchaseRequestAction.idempotency_key == idempotency_key))
    if action is not None:
        if action.payload_hash != payload_hash:
            raise ValueError("IDEMPOTENCY_KEY_REUSE")
        return request
    if request.status != "draft":
        raise ValueError("INVALID_PURCHASE_REQUEST_STATE")
    if request.version != expected_version:
        raise ValueError("PURCHASE_REQUEST_VERSION_CONFLICT")
    request.note = note
    request.supplier_ref = supplier_ref
    request.expected_arrival_date = expected_arrival_date
    request.version += 1
    db.add(PurchaseRequestAction(workspace_id=workspace_id, purchase_request_id=request_id, action_type="edit", from_status="draft", to_status="draft", idempotency_key=idempotency_key, payload_hash=payload_hash, expected_version=expected_version, actor=actor))
    db.commit()
    db.refresh(request)
    return request
def submit_purchase_request_draft(
    db: Session,
    *,
    workspace_id: int,
    request_id: int,
    actor: str,
    idempotency_key: str,
    expected_version: int,
) -> PurchaseRequest:
    """Submit one draft for manual approval; never performs purchasing or receiving."""
    if not idempotency_key or len(idempotency_key) > 128:
        raise ValueError("IDEMPOTENCY_KEY_REQUIRED")
    request = db.scalar(select(PurchaseRequest).where(
        PurchaseRequest.id == request_id,
        PurchaseRequest.workspace_id == workspace_id,
    ))
    if request is None:
        raise ValueError("PURCHASE_REQUEST_NOT_FOUND")
    payload_hash = _canonical_hash({"request_id": request_id, "expected_version": expected_version})
    action = db.scalar(select(PurchaseRequestAction).where(
        PurchaseRequestAction.workspace_id == workspace_id,
        PurchaseRequestAction.purchase_request_id == request_id,
        PurchaseRequestAction.action_type == "submit",
        PurchaseRequestAction.idempotency_key == idempotency_key,
    ))
    if action is not None:
        if action.payload_hash != payload_hash:
            raise ValueError("IDEMPOTENCY_KEY_REUSE")
        return request
    if request.status != "draft":
        raise ValueError("INVALID_PURCHASE_REQUEST_STATE")
    if request.version != expected_version:
        raise ValueError("PURCHASE_REQUEST_VERSION_CONFLICT")
    lines = db.scalars(select(PurchaseRequestLine).where(
        PurchaseRequestLine.purchase_request_id == request.id,
        PurchaseRequestLine.workspace_id == workspace_id,
    ).order_by(PurchaseRequestLine.id)).all()
    # 先完成所有 suggestion 校验，再变更 request/suggestion，避免 service
    # 被单独调用时留下半提交状态。
    suggestions = []
    for line in lines:
        suggestion = db.scalar(select(ReplenishmentSuggestion).where(
            ReplenishmentSuggestion.id == line.suggestion_id,
            ReplenishmentSuggestion.workspace_id == workspace_id,
        ))
        if suggestion is None or suggestion.version != line.source_suggestion_version or suggestion.status not in {"confirmed", "modified"}:
            raise ValueError("SUGGESTION_STALE")
        suggestions.append(suggestion)
    now = datetime.utcnow()
    request.status = "submitted"
    request.submitted_by = actor
    request.submitted_at = now
    request.version += 1
    for suggestion in suggestions:
        suggestion.status = "submitted"
        suggestion.submitted_by = actor
        suggestion.submitted_at = now
        suggestion.active_slot = None
        suggestion.version += 1
    db.add(PurchaseRequestAction(
        workspace_id=workspace_id, purchase_request_id=request_id, action_type="submit",
        from_status="draft", to_status="submitted", idempotency_key=idempotency_key,
        payload_hash=payload_hash, expected_version=expected_version, actor=actor,
    ))
    db.commit()
    db.refresh(request)
    return request


def submit_purchase_request(
    db: Session,
    *,
    workspace_id: int,
    suggestion_ids: list[int],
    actor: str,
    idempotency_key: str,
    note: str | None = None,
) -> PurchaseRequest:
    if not idempotency_key or len(idempotency_key) > 128:
        raise ValueError("IDEMPOTENCY_KEY_REQUIRED")
    if not suggestion_ids:
        raise ValueError("SUGGESTIONS_REQUIRED")
    unique_ids = sorted(set(suggestion_ids))
    payload = {"suggestion_ids": unique_ids, "note": note}
    payload_hash = _canonical_hash(payload)
    existing = db.scalar(select(PurchaseRequest).where(
        PurchaseRequest.workspace_id == workspace_id,
        PurchaseRequest.idempotency_key == idempotency_key,
    ))
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise ValueError("IDEMPOTENCY_KEY_REUSE")
        return existing
    suggestions = db.scalars(select(ReplenishmentSuggestion).where(
        ReplenishmentSuggestion.workspace_id == workspace_id,
        ReplenishmentSuggestion.id.in_(unique_ids),
    ).order_by(ReplenishmentSuggestion.id)).all()
    if len(suggestions) != len(unique_ids):
        raise ValueError("SUGGESTION_NOT_FOUND")
    warehouses = {item.warehouse_id for item in suggestions}
    if len(warehouses) != 1:
        raise ValueError("MIXED_WAREHOUSE")
    if any(item.status not in {"confirmed", "modified"} or item.decision_qty is None or item.decision_qty <= 0 for item in suggestions):
        raise ValueError("SUGGESTION_NOT_READY")
    warehouse_id = next(iter(warehouses))
    request = PurchaseRequest(
        workspace_id=workspace_id, warehouse_id=warehouse_id,
        request_no=f"PR-{uuid4().hex[:12].upper()}", status="submitted", note=note,
        submitted_by=actor, submitted_at=datetime.utcnow(), idempotency_key=idempotency_key,
        payload_hash=payload_hash,
    )
    db.add(request)
    db.flush()
    now = datetime.utcnow()
    for item in suggestions:
        db.add(PurchaseRequestLine(
            workspace_id=workspace_id, purchase_request_id=request.id,
            warehouse_id=item.warehouse_id, sku_id=item.sku_id, suggestion_id=item.id,
            requested_qty=item.decision_qty, source_suggestion_version=item.version,
        ))
        item.status = "submitted"
        item.submitted_by = actor
        item.submitted_at = now
        item.active_slot = None
        item.version += 1
        item.updated_at = now
    try:
        db.commit()
        db.refresh(request)
    except Exception:
        db.rollback()
        raise
    return request
