# R2 任务：身份上下文统一（去双 Principal、去适配层、删 app/security.py、轻量 RequestContext）

> 依据：ADR-0012（`docs/adr-0012-identity-context-and-error-mapping.md`）决策 2；
> `docs/p1-boundary-audit-20261012.md` §5 / §13 裁定记录；
> 契约 §3.2 九节 / §4.1 交付字段 / §4.3 硬闸门。
> 前置：R1（DomainError）已完成并提交（review-checklist §9），本文件所有行号基于 R1 之后的工作树。

## 1. 背景与目标

仓库当前存在**两套并行的身份模型**：

- `app/security.py`（90 行）：`Principal`（6 字段）+ `demo_principal` + `validate_auth_config` + `authenticate_api_key` + 私有 `_resolve_workspace`。**零测试覆盖**（tests/ 下 0 个文件引用），是早期版本。
- `app/employee_auth.py`：`Principal`（10 字段 + `is_employee`/`has_role`/`can` 三个方法）等，被 17 个测试文件覆盖，是**维护中的版本**。

两套由私有函数 `_as_employee_principal`（employee_auth.py:245）桥接，该函数**选择性拷贝字段**——`security.Principal` 一旦加字段会被静默丢弃。

R2 目标：收敛为唯一身份模型，删除 `app/security.py` 与适配层，并落地轻量 `RequestContext` 类型（P1 项 6）。

## 2. 现状事实（已由规划方核实，2026-10-26，勿重新调研）

### 2.1 app/security.py（将整体删除）

| 成员 | 行号 | 说明 |
|---|---|---|
| `Principal` | :10-16 | 6 字段：subject/tenant_id/key_id/scopes/workspace_id/auth_type |
| `_resolve_workspace` | :19-51 | 私有；单工作区自动 backfill、多工作区按 `tenant_key or api_tenant_id` 匹配、**零工作区自动创建** |
| `demo_principal` | :54-60 | 与 employee_auth 版逐字相同 |
| `validate_auth_config` | :63-69 | 只校验 API 侧两项 |
| `authenticate_api_key` | :72-89 | 走 `_resolve_workspace` |

全仓引用（已穷尽 grep，`upload_security` 是另一个模块不在此列）：

```text
app/main.py:176  from app.security import Principal as ApiPrincipal, authenticate_api_key
app/main.py:199      from app.security import validate_auth_config     （lifespan 内局部导入）
app/main.py:337      from app.security import demo_principal           （中间件 demo 分支局部导入）
```

**tests/ 下 0 个文件引用 app.security。** 无其他引用。

### 2.2 app/employee_auth.py（收敛基准，保留）

| 成员 | 行号 | 处置 |
|---|---|---|
| `Principal` | :48-76 | 10 字段 + 3 方法；**唯一版本**，迁往 `app/identity.py` |
| `ROLE_ADMIN/OPERATIONS/WAREHOUSE/CUSTOMER_SERVICE/READONLY` | :25-29 | 迁往 identity |
| `ROLE_PERMISSIONS` | :31-40 | 迁往 identity；**值一个都不能改** |
| `ROLE_LABELS` | :42-45 | 迁往 identity |
| `demo_principal` | :79-85 | 迁往 identity（与 security 版逐字相同） |
| `_resolve_compat_workspace` | :88-115 | demo 分支用；**不复活停用空间**；迁往 identity |
| `_resolve_api_workspace` | :118-125 | API 分支用；按 `api_tenant_id` 精确查 active，找不到返回 None；迁往 identity |
| `validate_auth_config` | :128-135 | API 侧两项 + SESSION_TTL 校验；**合并后唯一版本**，迁往 identity |
| `authenticate_api_key` | :138-159 | 走 compat/api 两个 resolver；迁往 identity |
| `hash_password`/`verify_password` | :166-189 | 保留原位（account_admin.py:9、cli.py:431 引用） |
| `create_session`/`load_session`/`principal_from_session` | :192-242 | 保留原位 |
| `_as_employee_principal` | :245-255 | **删除**；全仓唯一调用点 :261 |
| `require_principal` | :258-285 | 保留；:261 改为直接使用 `request.state.principal`（去适配层） |
| `validate_csrf` | :288-299 | 保留不动 |
| `require_warehouse_access` | :302- | 保留不动 |

注意：employee_auth.py 现已 `from app.errors import DomainError`（R1 成果），`require_principal`/`validate_csrf`/`require_warehouse_access` 内全部抛 `DomainError`。

### 2.3 app/main.py 使用点

| 行号 | 现状 | 处置 |
|---|---|---|
| :176 | `from app.security import Principal as ApiPrincipal, authenticate_api_key` | **`ApiPrincipal` 是死别名**（全文件除该行外 0 次出现）；改为 `from app.employee_auth import authenticate_api_key` |
| :314 | 中间件 API 分支调 `authenticate_api_key(...)` | 导入路径切换后此行不动 |
| :199-202 | lifespan 局部导入 security 版 `validate_auth_config`，然后**串行调用两次**：`validate_auth_config(get_settings())` + `validate_employee_auth_config(get_settings())` | 合并为**一次** employee_auth 版调用（其校验是 security 版的超集：同样两项 API 校验 + SESSION_TTL），并删除 :190 的 `validate_auth_config as validate_employee_auth_config` 别名导入 |
| :337 | 中间件 demo 分支局部导入 `from app.security import demo_principal` | 改 `from app.employee_auth import demo_principal` |
| :177-190 | `from app.employee_auth import (...)` 块 | 除上述别名行外**不动**（:618/:635/:1015 还引 ROLE_*，依赖 re-export） |

### 2.4 其他消费者（导入路径必须保持不变）

```text
app/account_admin.py:9   from app.employee_auth import ROLE_ADMIN, hash_password
app/cli.py:431           from app.employee_auth import hash_password
app/demo_seed.py:23      from app.employee_auth import ROLE_ADMIN
tests/ 下 17 个文件       引 employee_auth（路径不变即可，零修改）
```

### 2.5 request.state 现状（RequestContext 落地面）

`app/main.py` 中 `state.principal` 4 处、`state.request_id` 2 处、`state` 合计 8 处。
中间件 `request_context_middleware`（:302 起）已单点生成 `request_id`、解析 principal（/api/ 分支、session 分支、demo 分支三个成功赋值点写 `request.state.principal`）、做 CSRF 与 401 早退。**P1 项 6 缺的只是"一个公共类型"，不是功能。**

### 2.6 有意行为变更（必须随交付说明登记，写"真实"）

security 版 `authenticate_api_key` → employee_auth 版的语义差异（中间件 :314 调用路径）：

| 场景 | security 版（旧） | employee_auth 版（新，基准） |
|---|---|---|
| API 开启、零工作区 | `_resolve_workspace` **自动创建** workspace 并放行 | `_resolve_api_workspace` 精确查 `api_tenant_id` → None → 401 |
| API 开启、多工作区 | 按 `tenant_key or api_tenant_id` 匹配唯一者 | 只认 `api_tenant_id` 精确匹配 |
| auth 关闭（demo） | `_resolve_workspace`（含自动创建，name 取 `auth_bootstrap_workspace_name or configured`） | `_resolve_compat_workspace`（先查已存在，**停用空间不复活**，name 取 `auth_bootstrap_workspace_name or "默认商家"`） |

这是**安全收紧**（更少静默创建/复活），方向与 ADR-0012 一致；但属于行为变更，不是"逐字节不变"。

## 3. 本次范围（允许修改/新建/删除的文件）

```text
新建  app/identity.py            （唯一身份模型，禁止 import fastapi）
新建  app/request_context.py     （RequestContext + get_request_context，禁止 import fastapi 之外再引 app.main）
新建  tests/test_identity.py     （contract tests）
修改  app/employee_auth.py       （改从 identity re-import；删重复定义；删 _as_employee_principal）
修改  app/main.py                （:176/:199-202/:337 三处导入收敛；中间件三个成功赋值点挂 ctx）
删除  app/security.py            （git rm）
```

### 3.1 app/identity.py 规格自 employee_auth 迁入（逐字搬移，不重写）

```python
# 允许的导入面：dataclasses / hmac / __future__ annotations /
#   app.config.Settings / app.db(Workspace, backfill_legacy_workspace) / sqlalchemy.select / sqlalchemy.orm.Session
# 必须迁入：Principal(:48-76)、ROLE_* 五常量、ROLE_PERMISSIONS(:31-40)、ROLE_LABELS(:42-45)、
#   demo_principal(:79-85)、_resolve_compat_workspace(:88-115)、_resolve_api_workspace(:118-125)、
#   validate_auth_config(:128-135)、authenticate_api_key(:138-159)
# 禁止出现：fastapi / Request / DomainError（本模块不抛业务错误，也不碰 HTTP）
```

### 3.2 app/employee_auth.py 改造

- 头部改为 `from app.identity import (...)` re-export 上述全部名字（保持 `ROLE_ADMIN`、`hash_password`、`create_session`、`principal_from_session`、`require_principal`、`require_warehouse_access`、`validate_csrf`、`verify_password` 等既有导入路径不变——account_admin/cli/demo_seed/main.py/tests 都按此路径引用）。
- 删除与 identity 重复的定义本体。
- **删除 `_as_employee_principal`**；:261 改为 `principal = getattr(request.state, "principal", None)` 直接使用（中间件是 `state.principal` 唯一写入点，类型恒为 identity.Principal；`validate_csrf` :290 本就直接使用，无需改）。
- `hash_password`/`verify_password`/session 三件套/CSRF/仓库授权**留在本文件**（它们是 HTTP 适配层，允许继续 `from fastapi import Request`）。

### 3.3 app/request_context.py 规格

```python
from dataclasses import dataclass
from app.errors import DomainError
from app.identity import Principal

@dataclass(frozen=True)
class RequestContext:
    request_id: str
    principal: Principal | None

def get_request_context(request) -> RequestContext:
    ctx = getattr(request.state, "ctx", None)
    if ctx is None:
        raise DomainError(code="UNAUTHORIZED", message="请求上下文缺失", http_status=401)
    return ctx
```

`request` 参数用鸭子类型即可（不强注 Request 类型，避免本模块 import fastapi——若实现方选择注解 Request 亦可接受，但那样本模块也会带 fastapi 导入，**二选一并在交付说明里写明选择**）。

### 3.4 app/main.py 中间件挂 ctx

- 在 `request_context_middleware` 内 `request.state.principal` 的**三个成功赋值点**之后，同步 `request.state.ctx = RequestContext(request_id=request_id, principal=principal)`。
- 401 早退分支**不得**构造 ctx。
- **保留**既有 `state.principal` / `state.request_id` 写入（兼容现有 8 处直接读取；路由迁移不在本切片）。
- :176/:199-202/:337 按 §2.3 处置；lifespan 合并后只调 employee_auth 版一次。

## 4. 明确不做的事情

1. **不修改任何既有测试文件**（17 个引 employee_auth 的 + 其余全部）。硬验收。
2. 不改 `ROLE_PERMISSIONS`/`ROLE_LABELS` 的任何值，不改 `can`/`has_role`/`is_employee` 语义。
3. 不改 `require_principal`/`validate_csrf`/`require_warehouse_access` 的判定逻辑（仅去适配层，抛错语义原样）。
4. 不改中间件的 401 响应体（`UNAUTHORIZED`/`认证失败`）、CSRF 段、500 兜底——R1 已固化。
5. 不碰 `hash_password`/`verify_password`/`create_session`/`load_session`/`principal_from_session` 的实现（只允许随 re-export 调整 import 行）。
6. 不改 `app/account_admin.py`、`app/cli.py`、`app/demo_seed.py`（它们的导入路径靠 re-export 保持）。
7. 不做路由拆分、不动 `source_mode`（ADR-0012 决策 3/4）。
8. 不把 120 个路由对 `request.state.*` 的读取改为 `get_request_context`（迁移不在本切片；ctx 是新增供给，不是替换读取）。
9. 不新增配置项、不改 `.env` 语义。

## 5. 迁移不变式（复核将逐条对拍）

1. `tests/test_identity.py` 之外，`git diff --stat tests/` 必须为空。
2. `app/identity.py` 与 `app/request_context.py` 中 `grep -c fastapi` = 0。
3. 全仓 `from app.security|import app.security|app\.security` 残留 = 0；`app/security.py` 不存在。
4. 全仓 `_as_employee_principal` 残留 = 0。
5. `ApiPrincipal` 残留 = 0。
6. `raise HTTPException` 保持全仓 0（R1 成果不回退）。
7. re-export 后 `python -c "from app.employee_auth import ROLE_ADMIN, hash_password, Principal, ROLE_PERMISSIONS"` 可用（等价断言写入测试）。
8. §2.6 的三条行为差异以 employee_auth 版为准生效，并在交付说明"有意行为变更"小节逐条登记为"真实"。
9. 中间件三个成功路径下 `request.state.ctx` 均存在且 `request_id`/`principal` 与旧字段一致（测试断言）。
10. 全量基线：当前 **288 passed / 0 failed**（R1 后，review-checklist §9.4）。完成后必须 ≥288 全绿（新增用例只增不减）。

## 6. 验证命令（一律带 R-15 前缀）

```bash
# 全量（复核方将独立重跑）
env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider

# 新增定向
env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_identity.py

# 残留自检（应全为 0 / 空输出）
grep -rn "app\.security" app/ tests/ --include=*.py
grep -rn "_as_employee_principal\|ApiPrincipal" app/ tests/ --include=*.py
grep -cn "fastapi" app/identity.py app/request_context.py
```

## 7. 完成标准（契约 §4.1 六字段交付）

1. §6 全部命令真实执行，**完整原始 stdout** 附入交付说明（H1，禁止摘要代替）。
2. 新建 `tests/test_identity.py` 至少覆盖：
   - `Principal.can`：demo 恒 False / api_key 按 scope 与通配 / admin 全权 / 角色权限表命中与否；
   - `has_role`；
   - `authenticate_api_key`：auth 关闭→demo principal；key 错→None；API 开启、workspace 缺失→None（不自动创建）；
   - `_resolve_compat_workspace`：停用同名空间不复活；
   - `app.security` 已删除：`pytest.raises(ImportError)` 包住 `import app.security`；
   - re-export 路径：`from app.employee_auth import ROLE_ADMIN, hash_password, Principal, ROLE_PERMISSIONS`；
   - 中间件：成功请求后 `response.request` 侧可通过任一业务接口断言 200 正常（既有测试已覆盖，可不另造）；`get_request_context`：ctx 缺失时抛 `DomainError("UNAUTHORIZED")`（可用假 request 对象测试，不必起服务）。
3. 交付说明含**有意行为变更**小节（§2.6 三条 + lifespan 两次 validate 合并为一次），逐条标注"真实"。
4. 证据文件放 `.testtmp/`（被 .gitignore 忽略），附 sha256 前 16 位。
5. 不自行 commit、不 push、不删任何未跟踪文件（契约 §4.3）。

## 8. 已排除的选项（勿再试错，规划方已论证过）

- **不把 `hash_password`/session 三件套迁入 identity.py**：它们要么无身份语义、要么天然绑定 HTTP/DB 适配，迁移只增加 diff 面而无收敛收益。
- **不改 `require_principal` 的签名或 Depends 用法**：约 120 个路由经 `Depends(require_principal)` 引用，改签名即全面开花，属路由拆分切片的事。
- **不做"路由侧改读 `get_request_context`"**：120 路由 × `state.*` 读取的替换收益低、回归面大；ctx 本切片只做"单点供给 + 类型化 + 测试"，判定为 P1 项 6 的达成形态由复核定夺。
- **不给 `security.py` 留兼容空壳/转发模块**：全仓引用仅 main.py 3 处且已全部迁移、0 测试引用，空壳只会留下第二个"事实错误码"式的旧约定温床。
- **不合并 `employee_auth` 的两个 workspace resolver**：`_resolve_compat_workspace`（demo 语义）与 `_resolve_api_workspace`（API 语义）行为差异是**有意的**（demo 宽、API 严），合并属于语义重设计，超出 R2。
- **不引入 `app/principal.py`/`app/auth.py` 等其他命名**：`identity.py` 一并承载 Principal/角色/权限/解析，避免再起一层。
- **不改 `.env` 或任何开关默认值**。

## 9. 边界外登记（不处理，只留痕）

- `app/employee_auth.py` 仍 import fastapi（Request 注解）——按 ADR-0012，HTTP 适配层允许；P1 项 5 的"领域函数不依赖 FastAPI"由 `identity.py` 达成，最终判定归复核。
- `request.state.*` 直接读取（main.py 8 处 + employee_auth 2 处）向 ctx 的迁移，随路由拆分解冻时一并做。
