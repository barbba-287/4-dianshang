"""Deterministic fixtures for the read-only MCP interview demo."""
from __future__ import annotations

from app.skills_ecommerce import SkillContext


class MockEcomDataSource:
    def sales(self, *, workspace_id: int, sku_id: int, days: int) -> list[dict]:
        return [{"sales_date": f"2026-09-{index + 1:02d}", "net_qty": 4 + (index % 5)} for index in range(days)]

    def ad(self, *, workspace_id: int, campaign_id: str) -> dict | None:
        return {"record_id": f"mock-{campaign_id}", "spend": 100, "attributed_sales": 260, "clicks": 420, "conversions": 18, "data_completeness": "complete"}

    def inventory(self, *, workspace_id: int, warehouse_id: int) -> list[dict]:
        return [{"sku_id": 101, "sku_code": "MOCK-TEA-001", "on_hand_qty": 3, "reorder_point_qty": 8, "days_of_inventory": 2.5}, {"sku_id": 102, "sku_code": "MOCK-MUG-001", "on_hand_qty": 40, "reorder_point_qty": 8, "days_of_inventory": 20}]

    def products(self, *, workspace_id: int, category: str | None) -> list[dict]:
        return [{"product_id": 1, "title": "Mock Green Tea", "growth_rate": 0.22, "days_of_inventory": 8}, {"product_id": 2, "title": "Mock Mug", "growth_rate": 0.03, "days_of_inventory": 30}]


def demo_context() -> SkillContext:
    return SkillContext(workspace_id=1, actor="interview-demo", request_id="demo-request-001", source_mode="mock")
