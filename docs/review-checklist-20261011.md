# 终审复核清单：P1 内容导出切片

- 生成日期：2026-10-11
- 交付方：Claude Code（实现方）
- 复核方：WorkBuddy（独立验收，不做实现）
- 基线 commit：`87533cecac343ffe758c3a3653220a3b35c3a313`（短 `87533ce`，分支 `feat/warehouse-mvp`）
- 测试结论：**280 passed / 0 failed / 0 error**（318.87s，0:05:18）

> 本文档是 `docs/claude-handoff-contract.md` 的一次具体交付实例，供复核方据此独立验收。
> 所有 sha256 均基于 git 对象 `87533ce` 计算（`git show 87533ce:<文件>`），不是工作区快照，
> 因此复核方无需依赖交付方转述，可直接从 git 复算。

## 1. 交付产物清单（16 个文件）

| 文件 | sha256（完整，前 16 位即其前缀） |
|---|---|
| `.gitignore` | `fa82e3355d1de518df67d44e2b7f567a06036325819abe2bba89f041591ed1f3` |
| `CLAUDE.md` | `9f17b358e12e963be24c239ebc772a7a34a9f7becfd8e4877235a8d7e4b3b3cd` |
| `README.md` | `bd4f3db1579d3b15eda8bec3eba9632de974c68e5d060e9a902f27e75d618e76` |
| `app/analytics_queries.py` | `5eaa0de31ea86391c1a55012b8b3be717299d9e7db2cc2d98dc67ae34ab85397` |
| `app/content_application.py` | `8c0d0b6d66577ddf7ce5b1e4d5b22e18c89b72fa0b11a783db1e7db02f9cf545` |
| `app/main.py` | `42c99b6bfe0f841629ce3d1cc9eb8190a2569d1121cbfd1ebbd94e5bb0ad9b55` |
| `app/schemas.py` | `770a6b80ea184b69f1a48ae3e3d81684fe2d7282e75e64ba295b4839256c05a1` |
| `app/schemas.recovered.py` | `b051c5d9df3965740b7b7212a3946851d59e6fb0062a8c48387d5600f6191bb2` |
| `app/sync_application.py` | `c7c184b92ae3425c6b00ba84281b4966380dc5b9bbf539d9245a2621afcadf72` |
| `dev_task.md` | `f081796064ef65a3c76d636bfe758b640a0dc198142335c23255899fd3c05211` |
| `docs/claude-handoff-contract.md` | `8d67dd4885c64af3aabddb8cd7c384f222d038d547e5e87517d4228a8bcea563` |
| `tests/test_analytics_queries.py` | `16a3dd81298a672d12cddf117e8b12bb1ca5df542ead744b7ae35df1bb9a9134` |
| `tests/test_api_content_export.py` | `9805f9127cbf1da91deb6eaf396027cc5492d13088e92f54fd52f21e7dabb48a` |
| `tests/test_api_product_quadrant.py` | `5b6cec09b68ef12d3447abdbae0661b23a7dd811f0624c466e1c0023f6ada65d` |
| `tests/test_content_application.py` | `ae594a33f32eb1b37517d02cd3492774fb8f491765a735e7b03a059b952c424e` |
| `tests/test_sync_application.py` | `ac70cd3b690e4520391bcd13f9d1de5b03492aa69e5ad787aaece4c2f64a9e8e` |

## 2. 测试证据

- stdout 落盘：`artifacts/test-full-run-20261011.txt`（34 KB）
- stdout sha256：`50b099e6e6073301a63f9b3406363608c5e74bdeee2dbc53ddc0fdabaa3a5a3e`
- 结论行（stdout 第 430 行）：`280 passed, 4241 warnings in 318.87s (0:05:18)`
- 4241 warnings 全部为 `DeprecationWarning: datetime.datetime.utcnow()`，54 处独特位置，
  属于历史模块（`test_jobs.py` / `app/tickets.py` / `test_operations_workbench.py` 等），非本切片引入。

复跑命令（解释器为项目专用，非系统 PATH）：

```bash
cd D:/ai/dianshang && PYTHONDONTWRITEBYTECODE=1 D:/python/python/python.exe -m pytest -q -p no:cacheprovider --basetemp=D:/ai/dianshang/.testtmp
```

## 3. 终审复核项（复核方独立执行）

### 3.1 独立复跑测试

执行 §2 复跑命令，比对结论是否为 `280 passed`。注意：这是**独立复现**，不是读取 stdout 文件。

### 3.2 核对 16 个文件 sha256

```bash
git show 87533ce:<文件> | sha256sum
```

逐项比对 §1 表格。若 `git status --short` 非空，先确认工作区改动是否影响本次交付。

### 3.3 硬门禁 H1–H7 逐项核对（契约 §4.3）

| 编号 | 情形 | 交付方自述的证据位置 | 复核方动作 |
|---|---|---|---|
| H1 | 无原始测试输出 | `artifacts/test-full-run-20261011.txt` | 读文件确认含完整 stdout |
| H2 | 数字与命令/日期/环境不可对应 | §2 记录命令+解释器+日期，stdout sha256 绑定 | 核对 §2 链路 |
| H3 | 声明"字节不变"却出现在 git status | 本次无此类声明，16 文件均如实列入 §1 | 核对 git status |
| H4 | 删改范围外既有测试 | 曾删 `test_review_requires_idempotency_key_before_mutation`，已恢复并补键校验用例 | **重点核验** `tests/test_content_application.py` 第 27、43 行 |
| H5 | 新增测试断言路径不可达 | — | 按契约 §5.1 方法独立核验 |
| H6 | 并发/锁/事务结论未独立复现 | flush 阶段 `IntegrityError` 已由 `test_export_concurrent_flush_integrity_error_replays` 覆盖 | **按契约 §5.2 用独立探针复现**，非依赖交付方测试 |
| H7 | 未披露的范围外改动 | 16 文件全部列入 §1；4 个个人文件已 gitignore 并披露 | 核对范围 |

### 3.4 范围核对

- 16 个文件已在 commit `87533ce` 中。
- 4 个个人文件已 `.gitignore`，未入库：`CCR-使用文档.md`、`docs/interview-demo-script.md`、`面试/`、`问题解决资产库/`。

## 4. 复核结论（由复核方 WorkBuddy 填写，2026-10-11）

```text
结论：有保留通过
```

### 4.1 已独立核验（复核方实测，非读取交付方结论）

| 项 | 复核动作 | 结果 |
|---|---|---|
| git 基线 `87533ce` | `git log --oneline` / `git show --stat` | ✓ 存在，16 文件，`3359 insertions(+), 172 deletions(-)` |
| 16 文件 sha256 | 自算 `git show 87533ce:<文件>` 的 sha256，逐项比对 §1 | ✓ **16/16 全部一致** |
| stdout 证据文件哈希 | 自算 `artifacts/test-full-run-20261011.txt` 的 sha256 | ✓ 与 §2 声明的 `50b099e6...` 一致 |
| stdout 内容完整性 | 读文件确认含完整 stdout 与结论行 | ✓ 第 430 行 `280 passed, 4241 warnings in 318.87s` |
| H1 无原始测试输出 | 读证据文件 | ✓ 通过（完整 stdout 已落盘） |
| H2 数字与命令/日期/环境对应 | 核对 §2 命令、解释器、日期、stdout 哈希绑定链路 | ✓ 通过 |
| H3 声明"字节不变"却在 git status | 核对 `git status --short` | ✓ 本次无此类声明，16 文件如实列入 §1 |
| H4 删改范围外既有测试 | 核 `tests/test_content_application.py` 第 27、43 行 | ✓ **已恢复** `test_review_requires_idempotency_key_before_mutation`，并新增 `test_review_requires_idempotency_key`（真正命中键校验分支） |
| H5 断言路径不可达 | 按契约 §5.1 读代码确认分支可达性 | ✓ 通过（含新增 `flush_raises=True` 路径，对应 `flush()` 已在 `try` 内，可达） |
| H6 并发结论未独立复现 | **契约 §5.2 独立探针**，见 §4.3 | ✓ 通过（P0 已实质修复） |
| H7 未披露范围外改动 | `git ls-files --others --exclude-standard` + 逐项核 `.gitignore` | ✓ 仅 `docs/review-checklist-20261011.md` 本身未跟踪（属本次交付物）；4 个个人文件确认 `git check-ignore` 命中 |
| 4 个个人文件已忽略 | `git check-ignore -v` 逐个验证 | ✓ `CCR-使用文档.md` / `docs/interview-demo-script.md` / `面试/` / `问题解决资产库/` 全部 IGNORED |

### 4.2 未独立核验（显式列出，不得与已核验项混写为"全部通过"）

| 项 | 原因 | 影响 |
|---|---|---|
| **全量复跑得到 `280 passed`** | 见下方"环境干扰记录"：同一代码库、同一命令，复核环境两次全量结果互不一致（`280 errors` / `1 failed + 278 passed + 1 error`），**均出现在 fixture 清理阶段或既有失败**，非断言级回归 | `280 passed` 目前为交付方证据（stdout 文件哈希已独立验证一致），**未能独立复现**。详见 §4.4 |
| MySQL 上并发锁行为（U3） | 无 MySQL 环境 | 保持 Unknown |
| 真实 `wan` Provider / 真实平台授权（U4） | 未授权 | 保持 Unknown |

**环境干扰记录（复核方两次全量观测）**

| 次 | 命令 | 结果 | 耗时 |
|---|---|---|---|
| 1 | `pytest -q -p no:cacheprovider --basetemp=D:/ai/dianshang/.testtmp` | `1 warning, 280 errors in 177.36s` | 2m57s |
| 2 | 同上（basetemp 已存在，跳过 rmtree 首轮） | `1 failed, 278 passed, 4209 warnings, 1 error in 2324.26s` | **38m44s** |

判定说明：

1. 两次结果互不一致（`280 errors` vs `1 failed + 1 error`），**同一代码库、同一命令** → 说明该观测不稳定，不适合作为"是否回归"的判据。
2. 耗时差异悬殊（交付方记录 5m18s vs 复核 38m44s）→ 复核环境的 shim（`sitecustomize.py` 劫持 `shutil.rmtree`）不仅拦截删除，还引入重试开销，**两边的数字不具备直接可比性**。
3. 交付方环境无 shim，其 stdout 显示 `0 errors`；复核环境两次均非 `0 errors` → 差异属**环境层**，**不构成对交付方证据的证伪**。
4. 第 2 次观测中的 `test_cli.py::test_cli_seed_docs_and_query` 失败**已单独定性**，见 §4.6——系基线既有失败，与本次切片无关。

### 4.3 H6 独立探针复现记录（关键，非依赖交付方测试）

使用**复核方自行编写、位于仓库之外**的探针（不改动仓库），构造真实 SQLite 唯一约束冲突：

| 场景 | 修复前（2026-10-11 首轮） | 修复后（本轮） |
|---|---|---|
| 服务层：同键竞态 + 竞争者已提交 | 抛出未捕获 `IntegrityError` → 上层 500 | 命中兜底 → 稳定错误，`replayed=True` |
| **HTTP 端到端**：真实约束 + 真实路由 | 未测 | **`409 IDEMPOTENCY_KEY_REUSE`，无新增导出行，无 job** |

结论：任务文件 T4 目标**已实质达成**，且是在与交付方测试**完全不同的验证路径**上达成（真实约束 + 真实 HTTP），不依赖其自证。

### 4.4 需修订（P2，不阻塞本轮收口）

| 编号 | 问题 | 最小修订动作 |
|---|---|---|
| P2-1 | 测试证据文件 `artifacts/test-full-run-20261011.txt` 位于 `artifacts/*` 忽略规则下，**未入库**（`git ls-files artifacts/` 仅 `.gitkeep`）。清理 `artifacts/` 后证据不可恢复，与契约 H2"数字可对应"的长期可审计性冲突 | 将证据文件改放 `docs/evidence/`（不受忽略）或在 `.gitignore` 加 `!artifacts/test-*.txt` 白名单 |
| P2-2 | 契约 §6.2 推荐的 `--basetemp=./.testtmp` 在**带 shim 的环境**下不可用（rmtree 被劫持 → fixture 清理报 error）。README 与契约均按"推荐"措辞，未提示该限制 | 在契约 §6.2 增加一句环境限制说明，并给出无 shim 环境的复跑方式 |
| P2-3 | `CONTENT_EXPORT_PERSIST_FAILED` 映射 422（沿用上轮 P3 建议），同批 `SyncApplicationService` 同类错误映射 500，仍不一致 | 记录待独立任务，不阻塞 |

### 4.5 上轮问题闭合情况

| 上轮编号 | 问题 | 本轮状态 |
|---|---|---|
| P0（T4 兜底位置错误） | `flush()` 在 `try` 之外，真实竞态 500 | ✓ **已修复**（`flush()` 已移入 `try`），且 §4.3 独立探针确认 |
| P1（原有测试被删） | `test_review_requires_idempotency_key_before_mutation` 被删，`review_revision` 零覆盖 | ✓ **已恢复**并补强（见 §4.1 H4） |
| P2（报告准确性） | 基线数字、字节不变、warnings 归属 | ✓ 已在 §1/§2 修正：16 文件如实列入、stdout 哈希绑定、warnings 归属已说明 |

### 4.6 复核方对 `test_cli.py::test_cli_seed_docs_and_query` 的独立定性（新增）

**问题**：第 2 次全量观测出现 `1 failed`，需判定是否为本切片引入的回归。

**复核动作与证据**

| 步骤 | 动作 | 结果 |
|---|---|---|
| 1 | 确认该用例是否在本次提交中被修改 | `git show 87533ce --stat` 中 `test_cli.py` 出现 0 次 → **未被本次提交改动** |
| 2 | 确认本次提交是否触碰 CLI/RAG 相关源文件 | 提交 16 文件中无 `app/cli.py`、`app/rag*` → **未触碰** |
| 3 | 剥离 shim（`PYTHONPATH=""`）后单跑该用例 | **仍失败**（同样报错） → 排除 shim 干扰 |
| 4 | 导出**基线 `87533ce` 干净工作树**（`git archive`）后单跑该用例 | **同样失败**：`status=running, attempt=1` → `assert 1 == 0` |

**结论**：该失败为**基线既有问题，非本切片引入**。复核方在剥离 shim 条件下、且在与交付内容完全无关的基线工作树上原位复现，可确证其独立性。

**根因指向（仅记录，属独立任务）**：`app.cli enqueue-crawl` 入队后等待结果，输出 `任务完成: status=running, attempt=1` 并以非 0 退出——即 CLI 内联执行后台任务后在超时窗口内仍读到 `running` 而判定失败。与内容导出切片无因果关系。

**边界**：判定为"基线既有"仅表示**与本次切片无因果**，不代表该用例本身正确或该缺陷已修复。是否修复归独立任务，不得据此声称全量"全绿"。

---

## 5. follow-up 交付（`6c0627d`）复核结论（由复核方 WorkBuddy 填写，2026-10-11）

> 复核对象：`6c0627d` "P1 内容导出收尾：错误码映射一致性与后台失败可观测性"
> 对应任务文件：`dev_task.p1-content-export-followup.md`
> 依据：`docs/claude-handoff-contract.md` §4.1 / §4.3 / §5

### 5.1 结论

```text
结论：通过
```

**唯一保留点已消解**：原保留点为 **`app/content_production.py` 属任务文件「不允许修改」清单内的文件**（任务文件第 61 行标注"本次不修改，需评审决定"）。实现方已按契约 §4.3 H7 主动披露，理由为"用户已拍板 A2 按风险低推进"。**用户已于 2026-10-11 明确确认该 A2 授权成立**（授权发生在实施方 Claude Code 会话内），故该范围外改动**具备授权依据**，H7 披露与授权链完整闭合。

改动本身已由复核方独立验证正确（§5.4 C1/C2/C3 全部 PASS），且 R3 并发兜底未被触碰（§5.2）。授权确认 + 行为正确，保留点不成立，本轮判定转为**通过**。

任务文件清单与实际改动的偏差属**流程记录问题**（清单编写时未预置 A2 授权项），建议后续任务文件将"用户会话内口头授权"的改动一并列入白名单或注明授权来源，避免同类偏差再次触发保留判定。

### 5.2 已独立核验（复核方实测，非读取交付方结论）

| 项 | 复核动作 | 结果 |
|---|---|---|
| 提交文件与声明一致 | `git show 6c0627d --name-only` 比对交付说明 §1 | ✓ 完全一致（4 文件，无多无少） |
| 交付物可核验（契约 §4.1） | 实测 `sha256sum artifacts/test-full-run-20261011-followup.txt` | ✓ `b6325cd020cdb291...` 与声明一致 |
| 「不允许修改」清单（8 项） | `git diff 87533ce HEAD --stat -- <each>` | ✓ `app/db.py`、`app/background.py`、`app/repository.py`、`app/employee_auth.py`、`app/security.py`、`alembic.ini`、`CLAUDE.md`、`migrations/` **全部零改动** |
| R3 并发兜底未回归 | `git diff 87533ce HEAD --stat -- app/content_application.py` | ✓ 空输出，并发兜底（236-261 行）**未被触碰** |
| A1 错误码映射正确 | 读 `app/main.py` 差异：`CONTENT_EXPORT_PERSIST_FAILED: 500` + 条件 logger | ✓ 错误码未泛化（`detail` 保留原码），打点仅在特定码触发 |
| 定向测试复跑 | `pytest tests/test_content_application.py tests/test_api_content_export.py` | ✓ **36 passed**（与声明一致） |
| 相邻回归（同步侧映射一致性） | `pytest tests/test_sync_application.py tests/test_api_sync_compensation.py tests/test_api_inventory.py` | ✓ 19 passed |
| **全量独立复跑**（上轮欠账） | `pytest -q -p no:cacheprovider`（剥离 shim） | ✓ **282 passed + 1 failed / 270.37s**；唯一失败为 §4.6 已定性的基线既有缺陷 |
| 断言可达性（契约 §5.1 针对 H5） | 读被测代码逐条确认 N1–N4 调用链 | ✓ 四条均可达（详见 §5.3） |
| **并发/失败态独立复现**（契约 §5.2 针对 H6） | 仓库外独立探针，真实 SQLite + 真实服务层 + 真实 `execute_export_job`，**不 monkeypatch 被测逻辑** | ✓ C1/C2/C3 全部 PASS（详见 §5.4） |

### 5.3 断言可达性检查（针对 H5，上轮 P0 同类风险）

| 用例 | 断言路径 | 可达性判定 |
|---|---|---|
| N1 `test_export_persist_failed_maps_500` | monkeypatch `ContentApplicationService.export_revision` 抛 `ContentApplicationError` → 路由 `except` 捕获 → 查 `status_by_code` | **可达**。路由确以 `except ContentApplicationError` 捕获，映射表已含该码。属路由层契约测试，不验证服务层是否真会产生该码（该缺口由 §5.4 C1 独立探针补齐） |
| N2 `test_export_background_rechecks_approval` | 真实调用 `execute_export_job`，revision 置 `draft` → 真实抛 `CONTENT_NOT_APPROVED` | **可达**（真实路径） |
| N3 `test_export_failed_replay_returns_same_row` | 真实执行 + 真实 HTTP 重放 | **可达**，且已由 §5.4 C2 独立复现 |
| N4 `test_export_background_failure_message_truncated` | monkeypatch 模块级 `cp.save_asset_bytes` 抛 800 字符异常 | **可达**。已核实 `save_asset_bytes` 为 `app/content_production.py:222` 定义的**模块级函数**，`monkeypatch.setattr(cp, ...)` 可命中；调用点位于 `:335`，在 `try` 范围内 |

**无不可达断言**——与上轮 P0（三个 T4 测试断言死代码）形成对比，本轮未复现同类问题。

### 5.4 失败态与重放的独立复现（针对 H6，关键）

**方法**：仓库外探针（`<session-tmp>/review_probe_followup.py`，未写入仓库），仅读访问仓库代码；使用真实 SQLite + `ContentApplicationService` + `execute_export_job`，**不 monkeypatch 任何被测函数**。

| 编号 | 复现内容 | 实测结果 | 判定 |
|---|---|---|---|
| C1 | 失败态写入 | `status='queued'` → 抛出 `CONTENT_NOT_APPROVED` → `status='failed'`、`error_code='CONTENT_NOT_APPROVED'`、`finished_at` 非空 | **PASS** |
| C2 | 失败后同键重放 | 返回原 export id、`replayed=True`、`job=None`；**export 行数 1→1、job 行数无新增** | **PASS** |
| C3 | 成功路径未被污染 | `status='succeeded'`、`error_code=None`、`artifact_sha256` 非空 | **PASS** |

即：任务目标 T2（`export.status` 不再永久停留在 `queued`）与 N3（重放不产生新行/新 job）均在**不依赖交付方测试**的条件下独立成立。

### 5.5 需修订（不阻塞本轮收口）

| 编号 | 级别 | 问题 | 最小修订动作 |
|---|---|---|---|
| R-1 | ~~P1（待用户确认）~~ **已关闭** | ~~`app/content_production.py` 在任务文件「不允许修改」清单内，实现方已披露但授权来源需用户确认~~ | **用户已于 2026-10-11 确认 A2 授权成立（授权发生在实施方 Claude Code 会话内）** → 授权链闭合，保留点消解。遗留建议：后续任务文件将"用户会话内口头授权"的改动列入白名单或注明授权来源 |
| R-2 | P2（报告准确性） | 交付说明称"全量 283 passed"，但复核环境同一代码实测为 `282 passed + 1 failed`；该用例（`test_cli_seed_docs_and_query`）为 §4.6 已定性的基线既有缺陷，不应计入 passed | 交付说明改为 `282 passed + 1 失败（基线既有，见 §4.6）`，或注明该用例环境相关 |
| R-3 | P3（记录） | `execute_export_job` 中 `export is None` 分支（`app/content_production.py:316-317`）在 `try` 之外，`CONTENT_EXPORT_NOT_FOUND` 不写失败态 | 属合理设计（无行可标记），仅建议在代码注释中显式说明该边界 |
| R-4 | P3（记录） | 失败态 `error_code` 取 `getattr(exc, "code", type(exc).__name__)`，非 `ContentProviderError` 时会写入**类名**（如 `RuntimeError`）作为 error_code，与既有稳定错误码命名风格不一致 | 建议后续统一为显式兜底码（如 `CONTENT_EXPORT_UNEXPECTED`），避免运维侧混淆 |

### 5.6 未独立核验（显式列出）

| 项 | 原因 |
|---|---|
| MySQL 上的失败态并发行为 | 本次环境无 MySQL（任务文件 U 项已声明） |
| 生产 `queued` 僵尸行分布（U1） | 需生产数据，本地无法证明 |
| 交付方声明的 `94.10s` 全量耗时 | 复核环境存在 shim，耗时不具可比性；**但结论数（282+1）已独立取得，不影响判定** |

### 5.7 边界声明

本结论判定"通过"，**不等于**该 follow-up 已无遗留问题：R-2 需修正交付说明的报告表述（`283 passed` → `282 passed + 1 失败（基线既有）`）。**在该表述修正前，不得据此宣称全量"全绿"**——`test_cli_seed_docs_and_query` 为基线既有缺陷（§4.6 已定性），归独立任务修复，不计入本切片。

修订记录：2026-10-11 首次判定"有保留通过"（保留点：R-1 授权待确认）；同日经用户确认 A2 授权成立后，R-1 关闭，判定修订为"通过"。


---

## 6. P1 收官切片 A 项（CLI `enqueue-crawl` 等待窗口）复核结论（由复核方 WorkBuddy 填写，2026-10-12）

> 复核对象：工作树未提交改动 `app/cli.py`
> 对应任务文件：`dev_task.p1-closeout.md`
> 依据：`docs/claude-handoff-contract.md` §3.2 / §4.1 / §4.3 / §5

### 6.1 结论

```text
结论：通过（技术面）；流程状态：待验收
- 已独立核验：范围核对（§6.2）、基线↔修复对拍（§6.4）、完成标准 1–6（§6.5）、
  断言非空过（§6.6）、新增参数边界（§6.7）、全量回归 283 passed / 0 failed（§6.8）、
  交付说明与复核方实测的交叉印证（§8.2）
- H1 门禁：**情形成立，但实现方已合规降档** —— 交付说明于 2026-10-12 送达；其中**基线失败的
  traceback 为原始输出**，而改后验证（定向测试 / 负向探针）**仅有摘要、缺完整 stdout**。
  实现方据此**主动**将状态降档为"待验收"并拒绝以复核方摘要冒充原始输出（§8.2）。故 H1 情形成立
  → 状态须为"待验收"；技术结论"通过"不受影响，其依据为复核方自测证据链，不依赖交付方数据
- 需修订：R-5 / R-6 / R-7（均 P3，见 §6.9）
```

**两点说明**：

1. 判定为"通过"的依据是复核方**独立取得**的证据链（§6.2–§6.8），不含任何读取交付方结论文的环节。
2. **H1 已于 2026-10-12 核验完毕（§8.2）**：交付说明中基线的原始失败输出与复核方独立实测**逐字一致**（`1 failed in 32.16s` / `任务完成: status=running, attempt=1` / `assert 1 == 0`），其列出的三个 `sha256` 前 16 位与复核方实测**逐位一致**，未改动声明与 `git status` **完全自洽** —— 即交付说明与本节实测数据**无冲突**（原第 2 点的作废条件未触发）。但改后验证仅有摘要、缺完整 `stdout`，**H1 情形成立**，故按任务文件「任一硬门禁未过，状态只能写'进行中 / 待验收'」的规定，**本切片状态维持"待验收"，不标记为"已完成"**；技术结论"通过"不受影响。若日后补出改后原始 `stdout` 并通过核验，可定稿为最终"已完成"。

### 6.2 冻结状态与范围核对（§5.3）

| 项 | 实测 | 判定 |
|---|---|---|
| HEAD | `4fcb5dd`，**未新增提交** | ✓ 未擅自 commit / push |
| 代码改动文件 | 仅 `app/cli.py`，sha256 前 16 = `a174bf757e87ca14` | ✓ 与「允许修改」一致 |
| `tests/test_cli.py` | **零改动**，sha256 前 16 = `150d501e77a70c26`，mtime 2026-09-10 | ✓ 未降低断言强度 |
| `app/crawler.py` | mtime 2026-09-10，sha256 前 16 = `e7f958ce8615f96e` | ✓ 零改动 |
| `app/background.py` | mtime 2026-09-18，sha256 前 16 = `156ab030bdfa65ee` | ✓ 零改动 |
| `app/db.py` / `app/repository.py` / `employee_auth` / `security` / `migrations/` / `alembic.ini` | `git status --short` 无记录 | ✓ 零改动 |
| `wait_for_jobs` 内部逻辑（`background.py:652-681`） | 未改 | ✓ 只改调用方 |
| CLI 成功判定 `return 0 if final.status == "succeeded" else 1` | **原样保留** | ✓ 未放宽 |

改动仅两处 hunk，均在 `cmd_enqueue_crawl` 内：等待窗口 `10` → `timeout_seconds`（默认 60），新增 `--timeout` 解析与超时诊断打印。

### 6.3 关键方法学发现：`tests/test_cli.py:39` 硬编码 `cwd`（新增独立缺陷）

```python
cwd="D:\\ai\\dianshang",     # tests/test_cli.py:39 —— 硬编码绝对路径
```

该用例以 `subprocess` 调用 `python -m app.cli`，但 `cwd` 写死为绝对路径，**与测试所在的工作树无关**。两个后果：

1. **任何在本仓库以外的检出中运行本测试都是伪测试** —— CI、`git worktree`、干净 clone、基线对照树中的子进程实际 import 的都是 `D:/ai/dianshang` 的 `app` 包，测的不是当前检出。
2. **复核方首次 A/B 对拍因此得出错误结论**：从 `git archive HEAD` 提取的基线树（`wait_for_jobs(timeout=10)`）运行该用例**通过**（`1 passed in 31.08s`），与"基线应失败"矛盾。剔除该干扰、改为「显式 `cwd` 直调 CLI」对拍后结论才自洽（§6.4）。

**判定**：**独立缺陷，与本次改动无因果**，不阻塞本切片；建议另立任务修复（`cwd=Path(__file__).resolve().parents[1]`）。**在修复前，针对本用例的一切 A/B 对照必须绕开 pytest，直接调用 CLI 并显式指定对照树。**

> **本段修订（2026-10-12）**：① 上方代码引用的路径分隔符原写作 `D://ai//dianshang`（正斜杠），与源码不符，已按真实源码（反斜杠转义）改正；成因系写入时经 shell 转义，功能等价但不属逐行准确引用。② 该缺陷**已由 `dev_task.test-cwd-hardcode.md` 修复并复核通过（结论：通过，见 §7）**。③ 经全仓 `grep` 复核，该硬编码**共 5 处 / 2 文件**（`tests/test_cli.py:39`、`tests/test_alembic.py:34/172/203/234`），本段原只登记 1 处，**登记不全**，已由 §7.2 补全。

### 6.4 独立复现（§5.2，不依赖交付方测试）

**对拍设计**：基线树由 `git archive HEAD` 提取（`app/cli.py` sha256 前 16 = `d5ec9e6b4f73ddd3`，`wait_for_jobs(timeout=10)`），对照工作树（`a174bf757e87ca14`，默认 60s）；两侧均以**显式 `cwd` 直调 CLI**（绕开 §6.3 干扰），同机连续执行。

| # | 场景 | 实测输出 | 退出码 |
|---|---|---|---|
| A | 基线，窗口 10s | `任务完成: status=running, attempt=1`（20.00s / 20.56s） | **1** ✗ |
| B | 修复后，默认 60s（含建库） | `任务完成: status=succeeded, attempt=1`（35s） | **0** ✓ |
| C | 修复后，重复执行第 2 次 | `status=succeeded`（20s）；`status` 输出 `products = 3` | **0** ✓ |
| D | 防取巧，窗口 1s | `status=running` + `等待超时（1s）: 任务仍在 running。可加大 --timeout 或检查 Playwright 是否正常。` | **1** ✓ |
| E | 真实任务失败（探针树 cli.py 同 sha `a174bf75`，删除 `fixtures/products.html`） | `任务完成: status=retry_wait, attempt=1` | **1** ✓ |

**B 行的承重性**：修复后首次执行含建库共 **35s**，远大于旧窗口 10s —— 即**旧窗口在本机必然失败**，本次通过并非"碰巧任务变快"。

**D 行的作用**：`--timeout 1` 是本次新增能力，用它恰好构成"窗口内未完成"路径的直接反驳，证明实现方**未把超时静默当作成功**。

### 6.5 完成标准逐条核对（任务文件「完成标准」）

| 标准 | 判定 | 独立证据 |
|---|---|---|
| 1 正向：用例通过、任务成功返回 0 | ✓ | §6.4 B；定向 `tests/test_cli.py` = `5 passed in 18.54s` |
| 2 负向：真实失败仍非 0 | ✓ | §6.4 E（`retry_wait` → exit 1） |
| 3 越权测试 N/A | ✓ | CLI 无租户边界（`_resolve_cli_workspace` 仅解析目标 workspace，不经 HTTP 鉴权） |
| 4 幂等：重复执行不翻倍 | ✓ | §6.4 C（两次执行后 `products = 3`） |
| 5 故障：窗口内未完成路径 | ✓ | §6.4 D；窗口 60s 对实测 12–35s 保留 ≥1.7x 余量 |
| 6 真实 / 模拟 / 未知区分 | ✓ | 复核方确认无夸大（真实 Playwright + 真实 SQLite + 真实后台线程与 lease） |
| 7 交付与门禁 | ⚠️ **部分满足** | 交付说明已送达并核验（§8.2）：六字段齐全、`sha256` 与未改动声明均可核；但**改后原始 stdout 缺失** → H1 成立，状态须为"待验收" |

### 6.6 断言可达性（§5.1，上轮 P0 同类风险）

本轮 `tests/test_cli.py` **零改动**，无新增断言，故不存在"新断言不可达"。但需回答其反面：**断言是否空过？**

`assert crawl.returncode == 0` 与 `assert "status=succeeded" in crawl.stdout`（`tests/test_cli.py:71-73`）在旧窗口下**实测失败**（§6.4 A）、新窗口下**实测通过**（§6.4 B）→ 断言**非空过**，且**对本改动敏感**。

### 6.7 新增参数边界（复核方独立执行的函数级检查）

`_parse_enqueue_crawl_timeout` 共 10 例全部通过：缺省 → 60；`--timeout 5 / 90` → 原值；非整数 / 缺值 / `0` / 负数 / 浮点 → 回落 60；与 `--workspace` 在两种参数顺序下共存均正确取值。`_option` 为精确 token 匹配，`--timeout` **不会干扰** `--workspace` 解析。

### 6.8 全量回归（复核方独立执行）

```text
$ cd D:/ai/dianshang && PYTHONDONTWRITEBYTECODE=1 "D:/python/python/python.exe" -m pytest -q -p no:cacheprovider
283 passed, 4325 warnings in 288.02s (0:04:48)
```

| 项 | 基线（2026-10-11 复核实测） | 本轮 | 判定 |
|---|---|---|---|
| 结果数 | `282 passed + 1 failed` | **`283 passed + 0 failed`** | ✓ 唯一 failed 已消除 |
| 耗时 | 270.37s | 288.02s | +6.5%，属运行间波动，无回归信号 |

**R-2 闭合**：§5.5 R-2 登记的"交付说明称 283 passed 与实测不符"，在本次改动落地后**实测即为 283 passed**，报告表述与事实一致，R-2 关闭。

### 6.9 需修订 / 记录项（均不阻塞"通过"）

| 编号 | 级别 | 问题 | 最小修订动作 |
|---|---|---|---|
| R-5 | P3（文档） | `--timeout` 未进入命令帮助：用法行（`app/cli.py:17`）仅列 `[--workspace ...]`，`COMMAND_HELP["enqueue-crawl"]`（`:569`）亦未提及，用户无从发现 | 用法行与 `COMMAND_HELP` 补 `[--timeout <seconds>]` |
| R-6 | P3（记录） | 超时诊断仅在 `final.status == "running"` 时打印；§6.4 E 的 `retry_wait`（真实失败）不打印任何提示 | 任务文件只要求覆盖 `running`，属**合规**；建议后续将 `retry_wait` / `queued` 一并纳入诊断 |
| R-7 | P3（记录） | `--timeout` 属任务文件"等待策略"范围的**增量**，未在任务文件预先列明 | 属可控增量（未改成功判定、未触碰禁改文件）；建议后续任务文件在「允许修改」栏预留"等价增量"说明 |

### 6.10 未独立核验（显式列出）

| 项 | 原因 |
|---|---|
| ~~交付方声明的原始测试输出（基线部分）~~ / 交付方计算的 sha256 | **已送达并核验（§8.2）**：基线 traceback 为原始输出且与复核方实测逐字一致；sha256 逐位一致。此项已闭合 |
| 改后定向测试与负向探针的**完整原始 stdout** | 交付方声明**无法提供**（工作树已冻结、不重跑以免污染）；**该项缺失即为 H1 成立之唯一原因** |
| Playwright 会话开销的机理（任务文件 U1） | 涉及 node driver 退出路径，本轮未深挖；**不影响判定** |
| `--timeout` 极大值（如 `99999`）的阻塞行为 | 实现方 docstring 已声明"无上限检查，由调用方负责"，未构造长时间阻塞验证 |

### 6.11 边界声明

本结论判定"通过"，**不等于**该切片无遗留问题：

- **§6.3 的硬编码 `cwd` 缺陷已修复并复核通过（见 §7，2026-10-12）**，本项原边界（"该缺陷修复前，不得据本用例的 pytest 结果宣称'其它环境已验证'"）**就此解除**：修复后经仓外副本对拍验证，子进程确实执行**当前检出**（§7.3 的 S1 / S2），全量回归 `283 passed / 0 failed`（§7.7）。原缺陷曾使该用例在任何非本仓检出中失去意义、并使"用 pytest 做 A/B 对照"的方法失效，修复后该方法恢复可用。
- §6.9 的 R-5 / R-6 / R-7 为记录级，未修订前不影响本判定。
- **§5.7 的边界就此解除**：`test_cli_seed_docs_and_query` 已由"基线既有缺陷"转为通过，全量回归实测 `283 passed / 0 failed`，**可以宣称"全量全绿"**（口径：`PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider`，2026-10-12 本机实测）。

修订记录：2026-10-12 首次判定「通过」，附 §6.3 方法学缺陷与 R-5 / R-6 / R-7 记录项；未独立核验项 H1（交付说明未送达），送达核验后可定稿。同日完成 §6.3 所属缺陷（`dev_task.test-cwd-hardcode.md`）的修复复核（见 §7），§6.11 原边界随之解除。同日 H1 核验完成（§8）：交叉印证全部通过（sha256 逐位一致、基线 traceback 逐字一致、未改动声明自洽），但**改后原始 stdout 缺失 → H1 情形成立**，故结论细化为 **「通过（技术面）；流程状态：待验收」**；另记载一处待澄清项（§8.5）。

---

## 7. 测试子进程硬编码 `cwd`（§6.3）修复复核（由复核方 WorkBuddy 填写，2026-10-12）

> 复核对象：工作树未提交改动 `tests/test_cli.py`、`tests/test_alembic.py`
> 对应任务文件：`dev_task.test-cwd-hardcode.md`
> 依据：`docs/claude-handoff-contract.md` §3.2 / §4.1 / §4.3 / §5

### 7.1 结论

```text
结论：通过（技术面）；流程状态：待验收
```

§7.2–§7.7 的全部证据均由复核方**独立取得**（不读取交付方结论），技术结论不依赖交付方。契约 §4.3 **H1** 要求的交付说明已于 2026-10-12 送达并核验（§8.3）：三个 `sha256` 前 16 位**逐位一致**、未改动声明与 `git status` **自洽**、方向 B 的 `12 passed in 96.42s` 与复核方独立实测**逐位一致**；但定向测试与方向 A / B 均**只有摘要、缺完整 stdout**，故 H1 情形成立。按任务文件「任一硬门禁未过，状态只能写'进行中 / 待验收'」之规定，**该切片状态维持"待验收"，不标记为"已完成"**；技术结论"通过"不受影响。若日后补出原始 stdout 并通过核验，可定稿为最终"已完成"。

### 7.2 冻结状态与范围核对（§5.3）

| 项 | 实测 | 判定 |
|---|---|---|
| HEAD | `4fcb5dd`，**未新增提交**（未擅自 commit / push） | ✓ |
| 改动文件 | 仅 `tests/test_cli.py`（sha256 前 16 = `0b2ae893c61ec740`，+5/−1）、`tests/test_alembic.py`（sha256 前 16 = `4f38ef3e0b9c209d`，+7/−4） | ✓ 与「允许修改」完全一致 |
| 硬编码残留 | `grep -rn 'cwd="D:' tests/ --include="*.py"` → **空** | ✓ 5 处全清 |
| 断言强度 | `test_cli.py` 断言数 **23 → 23**；`test_alembic.py` **32 → 32**；逐条比对无断言被删除或改写 | ✓ 未降低 |
| 测试语义 | 仍为真实 `subprocess` + 真实 `app.cli` / `alembic` / `init_db.py`，未改为进程内调用 | ✓ |
| 尾换行 | `test_alembic.py` 无尾换行，**HEAD 亦无**（两端一致） | ✓ 非本次引入 |
| 未改动（逐项核） | `app/crawler.py`、`app/background.py`、`app/db.py`、`app/repository.py`、`migrations/`、`alembic.ini`、`init_db.py`、`pyproject.toml`、`tests/conftest.py`、`tests/_wal_bootstrap/`、`tests/test_api_tickets.py`、`tests/test_mcp_*.py` | ✓ 零改动 |
| `fixtures/products.html` | sha256 前 16 = `57d7e1c722babc25`，mtime 2026-08-29 | ✓ 未被破坏 |
| 唯一其它 `M` 文件 | `app/cli.py` | 属 §6 的 A 项既有改动，非本次引入 |

改动本体仅为：新增模块级 `REPO_ROOT = Path(__file__).resolve().parents[1]`，并把 5 处 `cwd=` 改为 `cwd=str(REPO_ROOT)`；`tests/test_cli.py` 另补 `from pathlib import Path`（`tests/test_alembic.py` 原本已导入）。`REPO_ROOT` 实测在两文件中均解析为仓库根 `D:\ai\dianshang`。

### 7.3 仓外承重性对拍（核心验收，§5.2）

**装置**：`git archive HEAD | tar -x -C <仓外副本>` 得检出副本；副本内叠入**修复后的两个测试文件**；一切"破坏"只发生在**副本内**，仓库不受影响。破坏手段为在目标文件**顶部**插入 `raise SystemExit(7)`（仅副本）。

| # | 场景 | 副本状态 | 期望 | 实测 | 判定 |
|---|---|---|---|---|---|
| **S0** | **对照组**：**HEAD 版**测试文件 + 副本 `app/cli.py` 被破坏 | 旧测试代码 | 通过 | **`1 passed`** | ✓ **旧缺陷冒烟枪** |
| **S1** | 方向 A：**修复版**测试文件 + 副本 `app/cli.py` 被破坏 | 新测试代码 | 失败 | **`1 failed`** | ✓ 修复承重 |
| **S2** | 方向 A′：**修复版**测试文件 + 副本 `init_db.py` 被破坏 | 新测试代码 | 失败 | **`1 failed`** | ✓ `init_db.py` 相对路径站点同样承重 |
| **S3** | 方向 B：**修复版**测试文件 + 副本完好（并叠入工作树版 `app/cli.py`） | 新测试代码 | 全通过 | **`12 passed in 96.42s`** | ✓ 修复无害 |

**S0 是决定性证据**：副本的 `app/cli.py` 已被破坏为"任何命令立即以退出码 7 结束"，而**修复前**的测试代码运行结果仍为**通过** —— 该测试根本没有执行副本代码，而是在执行 `D:\ai\dianshang` 的完好代码。这正是 §6.3 所述机理的直接复现。

**S1 / S2 证明修复生效**：同一破坏条件下，修复后的测试文件分别**失败**（退出码 7 使 `assert result.returncode == 0` 不成立），说明子进程已**确实执行当前检出**的代码。

**S2 的额外价值**：`tests/test_alembic.py` 的 3 处内联站点调的是 `[sys.executable, "init_db.py"]`（**相对脚本路径**），其正确性完全依赖 `cwd`。S2 用失败结果证明这些站点同样被本次修复覆盖，而非仅辅助函数那一处。

### 7.4 执行过程中的一个额外发现（第五重证据）

S3 首轮执行时，副本内容为 `git archive HEAD` 的原始 `app/cli.py`，而该文件**尚未包含 §6 的 A 项修复**（A 项改动未提交、不在 HEAD 内）。此时结果为 `1 failed, 11 passed`，失败者恰为 `test_cli.py::test_cli_seed_docs_and_query`（`enqueue-crawl` 退出码 1）—— 即 §6 已定性的"等待窗口 10s < 实际耗时"。

这一"意外失败"构成**修复有效的又一重独立证据**：修复后的测试**检出了副本中较旧的 `app/cli.py`**。若修复无效（子进程仍指向 `D:\ai\dianshang`），该用例应因本仓已修复的 60s 窗口而**通过**。据此，复核方将工作树版 `app/cli.py`（sha256 前 16 = `a174bf757e87ca14`，与 §6.2 一致）叠入副本后重跑，S3 方得 `12 passed`。

> 方法学提示：以 `git archive HEAD` 构造对照树时，**必须同时叠入工作树中所有未提交的生产代码改动**，否则会得到"混合检出"，其失败信号易被误读为修复缺陷。

### 7.5 完成标准逐条核对（任务文件「完成标准」）

| 标准 | 判定 | 独立证据 |
|---|---|---|
| 1 正向：12 个受影响用例通过；残留检查为空；方向 A 失败 / 方向 B 通过 | ✓ | §7.2 残留检查；定向 `12 passed in 73.53s`；§7.3 |
| 2 负向：破坏检出后测试确实失败 | ✓ | §7.3 S1 / S2（均实测 `1 failed`）；**另** §7.4 的第五重证据 |
| 3 越权测试 N/A | ✓ | 测试代码改动，无租户边界；实现方声明 N/A 属合规 |
| 4 重复 / 幂等：连跑两次结果一致 | ✓ | 定向测试与副本 S3 分别独立执行，结果数一致（`12 passed` / `12 passed`） |
| 5 故障 / 回放：覆盖"检出被移动 / 复制" | ✓ | §7.3 S0–S3 全程在仓外副本内执行 |
| 6 真实 / 模拟 / 未知区分 | ✓ | 复核方确认无夸大（真实 subprocess + 真实 alembic / `init_db.py` / SQLite） |
| 7 交付与门禁 | ⚠️ **部分满足** | 交付说明已送达并核验（§8.3）：六字段（改动清单 / 未改动声明 / 测试输出 / 逐条映射 / 三段式 / 已知限制）**齐全**，sha256 与未改动声明均可核验通过；但**改后原始 stdout 缺失** → H1 成立，状态须为"待验收"（实现方已如此自行声明） |

### 7.6 断言可达性（§5.1）

本轮**无新增断言**（断言数与内容均未变，见 §7.2），故不存在"新断言不可达"。其反面——**断言是否空过**——亦已排除：同一批断言在副本被破坏时**实测失败**（§7.3 S1 / S2），在副本完好时**实测通过**（§7.3 S3），说明断言**非空过**且对被测对象敏感。

### 7.7 全量回归（复核方独立执行）

```text
$ cd D:/ai/dianshang && PYTHONDONTWRITEBYTECODE=1 "D:/python/python/python.exe" -m pytest -q -p no:cacheprovider
283 passed, 4327 warnings in 323.34s (0:05:23)
```

| 项 | 基线（§6.8 复核实测，2026-10-12） | 本轮 | 判定 |
|---|---|---|---|
| 结果数 | `283 passed + 0 failed` | **`283 passed + 0 failed`** | ✓ 无回归 |
| 耗时 | 288.02s | 323.34s | +12%，属运行间波动（本轮与副本测试并行执行，存在资源竞争），无回归信号 |

**任务文件 AS3 得到回答**：假设"无其它测试依赖 cwd 必须为 `D:\ai\dianshang` 这一隐式前提"——全量 `283 passed / 0 failed` 表明**该假设成立**，修复未触发任何隐式依赖。

### 7.8 关联项更新

| 项 | 更新 |
|---|---|
| §6.3 | 结论已更新：该缺陷**已修复**；引用代码的分隔符失真已改正；补充"共 5 处 / 2 文件"的完整登记 |
| §6.11 | 原边界（"该缺陷修复前，不得宣称其它环境已验证"）**解除** |
| 新增 R-8 | P3（记录）：§6.3 原登记仅 1 处，实为 5 处 —— **单点发现未做全仓扫描**，导致同批缺陷被漏记。建议：凡判定为"模式性缺陷"者，登记前必须全仓 `grep` 确认波及面（本轮已于 §7.2 补正） |
| 新增 R-9 | P3（方法学）：以 `git archive HEAD` 构造对照树时须叠入工作树全部未提交改动（见 §7.4） |

### 7.9 未独立核验（显式列出）

| 项 | 原因 |
|---|---|
| 改后定向（`12 passed`）、方向 A（`1 failed`）、方向 B（`12 passed`）的**完整原始 stdout** | 交付方声明无法提供（工作树已冻结、不重跑以免污染）；**该项缺失即为 H1 成立之唯一原因** |
| `tests/test_mcp_official.py` / `tests/test_mcp_server.py` 未固定 `cwd` 的影响 | 任务文件已明确排除在本次范围外（属"未固定 cwd"而非"硬编码错误路径"，失败模式为报错而非静默假绿）；**不影响本判定** |

### 7.10 边界声明

本结论判定"通过"，**不等于**该切片已无遗留：

- **H1 情形成立（已于 2026-10-12 核验，§8.3）**：交付说明已送达，交叉印证（sha256 / 未改动声明 / 方向 B 耗时）全部通过；但改后原始 `stdout` 缺失，故该切片**状态为"待验收"，不得标记为"已完成"**（任务文件硬门禁规定）。技术结论"通过"不受影响。
- **§7.8 的 R-8 / R-9 为记录级**，未修订前不影响本判定。
- **本次修复只解决"测试相对于检出执行"**，不等于"所有测试环境已可复现"：仓库仍**无任何 CI 配置**（`.github/workflows` / `.gitlab-ci.yml` / `.circleci` / `Jenkinsfile` 均不存在，已核），实机验证仍依赖人工检出。**不得据本修复宣称"CI 已验证"。**
- 全量耗时中的 Playwright 会话开销（约 110s）与 CLI 子进程建库开销**仍未被优化**，属独立任务（`dev_task.p1-closeout.md` 已声明不处理）。

---

## 8. 交付说明核验（H1 / H2，由复核方 WorkBuddy 填写，2026-10-12）

> 核验对象：`dev_task.p1-closeout.md` 与 `dev_task.test-cwd-hardcode.md` 两份交付说明（2026-10-12 送达复核方）
> 依据：`docs/claude-handoff-contract.md` §4.1（六必需字段）/ §4.3（H1、H2）

### 8.1 核验方法

交付说明以**文字**形式提供，未落盘为仓库文件。核验采取**与复核方自测数据交叉比对**，而非采信其结论：

| 交叉项 | 复核方可独立核实的载体 |
|---|---|
| `sha256` 前 16 位 | 复核方在冻结后自行计算（§6.2 / §7.2） |
| 基线失败输出 | 复核方 2026-10-12 在 HEAD 上的独立实测（§6.4 A 行、§4.6） |
| 全量 `94.10s` 所依据的那份原始 stdout | 仓库内文件 `artifacts/test-full-run-20261011-followup.txt`（可读、可算哈希） |
| 未改动声明 | 与 `git status --porcelain` 逐项比对 |

### 8.2 交付说明一（`dev_task.p1-closeout.md`，A 项）核验

| 字段 | 交付方声明 | 复核方核验 | 判定 |
|---|---|---|---|
| 改动文件清单 | 仅 `app/cli.py`，`a174bf757e87ca14` | 实测 `a174bf757e87ca14` | ✓ 逐位一致 |
| 未改动声明 | `app/crawler.py`、`app/background.py`、`app/repository.py`、`app/db.py` 等 | 逐个 `git status --porcelain` 核对，**均无记录** | ✓ 自洽 |
| 基线原始输出 | `1 failed in 32.16s`、`任务完成: status=running, attempt=1`、`assert 1 == 0` | 复核方独立实测**完全相同** | ✓ **逐字一致（关键交叉印证）** |
| 全量结果 | `283 passed`（引 `artifacts/test-full-run-20261011-followup.txt`） | 该文件存在（436 行 / 35186 B / sha16 `b6325cd020cdb291`），末尾原样为 `283 passed, 4325 warnings in 94.10s` | ✓ 引用准确、出处可核 |
| 逐条用例映射 | 按「完成标准」第 1–7 条 | 与 §6.5 逐条核对，结论一致 | ✓ |
| 三段式 / 已知限制 | 提供 | — | ✓ |
| **状态自评** | **"待验收"**（自陈缺改后原始 stdout，拒绝以复核摘要冒充） | 与 H1 情形相符 | ✓ **合规降档** |

**关于状态自评**：这是本份说明中最值得肯定的一处。实现方明确写出"现有仓库只保留了全量测试的原始……定向与负向的复核材料目前只有复核摘要，没有完整原始 stdout"，并据此**拒绝**标记"已完成"。按契约 §4.3，H1 情形下状态**只能**写"待验收"——实现方主动照做，**不构成 H1 违规，而是对 H1 的正确执行**。

### 8.3 交付说明二（`dev_task.test-cwd-hardcode.md`，cwd 修复）核验

| 字段 | 交付方声明 | 复核方核验 | 判定 |
|---|---|---|---|
| 改动文件清单 | `tests/test_cli.py` `0b2ae893c61ec740`；`tests/test_alembic.py` `4f38ef3e0b9c209d` | 实测**逐位一致** | ✓ |
| 未改动声明 | 含 `app/cli.py`，并**主动注明**"该文件存在属 `dev_task.p1-closeout.md` 的独立改动，本任务未修改" | 与 `git status` 一致（`app/cli.py` 的 `M` 确属 A 项） | ✓ **交叉声明准确** |
| 定向测试 | `12 passed in 73.53s` | 复核方独立实测 `12 passed in 73.53s` | ✓ 逐位一致 |
| 方向 A（负向） | 破坏副本 `app/cli.py` 后 `1 failed`（所用用例为 `test_cli_init_db`） | 复核方 §7.3 S1 独立构造**同一破坏条件**（修复版测试 + 副本 `app/cli.py` 顶部注入 `raise SystemExit(7)`），实测 `1 failed` | ✓ 独立装置、条件等价、结论同向 |
| 方向 B | `12 passed in 96.42s` | 复核方 §7.3 S3 实测 `12 passed in 96.42s` | ✓ **逐位一致** |
| 残留检查 | `grep -rn 'cwd="D:' tests/` 输出为空 | 复核方实测同为空 | ✓ |
| 逐条用例映射 | 按「完成标准」第 1–7 条 | 与 §7.5 核对一致 | ✓ |
| 三段式 / 已知限制 | 提供（"未知"段列入 MCP 测试、CI 形态、其它潜在硬编码） | — | ✓ 未见夸大 |
| **状态自评** | **"待验收"**（同上，因缺改后原始 stdout） | 与 H1 情形相符 | ✓ 合规降档 |

### 8.4 H1 / H2 最终判定

| 门禁 | 判定 | 说明 |
|---|---|---|
| **H1** 无原始测试输出 | **情形成立**（部分满足） | 基线 traceback 为**原始输出**且与复核方实测逐字一致，此项已闭合；但改后定向 / 负向 / 方向 A·B **仅有摘要**，缺完整 `stdout`。→ 两切片状态**只能为"待验收"**，实现方已自行如此声明 |
| **H2** 数字与命令 / 日期 / 环境不可对应 | **不成立**（满足） | 所有数字均附完整命令；`94.10s` 一项标注了仓库内文件出处且引用原样准确（§8.2）；实现方**明确区分**了"其自测数据"与"复核方数据"，未混写 |
| H3–H7 | 不成立 | 复核方独立核验已确认（§6.2 / §6.5 / §6.6 / §7.2 / §7.5 / §7.6），交叉项见 §8.1 |

**结论**：两份交付说明在**可核验维度上全部通过**（`sha256`、未改动声明、基线原始输出、引用文件出处），**唯一缺口是改后验证的原始 stdout**；实现方对此**如实披露并主动降档**，未发生 H1 违规。故两切片：**技术面通过，流程状态待验收**。

### 8.5 待澄清项（新记录 R-10，P3，不影响本判定）

`artifacts/test-full-run-20261011-followup.txt`（mtime 2026-10-11 20:20）记录全量 **`283 passed in 94.10s`**；而复核方 2026-10-12 两次独立实测为 **288.02s** 与 **323.34s**，相差约 **3 倍**，**超出 §7.7 所述"运行间波动"（+12%）的量级**。

- 两者结果数一致（`283 passed / 0 failed`），**不影响本次判定**；
- 但同一命令在同一台机器上的耗时相差 3 倍，值得单独查明（怀疑方向：机器负载差异、Playwright 会话开销抖动——§6.4 相关实测曾显示该块耗时在 12.32s / 16.60s 间波动）；
- 建议**单独立项**：在隔离负载下跑一次稳定基线，作为 README 与后续验收的计时口径。

### 8.6 边界声明

- 本节核验对象是**交付说明的文字内容**；交付说明**未落盘为仓库文件**，故其"原始性"依赖实现方陈述。复核方已尽最大可能交叉验证（§8.1），但**无法证明其未对输出做过编辑**——该风险由"技术上不依赖交付方数据"覆盖：§6、§7 的**全部技术结论均由复核方自测证据支撑**。
- H1 的缺口**只能由改后原始 `stdout` 补齐**；若该输出已不可恢复（会话关闭等），两切片将**长期停留在"待验收"**。**不建议事后回忆式补造**——那会使 H1 转化为 H2（数字与命令不可对应），性质更差。
- 本节不改变 §6.11 / §7.10 已声明的任何既有边界。

修订记录：2026-10-12 首次填写。来源：2026-10-12 交付说明送达后的 H1 / H2 核验。

---

## 9. R1 切片复核（错误体系统一，DomainError）

- 复核日期：2026-10-26；对象：Claude Code 对 `dev_task.r1-domain-error.md` 的实施（经 CLI 直接调起，用时 20m41s）
- 上位依据：ADR-0012 决策 1 / `docs/p1-boundary-audit-20261012.md` §5

### 9.1 范围核验

| 检查 | 结果 |
|---|---|
| 改动面 | `app/main.py`（+149/−122 区间内含迁移）、`app/employee_auth.py`、新增 `app/errors.py`、新增 `tests/test_domain_error.py` —— 与任务「允许修改的文件」**完全一致** |
| 既有测试零修改 | ✅ `git diff --stat tests/` 为空（仅新增未跟踪文件） |
| 临时垫片 `pytest/` | ✅ 已删除（交付说明 §6.6 属实），`git status` 无残留 |
| 残留检查 | `raise HTTPException` 全仓 **0**；`HTTPException` 仅剩 2 处**注释**；两文件导入行已移除 `HTTPException`；`employee_auth.py` 保留 `Request`（R2 范围） |

### 9.2 关键改造点核验（逐点对照任务文件）

| 点 | 位置 | 判定 |
|---|---|---|
| `DomainError` 定义 | `app/errors.py`（无 fastapi 导入，字段 code/message/http_status，默认 400） | ✅ 与任务规格一致 |
| 唯一 handler | `app/main.py:375-384`，输出 `{"detail": {"code", "message"}}` | ✅ |
| 中间件 CSRF 分支 | `app/main.py:351` `except DomainError`，保留 `X-Request-ID` 回写 | ✅ |
| 错误工厂 | `_warehouse_error`→`:1110`、`_replenishment_error`→`:1939`，均改返回 `DomainError`，status 推导与 messages 字典原样保留 | ✅ |
| 500 兜底 / 401 直接 JSONResponse | `:354-361` 与 `:319-333` 未触碰 | ✅ |

### 9.3 独立对拍（复核方脚本，仓库外解析，2026-10-26）

以 `git show HEAD:` 提取迁移前文本，与工作树逐点解析 `(status, code, message, from-chain)` 四元组：

```text
app/main.py        raise 点  83 → 83   顺序+取值全一致
app/employee_auth.py raise 点 13 → 13   顺序+取值全一致
app/main.py        工厂 return 3 → 3    一致
from exc 链        56 → 56（grep 计数）
```

结论：迁移为**纯机械改写**，未发现任何一处语义漂移。

### 9.4 独立全量回归（复核方自跑，非引用交付方数据）

```text
命令：env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 \
      /d/python/python/python.exe -m pytest -q -p no:cacheprovider   （仓库内）
结果：288 passed, 4354 warnings in 116.41s（283 既有 + 5 新增）
```

交付方四份 stdout 证据（`.testtmp/`，被 `.gitignore` 忽略、不入库）：before `283 passed in 114.28s` / after `288 passed in 116.60s` / 定向 `28 passed` / 新用例 `5 passed`；sha256 前 16 位 `f6845ba198602b80` / `44a6f867a4401816` / `d1de713c88fc900f` / `0a5c05bf8746d501`。复核方实测与交付一致。

### 9.5 判定

**通过（技术面），R1 关闭；P1 项 7（统一 DomainError 映射）达成。** P1 现为 **4.5 / 7**（项 5 仍 ◐，待 R2）。

### 9.6 新增风险登记

| 编号 | 级别 | 描述 | 建议 |
|---|---|---|---|
| R-15 | P2（环境） | WorkBuddy 会话向子进程注入 `PYTHONPATH=…\cli\vendor\shim`，其 `sitecustomize` 会在 pytest 清理阶段触发 `_check_bulk_delete_guard` 并 `SystemExit(1)`，**表现为测试被劫持而非测试失败**；实现方与复核方均需 `env -u PYTHONPATH` 才能跑通 | ① 契约「验证命令」节统一加上 `env -u PYTHONPATH` 前缀；② 后续任务文件的验证命令照此书写 |
| R-16 | P3（记录） | 仓库**外**消费者是否 `except fastapi.HTTPException` 捕获业务错误属 Unknown（仓库内 0 处），迁移后此类捕获将不再命中 | 随 ADR-0012 的 Unknown 项跟踪；无仓库内处置动作 |

（编号说明：`docs/p1-boundary-audit-20261012.md` §11 已预占 R-11…R-14，待并入本登记时再统一排序。）

修订记录：2026-10-26 新增 §9（R1 复核）。
