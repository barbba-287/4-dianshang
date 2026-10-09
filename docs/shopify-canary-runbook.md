# Shopify Dev Store 只读 Canary Runbook

## 目标

用 Shopify Dev Store 的真实 Admin GraphQL 响应验证第三方 API 契约，并将订单、库存接入当前平台的外部事实层。该流程不执行 Shopify mutation，不写回平台库存，不把测试店铺数据描述成生产经营数据。

## 配置边界

`.env` 本地配置：

```dotenv
SHOPIFY_LIVE_ENABLED=true
SHOPIFY_STORE_DOMAIN=<本地配置的 myshopify 域名>
SHOPIFY_ACCESS_TOKEN=<仅本地环境变量，不提交、不打印>
SHOPIFY_API_VERSION=2026-07
```

`.env.example` 只保留空变量名。日志、数据库和接口响应不得出现 Token。

## 页面验证

启动：

```powershell
cd D:\ai\dianshang
python -m uvicorn app.main:app --reload
```

打开：`http://127.0.0.1:8000/shopify`

点击“刷新真实数据”，核对：

- 商品：当前 Dev Store 真实返回 17 条；
- 库存：当前真实返回 28 条库存位置记录；
- 订单：当前已创建测试订单，真实返回 3 条；
- 页面显示 `真实 Shopify 数据`，不显示 fixture/mock。

## 同步验证

在页面点击“同步到外部事实层”，或调用：

```text
POST /api/external/shopify/sync?resources=orders,inventory
```

同步结果：

- `orders` → `ExternalOrder` / `ExternalOrderLine`，并重建 `DailySkuSale`；
- `inventory` → `ExternalInventorySnapshot`；
- 不写 `InventoryBalance`、`InventoryTransaction`；
- 不执行 Shopify mutation；
- 返回 `run_id`、资源级状态、inserted/no-op/conflict 和数据完整度；
- 订单金额/退款字段未被当前查询完整覆盖时标记 `partial/unknown`，不伪造真实金额。

重复点击同步：

- 相同订单 payload 应为 no-op 或更新判断；
- 相同库存 payload 通过稳定 `idempotency_key` + `payload_hash` 去重；
- 不应产生重复事实记录。

## 面试演示口径

> 这是官方 Dev Store 的真实 API 数据结构验证，不是生产店铺经营数据。我的重点是把第三方 API 适配为只读外部事实层：先做 scope、分页、错误和幂等，再让上层 Skill/MCP 读取统一事实；外部库存观察值和内部库存台账分开，避免同步直接污染业务库存。

## 故障排查

- `SHOPIFY_LIVE_NOT_CONFIGURED`：检查本地 `.env` 和开关，不在聊天中粘贴 Token；
- `SHOPIFY_AUTH_FAILED`：检查 Token 是否属于当前店铺及 read scopes；
- `SHOPIFY_GRAPHQL_ERROR`：HTTP 200 仍需检查 GraphQL `errors`；
- 库存权限错误：当前查询只读取 `inventoryItems → inventoryLevels → quantities(available) → location.id`，不依赖 `read_locations` 的名称字段；
- 订单为 0：表示当前查询范围没有可见订单，不自动写成失败；
- 同步为 partial：查看资源级状态，订单和库存可独立重试。
