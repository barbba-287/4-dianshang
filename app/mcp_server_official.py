"""Official MCP SDK server compatible with MCP 1.x and 2.x.

The user's current Conda environment may contain MCP 2.x, where FastMCP was
renamed to MCPServer. This module keeps the same four read-only tools and
uses whichever official server class is available.
"""
from __future__ import annotations

from typing import Any

try:
    from mcp.server.fastmcp import FastMCP as _Server
except ModuleNotFoundError:
    from mcp.server.mcpserver import MCPServer as _Server

from app.skills_ecommerce import SkillContext, analyze_ad_roi, analyze_sales_fluctuation, check_inventory_alert, find_product_opportunities
from app.skills_ecommerce_fixture import MockEcomDataSource
from app.skills_ecommerce_platform import PlatformEcomDataSource

mcp = _Server(name="dianshang-ecommerce-analysis", instructions="只读电商分析工具；workspace 从可信上下文派生，不接受模型覆盖。")
_source = PlatformEcomDataSource()


def _context() -> SkillContext:
    return SkillContext(workspace_id=1, actor="mcp-demo", request_id="mcp-official-demo", source_mode="platform")


@mcp.tool()
def query_sales_trend(sku_id: int, days: int) -> dict[str, Any]:
    """查询 SKU 销售趋势；只读，缺失数据返回 insufficient。"""
    return analyze_sales_fluctuation(sku_id=sku_id, days=days, context=_context(), source=_source)


@mcp.tool()
def get_ad_performance(campaign_id: str) -> dict[str, Any]:
    """查询广告花费、归因销售额和 ROI；只读，缺归因返回 unknown。"""
    return analyze_ad_roi(campaign_id=campaign_id, context=_context(), source=_source)


@mcp.tool()
def check_inventory_alert(warehouse_id: int) -> dict[str, Any]:
    """查询仓库库存预警；只读，不修改库存。"""
    return check_inventory_alert(warehouse_id=warehouse_id, context=_context(), source=_source)


@mcp.tool()
def find_product_opportunities(category: str | None = None) -> dict[str, Any]:
    """识别商品机会候选；只读，不编造利润或竞争结论。"""
    return find_product_opportunities(category=category, context=_context(), source=_source)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
