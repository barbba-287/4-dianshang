# 任务：demo / 未登录身份路径 contract test（只加测试，不改产品代码）

- 任务编号：D-CT（demo contract tests）
- 上位文档：`docs/demo-path-audit-20261026.md`（审计结论与三项发现 D-1/D-2/D-3）
- 上位决策：ADR-0012 决策 2（身份上下文统一）
- 交付契约：`docs/claude-handoff-contract.md` §3.2（任务文件结构）、§4.1（交付字段）、§4.3（H1–H7）

---

## 1. 目标

新增 **一个** 测试文件 `tests/test_demo_path_contract.py`，用 8 条用例**固定 demo / 未登录身份路径的现状行为**。

**本任务不修任何缺陷。** 审计发现的 D-1（`DEMO_MODE_ENABLED` 语义误导）、D-2（未登录访问 `/api/*` 返回 303 而非 401）**尚未拍板**，因此本次只把现状写成可执行契约，供拍板后对照修改。任何"顺手修一下"都算越界。

---

## 2. 已核实的现状事实（不要重复调研）

### 2.1 配置来源与优先级

- `app/config.py:90-95`：`model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore")`
  → `Settings` **会读仓库根 `.env`**；`os.environ` 优先于 `.env`，缺失的键回落到 `.env`。
- `app/config.py:73-80` 默认值：`api_auth_enabled=False`、`api_key=""`、`api_tenant_id="default"`、`employee_auth_enabled=True`、`demo_mode_enabled=False`。
- `tests/conftest.py:21-25` 顶层设置（模块导入时，早于所有 fixture）：
  ```python
  os.environ["DATABASE_URL"] = "sqlite:///:memory:"
  os.environ["ARTIFACTS_DIR"] = "artifacts"
  os.environ["WORKER_LEASE_SECONDS"] = "30"
  os.environ["EMPLOYEE_AUTH_ENABLED"] = "false"
  os.environ["DEMO_MODE_ENABLED"] = "true"
  ```
- `tests/conftest.py:59-76` 两个 autouse fixture：`_isolate_env`（改 DATABASE_URL / ARTIFACTS_DIR / IMPORTS_DIR 到 tmp_path）、`_clear_settings_cache`（每例前 `get_settings.cache_clear()`）。
- **`conftest.py` 未覆盖 `SHOPIFY_*` / `API_AUTH_ENABLED`**，这些会读 `.env`。**不要改 conftest**（见 §4）。

### 2.2 中间件分支（`app/main.py:336-347`）

```python
    else:                                                    # api_auth_enabled = False
        public_paths = {"/login", "/health", "/ready", "/docs", "/openapi.json", "/redoc"}
        is_public_path = request.url.path in public_paths or request.url.path.startswith("/static/")
        if settings.employee_auth_enabled and not is_public_path:
            if session_principal is None:
                return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)   # :341
            request.state.principal = session_principal
            request.state.ctx = RequestContext(request_id=request_id, principal=session_principal)
        else:
            principal = session_principal or demo_principal(settings)                            # :345
            request.state.principal = principal
            request.state.ctx = RequestContext(request_id=request_id, principal=principal)
```

要点：
- `:341` 的 303 对**页面路由与 `/api/*` 一视同仁**，代码中没有 `/api/` 前缀判断（= D-2）。
- demo principal **只在**「公开路径」或「`employee_auth_enabled=false`」时被挂到 state。
- 若 `api_auth_enabled=True`（另一分支）：principal 为 None 时返回 **401 JSON**，不是 303。

### 2.3 `require_principal`（`app/employee_auth.py:145-171`）

```python
principal = getattr(request.state, "principal", None)
employee_auth_enabled = get_settings().employee_auth_enabled
if principal is None or (employee_auth_enabled and principal.auth_type in ("demo", "api_key")):
    raise DomainError(code="UNAUTHORIZED", message="请先登录", http_status=401)      # :150-153
if principal.workspace_id is None and principal.auth_type != "demo":
    raise DomainError(code="WORKSPACE_CONTEXT_REQUIRED", ..., http_status=401)       # :154-155
if principal.auth_type == "demo":
    if get_settings().demo_mode_enabled and not employee_auth_enabled:               # :157
        if roles:
            raise DomainError(code="FORBIDDEN", ..., http_status=403)
        return principal
    raise DomainError(code="UNAUTHORIZED", message="请先登录", http_status=401)      # :161
```

### 2.4 `demo_principal`（`app/identity.py:78-85`）

```python
def demo_principal(settings: Settings, *, workspace_id: int | None = None) -> Principal:
    return Principal(
        subject="anonymous",
        tenant_id=settings.api_tenant_id or "default",
        workspace_id=workspace_id,
        auth_type="demo",
    )
```

### 2.5 live 路由守卫（`app/main.py`）

| 路由 | 行 | 守卫 |
|---|---|---|
| `GET /api/external/shopify/capabilities` | 1726 | `require_principal(request, permission="inventory.read")`（:1728） |
| `POST /api/external/shopify/preview` | 1733 | 同上（:1735） |
| `POST /api/external/shopify/sync` | 1750 | `require_principal(..., "inventory.write")`（:1751） |

### 2.6 公开端点（全仓仅 5 个不走 `require_principal`）

`/ready`(:388)、`/health`(:416)、`GET /login`(:434)、`POST /login`(:445)、`/logout`(:506)。**其余 115 个全部经过 `require_principal`。**

`/ready` 的实现（`:389-405`）先检查 `is_accepting_jobs()`，为 False 时抛 `DomainError(WORKER_NOT_READY, 503)`；再 `SELECT 1` 查库，失败抛 503。**测试环境可能返回 503，不要断言 200。**

### 2.7 `cmd_seed_demo`（`app/cli.py:479-507`）

```python
if not get_settings().demo_mode_enabled:
    print("seed-demo 失败: DEMO_MODE_ENABLED=true 才允许执行"); return 2
workspace_key = _option(args, "--workspace")
if not workspace_key or not workspace_key.startswith("demo-"):
    print("seed-demo 失败: --workspace 必须使用 demo- 前缀"); return 2
database_url = get_settings().database_url
if not database_url.startswith("sqlite:"):
    print("seed-demo 失败: 仅允许 SQLite 演示数据库"); return 2
_ensure_schema()   # ← 三个检查之后才建库，所以前两项失败时不会建表
```

### 2.8 已有覆盖（**不要重复写**）

`tests/test_identity.py` 已有 24 例，其中与身份相关的已被覆盖、**不要重写**：
- `test_can_is_always_false_for_demo_principal`
- `test_authenticate_api_key_returns_demo_principal_when_api_auth_disabled`
- `test_demo_principal_uses_configured_tenant_and_workspace`
- `test_middleware_attaches_ctx_on_demo_success_path`
- `test_middleware_attaches_ctx_on_api_success_path` / `..._session_success_path`
- 其余 18 例见该文件

**可复用的参考实现**（照抄结构，不要 import）：
- `tests/test_identity.py:362-417` 的 `worker_client` fixture（含 `importlib.reload` 序列：cfg → db → repository → background → main）
- `tests/test_identity.py:420-438` 的 `_ctx_of()` 探针（用 `FastAPI()` 挂生产中间件）

**本任务补的是"拒绝路径"，不是"成功路径"。**

---

## 3. 本次范围

### 允许新建（唯一）

- `tests/test_demo_path_contract.py`

### 禁止改动

- `app/` 下**任何**文件（含 `main.py`、`employee_auth.py`、`identity.py`、`config.py`、`cli.py`）
- `tests/conftest.py`
- `tests/` 下**任何既有**测试文件
- `.env` / `.env.example`
- `pyproject.toml` / 依赖清单
- 任何文档文件（审计文档由复核方维护）

### 明确不做的事情

1. **不修 D-2**（不把 `/api/*` 的 303 改成 401）。
2. **不修 D-1**（不拆分 `DEMO_MODE_ENABLED`、不加启动告警）。
3. **不动中间件的 `public_paths` 集合**。
4. **不给 `conftest.py` 补 `SHOPIFY_*` 等开关**（D-3 待拍板）。
5. **不引入新依赖**（pytest / fastapi.testclient / starlette 均已可用）。
6. **不重构** `worker_client` 或 `_ctx_of` 到公共文件（放本文件内）。

---

## 4. 用例规格（8 条，逐条给断言）

文件头需要：

```python
"""demo / 未登录身份路径的契约测试（现状锁定）。

本文件固定的是 2026-10-26 的**现状**，不等于"期望行为"。已登记待拍板项：
- D-1：DEMO_MODE_ENABLED 与 EMPLOYEE_AUTH_ENABLED 并存时，前者对鉴权零影响
- D-2：未登录访问 /api/* 返回 303 而非 401

拍板后修改产品代码时，对应用例需同步更新并注明变更依据。
"""
```

### 公共 fixture

```python
@pytest.fixture
def anonymous_client(tmp_path, monkeypatch):
    """employee_auth 开启、无 session 的客户端（未登录）。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'demo-contract.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IMPORTS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("API_AUTH_ENABLED", "false")     # 走 employee_auth 分支（否则是 401 JSON）
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    # 参考 test_identity.py:362-417 的 reload 序列
    ...
    with TestClient(main_mod.app) as client:
        yield client
    cfg.get_settings.cache_clear()
```

**注意**：不要登录、不要建 `UserAccount`（未登录才有意义）。但**要** `db_mod.Base.metadata.create_all(db_mod.engine)`，否则 `/ready` 的 `SELECT 1` 会 503（这不影响断言，但建表更接近真实）。

---

### C1 `test_unauthenticated_api_request_redirects_to_login`

```python
r = anonymous_client.get("/api/inventory", follow_redirects=False)
assert r.status_code == 303
assert r.headers["location"] == "/login?next=/api/inventory"
```

固定对象：D-2 现状（API 路由同样被 303 拦）。
**必须能失败**：若将来 `/api/*` 改为 401，本用例失败 → 提示同步更新。

---

### C2 `test_unauthenticated_page_request_redirects_to_login`

```python
r = anonymous_client.get("/dashboard", follow_redirects=False)
assert r.status_code == 303
assert r.headers["location"].startswith("/login?next=")
```

---

### C3 `test_public_paths_bypass_login_gate`

```python
r = anonymous_client.get("/health", follow_redirects=False)
assert r.status_code == 200
assert r.json() == {"status": "ok"}

r = anonymous_client.get("/ready", follow_redirects=False)
assert r.status_code != 303                      # 关键：未被登录门拦截
assert r.status_code in (200, 503)               # 503 = 真实就绪状态，不是鉴权拦截
```

**必须能失败**：若 `/ready` 从 `public_paths` 移除，会变成 303 → 断言失败。

---

### C4 `test_live_capabilities_not_reachable_anonymously`

```python
r = anonymous_client.get("/api/external/shopify/capabilities", follow_redirects=False)
assert r.status_code == 303
assert r.headers["location"].startswith("/login?next=")
```

固定对象：live 入口匿名不可达（P2 上线前的关键性质）。
**不要**断言 401/403——现状是 303。

---

### C5 `test_require_principal_rejects_demo_identity_when_employee_auth_enabled`

单元级，不起 app：

```python
def _make_request():
    return Request({"type": "http", "method": "GET", "path": "/x",
                    "headers": [], "query_string": b"", "state": {}})

monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
cfg.get_settings.cache_clear()
settings = cfg.get_settings()
req = _make_request()
req.state.principal = demo_principal(settings)       # subject="anonymous", auth_type="demo"

with pytest.raises(DomainError) as ei:
    require_principal(req)
assert ei.value.code == "UNAUTHORIZED"
assert ei.value.http_status == 401
```

固定对象：第二道门（中间件之外）。

---

### C6 `test_require_principal_accepts_demo_identity_when_employee_auth_disabled`

```python
monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "false")
monkeypatch.setenv("DEMO_MODE_ENABLED", "true")
cfg.get_settings.cache_clear()
principal = demo_principal(cfg.get_settings())
req = _make_request()
req.state.principal = principal
assert require_principal(req) is principal
```

固定对象：第一道门的放行条件（`demo_mode_enabled and not employee_auth_enabled`）。

---

### C7 `test_demo_mode_flag_does_not_grant_api_access`

```python
settings = cfg.get_settings()
assert settings.demo_mode_enabled is True          # 前提：conftest 顶层设为 true
assert settings.employee_auth_enabled is True
r = anonymous_client.get("/api/inventory", follow_redirects=False)
assert r.status_code == 303                        # demo_mode 不改变鉴权结果
```

固定对象：D-1 的语义——`DEMO_MODE_ENABLED` 在 `employee_auth_enabled=true` 时对鉴权零影响。

---

### C8 `test_seed_demo_is_the_only_effect_of_demo_mode`

两条独立断言（可拆成两个 test，也可写一个 test 分两段）：

```python
# (a) demo_mode 关闭 → 拒绝，返回 2
monkeypatch.setenv("DEMO_MODE_ENABLED", "false")
cfg.get_settings.cache_clear()
assert cmd_seed_demo(["--workspace", "demo-x"]) == 2

# (b) demo_mode 开启但 workspace 无 demo- 前缀 → 拒绝，返回 2
monkeypatch.setenv("DEMO_MODE_ENABLED", "true")
cfg.get_settings.cache_clear()
assert cmd_seed_demo(["--workspace", "prod-shop"]) == 2
```

**不要**测 `cmd_seed_demo` 的成功路径（会建库、写数据、耗时长），本次只固定拒绝条件。

---

## 5. 可判别性要求

每条用例必须满足"改坏产品代码它会失败"：

| 用例 | 若被改坏，应失败于 |
|---|---|
| C1 | 把 `/api/*` 从 303 改为 401，或移除 303 分支 |
| C2 | 同上（页面路由） |
| C3 | 从 `public_paths` 移除 `/ready` 或 `/health` |
| C4 | 把 `require_principal` 从 capabilities 路由删掉 |
| C5 | 把 `:150-153` 的 demo 拒绝条件去掉 |
| C6 | 把 `:157` 的 `and not employee_auth_enabled` 改掉 |
| C7 | 让 `demo_mode_enabled` 参与第一道门的放行判断 |
| C8 | 去掉 `cmd_seed_demo` 的两个前置检查 |

**禁止写**以下形式的"永不失败"断言：
- `assert r.status_code in (200, 303, 401, 403)` 之类宽枚举
- 只断言 `r.status_code is not None`
- `pytest.skip` 兜底（如环境不满足导致必须跳过，请如实报告，不要静默跳过）

---

## 6. 已排除的选项

1. **改 `conftest.py` 增加 `anonymous_client`** —— conftest 是全局共享，改它会影响 312 个既有用例的隔离行为。
2. **把 fixture 抽到 `tests/_helpers.py`** —— 只为 1 个文件抽公共层，成本大于收益。
3. **用 `TestClient(app, follow_redirects=False)` 构造参数** —— 既有测试统一用 **per-request** `follow_redirects=False`（见 `tests/test_admin_users.py:78`），跟随项目惯例。
4. **用 `httpx.AsyncClient` / `ASGITransport`** —— 项目既有全部用 `fastapi.testclient.TestClient`。
5. **为 C5/C6 起完整 app** —— `require_principal` 是纯函数，构造 `Request` 即可，起 app 会引入 DB 与中间件噪声。
6. **断言 `r.text` 含中文提示** —— 消息文案可能调整，只断 `code` 与 `http_status`。
7. **测 `/api/inventory` 的 200 成功路径** —— 已由 `tests/test_api_inventory.py` 覆盖。
8. **测 `demo_principal.can()` 恒 False** —— 已由 `test_identity.py:48` 覆盖。

---

## 7. 验证命令

**必须带 `env -u PYTHONPATH -u PYTHONHOME`**（会话注入的 `sitecustomize` 垫片会在 pytest 清理阶段 `SystemExit(1)`，见复核登记 R-15）。
**必须用 `/d/python/python/python.exe`**（PATH 上的 `python` 是无 pytest 的受管解释器，见 R-15 附注）。

定向：

```bash
cd /d/ai/dianshang && \
env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 \
/d/python/python/python.exe -m pytest -q tests/test_demo_path_contract.py -p no:cacheprovider
```

全量（基线 312，新增后应为 320）：

```bash
cd /d/ai/dianshang && \
env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 \
/d/python/python/python.exe -m pytest -q -p no:cacheprovider
```

---

## 8. 完成标准

1. `tests/test_demo_path_contract.py` 存在，含 **8 条用例**（C1–C8，C8 可拆为两个函数则共 9 个 test）。
2. 定向测试 **全绿**，条数符合预期。
3. 全量 **320 passed / 0 failed**（312 + 8，若 C8 拆两次则为 321 —— 以实际为准并在交付说明中说明）。
4. `git status --porcelain` **只出现** `?? tests/test_demo_path_contract.py`；`git diff` 为空。
5. 既有测试文件零修改（`git diff --stat tests/` 为空）。
6. 逐条写明每个用例的"若被改坏会失败于何处"（对应 §5 表格）。
7. 按 §4.1 六字段输出交付说明，并**落盘**到 `.testtmp/delivery.md`（该目录已被 `.gitignore:20` 忽略），不依赖进程 stdout 存活（见复核登记 R-17）。

---

## 9. 交付要求

交付说明必须包含：

| 字段 | 要求 |
|---|---|
| 变更文件清单 | 仅新增文件，附 `git status --porcelain` 原始输出 |
| 关键决策 | 8 条用例与 §4 规格的对应关系（逐条映射，缺一不可） |
| 验证证据 | 定向 + 全量两次命令的**完整原始 stdout**（含末尾统计行），同时写入 `.testtmp/delivery.md` |
| 真实 / 模拟 / 未知 | 明确区分：测试真实执行 = 真实；未调用真实 Shopify API = 事实；未知项单列 |
| 已知限制 | 如 C3 的 `/ready` 在测试环境可能 503；C8 未测成功路径 |
| 未做验证 | 列出未覆盖的分支（如 `api_auth_enabled=true` 分支） |

若某条用例无法按规格实现（例如 `Request` 构造方式不适配当前 Starlette 版本），**如实报告并说明实际做法**，不要静默改成宽断言。
