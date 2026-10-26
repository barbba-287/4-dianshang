# 任务

> 生成时间：2026-10-26
> 生成方式：只读检查（`docs/engineering-roadmap.md`、`docs/adr-0012-identity-context-and-error-mapping.md`、`docs/p1-boundary-audit-20261012.md`、`app/main.py`、`app/employee_auth.py`、`app/content_production.py`、`tests/`、`docs/claude-handoff-contract.md`）+ 规划方脚本化盘点（96 处抛错点逐条解析）
> 执行角色：本文件只做需求拆解、边界划定与验收定义，代码实现由 Claude Code 负责
> 阶段归属：**P1 最小补完 · R1 切片**（错误体系统一；R2 身份上下文待本切片验收后另立）
> 上位依据：`docs/adr-0012-identity-context-and-error-mapping.md`（决策 1）、`docs/p1-boundary-audit-20261012.md`（§5）、`CLAUDE.md`、`docs/claude-handoff-contract.md`

## 任务标题

**R1 错误体系统一：新增 `DomainError` 与唯一异常处理器，把全仓 96 处直接 `raise HTTPException` 与 2 个错误工厂（20 处调用）迁移为 `DomainError`，对外 HTTP 契约（状态码 / `{"detail": {"code", "message"}}` 外形 / 错误码词表 / 响应头）逐字节不变。**

## 任务目标

1. 新增 `app/errors.py`，定义 `DomainError`（携带 `code` / `message` / `http_status`），**不依赖 fastapi**。
2. `app/main.py` 注册**唯一**的 `DomainError` exception handler，输出与现状**完全一致**的错误响应。
3. `app/main.py` 与 `app/employee_auth.py` 中所有 `raise HTTPException(...)`（83 + 13 处）及两个错误工厂（`_warehouse_error`、`_replenishment_error`，合计 20 处调用）迁移为 `DomainError`。
4. 中间件 CSRF 分支（`app/main.py:350` 的 `except HTTPException`）改为捕获 `DomainError`。
5. 迁移后 `grep -rn "raise HTTPException" app/` 残留为 **0**，两文件的 fastapi 导入中 `HTTPException` 被移除（`employee_auth.py` 保留 `Request`）。

## 选择理由

- 全仓业务错误**已经**是 `raise HTTPException(status_code=..., detail={"code": ..., "message": ...})` 的事实约定（96 处**全部**带 `code`，无例外），缺的只是把约定形式化为类型 + 单点映射。
- 抛错点**只存在于 2 个文件**（main.py 83、employee_auth.py 13），其余模块为 0 —— 改造面天然收窄。
- `tests/` 中 **0 个文件**引用 `HTTPException`，且既有 283 个用例大量断言 `response.json()["detail"]["code"]` —— 全量回归即为"行为不变"的现成护栏。
- 完成后 P1 项 7（统一 DomainError 映射）关闭，并为 R2（身份上下文统一）扫清 `employee_auth.py` 的 HTTPException 依赖。

## 当前事实

### 抛错点清单（2026-10-26 脚本化实测）

| 项 | 数值 |
|---|---|
| `app/main.py` 直接 `raise HTTPException` | **83** 处（行号可用 `grep -n "raise HTTPException" app/main.py` 得到） |
| `app/employee_auth.py` 直接 `raise HTTPException` | **13** 处（行号：265, 267, 271, 273, 276, 278, 281, 283, 295, 298, 315, 326, 334） |
| 错误工厂间接抛出 | `raise _warehouse_error(...)` **7** 处 + `raise _replenishment_error(...)` **13** 处 = **20** 处（工厂改造后这 20 处调用点**无需改动**） |
| 错误工厂定义 | `_warehouse_error`：`app/main.py:1088`（`return HTTPException` 于 `:1106`）；`_replenishment_error`：`app/main.py:1916`（`return HTTPException` 于 `:1919`、`:1924`） |
| **96 处直接抛错的形态** | **全部**带 `detail={"code": ..., "message": ...}`，**无一处例外**；**无一处**传 `headers=` |
| status_code 分布 | 字面量：404×26、422×22、409×7、403×7、401×6、503×5、500×4、415×2、400×1、413×1；**变量**：`status`×9、`status_code`×3、`status_by_code`×3（共 15 处由被捕获异常推导，如 `main.py:2306-2313`、`2498-2503`、`2633+`） |

### 关键耦合点（必须逐一处理）

1. **`app/main.py:350`**：全仓**唯一**的 `except HTTPException as exc:`，在 `request_context_middleware` 内捕获 `validate_csrf` 的 CSRF 失败并转 JSONResponse。`validate_csrf`（`app/employee_auth.py:287`，其 raise 在 `:295`/`:298`）迁移后改捕 `DomainError`。
2. **`app/main.py:366-368`**：现有唯一 exception handler（`RequestValidationError` → 422），**保持不动**；新 handler 注册在其旁。
3. **`app/main.py:354-361`**：中间件 `except Exception` → 500 `INTERNAL_ERROR` 兜底，**保持不动**。机制说明：exception handler 在 `call_next` 内层（路由层）生效，`DomainError` 不会穿透到中间件的 `except Exception`；请勿在该兜底里加对 `DomainError` 的捕获。
4. **中间件内直接构造的 `JSONResponse`（401/403，`main.py:319-333`）不走异常机制**，本任务**不触碰**。
5. **导入行**：`app/main.py:22`（`from fastapi import ... HTTPException ...`）与 `app/employee_auth.py:11`（`from fastapi import HTTPException, Request`）。迁移完成后从两处导入中**移除 `HTTPException`**；`employee_auth.py` 的 `Request`（`require_principal:257`、`validate_csrf:287` 等类型注解）**保留**，由 R2 处理。

### 不相关的相邻事实（避免误判）

- `app/content_production.py:24` 的 `ContentProviderError` 是**领域层既有异常**（带 `code`/`message`），被 `status_by_code` 块捕获后转 HTTP 形态。它**不是**本任务的迁移对象，只要求转换点改抛 `DomainError`。
- `app/security.py` 的双 `Principal` / 死导入问题属 **R2**，本任务**不碰** `app/security.py`。
- 既有测试共 53 个 `test_*.py` + `conftest.py`，当前基线 `283 passed + 0 failed`。

### Fact / Target / Assumption / Unknown

- **Fact：** 上表全部数字与行号（2026-10-26 实测）。
- **Target：** 残留 `raise HTTPException` 为 0；全量回归 `283 passed + 0 failed`；既有测试文件零修改。
- **Assumption：** 迁移是纯机械改写（同 code / 同状态码 / 同 message 文案），响应逐字节等价。
- **Unknown：** 仓库外脚本/客户端是否硬依赖 `HTTPException` 类名（仓库内证据为 0）。

## 本次范围

### 允许修改的文件

```text
app/errors.py                    # 新增：DomainError 定义（不得 import fastapi）
app/main.py                      # 新增 handler + import 调整 + 83 处迁移 + 2 个工厂 + :350 except 分支
app/employee_auth.py             # 13 处迁移 + :11 导入移除 HTTPException（保留 Request）
tests/test_domain_error.py       # 新增：见"完成标准 1/2/5"
```

### 不允许修改的文件

```text
app/ 下除上述外的任何文件（尤其 app/security.py、app/content_production.py、app/db.py、app/repository.py）
tests/ 下任何既有文件（含 conftest.py、_wal_bootstrap/）
docs/ 任何文件
pyproject.toml / alembic.ini / migrations/
```

### 明确不做的事情

1. **不改变任何**错误码字符串、状态码、message 文案、响应头 —— 一处都不改。
2. 不给 `DomainError` 加 `headers`、重试、日志、i18n、序列化等任何附加能力。
3. 不把 `status_by_code` 的推导逻辑"优化"或搬进 `errors.py` —— **留在原地**，只把最终 `raise HTTPException(...)` 换成 `raise DomainError(..., http_status=...)`。
4. 不动中间件的 401/403 直接 `JSONResponse` 分支（`main.py:319-333`）与 500 兜底（`:354-361`）。
5. 不删除 `employee_auth.py` 的 `from fastapi import Request`（R2 的事）。
6. 不实现 `RequestContext`、不合并 `Principal`（R2 的事）。
7. 不拆分 `main.py` 路由、不新增/删除任何路由。
8. 不改 `ContentProviderError` 及领域层异常定义。

## 实施要求

### 数据流和状态边界

- 纯异常类型替换，**无任何数据、状态、事务变化**。
- `DomainError` 建议形态（字段名可微调，语义不变）：

```python
# app/errors.py —— 不得 import fastapi
class DomainError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.http_status = http_status
```

- handler 建议形态（响应外形必须与 FastAPI 对 `HTTPException` 的默认处理**逐字节一致**）：

```python
@app.exception_handler(DomainError)
async def domain_error_handler(_request: Request, exc: DomainError):
    return JSONResponse(
        status_code=exc.http_status,
        content={"detail": {"code": exc.code, "message": exc.message}},
    )
```

### 迁移不变式（逐条验收）

1. `raise HTTPException(status_code=S, detail={"code": C, "message": M})` → `raise DomainError(code=C, message=M, http_status=S)`。
2. 变量形态：`raise HTTPException(status_code=status, detail={"code": code, "message": ...}) from exc` → `raise DomainError(code=code, message=..., http_status=status) from exc`；**`from exc` 链全部保留**。
3. 工厂函数：仅把返回类型与 3 处 `return HTTPException(...)` 换成 `DomainError(...)`，**status 推导、messages 字典、special 集合一律原样保留**；20 处调用点不改。
4. `main.py:350`：`except HTTPException as exc:` → `except DomainError as exc:`，其 `content={"detail": exc.detail}` 相应改为从 `exc.code` / `exc.message` 构造（等价外形）。

### Workspace / RBAC 边界

- 不涉及。401/403 的判定逻辑（`require_principal`、`validate_csrf`）只换异常类型，判定结果与顺序不变。

### 幂等、版本、事务和审计要求

- 不涉及（无状态变更）。

### 失败、重试、取消和人工接管要求

- 不涉及。若实施中发现任何"迁移会改变行为"的点（如某处 message 是动态拼接且格式存在差异），**停止该处迁移、按原样保留 `HTTPException` 并在交付说明中登记**，不得为了"清零"而改写 message。

## 验证命令

```bash
# 定向（新增测试 + 两个受影响文件直接相关的测试面）
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_domain_error.py -q -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_admin_users.py tests/test_api_content_export.py tests/test_api_tickets.py tests/test_frontend_warehouse.py -q -p no:cacheprovider

# 残留检查（两个都应为 0 行输出）
grep -rn "raise HTTPException" app/
grep -n "HTTPException" app/employee_auth.py   # 期望仅剩 from fastapi import Request 相关行，无 HTTPException

# 全量（基线 283 passed + 0 failed，测试提速 WAL 已就位）
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

## 完成标准

### 1. 正向测试

新增 `tests/test_domain_error.py`，用最小 FastAPI 应用（不依赖 app.db）验证 handler：抛 `DomainError("X_NOT_FOUND", "msg", http_status=404)` → 响应 404 且 `response.json() == {"detail": {"code": "X_NOT_FOUND", "message": "msg"}}`；另验证默认 `http_status=400`。

### 2. 负向测试

同文件内验证 CSRF 中间件路径：对 `/api/` 下任一写端点缺 CSRF 调用 → 仍为 403 且 `code == "CSRF_INVALID"`（沿用既有测试的构造方式，参考 `tests/test_admin_users.py` / `tests/test_frontend_warehouse.py` 中现成的 CSRF 用例写法）。

### 3. 越权测试

不新增。既有 403 用例（`FORBIDDEN`）必须全部继续通过（全量回归覆盖）。

### 4. 重复 / 幂等测试

不新增。既有幂等冲突用例（`IDEMPOTENCY_KEY_REUSE` / `*_VERSION_CONFLICT`，经 `_replenishment_error`）必须全部继续通过。

### 5. 故障 / 回放测试

新增 1 例：未注册 handler 的任意异常 → 仍为 500 `{"detail": {"code": "INTERNAL_ERROR", "message": "服务内部错误"}}`（证明中间件兜底未被破坏）。

### 6. 真实、模拟和未知范围的区分

- 本任务**不触碰任何数据面与外部平台调用**。交付说明须声明：本切片无真实/模拟数据语义变化。
- 若实施中发现无法机械迁移的位点，按"实施要求"末条保留 `HTTPException` 并在交付说明中**如实列为 Unknown/遗留**，不得静默处理。

### 7. 交付与门禁（`CLAUDE.md`、契约 §4.1 / §4.3）

- 按契约 §4.1 交付：改动文件清单（含行号区间）/ 关键 diff 说明 / 验证命令与**完整原始 stdout**（H1）/ 真实·模拟·未知区分 / 已知限制 / 未修改文件声明。
- **既有测试文件零修改**是硬性完成标准：`git diff --stat tests/` 只允许出现**新增**的 `tests/test_domain_error.py`。
- 不 commit、不 push（由规划方复核后统一处理）。

## 禁止操作

- `git reset` / `git clean` / 删除任何未跟踪文件 / `git commit` / `git push`
- 修改或删除 `tests/` 既有文件与 `tests/_wal_bootstrap/`
- 任何外部平台写入、任何真实平台调用
- 触碰 `docs/`、`migrations/`、`pyproject.toml`

## 附：本任务与既有文档的对应关系

| 文档 | 关联 |
|---|---|
| `docs/adr-0012-identity-context-and-error-mapping.md` | 本任务 = 决策 1 的实施切片 |
| `docs/p1-boundary-audit-20261012.md` §5 | 96 处改造面收窄的依据 |
| `docs/engineering-roadmap.md` L54 | 对应 P1 项 7（统一 DomainError 映射） |
| `docs/claude-handoff-contract.md` §3.2 / §4.1 / §4.3 | 任务结构 / 交付字段 / 硬闸门 |
