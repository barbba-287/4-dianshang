# P1 模块边界：现状盘点与范围裁定

- 生成时间：2026-10-12
- 生成方式：**只读检查**（`docs/engineering-roadmap.md`、`docs/adr-index.md`、`README.md`、`app/main.py`、`app/security.py`、`app/employee_auth.py`、`app/*application*.py`、`app/repository.py`、`app/db.py`、`tests/`、`.env`、`.github/`）+ `git log`/`git status`
- 生成角色：本文件只做**现状事实核对与范围裁定**，不写实现代码；实现由 Claude Code 负责
- 上位依据：`docs/engineering-roadmap.md`（P1 七项工作项，L48–54）、`CLAUDE.md`、`docs/claude-handoff-contract.md`
- 对接文档：`docs/review-checklist-20261011.md`（§5–§8 复核记录）

---

## 1. 结论摘要

**Fact：P1 阶段未完工。** 按路线图 P1 的 7 项工作项逐项核对，约 **3.5 / 7** 达成，其中 2 项完全未做、1 项未达成。

**Fact：此前的措辞把两个概念混同了。** `dev_task.p1-closeout.md` 让全量回归由 `282 passed + 1 failed` 变为 `283 passed + 0 failed`，那是**"P1 内容导出切片"的收官**；它不等于**"P1 阶段"完成**。本文件把两者分开。

**So what：** 若不先裁定，后续每个"P1 已完成"的前提都会传导到排期与 P2 进入判断上。

**建议：** 下一实现切片取「错误体系统一」，理由是它**同时关闭 P1 项 5 与项 7**，且改造面已被收窄到 **2 个文件 / 96 处**、有现成错误码词表、可增量落地。`main.py` 路由拆分建议**移出本轮**（成本最高、不降低业务风险）。

---

## 2. P1 七项逐项核对（Fact）

| # | 路线图工作项（L48–54） | 实测证据 | 判定 |
|---|---|---|---|
| 1 | `main.py` 只保留路由、鉴权、参数和响应 | `app/main.py` **2736 行**；路由装饰器 **120** 个；文件内 `select(` **49** 处；`Depends(` **93** 处 | ✗ 未达成 |
| 2 | 分析查询进入 `AnalyticsQueryService` | `app/analytics_queries.py` 存在，`main.py` 中引用 | ✓ 达成 |
| 3 | 同步编排进入 `SyncApplicationService` | `app/sync_application.py` 存在，`main.py` 中引用 | ✓ 达成 |
| 4 | 内容流程进入 `ContentApplicationService` | `app/content_application.py` 存在，`main.py` 中引用 | ✓ 达成 |
| 5 | 领域函数与 Repository 不依赖 FastAPI | `app/` 下**仅 2 个文件** import fastapi：`main.py`、`employee_auth.py`。`repository.py`、`db.py`、`analytics_queries.py`、`sync_application.py`、`content_application.py`、`replenishment.py` **均为 0** | ◐ 基本达成（`employee_auth.py` 待清） |
| 6 | 公共 `RequestContext` 传递 workspace / actor / request_id / source_mode | **已事实存在**（见 §4），但实现为「中间件 + `request.state` + 两套 `Principal` + 过渡适配层」，不是单一类型 | ◐ 部分达成 |
| 7 | 统一 `DomainError` → HTTP 错误码映射 | **无 `DomainError`、无统一异常处理器**；错误以 `raise HTTPException(..., detail={"code": ...})` **约定**表达 | ✗ 未达成（但改造面已收窄，见 §5） |

三项应用服务（项 2/3/4）已落地，是 P1 唯一**明确完成**的部分。

---

## 3. 关键发现 A：`main.py` 仍是编排中心

| 指标 | 数值 |
|---|---|
| 文件行数 | 2736 |
| 路由处理器 | 120 |
| 文件内 `select(` | 49 |
| `Depends(` | 93 |
| `principal` 引用 | 323 |
| `raise HTTPException` | 83（全仓 96 的 86%） |

**Fact：** 路由处理器内仍在直接构造 SQL 查询（49 处 `select(`），并把 `principal.workspace_id` 逐站手工下传（323 处 `principal`）。无 `app/routers/` 目录、无 `app/*router*` 文件。

**Assumption（未验证）：** 这 49 处查询未必都应下沉——部分可能只是"路由内单表读"，下沉与否需逐条判断。**本文件不预设结论。**

---

## 4. 关键发现 B：身份上下文「事实存在，但双份」

### 4.1 机制现状（Fact）

`app/main.py:299–363` 存在中间件 **`request_context_middleware`**，已实现：

1. 取 `X-Request-ID` 或生成 `req-<12 hex>` → 写入 `request.state.request_id`，并在所有响应（含 401/500）回写 `X-Request-ID`；
2. 解析身份：session（`principal_from_session`）或 API key（`authenticate_api_key`）→ 写入 `request.state.principal`；
3. 非 `/api/` 路径做登录重定向与 `demo_principal` 兜底；
4. 写方法校验 CSRF（`validate_csrf`）；
5. 未捕获异常兜底为 `500 {"code": "INTERNAL_ERROR"}` 并记日志。

消费端 `require_principal(request, *, permission, roles)`（`app/employee_auth.py:257`）从 `request.state.principal` 取身份并做权限判定，含 `WORKSPACE_CONTEXT_REQUIRED` 的 workspace 缺失拦截。

→ **所以 `RequestContext` 的功能不是"没有"，而是"以中间件 + 无类型 `request.state` 的形式存在"。**

### 4.2 代价：身份模型被拆成两份（Fact）

| 名称 | `app/security.py` | `app/employee_auth.py` |
|---|---|---|
| `Principal` | `:10`（字段：subject / tenant_id / key_id / scopes / workspace_id / auth_type） | `:48`（上列 + user_id / roles / warehouse_ids / session_id，并含 `has_role()` / `can()`） |
| `demo_principal` | `:54` | `:78` |
| `validate_auth_config` | `:63` | `:127` |

- 两者由 **`_as_employee_principal()`**（`app/employee_auth.py:244`）桥接；该函数把 API-key 形态的 principal **字段选择性拷贝**（不拷 `user_id`/`roles`/`warehouse_ids`/`session_id`）。
- `app/main.py:175` 有 `from app.security import Principal as ApiPrincipal`，但 **`ApiPrincipal` 全文件仅此一处出现 → 死导入**。
- 同时 `app/main.py:189` 把 `employee_auth.validate_auth_config` 别名导入为 `validate_employee_auth_config`，与 `app.security.validate_auth_config`（`:198` 局部导入）并存 —— **同名函数被别名区分**。

**判定：** 路线图 L56 写明"不在此轮搬迁所有模型文件；**先用适配层和 contract tests 降低风险**"。因此"双 Principal + 适配层"**符合本轮既定策略**，不是实现方失误。真正的缺口是：**该过渡态从未被排期收敛**，且适配层是私有函数（`_` 前缀）、无 contract test 固定其行为——一旦 `security.Principal` 增字段，会被静默丢弃。

### 4.3 覆盖度缺口（Fact）

| 模块 | 被测试文件引用 |
|---|---|
| `app/security.py` | **0 个**（`tests/` 下无任何文件引用 `app.security`） |
| `app.employee_auth.py` | **16 个** |

`tests/` 共 54 个 `.py`（53 个 `test_*.py` + `conftest.py`）。**`security.py` 是当前身份路径上唯一零测试覆盖的模块。**

### 4.4 actor 的现状（Fact，附观察）

- `actor=` 赋值 **25** 处，约定为 `actor=principal.subject`（如 `app/main.py:1623/1652/1660`）。
- 无 `actor_id` 字段。**观察（非结论）：** 审计以 `subject`（可更改的登录串）为键，而非稳定 `user_id`；是否构成问题取决于 `subject` 的不可变性，本文件未验证。

---

## 5. 关键发现 C：错误映射是「约定」而非「机制」——但改造面比预想小

| 指标 | 数值 |
|---|---|
| 全仓 `raise HTTPException` | **96**（`main.py` 83 + `employee_auth.py` 13） |
| 其他模块 `raise HTTPException` | **0** |
| 唯一 `exception_handler` | `main.py:366`，且只处理 `RequestValidationError` → 422 |
| `DomainError` / `ApiError` | 全仓 **0** 处 |

**Fact：** 错误以 `raise HTTPException(status_code=..., detail={"code": "<SCREAMING_SNAKE>", "message": ...})` 表达，已形成**事实词表**（如 `FORBIDDEN`、`UNAUTHORIZED`、`PRODUCT_NOT_FOUND`、`WORKSPACE_CONTEXT_REQUIRED`、`CONTENT_ASSET_NOT_FOUND`、`CSRF_INVALID`、`IDEMPOTENCY_KEY_REQUIRED` 等）。

**So what（这是本文件最有行动价值的判断）：** 该切片不是"从零引入错误体系"，而是**把既有约定形式化**——
- 只需定义 `DomainError`（携带 `code` / `status_code` / `message`）+ 注册**一个**异常处理器；
- `raise HTTPException` 集中在 **2 个文件 / 96 处**，可分批迁移、新旧并存；
- 迁移完成后 `employee_auth.py` 不再 import fastapi → **P1 项 5 由 ◐ 变 ✓**，项 7 由 ✗ 变 ✓。
  （`employee_auth.py` 现从 fastapi 取 `HTTPException`（13 处）与 `Request`（3 处类型注解），共 337 行。）

---

## 6. 关键发现 D：`source_mode` 语义需先对齐，避免误改

路线图 P1 项 6 要求 `RequestContext` 携带 `source_mode`。**Fact：代码中 `source_mode` 的既有语义是"数据来源"**——取值如 `shopify_live`、`platform`、`replay`、`fixture`，落在 `ExternalSyncRun.source_mode`、`ProductMediaAsset.source_mode`、`TicketEvent.source_mode` 等**数据字段**上（`app/` 中 162 处）。

→ **Assumption：** 路线图作者在写"RequestContext 携带 source_mode"时，指的是**请求级来源模式**（本次请求走真实平台还是回放），而**不一定是**上述数据级字段。**两者同名不同义。**

**结论：** 在动手前必须先对齐语义，否则极易把请求级字段写进数据模型（或反之）。**这是项 6 的一个未澄清前置问题，记为 Unknown。**

---

## 7. 关键发现 E：周边环境事实

| 项 | 事实 |
|---|---|
| CI | **不存在** `.github/workflows`。路线图 P11 才把 CI 列为交付物 |
| P2 入口条件 | `.env` 中 `SHOPIFY_LIVE_ENABLED=true`，`SHOPIFY_STORE_DOMAIN` / `SHOPIFY_ACCESS_TOKEN` 均在（Dev Store Canary）；`TAOBAO_ADAPTER_ENABLED=false`。**"至少一个平台测试授权"疑似满足**（未实跑验证） |
| README 收口项 | README 已更新为 `283 passed`（不再陈旧）；仍列 `app/assistant.py`、`app/business_dates.py` 与新文档**未进发布清单** → 干净 clone 可复现性尚不能宣称 |
| ADR 覆盖 | `docs/adr-index.md` 已登记 ADR-0001…0011。**无**关于身份上下文（RequestContext）与错误映射的 ADR |
| 项目设置文本 | 项目级 instructions 仍写「四级分支规范：develop → staging → temp-prod → main」，与仓库现实（仅 `main` / `feat/<slice-id>`）**冲突**，每次会话注入 |

---

## 8. 对上一轮结论的更正（自我校对）

上一轮我给出的表述是「`RequestContext` 全仓 0 处 → 完全未做」。

**该表述只在"标识符名称"层面成立，在"功能"层面不准确。** 事实是：功能由 `request_context_middleware`（`main.py:299`）+ `request.state` + `require_principal` 承载，`request_id` 已生成并回传。**故本文件把项 6 判定改为 ◐ 部分达成，而非 ✗。** 记录此更正以免下游误按"从零新建"开工。

---

## 9. 范围裁定建议

| 残留块 | 建议 | 理由 |
|---|---|---|
| **错误体系统一**（`DomainError` + 单一处理器） | **做，作为下一实现切片（R1）** | 改造面小（2 文件 / 96 处）、有现成词表、可增量；**一次关闭项 5 与项 7**；风险低、易测 |
| **身份上下文统一**（`Principal` 合一 → 单一 `RequestContext`；去 `_as_employee_principal` 过渡层；补 `security.py` 测试） | **做，排在 R1 之后（R2）** | 直接支撑项目硬约束"所有查询强制带 workspace_id"；当前靠 323 处手工下传 + 逐路由 `require_principal`，缺结构性保证。风险中高（触 `main.py`） |
| **`main.py` 路由拆分**（120 路由 → routers） | **移出本轮，显式记录理由** | 成本最高、且不降低业务风险（属可维护性收益）。若要做，应限"按域机械搬迁 + 行为不变 + 契约测试" |

**前置条件（阻断项 6 开工）：** §6 的 `source_mode` 语义先对齐。

**替代路线（并列选项，非推荐）：** 冻结 P1 残留并**显式记录**（"已知未完成，理由 = 优先业务价值"），直接进入 **P2 外部事实生产化**（入口条件疑似已满足）。

**建议的副产品：** 项 6/7 涉及跨模块契约与权限边界，按 `docs/adr-index.md` 的门槛，宜补 **ADR-0012（身份上下文与错误映射）**，避免第三次重复讨论。

---

## 10. 待用户决策的开放问题

1. P1 是「补完再走 P2」还是「冻结残留、记录理由、进 P2」？
2. 若补完：是否接受建议顺序 **R1 错误体系 → R2 身份上下文**？`main.py` 路由拆分是否确认移出本轮？
3. §6 的 `source_mode` 语义由谁裁定（是否即"请求级来源模式"）？
4. 是否补 ADR-0012？
5. 项目级 instructions 里的四级分支文本是否修正？（与仓库现实冲突）

---

## 11. 风险登记（待并入 `docs/review-checklist-20261011.md`）

| 编号 | 级别 | 描述 | 建议 |
|---|---|---|---|
| R-11 | P1（边界） | P1 阶段被口头当作"已收官"，实际 3.5/7；若不自查会传导到 P2 进入判断 | 本文件即处置；后续在 README/路线图标注 P1 实际状态 |
| R-12 | P2（安全） | `app/security.py` **零测试覆盖**，却是身份路径一环；`_as_employee_principal` 为私有适配层且无 contract test，字段静默丢弃 | R2 切片内补测试；过渡期至少为适配层加 contract test |
| R-13 | P3（记录） | `app/main.py:175` `Principal as ApiPrincipal` 为死导入；两模块存在 `Principal` / `demo_principal` / `validate_auth_config` 三组同名定义，靠别名区分 | 随 R2 收敛 |
| R-14 | P3（文档） | `source_mode` 在路线图（请求级）与代码（数据级）**同名不同义** | 开工前对齐并写进 ADR-0012 |

---

## 12. 变更记录

| 日期 | 变更 | 说明 |
|---|---|---|
| 2026-10-12 | 新建 | P1 边界现状盘点与范围裁定首次生成；含对上一轮"RequestContext 全仓 0 处"表述的更正（§8） |
| 2026-10-26 | 裁定 | 用户裁定见 §13；R1 任务文件与 ADR-0012 落地 |

## 13. 裁定记录（2026-10-26，用户）

1. **P1 不宣称完成**——对外表述以本文件 §2 的 3.5/7 为准。
2. **R1 → R2 最小补完**：R1 = 错误体系统一（`dev_task.r1-domain-error.md`）；R2 = 身份上下文统一（待 R1 验收后另立任务文件）。
3. **P1 项 1（`main.py` 路由拆分）冻结移出本轮**，不排期。
4. **补 ADR-0012**：已落地 `docs/adr-0012-identity-context-and-error-mapping.md`（含 §6 `source_mode` 语义裁定：该名保留给数据级字段，请求级上下文不复用）。
5. R1/R2 验收后**进入 P2 外部事实生产化**。
