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

注意：`DEMO_MODE_ENABLED=true` 只放行 seed-demo 数据种子命令，对登录鉴权与 API 鉴权零影响——运维不要把"演示模式开着"理解为任何鉴权被放宽（详见 `docs/demo-path-audit-20261026.md` D-1，现状即此语义，2026-10-27 确认）。

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

## 响应留存（排障用）

每次 live 同步会把上游原始响应**原样**落盘，出问题可回取同一份数据（不再依赖 `raw_ref` 合成串）：

- 位置：`artifacts/shopify_sync/<run_id>/`，每页一对文件：
  `<request_id>.json`（原样正文）或 `<request_id>.json.truncated`（超单文件上限，只存前 N 字节）
  + `<request_id>.meta.json`（request_id / http_status / attempt / elapsed_ms / query / variables / truncated / size）。
- 上限：`SHOPIFY_RETENTION_MAX_BYTES`（默认 10485760 = 10MB）。
- 查看：`python -m app.cli retention list --run-id <run_id>`；
  取正文 `python -m app.cli retention get --run-id <run_id> --request-id <request_id>`。
  **截断文件会被拒绝解析**（明确报"截断、非完整响应"），不得作为重放基准。
- 台账可查：`resource_status_json` 的资源条目里有 `retention`（`status=ok|truncated|failed`、`files`、`dir`）。
- 留存失败**不影响同步成功**（降级为 warning + 台账 failed），但排障时先看台账 `retention` 是否 `ok`。
- 留存文件含店铺真实业务数据：**不外传**（不进 git / 聊天 / 导出），目录已被 `.gitignore` 覆盖。

## 面试演示口径

> 这是官方 Dev Store 的真实 API 数据结构验证，不是生产店铺经营数据。我的重点是把第三方 API 适配为只读外部事实层：先做 scope、分页、错误和幂等，再让上层 Skill/MCP 读取统一事实；外部库存观察值和内部库存台账分开，避免同步直接污染业务库存。

## 故障排查

- `SHOPIFY_LIVE_NOT_CONFIGURED`：检查本地 `.env` 和开关，不在聊天中粘贴 Token；
- `SHOPIFY_AUTH_FAILED`：检查 Token 是否属于当前店铺及 read scopes；
- `SHOPIFY_GRAPHQL_ERROR`：HTTP 200 仍需检查 GraphQL `errors`；
- 库存权限错误：当前查询只读取 `inventoryItems → inventoryLevels → quantities(available) → location.id`，不依赖 `read_locations` 的名称字段；
- 订单为 0：表示当前查询范围没有可见订单，不自动写成失败；
- 同步为 partial：查看资源级状态，订单和库存可独立重试。
