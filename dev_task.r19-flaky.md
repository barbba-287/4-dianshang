# R-19 立项：全量 flaky 根因定位与隔离（时间盒 1 个工作日）

> 立项：2026-10-27（WorkBuddy），依据用户裁定「单独立项、给时间盒、定位不了就隔离」。
> 优先级：**P1 残留三件（S4→R6→R4）之后、P2 主体之前**。时间盒 **1 个工作日**，超时即转隔离方案，不无限追。

## 1. 问题陈述（Fact）

| # | 事实 | 证据 |
|---|---|---|
| F1 | 全量约 1/3 概率出现 1 个与 frontend 相关的失败/错误，位置不定 | 2026-10-12 起多次复现；10-26 `test_frontend_orders` FAILED、另一轮 `test_frontend_warehouse` ERROR、10-27 又 `test_frontend_orders` FAILED |
| F2 | 失败用例**单跑必过** | 多次单跑验证（1.2s 通过） |
| F3 | 复跑全量通常转绿 | 每次收口都按「重跑一次全绿即过 + 登记」 |
| F4 | 失败位置在 `test_frontend_*` 之间随机漂移 | orders / warehouse 轮换 |

## 2. 假设清单（Hypothesis，按可能性排序）

| # | 假设 | 支持证据 | 反证/待验 |
|---|---|---|---|
| H1 | **全局模块 reload 竞态**：`test_frontend_orders.py:8-15` 用 `importlib.reload` 重载 `cfg/db/bg/main` 四个模块，与并行/先后用例共享的模块级状态冲突 | 该用例手工 reload 了 4 个全局模块，且 `main_mod.app` 是模块级单例 | 需看是否有 `-p xdist`；若是串行，需解释跨用例影响 |
| H2 | **后台 executor 残留**：`bg_mod.reset_executor()` 在用例末尾，若断言先失败则 `reset_executor` 不执行 → 线程/连接泄漏到后续用例 | 末尾清理不在 `finally` 中 | 需复现失败后检查线程数 |
| H3 | **SQLite WAL 文件锁**：`tests/_wal_bootstrap/sitecustomize.py` + tmp_path 下多库并发 | 已知提速改造 | 失败时应为 OperationalError（`test_zz_probe` 曾出现同类） |
| H4 | **TestClient 生命周期**：`with TestClient(...)` 退出时 shutdown 未完成即进入下一用例 | 上下文管理器应已 wait | 待验证 |

## 3. 时间盒内要做的（Task）

| 步 | 内容 | 产出 |
|---|---|---|
| T1 | 连续跑全量 **5 次**，记录每次失败用例名与异常类型（`--tb=short` 存档） | `.testtmp/r19-runs.txt` 五次结果表 |
| T2 | 单独跑 `tests/test_frontend_*.py` **20 次**循环，判定是否仅在这批文件内互扰 | 是否可缩小到文件集合的结论 |
| T3 | 验证 H1：把 `test_frontend_orders` 的 reload 段改为 `monkeypatch` + 仅在必要时 reload，观察 flaky 是否消失 | 差分实验结论 |
| T4 | 验证 H2：把清理移入 `try/finally`，看是否消失 | 同上 |
| T5 | 若 T1–T4 定位不到：收集 2 次失败现场的线程栈（`faulthandler.dump_traceback_later`） | 至少一份跨用例泄漏的现场证据 |

## 4. 隔离方案（时间盒到期即执行，Fact 优先于完美）

1. 把 `tests/test_frontend_*.py` **拆到单独 pytest 调用**（`pytest -q tests -k "not frontend"` + `pytest -q tests/test_frontend_*` 两次），
   全量绿的定义改为"两段都绿"；CI/复核脚本同步改。
2. 不改断言语义、不 `skip`（skip 会掩盖真实回归）。
3. 隔离落地后在 `docs/p2-canary-readiness-gap-20261026.md` 或独立风险表登记"frontend 用例需独立进程执行（R-19 未定位）"。

## 5. 边界

- **允许**改：`tests/test_frontend_*.py`（仅清理/隔离段，断言不变）、`tests/conftest.py`（如需 fixture 兜底）。
- **禁止**改：`app/` 任何文件（flaky 不得通过改产品代码"修"）。
- 一次提交，提交信息注明是否定位到根因。

## 6. 完成标准

- 时间盒内定位到根因 → 修掉 + 全量连跑 5 次全绿（记录）。
- 未定位 → 隔离方案落地 + 两段式全量连跑 5 次全绿（记录）+ 残留登记。
- 两种结局都必须落盘 `.testtmp/delivery.r19.md`（5 次结果表 + 结论 + 证据路径）。
