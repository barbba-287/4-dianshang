# P1 残留三件任务文件：S4 → R6 → R4（顺序强制）

> 创建：2026-10-27（WorkBuddy）。用户裁定：先清这三件再进 P2 主体；顺序 **S4 → R6 → R4**；
> **R6 必须是 R4 的前置提交**（R4 的断点续跑依赖 R6 的 cursor；不得反过来）。
> 基线：`339 passed / 0 failed`（`8b84fd4`）。live 开关 `SHOPIFY_LIVE_ENABLED` 保持 `false`。
> 每件独立任务文件另存（`dev_task.s4-concurrency.md` 等），本文件只作总纲与顺序依据。

## 0. 为什么是这个顺序（用户理由，勿改）

| 序 | 项 | 一句话理由 |
|---|---|---|
| 1 | **S4 并发闸门** | 入口控制。当前"单进程假设"必须显式化（锁/单例），否则 R6/R4 会被并发问题污染 |
| 2 | **R6 cursor 持久化** | 断点续跑的基础。R4 要做"续跑"而不是"整 run 重跑"，必须先有 cursor |
| 3 | **R4 live run 重试** | 用 R6 的 cursor 做断点续跑；R6 未合入不得启动 R4 |

---

## 1. S4：并发闸门（第一件）

### 1.1 现状（Fact）

| # | 事实 | 位置 |
|---|---|---|
| F1 | 进程内已有锁：`_sync_cancel_lock`（cancel token dict）、`_executor_lock`（后台执行器） | `main.py:1789`、`background.py:42` |
| F2 | **sync 路由无"同时只允许一个 live run"的闸门**：两个请求并发进来会各自拉取、各自落盘、各自写台账 | `_shopify_sync_impl` 无互斥 |
| F3 | 留存目录按 `run_id` 分目录（天然隔离），但**配额/凭据是共享的**：并发多路 = 上游 40 次/桶 限流叠加更快撞限 | `shopify.py` transport 无令牌桶 |
| F4 | 现有退避：429/5xx 重试 + `Retry-After` 优先 + 指数退避上限 8s；**无并发闸门、无令牌桶** | `shopify.py:60-66` |

### 1.2 目标（Target）

**二选一，倾向 A**（用户原话："要么显式化单进程约束（加锁/单例锁），要么加并发闸"）：

- **方案 A（推荐）单进程单飞闸门**：进程内 `threading.Lock` + 非阻塞 `acquire`；
  拿不到锁 → 立即 `409 SHOPIFY_SYNC_IN_PROGRESS`（带当前 run_id），不排队、不阻塞。
  理由：live Canary 是人工触发、低频；排队会掩盖"误操作连点"；409 让运维立刻知道"已经在跑"。
- **方案 B 并发闸（信号量 N=1，可扩展到 N）**：允许排队，超时 `SHOPIFY_SYNC_QUEUE_TIMEOUT`。
  理由：将来多店家/多资源并行时需要。本期不做，B 记入路线图。

### 1.3 边界

- 只加**闸门**，不改分页、不改预算、不改取消语义（P2-0a 已收口的部分一律不动）。
- fixture 同步路径**不受闸门限制**（它不是 live、不烧配额）；或统一受闸但需在交付说明写明取舍并加用例。
- 必须显式登记残留：**多 worker / 多副本部署时进程内锁失效**（与 P2-0a 热开关同类）。

### 1.4 用例（最小 4 条）

| # | 场景 | 期望 |
|---|---|---|
| S4-1 | 第一个 run 运行中，第二个 sync 请求进来 | 第二个 **409** + `error_code=SHOPIFY_SYNC_IN_PROGRESS`，且第一个不受影响 |
| S4-2 | 第一个 run 结束（成功/失败/取消/异常）后再请求 | 第二个**正常执行**（锁已释放，验证 `finally` 释放） |
| S4-3 | run 因预算/取消提前终止 | 锁必须释放，后续请求可进 |
| S4-4 | 并发同一 run_id 的 cancel 请求 | 不因闸门被拒（cancel 不走 sync 闸门） |

### 1.5 完成标准

- S4 用例定向绿；全量 339+4=343 passed / 0 failed（R-19 规则照常）。
- 409 走既有 `DomainError` 体系；不新增 DB 字段、不改 schema。
- 交付说明必须写：多 worker 残留、为何选 A 不选 B、闸门与 `runtime_flags` 的关系（热开关关闭时是否仍占锁）。

---

## 2. R6：cursor 持久化（第二件，R4 前置）

### 2.1 现状（Fact）

| # | 事实 | 位置 |
|---|---|---|
| F1 | **`ExternalSyncRun` 无 cursor 字段**（已逐字段核对：run_id/platform/account_ref/store_ref/sync_type/source_mode/simulated/status/时间戳/attempt/retry_*/resource_status_json/total/inserted/updated/no_op/conflict…均无 cursor） | `db.py:390-430` |
| F2 | `CrawlJob.cursor`（`db.py:170`，String(255)）是**爬取任务**的，与 sync 无关，不可复用 | — |
| F3 | 分页游标是**页内局部变量** `cursor` / `seen` 集合，退出函数即丢 | `shopify.py:_pages` |
| F4 | 中断后只能从首页重来（叠加 max_pages=100、page_size=50 的耗时） | 缺口矩阵 R6 |

### 2.2 目标（Target）

- `ExternalSyncRun` 新增 **`cursor_json`**（Text，默认 `"{}"`）：
  `{"orders": {"cursor": "<endCursor>", "pages": N, "fetched": M}, "inventory": {...}}`
  ——按资源分键（一次 run 可能同步两个资源，各自游标独立）。
- 每页成功后**落库一次**（与 `record_resource_metadata` 同点提交，避免额外事务）；
  run 终态后保留（可审计，也供 R4 续跑）。
- 适配器 `_pages()` 支持 `start_cursor` 入参与 `on_page_done(cursor, pages, fetched)` 回调
  （默认 `None` = 现有行为零变化，同 P2-0a 护栏参数的兼容手法）。

### 2.3 边界与红线

- **必须新增 Alembic migration**（`migrations/versions/00xx_external_sync_run_cursor.py`），
  因 `db.py` 需加字段；migration 必须可 `downgrade`（删列）且不破坏既有行（默认值填充）。
- 不改 `resource_status_json` 既有结构（cursor 独立字段，不塞进 JSON 台账）。
- 不实现"自动续跑"（那是 R4）；R6 只保证"游标被持久化了，可被读到"。
- `seen` 集合（防环）也要能恢复：建议存最近 N 个游标（或只存最后游标 + 页数，环检测退化）
  ——**取舍必须在交付说明写明**：只存最后游标时，跨续跑的环检测不再完整（登记为已知限制）。

### 2.4 用例（最小 5 条）

| # | 场景 | 期望 |
|---|---|---|
| R6-1 | 正常 3 页同步 | 每页后 `cursor_json[resource].pages` 递增，终值 = 3，`cursor` = 末页 endCursor |
| R6-2 | 中途 fail（模拟第 2 页抛错） | `cursor_json` 保留到第 1 页末端游标 |
| R6-3 | 取消中途 | 同上，游标保留（取消也是可续跑的） |
| R6-4 | migration up/down | `alembic upgrade head` + `downgrade -1` 均成功；既有行 `cursor_json` 为 `"{}"` |
| R6-5 | 双资源并发（orders+inventory） | 两个 key 互不覆盖 |

### 2.5 完成标准

- 用例定向绿；全量 343+5=348 passed / 0 failed。
- migration 在 SQLite（测试）与 MySQL（既有 migration 测试如果覆盖）均通过。
- **R6 必须作为独立提交合入，且合入时间早于 R4 的第一个提交**（用户红线）。

---

## 3. R4：live run 断点续跑式重试（第三件，依赖 R6）

### 3.1 现状（Fact）

| # | 事实 | 位置 |
|---|---|---|
| F1 | 已有 `/api/external/sync/runs/{run_id}/retry`（`main.py:2310` 附近）→ `retry_fixture_bundle` | 缺口矩阵 R4 |
| F2 | 该重试走 **fixture 语义**：重放调用方传入的 fixture，不是 live 数据 | 同上 |
| F3 | live run 失败后只能**重新拉一次**（数据已变），无"从断点继续"能力 | 同上 |
| F4 | R6 之后，`cursor_json` 里有每个资源的末页游标与页数 | 本文件 §2 |

### 3.2 目标（Target）

- 新增 `POST /api/external/sync/runs/{run_id}/resume`（**新路由**，不复用 retry，避免语义混淆）：
  - 前置校验：源 run 存在、属当前 workspace、`status in (failed, partial)`、`platform=shopify`、
    `source_mode=shopify_live`、**有可续游标**（`cursor_json` 非空且该资源确有未取完）；
  - 不满足 → `409 SYNC_RUN_NOT_RESUMABLE`（带原因码）；
  - 满足 → 新建 run（`retry_of_run_id=源 run_id`、`attempt=源+1`），从 `cursor_json` 的游标继续；
  - **已完成资源不重拉**（`resource_status_json.status=succeeded` 且游标到末页）；
  - 留存目录用新 run_id（不复用源目录），台账 `resumed_from` 记录源 run。
- 预算与取消：新 run 拥有**自己的** wall-clock 预算（不继承源 run 的剩余预算）——理由：续跑是新一轮
  上游调用，预算应按新 run 计；此取舍必须在交付说明声明并加用例。

### 3.3 边界与红线

- **不得**改既有 `/retry` 的 fixture 语义（那是另一条链路，有测试锁定）。
- 不做"自动重试"（不做后台轮询/自动续跑）；R4 只提供**人工触发**的续跑。
- 不做跨 run 的幂等去重改动（既有 `payload_hash`/`idempotency_key` 去重不动，续跑天然受益）。

### 3.4 用例（最小 5 条）

| # | 场景 | 期望 |
|---|---|---|
| R4-1 | status=succeeded 的 run 调 resume | 409 `SYNC_RUN_NOT_RESUMABLE` |
| R4-2 | 失败的 orders-only run（游标在第 1 页末）resume | 新 run 从第 2 页开始（transport 调用次数 = 总页数 - 已取页数） |
| R4-3 | 已 succeeded 的 inventory + 失败的 orders，resume | **只拉 orders**，inventory 零上游调用 |
| R4-4 | resume 新 run 超预算 | 新 run `failed + SHOPIFY_SYNC_BUDGET_EXCEEDED`，源 run 不变 |
| R4-5 | 并发 resume 同一 run | 第二个 409（受 S4 闸门；证明三件串起来成立） |

### 3.5 完成标准

- 用例定向绿；全量 348+5=353 passed / 0 failed。
- 路由注册冒烟（`/resume` 在 `app.routes`）。
- 交付说明必须含：预算归属取舍、留存目录不复用的理由、与 S4 闸门的交互。

---

## 4. 三件通用约束

| 项 | 要求 |
|---|---|
| 分块 | 每件按"实现 → 测试"或更小块调用实施，**每块落盘交付说明**（R-22 缓解） |
| 交付说明路径 | `.testtmp/delivery.s4.md` / `.testtmp/delivery.r6.md` / `.testtmp/delivery.r4.md` |
| 提交 | 每件独立提交；R6 提交必须早于 R4 第一个提交（用户红线） |
| 复核 | WorkBuddy 逐件复核（范围 + 独立全量 + 语义边界），通过后报用户 |
| live 开关 | 三件全部合入 **且** 受控演练通过前，`.env` 的 `SHOPIFY_LIVE_ENABLED` 保持 `false` |
| R-19 | 三件完成后、P2 主体前执行 `dev_task.r19-flaky.md`（时间盒 1 天） |
