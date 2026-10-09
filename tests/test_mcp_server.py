import json
import subprocess
import sys


def test_mcp_tools_list_and_calls():
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "query_sales_trend", "arguments": {"sku_id": 1, "days": 7}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_ad_performance", "arguments": {"campaign_id": "c-1"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "check_inventory_alert", "arguments": {"warehouse_id": 1}}},
    ]
    result = subprocess.run([sys.executable, "-m", "app.mcp_server"], input="".join(json.dumps(x) + "\n" for x in messages), text=True, capture_output=True, check=True)
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert [item["id"] for item in responses] == [1, 2, 3, 4]
    assert {item["name"] for item in responses[0]["result"]["tools"]} >= {"query_sales_trend", "get_ad_performance", "check_inventory_alert"}
    assert all(item["result"]["isError"] is False for item in responses[1:])


def test_mcp_rejects_workspace_override():
    result = subprocess.run([sys.executable, "-m", "app.mcp_server"], input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "query_sales_trend", "arguments": {"sku_id": 1, "days": 7, "workspace_id": 999}}}) + "\n", text=True, capture_output=True, check=True)
    response = json.loads(result.stdout)
    assert response["error"]["message"] == "WORKSPACE_PARAMETER_FORBIDDEN"
