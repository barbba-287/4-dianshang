# 任务

> 生成时间：2026-10-12
> 生成方式：只读检查（`tests/test_cli.py`、`tests/test_alembic.py`、`tests/test_api_tickets.py`、`tests/conftest.py`、`pyproject.toml`、`docs/claude-handoff-contract.md`、`docs/review-checklist-20261011.md`、`git status --short`）+ 仓库外独立探针实测
> 执行角色：本文件只做需求拆解、根因定位与验收定义，代码实现由 Claude Code 负责
> 阶段归属：**测试基础设施缺陷（跨切片）**，不属于 P1 功能切片
> 对接复核：`docs/review-checklist-20261011.md` §6.3（该缺陷的发现与既有定性来源）

## 任务标题

**修复测试子进程的硬编码工作目录 `cwd="D:\\ai\\dianshang"`（5 处 / 2 文件 / 影响 12 个测试函数），使 subprocess 型测试相对于「当前检出树」执行，而非相对于本机固定路径 `D:\ai\dianshang`。**

## 任务目标

1. 消除 5 处硬编码绝对路径，使子进程的工作目录由**测试文件自身位置**推导得出。
2. **不降低任何断言强度**：不得为绕过问题而删改、放宽既有断言。
3. **不改变测试的既有语义**：仍必须是「真实 `subprocess` 调用 + 真实 `app.cli` / 真实 `alembic`」，不得改为进程内直接调用（那会失去该测试存在的意义）。
4. 使该用例在**任何检出**（干净 clone / `git worktree` / `git archive` 副本 / 未来 CI）中都能真实测到**当前检出**的代码。

## 选择理由

| 依据 | 说明 |
|---|---|
| 缺陷使测试在非本仓检出中**静默失真** | 子进程 `cwd` 写死为 `D:\ai\dianshang`，任何其它检出中运行的 `pytest` 实际执行的都是本机那份代码；测试**通过**但不能证明当前检出正确 —— 属"假绿"，比失败更危险 |
| 已被实证：它导致过一次**错误的复核结论** | `docs/review-checklist-20261011.md` §6.3：复核方用 `git archive HEAD` 提取基线树做 A/B 对拍，基线树（`timeout=10`）**本应失败**却报 `1 passed in 31.08s`，正是该硬编码所致；若未察觉，会得出"修复不承重"的反向结论 |
| 挡住 README 的「干净 clone 可复现」承诺 | `README.md` 该节要求"干净 clone 可复现"；只要这 5 处存在，该承诺在测试维度上就不成立 |
| 是后续所有 A/B 与 CI 的前置 | 修复前，任何"换个检出跑一遍"的验证方法都不可信（含本复核流程自身）。属**方法论基础设施** |
| 修复面极小、风险极低 | 只改 5 个 `cwd=` 实参，不触碰任何生产代码、不触碰断言、不触碰 `conftest.py` |

---

## 当前事实

### 缺陷清单（行号为本次会话实测，`grep -n` 结果）

| # | 文件 | 行号 | 所在位置 | cwd 值 |
|---|---|---|---|---|
| 1 | `tests/test_cli.py` | **39** | `_run_cli()` 辅助函数（`:17-40`）的 `subprocess.run(...)` | `"D:\\ai\\dianshang"` |
| 2 | `tests/test_alembic.py` | **34** | `_run_alembic()` 辅助函数（`:21-35`）的 `subprocess.run(...)` | `"D:\\ai\\dianshang"` |
| 3 | `tests/test_alembic.py` | **172** | `test_init_db_py_uses_alembic()` 内联 `subprocess.run` | `"D:\\ai\\dianshang"` |
| 4 | `tests/test_alembic.py` | **203** | `test_init_db_py_on_legacy_db_uses_stamp()` 内联 `subprocess.run` | `"D:\\ai\\dianshang"` |
| 5 | `tests/test_alembic.py` | **234** | `test_init_db_py_on_migrated_db_is_idempotent()` 内联 `subprocess.run` | `"D:\\ai\\dianshang"` |

`tests/test_cli.py` 的行 39 位于文件内**唯一的**子进程辅助函数中，故影响该文件全部 5 个测试函数。

### 受影响测试函数（共 12 个）

| 文件 | 测试函数 | 经由 |
|---|---|---|
| `tests/test_cli.py` | `test_cli_init_db`（`:43`） | `_run_cli`（`cwd` 于 `:39`） |
| `tests/test_cli.py` | `test_cli_status`（`:50`） | 同上 |
| `tests/test_cli.py` | `test_cli_seed_docs_and_query`（`:59`） | 同上 |
| `tests/test_cli.py` | `test_cli_unknown_command`（`:90`） | 同上 |
| `tests/test_cli.py` | `test_cli_query_without_args`（`:96`） | 同上 |
| `tests/test_alembic.py` | `test_alembic_upgrade_head_creates_all_tables`（`:43`） | `_run_alembic`（`cwd` 于 `:34`） |
| `tests/test_alembic.py` | `test_alembic_current_shows_head`（`:94`） | 同上 |
| `tests/test_alembic.py` | `test_alembic_upgrade_repairs_stamped_0017_ticket_table`（`:106`） | 同上 |
| `tests/test_alembic.py` | `test_alembic_stamp_head_marks_existing_db`（`:140`） | 同上 |
| `tests/test_alembic.py` | `test_init_db_py_uses_alembic`（`:158`） | 内联（`cwd` 于 `:172`） |
| `tests/test_alembic.py` | `test_init_db_py_on_legacy_db_uses_stamp`（`:183`） | 内联（`cwd` 于 `:203`） |
| `tests/test_alembic.py` | `test_init_db_py_on_migrated_db_is_idempotent`（`:217`） | 内联（`cwd` 于 `:234`） |

### 决定性证据（规划方仓库外实测，2026-10-12）

**装置**：`git archive HEAD | tar -x -C <仓外副本>` 提取检出树；执行**同一段**子进程代码，只改 `cwd`：

```text
cwd=副本目录（正确写法）          -> C:\Users\董\WorkBuddy\...\.workbuddy\tmp\cwdproof\app\__init__.py
cwd=D:\ai\dianshang（现状硬编码） -> D:\ai\dianshang\app\__init__.py
```

同一段代码、同一台机器，**`cwd` 决定了子进程 import 到哪个 `app` 包**。这是该缺陷的机理本身，无需推断。

**机理**：`python -m app.cli` / `python -m alembic` 会把 `cwd` 放入 `sys.path[0]`，且 `alembic.ini`、`init_db.py` 均按 `cwd` 解析相对路径。因此 `cwd` 写死 = 强制指向 `D:\ai\dianshang`。

**已发生的实际危害（不是假想）**：`docs/review-checklist-20261011.md` §6.3 记载 —— 复核方在 `git archive HEAD` 基线树中运行 `pytest tests/test_cli.py::test_cli_seed_docs_and_query`，结果为 `1 passed in 31.08s`；而基线树中 `app/cli.py` 的等待窗口是 `10s`、实测任务需 12–20s，**基线树本应失败**。该矛盾即由本缺陷造成。

### 正确写法在仓库内已有先例

`tests/test_api_tickets.py:48` 已经采用正确形式：

```python
migration_cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
```

即 `Path(__file__).resolve().parents[1]` = 仓库根。本次修复沿用同一形式即可，无需新发明。

### 不相关的相邻事实（避免实现方误判）

| 项 | 说明 |
|---|---|
| `pyproject.toml:1-4` `pythonpath = ["."]` | 只影响 **pytest 进程自身**的 `sys.path`（相对 rootdir），**不影响** subprocess；不能替代本次修复 |
| `tests/conftest.py` 的 WAL bootstrap（`PYTHONPATH` 注入 `tests/_wal_bootstrap`） | 用的是**由 `__file__` 推导的绝对路径**，与 `cwd` 无关，因此**不受本次修复影响，也无需同步修改** |
| `tests/test_mcp_official.py` / `tests/test_mcp_server.py` | 也起 subprocess（`-m app.mcp_server`），但**未指定 `cwd`**（继承 pytest 调用目录）。属**另一类问题**，见「明确不做的事情」 |
| 仓库无 CI 配置 | `.github/workflows/`、`.gitlab-ci.yml`、`.circleci`、`Jenkinsfile` 均不存在（已核）；当前只有 1 个 worktree。故本缺陷的**现实危害集中在"人工换检出验证"与"未来 CI"** |

### 相关文件（行号为本次会话实测）

| 文件 | 角色 | 关键位置 |
|---|---|---|
| `tests/test_cli.py` | **本次主改造目标** | `:17-40` `_run_cli`（`:39` cwd）；`:43/50/59/90/96` 受影响测试 |
| `tests/test_alembic.py` | **本次主改造目标** | `:21-35` `_run_alembic`（`:34` cwd）；`:158/183/217` 内联 subprocess（`:172/203/234` cwd） |
| `tests/test_api_tickets.py` | **改写形式的参照**（本文件不改） | `:48` `Path(__file__).resolve().parents[1]` |
| `tests/conftest.py` | 经确认无需改动 | `:26-35` PYTHONPATH 注入（绝对路径） |
| `pyproject.toml` | 经确认无需改动 | `:1-4` pytest 配置 |
| `docs/review-checklist-20261011.md` | 缺陷发现与定性来源 | §6.3、§6.11 |

### Fact / Target / Assumption / Unknown

**Fact（本次会话已验证）**

| 编号 | 事实 |
|---|---|
| F1 | 仓库内硬编码 `cwd="D:\ai\dianshang"` 共 **5 处**，分布于 `tests/test_cli.py:39`、`tests/test_alembic.py:34/172/203/234`（`grep` 全仓 `*.py` 结果，除这 5 处无其它） |
| F2 | 该硬编码使子进程 import 到 `D:\ai\dianshang\app`，与测试所在检出无关（已用同一段代码的 `cwd` 对照实验证实，见上） |
| F3 | 受影响测试函数共 **12 个**（5 + 7） |
| F4 | 该缺陷已造成过一次**错误的复核结论**（§6.3 的 A/B 对拍误判），非纯理论风险 |
| F5 | `tests/test_api_tickets.py:48` 已存在正确写法 `Path(__file__).resolve().parents[1]`，可作为改写参照 |
| F6 | `tests` 目录**无 `__init__.py`**（非包） |
| F7 | 仓库当前无任何 CI 配置，仅 1 个 git worktree；工作树 HEAD = `4fcb5dd` |
| F8 | 修复不涉及生产代码：`app/**` 当前无本缺陷相关改动 |

**Target（本次要达成，非既成事实）**

| 编号 | 目标 |
|---|---|
| T1 | 5 处 cwd 全部改为由 `__file__` 推导的仓库根，无硬编码绝对路径残留 |
| T2 | 12 个受影响测试函数全部仍通过，断言强度不变 |
| T3 | 修复具备"承重性"：在**仓外检出**中，子进程确实执行该检出的代码（而非 `D:\ai\dianshang`） |
| T4 | 全量回归无 failed（当前基线 `283 passed / 0 failed`） |

**Assumption（本任务成立的前提）**

| 编号 | 假设 | 若不成立的影响 |
|---|---|---|
| AS1 | `tests/test_cli.py` / `tests/test_alembic.py` 两文件均直接位于 `<repo>/tests/`，故 `parents[1]` 即仓库根 | 若不成立需调整层级；已核对文件路径，成立 |
| AS2 | `alembic.ini`、`init_db.py`、`fixtures/products.html` 均位于仓库根 | 已核实（`ls` 通过）；若缺失则本任务失败，但它们是 `HEAD` 跟踪文件 |
| AS3 | 无其它测试依赖"cwd 必须是 `D:\ai\dianshang`"这一隐式前提 | 若存在，将因本次修复而失败 —— **这正是需要回归覆盖的**（见完成标准） |

**Unknown（本任务未验证）**

| 编号 | 未知项 | 原因 |
|---|---|---|
| U1 | 未来 CI 会以何目录为 rootdir | 当前无 CI 配置；本任务只保证"相对检出"，不预设 CI 形态 |
| U2 | `tests/test_mcp_*.py` 未固定 cwd 是否会在特定调用方式下失败 | 属相邻问题，本次不处理（见「明确不做的事情」）；**不影响本任务** |
| U3 | 是否存在其它形式的机器相关硬编码（如绝对路径字面量） | 本次仅按 `cwd=` 与路径字面量两种模式检索；未做全量静态扫描 |

---

## 本次范围

### 允许修改的文件

| 文件 | 允许的改动 |
|---|---|
| `tests/test_cli.py` | 仅把 `_run_cli` 的 `cwd` 实参改为由 `__file__` 推导；可新增文件级常量 |
| `tests/test_alembic.py` | 仅把 `_run_alembic` 与 3 处内联 `subprocess.run` 的 `cwd` 实参改为由 `__file__` 推导；可新增文件级常量；必要的新增 `import`（`from pathlib import Path`） |

**建议改法**（不强制，但推荐照此以减少返工）：

```python
# tests/test_cli.py / tests/test_alembic.py 文件顶部（模块级）
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
```

随后把 5 处 `cwd="D:\\ai\\dianshang"` 改为 `cwd=str(REPO_ROOT)`。

> `tests/test_alembic.py` 已 `import sys`、`import subprocess`、`import tempfile`、`from pathlib import Path`（`:9-12`），仅需在 `_run_alembic` 内联处复用文件级 `REPO_ROOT`。
> `tests/test_cli.py` 已 `import subprocess`、`import sys`（`:11-12`），需补 `from pathlib import Path`。

### 不允许修改的文件

| 文件 | 原因 |
|---|---|
| `app/**`（全部） | 生产代码，本次缺陷不在生产侧；修复后若生产代码需改动，说明定位有误 |
| `app/crawler.py`、`app/background.py` | Playwright 与任务调度，性能问题另立任务 |
| `migrations/`、`alembic.ini`、`init_db.py` | 无 schema 与迁移变更 |
| `tests/conftest.py`、`tests/_wal_bootstrap/**` | WAL bootstrap 用绝对路径注入，与本缺陷无因果，**不得顺手改动** |
| `pyproject.toml` | pytest 配置，本缺陷不通过配置解决 |
| `tests/test_api_tickets.py` | 已是正确写法，无需改；改它属范围外 |
| `tests/test_mcp_official.py`、`tests/test_mcp_server.py` | 相邻但不同的问题，见下 |
| 其余 `tests/test_*.py` | 与本缺陷无关 |
| `CLAUDE.md`、`docs/claude-handoff-contract.md`、`docs/review-checklist-*.md` | 规范/复核产物，不由实现方改动 |
| 本任务文件 | 实现方不得修改任务文件 |

### 明确不做的事情

1. **不为这两处新建共享模块**（如 `tests/_paths.py`）。理由：只需 2 个文件各自一行常量；而 `tests/` **无 `__init__.py`（非包，F6）**，跨文件导入依赖 pytest 的 sys.path 插入行为，引入不必要的耦合面。**就地声明常量即可。**
2. **不修改 `tests/test_mcp_official.py` / `tests/test_mcp_server.py`**。它们同样起 subprocess 但**未指定 `cwd`**（继承 pytest 调用目录），属"未固定 cwd"而非"硬编码错误路径"，失败模式不同（前者会**报错**，后者会**静默假绿**）。如判定需固定，另立小任务。
3. **不把 subprocess 调用改写为进程内调用**（例如直接 `from app.cli import main`）。那会使测试失去"验证真实命令行入口"的意义，属**降低测试价值**。
4. **不放宽、不删除任何既有断言**；不得通过改断言使测试变绿。
5. **不顺手修 Playwright 性能**（全量耗时中的 ~110s），另立任务。
6. **不引入新的测试依赖**（如 `pytest-subprocess`、`rootdir` 插件）。

---

## 实施要求

### 数据流和状态边界

```text
pytest 进程（cwd = 调用者目录，rootdir 由 pyproject testpaths 决定）
  → 测试函数
  → subprocess.run([sys.executable, "-m", "app.cli" | "-m" "alembic" | "init_db.py"],
                   cwd = <本次要改的实参>)          ← 唯一改动点
  → 子进程 sys.path[0] = cwd  →  import app 解析到 <cwd>/app
  → alembic.ini / init_db.py 按 cwd 解析相对路径
```

- 本次只改「子进程在哪个目录启动」，**不改变**调用方式、命令、env 传递、断言。
- 环境变量经 `env=` 显式传递（`tests/test_cli.py:31`）或 `os.environ.copy()`（`tests/test_alembic.py:25`），均为绝对路径或继承值，**不受 cwd 变化影响**。

### Workspace / RBAC 边界

N/A。本次仅涉及测试代码的工作目录参数，不触及 `workspace_id` 解析、HTTP 鉴权或 RLS。**不得引入任何 RBAC 相关改动。**

### 幂等、版本、事务和审计要求

- 无 schema 变更、无迁移、无版本变更。
- 不得新增或改动 `Idempotency-Key` 相关语义。
- 改动不产生运行时副作用（测试专用代码）。

### 失败、重试、取消和人工接管要求

- 本改动**不引入**任何失败处理逻辑；若修复后某测试失败，须**定位原因并报告**，不得以放宽断言或回退 `cwd` 的方式掩盖。
- **特别警示**：若修复后出现新的 failed，最大可能是 AS3 —— 有测试隐式依赖了 `D:\ai\dianshang` 这个 cwd。此时应**如实报告并保留失败**，交复核方判定，而非就地绕过。

---

## 验证命令

```bash
# 定向（两个受影响文件）
cd D:/ai/dianshang && PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_cli.py tests/test_alembic.py -q -p no:cacheprovider

# 残留检查（应为空）
cd D:/ai/dianshang && grep -rn 'cwd="D:' tests/ --include="*.py"

# 全量
cd D:/ai/dianshang && PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

> 基线格式（契约 §4.4）：改动前 `283 passed / 0 failed`（`PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider`，复核方实测 2026-10-12）→ 改动后 `<N'> passed / <M'> failed`。

**仓外承重性验证（本任务的核心验收，必须执行）**：

```bash
# 1) 提取当前 HEAD 到仓外副本（不修改仓库）
COPY="<仓外临时目录>"
cd D:/ai/dianshang && git archive HEAD | tar -x -C "$COPY"

# 2) 把修复后的两个测试文件拷入副本
cp tests/test_cli.py tests/test_alembic.py "$COPY/tests/"

# 3) 【方向 A：证明修复生效】在副本中故意"弄坏" app/cli.py
#    在 "$COPY/app/cli.py" 顶部插入一行：  raise SystemExit(7)
#    （只改副本；仓库内 app/cli.py 绝不改动）

# 4) 从副本目录运行
cd "$COPY" && PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_cli.py::test_cli_init_db -q -p no:cacheprovider
# 期望：FAILED（子进程现在真的跑副本的 cli.py，退出码 7 ≠ 0）
# 若仍 PASS → 修复未生效，子进程仍在执行 D:\ai\dianshang 的代码，必须重做

# 5) 【方向 B：证明修复无害】还原副本中的 app/cli.py，再跑一次
cd "$COPY" && PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_cli.py tests/test_alembic.py -q -p no:cacheprovider
# 期望：全部 PASS
```

> 方向 A 是**判定修复是否承重**的唯一手段。若不做 A，就无法区分"修复生效"与"测试恰好通过"——这正是本缺陷此前骗过复核方的同一手法。

---

## 完成标准

### 1. 正向测试

- `tests/test_cli.py`（5 个用例）与 `tests/test_alembic.py`（7 个用例）全部通过；
- `grep -rn 'cwd="D:' tests/ --include="*.py"` **输出为空**；
- 仓外副本「方向 A」按预期 **FAILED**、方向 B 全部 **PASS**（见「验证命令」）。

### 2. 负向测试

- **必须证明修复后测试仍能捕获真实回归**，即上述「方向 A」：在仓外副本中破坏 `app/cli.py`，测试必须失败。
- 若该构造在实现方环境下无法完成，至少须提供**手工执行的原始命令与完整输出**作为证据，并在交付说明中说明替代手段。
- **不得**以"测试通过"代替负向证据。

### 3. 越权测试

N/A（测试代码改动，无租户边界）。**须在交付说明中显式声明不适用及原因**，不得留空。

### 4. 重复 / 幂等测试

- 连续执行两次定向测试与全量回归，结果数一致（无跨运行的临时目录或状态泄漏）。
- 说明：本次改动本身不产生数据写入，此项主要验证"改动未引入顺序依赖"。

### 5. 故障 / 回放测试

- 覆盖"检出被移动/复制"这一场景，即「验证命令」的方向 A / 方向 B。
- 说明：本任务**不要求**在仓内构造故障；故障构造一律在**仓外副本**中进行。

### 6. 真实、模拟和未知范围的区分

| 分类 | 内容 |
|---|---|
| 真实 | 真实 `subprocess`、真实 `python -m app.cli`、真实 `python -m alembic`、真实 `init_db.py`、真实 SQLite |
| 模拟 | `fixtures/products.html` 为本地静态 fixture；商品与文档数据为模拟数据 |
| 未知 | U1（未来 CI 形态）、U2（MCP 测试的 cwd 策略）、U3（是否存在其它机器相关硬编码） |

### 7. 交付与门禁（`CLAUDE.md`、契约 §4.1 / §4.3）

- 附**原始测试输出**（完整命令 + stdout，含方向 A / 方向 B 两侧）；
- 给出用例编号（T1–T4）→ 测试函数名映射；
- 声明改动文件清单（含 sha256 前 16 位）与"未改动但相邻"的文件（尤其 `app/cli.py`、`tests/conftest.py`、`tests/test_mcp_*.py`）；
- 声明基线：`283 passed / 0 failed` → `<N'> / <M'>`；
- 任一硬门禁（H1–H7）未过，状态只能写"进行中 / 待验收"，不得写"已完成"。

---

## 禁止操作

- `git reset` / `git clean` / `git checkout --` 等破坏性操作；
- 删除或覆盖任何未跟踪文件；
- `git commit` / `git push`（由用户决定提交与分支推进）；
- 任意外部平台写入；
- 修改 `app/**`、`migrations/`、`alembic.ini`、`init_db.py`、`pyproject.toml`、`tests/conftest.py`、`tests/_wal_bootstrap/**`；
- 修改本任务文件、`CLAUDE.md`、`docs/claude-handoff-contract.md`、`docs/review-checklist-*.md`；
- 在仓库内构造故障（如删改 `fixtures/products.html`、临时改 `app/cli.py`）—— 一切故障构造一律在**仓外副本**中进行。

---

## 附：本任务与既有文档的对应关系

| 文档 | 关系 |
|---|---|
| `docs/review-checklist-20261011.md` §6.3 | 该缺陷的发现与定性来源（含"导致复核方 A/B 对拍误判"的记载） |
| `docs/review-checklist-20261011.md` §6.11 | 边界声明："该缺陷修复前，不得据本用例的 pytest 结果宣称**其它环境已验证**"——本任务即解除该边界 |
| `docs/review-checklist-20261011.md` §6.9 R-5 | `--timeout` 未进命令帮助（P3），与本任务无关，**不要顺手修** |
| `README.md` 测试章节 | "干净 clone 可复现"承诺在测试维度上的前置条件 |
| `docs/claude-handoff-contract.md` §3.2 | 本文件遵循的九章节格式 |
