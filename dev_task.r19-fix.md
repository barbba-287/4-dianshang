# 任务：R-19 根因修复（后台调度线程 reload 泄漏）

> 生成时间：2026-10-27
> 生成方式：只读检查 `tests/conftest.py`、`app/background.py`、`tests/test_demo_path_contract.py`、`tests/test_domain_error.py`，并用 6 个探针脚本实测（结论见 §2）
> 执行角色：实现方
> 阶段归属：P1 残留（R-19 flaky 定位）· 收口切片
> 上位依据：`docs/review-checklist-20261011.md` §14（验收口径定稿 + 根因定位）

---

## 1. 背景与目标

**现状**：全量回归每次都有 1 处失败，异常恒为
`sqlite3.OperationalError: attempt to write a readonly database`，位置每次不同。
已被当作"随机 flaky"（R-19）容忍多轮，并按「重跑一次全绿即过 + 登记」处理。

**2026-10-27 已定位为确定性缺陷**：不是 flaky，是**测试间顺序污染**。

**做完之后多出什么**：R-19 可从风险清单关闭，验收口径回调为「全量 0 failed」，
R6/R4 的验收不再被无谓阻断。

**不做什么**（防范围蔓延）：

- **不碰**任何业务代码（`app/` 下除新增 worker 登记小模块外，一律不改）。
- **不碰** R6 相关代码（`cursor_json`、`external_sync.py` 的 cursor 逻辑）。
- **不改**任何既有测试的断言。
- **不改** `app/background.py` 的调度逻辑（`_dispatch_loop` 一行不动）。
- **不做**"扩大 flaky 清单"这种治标方案。
- **不引入**第三方库。

---

## 2. 现状事实（Fact）🔒

> R-20 强制：每条附实测方法，取证不得中段采样。

| # | 事实 | 位置 / 实测方法 |
|---|---|---|
| F1 | 四条命令构成严格单向顺序依赖，**同一组用例只换顺序结果就从 failed 变 passed** | `pytest test_demo_path_contract.py test_domain_error.py` → 1 failed,13 passed；反向 → 14 passed；各自单跑 → 9 passed / 5 passed |
| F2 | 异常恒定 `readonly database`（不是 `locked`），四次全量四次同型 | `.testtmp/exp-with-s4-{1,2}.txt`、`.testtmp/exp-no-s4-{1,2}.txt` 四份 grep |
| F3 | `ds-dispatcher` 线程自 `test_live_capabilities_not_reachable_anonymously` 之后**一直存活**，探针实测 `存活=1` 直到会话结束 | 探针 `pytest_runtest_logfinish` 打印 `threading.enumerate()` |
| F4 | **单独用 `with TestClient(...)` 时线程正常归零**（`with` 内 1 → 退出后 0） | 手工脚本：`with TestClient(m.app): pass` 前后计数 |
| F5 | **`importlib.reload(bg_mod)` 后，新模块 `_dispatch_stop=None`、`_dispatch_thread=None`，但旧线程仍存活** | 手工脚本实测：`reload 后: 1 \| 新 _dispatch_stop: None` |
| F6 | **新模块的 `stop_background_workers()` 对旧线程完全无效（仍 1）；对旧 `stop_event` 调 `set()` 立即归零（→ 0）** | 同一脚本连续四步实测 |
| F7 | `_dispatch_loop` 的退出条件为 `stop_event.wait(poll_seconds)`，`stop_event` 是**闭包参数**，外部只能通过持有它的变量访问 | `Read app/background.py:84-90` |
| F8 | `stop_background_workers()` 只操作**当前模块**的 `_dispatch_stop` / `_dispatch_thread` | `Read app/background.py:156-185` |
| F9 | 35 处 `reload(bg_mod)`，**0 处**在 reload 前调用 stop | `grep -rn "reload(bg_mod)" tests/ \| wc -l` = 35；`grep -rn -B2 "reload(bg_mod)" tests/ \| grep -c "stop_background\|reset_executor"` = 0 |
| F10 | `tests/conftest.py` 现有 2 个 autouse fixture（`_isolate_env` / `_clear_settings_cache`），**无任何 worker 清理** | `grep -n "autouse" tests/conftest.py` |

**根因链（由 F5+F6+F7+F8 串成）**：

```
reload(bg_mod) → 模块级 _dispatch_thread 重置为 None
                但旧线程对象仍存活（闭包持有旧模块的 stop_event）
   ↓
新模块的 stop_background_workers() 操作新的 _dispatch_stop（None）
   ↓
旧线程永远收不到停止信号 → 一直用旧 engine 扫库
   ↓
旧 tmp_path 被 pytest 清理 → 下一个用例 setenv 到新路径
但旧线程仍写旧路径 → 父目录不存在 → SQLite 以只读打开 → readonly database
```

---

## 3. 目标方案

### 3.1 方案选择

| 方案 | 做法 | 评估 |
|---|---|---|
| **A（选定）** | 新增 `app/_worker_registry.py`，`start_background_workers` 时把 `(thread, stop_event)` 登记到该模块；conftest 在 teardown 遍历登记表逐个 `set()` + `join()` | 唯一能停掉"失去模块引用的旧线程"的办法（F6 已证）。新增文件不随 `reload(background)` 丢失 |
| B | 改 35 处 `reload(bg_mod)`，每处前加 stop | 侵入面大、易漏、违反"不改既有测试"；且新增测试仍会忘 |
| C | 扩大 flaky 清单 / 加 `connect_args timeout` | 治标，不解决线程泄漏；且会让真实缺陷被清单掩盖 |
| D | 导 pytest 插件 hooks 拦截 | conftest 无法拦截测试函数体内的 `reload` 调用 |

**选 A**。理由：F6 是决定性证据——**只有旧 `stop_event.set()` 能停掉旧线程**，而登记表是唯一能在 reload 后仍持有该引用的地方。

### 3.2 改动清单

| 文件 | 性质 | 允许改动范围 |
|---|---|---|
| `app/_worker_registry.py` | **新增** | worker 生命周期登记表：`register(thread, stop_event)` / `reap_all(timeout)` |
| `app/background.py` | **改 2 行** | `start_background_workers()` 里登记一行；`stop_background_workers()` 里做对称登记清理。**`_dispatch_loop` 一行不动** |
| `tests/conftest.py` | **追加** | 一个 autouse fixture，teardown 调 `reap_all()` |
| `tests/test_r19_worker_leak.py` | **新增** | 回归用例，固化"顺序污染不再发生" |

---

## 3b. 前置校验 🔒

| # | 前置条件 | 校验命令 | 期望 | 实测结果 |
|---|---|---|---|---|
| P1 | 前置提交已合入 | `git merge-base --is-ancestor {SHA} HEAD` | OK | `cfd8bf6` OK、`bd2123a` OK |
| P2 | 工作区干净 | `git status --porcelain` | 空 | **非空**：3 个文档改动（上轮 R-19 口径/模板），需先提交 |
| P3 | 基线测试 | 见 §6 | 记录 | **27 passed / 2 errors**（污染本身，符合预期） |
| P4 | 目标符号存在 | `grep -n "def reset_executor\|def stop_background_workers" app/background.py` | 存在 | `187:def reset_executor`、`156:def stop_background_workers` |
| P5 | 无并行进程 | `ps -W \| grep -Ei "pytest\|uvicorn\|claude\|codex\|cursor"` | 无 | 无 |
| P6 | `SHOPIFY_LIVE_ENABLED` | `grep .env` | false | `.env:21 false` |
| P7 | 改动规模 | `git diff --numstat` | 与 §3.2 一致 | 待提交前核对 |

**红线**：任一不满足即停下报告。P2 本轮需先把上轮 3 个文档改动提交（与本次修复无关，分开提）。

---

## 4. 边界与红线 🔒

- **允许改**：`app/_worker_registry.py`（新增）、`app/background.py`（仅 `start`/`stop` 两个函数各 1-2 行）、`tests/conftest.py`（仅追加）、`tests/test_r19_worker_leak.py`（新增）。
- **禁止改**：`app/` 下其他任何文件；`app/background.py` 的 `_dispatch_loop`；任何既有测试的断言；任何 R6 相关代码。
- **数据/契约**：无 schema 变更、无 API 变更、无 migration。

## 4b. 已知限制与残留登记 🔒

| 编号 | 限制/残留 | 影响面 | 本期处置 | 后续归属 |
|---|---|---|---|---|
| R-19 | 本任务修复后关闭 | — | 修复 + 回归用例固化 | — |
| R-29 | 并行 AI CLI 写者不在 P5 覆盖内 | 多界面同开同一仓库 | 已在模板 P5 扩展 `claude\|codex\|cursor` | 已落地 |
| R-30（新增） | `_worker_registry` 只登记 `ds-dispatcher`，若将来新增其他后台线程需同步登记 | 新增后台线程时 | 登记表按 thread name 匹配，非硬编码单例 | 新增线程时同步 |

---

## 5. 用例 🔒

| # | 场景 | 构造 | 期望 |
|---|---|---|---|
| T1 | **回归固化**：污染源在前，受害者在后 | `pytest test_demo_path_contract.py test_domain_error.py` | **全绿**（修复前 1 failed） |
| T2 | 反向顺序 | `pytest test_domain_error.py test_demo_path_contract.py` | 全绿 |
| T3 | 各自单跑 | 两个文件分别跑 | 全绿（防修复引入新问题） |
| T4 | 线程探针断言 | 在 `test_r19_worker_leak.py` 内：跑完一个用 `TestClient` 的用例后，断言无存活 `ds-dispatcher` | 存活数 = 0 |
| T5 | 登记表对称性 | 调用 `start_background_workers()` 两次，再 `reap_all()` | 线程归零，登记表为空 |

---

## 6. 验证命令 🔒

```bash
# 定向（R-15：必须 env -u PYTHONPATH）
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_demo_path_contract.py tests/test_domain_error.py \
  tests/test_r19_worker_leak.py -p no:cacheprovider

# 顺序依赖（修复前必失败，修复后必全绿）
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_demo_path_contract.py tests/test_domain_error.py -p no:cacheprovider
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_domain_error.py tests/test_demo_path_contract.py -p no:cacheprovider

# 全量 × 3（验收要求）
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

PowerShell 等价：`$env:PYTHONPATH=""; $env:PYTHONDONTWRITEBYTECODE="1"; & "D:\python\python\python.exe" -m pytest ...`

---

## 7. 完成标准 🔒

**核心验收（用户裁定）：**

| # | 标准 | 判据 |
|---|---|---|
| A1 | **顺序对两种都绿** | T1（污染者在前）与 T2（在后）**均全绿** |
| A2 | **全量 3 次稳定** | 连跑 3 次全量，**3 次均 0 failed / 0 error** |
| A3 | **回归用例固化** | `tests/test_r19_worker_leak.py` 存在且通过，能在回退时失败 |
| A4 | **delivery** | `.testtmp/delivery.r19fix.md` 落盘，含 §8 五项 |
| A5 | 基线不回退 | 定向基线（`test_p2a_sync_guardrails.py` 10 例 + `test_s4_sync_gate.py` 5 例）全绿 |

**失败的处置（用户裁定）**：

- **修好** → R-19 关闭，验收口径回调为「**全量 0 failed**」。
- **修不好** → 止损转隔离，用「排除清单」机制开 R6，R-19 保持开启并登记
  `docs/review-checklist-20261011.md` §14.3。
- **时间盒：半天**。到点未达成 A1+A2 即止损，不做"再试一次"。

---

## 8. 交付说明 🔒

落盘 `.testtmp/delivery.r19fix.md`，必答：

1. 根因链的**决定性证据**（F5/F6 的实测输出原文）；
2. 为什么选 A 不选 B/C/D，以及 B 的侵入面实测（35 处）；
3. 产品代码改动面（`background.py` 改了哪几行、为什么不能更少）；
4. 全量 3 次的结果与是否达成「0 failed」；
5. 若走到止损：隔离清单与 R-19 状态。

---

## 9. 提交要求 🔒

- 先提交上轮 3 个文档改动（`dev_task.r19-flaky.md` / `dev_task.template.md` /
  `docs/review-checklist-20261011.md`），**与本次修复分开**。
- 本次修复单独一个提交。
- 提交前跑 P7：`git diff --cached --numstat`，核对与 §3.2 一致，删除数非 0 必须解释。

---

## 10. 复核清单

- [x] A1 顺序双向全绿（附两次命令与结果）
- [x] A2 全量 3 次全绿（附三次 tail 输出）
- [x] A3 回归用例存在且能失败（回退验证：临时回退修复看是否变红）
- [x] A4 delivery 五项齐全
- [x] A5 基线 15 例全绿
- [x] 产品代码改动仅限 `background.py` 两个函数各 1-2 行

---

## 11. 执行记录（2026-10-27 完成）

**提交**：`f2e7684` — 4 files changed, 389 insertions(+), 1 deletion(-)

| 文件 | 规模 | 说明 |
|---|---|---|
| `app/_worker_registry.py` | +108 / -0 | 新增，进程级登记表 |
| `app/background.py` | +11 / -0 | 两处：start 登记、stop 反登记 |
| `tests/conftest.py` | +42 / -1 | 追加 autouse fixture；1 处删除为补末尾换行 |
| `tests/test_r19_worker_leak.py` | +228 / -0 | 新增 6 例 |

**A1**
```
pytest tests/test_demo_path_contract.py tests/test_domain_error.py  -> 14 passed
pytest tests/test_domain_error.py tests/test_demo_path_contract.py  -> 14 passed
```
修复前：污染者在前 `1 failed, 13 passed`。

**A2**（`pytest tests/ -q -p no:randomly`）
```
第1次: 350 passed in 139.78s
第2次: 350 passed in 139.96s
第3次: 350 passed in 133.33s
```

**A3** 6 例通过，且两方向回退均变红：

| 回退对象 | 变红用例 | 输出 |
|---|---|---|
| `conftest.py` autouse fixture | `test_a3_stage2_previous_leak_already_reaped` | `1 failed, 5 passed` |
| `background.py` 的 `register()` | `test_no_dispatcher_left_after_testclient_case`、`test_pollution_source_does_not_break_following_test` | `2 failed, 4 passed` |

> A3 第一次做是假绿（原 4 例自己清理，不依赖 fixture，回退后仍 4 passed）。
> 已补一对"自己不清理由 fixture 收口"的用例修正，教训见 checklist §14.8。

**A5**：`test_p2a_sync_guardrails.py` + `test_s4_sync_gate.py` = `15 passed`

**R-29 检查**：提交前 P5 无并发写者、无 `.git/*.lock`。

**结论**：R-19 关闭，验收口径回调为「全量 0 failed」。

