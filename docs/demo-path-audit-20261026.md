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
| 是否发现新的契约缺陷 | **是**。未登录访问 `/api/*` 返回 `303 → /login` 而非 401，且零测试覆盖（发现 D-2） |
| 是否发现测试环境问题 | **是**。测试读仓库外 `.env` 的 `SHOPIFY_*` 值，结果随本地环境漂移（发现 D-3） |

处置建议：**不改产品代码**，先补 contract test 固定现状（8 条），再拍 D-1/D-2 的修法。

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

即 **demo 只在两种情况被挂到 `state.principal`**：
1. `employee_auth_enabled=true` **且** 路径是公开路径；
2. `employee_auth_enabled=false`（本意即"关闭鉴权"）。

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

**双层结论：中间件是第一道门（303），`require_principal` 是第二道门（401）。** 两道门都依赖 `employee_auth_enabled` 这一开关，因此若该开关被误设为 false，鉴权整体失效——这是设计意图，不是缺陷，但**需要热开关旁的可观测性**（属 P2-0 范围）。

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

## 5. 发现 D-2：未登录访问 `/api/*` 返回 303 而非 401（P1，API 契约）

`app/main.py:341` 的 `RedirectResponse(status_code=303)` 对**页面路由与 API 路由一视同仁**，代码中没有任何 `/api/` 前缀判断。

因此：`GET /api/inventory` 未登录 → `303 Location: /login?next=/api/inventory`；若客户端跟随重定向，最终得到 **200 + 登录页 HTML**，而不是 401 JSON。

影响面（P2 前需评估）：
- 任何 API 客户端 / 监控探针 / Canary 脚本会把"未登录"误读为"成功返回 HTML"；
- 与 R1 建立的统一错误契约（`{"detail": {"code", "message"}}`）不一致——这条路径绕过了 `DomainError` 体系。

**测试现状：0 覆盖。** `tests/` 下 12 处 `303` 断言全部是**登录成功后**的重定向（`POST /login` → 303），**没有一条测"未登录被拦"**。

已登记为待拍板项，本审计**不主张立即改产品代码**——先用 contract test 锁定现状，避免修复时误伤。

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

见 `dev_task.demo-contract-tests.md`，共 8 条，全部为**现状锁定**，不改产品代码：

| # | 固定对象 | 类型 |
|---|---|---|
| 1 | 未登录 `GET /api/inventory` → 303 + `Location: /login?next=` | D-2 现状 |
| 2 | 未登录 `GET /dashboard` → 303 | 页面路由基线 |
| 3 | `/health`、`/ready` 未登录 → 200 | 公开端点基线 |
| 4 | 未登录 `GET /api/external/shopify/capabilities` → 303 | live 入口匿名不可达 |
| 5 | demo principal + `employee_auth_enabled=true` → `require_principal` 抛 401 | 第二道门 |
| 6 | demo principal + `employee_auth_enabled=false` → `require_principal` 放行 | 第一道门 |
| 7 | `DEMO_MODE_ENABLED=true` + `employee_auth_enabled=true` → 仍 303 | D-1 语义 |
| 8 | `cmd_seed_demo`：demo_mode=false → 2；workspace 无 `demo-` 前缀 → 2 | D-1 唯一真实作用 |

---

## 8. 待拍板（不进本次实施）

| 编号 | 问题 | 选项 |
|---|---|---|
| D-1 | `DEMO_MODE_ENABLED` 与 `EMPLOYEE_AUTH_ENABLED` 并存语义 | A 启动告警 / B 拆分开关 |
| D-2 | 未登录访问 `/api/*` 应 303 还是 401 | A 保持 303（前端无感）/ B API 前缀改 401 JSON（与错误契约一致） |
| D-3 | 测试是否补齐 `SHOPIFY_*` 等开关 | A 补齐 / B 保持现状并记入 CI 说明 |

---

## 9. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-10-26 | 首版。依据 HEAD `632d1ab`；含 `.env` 泄露排查、店铺性质确认、demo 暴露面量化、D-1/D-2/D-3 三项发现 |
