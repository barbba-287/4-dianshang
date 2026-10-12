# 任务

> 生成时间：2026-10-12
> 生成方式：只读检查（`CLAUDE.md`、`docs/engineering-roadmap.md`、`docs/claude-handoff-contract.md`、`app/cli.py`、`app/background.py`、`app/crawler.py`、`tests/test_cli.py`、`git status --short`）+ 仓库外独立探针实测
> 执行角色：本文件只做需求拆解、根因定位与验收定义，代码实现由 Claude Code 负责
> 阶段归属：P1 模块边界 —— **收官切片**（清掉唯一 failed，使全量回归可宣称"全绿"）
> 对接复核：`docs/review-checklist-20261011.md` §4.6（该缺陷的既有定性）

## 任务标题

**修复 CLI 基线缺陷 `test_cli.py::test_cli_seed_docs_and_query`：`enqueue-crawl` 的等待窗口（10s）短于 Playwright 采集实际耗时（~17s），导致 CLI 在任务成功的情况下误判为失败并返回退出码 1。**

## 任务目标

1. 让 `tests/test_cli.py::test_cli_seed_docs_and_query` 通过（消除全量回归中唯一的 `failed`）。
2. **不改变 CLI 的对外语义**：`enqueue-crawl` 的退出码含义保持"任务是否成功"，不因本次修改把失败也变成成功。
3. 使该等待逻辑对未来更慢的任务具备合理容错，避免同类"任务成功却被判失败"再次发生。

## 选择理由

| 依据 | 说明 |
|---|---|
| P1 完成门禁要求全量回归 | `docs/engineering-roadmap.md:12` P1 完成门禁含"全量回归"；当前 `282 passed + 1 failed`，该 failed 是唯一挡项 |
| 该缺陷已被独立定性为基线既有 | `docs/review-checklist-20261011.md` §4.6：与内容导出切片无因果，归独立任务修复 |
| 根因已由规划方定位到行 | 见下文 F3–F5，无需实现方从头排查，交付成本低 |
| 不修则阻塞对外表述 | §5.7 边界声明：该用例未修前，不得宣称全量"全绿"；R-2（交付说明 `283 passed` ≠ 实测）也挂在它身上 |
| 与"测试耗时"是两个问题 | 见 F5 说明：**本次不优化 Playwright**，只修等待窗口（理由见"明确不做的事情"） |

---

## 当前事实

### 已实现内容

| 项 | 事实 | 证据 |
|---|---|---|
| 分支与 HEAD | 分支 `feat/warehouse-mvp`，HEAD `4fcb5dd 文档：更新回归基线数字与 WAL 提速说明`，5 commits 未推送 origin | `git status`、`git log --oneline -8` |
| 失败用例位置 | `tests/test_cli.py:59-87`，失败断言在 `:71 assert crawl.returncode == 0` | `tests/test_cli.py` |
| 失败表现 | `enqueue-crawl` 打印 `已入队: job_id=1, status=queued` → `任务完成: status=running, attempt=1`，退出码 1 | 实测 pytest 原始输出（见下） |
| 等待调用点 | `app/cli.py:258 wait_for_jobs(timeout=10)` —— **硬编码 10 秒** | `app/cli.py:250-268` |
| CLI 成功判定 | `app/cli.py:265 return 0 if final.status == "succeeded" else 1` | `app/cli.py:264-265` |
| 等待实现 | `app/background.py:652-681 wait_for_jobs()`：以 `_cancel_futures` 注册表是否为空 + future 是否完成轮询，至 `deadline` 为止 | `app/background.py:652-681` |
| 任务执行链 | `_execute_crawl_fixture()` → `collect_fixture()` → **`sync_playwright()` 会话** | `app/background.py:399-415`、`app/crawler.py:76-169` |
| 任务本身逻辑很薄 | `_execute_crawl_fixture` 仅 3 步：`collect_fixture` → `upsert_product_batch`（3 条记录）→ `db.commit()`；`upsert_product_batch` 是简单 for 循环，无 N+1 提交 | `app/background.py:399-415`、`app/repository.py:110-125` |
| 全量回归现状 | `282 passed + 1 failed`（`PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider`，2026-10-11 复核实测） | `docs/review-checklist-20261011.md` §5.2 |

### 原始失败输出（规划方实测，2026-10-12）

```text
$ cd D:/ai/dianshang && PYTHONDONTWRITEBYTECODE=1 "D:/python/python/python.exe" -m pytest "tests/test_cli.py::test_cli_seed_docs_and_query" -q -p no:cacheprovider

>       crawl = _run_cli(["enqueue-crawl"], tmp_path, None)
>       assert crawl.returncode == 0, crawl.stdout + crawl.stderr
E       AssertionError: 数据库: sqlite:///C:\Users\董\AppData\Local\Temp\pytest-of-董\...\cli.db
E         检测: migrated | 已有表 44 张
E         已迁移库：执行 alembic upgrade head（幂等）...
E         当前 alembic 版本: 0018_repair_service_ticket_schema
E         已入队: job_id=1, status=queued
E         任务完成: status=running, attempt=1
E       assert 1 == 0

tests\test_cli.py:71: AssertionError
1 failed in 32.16s
```

### 根因定位（规划方仓库外探针实测，2026-10-12）

**探针**：`<session-tmp>/cli_probe_wait.py`（未写入仓库），真实 SQLite + 真实 `submit_crawl_fixture` + 真实 `collect_fixture`，仅把等待窗口放大到 60 秒。

```text
[1] _ensure_schema ...                     建库耗时 13.51s
[2] 提交任务 ...                             job_id=1 status=queued 提交耗时 0.07s
[3] wait_for_jobs(timeout=60) ...           wait 返回耗时 17.08s
[4] 最终状态: status=succeeded attempt=1 error=no   总耗时 17.15s
```

**结论**：

1. **任务最终成功**（`succeeded`），不是功能性缺陷。
2. **任务实际耗时 17.08s > 等待窗口 10s**。CLI 在 10 秒窗口耗尽后读取 DB，读到中间态 `running`，据此返回 1。
3. 17 秒几乎全部来自 **`sync_playwright()` 会话开销**，而非数据库或业务逻辑：
   - `collect_fixture` 用 `sync_playwright()` 启动 chromium → 加载本地 html → 抽 3 张卡片 → `tracing.stop()` → `context.close()` → `browser.close()`（`app/crawler.py:88-169`）；
   - 上一轮会话已独立实测：`chromium.launch` 仅 **0.45s**，而整个 `with sync_playwright()` 块耗时 **16.60s**（第二次 12.32s）——**开销在会话建立/拆卸，不在页面操作**；
   - 该数值与本次探针的 17.08s 相互印证。

### 相关文件（行号为本次会话实测）

| 文件 | 角色 | 关键位置 |
|---|---|---|
| `app/cli.py` | **本次主改造目标** | `:250-268` `cmd_enqueue_crawl`；`:258` `wait_for_jobs(timeout=10)`；`:265` 成功判定 |
| `app/background.py` | 等待机制实现，**本次不改** | `:652-681` `wait_for_jobs`；`:61-81` `_submit_future`；`:399-415` `_execute_crawl_fixture` |
| `app/crawler.py` | Playwright 采集，**本次不改** | `:76-169` `collect_fixture`（`:88` `sync_playwright()`） |
| `app/repository.py` | 批量写入，**本次不改** | `:110-125` `upsert_product_batch` |
| `tests/test_cli.py` | 失败用例所在，**仅在必要时改** | `:59-87` `test_cli_seed_docs_and_query`；`:17-40` `_run_cli` 辅助函数 |
| `docs/review-checklist-20261011.md` | 既有定性依据 | §4.6、§5.5 R-2、§5.7 |

### Fact / Target / Assumption / Unknown

**Fact（本次会话已验证）**

| 编号 | 事实 |
|---|---|
| F1 | 失败可稳定复现，且在 pytest 环境与仓库外裸 CLI 环境**均可复现**（19.46s / `running`） |
| F2 | 任务最终状态为 `succeeded`，`attempt=1`，`error_message` 为空 |
| F3 | 等待窗口为硬编码 `timeout=10`（`app/cli.py:258`） |
| F4 | `wait_for_jobs` 的语义是"等到所有已提交 future 完成**或** deadline 到期"，因此**只要窗口够大它就能等到终态**——无需改其内部逻辑 |
| F5 | 任务耗时主因是 `sync_playwright()` 会话开销（实测整块 ~16s，`launch` 仅 0.45s），非 SQLite、非 WAL、非 shim |
| F6 | 该失败与内容导出切片无因果（§4.6 已在基线工作树上原位复现） |
| F7 | `_execute_crawl_fixture` 业务逻辑仅 3 步、无 N+1 提交，因此"任务慢"不能通过优化业务代码解决 |

**Target（本次要达成，非既成事实）**

| 编号 | 目标 |
|---|---|
| T1 | `tests/test_cli.py::test_cli_seed_docs_and_query` 通过 |
| T2 | CLI 退出码语义不变（成功 0 / 失败非 0），不得通过"放宽成功判定"取巧 |
| T3 | 等待窗口具备对慢任务的合理容错，且不使测试无限挂起 |
| T4 | 定向测试 + 全量回归无 failed |

**Assumption（本任务成立的前提）**

| 编号 | 假设 | 若不成立的影响 |
|---|---|---|
| AS1 | Playwright 会话耗时 12–17s 在当前机器上稳定，非偶发抖动 | 若为抖动，提高窗口仍是正确修法，但需更大余量 |
| AS2 | `test_cli.py` 中其他用例（`init-db`/`status`/`query`）不触发 Playwright | 若触发则同样受影响，需一并评估 |

**Unknown（本次未验证）**

| 编号 | 未知项 | 原因 |
|---|---|---|
| U1 | Playwright 会话为何在 Windows 上 teardown 需 ~16s | 涉及 node driver 进程退出路径，未深挖；**不影响本任务** |
| U2 | 生产环境是否存在真实用户受 10s 窗口影响 | `enqueue-crawl` 为 CLI 开发/演示命令，非 HTTP 路径；未查生产调用方 |
| U3 | 25 个 Playwright 相关测试的合计耗时中，有多少可被"会话复用"消除 | 属独立任务（性能优化）范围，见"明确不做的事情" |

---

## 本次范围

### 允许修改的文件

| 文件 | 允许的改动 |
|---|---|
| `app/cli.py` | `cmd_enqueue_crawl` 的等待策略（窗口时长 / 等待目标） |
| `tests/test_cli.py` | **仅在必要且不降低断言强度时**补充或调整；不得删除既有断言 |

### 不允许修改的文件

| 文件 | 原因 |
|---|---|
| `app/crawler.py` | Playwright 采集逻辑属生产路径，性能优化须另立任务 |
| `app/background.py` | 任务调度/执行机制，`wait_for_jobs` 语义被多处测试依赖 |
| `app/repository.py`、`app/db.py` | 数据层，与本次根因无关 |
| `app/employee_auth.py`、`app/security.py` | 权限与安全，与本次无关 |
| `migrations/`、`alembic.ini` | 无 schema 变更 |
| `CLAUDE.md`、`docs/claude-handoff-contract.md` | 规范文件，不由实现方改动 |
| `docs/review-checklist-20261011.md` | 复核方产出物 |

### 明确不做的事情

1. **不优化 Playwright 性能**。理由：`app/crawler.py` 是生产采集路径，改动影响面大（浏览器生命周期、trace、异常截图），与"清掉一个 failed"的成本收益严重不匹配。**Playwright 会话复用列为独立任务**（可同时收益全量耗时 ~110s，见 U3）。
2. **不修改 `wait_for_jobs` 的内部判定逻辑**（`app/background.py:652-681`），只允许改调用方传入的窗口或等待目标。
3. **不放宽 CLI 成功判定**（不得把"超时"也当作成功返回 0）。
4. **不调整其它 CLI 命令**（`init-db`/`seed-docs`/`query` 等）。

---

## 实施要求

### 数据流和状态边界

```text
python -m app.cli enqueue-crawl
  → _ensure_schema()                                   (alembic upgrade head，幂等，~5-13s)
  → submit_crawl_fixture()  → enqueue_job + _submit_future   (返回 queued)
  → 【等待】：wait_for_jobs(timeout=N)  ← 本次要改的窗口
  → db.expire_all(); db.get(CrawlJob, job.id)
  → 读 final.status → return 0 (succeeded) / 1 (其它)
```

- 状态机：`queued → running → succeeded | cancelled | retry_wait → failed`（由 `app/background.py` 维护，本次不改）；
- 本次只影响"读状态前等多久"，不改变任何状态迁移。

### Workspace / RBAC 边界

N/A。`enqueue-crawl` 是本地 CLI 命令，通过 `_resolve_cli_workspace` 解析 workspace，不经过 HTTP 鉴权。**不得引入任何 RBAC 相关改动。**

### 幂等、版本、事务和审计要求

- 无 schema 变更、无迁移；
- `ProductContentExport` 式唯一约束与本次无关；
- 不得新增或改动 `Idempotency-Key` 相关语义。

### 失败、重试、取消和人工接管要求

- **超时路径必须可诊断**：窗口耗尽而任务仍在 `running` 时，输出应能让使用者看出"是超时，不是任务失败"（当前输出 `status=running, attempt=1` 已具备部分信息，可考虑补充 `(wait timeout)` 之类提示）；
- 不得把超时静默当作成功；
- 不做自动重试（CLI 命令由人执行）。

---

## 验证命令

```bash
# 定向（本次唯一失败用例）
cd D:/ai/dianshang && PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_cli.py -q -p no:cacheprovider

# 相邻回归（确认未波及后台任务测试）
cd D:/ai/dianshang && PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_jobs.py tests/test_crawler.py -q -p no:cacheprovider

# 全量
cd D:/ai/dianshang && PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

> 基线格式（契约 §4.4）：改动前 `282 passed / 1 failed`（`PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider`，复核方实测 2026-10-11）→ 改动后 `<N'> passed / <M'> failed`。

---

## 完成标准

### 1. 正向测试

- `tests/test_cli.py::test_cli_seed_docs_and_query` 通过；
- `enqueue-crawl` 在任务实际成功后返回退出码 0，且输出 `status=succeeded`。

### 2. 负向测试

- **必须构造一次真实失败**并证明退出码仍为非 0：例如将 fixture 路径指向不存在文件（或使用等价手段），使 `collect_fixture` 抛错，确认 CLI 输出失败状态且返回非 0。
- 若无法在测试中干净构造，至少须提供**手工执行的原始命令与输出**作为证据。

### 3. 越权测试

N/A（CLI 无租户边界）。**在交付说明中显式声明不适用及原因**，不得留空。

### 4. 重复 / 幂等测试

- 连续执行两次 `enqueue-crawl`，确认 `products` 数量不因重复执行而翻倍（`status` 输出中 `products = 3` 保持为 3）。

### 5. 故障 / 回放测试

- 覆盖"窗口内未完成"这一路径：按 F4，窗口够大即可等到终态；需说明所选窗口值（N）的依据（实测 17.08s → 建议留 ≥2x 余量）。

### 6. 真实、模拟和未知范围的区分

| 分类 | 内容 |
|---|---|
| 真实 | 真实 Playwright/chromium 浏览器、真实 SQLite、真实后台线程与 lease 机制 |
| 模拟 | `fixtures/products.html` 为本地静态 fixture；商品数据为模拟数据 |
| 未知 | U1（Playwright teardown 慢的机理）、U2（生产调用方）、U3（会话复用收益） |

### 7. 交付与门禁（`CLAUDE.md:26-42`）

- 附**原始测试输出**（完整命令 + stdout）；
- 给出用例编号（T1–T4）→ 测试函数名映射；
- 声明改动文件清单（含 sha256 前 16 位）与"未改动"文件；
- 任一硬门禁未过，状态只能写"进行中 / 待验收"，不得写"已完成"。

---

## 禁止操作

- `git reset` / `git clean` / `git checkout --` 等破坏性操作；
- 删除或覆盖任何未跟踪文件；
- `git commit` / `git push`（由用户决定提交与分支推进）；
- 任意外部平台写入；
- 修改本任务文件、`CLAUDE.md`、`docs/claude-handoff-contract.md`、`docs/review-checklist-*.md`；
- 修改 `app/crawler.py`、`app/background.py`（见"不允许修改的文件"）。

---

## 附：本任务与既有文档的对应关系

| 文档 | 关系 |
|---|---|
| `docs/review-checklist-20261011.md` §4.6 | 该缺陷的既有定性来源（"基线既有、与切片无因果"） |
| `docs/review-checklist-20261011.md` §5.5 R-2 | 交付说明 `283 passed` 的修正，依赖本任务完成后才能定稿 |
| `docs/review-checklist-20261011.md` §5.7 | 明确"该用例未修前不得宣称全量全绿"——本任务即解除该边界 |
| `docs/engineering-roadmap.md:12` | P1 完成门禁含"全量回归"，本任务为 P1 收官的前置 |
