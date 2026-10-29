# dev_task.p2s4-compensation — P2-4 补偿机制（死信隔离 + 人工重放，live 验证切片）

> 生成时间：2026-10-29（v1.0 草案，待用户评审，未开工）
> 生成方式：WorkBuddy 只读调研（`main.py:2996` 既有 retry 端点、`sync_application.py retry_fixture_bundle`、
> `external_sync.py` finalize/heartbeat/is_retryable_error、`db.py` uq_external_sync_run_retry_idempotency、
> `config.py` external_sync_run_stale_after_seconds、`shopify.py` AdapterError retryable 分类）
> 执行角色：Claude Code（实施）；WorkBuddy（复核）
> 阶段归属：P2 主体 · 切片 4/4（收官片）——**live 验证切片**
> 上位依据：`docs/p2-entry-readiness-20261028.md` B2 裁定（P2-4 补偿）；B3 裁定（R-36 意向）；
> §18.2 复核结论（R-45 拦截语义 = 本片必答）；runbook v1.4 §5.2（live 四条件）

## 0. 两个设计边界（用户 2026-10-29 写死，实施方不得偏离）

1. **R-45 不升级**：P2-3 观察态"不拦截"维持不变。本片**不做自动路由中的硬性拦截**——
   补偿机制处理的是 **sync 失败**与 **quality 警告**两类数据，方式是
   **人工介入 + 死信隔离**，不是自动改道。质量警告**不进死信表**（见 T2）。
2. **R-36 不升级**：本片重试只针对 **failed 的 run** 发起，不需要 409 精度消歧；
   僵尸 run 只做**只读判定与展示**（判定标准见 T6），不强依赖 R-36 的 409 语义。
   → B3 正式关闭：R-36 维持 P3 观察挂起，无升级。

## 1. 目标（三个，不扩围）

- **T1 死信隔离**：run 级失败自动登记死信表（新表 + migration 0022），
  错误分类（auto/manual）、退避时刻、attempt 上限一次算死落库。
- **T2 人工入口**：CLI + API 两套死信操作（list / show / replay / ignore / zombie 视图），
  全部人工触发，**无后台调度器**（R-27 单进程定调，见 §1 边界）。
- **T3 补偿幂等**：重放不产生重复事实——DB 级复用既有唯一约束 + 数据级复用 R-31 幂等键
  （已生产实锤 §15.5），死信行自身防重复登记/重复重放。

**明确不做**（超围即停）：自动定时重试守护进程、跨进程分布式锁、
自动清理/删除 run 行、对 succeeded run 的任何写操作、质量拦截语义。

**🚫 禁区（用户 2026-10-29 醒目裁定，违反即退回）**：

**严禁复用 `POST /api/external/sync/runs/{run_id}/retry` 路由及其背后的
`sync_application.py retry_fixture_bundle` fixture 重放逻辑。** 该端点是
**本地假数据重放语义**（必传 orders/inventory 内容文件，F1）——死信 replay 若挂上去，
上线一跑拉的是 fixture 假数据而非上游真实数据。死信重放必须走本片新增的
`dead-letter replay`（CLI/API），调起**真实外网同步链路**（既有 /sync + R4 resume）。
`sync_application.py` 全程零改动（§3 禁触清单）。

## 2. 现状事实（F1–F8，实施前 P4 校验逐条核实）

| # | 事实 | 位置 |
|---|---|---|
| F1 | `POST /api/external/sync/runs/{run_id}/retry` 已存在，但仅 **fixture 语义**（必传 orders/inventory 内容，sync_type 限 fixture_bundle/orders/inventory，status 限 failed/partial） | `main.py:2996`、`sync_application.py:59` |
| F2 | 幂等硬约束已有：`uq_external_sync_run_retry_idempotency(workspace_id, retry_of_run_id, retry_idempotency_key)` + `Idempotency-Key` 头校验 + `IDEMPOTENCY_KEY_REUSE` 409 + retry_payload_hash | `db.py:393`、`main.py:3001` |
| F3 | 错误可重试分类素材已有：`AdapterError.retryable`（SHOPIFY_TIMEOUT / SHOPIFY_RATE_LIMITED / SHOPIFY_UPSTREAM_ERROR=True）+ `is_retryable_error()` + 资源级 `retryable_resources_json` 落库 | `shopify.py:78,85,106`、`external_sync.py:521` |
| F4 | 错误码全集：SHOPIFY_SYNC_BUDGET_EXCEEDED / SHOPIFY_SYNC_CANCELLED / SYNC_RUN_NOT_CANCELLABLE / SYNC_RUN_NOT_FOUND / SYNC_RUN_NOT_RESUMABLE / SYNC_RUN_NOT_RETRYABLE / PAYLOAD_HASH_CONFLICT / SHOPIFY_LIVE_NOT_CONFIGURED | `grep` 实测 |
| F5 | 僵尸判定素材现成：`heartbeat_at` 列 + `external_sync_run_stale_after_seconds`（默认 **900s**，既有配置） | `config.py:41` |
| F6 | run 状态集：running / succeeded / failed（cancelled 为运行中取消的终态）；finalize 失败路径单点在 `finalize_resource_run`（P2-3 质量挂接同函数） | `external_sync.py:553-561,673,690` |
| F7 | attempt / retry_of_run_id 链式语义已有（R4 收口），begin_sync_run 签名支持 attempt/retry_idempotency_key/retry_payload_hash 透传 | `external_sync.py:623-650` |
| F8 | migration head = 0021；基线全量 **420 passed / 0 failed**（P2-3 后口径） | `migrations/versions/` |

## 3. 改动面（允许清单，超清单须偏差声明）

| 文件 | 动作 |
|---|---|
| `app/dead_letters.py` | **新增**：死信登记/分类/退避计算/重放前置校验/僵尸判定（纯模块，领域逻辑） |
| `app/db.py` | 新增 `ExternalSyncDeadLetter` 模型（新表） |
| `migrations/versions/0022_external_sync_dead_letters.py` | **新增** |
| `app/external_sync.py` | **仅一处挂接**：`finalize_resource_run` 失败路径追加 `register_dead_letter(db, run)`（与 `apply_quality_gate` 同款单点模式）；**零其他改动** |
| `app/cli.py` | 新增 `dead-letter list|show|replay|ignore` 子命令 |
| `app/main.py` | 新增 3 个端点（list / replay / ignore，见 T4） |
| `app/schemas.py` | 新增死信响应 schema（只增不改既有） |
| `app/config.py` | 新增 `sync_compensation_max_attempts: int = 3`、`sync_compensation_backoff_base_seconds: int = 30`（两个新配置，理由：无可复用的既有配置，R-46 教训已评估） |
| `tests/test_p2s4_compensation.py` | **新增**（D-1..D-14 + D-7b） |
| `docs/review-checklist-20261011.md` | §19 台账登记（R-48..） |
| ⚠️ 禁止触碰 | `app/sync_application.py`（fixture retry 既有语义不动）、P2-2 归因实现、P2-3 质量实现、`app/source_quality.py`、`app/identity.py` |

连带许可（预先授权，仍须偏差声明）：`tests/test_alembic.py` head 断言跟随；
`tests/test_p2s3_quality.py` / `tests/test_p2s2_source_batch.py` 中钉死版本号的
`upgrade` 目标行**如需**跟随推进（R6-4 同款，只改目标行不改断言语义）。

## 4. 交付内容

### T1 死信表（migration 0022）

`external_sync_dead_letters`：

| 列 | 类型 | 说明 |
|---|---|---|
| id | PK | |
| workspace_id | Integer FK NULL, index | 多租户隔离，强制 |
| run_id | String(64), index | 来源 run（**不 FK**，run 行永不删除，软引用） |
| platform / sync_type / source_mode | String | 来源快照（登记时定死，不随源变） |
| error_code | String(64) | 来源 run.error_code |
| error_message | String(500) | |
| retry_class | String(16) | `auto` / `manual`（登记时按 §4 分类表算死） |
| attempt | Integer | 来源 run.attempt |
| next_replay_at | DateTime NULL | **退避门槛（用户补丁 1）**：auto 类 = 失败时刻 + 退避（attempt=1 首登 ≈ +30s±25%），replay 时强校验 `now() >= next_replay_at`，未到 → 409 `DEAD_LETTER_BACKOFF_NOT_REACHED`；manual 类恒 NULL（replay 被 manual_only 挡死，无需此字段参与） |
| exhausted | Boolean, default False | attempt ≥ 上限时 True（open 上的标记位，不是状态） |
| quality_status | String(16) NULL | 登记时源 run 的质量结论快照（上下文，不参与判定） |
| status | String(16) | `open` / `replayed` / `ignored`（三态，见状态流转） |
| replayed_run_id | String(64) NULL | 重放产生的新 run |
| actor / request_id | String NULL | **操作归因**：谁 replay/ignore（复用 P2-2 ContextVar；系统路径同 req-bg- 约定） |
| ignore_reason | String(500) NULL | ignore 必填（API 校验 422） |
| created_at / updated_at | DateTime | |

**约束（写死）**：唯一约束 `(workspace_id, run_id)` —— 一个 run 至多一条死信；
upsert 语义 = 已存在则**不重复登记**（幂等登记，finalize 重入安全）。

**migration 两约束**：① down 逐列还原 0021 形态（R6-4 同款逐列断言：列名/类型/可空/
默认值/位置 + 索引 + 唯一约束整体相等）；② 全列 nullable 或带默认，无数据回填，
**SQLite batch 例外不使用**（必须用时停下按 R-33 先例报告）。

### T2 登记触发与范围（写死）

- **登记条件**：`finalize_resource_run` 落定 `run.status == "failed"` 时自动登记
  （含资源级部分失败后整体 failed 的情形；预算耗尽 SHOPIFY_SYNC_BUDGET_EXCEEDED 天然落入）。
- **不登记**：succeeded（D-12 负向锁）；**quality failed 但 run succeeded 的 run 不进死信**
  ——观察态边界不被本片破坏（R-45 不升级的锁死用例）。质量警告的人工出口 =
  既有 `CLI quality show` + 本片 `dead-letter list --quality <warn|failed>` **只读视图**
  （join run 表展示，不落死信行、不可 replay）。
- **cancelled**：SHOPIFY_SYNC_CANCELLED 的 run **登记**，`retry_class=manual`
  （人工取消永不自动重试——分类表锁死，D-4）。
- 幂等登记：同 run 重复 finalize 不产生第二行（唯一约束 + 先查后插）。

### T3 重试策略（写死，三件套）

**① 分类表**（模块常量 `RETRY_CLASS_BY_CODE`，测试锁死）：

| retry_class | error_code | 理由 |
|---|---|---|
| `auto` | SHOPIFY_TIMEOUT / SHOPIFY_RATE_LIMITED / SHOPIFY_UPSTREAM_ERROR | 瞬时类：与 AdapterError.retryable=True 对齐（恰好 3 码，用户裁定 Q 补丁 3） |
| `manual` | SHOPIFY_SYNC_BUDGET_EXCEEDED | **资源耗尽状态，归人工**（用户 2026-10-29 裁定）：预算耗尽若因"数据太大/上游太慢"，无脑自动重试只是原地撞墙。人工出路 = 排查后 ignore 死信 + 调大 `SHOPIFY_SYNC_BUDGET_SECONDS` 经正常 /sync 重新发起（不设"强制重试"后门） |
| `manual` | SHOPIFY_SYNC_CANCELLED | 人工取消永不自动重试 |
| `manual` | **其余一切**（含 SHOPIFY_LIVE_NOT_CONFIGURED / PAYLOAD_HASH_CONFLICT / 未知码） | 未知即人工，缺数据不硬给结论 |

**② 退避**：`next_replay_at = failed_at + base × 2^(attempt−1) × (1 + jitter)`，
jitter ∈ [0, 0.25] 均匀随机（写入时一次算死，不随读取漂移）；
base = `sync_compensation_backoff_base_seconds`（30s）。
attempt=1 → ≈30~37s；attempt=2 → ≈60~75s；attempt=3 → ≈120~150s。
**无自动触发**（R-27 单进程定调，不起线程/守护进程），且**强校验**（用户补丁 1）：
replay 入口（CLI 与 API 共用同一校验函数）必须比对 `now() >= next_replay_at`，
未到期 → 409 `DEAD_LETTER_BACKOFF_NOT_REACHED`——人工连点两下也过不去，退避才有意义。

**③ 上限**：`attempt ≥ sync_compensation_max_attempts`（默认 3）→ `exhausted=True`，
replay 拒绝（409 DEAD_LETTER_EXHAUSTED）；上限拍板值非 SLA（R-49 留痕）。
exhausted 后出路仅两条：人工 `ignore`（归档）或用户显式调大配置后人工重试
（配置读取发生在 replay 时，改配置即解锁——不另设"强制"参数）。

**状态流转**：

```
open ──replay 成功──▶ replayed（终态，replayed_run_id 落库）
open ──ignore──────▶ ignored（终态，ignore_reason 必填）
open（attempt≥3）──▶ exhausted=True（标记位；仅可 ignore）
replayed / ignored ──▶ 任何再操作 409（DEAD_LETTER_NOT_OPEN）
```

### T4 人工入口

**CLI**（`app/cli.py`）：

| 子命令 | 行为 |
|---|---|
| `dead-letter list [--status S] [--class C] [--quality Q] [--zombie] [--limit N]` | 列表；`--quality` 只读视图（见 T2）；`--zombie` 僵尸视图（见 T6） |
| `dead-letter show <run_id>` | 单条详情（含源 run 归因四件套 + quality 摘要） |
| `dead-letter replay <run_id> --yes` | 发起重放；幂等键自动生成 `dl-<run_id>-<attempt>`（deterministic，同参数重放天然命中既有约束） |
| `dead-letter ignore <run_id> --reason "..."` | 归档；reason 必填 |

**API**（`app/main.py`，全部过 `require_principal`）：

| 端点 | 权限 | 说明 |
|---|---|---|
| `GET /api/external/sync/dead-letters` | inventory.read | list，支持 status/retry_class 过滤 |
| `POST /api/external/sync/dead-letters/{run_id}/replay` | catalog.write | 要求 `Idempotency-Key` 头（复用既有 422/128 长度校验）；200=幂等命中既有重放、201=新重放 |
| `POST /api/external/sync/dead-letters/{run_id}/ignore` | catalog.write | body 必填 reason，缺省 422 |

**replay 语义（写死，零新传输逻辑）**，**四道闸门依次校验**：

1. **状态闸**：`status=open` 且未 exhausted，否则 409 DEAD_LETTER_NOT_OPEN / DEAD_LETTER_EXHAUSTED；
2. **分类闸**：`retry_class=auto`，manual 类 → 409 DEAD_LETTER_MANUAL_ONLY；
3. **退避闸（用户补丁 1）**：`now() >= next_replay_at`，未到 → 409 DEAD_LETTER_BACKOFF_NOT_REACHED；
4. **并发闸（用户补丁 2）**：replay 发起新 run 前**必须非阻塞抢 `_sync_gate_lock`**——
   与 /sync、/resume 完全同款模式（`main.py:1905/2465` 先例复用），
   抢不到 → 409 `SHOPIFY_SYNC_IN_PROGRESS`（不排队、不阻塞）。
   **不许因它是人工触发就跳过闸门**：死信重放本质是"人工触发的同步请求"，
   依然要打 Shopify API，必须受 S4 单飞约束。

四道闸全过 → 放行：对源 run 的 `retryable_resources` **发起新 run**
（`begin_sync_run(attempt=源+1, retry_of_run_id=源, retry_idempotency_key=幂等键)`，
同步本体走既有 /sync 链路与 R4 resume 续跑，**不新写任何传输/分页代码**）。
跨 workspace 访问 → 404（多租户隔离）。

### T5 补偿幂等（三层，写死）

1. **死信行幂等**：唯一约束 `(workspace_id, run_id)` + upsert 先查后插；
   二次 replay 409 DEAD_LETTER_NOT_OPEN。
2. **run 行幂等**：复用既有 `uq_external_sync_run_retry_idempotency`——
   同幂等键重放返回既有新 run（200），不建第二行。
3. **事实幂等**：R-31 快照幂等键（`external_inventory_snapshots.idempotency_key`）
   保证重放只多翻页不重复写数——生产实锤（§15.5 conflict=28/inserted=0），
   本片测试用 mock 双重放断言 inserted=0/no_op=N 复证。

### T6 僵尸 run（R-36 裁定落地，只读）

- **判定标准（写死）**：`run.status == "running"` 且 `finished_at IS NULL` 且
  `heartbeat_at < now − external_sync_run_stale_after_seconds`（默认 900s，复用既有配置）。
- **处置**：仅 `dead-letter list --zombie` 只读展示（run_id/heartbeat 年龄/error 线索）+
  人工可对僵尸 run 执行 `ignore` 入死信归档（status=ignored + reason=zombie-marked）。
- **不做**：不自动改 run 状态、不自动 kill、不生成新 run。
  理由：改 running 终态涉及取消令牌/进程内态归属（R-27/R-39），范围爆炸，
  留给未来切片（R-50 留痕）。**不强依赖 R-36 的 409 精度** → B3 关闭。

## 5. live 验证边界（§5.2 四条件照旧 + 本片专用方案）

四条件（事前用户书面裁定 / 基线不动 / 开门前复测 Gate 0 双项 / 有人值守+急停可用）
全文照 `docs/live-drill-runbook-20261028.md` v1.4 执行，本片不重复。

**本片 live 验证（最小化方案，预计 ≤4 次 API）**：

1. **负向（必须）**：一次正常 live orders 同步成功 → 断言死信表**无新行**
   （成功不进死信，防"补偿机制误吞正常流量"）；
2. **正向（构造失败）**：运行时前缀 `SHOPIFY_SYNC_BUDGET_SECONDS=1` 启动，
   页间预算检查点触发 → run failed（SHOPIFY_SYNC_BUDGET_EXCEEDED）→
   断言死信自动登记（**retry_class=manual**（用户裁定）、attempt=1、
   manual 类 next_replay_at=NULL）；
3. **不做**：真实重放不打 live（重放会再耗配额且属同步本体行为，已被 /sync 演练
   反复验证）；重放链路以 mock 全覆盖。

## 6. 用例（最小 15 条）🔒

| # | 用例 | 断言要点 |
|---|---|---|
| D-1 | migration 0022 up/down | up 加新表；down 逐列还原 0021 形态（逐列断言含索引与唯一约束） |
| D-2 | failed run 自动登记 | error_code/retry_class/attempt/quality_status 快照正确；重复 finalize 只有一行 |
| D-3 | succeeded 不登记 | 成功 run 死信表零行（含资源级全成功的 finalize） |
| D-4 | 分类表锁死 | auto 恰好 **3** 码（TIMEOUT/RATE_LIMITED/UPSTREAM_ERROR）；BUDGET_EXCEEDED=manual；CANCELLED=manual；未知码=manual（新增伪码断言）+ 死信行无凭据形态字符串 |
| D-5 | 退避计算 | attempt=1/2/3 的 next_replay_at ∈ [base×2^(n−1), base×2^(n−1)×1.25]（冻结时钟）；manual 类为 NULL |
| D-6 | exhausted | attempt=3 的死信 replay → 409 DEAD_LETTER_EXHAUSTED，exhausted=True |
| D-7 | 退避闸未到期 | next_replay_at 未到 replay → 409 DEAD_LETTER_BACKOFF_NOT_REACHED（冻结时钟前推）；到期后放行 |
| D-7b | 并发闸（用户补丁 2） | 先占 `_sync_gate_lock` 再调 replay → 409 SHOPIFY_SYNC_IN_PROGRESS；锁释放后可正常重放 |
| D-8 | replay 成功链 | 新 run attempt=源+1、retry_of_run_id=源、死信 status=replayed + replayed_run_id |
| D-9 | replay 幂等 | 同 Idempotency-Key 二次 → 200 返回同一新 run；不同键同参数 → DB 唯一约束兜底不产生第二 run 行 |
| D-10 | ignore | status=ignored + reason 落库；二次 ignore → 409 DEAD_LETTER_NOT_OPEN |
| D-11 | manual 拒绝重放 | CANCELLED 死信 replay → 409 DEAD_LETTER_MANUAL_ONLY |
| D-12 | 质量警告不进死信 | quality failed 但 run succeeded → 死信零行 + `--quality failed` 只读视图可见该 run（R-45 观察态不被破坏的锁） |
| D-13 | 僵尸判定 | 构造 running + 过期 heartbeat → `--zombie` 可见；run.status 仍 running（未被改动） |
| D-14 | 权限与隔离 | 跨 workspace 死信 404；无 catalog.write 调 replay/ignore → 403；无 Idempotency-Key → 422 |

## 7. 前置校验（开工前逐条执行并贴输出）

| # | 校验 | 命令 | 期望 | 结果 |
|---|---|---|---|---|
| P1 | 串行前提：P2-3 收口为 HEAD 祖先 | `git merge-base --is-ancestor ce88a0a HEAD && echo OK` | OK | 待贴 |
| P2 | 无并行写者 | `R-29` 流程（tasklist 查 claude 进程） | 无 | 待贴 |
| P3 | 基线 | `env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe -m pytest -q -p no:cacheprovider` | **420 passed / 0 failed** | 待贴 |
| P4 | F1–F8 逐条核实 | 见 §2 位置列 | 全部属实 | 待贴 |
| P5 | `dead-letter` 命名无冲突 | `grep -rn "dead.letter\|dead_letter" app/ tests/` | 零命中 | 待贴 |
| P6 | migration head | `alembic heads`（实跑，输出原文进交付说明） | 0021 唯一 head | 待贴 |

## 8. 数据/契约红线

- **契约**：既有响应/错误码逐字节不变；新增仅 3 端点 + 死信 schema。
  **`SYNC_RUN_NOT_RETRYABLE` / fixture retry 既有语义零改动**（`sync_application.py` 禁触）。
- **数据**：down 逐列还原；新表无回填；SQLite batch 不用。
- **R-27 定调**：本片无任何后台线程/定时器/跨进程机制；退避与到期判定全部为
  请求时惰性计算。实施方不得以"自动重试更完整"为由引入调度器。
- **凭据红线**：死信表无任何凭据材料（error_message 截断 500，不得含 token——
  D-4 之外补一条断言：登记内容 grep 不到 token 形态字符串）。

## 9. R-34 分块条款（照模板 v1.1 全文）

每块 <10 分钟、编辑即落盘、块交付说明写 `.testtmp/p2s4-chunk<N>.md`；
进程疑似死亡先 `git status` 再判死；长验证（全量回归）留给复核方自跑；
收口提交由复核方代笔。建议分块：① 模块+模型+migration → ② 挂接+CLI →
③ API+schema → ④ 测试前半（D-1..D-7b）→ ⑤ 测试后半（D-8..D-14）。

## 10. 完成标准与验收口径

- [ ] 基线 420 保持，全量 **420+N** / 0 failed（复核方自跑确认）；
- [ ] §6 十五条定向绿（贴条数与用例名）；
- [ ] 零超围证明：`grep -rn "Thread\|scheduler\|crontab\|BackgroundTasks" app/dead_letters.py` 零命中；
- [ ] `app/sync_application.py` 零改动（`git diff --stat` 证实）；
- [ ] live 验证 §5 两步留证（证据目录 `.testtmp/drill-evidence-p2s4/`）；
- [ ] `.testtmp/delivery.p2s4-compensation.md` 五节齐全（改动面/用例与结果/**偏差声明**/
      残留与已知限制/证据路径）+ 5 份分块说明落盘；
- [ ] R-48/49/50 登记进 §19 台账，编号连续。

## 11. 裁定记录（2026-10-29 用户评审，全部关闭，无遗留待裁项）

| # | 项 | 裁定 |
|---|---|---|
| Q1 | cancelled 入死信 | **入**，retry_class=manual，永不自动重试 |
| Q2 | 上限 3 | **接受**（`sync_compensation_max_attempts` 可调） |
| Q3 | live 最小化方案 | **接受**（1 正向 1 负向，≤4 次 API；不为测重试真实重放） |
| Q4 | 质量警告出口=只读视图 | **接受**（quality warn/failed 不进死信、不被 replay 自动重跑，观察态坚守） |
| Q5 | 两个新配置项 | **接受** |
| 补丁 1 | next_replay_at 强校验 | **已落**（§4 T3②/T4：字段 + 409 DEAD_LETTER_BACKOFF_NOT_REACHED，D-7） |
| 补丁 2 | replay 抢 S4 闸门 | **已落**（§4 T4 第 4 道闸：`_sync_gate_lock` 非阻塞，409 SHOPIFY_SYNC_IN_PROGRESS，D-7b） |
| 补丁 3 | BUDGET_EXCEEDED 归 manual | **已落**（§4 T3①：资源耗尽不自动重试；人工排查后调大预算走正常 /sync 重发） |
| 禁区 | 严禁复用 /retry fixture 路由 | **已落**（§1 🚫 禁区醒目段：死信 replay 走真实外网同步链路，`sync_application.py` 零改动） |

## 12. 新残留（预登记，编号接 R-47）

| 编号 | 内容 | 定级 |
|---|---|---|
| R-48 | 死信无跨进程调度：next_replay_at 仅是放行门槛，多 worker 下重放靠人工触发，无竞争防护（唯一约束保底不重复） | P3 |
| R-49 | 补偿上限 3 与退避 base 30s 是拍板值非 SLA，无压测依据 | P3 |
| R-50 | 僵尸 run 只读不处置（不改 running 终态），进程内态归属问题留给未来切片 | P3 |
