# R4 任务文件：live 同步断点续跑（POST /runs/{run_id}/resume）

> 创建：2026-10-27（WorkBuddy）。上级总纲：`dev_task.p1-residuals-s4-r6-r4.md` §3。
> **前置 R6 已收口（e42f474）**：`cursor_json` 列、`record_sync_cursor`、`resume_plan()` 均已落地，
> 本任务只做消费端。基线：**368 passed / 0 failed / EXIT=0**（复核方 2026-10-27 独立实测）。
> e42f474 暂留本地，与 R4 收口后一起推（用户推送策略，见 §0.1）。

## 0. 模板固化条款（全部适用）

| 条款 | 本任务落地 |
|---|---|
| R-15 | 验证命令统一 `env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1` 前缀（R6 全程如此，未遇 SystemExit:1；沿用） |
| R-17 | 完成即写 `.testtmp/delivery.r4.md`（落盘） |
| R-20 | §1 现状每条附实测方法；控制流断言读整函数或跑探针 |
| R-27 | 开工前逐条跑 §3b 前置校验；任一不满足停手报告 |
| R-28 | 已知限制/残留带编号登记。**编号现状**：R-31（seen 不持久化，R4 交付时按 §7 第 4 条方法复核）、R-32（未触发，已按阈值关闭）、**R-33 空置记录：batch 例外已批但实现未走 batch，该编号保留不登记事项、亦不复用于其他主题**（避免后来者看到空号误以为漏登记）。**本任务新编号从 R-34 起** |
| R-29 | 开工前 + 提交前各查一次 `ps -W \| grep -Ei "pytest\|uvicorn\|claude\|codex\|cursor\|code\b"`；有输出停手报告 |
| §3b P7 | 提交前 `git diff --cached --numstat`；删除数非 0 必须解释 |

### 0.1 推送策略（用户 2026-10-27 裁定，长期约定）

**按「阶段收口 / 风险点 / 上限触发」推，不按切片推。**
当前 `e42f474`（R6）暂留本地；**R4 收口后一起推**。实施方不得自行 push。

### 3b. 前置校验（接手方开工时逐条跑并粘贴结果）

| 项 | 期望 |
|---|---|
| P5 进程 / git 锁 | 无 AI CLI 与测试进程残留、无 `.git/*.lock` |
| 工作区 | `git status` 干净（HEAD = e42f474） |
| R6 定向 | `tests/test_r6_cursor_persistence.py` 17 passed |
| 基线 | 全量 368 passed / 0 failed / EXIT=0（约 135s） |

## 1. 现状（Fact，附实测方法）

| # | 事实 | 位置 | 实测方法 |
|---|---|---|---|
| F1 | `resume_plan(cursor_json_str, targets) -> ResumePlan` 已落地，判定表与任务文件 §2.2 逐行一致（`{}` 快速通道→全量重拉；NULL/非 JSON→NO_CURSOR；缺键/非 1→UNSUPPORTED_CURSOR_SCHEMA；complete→SKIP） | `app/external_sync.py:306-` | 读整函数（R-20） |
| F2 | `ResumePlan` 含 `allowed / reason / resources{name→ResourceResume(action: RESTART/RESUME/SKIP, cursor, pages, fetched)}`；`begin_sync_run` 已支持 `retry_of_run_id / attempt / retry_idempotency_key` 入参 | `app/external_sync.py`（dataclass 区 + `begin_sync_run:470-485`） | grep + 读整函数 |
| F3 | 既有 `/api/external/sync/runs/{run_id}/retry`（fixture 语义，`SyncApplicationService.retry_fixture_bundle`）**不动** | `app/main.py:2565-2576` | 读路由整段 |
| F4 | S4 单飞闸门在 `_shopify_sync_impl`：非阻塞抢锁→`_shopify_sync_run`→`finally` 释放；`begin_sync_run` 之前占槽 | `app/main.py:1860-1893` | 读整段 |
| F5 | `_shopify_sync_run` 从零构建 adapter（无 `start_cursor` 传入）；adapter 已支持 `start_cursor` / `on_page_done`（R6） | `app/main.py:1893-`；`shopify.py:217-258` | 读两段 |
| F6 | 留存目录按 run_id 分目录：`artifacts/shopify_sync/<run_id>` | `app/main.py` `_shopify_sync_run` 内 | grep `retention_dir` |
| F7 | 取消路由 `/runs/{run_id}/cancel` 判定 `platform=="shopify" and source_mode=="shopify_live"` 才有效——**resume 的新 run 也是 shopify_live，取消天然适用**（无需改 cancel） | `app/main.py` cancel 路由 | 读整段 |

## 2. 目标

### 2.1 新路由

`POST /api/external/sync/runs/{run_id}/resume`，鉴权与 `/sync` 相同
（`require_principal(request, permission="inventory.write")` + CSRF）。

**处理顺序（两段式，闸门只包上游拉取段）**：

1. **前置校验段（不占闸门，只读库）**：
   - run 不存在或不属当前 workspace → 404 `SYNC_RUN_NOT_FOUND`；
   - `platform != "shopify"` 或 `source_mode != "shopify_live"` → 409（reason=`NOT_LIVE_RUN`）；
   - `status not in (failed, partial)` → 409，reason=`NOT_RESUMABLE_STATE`；
   - `resources` 请求参数（缺省=源 run 的资源集）与 `resume_plan` 判定：
     `resume_plan(source.cursor_json, targets)` → `allowed=False` → 409，reason=plan 的 reason
     （`NO_CURSOR` 仅 NULL/损坏；`UNSUPPORTED_CURSOR_SCHEMA` 缺键/非 1）；
     `{}` / 空条目 → **允许**（RESTART=全量重拉，**不是 409**）；
   - plan 全部 SKIP（所有目标资源都已 complete）→ 409，reason=`NOTHING_TO_RESUME`。
2. **执行段（占 S4 同一把单飞闸门）**——**进闸后必须重读源 run（TOCTOU 防护，用户 2026-10-27 追加）**：
   前置校验段与抢锁之间存在时间窗，源 run 状态可能已被并发请求改变
   （被 cancel、被另一次 resume 改写 cursor_json、甚至手工重跑）。因此**持锁后第一件事**
   是按主键重读源 run 并**复核全部前置条件**（status / platform / source_mode）；
   任一不再满足 → 释放锁并按 §2.1 同款 409/404 返回（不得"校验过了就直接跑"）。
   cursor_json 同样以**持锁后的重读值**为准调 `resume_plan`（不是前置段的旧值）。
   其余执行项：
   - 复用 `_shopify_sync_impl` 的闸门包装（建议提取公共 helper，或 resume impl 内同样的
     非阻塞 acquire + finally 释放；**顺序铁律不变：抢锁在 `begin_sync_run` 之前**）；
   - 与 `/sync` 并发互斥：sync 或 resume 任一在跑，另一方 409 `SHOPIFY_SYNC_IN_PROGRESS`；
   - 新建 run：`begin_sync_run(..., retry_of_run_id=源.run_id, attempt=源.attempt+1,
     source_mode="shopify_live", simulated=False)`；
   - **每个资源按 plan.action 分派**：`RESUME` → adapter `start_cursor=<cursor>`；
     `RESTART` → `start_cursor=None`；`SKIP` → **不调用 adapter**，直接
     `mark_resource_succeeded`（台账标注 resumed/skip，沿用源 run 的 fetched 计数基数）；
   - **pages/fetched 累计与预算严格分离（用户 2026-10-27 追加）**：
     * `cursor_json.resources[x].pages/fetched` 是**跨 run 累计值**（续跑台账口径），
       以 plan 携带的源基数为起点继续累加；
     * **页数预算 `shopify_max_pages` 与 wall-clock 预算均为单 run 口径**：新 run 的
       `pages_used`（resource_status_json 里的那个）**从 0 计**，不继承源 run 已用页数；
       wall-clock 同理（总纲 §3.2 已定，此处补页数侧）；
     * 实现上两套计数**不得混用同一变量**：累计值只进 cursor_json，单 run 值只进
       resource_status_json/护栏判断。混用会导致"续跑几次后页数预算被历史耗尽"的隐性 bug。
   - **SKIP 资源的状态与台账（用户 2026-10-27 追加，写法定死）**：
     * 状态：`mark_resource_succeeded`（SKIP 不是失败也不是"未开始"）；
       新 run 的 `attempt` 无论 SKIP 与否一律 = 源 attempt + 1（SKIP 只跳过拉取，
       **不改变 run 级链路元数据**：attempt、retry_of_run_id、resumed_from 照常记录）；
     * `resource_status_json` 该资源条目 metadata 必须含：
       `{"resumed": "skip", "resumed_from_run": <源run_id>, "source_pages": <源累计pages>, "source_fetched": <源累计fetched>}`，
       使台账能区分"本次真拉取的成功"与"承袭源 run 的成功"；
     * 响应体 `resources[x]` 同样携带 `"resumed": "skip"` 与源基数，前端/CLI 可见；
     * SKIP 资源**不得**写 `pages_used`（没拉页），断言其缺省或为 0。
   - 留存目录 = **新 run_id** 目录（不复用源目录）；台账记录 `resumed_from=<源 run_id>`；
   - 预算与取消：新 run **自己的** wall-clock 预算（不继承源 run 剩余量）；取消 token 照 R6/P2-0a 流程。

### 2.2 错误码定死（用户 2026-10-27 追加：不新增第四个 code）

| HTTP | `code`（DomainError 第 5 槽位之外不加） | reason（进 `message`，`reason=<XXX>` 格式） |
|---|---|---|
| 404 | `SYNC_RUN_NOT_FOUND` | — |
| 409 | `SHOPIFY_SYNC_IN_PROGRESS` | —（S4 既有码，复用） |
| 409 | `SYNC_RUN_NOT_RESUMABLE` | `NOT_LIVE_RUN` / `NOT_RESUMABLE_STATE` / `NOTHING_TO_RESUME` / `NO_CURSOR` / `UNSUPPORTED_CURSOR_SCHEMA` |
| 409 | `SYNC_RUN_NOT_CANCELLABLE` | —（既有，不改） |

- resume 自身的 409 **一律** code=`SYNC_RUN_NOT_RESUMABLE`，细分靠 reason——**不得**为每个
  reason 单造 code（错误码面扩张会让前端/运维字典膨胀，且与 R1 逐字节契约精神相悖）。
- reason 枚举**定死为上述 5 个**；实现中发现需要第 6 个 → 停手报告，不得自行加。
- 响应外形仍走既有处理器：`{"detail": {"code", "message"}}`，**不扩 DomainError 结构**（S4 先例）。

### 2.3 响应外形

成功 → 200，body 与 `/sync` 同构（`run_id`/`platform`/`source_mode`/`status`/`resources{...}`），
**外加** `"resumed_from": "<源 run_id>"`。
所有 409 走 `DomainError`（R1 错误体 `{"detail":{"code","message"}}`）；
reason 放 `message` 内以 `reason=<XXX>` 形式承载（**不扩 DomainError 结构**，同 S4 先例）。

## 3. 边界与红线

- `/retry` 的 fixture 语义**一字不动**（F3，有既有测试锁定）。
- 不做自动续跑/后台轮询；仅人工触发。
- 不改 `db.py` / migration / `resume_plan` 判定逻辑（R6 已冻结；发现判定不够用→停手报告）。
- 不改 `/sync`、`/cancel` 既有语义；闸门 helper 重构若必须动 `_shopify_sync_impl`，
  **改动前后 S4 的 5 例必须逐例不回退**。
- 不引入第三方依赖。

## 4. 用例（新建 `tests/test_r4_resume.py`）

复用 R6 测试的 harness 手法（FakeShopifyTransport + page_hook + Event 确定性同步；禁 sleep 赌时序）。

| # | 场景 | 期望 |
|---|---|---|
| R4-1 | status=succeeded 的 run 调 resume | 409，reason=`NOT_RESUMABLE_STATE` |
| R4-2 | 失败的 orders-only run（cursor=orders-0，complete=false）resume | 新 run 从第 2 页开始：transport 对 orders 的调用次数 = 总页数−1；`resumed_from`=源 run_id；attempt=源+1 |
| R4-3 | inventory 已 succeeded（complete=true）+ orders 失败，resume | **inventory 零上游调用**（SKIP 路径），台账标注；orders 照续 |
| R4-4 | resume 新 run 超预算（预算调 0） | 新 run failed + `SHOPIFY_SYNC_BUDGET_EXCEEDED`；**源 run 字段不变**（status/cursor_json 均不动） |
| R4-5 | 源 run 运行中（S4 卡页法）调 resume | 409 `SHOPIFY_SYNC_IN_PROGRESS`——**闸门共享**的直接证明 |
| R4-6 | 源 run `cursor_json` 列为 NULL（手工 UPDATE 置 NULL） | 409，reason=`NO_CURSOR` |
| R4-6b | 源 run `cursor_json == "{}"`（未开始分页） | **不是 409**：新 run 全量重拉（RESTART），等同新 sync |
| R4-7 | `cursor_json.schema_version=99` | 409，reason=`UNSUPPORTED_CURSOR_SCHEMA` |
| R4-8 | `cursor_json` 缺 `schema_version` 键 | 409，reason=`UNSUPPORTED_CURSOR_SCHEMA` |
| R4-9 | resume 途中取消（page_hook 卡页 + `/cancel` 打新 run_id） | 新 run failed + `SHOPIFY_SYNC_CANCELLED`；**游标保留**（再 resume 可继续——R6-3 语义串联） |
| R4-10 | **链式续跑**（用户 2026-10-27 定死语义）：run A 失败 → resume 生成 B，B 也失败（在第 2 页后）→ 再对 **B** 调 resume 生成 C | ① C 的 `retry_of_run_id` = **B**（直接前驱，不是最初的 A——`attempt` 单调 +1 即可追溯全链）；② C 的 pages 基数 = **B 落库的累计值**（≥ A 的累计值），验证 pages 严格递增不回退不重置；③ C 的 `resumed_from` = B 的 run_id |
| R4-11 | **闸门反方向**（用户 2026-10-27 追加）：resume 在跑（page_hook 卡页），此时发 `/sync` 请求 | `/sync` 409 `SHOPIFY_SYNC_IN_PROGRESS`——证明闸门是同一把、方向对称（R4-5 的反例） |
| R4-12 | **TOCTOU**：前置校验通过后、抢锁前，源 run 被并发取消（变成 succeeded/终态） | 持锁后重读发现状态不再满足 → 409 `NOT_RESUMABLE_STATE`，**不建新 run、不留 running 孤儿**（构造法：前置段返回前用 Event 卡住实施路径，或直接单测 resume impl 的重读分支） |

## 5. 改动范围预期（P7 核对基准）

| 文件 | 预期 |
|---|---|
| `app/main.py` | +`/resume` 路由 + resume impl；闸门包装如需重构，`_shopify_sync_impl` 行为等价（diff 尽量小） |
| `app/external_sync.py` | 如需 `SKIP` 资源的台账标记函数（+≤20 行）；判定逻辑零改动 |
| `tests/test_r4_resume.py` | 新增（§4 用例 R4-1..R4-10） |
| 其余 | **零**；发现必须动 → 停手报告 |

## 6. 验收口径

| 层 | 判据 | 阻断 |
|---|---|---|
| **主判据 ①** | 全量 **368 + N** passed / 0 failed / EXIT=0。**N 不写死**：N = 实际新增用例数（§4 目前列 13 例——R4-1..R4-12 含 6b；实施中用例增删以 delivery 声明数为准并说明理由，**不得为凑数硬造用例，也不得静默少于 §4**） | 是 |
| **主判据 ②** | 定向：`test_r4_resume.py` 全绿 | 是 |
| **主判据 ③** | 相关零回退：`test_r6_cursor_persistence.py`（17）+ `test_p2a_sync_guardrails.py`（10）+ `test_s4_sync_gate.py`（5）+ `test_r19_worker_leak.py`（7）+ `test_alembic.py` 全绿——**尤其 S4 5 例**（闸门重构风险点） | 是 |
| 回退实验（R6-9 同款） | 摘掉 resume 的"按 plan 分派"（如把 RESUME 退化成 RESTART）→ R4-2 变红；交付说明附实测输出 | 是 |

## 7. 交付说明（R-17）

`.testtmp/delivery.r4.md` 必含：

1. 与 R6 的接口消费方式（plan.action → adapter 分派的代码位置）；
2. 闸门共享的实现方式与 S4 5 例零回退证明；
3. 回退实验输出（R6-9 同款，不依赖转述）；
4. **R-31 去重兜底复核（方法定死，用户 2026-10-27 追加）**：
   - **结论从实测来，不是引述 R6 文档**。方法：利用 R4-2/R4-10 场景中"续跑会重拉重叠页"的
     天然条件，在续跑完成后对**本次新 run 写入的记录**做重复检查——
     `SELECT external_line_id, COUNT(*) ... GROUP BY ... HAVING COUNT(*) > 1`（orders 以
     external_line_id、inventory 以 external_sku+location 维度），断言**零重复**；
   - 同时确认既有 `payload_hash`/`idempotency_key` 去重路径未被 resume 改动
     （`git diff` 不得触及 ingest 层去重函数——§5 已把 external_sync.py 限为 +≤20 行台账标记）；
   - 两条都满足才写"R-31 兜底仍成立"；任何一条不满足 → 停手报告并升级为新编号。
5. 新增残留（R-34 起，如有）；
6. 全量数字与命令形态、numstat 粘贴与逐文件解释、R-29 提交前检查输出。

## 8. 完成标准

- §6 三层主判据 + 回退实验全过；
- 提交信息含 delivery 要点摘录；**不自行 push**（§0.1）；
- R4 收口 = 执行线「P0 → S4 → R-19 → R6 → R4」全清，届时按用户推送策略一次性推送。
