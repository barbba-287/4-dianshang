"""Read-only e-commerce analysis skills for the interview MCP demo."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol

from app.db import DailySkuSale, ExternalAccount, InventoryBalance, InventoryPolicy, Product, ProductSku, SessionLocal, Warehouse
from sqlalchemy import func, select


class EcomDataSource(Protocol):
    def sales(self, *, workspace_id: int, sku_id: int, days: int) -> list[dict]: ...
    def ad(self, *, workspace_id: int, campaign_id: str) -> dict | None: ...
    def inventory(self, *, workspace_id: int, warehouse_id: int) -> list[dict]: ...
    def products(self, *, workspace_id: int, category: str | None) -> list[dict]: ...


@dataclass(frozen=True)
class SkillContext:
    workspace_id: int
    actor: str
    request_id: str
    source_mode: str = "mock"
    timezone: str = "UTC"


def _require_scope(context: SkillContext) -> None:
    if context.workspace_id is None or context.workspace_id <= 0:
        raise ValueError("WORKSPACE_CONTEXT_REQUIRED")


def analyze_sales_fluctuation(*, sku_id: int, days: int, context: SkillContext, source: EcomDataSource) -> dict[str, Any]:
    _require_scope(context)
    if sku_id <= 0 or days < 2 or days > 90:
        raise ValueError("INVALID_SALES_WINDOW")
    rows = source.sales(workspace_id=context.workspace_id, sku_id=sku_id, days=days)
    values = [int(row.get("net_qty", 0)) for row in rows]
    total = sum(values)
    midpoint = max(1, len(values) // 2)
    previous = sum(values[:midpoint])
    recent = sum(values[midpoint:])
    change = None if previous == 0 else (recent - previous) / previous
    completeness = "complete" if len(rows) >= days else "insufficient"
    return {"metric": "sales_fluctuation", "sku_id": sku_id, "days": days, "total_net_qty": total, "recent_qty": recent, "previous_qty": previous, "change_rate": change, "direction": "up" if change is not None and change > 0.1 else "down" if change is not None and change < -0.1 else "stable" if change is not None else "unknown", "data_completeness": completeness, "source": context.source_mode, "as_of": date.today().isoformat(), "timezone": context.timezone, "evidence": rows}


def analyze_ad_roi(*, campaign_id: str, context: SkillContext, source: EcomDataSource) -> dict[str, Any]:
    _require_scope(context)
    if not campaign_id.strip():
        raise ValueError("CAMPAIGN_ID_REQUIRED")
    row = source.ad(workspace_id=context.workspace_id, campaign_id=campaign_id)
    if not row:
        return {"metric": "ad_roi", "campaign_id": campaign_id, "status": "unknown", "reason": "CAMPAIGN_NOT_FOUND", "source": context.source_mode, "as_of": date.today().isoformat(), "timezone": context.timezone}
    spend = float(row.get("spend", 0))
    sales = float(row.get("attributed_sales", 0))
    roi = None if spend <= 0 else sales / spend
    return {"metric": "ad_roi", "campaign_id": campaign_id, "status": "known" if spend > 0 else "unknown", "spend": spend, "attributed_sales": sales, "roi": roi, "clicks": row.get("clicks"), "conversions": row.get("conversions"), "data_completeness": row.get("data_completeness", "complete"), "source": context.source_mode, "as_of": date.today().isoformat(), "timezone": context.timezone, "evidence": {"record_id": row.get("record_id")}}


def check_inventory_alert(*, warehouse_id: int, context: SkillContext, source: EcomDataSource) -> dict[str, Any]:
    _require_scope(context)
    rows = source.inventory(workspace_id=context.workspace_id, warehouse_id=warehouse_id)
    alerts = []
    for row in rows:
        on_hand = int(row.get("on_hand_qty", 0))
        reorder = int(row.get("reorder_point_qty", 0))
        days = row.get("days_of_inventory")
        if on_hand <= reorder or (days is not None and float(days) <= 3):
            alerts.append({"sku_id": row.get("sku_id"), "sku_code": row.get("sku_code"), "severity": "critical" if on_hand <= 0 else "warning", "on_hand_qty": on_hand, "reorder_point_qty": reorder, "days_of_inventory": days, "reason": "LOW_STOCK" if on_hand <= reorder else "LOW_COVERAGE"})
    return {"metric": "inventory_alert", "warehouse_id": warehouse_id, "alert_count": len(alerts), "alerts": alerts, "data_completeness": "complete" if rows else "insufficient", "source": context.source_mode, "as_of": date.today().isoformat(), "timezone": context.timezone}


def find_product_opportunities(*, category: str | None, context: SkillContext, source: EcomDataSource) -> dict[str, Any]:
    _require_scope(context)
    rows = source.products(workspace_id=context.workspace_id, category=category)
    candidates = []
    for row in rows:
        growth = row.get("growth_rate")
        coverage = row.get("days_of_inventory")
        if growth is not None and coverage is not None and float(growth) > 0.1 and float(coverage) < 14:
            candidates.append({"product_id": row.get("product_id"), "title": row.get("title"), "growth_rate": growth, "days_of_inventory": coverage, "reason": "GROWTH_WITH_LOW_COVERAGE"})
    return {"metric": "product_opportunities", "category": category, "count": len(candidates), "items": candidates, "data_completeness": "complete" if rows else "insufficient", "source": context.source_mode, "as_of": date.today().isoformat(), "timezone": context.timezone}
