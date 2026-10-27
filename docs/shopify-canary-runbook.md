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

## live 热开关：无 UI，走 API

> 现状（2026-10-27 核实）：`POST /api/admin/live-switch` 存在且可用，但**没有任何前端调用点**——
> `app/templates/` 与 `app/static/` 中检索 `live-switch` 结果为空，`/shopify` 页面也不含该开关。
> 运维只能用 API / 命令行操作，请勿在页面上找这个按钮。

**开关性质**（先读，避免误判）：

- 路径：`POST /api/admin/live-switch`，body `{"live_enabled": true|false}`，返回 `{"live_enabled": ..., "source": "runtime_flag"}`。
- 权限：仅 `ROLE_ADMIN`（需已登录管理员会话 + `X-CSRF-Token`，取 `dianshang_csrf` cookie 值）。
- **只存进程内存**（`app/runtime_flags.py`），**不落库、不改 `.env`**；进程重启后回落到 `.env` 的 `SHOPIFY_LIVE_ENABLED`。
- **多 worker / 多副本部署时进程内状态不共享**：每个 worker 各自一份。要做全实例关闭，需逐 worker 调用或改为重启加载 `.env`（现行假设是单进程）。
- **同一限制也适用于 S4 的 live 同步单飞闸门**：闸门是进程内 `threading.Lock`（`app/main.py` 的 `_sync_gate_lock`），多 worker 部署时每个 worker 各自独立，**跨进程不具备互斥能力**——两个 worker 可能同时跑 live 同步。登记为 **R-27**，解除条件见 `dev_task.p1-residuals-s4-r6-r4.md`。现行假设仍是单进程，因此该限制不影响当前 Canary。
- 关闭后的预期行为：同步请求返回 `409 SHOPIFY_LIVE_NOT_CONFIGURED`；`capabilities` 报 `live_enabled: false`。

**操作例（关闭 live，先关再排查）**：

> CSRF 取值两种方式，任选其一：
> - **推荐**：浏览器按 F12 → Application/存储 → Cookies → 复制 `dianshang_csrf` 的值（最省事，跨平台不会踩转义坑）。
> - 命令行从 cookies 文件里抠（**Windows Git Bash 下 `\t` 可能不生效**，此写法主要给 Linux/macOS 用；Git Bash 请直接用手工复制）：

```bash
# Linux/macOS（GNU grep 支持 \t）
CSRF=$(grep -o 'dianshang_csrf\t[^;]*' cookies.txt | cut -f2)

# Windows Git Bash：\t 常被当字面量，改用 $'\t' 或直接手填
CSRF=$(grep -o $'dianshang_csrf\t[^;]*' cookies.txt | cut -f2)
# 仍失败就直接把 devtools 里复制的值贴进来：
# CSRF=<粘贴此处>

curl -X POST http://127.0.0.1:8000/api/admin/live-switch \
  -b cookies.txt -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $CSRF" \
  -d '{"live_enabled": false}'
```

重启后恢复 `false`（`.env` 基线）：

```bash
# .env 内保持 SHOPIFY_LIVE_ENABLED=false，重启即回到基线
python -m uvicorn app.main:app --reload
```

**验证是否已关**：

```text
GET /api/external/shopify/capabilities  → live_enabled: false
POST /api/external/shopify/sync         → 409 SHOPIFY_LIVE_NOT_CONFIGURED
```

**多 worker 场景的自查**：若某次调用后同步仍然成功，多半是打到了另一个 worker，逐实例重复调用即可（这是已知限制，不算故障）。

> 归属：这是一条运维可用性缺口（无 UI），不影响"可停"能力（API 可用即达标）。
> 登记见 `docs/review-checklist-20261011.md` R-25；产品化 UI 另行立项，不在当前执行线内。

## 面试演示口径

> 这是官方 Dev Store 的真实 API 数据结构验证，不是生产店铺经营数据。我的重点是把第三方 API 适配为只读外部事实层：先做 scope、分页、错误和幂等，再让上层 Skill/MCP 读取统一事实；外部库存观察值和内部库存台账分开，避免同步直接污染业务库存。

## 故障排查

- `SHOPIFY_LIVE_NOT_CONFIGURED`：检查本地 `.env` 和开关，不在聊天中粘贴 Token；
- `SHOPIFY_AUTH_FAILED`：检查 Token 是否属于当前店铺及 read scopes；
- `SHOPIFY_GRAPHQL_ERROR`：HTTP 200 仍需检查 GraphQL `errors`；
- 库存权限错误：当前查询只读取 `inventoryItems → inventoryLevels → quantities(available) → location.id`，不依赖 `read_locations` 的名称字段；
- 订单为 0：表示当前查询范围没有可见订单，不自动写成失败；
- 同步为 partial：查看资源级状态，订单和库存可独立重试。
