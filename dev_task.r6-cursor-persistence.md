# R6 任务文件：同步游标持久化（cursor_json + Alembic migration）

> 创建：2026-10-27（WorkBuddy，按用户指令接手原界面执行线）。
> 上级总纲：`dev_task.p1-residuals-s4-r6-r4.md` §2。细化规则以本文件为准，冲突处**本文件赢**。
> **R6 提交必须早于 R4**（R4 断点续跑依赖 R6 的 cursor；顺序强制）。

## 0. 模板固化条款（dev_task.template.md，本任务全部适用）

| 条款 | 本任务的落地 |
|---|---|
| R-15 | 验证命令统一加 `env -u PYTHONPATH -u PYTHONHOME` 前缀。**实测记录**：2026-10-27 基线在无前缀下即 351 passed / EXIT=0（本会话未注入垫片）；实施方环境若遇 `SystemExit: 1` 必须改用前缀重试并在交付说明记录 |
| R-17 | 完成即写 `.testtmp/delivery.r6.md`（落盘），不允许只在对话里报完成 |
| R-20 | §1 现状事实每条附实测方法；控制流断言读整函数或跑探针，禁止 `sed` 中段采样 |
| R-27 | 开工前逐条跑 §3b 前置校验（P1–P7），任一不满足即停手报告 |
| R-28 | 已知限制/残留必须带编号登记（本任务新增编号从 **R-31** 起），并同步 `docs/review-checklist-20261011.md` |
| R-29 | 并行 AI CLI 写者检测：**开工前与提交前各查一次** `ps -W \| grep -Ei "pytest\|uvicorn\|claude\|codex\|cursor\|code\b"`；有输出即停手报告，不提交、不 stash、不覆盖 |
| §3b P5 | 同 R-29；另查 `.git/*.lock` |
| §3b P7 | 提交前 `git diff --cached --numstat` 核对规模；**删除数非 0 必须解释**（换行符污染/混入他人改动的第一信号） |

### 3b. 前置校验记录（2026-10-27，接手方实测）

| 项 | 结果 |
|---|---|
| P5 进程检查 | `ps -W \| grep -Ei ...` → **无输出（EXIT=1），干净** |
| P5 git 锁 | `.git/*.lock` → 无 |
| 工作区 | `git status --short` → 干净（本地 HEAD = `ba6541e`） |
| 基线全量 | **351 passed / 0 failed / EXIT=0**（137.50s，命令 `"D:/python/python/python.exe" -m pytest tests/ -q -p no:randomly`） |
| 基线口径 | 用户 2026-10-27 指令：**基线 351 而非 350**——R-19 修复动了生产代码（`app/background.py` + 新增 `app/_worker_registry.py`），新增 6 例回归用例所致 |

## 1. 现状（Fact，全部附实测方法）

| # | 事实 | 位置 | 实测方法 |
|---|---|---|---|
| F1 | `ExternalSyncRun` **没有 cursor 字段**（逐字段核对：run_id/platform/account_ref/store_ref/sync_type/source_mode/simulated/status/时间戳/attempt/retry_*/resource_status_json/total/inserted/updated/no_op/conflict/heartbeat_at 均无 cursor） | `app/db.py:390-430` | `grep -n "class ExternalSyncRun" -A 45 app/db.py` 整段读 |
| F2 | `CrawlJob.cursor`（String(255)）是**爬取任务**的字段，与 sync run 无关，**不可复用** | `app/db.py:170` | grep 全文件确认归属 |
| F3 | 分页游标是 `_pages()` 内的**局部变量**（`cursor` / `seen` 集合），函数退出即丢；中断后只能从首页重来 | `app/platform_adapters/shopify.py` `_pages()` | 读整函数（R-20：不从行号采样） |
| F4 | 已有 `record_resource_metadata(db, run, resource, metadata)`：把 metadata 并入 `resource_status_json` 对应资源条目、不动 status/error_code——R6 的"每页落库一次"可挂同一点提交，**但 cursor 不塞进 resource_status_json**（见 §4 红线） | `app/external_sync.py` | grep + 读整函数 |
| F5 | `begin_sync_run` 内部 `db.commit()`+`refresh()`（:260-262）；新 run 的 `resource_status_json` 初始为 `{"schema_version": RESOURCE_SCHEMA_VERSION}` | `app/external_sync.py:227-263` | grep `def begin_sync_run -A 40` |
| F6 | migration 目录已有 Alembic 体系与若干历史版本（`alembic_version` 表在用）；heads 需开工时确认 | `migrations/versions/` | `ls migrations/versions/` + `alembic heads`（实施方跑，接手方未跑） |
| F7 | S4 已落地单飞闸门（`bd2123a`）：**live 同步同一进程内单飞**。R6 落库发生在同步线程内，与闸门无冲突，但 cursor 写入不得引入新的锁 | `app/main.py` 护栏区 | 读 `dev_task.s4-concurrency.md` + `git show bd2123a --stat` |

## 2. 目标

### 2.1 列与 schema（用户 2026-10-27 指令逐字落地）

- `ExternalSyncRun` 新增 **`cursor_json`**：`Text` / `nullable=False` / `default="{}"` / `server_default="{}"`。
- **schema 定死，键名一经确定不得漂移**：

```json
{
  "schema_version": 1,
  "resources": {
    "orders":    {"cursor": "<endCursor|null>", "pages": 3, "fetched": 150, "complete": false},
    "inventory": {"cursor": "<endCursor|null>", "pages": 1, "fetched": 28,  "complete": true}
  },
  "updated_at": "<UTC ISO8601>"
}
```

- `schema_version` 固定 `1`；`resources` 只含本次 run 实际同步的资源键（`orders`/`inventory`）；
  取完（`hasNextPage=false`）→ `cursor=null` 且 `complete=true`；`pages`/`fetched` 为累计值
  （跨续跑从源 run 起算、不归零——R4 消费）；`updated_at` 每次落库更新。
- **每页成功后落库一次**（与 `record_resource_metadata` 同点提交，避免额外事务）；
  run 终态后保留（可审计，供 R4）。
- 适配器 `_pages()` 支持 `start_cursor` 入参 + `on_page_done(cursor, pages, fetched)` 回调
  （默认 `None` = 既有行为零变化，沿用 P2-0a deadline/cancel_check 的兼容手法）。

### 2.2 resume_plan() 单一出口（用户指令原文落地，R4 的读取端也是它的验收面）

R6 交付 `resume_plan(cursor_json_str, target_resources) -> ResumePlan`（建议放
`app/external_sync.py`，供 R4 的 `/resume` 调用；R6 阶段必须有**单测直接覆盖它**）：

| 输入情形 | 判定 | 结果 |
|---|---|---|
| 字符串恰为 `"{}"`（**未开始**） | 允许续跑 | **全量重拉**（等同第 1 页开始），**非 409** |
| JSON 合法、`schema_version==1`、`resources` 无条目或缺目标资源键 | 允许续跑 | 该资源从第 1 页开始；全部资源如此则同全量重拉 |
| JSON 合法、`schema_version==1`、目标资源有条目且 `complete=false` | 允许续跑 | 从该资源 `cursor` 继续 |
| `schema_version` 存在且 **非 1** | 拒绝 | 409，reason=`UNSUPPORTED_CURSOR_SCHEMA`（不得猜测） |
| JSON 合法但**缺 `schema_version` 键** | 拒绝 | 409，`UNSUPPORTED_CURSOR_SCHEMA`（缺失≠默认 v1） |
| 列值为 **NULL** / 非 JSON 字符串（损坏） | 拒绝 | 409，reason=`NO_CURSOR`（历史脏数据专用） |

- **禁止用 `cursor_json == "{}"` 字符串比较做"是否可续"的最终判定**——它无法区分
  `"{}"` 与 `{"schema_version":1,"resources":{}}`（后者也应全量重拉）。字符串等值只能作为
  "未开始"的**快速通道**，最终判定必须是"每个目标资源都没有已取页记录"。
- `complete=true` 的资源在续跑时**跳过**（R4-3 语义，R6 的 plan 层就要体现）。

## 3. 验收口径（用户 2026-10-27 指令逐字）

| 层 | 判据 | 阻断 |
|---|---|---|
| 基线 | R6 开工前全量 **351 passed / 0 failed / EXIT=0**（已达成，见 §3b） | — |
| **主判据 ①** | **全量 0 failed**（R6 交付后的全量；R-19 已关闭，全量恢复为阻断项，`§14.2` 清单分支已失效）；数字口径 **351 + 本任务新增用例数 N**（§6 定向若 N 例则期望 `351+N passed`；若实施方实际用例数与 §6 有出入，以交付说明声明数为准并说明理由） | **是** |
| **主判据 ②** | **定向全绿**：`tests/test_r6_cursor_persistence.py`（新建）全部通过 | **是** |
| **主判据 ③** | **相关零回退**：`test_p2a_sync_guardrails.py`（10 例）+ `test_s4_sync_gate.py`（5 例）+ `test_r19_worker_leak.py`（7 例）全绿 | **是** |
| 规模 | P7：`git diff --cached --numstat` 与 §5 预期一致；**删除数非 0 必须解释** | 是 |

全量命令口径（R-15）：先无前缀试跑；遇 `SystemExit: 1` 改用
`env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 "D:/python/python/python.exe" -m pytest tests/ -q -p no:randomly`，
实际使用的形态记入交付说明。

## 4. migration 两条约束（用户指令逐字）+ 红线

1. **downgrade 只允许一条 `op.drop_column("external_sync_runs", "cursor_json")`**——只丢
   cursor 列，`external_sync_runs` 其余字段**零变化**（不重建表、不改既有列的类型/默认值/
   索引/约束、不动其他表）。up 填充默认 `"{}"`，既有行回填。
2. **默认禁止 `op.batch_alter_table` 整表重建；SQLite 兼容性例外（用户 2026-10-27 裁定）**：
   若实施方实测确认当前 Alembic/SQLite 组合下 `op.drop_column` 无法直接工作（或需要
   `render_as_batch` 才能生效），**允许使用 batch 路线**，但必须同时满足三条件：
   ① delivery 说明里写明"为什么必须 batch"（附实测报错或方言限制原文）；
   ② down 后仍按 R6-4 做**逐列断言**（inspector/PRAGMA），证明除 cursor_json 外全表零变化
   （batch 重建最容易悄悄改掉的：列顺序、默认值表达、约束名——断言必须覆盖）；
   ③ 已知限制登记 **R-33**（见 §7）。三者缺一，复核退回。
   其余方言（若必须重建表才能 drop column 且无 batch 机制）仍按原规则**停下报告**。
3. **不改** `resource_status_json` 既有结构（cursor 独立成列，不塞进 JSON 台账）。
4. **不动** `CrawlJob.cursor`（F2，另一条链路）。
5. **不实现**自动续跑（那是 R4）；R6 只保证"游标被持久化、可被读到、`resume_plan` 可判"。
6. **不动** `/retry` 的 fixture 语义（有既有测试锁定）。
7. **不新增**第三方依赖；不用数据库行锁/唯一约束实现持久化。

## 5. 改动范围预期（P7 核对基准）

| 文件 | 预期 |
|---|---|
| `app/db.py` | +1 列定义（`ExternalSyncRun.cursor_json`，Text/nullable=False/default `"{}"`/server_default `"{}"`） |
| `app/platform_adapters/shopify.py` | `ShopifyReadOnlyAdapter.__init__` +`start_cursor`/`on_page_done`（默认 None）；`_pages()` 消费二者并透出末游标；既有分支零行为变化 |
| `app/external_sync.py` | +`record_sync_cursor(db, run, resource, cursor, pages, fetched, complete)`、+`resume_plan(cursor_json_str, target_resources)`、+`CURSOR_SCHEMA_VERSION = 1` 常量 |
| `app/main.py` | 仅 `_shopify_sync_impl` 内挂接每页回调与 `record_sync_cursor` 调用（约 ±10 行）；S4 闸门代码（`_sync_gate_lock` 区）不动 |
| `migrations/versions/`（新文件，序号接当前 head，如 `00xx_external_sync_run_cursor.py`） | up 加列 + down 单条 drop_column |
| `tests/test_r6_cursor_persistence.py` | 新增（§6 用例 R6-1..R6-9） |
| 其余 | **零**；若实现中发现必须动别的文件，**先停下报告** |

## 6. 用例（最小集，新建单文件）

| # | 场景 | 期望 |
|---|---|---|
| R6-1 | 正常 3 页同步 | 每页后 `resources[resource].pages` 递增，终值 3，`cursor`=末页 endCursor，`updated_at` 更新 |
| R6-2 | 中途 fail（第 2 页抛错） | `cursor_json` 保留第 1 页末端游标（不因失败清空） |
| R6-3 | 中途取消（S4/P2-0a 语义） | 同上，游标保留 |
| R6-4 | migration up/down | up+down 均成功；既有行 `cursor_json=="{}"`；**down 后逐列核对表结构与 down 前一致（仅少 cursor_json）**——用 inspector 或 PRAGMA 逐列断言，不靠肉眼 |
| R6-5 | 双资源（orders+inventory） | 两个 key 互不覆盖 |
| R6-6 | schema 形状 | JSON 可解析、`schema_version==1`、顶层键恰为 `schema_version/resources/updated_at`；取完资源 `cursor is None and complete is True` |
| R6-7 | `resume_plan` 全分支 | §2.2 表格逐行各一断言：`{}`→全量重拉非 409；空 resources→全量重拉；schema_version=99→UNSUPPORTED_CURSOR_SCHEMA；缺 schema_version 键→UNSUPPORTED_CURSOR_SCHEMA；NULL/非 JSON→NO_CURSOR；complete 资源→跳过标记 |
| R6-8 | 新建 run 初始值 | `begin_sync_run` 后 DB 读出 `cursor_json == "{}"`（不是 NULL、不是空壳 JSON）——保证 A 情形可判 |
| R6-9 | 回退验证（A3 教训，§14.8） | 交付前做一次**回退实验**并记录：删掉"每页落库"挂接 → R6-1/R6-2 变红。做不到的固化不算固化 |

## 7. 已知限制（R-28：带编号登记）

| 编号 | 内容 |
|---|---|
| R-31 | `seen` 防环集合不持久化：跨续跑的环检测不完整（只恢复末游标）。R6 交付说明必须声明该取舍及理由。**兜底**：数据层既有 `payload_hash`/`idempotency_key` 去重不受续跑影响，环导致的重复拉取会被入库层去重吸收——即环检测退化的最坏后果是"多翻几页"，不是"重复写数"；此句即去重兜底说明，R4 交付时复核仍成立 |
| R-32（若适用） | `_pages()` 每页落库 = 每页一次额外 commit。**触发阈值**：定向套件总耗时较改前增幅 >10%，或任一单用例耗时增幅 >2s，即升级 R-32 为正式登记并附前后对比数据；未触阈值则在交付说明记"已测、未触发、数字如下"后关闭 |
| R-33（若走 batch） | migration 使用了 `op.batch_alter_table`（SQLite 兼容性例外）：跨方言移植时 batch 重建表的行为差异是隐患；交付说明必须含"为何必须 batch"的证据 + R6-4 逐列断言覆盖列顺序/默认值/约束名 |
| — | 多 worker 下"单飞闸门 + cursor 持久化"仍是进程内语义（R-26/R-27 已登记的同源限制，不重复编号） |

## 8. 交付说明（R-17）

写 `.testtmp/delivery.r6.md`，必含：

1. migration 的 down_revision 与 `alembic heads` 实测输出；
2. §2.2 `resume_plan` 判定表与实现的逐行对应；
3. R-31 取舍说明；
4. 全量命令实际形态（是否用了 `env -u` 前缀）与最终数字；
5. 回退实验记录（R6-9）：回退谁 → 哪个用例红；
6. 提交前 `git diff --cached --numstat` 粘贴 + 逐文件解释；
7. R-29 提交前检查的输出粘贴。

## 9. 完成标准

- §3 三层主判据全过；§6 用例定向全绿；
- 提交信息含结论摘要（delivery.r6.md 的要点摘录）；
- **R6 提交早于 R4**；R4 开工前 R6 必须已合入；
- 实施方不得在本文件范围外改文件；发现必须改 → 停手报告。
