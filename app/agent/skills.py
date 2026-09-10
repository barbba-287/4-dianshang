"""Provider-neutral read-only operator skills backed by domain services."""
from __future__ import annotations

from datetime import date

from app.agent.registry import AgentTool, ToolContext
from app.dashboard import build_sku_health
from app.db import SessionLocal
from app.replenishment import calculate_replenishment
from app.repository import list_inventory


def _reject_workspace_override(payload: dict) -> None:
    if any(key in payload for key in ("workspace_id", "workspace", "tenant_id", "tenant")):
        raise ValueError("WORKSPACE_PARAMETER_FORBIDDEN")


def _warehouse_scope(payload: dict, context: ToolContext) -> tuple[int | None, list[int] | None]:
    warehouse_id = payload.get("warehouse_id")
    allowed = list(context.extras.get("warehouse_ids", ()))
    all_access = bool(context.extras.get("all_warehouse_access"))
    if warehouse_id is not None and not all_access and warehouse_id not in allowed:
        raise ValueError("WAREHOUSE_NOT_FOUND")
    return warehouse_id, None if all_access else allowed


def get_inventory_handler(payload: dict, context: ToolContext) -> dict:
    _reject_workspace_override(payload)
    warehouse_id, warehouse_ids = _warehouse_scope(payload, context)
    db = SessionLocal()
    try:
        rows, total = list_inventory(
            db,
            warehouse_id=warehouse_id,
            sku_id=payload.get("sku_id"),
            warehouse_ids=warehouse_ids,
            workspace_id=context.workspace_id,
            page=payload.get("page", 1),
            page_size=payload.get("page_size", 20),
        )
        return {"items": rows, "total": total, "page": payload.get("page", 1), "page_size": payload.get("page_size", 20)}
    finally:
        db.close()


def list_sku_health_handler(payload: dict, context: ToolContext) -> dict:
    _reject_workspace_override(payload)
    warehouse_id, warehouse_ids = _warehouse_scope(payload, context)
    requested = payload.get("warehouse_ids")
    if requested is not None:
        if not bool(context.extras.get("all_warehouse_access")) and not set(requested).issubset(set(context.extras.get("warehouse_ids", ()) )):
            raise ValueError("WAREHOUSE_NOT_FOUND")
        warehouse_ids = requested
    elif warehouse_id is not None:
        warehouse_ids = [warehouse_id]
    db = SessionLocal()
    try:
        rows = build_sku_health(db, workspace_id=context.workspace_id, coverage_days=payload.get("coverage_days", 14), warehouse_ids=warehouse_ids)
        return {"items": rows, "total": len(rows), "coverage_days": payload.get("coverage_days", 14)}
    finally:
        db.close()


def get_replenishment_evidence_handler(payload: dict, context: ToolContext) -> dict:
    _reject_workspace_override(payload)
    warehouse_id, _ = _warehouse_scope(payload, context)
    if warehouse_id is None:
        raise ValueError("WAREHOUSE_REQUIRED")
    db = SessionLocal()
    try:
        result = calculate_replenishment(
            db,
            workspace_id=context.workspace_id,
            warehouse_id=warehouse_id,
            sku_id=payload["sku_id"],
            coverage_days=payload.get("coverage_days", 14),
            as_of=date.fromisoformat(payload["as_of"]) if payload.get("as_of") else None,
        )
        result["as_of_date"] = result["as_of_date"].isoformat()
        if result["daily_avg_qty"] is not None:
            result["daily_avg_qty"] = float(result["daily_avg_qty"])
        return result
    finally:
        db.close()


GET_INVENTORY_TOOL = AgentTool(
    name="get_inventory", description="查询当前工作空间的已确认内部库存；只读，不修改库存。",
    input_schema={"type": "object", "properties": {"warehouse_id": {"type": "integer"}, "sku_id": {"type": "integer"}, "page": {"type": "integer"}, "page_size": {"type": "integer"}}, "required": []},
    handler=get_inventory_handler, is_readonly=True, timeout_seconds=3.0, max_calls_per_minute=30, requires=("inventory.read",),
    output_schema={"type": "object", "properties": {"items": {"type": "array"}, "total": {"type": "integer"}, "page": {"type": "integer"}, "page_size": {"type": "integer"}}},
)

LIST_SKU_HEALTH_TOOL = AgentTool(
    name="list_sku_health", description="查询 SKU 库存健康、销量、可售天数和确定性补货建议；只读。",
    input_schema={"type": "object", "properties": {"coverage_days": {"type": "integer"}, "warehouse_id": {"type": "integer"}, "warehouse_ids": {"type": "array"}}, "required": []},
    handler=list_sku_health_handler, is_readonly=True, timeout_seconds=5.0, max_calls_per_minute=20, requires=("replenishment.read",),
    output_schema={"type": "object", "properties": {"items": {"type": "array"}, "total": {"type": "integer"}, "coverage_days": {"type": "integer"}}},
)

GET_REPLENISHMENT_EVIDENCE_TOOL = AgentTool(
    name="get_replenishment_evidence", description="计算单个 SKU 的补货公式依据；不创建建议、不提交采购。",
    input_schema={"type": "object", "properties": {"warehouse_id": {"type": "integer"}, "sku_id": {"type": "integer"}, "coverage_days": {"type": "integer"}, "as_of": {"type": "string"}}, "required": ["warehouse_id", "sku_id"]},
    handler=get_replenishment_evidence_handler, is_readonly=True, timeout_seconds=5.0, max_calls_per_minute=20, requires=("replenishment.read",),
    output_schema={"type": "object", "properties": {"warehouse_id": {"type": "integer"}, "sku_id": {"type": "integer"}, "suggested_qty": {"type": "integer"}, "data_completeness": {"type": "string"}}},
)


def register_operator_skills(registry) -> None:
    for tool in (GET_INVENTORY_TOOL, LIST_SKU_HEALTH_TOOL, GET_REPLENISHMENT_EVIDENCE_TOOL):
        if registry.get(tool.name) is None:
            registry.register(tool)
