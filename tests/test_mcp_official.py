import json
import subprocess
import sys


def test_official_mcp_module_imports():
    completed = subprocess.run([sys.executable, "-c", "from app.mcp_server_official import mcp; print(len(mcp._tool_manager._tools))"], text=True, capture_output=True, check=True)
    assert int(completed.stdout.strip()) == 4


def test_legacy_json_rpc_demo_remains_reproducible():
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "check_inventory_alert", "arguments": {"warehouse_id": 1}}}
    completed = subprocess.run([sys.executable, "-m", "app.mcp_server"], input=json.dumps(message) + "\n", text=True, capture_output=True, check=True)
    response = json.loads(completed.stdout)
    assert response["result"]["isError"] is False
