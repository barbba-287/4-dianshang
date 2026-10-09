"""Minimal JSON-RPC MCP-style server for the interview demo.

This module is provider-neutral and deliberately read-only. It exposes the
same discover/call contract that can be mounted behind a real MCP transport;
the transport is kept dependency-free for a reproducible local demo.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict

from app.skills_ecommerce import SkillContext, analyze_ad_roi, analyze_sales_fluctuation, check_inventory_alert, find_product_opportunities
from app.skills_ecommerce_fixture import MockEcomDataSource

TOOLS = {
    "query_sales_trend": {"description": "查询 SKU 销售趋势（只读）", "inputSchema": {"type": "object", "properties": {"sku_id": {"type": "integer"}, "days": {"type": "integer"}}, "required": ["sku_id", "days"]}},
    "get_ad_performance": {"description": "查询广告投产表现（只读）", "inputSchema": {"type": "object", "properties": {"campaign_id": {"type": "string"}}, "required": ["campaign_id"]}},
    "check_inventory_alert": {"description": "查询仓库库存预警（只读）", "inputSchema": {"type": "object", "properties": {"warehouse_id": {"type": "integer"}}, "required": ["warehouse_id"]}},
    "find_product_opportunities": {"description": "查询商品机会候选（只读）", "inputSchema": {"type": "object", "properties": {"category": {"type": ["string", "null"]}}, "required": []}},
}


def _context(params: dict) -> SkillContext:
    if "workspace_id" in params or "tenant_id" in params:
        raise ValueError("WORKSPACE_PARAMETER_FORBIDDEN")
    # In production this comes from the authenticated Webman/session context;
    # the local demo fixes it to a non-secret fixture workspace.
    return SkillContext(workspace_id=1, actor="mcp-demo", request_id="mcp-local-request", source_mode="mock")


def call_tool(name: str, arguments: dict) -> dict:
    context = _context(arguments)
    source = MockEcomDataSource()
    if name == "query_sales_trend":
        result = analyze_sales_fluctuation(sku_id=int(arguments["sku_id"]), days=int(arguments["days"]), context=context, source=source)
    elif name == "get_ad_performance":
        result = analyze_ad_roi(campaign_id=str(arguments["campaign_id"]), context=context, source=source)
    elif name == "check_inventory_alert":
        result = check_inventory_alert(warehouse_id=int(arguments["warehouse_id"]), context=context, source=source)
    elif name == "find_product_opportunities":
        result = find_product_opportunities(category=arguments.get("category"), context=context, source=source)
    else:
        raise ValueError("TOOL_NOT_FOUND")
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}


def handle(message: dict) -> dict:
    method = message.get("method")
    if method in {"initialize", "notifications/initialized"}:
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "dianshang-ecommerce-analysis", "version": "0.1.0"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": {"tools": [{"name": name, **spec} for name, spec in TOOLS.items()]}}
    if method == "tools/call":
        try:
            params = message.get("params") or {}
            return {"jsonrpc": "2.0", "id": message.get("id"), "result": call_tool(params.get("name", ""), params.get("arguments") or {})}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32602, "message": str(exc)}}
    return {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32601, "message": "METHOD_NOT_FOUND"}}


def main() -> None:
    for line in sys.stdin:
        if line.strip():
            print(json.dumps(handle(json.loads(line)), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
