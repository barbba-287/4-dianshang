# P2 入口调研：只读 Canary「可停 / 可回放 / 可审计」缺口矩阵

- 日期：2026-10-26；基线：`feat/warehouse-mvp` @ `1cb7698`（R1+R2 已合并）
- 方式：**只读**（读代码、模型、路由、`.env` 键名与开关值；未发起任何上游请求、未改一行代码）
- 上位依据：`docs/engineering-roadmap.md` P2 行（"只读 Canary 可停、可回放、可审计"）+ 纵向切片要求
- 结论：**不建议直接进 P2 主体**，建议先做一个 4 项的「P2-0 护栏」切片（见 §6）

## 1. P2 入口条件（路线图原文）

| 项 | 原文 |
|---|---|
| 阶段 | P2 外部事实生产化 |
| 目标 | 建立可持续平台同步 |
| 关键能力 | Connector、授权引用、source batch、质量、回放、补偿 |
| 入口 | 至少一个平台测试授权 |
| DoD | 只读 Canary **可停、可回放、可审计** |

## 2. 现状盘点（已具备的底座）

| 组件 | 位置 | 现状 |
|---|---|---|
| 只读适配器契约 | `app/platform_adapters/base.py` | `ReadOnlyPlatformAdapter` 协议，暴露 `read_only/live_enabled/simulated` |
| Shopify 适配器 | `app/platform_adapters/shopify.py` | GraphQL 只读；20s 超时、2 次重试、429/5xx 退避、`max_pages`/`page_size` 上限 |
| 淘宝适配器 | `app/platform_adapters/taobao.py` | `TAOBAO_ADAPTER_ENABLED=false`（未启用） |
| 同步运行模型 | `app/db.py:389 ExternalSyncRun` | status/attempt/retry_of_run_id/resource_status_json/retry_idempotency_key/心跳/计数/error_code |
| 快照与事件 | `ExternalInventorySnapshot`(:426)、`ExternalEventInbox`(:461) | 唯一约束含 `payload_hash` + `idempotency_key`；`source_mode`/`simulated`/`raw_ref` 落库 |
| 同步编排 | `app/external_sync.py`、`app/sync_application.py` | `begin_sync_run`/`initialize_resources`/`mark_resource_*`/`retry_fixture_bundle` |
| 接口 | `app/main.py:1750 /api/external/shopify/sync`、`:2280` 运行查询、`:2310` 重试、`:2252` fixture 同步 | — |
| 后台任务 | `app/background.py` | ThreadPoolExecutor + lease/heartbeat + `request_cancel`/`is_cancel_requested` + `stop_background_workers(5s)` |
| 本地回放 | `app/catalog_replay.py` + CLI `replay-catalog` | manifest 指定本地 HTML，离线 Playwright，不触网 |
| 凭证开关 | `app/config.py` | `shopify_live_enabled`、`shopify_store_domain`、`shopify_access_token` 三者同时成立才走 live |

## 3. 缺口矩阵

级别：**P0** = 做 Canary 前必须补；**P1** = 首批 Canary 期间必须补；**P2** = 可延后。

### 3.1 可停（Stoppable）

| # | 能力项 | 现状证据 | 缺口 | 级别 |
|---|---|---|---|---|
| S1 | 配置级总开关 | `main.py:1754` 三条件与才走 live；`config.py:64` 默认 False；**P2-0a 新增进程内热开关**（`app/runtime_flags.py` + `POST /api/admin/live-switch`） | 已具备（部分）：热开关为进程内存态，单进程假设成立；多 worker 部署需外部协调 | **已具备（P2-0a）** |
| S2 | 单次运行时间预算 | **P2-0a**：run 级 wall-clock `shopify_sync_budget_seconds`（默认 300s）+ 页间检查点 + `pages_used` 台账留痕 | 已具备：超预算终态 `failed + SHOPIFY_SYNC_BUDGET_EXCEEDED` | **已具备（P2-0a）** |
| S3 | 运行中取消 | 后台任务有 `request_cancel`；**P2-0a**：进程内 cancel token + 页间/资源间检查点 + `POST /api/external/sync/runs/{run_id}/cancel`（202/404/409） | 已具备（部分）：取消在检查点生效，**当前页 transport 等待不可中断** | **已具备（部分，P2-0a）** |
| S4 | 上游退避与限流 | `shopify.py:60-66`：429/5xx 重试、`Retry-After` 优先、指数退避上限 8s | 无并发闸门（多请求同时触发即多路拉取），无令牌桶 | P2 |
| S5 | 只读保证 | `read_only=True`（`base.py`/`shopify.py:101`），仅 GraphQL query，无 mutation 分支 | 无编译期/静态检查强制"只读"（靠约定） | P2 |
| S6 | 触发权限门槛 | `main.py:1752` 需 `inventory.write`；`EMPLOYEE_AUTH_ENABLED=true` | 与 `DEMO_MODE_ENABLED=true` 并存，需确认 demo 路径不能触达 live（见 §5-K2） | P1 |
| S7 | 进程级停止 | `stop_background_workers(5s)` + `executor.shutdown(cancel_futures=True)` | 只覆盖后台任务，**不覆盖同步 HTTP 路径** | P1 |

### 3.2 可回放（Replayable）

| # | 能力项 | 现状证据 | 缺口 | 级别 |
|---|---|---|---|---|
| R1 | 抓取类离线回放 | `app/catalog_replay.py`（manifest → 本地 HTML，不触网）+ CLI `replay-catalog` | 仅覆盖淘宝类目抓取，与平台同步链路无关 | 已具备（范围外） |
| R2 | fixture 同步与重试 | `/api/external/sync/fixture`（:2252）、`/runs/{run_id}/retry`（:2310）→ `retry_fixture_bundle`，带 `Idempotency-Key`、返回 `replayed` 标记 | **只回放调用方传入的 fixture 内容**，不是"捕获过的真实响应" | 已具备（部分） |
| R3 | live 响应留存 | **P2-0b 项 4 已落地**：`ShopifyLiveTransport` 逐页原样落盘 `artifacts/shopify_sync/<run_id>/<request_id>.json`（超限 `.json.truncated` + meta），台账 `retention{status,files,dir}` 留痕；CLI `retention list|get` 读取 | 已具备：出问题可按 run 取回同一份数据；`raw_ref` 合成串问题（`shopify.py:217`）不再是回放唯一依据 | **已具备（P2-0b 项 4）** |
| R4 | live 运行重试 | 重试端点走 fixture 语义（`retry_fixture_bundle`） | live run **不可重试/不可重放**；只能重新拉一次（数据已变） | P1 |
| R5 | 幂等与去重 | 快照唯一约束含 `payload_hash` + `idempotency_key`；`uq_external_sync_run_retry_idempotency` | 已具备（重复导入安全） | 已具备 |
| R6 | 断点续跑 | `AdapterPage.next_cursor` 存在于分页循环内 | **cursor 未在 run 级持久化**，中断后只能从首页重来（叠加 S2 的耗时问题） | P1 |

### 3.3 可审计（Auditable）

| # | 能力项 | 现状证据 | 缺口 | 级别 |
|---|---|---|---|---|
| A1 | 运行记录完整度 | `ExternalSyncRun`：status/attempt/retry_of/counts(inserted/updated/no_op/conflict/stale)/resource_status_json/error_code/heartbeat | 已具备（这一项是全仓做得最扎实的） | 已具备 |
| A2 | 谁触发了这次拉取 | run 表无 `actor`/`created_by`；该路径无 `request_id` 落库（`grep request_id` 在 `external_sync/platform_sync/sync_application` 均为 0） | **无法回答"这次 live 拉取是谁、从哪个请求触发的"** | P1 |
| A3 | 上游请求 id | 适配器每页生成 `shopify-<uuid12>` 并放进 `X-Request-ID` 头（`shopify.py:117-118`），响应里回传 | **未落库**，对账上游日志时无从关联 | P1 |
| A4 | 数据来源标记 | `source_mode`/`simulated` 落到 run / snapshot / order / account | 已具备 | 已具备 |
| A5 | 业务动作审计 | 25 处 `actor=`（tickets、补货决策、草稿等）；`app/agent/audit.py` 仅记 Agent 工具调用（JSONL） | 无统一业务审计表；跨模块动作无法按主体/时间串起来 | P2 |
| A6 | 观测指标 | `/health`、`/ready`、`/api/external/snapshots/freshness`、alerts refresh | 无 `/metrics`、无 Prometheus、无同步耗时/错误率指标 | P2 |
| A7 | 异常可发现 | alerts refresh + freshness 接口 | 无推送/告警通道（仅接口轮询） | P2 |

统计：**P0 缺口 3 项**（S2 预算、S3 取消、R3 留存），**P1 缺口 6 项**，已具备 5 项。

## 4. 关键洞察（决定"要不要现在进 P2"）

1. **底座比预期好，短板集中且明确**：运行记录（A1）、幂等（R5）、只读契约（S5）、退避（S4）都在；缺的三样**全部围绕"这次 live 拉取"本身**——跑多久、能不能中止、事后能不能复现。
2. **S2+S3 叠加是最现实的事故形态**：同步在 HTTP 请求内跑，100 页上限 × 单页最长 60s，最坏近 100 分钟，期间既不能取消也无法观测进度，客户端早已超时。这不是理论风险，是当前参数的直接推论。
3. **R3 是"可回放"的根**：没有原始响应留存，其余回放能力（R2 的 fixture、R1 的本地 HTML）都无法覆盖 live 数据；R4/R6 也就无从谈起。**留存方案一旦定，R4/R6 顺带解决**。
4. **A2/A3 是"可审计"的最后一段**：数据侧标记（A4）齐了，但"谁触发、上游哪次请求"缺位，事故复盘时会卡在最基本的归因上。

## 5. 三个需要你拍板的前置问题

| 编号 | 问题 | 依据 | 影响 |
|---|---|---|---|
| K1 | **`.env` 里 Shopify 凭证是测试店铺还是正式店铺？** | `SHOPIFY_LIVE_ENABLED=true`；`SHOPIFY_STORE_DOMAIN` 27 字符、`SHOPIFY_ACCESS_TOKEN` 38 字符（只判长度，未读取值；`.env` 已被 `.gitignore:5` 忽略、未入库） | 若是正式店，P2-0 护栏是**上线前必做**而非"建议"；Canary 只能用测试店 |
| K2 | **`DEMO_MODE_ENABLED=true` 与 `EMPLOYEE_AUTH_ENABLED=true` 并存是否符合预期？** | `.env` 两者同时为 true；`require_principal` 在 employee auth 开启时拒绝 demo/api_key 主体 | 若 demo 路径能绕过到 live sync，权限门槛形同虚设 → 需一次显式回归 |
| K3 | **Canary 期间的 live 开关策略**：运行期可关（DB/文件热开关）还是接受"改 env + 重启"？ | S1 缺口 | 决定 P2-0 是否包含热开关；重启窗口内无法止损 |

## 6. 建议：先做 P2-0「护栏」切片，再做 P2 主体

推荐顺序（**P2-0 不碰业务逻辑，只加约束与留痕**）：

| 切片 | 内容 | 关闭缺口 | 规模估计 |
|---|---|---|---|
| P2-0a | 运行预算：`wall-clock deadline`（如 120s）+ 记录数上限；超限主动中止并落 `error_code=SYNC_BUDGET_EXCEEDED` | S2 | 小（适配器循环 + run 字段） |
| P2-0b | 可取消：同步改后台 job（复用 `background.py` 的 cancel/lease），或至少加"取消标记 + 循环内检查" | S3、S7 | 中 |
| P2-0c | live 响应留存：每页原始 JSON 落盘/落表（`payload_hash` + `raw_ref` 指向真实文件），并提供"按 run_id 重放" | R3，顺带 R4/R6 | 中 |
| P2-0d | 审计补全：run 记录 `actor` + 请求 `request_id` + 上游 `request-id` 列表 | A2、A3 | 小 |
| P2-0e | 热开关：运行期可关（复用 alerts/配置表或文件开关），不必重启 | S1 | 小 |

之后才进 P2 主体（connector 生产化、source batch、质量门禁、补偿编排）。

**替代路线（若不接受 P2-0 先行）**：

- **方案 B**：直接做主体，护栏随做 → 风险：live 已开、无预算无取消无留存，第一次超长拉取就会污染 run 表且无法复现。**不推荐**。
- **方案 C**：先把 `SHOPIFY_LIVE_ENABLED` 关掉，P2 主体在 fixture 上做到"可回放"完备，再开 live → 成本最低但进度慢，适合 K1 答案是"正式店"时。

## 7. 未知项（Unknown，不可从仓库判定）

- 凭证有效性、店铺类型（测试/正式）、实际 rate limit 额度 → 需一次**只读** capabilities 调用或由你确认（本次调研刻意未发起）。
- 生产库类型（SQLite / MySQL）与 `external_sync_runs` 表实际行数、历史 run 的 `source_mode` 分布。
- Shopify API 版本兼容性（`SHOPIFY_API_VERSION` 已配，未验证 schema 漂移）。
- 现存 `data/` 下是否已有历史 live 快照（`raw_ref` 是否出现过非 mock 前缀）。

## 8. 变更记录

| 日期 | 变更 | 说明 |
|---|---|---|
| 2026-10-26 | 新建 | P2 入口只读调研 + 缺口矩阵；P0 3 项 / P1 6 项 / 已具备 5 项；建议先做 P2-0 护栏 |
| 2026-10-27 | **P0 三项全部收口** | S2 预算、S3 取消（P2-0a，`72653ca`）；R3 留存（P2-0b 项 4，`932a0d6`+`d23d54f`）。矩阵行已同步改判，残留降级见下 |

## 9. P0 收口记录（2026-10-27）

| 原 P0 项 | 交付 | 证据 | 残留（已降级，非 P0） |
|---|---|---|---|
| S2 无 wall-clock 预算（最坏 ~100 分钟/资源） | P2-0a：`shopify_sync_budget_seconds=300` + 页间检查点 | `tests/test_p2a_sync_guardrails.py` T1×2/T2 绿；错误码 `SHOPIFY_SYNC_BUDGET_EXCEEDED` | 预算只覆盖页间，单页 transport 等待仍受 `timeout×retries` 约束 |
| S3 同步阻塞不可取消 | P2-0a：cancel token + 页间/资源间检查点 + `POST /api/external/sync/runs/{run_id}/cancel` | T3/T4/T4b 绿；终态 `failed + SHOPIFY_SYNC_CANCELLED` | 取消非即时（页间生效）；单进程假设 |
| R3 live 响应无原样留存 | P2-0b 项 4：transport 逐页原样落盘（`.json` / 超限 `.json.truncated` + meta）、台账 `retention` 留痕、CLI 读取 | `tests/test_p2b_retention.py` 7 条绿（含截断、凭证零落盘双路径、fixture 零留存）；设计定稿 v1.2 `docs/p2-retention-design-20261027.md` | 无自动清理（策略已定：30 天、整目录删）；无自动重放（本期只保证"人可取回"）；非 Dev Store 接生产前需重审脱敏 |

**结论**：只读 Canary 的"可停 / 可回放"已从 P0 降级为既有能力 + 已登记残留；下一步可进 P2 主体
（Connector / source batch / 质量 / 补偿），残留项按 §3 表的 P1/P2 级别排期。
