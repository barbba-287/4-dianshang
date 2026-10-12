# 任务

> 生成时间：2026-10-10
> 生成方式：只读检查（`CLAUDE.md`、`电商ai.md`、`docs/engineering-roadmap.md`、`git status --short`、未提交改动 diff、内容生产相关代码与测试）
> 执行角色：本文件只做需求拆解与验收定义，代码实现由 Claude Code 负责
> 阶段归属：P1 模块边界（`docs/engineering-roadmap.md` 第二轮）

## 任务标题

**P1-内容导出应用边界：把「导出已审核内容版本」的业务编排从 `app/main.py` 抽到 `ContentApplicationService.export_revision()`，行为不变，并补齐该路径的契约测试与 API 回归测试。**

## 任务目标

1. `app/main.py` 的导出端点只保留 HTTP 语义（鉴权、请求头、参数、状态码、响应模型），导出编排下沉到 `ContentApplicationService`。
2. 让导出路径可脱离 FastAPI 单测（满足 roadmap P1 DoD：核心服务可脱离 FastAPI 单测）。
3. 为当前**零测试覆盖**的内容导出路径补齐正向、负向、越权、重复/幂等、故障/回放五类测试。
4. 严格保持既有对外行为：路由签名、`response_model`、`status_code=202`、错误码、`Idempotency-Key` 语义、`payload_hash` 兼容性均不变。

## 选择理由

| 依据 | 说明 |
|---|---|
| 直接命中 roadmap P1 优先切片 | `docs/engineering-roadmap.md:51` 明确要求「内容流程进入 `ContentApplicationService`」，且同轮列出的只读分析、同步编排已完成 |
| 同一模块已完成 2/3，导出是剩下的写编排 | `create_generation`、`review_revision` 已抽取并接线；`export_revision` 仍是 `main.py` 内联的 26 行业务编排（`app/main.py:2645-2670`） |
| 不新增领域行为，风险最低 | 本次只搬移编排位置，不新增状态、不改模型、不加迁移，可验证性最高（符合 `docs/engineering-roadmap.md:76-83` 变更排序原则） |
| 当前风险实际集中在该路径 | 该路径包含幂等键、`payload_hash`、入队、后台副作用、审批门禁，是内容主线中唯一「有副作用且无任何测试」的写路径 |
| 无外部调用方，改造面可控 | 前端 `app/templates/content.html` 只调用生成与审核接口，未调用导出端点；MCP 工具未暴露该端点 → 抽移不影响调用方 |
| 比继续搬只读查询价值更高 | `list_content_runs`/`get_content_run`/下载素材是纯查询，无幂等与后台副作用，风险与可验证性收益都低于导出 |

---

## 当前事实

### 已实现内容

| 项 | 事实 | 证据 |
|---|---|---|
| 分支与 HEAD | 分支 `feat/warehouse-mvp`，HEAD `d604043 完善电商运营与客服工作台` | `git status`、`git log --oneline -8` |
| `ContentApplicationService` 已存在 | 258 行，提供 `create_generation()`、`review_revision()`，稳定错误类 `ContentApplicationError(code, message)` | `app/content_application.py:26`、`:42`、`:182` |
| 已接线 | `main.py` 的 2 个路由已调用该服务，错误码→HTTP 映射写在路由 | `app/main.py:2488-2503`、`:2619-2640` |
| `SyncApplicationService`/`AnalyticsQueryService` 已存在 | 同批 P1 产出；同步服务同样输出 response mapper（`fixture_response_data`/`inventory_response_data`） | `app/sync_application.py:18`、`app/analytics_queries.py:24` |
| 导出编排仍在路由内 | `POST /api/content/revisions/{revision_id}/export`：校验 revision 已审批 → 幂等探测 → 建 `ProductContentExport` → `enqueue_job` → `_submit_future` | `app/main.py:2645-2670` |
| 导出读取仍在路由内 | `GET /api/content/exports/{export_id}` 仅按 workspace 查询 | `app/main.py:2673-2679` |
| 后台执行器已存在 | `content_export` 任务分派 → `_execute_content_export` → `execute_export_job`：复检 `approved`、写 `listing.json`/`listing.md`/`manifest.json`/`candidate-*.png`、计算 `artifact_uri/sha256/size`、`status=succeeded` | `app/background.py:308-309`、`:623-627`、`app/content_production.py:313-336` |
| 持久化模型 | `ProductContentExport`，唯一约束 `(workspace_id, idempotency_key)` | `app/db.py:931-935` |
| DTO | `ContentExportRequest{revision_id, format}`、`ContentExportResponse` | `app/schemas.py:1171-1188` |
| 权限码已存在但未使用 | `content.read/content.write/content.review/content.export` 已在 `ROLE_PERMISSIONS[operations]` 声明，但所有内容路由用 `roles=(ROLE_ADMIN, ROLE_OPERATIONS)`，未使用 `permission=` | `app/employee_auth.py:24-36`、`app/main.py:2647` |
| 导出无自动化测试 | `tests/` 下 `grep` `/api/content`、`ProductContentExport`、`ProductContentRevision` 均 0 命中 | `grep -rn`（本次会话实测） |
| 导出无前端/MCP 入口 | `content.html` 只调 `/api/content/runs`、`/api/content/runs/{job_id}`、`/api/content/revisions/{id}/review`；MCP 未暴露导出 | `app/templates/content.html:6-8`、`app/mcp_server.py` |
| 测试环境 | 项目运行环境为 `D:\python\python\python.exe`（Python 3.14.4，fastapi 0.141.1，pytest 8.4.2）；`tests/conftest.py` 强制 SQLite + `EMPLOYEE_AUTH_ENABLED=false` | 本次会话实测、`tests/conftest.py:12-15` |

### 当前未提交改动（`git status --short`）

| 状态 | 文件 | 说明 |
|---|---|---|
| `M` | `app/main.py` | P1 接线（sync 3 处、content 2 处、analytics 4 处），共 259 行变更 |
| `M` | `app/analytics_queries.py` | `AnalyticsQueryService` 调整（8 行） |
| `M` | `app/schemas.py` | 新增 3 行（指标/DTO 字段） |
| `M` | `tests/test_analytics_queries.py` | 新增 62 行契约测试 |
| `M` | `tests/test_api_product_quadrant.py` | 新增 27 行回归测试 |
| `??` | `CLAUDE.md` | 开发规范（未跟踪） |
| `??` | `app/content_application.py` | **本次任务要改的主文件（未跟踪，尚未提交）** |
| `??` | `app/sync_application.py` | 未跟踪 |
| `??` | `app/schemas.recovered.py` | 恢复文件，不得作为事实源或发布闭包 |
| `??` | `docs/interview-demo-script.md`、`面试/` | 个人资料，**不作为项目事实源**（`CLAUDE.md:42`）；不得删除/覆盖 |
| `??` | `tests/test_content_application.py`、`tests/test_sync_application.py` | 新增契约测试（未跟踪） |
| `??` | `问题解决资产库/` | 方法论资产，本次只读参考 |

> `app/content_application.py`、`tests/test_content_application.py` 均**未提交**。本任务在其上继续叠加改动，不得因为「文件未跟踪」而重建、覆盖或删除。

### 相关文件（行号为本次会话实测）

| 文件 | 角色 | 关键位置 |
|---|---|---|
| `app/main.py` | 待瘦身的接口层 | `:2645-2670` 导出 POST；`:2673-2679` 导出 GET；`:2521-2555` 内容任务/运行只读查询 |
| `app/content_application.py` | **主改造目标** | `:26` 错误类；`:36` 服务；`:128` 现有 `canonical_json` 哈希用法；`:182` `review_revision` |
| `app/content_production.py` | 领域/后台执行，**本次不改** | `:313-336` `execute_export_job`；`:318-320` 复检 `approved` |
| `app/background.py` | 任务分派，**本次不改** | `:308-309`、`:623-627` |
| `app/repository.py` | `enqueue_job`，**本次不改** | `:194-232`（`:230` 内部 `db.commit()`） |
| `app/db.py` | `ProductContentExport`，**本次不改** | `:931-953` |
| `app/schemas.py` | DTO，**本次不改** | `:1171-1188` |
| `app/employee_auth.py` | RBAC，**本次不改** | `:24-36`、`:257-291` |
| `tests/test_content_application.py` | 契约测试（追加） | 现有 1 条用例，使用极简 fake session |
| `tests/test_api_analytics.py` | **API 测试范式参考** | `:12-56` 建 workspace/user/membership + 登录 + `X-CSRF-Token` |
| `docs/engineering-roadmap.md` | 路线依据 | `:44-56` P1；`:99-110` 停止条件 |
| `电商ai.md` | 业务事实源 | `:208-219` V6-P1；`:293-297` 内容状态机 |

### Fact / Target / Assumption / Unknown

**Fact（本次会话已验证）**

| 编号 | 事实 |
|---|---|
| F1 | 导出编排 100% 在路由函数内（`app/main.py:2645-2670`），服务层无对应方法 |
| F2 | 导出路径无任何自动化测试（`tests/` 下 0 命中），是内容主线唯一有副作用且零覆盖的写路径 |
| F3 | 路由的幂等哈希为 `sha256(json.dumps(payload.model_dump(), ensure_ascii=False, sort_keys=True))`，使用 Python 默认分隔符 `", "` / `": "`；而 `content_application.py` 已导入的 `canonical_json` 使用紧凑分隔符 `,` / `:`。**同一 dict 两者哈希不同** |
| F4 | `enqueue_job()` 内部执行 `db.commit()`（`app/repository.py:230`），因此 `ProductContentExport` 行是被这次 commit 落库的；路由之后又 `db.commit()` 并 `db.refresh(export)` |
| F5 | 任务幂等键为 `f"export:{key}"`，与 export 行的 `idempotency_key=key` 不同；两者 `payload_hash` 相同 |
| F6 | 幂等重放路径直接返回既有行，**不重新提交后台任务**（无 `_submit_future`） |
| F7 | 演示模式（`demo_mode_enabled=True` 且 `employee_auth_enabled=False`）下，任何传 `roles=` 的端点直接返回 403；因此导出端点的 API 级测试必须启用员工登录鉴权并带 `X-CSRF-Token` |
| F8 | 跨 workspace 的 revision 在导出路由中表现为「查不到 approved revision」→ `409 CONTENT_NOT_APPROVED`（不是 404），不暴露其它租户是否存在 |
| F9 | 后台 `execute_export_job` 会**再次**校验 `revision.status == "approved"`，执行时校验是最终权威 |
| F10 | 导出成功后 `revision.status` **不会**迁移为 `exported`，与 `电商ai.md:294` 状态机文档的 `approved → exported/archived` 存在口径差异 |
| F11 | `export.format` 仅被持久化，`execute_export_job` 未读取它（固定输出 json+md+manifest+png） |
| F12 | 内容路由使用角色鉴权而非权限码；`content.export` 权限码已声明但无引用点 |
| F13 | 前端与 MCP 均未调用导出端点 → 无外部调用方阻塞本次抽移 |

**Target（本次要达成，非既成事实）**

| 编号 | 目标 |
|---|---|
| T1 | 导出编排进入 `ContentApplicationService.export_revision()`，`main.py` 仅保留 HTTP 语义 |
| T2 | 导出路径具备正向/负向/越权/重复幂等/故障回放五类测试（服务级 + API 级） |
| T3 | 服务可脱离 FastAPI 单测（`docs/engineering-roadmap.md:44-56` P1 DoD） |
| T4 | **（新增健壮性，需评审确认是否纳入）** 并发同键请求命中唯一约束时不再 500：按 `SyncApplicationService.retry_fixture_bundle()` 的既有模式捕获 `IntegrityError` → rollback → 重查 → 同哈希返回 `replayed=True`，异哈希返回 `IDEMPOTENCY_KEY_REUSE` |

**Assumption（本任务成立的前提，未逐一验证）**

| 编号 | 假设 | 若不成立的影响 |
|---|---|---|
| A1 | 抽移后既有 HTTP 契约（202、状态码映射、响应字段）可逐字保持 | 需追加兼容层或调整验收标准 |
| A2 | 后台线程在测试环境可用（`_submit_future` 生效），故障注入通过 monkeypatch 实现 | 故障类测试需改为直接调用 `execute_export_job` |
| A3 | 本次无 schema 变更，不需要 Alembic 迁移 | 需追加迁移任务与回滚验证 |
| A4 | 产品意图是 `format` 字段未来生效，但本次不改语义 | T 范围需重新界定 |

**Unknown（本次未验证，不得写成已完成）**

| 编号 | 未知项 | 说明 |
|---|---|---|
| U1 | 全量测试基线条数 | 本次为只读检查，未运行 pytest；执行者必须先跑一次并记录**实测**数字，不得沿用历史数字 |
| U2 | 生产/开发 MySQL 中是否已存在 `product_content_exports` 行需要哈希兼容 | 本地 SQLite 无法证明 → 因此**保守选择沿用旧哈希算法** |
| U3 | 并发场景在 MySQL 上的锁/唯一约束行为 | 本地 SQLite 只覆盖到「同一进程两次串行/并发提交」 |
| U4 | 真实 `wan` Provider 与真实平台授权状态 | 本次不触及，不得据此声称生产能力 |
| U5 | 导出失败补偿、取消、`revision→exported` 状态迁移的期望语义 | 涉及产品决策，另开任务 |

---

## 本次范围

### 允许修改的文件

| 文件 | 允许的改动 | 约束 |
|---|---|---|
| `app/content_application.py` | 新增 `export_revision()`；可选新增 `get_export()`；新增稳定错误码；如需可在模块内新增私有哈希辅助函数 | 不改 `create_generation()`/`review_revision()` 语义；不改类名/构造签名 |
| `app/main.py` | **仅** `export_content_revision()`（`:2645-2670`）与 `get_content_export()`（`:2673-2679`）两个函数体 | 路由装饰器、路径、`response_model`、`status_code`、函数签名、错误码→HTTP 映射保持不变 |
| `tests/test_content_application.py` | 追加服务级契约测试（沿用现有无 FastAPI 的 fake 风格） | 不删改现有用例 |
| `tests/test_api_content_export.py` | **新建**：导出端点 API 回归测试（登录 + CSRF，参考 `tests/test_api_analytics.py:12-56`） | 新建文件，不写入其它测试文件 |

### 不允许修改的文件

- `app/content_production.py`、`app/background.py`、`app/repository.py`、`app/db.py`、`app/schemas.py`、`app/employee_auth.py`、`app/security.py`、`app/visual_workflows.py`、`app/quality_gate.py`
- 其它 `tests/*.py`（含 `tests/conftest.py`）
- `migrations/`、`alembic.ini`、`init_db.py`
- `docs/*`（含本任务引用的 roadmap；文档更新另开任务）
- `CLAUDE.md`、`app/schemas.recovered.py`、`docs/interview-demo-script.md`、`面试/`、`问题解决资产库/`

### 明确不做的事情

1. 不做 `revision.status → exported` 状态迁移（F10 是已知口径差异，需独立决策）。
2. 不把角色鉴权切换为 `permission="content.export"`（F12；切换会改变鉴权契约，须独立评审）。
3. 不改任务幂等键规则（保持 `export:{key}`）、不引入 Outbox/队列表/新后台框架。
4. 不让 `format` 字段影响产物内容（F11/A4）。
5. 不新增 `request_id` 等数据库字段，不做 Alembic 迁移。
6. 不引入任何真实平台网络调用或真实 AIGC 调用；不得把 `local`/`local_compositor`/fixture 描述为生产能力。
7. 不重构 `create_generation()`/`review_revision()`，不做 `main.py` 其它区域的顺手清理。
8. 不改前端 `content.html`，不为导出新增 UI 入口（不得声称端到端闭环已具备）。

---

## 实施要求

### 数据流和状态边界

```text
POST /api/content/revisions/{revision_id}/export
  [路由层]  require_principal(roles=(ADMIN, OPERATIONS))
            Idempotency-Key 头读取
  → ContentApplicationService.export_revision(
        workspace_id, actor, revision_id(path), revision_id(body), format, idempotency_key)
      S1 路径与请求体 revision_id 不一致            → REVISION_MISMATCH
      S2 workspace 内且 status=approved 查 revision  → CONTENT_NOT_APPROVED
      S3 幂等探测 (workspace_id, idempotency_key)
            命中且 payload_hash 相同                 → 返回 replay（不建行、不入队、不提交后台）
            命中且 payload_hash 不同                 → IDEMPOTENCY_KEY_REUSE
      S4 新建 ProductContentExport(status=queued, simulated=revision.simulated, created_by=actor)
      S5 enqueue_job(type=content_export, source=content, keyword=format,
                     payload={"export_id": ...}, workspace_id,
                     idempotency_key=f"export:{key}", payload_hash=body_hash)
      S6 返回 {export, job, replayed}
  [路由层]  replayed=False → _submit_future(job.id)
            返回 ContentExportResponse（HTTP 202）
  [后台]    content_export → execute_export_job
            再次复检 revision.status == approved（最终权威）
            → 写 exports/{workspace_id}/{export_id}/listing.json|listing.md|manifest.json|candidate-*.png
            → artifact_uri / artifact_sha256 / artifact_size / status=succeeded
```

| 边界 | 规则 |
|---|---|
| 外部平台写入 | 无。本切片不触碰任何平台 API |
| 内部台账 | 不写库存/订单/采购台账；产出物只落 `artifacts/content/exports/{workspace_id}/{export_id}/` |
| 事实与派生 | `ProductContentRevision` 是派生版本；导出只是其审批后的再派生，不反向修改源快照 |
| 状态归属 | 导出不修改 `revision.version`、不修改 `revision.status`；`ProductContentExport.status` 由后台执行器推进 |

### Workspace / RBAC 边界

1. `workspace_id` 只允许来自 `principal.workspace_id`；服务方法**不接受**调用方自报租户值。
2. 服务内所有读取（revision 查询、幂等探测）都必须带 `workspace_id` 过滤；不得用「先按 id 查、再比对 workspace」的二段式写法。
3. RBAC 保持 `roles=(ROLE_ADMIN, ROLE_OPERATIONS)`（行为不变）。**不在本次**切换到 `permission=`。
4. 跨租户语义保持现状：他租户 revision 与「未审批 revision」返回同一错误码 `CONTENT_NOT_APPROVED`（F8），不泄露存在性。
5. 日志与错误信息不得包含 token/密钥/密码；错误 message 不含其它 workspace 的标识。

### 幂等、版本、事务和审计要求

| 要求 | 具体规则 |
|---|---|
| 幂等键 | 必填，长度 1–128。**双处校验**：路由保持现校验；服务内部也要求非空（`IDEMPOTENCY_KEY_REQUIRED`），防止绕过 HTTP 直调服务 |
| `payload_hash` 兼容 | **必须逐字节沿用旧算法**：`sha256(json.dumps({"revision_id":…, "format":…}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()`。**禁止**改用 `canonical_json`（F3：分隔符不同 → 已存在的导出行在重放时会误判 `IDEMPOTENCY_KEY_REUSE`）。若确需统一哈希算法，必须另开任务并说明存量数据处置 |
| 唯一约束 | `(workspace_id, idempotency_key)` 是幂等的最终防线；并发场景按 T4 处理（若纳入本次） |
| 版本 | `ProductContentExport` 无版本列 → 不新增乐观锁（记录为已知限制）；不修改 `revision.version` |
| 事务边界 | 保持现状顺序：`db.add(export)` → `db.flush()` → `enqueue_job`（内部 commit 落库）→ `db.commit()` → `db.refresh(export)`。**不得**把 `enqueue_job` 挪到事务外而改变可见性顺序；不得改成两次独立事务 |
| 审计 | `created_by=actor` 保留；`ProductContentExport` 无 `request_id` 列 → 本次不新增，记录为缺口；不得用日志替代审计字段 |
| 数据完整度 | 响应与记录中的 `simulated` 必须继承 `revision.simulated`；`local`/`mock` 一律 `simulated=true`，不得声明为真实生产能力 |

### 失败、重试、取消和人工接管要求

| 场景 | 现状（Fact） | 本次要求 |
|---|---|---|
| 后台执行失败 | `execute_export_job` 抛错由 background 记任务失败；`export.status` 无失败赋值（保持 `queued`） | 不改行为；在完成标准中显式记录为**已知限制**，不得声称「导出失败可自动补偿」 |
| 重试 | 同 `Idempotency-Key` 重放返回既有行；换新键可再次入队 | 保持；新增测试断言重放不产生第二个 job |
| 取消 | 现有取消端点只作用于爬取任务；`content_export` 无取消入口 | 本次不新增取消入口；在完成标准中记录人工接管方式为「等待 / 换新幂等键重新导出」 |
| 人工接管 | 无 Runbook 条目 | 本次不改 `docs/`；允许在交付说明里给出可复现的只读排查命令（查 export 行、查 job、查产物目录），Runbook 更新另开任务 |
| 外部依赖失败 | 无外部依赖 | 测试中禁止任何真实网络；AIGC 使用 `local` Provider |

---

## 验证命令

工作目录：`D:\ai\dianshang`。项目解释器为 `D:\python\python\python.exe`（3.14.4）。

**定向测试（本次改动的最小闭环）**

```bash
cd "D:/ai/dianshang"
D:/python/python/python.exe -m pytest -q -p no:cacheprovider tests/test_content_application.py tests/test_api_content_export.py
```

**相邻回归（内容/导出/质量门禁链路）**

```bash
cd "D:/ai/dianshang"
D:/python/python/python.exe -m pytest -q -p no:cacheprovider tests/test_content_application.py tests/test_api_content_export.py tests/test_quality_gate.py tests/test_exports.py tests/test_api.py
```

**契约兼容前置检查（证明 F3，抽取前先跑，抽取后必须仍成立）**

```bash
cd "D:/ai/dianshang"
D:/python/python/python.exe -c "import hashlib,json;p={'revision_id':1,'format':'json'};a=hashlib.sha256(json.dumps(p,ensure_ascii=False,sort_keys=True).encode()).hexdigest();b=hashlib.sha256(json.dumps(p,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest();print(a);print(b);print('same=',a==b)"
```

**全量测试（基线与收口；命令形式沿用 `docs/p0-0-release-review.md:20` 的记录）**

```bash
cd "D:/ai/dianshang"
PYTHONDONTWRITEBYTECODE=1 D:/python/python/python.exe -m pytest -q -p no:cacheprovider
```

> 执行顺序要求：**先跑一次全量并记录实测 passed 数与失败清单（U1）**，再做改动；改动后重跑，逐条对比差异。不得用历史数字（如文档中的 205）替代本次实测结果（`CLAUDE.md:39`）。

---

## 完成标准

### 1. 正向测试

| 编号 | 用例 | 预期 |
|---|---|---|
| P1 | 已 `approved` 的 revision + 合法 `Idempotency-Key` 调导出 | HTTP 202；`status="queued"`；`ProductContentExport` 新增 1 行且 `revision_id`/`format`/`created_by` 正确；`job.type="content_export"`、`run_id` 非空 |
| P2 | 响应契约 | 返回字段与既有 `ContentExportResponse` 完全一致（`id/revision_id/format/status/artifact_*/simulated/created_at/finished_at`），无新增/删除字段 |
| P3 | `simulated` 继承 | `provider=local` 的 revision → `export.simulated=True` |
| P4 | 后台执行（直调 `execute_export_job` 或等 `wait_for_jobs`） | `status="succeeded"`；`artifact_sha256`/`artifact_size` 非空；`manifest_json` 含 `content_hash`、`source_snapshot_hash`、资产 sha256 |
| P5 | 只读查询 | `GET /api/content/exports/{export_id}` 返回同一行（同租户）；他租户返回 404 `CONTENT_EXPORT_NOT_FOUND` |

### 2. 负向测试

| 编号 | 用例 | 预期 |
|---|---|---|
| N1 | revision 为 `draft`（未审批） | 409 `CONTENT_NOT_APPROVED`；**无** export 行、**无** job 入队 |
| N2 | 路径 `revision_id` 与请求体不一致 | 422 `REVISION_MISMATCH`；无副作用 |
| N3 | 缺失 `Idempotency-Key` | 422 `IDEMPOTENCY_KEY_REQUIRED`；无副作用 |
| N4 | `Idempotency-Key` 长度 129 | 422 `IDEMPOTENCY_KEY_REQUIRED`（保持现状码，不新增码）；无副作用 |
| N5 | revision 不存在 | 409 `CONTENT_NOT_APPROVED`（保持现状语义，**不改**为 404） |
| N6 | `format` 非法值 | 422（`ContentExportRequest` 的 Literal 约束）；无副作用 |

### 3. 越权测试

| 编号 | 用例 | 预期 |
|---|---|---|
| A1 | workspace B 的 revision，用 workspace A 主体导出 | 409 `CONTENT_NOT_APPROVED`；**A 租户内不新增任何 export 行** |
| A2 | 服务层直接传入非属主 `workspace_id` | 不返回任何数据、不落库 |
| A3 | 角色非 ADMIN/OPERATIONS（启用员工鉴权，如 `readonly`/`warehouse`） | 403 `FORBIDDEN` |
| A4 | 未登录（启用员工鉴权） | 303 重定向登录 / 401（保持现状，与中间件行为一致） |
| A5 | 他租户 export_id 走 `GET /api/content/exports/{id}` | 404，不泄露存在性 |

### 4. 重复 / 幂等测试

| 编号 | 用例 | 预期 |
|---|---|---|
| R1 | 同 key + 完全同 body 连续两次 | 第二次返回**同一** `export.id`；`replayed=True`；export 行总数 = 1；job 总数 = 1；**不再** `_submit_future` |
| R2 | 同 key + 不同 `format` | 409 `IDEMPOTENCY_KEY_REUSE`；无新行 |
| R3 | 同 key 重放后再进后台 | 产物不被重复生成（仍只有 1 组 `listing.*`/`manifest.json`） |
| R4 | 换新 key 再导出同一 revision | 允许新增第 2 行（现状行为，保留，不做去重） |
| R5（若 T4 纳入） | 两会话并发同 key | 仅 1 行 export + 1 个 job；一方返回 202、另一方返回重放语义（或 409 仅在同哈希不同）；**不得** 500 |

### 5. 故障 / 回放测试

| 编号 | 用例 | 预期 |
|---|---|---|
| F1 | `enqueue_job` 抛异常 | 错误语义稳定（错误码可辨），不产生半成品行（按现状事务顺序断言实际结果并记录） |
| F2 | `execute_export_job` 因 revision 被改为非 approved 而失败 | 抛 `CONTENT_NOT_APPROVED`（`app/content_production.py:318-320`），job 记为失败，`export.status` 仍为 `queued`（**已知限制，须在交付说明中写明**） |
| F3 | 后台失败后同 key 重放 | 不重复建行；返回既有行 |
| F4 | 同一 revision + 同一内容重复执行后台导出 | `artifact_sha256` 对同一输入可复现（除时间戳字段）；`manifest` 中 `content_hash`/`source_snapshot_hash` 稳定 |
| F5 | 回放/离线约束 | 全程无真实网络：不触发平台请求、不触发真实 AIGC（`provider ∈ {local, local_compositor}`） |

### 6. 真实、模拟和未知范围的区分

| 类别 | 内容 |
|---|---|
| **真实（本次可验证）** | SQLite 文件库上的 HTTP 与服务行为；员工登录 + CSRF 会话；后台线程执行；产物落盘；哈希一致性与兼容性 |
| **模拟（仅证明流程）** | `local`/`local_compositor` 生成的候选图与文案，仅代表流程可跑通，**不代表生成质量**；无真实平台授权、无真实 AIGC 调用 |
| **未知（本次不覆盖，不得写成已完成）** | MySQL/生产库并发与锁行为（U3）；生产存量导出行的哈希兼容（U2）；真实 `wan` Provider 行为（U4）；导出失败补偿、取消入口、`revision→exported` 状态迁移（U5）；`format` 字段生效语义（F11/A4）；端到端闭环（无 UI 入口，F13） |

### 7. 交付与门禁（`CLAUDE.md:23-35`）

- [ ] 输出/输入 Schema 未变，错误语义与错误码列表在交付说明中列出
- [ ] 本次改动文件清单 + 明确「未改动」清单（对照「本次范围」）
- [ ] 全量回归实测结果（命令、日期、passed 数、失败清单）写入交付说明，不写「已完成」除非门禁全通过
- [ ] `main.py` 导出函数体行数与职责收敛可量化（仅 HTTP 语义）
- [ ] 说明真实/模拟/未知边界；未通过项只能标记「进行中 / 待验收 / 可演示（模拟）」

---

## 禁止操作

| 禁止项 | 说明 |
|---|---|
| `git reset` | 任何形式（`--hard`/`--soft`/`--mixed`）均禁止 |
| `git clean` | 任何形式（含 `-fd`/`-fdx`）均禁止 |
| 删除或覆盖未跟踪文件 | 尤其 `CLAUDE.md`、`app/content_application.py`、`app/sync_application.py`、`app/schemas.recovered.py`、`tests/test_content_application.py`、`tests/test_sync_application.py`、`docs/interview-demo-script.md`、`面试/`、`问题解决资产库/` |
| `commit` | 禁止提交（分支推进由人工执行，模型见 `CLAUDE.md` 与契约 §6.1） |
| `push` | 禁止推送（含任何远端操作） |
| 外部平台写入 | 禁止任何平台侧写入；禁止开启真实平台网络 |
| 其它 | 不安装第三方依赖或 MCP 包；不执行 Alembic 迁移或 `init_db.py` 重建；不改 `.env`；不把 mock/fixture/local Provider 描述为生产能力；不改本文件以外无关区域 |

**交付方式**：本任务只新增/修改「本次范围」中列出的 4 个文件，其余文件保持字节不变。
