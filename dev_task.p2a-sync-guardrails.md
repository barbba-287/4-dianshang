# 任务：P2-0a 同步护栏（热开关 + 双预算 + 可取消）

- 上位文档：`docs/p2-canary-readiness-gap-20261026.md`（P0 缺口 1/2 + 热开关提前的用户裁定 2026-10-26）
- 用户裁定原文：「P2-0a 开，热开关+双预算+可取消」「热开关提前，P2-0 升为上线前必做」
- 交付契约：`docs/claude-handoff-contract.md` §3.2 / §4.1 / §4.3；验证命令带 `env -u PYTHONPATH`（R-15）

---

## 1. 目标

给 `/api/external/shopify/sync`（当前最坏可跑 100+ 分钟、不可中止）加三道护栏，**不改变正常路径的同步结果**：

1. **热开关**：live 模式运行期可关（不必改 `.env` 重启）；
2. **双预算**：run 级 wall-clock 预算（新增）+ 既有页数预算的显式留痕；
3. **可取消**：运行中的 sync 可从另一连接请求取消。

**术语对齐（"双预算"的定义）**：(a) **时间预算** = 单次 sync run 的 wall-clock 上限，超限即终止；
(b) **页数预算** = 已存在的 `shopify_max_pages`（每资源 100 页），本次不做逻辑改动，但要求把**实际消耗页数**
写入 run 的 resource_status_json，使预算执行可审计。若此定义与用户意图不符，交付说明中单列说明。

---

## 2. 已核实的现状事实（含实测方法；不要重复调研）

### 2.1 分页循环 = 唯一的执行长循环（`app/platform_adapters/shopify.py:112-130`）

```python
    def _pages(self, query: str, data_key: str, variables: dict | None = None) -> list[dict]:
        cursor = None
        seen: set[str] = set()
        records: list[dict] = []
        for _ in range(self.max_pages):
            request_id = f"shopify-{uuid.uuid4().hex[:12]}"
            response = self._transport.query(query=query, variables={...}, request_id=request_id)
            ...
            if not page_info.get("hasNextPage"):
                return records
            ...
        raise AdapterError("SHOPIFY_PAGE_LIMIT", "Shopify 分页超过上限")   # :130
```

- `for _ in range(self.max_pages)` 是页数预算的强制点；**每页之间**是时间/取消检查的插入点。
- `AdapterError`（同文件已定义）带 `(code, message)`，是预算/取消错误的**同类先例**。
- `__init__`（:105-110）签名：`(self, transport, *, live_enabled=False, simulated=True, page_size=50, max_pages=100)`。
- 单请求超时在 transport 层（`shopify_request_timeout_seconds=20`），**不是** run 级预算。

### 2.2 sync 路由（`app/main.py:1750-1855`）

- `:1754` live 配置判定：`if not (settings.shopify_live_enabled and settings.shopify_store_domain and settings.shopify_access_token): raise DomainError(SHOPIFY_LIVE_NOT_CONFIGURED, 409)`
- `:1764` adapter 构造：`ShopifyReadOnlyAdapter(transport, live_enabled=True, simulated=False, page_size=..., max_pages=...)`
- `:1770` `run = begin_sync_run(db, ...)`（external_sync.py:212-250，status="running"）
- `:1775` `for resource in selected:` → `mark_resource_running` → `adapter.fetch_orders()/fetch_inventory()` → ingest → `mark_resource_succeeded` / `except: mark_resource_failed` → `finalize_resource_run`
- **预算/取消异常会自然落入 `except` → `mark_resource_failed(db, run, resource, exc)`**，无需新状态机。

### 2.3 live 读取点（热开关的作用面，全仓仅 3 处判定）

| 位置 | 内容 |
|---|---|
| `app/main.py:1730` | capabilities 响应的 `live_enabled` / `simulated` 字段 |
| `app/main.py:1739` | preview 路由的 live 判定 |
| `app/main.py:1754` | sync 路由的 live 判定 |

`app/config.py:64-71`：`shopify_live_enabled=False`、`shopify_request_timeout_seconds=20`、`shopify_max_retries=2`、`shopify_max_page_size=50`、`shopify_max_pages=100`。

### 2.4 取消机制的既有参照（`app/background.py`）

- 模块级 `_dispatch_stop: threading.Event`（:43）、`request_cancel` / `is_cancel_requested`（DB 级）+ future cancel。
- 本项目**单进程**（worker 用 threading，无 Redis）。**内存态取消标记是既有假设下的合法方案**。

### 2.5 run 状态机（`app/external_sync.py`）

- `calculate_overall_status`（:167-186）取值集：`running / partial / failed / succeeded`。
- `finalize_resource_run`（:188-210）写 `run.status / finished_at / error_code[:64] / error_message[:500]`。
- resource 级状态存于 `resource_status_json`（`_resource_entry` :34，含 status/execution 字段）。
- **本次不新增 run 终态**：取消的 run 记为 `failed` + `error_code="SHOPIFY_SYNC_CANCELLED"`，避免动
  DB schema 与响应 schema（`ExternalSyncRunResponse` 不改）。

---

## 3. 本次范围

### 允许新建（仅 2 个）

- `app/runtime_flags.py` — 进程内运行期开关（热开关的存储与读取）
- `tests/test_p2a_sync_guardrails.py` — 本切片测试

### 允许修改

- `app/platform_adapters/shopify.py` — `_pages()` 加预算/取消检查；`__init__` 加可选参数
- `app/main.py` — sync/preview/capabilities 三处 live 判定接入热开关；sync 路由组装预算与取消 token；新增 2 个管理路由（见 §4）
- `app/config.py` — 新增 `shopify_sync_budget_seconds: int = 300`
- `app/external_sync.py` — **仅当**需要在 resource_status_json 写入页数/预算信息时（`_resource_entry`/`mark_resource_*` 附近）
- `.env.example` — 登记新配置键

### 禁止改动

- `app/db.py`（**不加列、不改 schema**）
- `app/schemas.py`（响应模型不改）
- `app/background.py`（取消机制独立实现，不复用 worker 的 DB 级取消）
- 任何既有测试文件；`tests/conftest.py`
- `dev_task.*.md` / `docs/`（文档由复核方维护）

---

## 4. 实现规格

### 4.1 `app/runtime_flags.py`（新建，≤40 行）

```python
"""进程内运行期开关。单进程假设与 app/background.py 相同；重启后回落到 .env 配置值。"""
import threading

_live_enabled = threading.Event()
_live_enabled.set()          # 默认与 settings.shopify_live_enabled 一致（不额外拦截）

def set_live_enabled(value: bool) -> None: ...
def live_enabled() -> bool: ...
```

### 4.2 热开关接入（main.py 三处判定 + 1 个管理路由）

- 三处判定改为 `settings.shopify_live_enabled and runtime_flags.live_enabled() and ...`
  （:1730 的 capabilities 报告同时把热开关状态并入 `live_enabled` 字段）。
- 新增 `POST /api/admin/live-switch`：`require_principal(request, roles=(ROLE_ADMIN,))`，body
  `{"live_enabled": bool}`（复用现有 BaseSchema 风格或直接 dict 校验），成功返回
  `{"live_enabled": <bool>, "source": "runtime_flag"}`。切换时打印日志。**不落库**。

### 4.3 双预算

- config 新增 `shopify_sync_budget_seconds: int = 300`（`.env.example` 登记，含注释）。
- sync 路由在构造 adapter 前：`deadline = time.monotonic() + settings.shopify_sync_budget_seconds`。
- `ShopifyReadOnlyAdapter.__init__` 增加可选参数 `deadline: float | None = None`、
  `cancel_check: Callable[[], bool] | None = None`（默认 None，**既有调用方零改动**）。
- `_pages()` 循环体**第一行**检查（每页一次）：
  ```python
  if self._cancel_check is not None and self._cancel_check():
      raise AdapterError("SHOPIFY_SYNC_CANCELLED", "同步已被请求取消")
  if self._deadline is not None and time.monotonic() > self._deadline:
      raise AdapterError("SHOPIFY_SYNC_BUDGET_EXCEEDED", "同步超出 wall-clock 预算")
  ```
- 页数留痕：sync 路由在 `mark_resource_failed` / `mark_resource_succeeded` 之外，把
  `pages_used`（本次资源实际翻页数，可让 `_pages` 返回或经实例属性暴露）与
  `budget_seconds` 写入 resource_status_json（经 `_resource_entry` 或 mark_* 的 metadata 扩展）。

### 4.4 可取消

- 新增 `POST /api/external/sync/runs/{run_id}/cancel`：`require_principal(request, permission="inventory.write")`。
- 内存标记：`app/runtime_flags.py` 或 main.py 模块级 `_sync_cancel_tokens: dict[str, threading.Event]`。
  sync 路由 begin_sync_run 后注册；finalize 后注销（防泄漏）。
- 取消语义：对**不在运行**的 run_id 返回 404 或 409（说明清楚选哪种并写测试）；对运行中的 run，
  返回 202 `{"run_id": ..., "cancel_requested": true}`，实际终止由下一个分页检查点完成。
- 取消后 run 的终态：`failed` + `error_code="SHOPIFY_SYNC_CANCELLED"`（经既有 except 路径自然达成）。

### 4.5 正常路径不变式

- `deadline=None, cancel_check=None` 时 `_pages()` 行为与现状**逐字节等价**（fixture 既有测试不感知）。
- `ShopifyFixtureTransport` 路径（simulated）完全不受影响。

---

## 5. 测试规格（`tests/test_p2a_sync_guardrails.py`，≥7 条）

参考 `tests/test_identity.py:362-417` 的 reload 序列与 `tests/test_api_inventory.py` 的构造方式。

| # | 用例 | 断言要点 |
|---|---|---|
| T1 | 预算超限终止 | transport 每页强制 sleep 或极小 budget（如 0）+ 多页 fixture → sync 返回 200 且 resource error_code=`SHOPIFY_SYNC_BUDGET_EXCEEDED`，run.status=failed，**已取到的页不写入**或如实说明 |
| T2 | 页数预算仍生效 | max_pages=2 + 持续 hasNextPage fixture → `SHOPIFY_PAGE_LIMIT`（既有行为回归） |
| T3 | 取消终止 | 发起 sync 前预注册 cancel（或另线程置位）→ error_code=`SHOPIFY_SYNC_CANCELLED` |
| T4 | 取消 API 对不存在/已结束 run | 404 或 409（二选一，测试固定） |
| T5 | 热开关关闭后 sync 拒绝 | admin 调 live-switch false → sync 返回 409 SHOPIFY_LIVE_NOT_CONFIGURED；capabilities 报 `live_enabled: false` |
| T6 | 热开关需要 admin | 非 admin principal 调 live-switch → 403 |
| T7 | 正常路径不受影响 | deadline/cancel_check 缺省时 fixture sync 全流程成功（回归护栏） |

每条必须"改坏产品代码会失败"（可判别性）；**禁止宽枚举断言**（R-20 教训：涉及控制流的行为先跑探针再写规格）。

---

## 6. 已排除的选项

1. **run 新增 `cancelled` 终态** — 需动 db.py/schema.py/状态机，超出"护栏"切片体量。
2. **取消落 DB（复用 background 的 request_cancel）** — sync 不是 job，无 job_id；DB 级取消留 P2-0b。
3. **热开关落库/落 .env** — 运行期开关落库引入一致性问题；重启回落 env 是安全默认。
4. **预算检查放 transport 层** — transport 是单请求层，run 级 wall-clock 只能放分页循环。
5. **改 ExternalSyncRunResponse** — 契约变更，留给 P2-0b（审计补全）一并做。
6. **为热开关加鉴权外的审计日志表** — 本次只打应用日志。
7. **异步化 sync 路由（改成 202 + job）** — 那是"可停"的架构级方案，超出本切片；预算+取消已满足 Canary 门禁。
8. **动 `ShopifyLiveTransport` 的单请求超时/重试** — 与 run 预算是两个层次，保持不动。

---

## 7. 验证命令

```bash
cd /d/ai/dianshang && \
env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 \
/d/python/python/python.exe -m pytest -q tests/test_p2a_sync_guardrails.py -p no:cacheprovider

cd /d/ai/dianshang && \
env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 \
/d/python/python/python.exe -m pytest -q -p no:cacheprovider
```

全量基线：**320 passed**（268 既有核心 + 24 identity + 8 demo contract + 20 既有 CLI/其他 —— 以实际输出为准）。
⚠️ 已知 flaky（R-19）：3 次约 1 次出现 1 个与 frontend 相关的随机错误，位置不定、单跑通过。
**若出现且单跑该用例通过，重跑一次全量；两次连续同位失败才算本切片回归。** 交付说明必须如实记录每次结果。

---

## 8. 完成标准

1. §5 全部用例定向绿；全量 320 全绿（或按 R-19 规则复跑确认）。
2. `git status --porcelain` 仅出现 §3 允许的新建/修改文件；`git diff --stat` 与允许清单一致。
3. 既有调用方零破坏：`ShopifyReadOnlyAdapter(` 的既有构造点（main.py:1764/1739 附近与全部测试）行为不变。
4. 按 §4.1 输出交付说明并**落盘** `.testtmp/delivery.md`（附两次验证命令完整原始 stdout）。

---

## 9. 交付要求

| 字段 | 要求 |
|---|---|
| 变更文件清单 | 与 §3 逐一对映，附 `git status --porcelain` 原始输出 |
| 关键决策 | 热开关存储位置、取消 token 生命周期（注册/注销点）、页数留痕的落点 |
| 验证证据 | 定向 + 全量完整原始 stdout（同时写入 `.testtmp/delivery.md`） |
| 真实/模拟/未知 | 测试真实执行=真实；未调真实 Shopify API=事实；未知单列 |
| 已知限制 | 单进程假设（多 worker 部署时内存开关/取消失效）；重启后热开关回落 |

---

## 10. 复工说明（2026-10-26 14:30 复核方追加，第一次实施进程 R-17 消失）

第一次实施进程只落盘了 1 个文件后无声消失（后台任务 `6ag60f` 已不存在，且未写交付说明）。

### 10.1 已完成（经复核方读过全文，视为合格，保留不动）

- `app/runtime_flags.py`（27 行）：`threading.Event` 存储运行期 live 开关，
  `set_live_enabled()` / `live_enabled()`，默认放行（不额外拦截，真正放行与否仍由
  `settings.shopify_live_enabled` 决定）。**本文件不需要改动。**

### 10.2 尚未开始（本此复工的全部剩余范围）

1. `app/config.py`：新增 `shopify_sync_budget_seconds`（默认 300）。
2. `app/main.py`：`POST /api/admin/live-switch`（过 `require_principal`，body 为
   `{"live_enabled": bool}`，调 `runtime_flags.set_live_enabled`）。
3. 同步服务（`app/services/shopify_sync.py` 或实际所在模块）：
   - run 级 wall-clock 预算：每页循环间检查 elapsed，超预算即置
     `failed + error_code=SHOPIFY_SYNC_BUDGET_EXCEEDED`；
   - 取消 token（进程内 dict：run_id -> Event，run 结束注销）；检查点与预算相同（页间）；
     触发置 `failed + error_code=SHOPIFY_SYNC_CANCELLED`。
4. `app/main.py`：`POST /api/external/sync/runs/{run_id}/cancel`（取消 token 置位；
   run 不存在或已终态返回 404/409，语义自行判断并在交付说明写明）。
5. `tests/test_p2a_sync_guardrails.py`：§5 全部用例。

### 10.3 复工硬要求

- 不得重写或删除 `app/runtime_flags.py`（已完成物，改动需在交付说明声明理由）。
- 其余约束（§1 边界、§3 允许清单、§6 排除项、§7 验证命令、§8 完成标准）全部不变。
- **完成即把交付说明写到 `.testtmp/delivery.md`（覆盖旧内容），落盘，不要依赖进程 stdout 存活**
  —— 前两次实施进程均在写完代码后无声消失，落盘文件是复核的唯一入口。

---

## 11. 复核结论（2026-10-26 18:40，WorkBuddy）

**判定：通过（实现 + 测试均达标，全量 330 passed / 0 failed）。**

### 11.1 实施过程：R-17 根因查明（R-21）

本切片经历 6 次实施进程中断（1 次只写 1 文件、1 次零产出、2 次写一半、2 次写完未落盘说明）。
根因：Claude Code CLI 在 Windows 内部调用 `reg.exe`，被 WorkBuddy 安全中心程序黑名单 SIGTERM，
关沙箱也绕不开；用户移除黑名单后，长会话仍偶发被杀但已能产出。**处置经验：分块小任务 +
每块落盘产物**，死一块只损失一块。另：`reg.exe` 放行后仍有间歇性 SIGTERM（未定位，登记 R-22）。

### 11.2 结论正确性复核（我独立执行）

| 项 | 方法 | 结果 |
|---|---|---|
| 语法 | `py_compile` 5 文件 | 通过 |
| 路由注册 | 导入冒烟（`/api/admin/live-switch`、`/api/external/sync/runs/{run_id}/cancel`、`/api/external/shopify/sync`） | 3/3 True |
| 预算默认 | `Settings().shopify_sync_budget_seconds` | 300 |
| 热开关默认 | `runtime_flags.live_enabled()` | True（只提供"关"能力，真放行仍看 settings） |
| 定向测试 | `tests/test_p2a_sync_guardrails.py` | 10 passed（6.56s） |
| 全量 | `pytest -q`（含 R-19 规则：首跑 319+1 flaky → 单跑过 → 复跑 320 全绿） | **330 passed / 0 failed**（125.87s） |
| 既有构造点 | `ShopifyReadOnlyAdapter(` 三处，仅 sync 传新参数 | 默认行为不变（T7b 覆盖） |

### 11.3 复核发现并已修正的问题（1 处）

测试文件三处 `monkeypatch.setenv("SHOPIFY_SYNC_BUDGET_SECONDS"/"SHOPIFY_MAX_PAGES")` 后缺
`get_settings.cache_clear()`，缓存导致预算不生效、跑满 100 页报 PAGE_LIMIT（3 红，245s）。
修法：三处各补一行 `cfg.get_settings.cache_clear()`，断言零改动 → 10 绿、6.56s。

### 11.4 清理

删除实施进程遗留的临时探针 `tests/test_zz_probe_p2a.py`（自述"非交付文件，跑完即删"，
8 用例中 4 红且失败均为探针自身问题，与产品无关；正式测试文件已覆盖同样行为）。

### 11.5 残留风险（登记，不阻塞）

- **R-22**：`reg.exe` 放行后 claude CLI 长会话仍偶发 SIGTERM（无拦截提示、位置随机）。缓解：分块 + 落盘产物。
- **单进程假设**：热开关与取消 token 均为进程内存态，多 worker/多副本部署时失效（交付说明已列为已知限制）。
- **取消延迟**：取消在下一页/下一资源检查点生效，当前页 transport 等待不可中断（P0 第 2 项"同步阻塞不可取消"仅部分缓解：可从"跑完才停"提升为"页间可停"）。
- **页数留痕字段**：写入 `resource_status_json`（`pages_used`/`budget_seconds`/`max_pages`），未改 DB schema。
