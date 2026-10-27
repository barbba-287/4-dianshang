# demo 身份路径审计（2026-10-26）

- 审计类型：只读代码审计 + 一次只读 Shopify Admin API 调用
- 触发：用户 2026-10-26 指令「demo 路径先审计加 contract test 再拍」
- 上位文档：`docs/p2-canary-readiness-gap-20261026.md`、ADR-0012
- 相关任务文件：`dev_task.demo-contract-tests.md`（本审计的后续动作）

---

## 0. 结论摘要

| 问题 | 结论 |
|---|---|
| demo 身份能否绕过鉴权触达业务路由 | **不能**。115 个业务路由全部经过 `require_principal`；未经过的 5 个全是公开端点 |
| `DEMO_MODE_ENABLED=true` + `EMPLOYEE_AUTH_ENABLED=true` 是否危险 | **不危险，但语义误导**：此组合下 demo 在鉴权上完全无效，唯一实际效果是放行 `seed-demo` CLI 命令（发现 D-1） |
| 是否发现新的契约缺陷 | ~~发现 D-2（未登录访问 `/api/*` 返回 303）~~ **已裁定（2026-10-27 用户裁定 P2-0b 项 1）**：中间件 `app/main.py:316` 存在 `/api/` 前缀专用分支，未登录访问 `/api/*` 返回 **401 JSON**、页面路由 303 —— 此即**规格语义**，非缺陷。详见 §5 正式裁定 |
| 是否发现测试环境问题 | **是**。测试读仓库外 `.env` 的 `SHOPIFY_*` 值，结果随本地环境漂移（发现 D-3） |

处置建议（更新后）：**不改产品代码**，contract test 以**规格锁定**方式固化（9 条，已实施并复核通过，见 §7）。
D-2 已于 2026-10-27 由用户正式裁定（§5），不再作为待拍板项流转。

---

## 1. 检查项一：`.env` 的版本控制状态（无泄露）

| 检查 | 命令 | 结果 |
|---|---|---|
| 是否被跟踪 | `git ls-files --error-unmatch .env` | `error: pathspec '.env' did not match any file(s) known to git` → **未跟踪** |
| 忽略规则 | `grep -n .env .gitignore` | `.gitignore:5` = `.env` ✅ |
| 全历史对象中的 `.env` blob | `git rev-list --all --objects \| grep -cE '(^\|/)\.env$'` | **0** |
| 是否有提交触碰过 | `git log --all --oneline -- .env` | 空 |
| 覆盖的 ref | `git for-each-ref` | 3 个本地分支、2 个远端跟踪分支、1 个标签 `v0.1.0-week1` 均已覆盖 |

**判定：凭证从未进入版本库，无需轮换，无需清理历史。** 仓库外另有 `.env.example`（2239 B，已跟踪）作为键名模板。

附带发现（非安全，属整洁性）：`.env` 中 `SHOPIFY_API_VERSION` **重复出现两次**（第 8 行与第 14 行），dotenv 以**后出现者**为准。当前两值相同，无实际影响，但属易错点。

---

## 2. 检查项二：店铺性质（确认为开发店）

以只读 `GET /admin/api/{version}/shop.json` 取得，**未打印 token**（探针只输出长度与前 4 字符）。

| 字段 | 值 | 判读 |
|---|---|---|
| `name` | `dianshang-dev` | 名称含 dev |
| `domain` / `myshopify_domain` | `dianshang-dev.myshopify.com` | 未绑定自定义域名 |
| `plan_name` | `basic` | — |
| `plan_display_name` | **`Basic App Development`** | Shopify Partner **开发店**的专用计划名 |
| `password_enabled` | **`true`** | 店铺启用密码保护，终端顾客无法访问 |
| `created_at` | 2026-09-18T11:27:39-04:00 | 开店 1 个多月 |
| `country_code` / `currency` | `US` / `USD` | 默认配置 |
| `X-Shopify-Shop-Api-Call-Limit` | `1/40` | Basic 计划 40 次/桶 |
| 凭证形态 | `shpat_` 前缀，38 字符 | Shopify Admin API access token 标准形态 |

**判定：开发店，不是正式营业店。** 但仍按「保守默认」把 `SHOPIFY_LIVE_ENABLED` 置为 `false`（见 §5），理由有二：
1. 开发店同样消耗真实 API 限额，40 次/桶下 `SHOPIFY_MAX_PAGES=100` 的同步必然撞限；
2. 热开关未落地前，改 env 重启是唯一止损手段。

---

## 3. 检查项三：demo 的暴露面（0）

### 3.1 路由守卫覆盖

用解析器逐个装饰器切段统计（`app/main.py`，HEAD `632d1ab`）：

```text
路由总数: 120
未调用 require_principal 的路由: 5
  L388   /ready      ready
  L416   /health     health
  L434   /login      login_page
  L445   /login      login
  L506   /logout     logout
```

5 个全部是**必须公开**的端点（存活探针 + 登录页 + 登出）。**没有任何业务或数据路由漏门。**

### 3.2 demo 身份的装配条件

中间件 `app/main.py:336-347`：

```python
else:                                                    # api_auth_enabled = False
    public_paths = {"/login", "/health", "/ready", "/docs", "/openapi.json", "/redoc"}
    is_public_path = request.url.path in public_paths or request.url.path.startswith("/static/")
    if settings.employee_auth_enabled and not is_public_path:
        if session_principal is None:
            return RedirectResponse(url=f"/login?next={...}", status_code=303)   # :341 直接拦掉
        request.state.principal = session_principal
        request.state.ctx = RequestContext(...)
    else:
        principal = session_principal or demo_principal(settings)                 # :345 demo 仅在此回落
        request.state.principal = principal
        request.state.ctx = RequestContext(...)
```

即 **demo 被挂到 `state.principal` 的全部情况**（含 2026-10-26 复核修正）：
1. `/api/*` 且 `employee_auth_enabled=true`：demo/api_key/None 一律 **401**（`:316-333`），**不会挂载**；
2. `/api/*` 且 `employee_auth_enabled=false`：`authenticate_api_key` 在 `api_auth_enabled=false` 时返回 demo principal（`identity.py:143`）→ **挂载**，能否过路由由 `require_principal` 第二道门决定；
3. 页面路由且 `employee_auth_enabled=true`、非公开路径：无 session 时 **303**（`:341`），有 session 时挂 session principal；
4. 页面路由且 `employee_auth_enabled=false`：demo principal 挂载（`:345`）。

### 3.3 纵深防御仍然存在

即使 demo principal 被挂上，`require_principal`（`app/employee_auth.py:145-171`）仍会拒绝：

```python
employee_auth_enabled = get_settings().employee_auth_enabled
if principal is None or (employee_auth_enabled and principal.auth_type in ("demo", "api_key")):
    raise DomainError(code="UNAUTHORIZED", message="请先登录", http_status=401)   # :150-153
...
if principal.auth_type == "demo":
    if get_settings().demo_mode_enabled and not employee_auth_enabled:
        ...
        return principal
    raise DomainError(code="UNAUTHORIZED", message="请先登录", http_status=401)   # :156-161
```

**双层结论：对 `/api/*`，中间件 API 分支（`:316-333`）是第一道门（401 JSON），`require_principal` 是第二道门（401 DomainError，纵深防御）；对页面路由，第一道门是 303。** 两道门都依赖 `employee_auth_enabled` 这一开关，因此若该开关被误设为 false，鉴权整体失效——这是设计意图，不是缺陷，但**需要热开关旁的可观测性**（属 P2-0 范围）。

### 3.4 live 入口的守卫

`app/main.py:1726-1760`：

| 路由 | 守卫 |
|---|---|
| `GET /api/external/shopify/capabilities` | `require_principal(request, permission="inventory.read")`（:1728） |
| `POST /api/external/shopify/preview` | `require_principal(..., "inventory.read")`（:1735） |
| `POST /api/external/shopify/sync` | `require_principal(..., "inventory.write")`（:1751） |

三条 live 相关路由均需 `inventory.*` 权限，demo principal 的 `can()` 恒为 False（`identity.py:70-75`），**匿名不可触达**。

---

## 4. 发现 D-1：`DEMO_MODE_ENABLED` 的语义误导（P2，配置层）

`demo_mode_enabled` 全仓只有两个消费点：

| 位置 | 用途 |
|---|---|
| `app/cli.py:485-487` | `seed-demo` 命令的准入开关 |
| `app/employee_auth.py:157` | demo 分支的准入，但**前提是 `not employee_auth_enabled`** |

因此当前 `.env`（`DEMO_MODE_ENABLED=true` + `EMPLOYEE_AUTH_ENABLED=true`）的实际效果是：

- ✅ `seed-demo` CLI 可用；
- ❌ 鉴权上**零影响**（demo 已被中间件 303 与 `require_principal` 401 双重拦下）。

**风险不在于它放行了什么，而在于它让运维误以为"演示模式开着"。** 建议二选一（待拍板）：
- A. 启动时校验：两者同开则记录 WARN（配置意图不一致）；
- B. 拆分开关：`SEED_DEMO_ENABLED`（CLI 用）与 `DEMO_MODE_ENABLED`（鉴权用）分离，消除同名双义。

此发现与 ADR-0012 记下的 `source_mode` 同名不同义属**同一类问题**（一个名字承担两种语义），建议一并纳入 P2-0 的配置契约整理。

---

## 5. D-2 正式裁定（2026-10-27 用户裁定，P2-0b 项 1）

> **裁定状态：已拍板（正式裁定）。** 本节不再是"撤销记录"，而是**规格裁定**：本次行为即期望行为，
> 不再作为待拍板项流转，contract test 以**规格锁定**方式固化。

**裁定人 / 日期**：用户，2026-10-27（依据 `dev_task.p2b-audit-closure-and-retention.md` §1 分项裁定项 1）。

**纠正历程（303 → 401）**：本审计初版（2026-10-26）曾断言"未登录访问 `/api/*` 返回 303"；
同日复核证实该断言错误——根因是以 `sed 325,355` 采样窗口读中间件，切断了 `:300-324` 的控制流
（登记为教训 R-20）。实测纠正为 401 JSON，原结论当日撤销。本节于 2026-10-27 由"撤销记录"
升格为"正式裁定"，401/303 语义自此为规格而非"实测凑合"。初版误判的完整记录保留于 §8 时间线。

**裁定内容**：**未登录访问 `/api/*` 返回 401 JSON、页面路由返回 303**，此即**规格语义**，非"现状凑合"。

**裁定依据**：API 语义应当是 401（携带 `WWW-Authenticate: ApiKey`，JSON body 含稳定错误码
`UNAUTHORIZED`）而非 302/303；浏览器跳转语义（303 → `/login?next=...`）只适用于**页面路由**。
把两者混用会让 API 客户端收到 HTML 重定向，无法解析错误码，也无法区分"未认证"与"资源不存在"。

**事实依据**（复核方于 2026-10-27 再次实测确认；行号对应 HEAD `aa76d91` 的 `app/main.py`）：

- 中间件 `request_context_middleware`（`app/main.py:304`）按**路径前缀**分流：
  - `:316` `if request.url.path.startswith("/api/"):` → API 分支；
    `:321-330` 与 `:331-338` 两条路径均产出 401 JSON
    `{"detail": {"code": "UNAUTHORIZED", "message": "认证失败"}}`；
  - `:341` `else:` → 页面分支；`:344-346` 未登录且非公开路径时 303 重定向到 `/login?next=<path>`。
- 公开路径集合（`:342`）`{"/login", "/health", "/ready", "/docs", "/openapi.json", "/redoc"}` 及
  `/static/` 前缀不经过登录门。

**前情（原撤销记录，2026-10-26 复核，保留备查）**：

**原结论作废**：本审计初版称「未登录访问 `/api/*` 返回 303 而非 401」。2026-10-26 复核证实该结论**错误**，
并据以撤销 D-2（当时的处置是"保持现状观察"）。2026-10-27 的本次裁定在此基础上**升格为正式规格**。

```python
@app.middleware("http")
async def request_context_middleware(request, call_next):
    ...
    if request.url.path.startswith("/api/"):                 # :316  ← API 专用分支
        principal = session_principal or authenticate_api_key(...)
        if settings.employee_auth_enabled and (
            principal is None or getattr(principal, "auth_type", None) in ("demo", "api_key")
        ):
            response = JSONResponse(status_code=401, ...)    # :321-330  401 JSON
            return response
        if principal is None:
            response = JSONResponse(status_code=401, ...)    # :331-338
            return response
        request.state.principal = principal
    else:                                                    # :341  仅页面路由
        ... 303 重定向逻辑（:346）...
```

**错误根因（如实登记）**：初版调研用 `sed -n '325,355p'` 采样中间件，窗口恰好从 `/api/` 分支的
**尾部**（401 JSONResponse）开始，**漏读了 300-324**，把 `:326-333` 的 401 误认为
`api_auth_enabled` 分支的一部分、把 `:336` 的 `else:` 当作整个非 API 路径的开头——控制流被采样窗口切断。
教训：**读中间件这类分支结构必须从函数定义行读到函数尾，不能用中段窗口推断**。

**下游修正（本次裁定后）**：
- contract test（`tests/test_demo_path_contract.py`）C1/C4/C7 的 401 断言**与裁定一致，无需改动**；
  文件头已由"现状锁定/待拍板"翻转为**"规格锁定"**并引用本裁定。
- 新增 1 条边界用例锁定 `/api/` 前缀的**精确语义**：`/apifoo`（无尾斜杠）不进入 401 分支，
  按页面路由处理（303 → `/login?next=/apifoo`）。该行为来自 `startswith("/api/")` 的实现语义，
  2026-10-27 实测确认。
- §8 待拍板表中 D-2 行由"撤销"改为"**已裁定**"；D-2 不再流转至后续分项。
- **产品代码零改动**（P2-0b 项 1 边界）。

**仍然成立的相邻事实**：303 语义只影响页面路由（浏览器 UX，合理）；`/api/*` 的 401 契约此前**零测试覆盖**，
现由 C1/C4/C9 固定。

---

## 6. 发现 D-3：测试结果依赖仓库外 `.env`（P2，测试确定性）

`app/config.py:90-95` 声明 `env_file=".env"`，而 `tests/conftest.py:22-25` 只覆盖了 4 个变量：

```python
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["ARTIFACTS_DIR"] = "artifacts"
os.environ["WORKER_LEASE_SECONDS"] = "30"
os.environ["EMPLOYEE_AUTH_ENABLED"] = "false"
os.environ["DEMO_MODE_ENABLED"] = "true"
```

**未覆盖 `SHOPIFY_*`、`TAOBAO_*`、`API_AUTH_ENABLED` 等**，这些值在本地会从 `.env` 读入（`os.environ` 优先于 `.env`，但缺失的键仍读 `.env`）。

后果：同一份代码在"有 .env 的本地"与"无 .env 的 CI"下，`shopify_live_enabled` / `employee_auth_enabled` 之外的配置可能不同。今天 312 passed 是在 `SHOPIFY_LIVE_ENABLED=false` 下取得的；若为 true，未覆盖的用例可能走到不同分支。

建议（P2-0 内顺带处理）：`conftest.py` 补齐所有影响行为的开关为显式值，使测试与本地环境解耦。

---

## 7. 本审计产出的 contract test 清单

见 `dev_task.demo-contract-tests.md`，原 8 条 + 2026-10-27 裁定后新增 1 条，共 **9 条**，
全部为**规格锁定**（依据 §5 裁定），不改产品代码：

| # | 固定对象 | 类型 |
|---|---|---|
| 1 | 未登录 `GET /api/inventory` → **401 JSON**（原判 303，已按实测更正） | API 契约基线 |
| 2 | 未登录 `GET /dashboard` → 303 | 页面路由基线 |
| 3 | `/health`、`/ready` 未登录 → 200 / 非 303 | 公开端点基线 |
| 4 | 未登录 `GET /api/external/shopify/capabilities` → 401；demo 身份（鉴权关）→ 第二道门 401 | live 入口匿名不可达 |
| 5 | demo principal + `employee_auth_enabled=true` → `require_principal` 抛 401 | 第二道门 |
| 6 | demo principal + `employee_auth_enabled=false` + demo_mode → 放行；demo_mode 关 → 401 | 第一道门 |
| 7 | `DEMO_MODE_ENABLED=true` + `employee_auth_enabled=true` → `/api` 401、页面 303 | D-1 语义 |
| 8 | `cmd_seed_demo`：demo_mode=false → 2；workspace 无 `demo-` 前缀 → 2 | D-1 唯一真实作用 |
| 9 | 未登录 `GET /apifoo`（无尾斜杠）→ **303 → `/login?next=/apifoo`**，不进 401 分支 | `/api/` 前缀精确边界（2026-10-27 裁定新增） |

---

## 8. 待拍板（不进本次实施）

| 编号 | 问题 | 选项 |
|---|---|---|
| D-1 | `DEMO_MODE_ENABLED` 与 `EMPLOYEE_AUTH_ENABLED` 并存语义 | A 启动告警 / B 拆分开关 |
| D-2 | ~~未登录访问 `/api/*` 应 303 还是 401~~ | **已裁定**（2026-10-27 用户裁定：401 JSON 为规格语义，页面路由 303；见 §5）——不再流转 |
| D-3 | 测试是否补齐 `SHOPIFY_*` 等开关 | A 补齐 / B 保持现状并记入 CI 说明 |

---

## 9. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-10-26 | 首版。依据 HEAD `632d1ab`；含 `.env` 泄露排查、店铺性质确认、demo 暴露面量化、D-1/D-2/D-3 三项发现 |
| 2026-10-26 | **复核更正**：D-2 撤销（`main.py:311` 存在 `/api/` 前缀分支，未登录 API 实为 401 JSON）；初版因采样窗口切断控制流漏读 `:300-324`。§0/§3/§5/§7/§8 同步更正；contract test C1/C4/C7 按实测断言 401 |
| 2026-10-27 | **D-2 正式裁定**（用户裁定，P2-0b 项 1）：§5 由"撤销记录"升格为"正式裁定"——401 JSON / 303 即规格语义；§0/§7/§8 同步更新；contract test 文件头由"现状锁定/待拍板"翻转为**"规格锁定"**；新增第 9 条用例锁定 `/apifoo` 前缀边界（303）。**产品代码零改动** |
