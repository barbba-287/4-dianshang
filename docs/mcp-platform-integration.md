# MCP 与当前电商平台接入说明

`app/mcp_server_official.py` 的官方 MCP transport 已切换为 `PlatformEcomDataSource`：

```text
query_sales_trend       → daily_sku_sales
check_inventory_alert   → inventory_balances + inventory_policies
find_product_opportunities → products（当前缺增长/库存证据时返回 insufficient）
get_ad_performance      → 当前项目没有广告事实表，返回 unknown
```

这意味着 MCP 不再只是 Mock 演示：销售和库存工具会读取当前项目 Workspace 数据库。广告 ROI 仍明确标记为未知，因为当前项目没有广告花费/归因销售额模型；不能伪造数据。

## 运行前提

官方 MCP Server 的 demo context 当前固定使用 workspace `1`，仅用于本地面试演示。生产 Webman 接入必须从认证 session/可信 gateway 派生 workspace、actor 和 request_id，不能把固定值带入生产。

```powershell
cd D:\ai\dianshang
python -m app.mcp_server_official
```

它是 stdio Server，会等待 MCP Client；不要把“启动后没有普通提示符”当作卡死。使用 MCP Inspector 或支持 MCP 的客户端调用 `tools/list` / `tools/call`。

## 面试诚实口径

- 销售趋势和库存预警：读取当前自有电商平台数据库；
- 商品机会：当前商品数据可读，但没有完整增长/竞争/利润证据时返回 `insufficient`；
- 广告 ROI：当前项目没有广告事实表，因此保留 Mock/Webman contract 或返回 `unknown`；
- 真实 Webman：只需替换 `PlatformEcomDataSource` 为 `WebmanEcomDataSource`，Skill 和 MCP 工具契约不变；
- 所有工具只读，不提交采购、不改库存、不修改广告。
