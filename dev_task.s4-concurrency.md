# S4 任务文件：live 同步单飞闸门（并发互斥）

> 创建：2026-10-27（WorkBuddy）。用户裁定：**S4 点头开工**，明确两条硬约束 ——
> ① **锁释放放 `finally`**；② **409 走 R1 错误体**（`DomainError` → `{"detail":{"code","message"}}`）。
> 上级总纲：`dev_task.p1-residuals-s4-r6-r4.md` §1（本文件细化并可独立执行）。
> 基线：`a115107`（339 passed / 0 failed）。live 开关 `SHOPIFY_LIVE_ENABLED` 保持 `false`。

## 0. 一句话目标

live 同步同一时刻**只允许一个 run 在跑**；第二个请求立即 `409 SHOPIFY_SYNC_IN_PROGRESS`，
不排队、不阻塞；任何退出路径都必须释放锁。

---

## 1. 现状（Fact，均已实测，附方法）

| # | 事实 | 位置 | 实测方法 |
|---|---|---|---|
| F1 | `DomainError(code, message, http_status=...)` 是唯一业务异常；`main.py` 有唯一处理器把它渲染成 `{"detail":{"code","message"}}` | `app/errors.py:12-23`；`app/main.py:381-390` | `Read app/errors.py` 全文；`grep -n "exception_handler(DomainError)" -A 10 app/main.py` |
| F2 | 进程内已有两把锁：`_sync_cancel_lock`（保护 cancel token dict）、`_executor_lock`（在 `background.py`） | `app/main.py:1789`；`app/background.py:42` | `grep -n "_sync_cancel_lock\|_executor_lock" app/ -r` |
| F3 | **没有任何"同时只允许一个 live run"的闸门**：`_shopify_sync_impl` 全程无互斥 | `app/main.py:1842-1993` | 从 `def _shopify_sync_impl` 逐行读到函数尾（R-20：不得中段采样），确认无 `Lock`/`Semaphore`/`acquire` |
| F4 | 同步路由是**壳 + impl** 结构，壳已有 `try/finally`（用于注销 cancel token） | `app/main.py:1996-2005` | `Read app/main.py:1996-2006` |
| F5 | `begin_sync_run` **自己会 `db.commit()`**，但它是 impl 里第 5 步左右才调用（前面还有 `require_principal` / live 配置校验 / 资源名校验，都会抛 `DomainError`，不落库） | `app/external_sync.py:227-263`；`app/main.py:1848-1868` | `grep -n "def begin_sync_run" -A 40 app/external_sync.py`；确认 1848→1868 之间的抛错点 |
| F6 | 取消路由 `cancel_external_sync_run` 是**独立路由**，不经过 sync 入口 | `app/main.py:2008` | `Read app/main.py:2008-2035` |
| F7 | 测试 harness 已存在且可复用：`tests/test_p2a_sync_guardrails.py::harness`（admin 已登录、live 配置齐备、`FakeShopifyTransport` 顶替真实网络、`page_hook` 在**请求线程内**回调，可做确定性同步） | `tests/test_p2a_sync_guardrails.py:132-200`、`:92-120` | `Read` 上述行段 |

## 2. 目标设计（方案 A：单飞闸门）

### 2.1 选型（用户已认可 A，勿再讨论 B）

**A：进程内非阻塞 `threading.Lock`，拿不到立即 409。**
理由：live Canary 是人工触发的低频操作，排队只会掩盖"误操作连点"；409 让运维立刻看到
"已经在跑"，并可带出正在跑的 `run_id` 以便取消。

B（信号量 N=1、允许排队）本期不做，在交付说明里写进路线图。

### 2.2 实现位置与形状

新增到 `app/main.py` 的护栏区（紧邻 `_sync_cancel_lock`，约 `:1789` 之后）：

```python
# live 同步单飞闸门：进程内只允许一个 live run 同时执行。
# 用 Lock 而非 Semaphore：本期只有 N=1 语义；将来要放开并发再换 Semaphore(N)。
_sync_gate_lock = threading.Lock()
# 当前持锁的 run_id（仅用于 409 响应体与日志），由持锁线程写入/清空。
_sync_gate_owner: str | None = None
```

**关键：槽位必须在 `begin_sync_run` 之前占用。**
原因（F5）：`begin_sync_run` 内部 `commit()`，一旦先建 run 再抢锁失败，就会留下
**一个永远 running 的孤儿 run**（P2-0a 项 4 已因同类问题把域名校验前移，见
`canonical_store_domain` 前移注释）。因此顺序必须是：

```
1. 抢锁（非阻塞）——失败立刻 409，此时库里什么都没写
2. try:
3.     占槽位（写 _sync_gate_owner=None 占位，run 建立后再更新为 run_id）
4.     ...（原有 impl 全部逻辑）
5. finally:
6.     清槽位 + 释放锁（无论成功/失败/取消/异常/BaseException）
```

### 2.3 锁的释放（用户硬约束 ①）

- **释放必须在 `finally`**，且 `finally` 内先清 `_sync_gate_owner` 再 `release()`，
  两步都在同一个 `finally` 块里、且 `release()` 外面不能再包可能抛错的东西。
- 已持有锁的线程即便在 impl 内抛 `BaseException`（如 `KeyboardInterrupt`、测试里的
  `pytest.fail`），也必须走到 `finally`。
- **不要在 `finally` 里判断"是不是我持的锁"再释放** —— `threading.Lock` 的 `release()`
  对非持有线程会抛 `RuntimeError`，而本路由是同步函数、全程同一线程，简单情形下
  只要保证"抢到才进 try"即可：
  ```python
  acquired = _sync_gate_lock.acquire(blocking=False)
  if not acquired:
      owner = _sync_gate_owner
      raise DomainError(code="SHOPIFY_SYNC_IN_PROGRESS", message=..., http_status=409)
  try:
      ...
  finally:
      _sync_gate_owner = None   # 先清归属
      _sync_gate_lock.release() # 再释放（顺序不可颠倒）
  ```
- **禁止**用 `with _sync_gate_lock:`（会阻塞，违反"不排队"）；**禁止**
  `acquire(timeout=...)`（同样是排队）。

### 2.4 409 走 R1 错误体（用户硬约束 ②）

```python
raise DomainError(
    code="SHOPIFY_SYNC_IN_PROGRESS",
    message="已有 live 同步正在运行，请等待其结束或先调用取消接口",
    http_status=409,
)
```

响应必须逐字节等于既有格式（由 `main.py:381-390` 的处理器统一渲染）：

```json
{"detail": {"code": "SHOPIFY_SYNC_IN_PROGRESS", "message": "已有 live 同步正在运行，请等待其结束或先调用取消接口"}}
```

- **不得**新增专用 `JSONResponse`、**不得**改 `DomainError` 结构、**不得**加自定义
  exception handler。
- `run_id` 属于"附加信息"，而 `DomainError` 只有 `code/message` 两个字符串槽位。
  **本期做法：把在跑的 run_id 拼进 `message` 或只写日志，不扩 `DomainError`**
  （扩结构会动 R1 的逐字节契约，得不偿失）。推荐：`message` 保持稳定文案，
  run_id 只进 `logger.warning`；用例只断言 `code`。

### 2.5 闸门覆盖范围（必须显式声明，写进交付说明并加用例）

| 路径 | 是否受闸门 | 理由 |
|---|---|---|
| `POST /api/external/shopify/sync`（live） | **受** | 唯一会烧上游配额与写留存目录的入口 |
| fixture 同步路径（`source_mode != shopify_live`） | **不受** | 不烧配额、不共享上游限流桶；加闸会误伤定时 fixture 回放。**但**如果实现发现 fixture 路径与本路由是同一函数体，则在 `live 判定之后` 再加闸，确保 fixture 早退前不占锁 |
| `POST /api/external/sync/runs/{run_id}/cancel` | **不受** | 取消必须随时可用，否则闸门会把自己锁死（S4-4） |
| `POST /api/external/sync/runs/{run_id}/retry` | **不受**（本期） | fixture 语义，无上游调用。将来 R4 的 `/resume` **必须受**（它会真调上游），已在总纲 §3 记下 |
| 热开关 `POST /api/admin/live-switch` | **不受** | 关开关是应急动作，任何情况下都不能被同步挡住 |

### 2.6 与热开关的关系（交付说明必答）

- 热开关**关闭时**：`_shopify_sync_impl` 在第 2 步（F5 的 live 配置校验）就抛
  `SHOPIFY_LIVE_NOT_CONFIGURED`。此时**锁已抢到又被 `finally` 释放**，不会残留。
  验收要求：热开关关 → 连续两次请求都应得到 `409 SHOPIFY_LIVE_NOT_CONFIGURED`
  （第二次**不是** `SHOPIFY_SYNC_IN_PROGRESS`），证明第一次确实释放了锁。
- 热开关只在**入口**判定，一旦 run 开跑，中途关开关不影响本次 run（沿用 P2-0a 语义）。

### 2.7 已知残留（必须登记）

- **多 worker / 多副本部署时进程内锁失效**：与 P2-0a 热开关、取消 token 同源限制。
  登记为 R-27（新编号），并在 `docs/shopify-canary-runbook.md` 现有「单进程假设」
  段落追加一句。
- `_sync_gate_owner` 是没有加锁保护的裸全局变量（只在持锁期间读写，天然互斥）；
  若将来改成允许并发（B 方案），必须改为受锁保护的 dict。

## 3. 边界与红线

- **不动**分页、预算、取消、留存语义（P2-0a / P2-0b 已收口的代码一律不改）。
- **不动** `db.py`、`schemas.py`、migration（S4 无 schema 变更）。
- **不新增**错误码以外的对外契约；错误码只有 `SHOPIFY_SYNC_IN_PROGRESS` 一个。
- **不引入**第三方库（无 file lock / redis / 数据库行锁）。
- **不得**用数据库唯一约束或 `SELECT FOR UPDATE` 实现（SQLite 下不可靠，且引入 schema 变更）。

## 4. 用例（4 条，新建 `tests/test_s4_sync_gate.py`）

复用 `tests/test_p2a_sync_guardrails.py` 的 `harness` / `_install_fake_transport` /
`FakeShopifyTransport`（可 `from tests.test_p2a_sync_guardrails import harness`，
若 conftest 导入路径不便，则把 fixture 复制到新文件并在文件头注明"与 p2a 同源，
改动需同步两边"）。

| # | 场景 | 构造方法（确定性，不用 sleep 赌时序） | 期望 |
|---|---|---|---|
| **S4-1** | 第一个 run 运行中，第二个 sync 请求进来 | 第一个请求放到后台线程；`page_hook` 在**第 1 页**回调时 `entered.set()` 然后 `release.wait(10)` 卡住；主线程 `entered.wait(10)` 后再发第二个请求 | 第二个 **409**，`body["detail"]["code"] == "SHOPIFY_SYNC_IN_PROGRESS"`；第一个请求仍能正常跑完（`release.set()` 后 join，状态 200 且 run succeeded） |
| **S4-2** | 第一个 run 成功结束后再请求 | 顺序两次请求（无并发） | 第二次 **200**，且数据库里有两个不同的 run_id、都是成功态 —— 证明锁在 `finally` 释放 |
| **S4-3a** | run 因**预算超限**终止后再请求 | 第一个请求 `SHOPIFY_SYNC_BUDGET_SECONDS=0`（起始即超限）→ 得到 failed run；再发第二个（预算恢复 300） | 第二个 **200**（不是 409）—— 异常路径确实走了 `finally` |
| **S4-3b** | run 因**取消**终止后再请求 | 用 `page_hook` 卡住第 1 页，主线程调 `/cancel`，`release.set()` 让第一页返回后检查点抛 `SHOPIFY_SYNC_CANCELLED`；再发第二个请求 | 第二个 **200** |
| **S4-4** | 并发同一 run 的 cancel | 同 S4-1 的卡住手法，在第一个 run 运行中调 `/cancel` | cancel 返回 **202**（不被闸门拦），随后第一个 run 以 `SHOPIFY_SYNC_CANCELLED` 收尾 |

> 合计 5 个测试函数（S4-1、S4-2、S4-3a、S4-3b、S4-4）。S4-3 拆 a/b 是因为
> "预算终止"与"取消终止"走的是两条不同分支，`finally` 覆盖性必须分别证明。

### 4.1 并发用例的硬性写法要求（R-19 防污染）

- **禁止 `time.sleep` 赌时序**：唯一同步手段是 `threading.Event`（`entered` / `release`），
  所有 `wait()` 必须带超时（≤ 10s），超时即 `pytest.fail("...卡住未进入")`。
- 后台线程必须 `daemon=True`，并在 `finally` 里 `release.set()` + `join(timeout=10)`，
  否则异常时会把后续用例一起拖死（这正是 R-19 的疑似成因之一）。
- 每个用例结束前断言 `main_mod._sync_gate_lock.locked() is False`
  —— 这是锁泄漏的**直接探针**，比"第二个请求成功"更早暴露问题。

## 5. 完成标准

- 新增用例定向全绿：`pytest -q tests/test_s4_sync_gate.py` → 5 passed。
- 全量：**339 + 5 = 344 passed / 0 failed**（R-19 规则照常：允许重跑一次全绿，但必须登记）。
- 既有 `tests/test_p2a_sync_guardrails.py` 10 例必须仍全绿（闸门不改既有语义）。
- `docs/shopify-canary-runbook.md` 的「单进程假设」段落追加一句指向 R-27。
- **交付说明落盘到 `.testtmp/delivery.s4.md`**（R-22 缓解：进程可能无声死亡，
  不依赖 stdout 存活）。必须回答：
  1. 多 worker 残留（R-27）；
  2. 为何选 A 不选 B；
  3. 闸门与 `runtime_flags` 的关系（§2.6 的实测结果）；
  4. 槽位前移到 `begin_sync_run` 之前的理由与证据（孤儿 run 不可产生的验证）；
  5. `finally` 释放的实现位置（行号）与 S4-3a/3b 如何证明覆盖了异常分支。

## 6. 已完成的前置（不用重做）

- R-25 已独立提交并推送：`a115107`（远端已核对一致）。
- R6 的 `"{}"` 语义已在本轮写入总纲 §2.3 与 §3.4（`{}` = 未开始，不是拒绝）。

## 7. 复核结论

（待实施后由 WorkBuddy 填写）
