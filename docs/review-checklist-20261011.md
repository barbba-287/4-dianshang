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
