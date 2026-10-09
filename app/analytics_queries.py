"""只读分析查询应用服务。

该模块只负责把已认证范围内的查询编排成 API 所需的响应，不拥有
Workspace/RBAC 决策，也不改变底层指标公式或业务事实。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class AnalyticsQueryError(Exception):
    """查询输入或底层指标构建失败时使用的稳定领域错误。"""

    code: str

    def __str__(self) -> str:
        return self.code


class AnalyticsQueryService:
    """分析查询的应用编排门面。

    ``workspace_id`` 必须由调用方从已认证 principal 传入；服务不接受
    前端 workspace 参数，也不负责推导或切换工作空间。
    """

    def __init__(self, db: Session):
        self.db = db

    def sales(self, *, workspace_id: int, as_of: date | None = None) -> dict:
        from app.analytics import build_sales_summary

        return build_sales_summary(self.db, workspace_id=workspace_id, as_of=as_of)

    def inventory_health(
        self,
        *,
        workspace_id: int,
        coverage_days: int = 14,
        as_of: date | None = None,
        warehouse_id: int | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        from app.analytics import build_inventory_health

        offset = (page - 1) * page_size
        body = build_inventory_health(
            self.db,
            workspace_id=workspace_id,
            coverage_days=coverage_days,
            as_of=as_of,
            warehouse_id=warehouse_id,
            limit=page_size,
            offset=offset,
        )
        body["page"] = page
        body["page_size"] = page_size
        return body

    def product_quadrant(
        self,
        *,
        workspace_id: int,
        as_of: date | None = None,
        growth_window: int = 7,
        baseline_window: int = 14,
        days_window: int = 14,
        warehouse_id: int | None = None,
        growth_high: float | None = None,
        days_low: float | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        from app.analytics import build_product_quadrant

        try:
            summary = build_product_quadrant(
                self.db,
                workspace_id=workspace_id,
                as_of=as_of,
                growth_window=growth_window,
                baseline_window=baseline_window,
                days_window=days_window,
                warehouse_id=warehouse_id,
                growth_high=growth_high,
                days_low=days_low,
            )
        except ValueError as exc:
            raise AnalyticsQueryError(str(exc) or "ANALYTICS_QUERY_INVALID") from exc

        items = summary.pop("items")
        quality = (
            "complete"
            if all(item.get("data_completeness") == "complete" for item in items)
            else "partial"
            if any(item.get("data_completeness") == "partial" for item in items)
            else "insufficient"
        )
        as_of_value = summary["as_of"]
        timezone = summary["business_timezone"]
        summary["meta"] = {
            "workspace_id": workspace_id,
            "as_of": as_of_value,
            "timezone": timezone,
            "source": "InventoryBalance+DailySkuSale",
            "data_completeness": quality,
            "metric_version": "1",
            "metrics": [
                {
                    "metric": "product_quadrant_items",
                    "value": len(items),
                    "numerator": len(items),
                    "denominator": len(items) or None,
                    "workspace_id": workspace_id,
                    "warehouse_id": warehouse_id,
                    "as_of": as_of_value,
                    "timezone": timezone,
                    "source": "InventoryBalance+DailySkuSale",
                    "data_completeness": quality,
                    "metric_version": "1",
                    "reason": None if quality == "complete" else "INCOMPLETE_COVERAGE",
                }
            ],
            "limitations": summary.get("limitations", []),
        }
        # Preserve the historical API behavior: page is reported, but the
        # query only slices the first page and does not apply an offset.
        summary["items"] = items[:page_size]
        summary["page"] = page
        summary["page_size"] = page_size
        summary["total"] = len(items)
        return summary

    def dashboard_summary(
        self,
        *,
        workspace_id: int,
        days: int = 7,
        as_of: date | None = None,
        warehouse_id: int | None = None,
        sku_id: int | None = None,
        recent_limit: int = 10,
    ) -> dict:
        from app.dashboard import build_dashboard_summary

        return build_dashboard_summary(
            self.db,
            workspace_id=workspace_id,
            days=days,
            as_of=as_of,
            warehouse_id=warehouse_id,
            sku_id=sku_id,
            recent_limit=recent_limit,
        )
