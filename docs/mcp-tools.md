## Official MCP transport

面试演示优先使用官方 Python MCP SDK 版本：

```powershell
cd D:\ai\dianshang
python -m app.mcp_server_official
```

它使用 `mcp.server.fastmcp.FastMCP` 和 stdio transport，暴露 4 个只读工具：

- `query_sales_trend`
- `get_ad_performance`
- `check_inventory_alert`
- `find_product_opportunities`

当前数据源是 `MockEcomDataSource`，因此这是 E2/E3 可复现演示，不是真实 Webman 接入。领域 Skill、输入输出语义和安全边界与 `app/mcp_server.py` 共用；未来只替换数据源为 `WebmanEcomDataSource`。

### 为什么保留两个入口

- `mcp_server_official.py`：官方 MCP SDK transport，面试时说明标准 MCP 实现；
- `mcp_server.py`：无额外 SDK 的 JSON-RPC 调试入口，方便快速 smoke 和排查。

两者都只读，不提供写工具，不接受模型传入的 workspace/tenant 覆盖。

### 安全要求

当前 demo 固定使用 workspace fixture `1`。生产实现必须从已认证的 Webman/session/gateway 上下文构造 `ToolContext`，不能把固定 workspace 逻辑直接带入生产；Token 通过 transport 透传或 secret_ref 注入，不写代码、数据库或日志。
