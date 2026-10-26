# ADR-0012 身份上下文与错误映射的统一

- 状态：accepted（2026-10-26，用户裁定：P1 不宣称完成；R1→R2 最小补完；`main.py` 路由拆分冻结移出本轮；随后进 P2）
- 日期：2026-10-26
- owner：规划方（WorkBuddy）
- scope：identity / 错误映射 / 全部 HTTP 边界
- 复核日期：2026-10-26
- 来源：`docs/p1-boundary-audit-20261012.md`（P1 边界盘点，§4–§6）

## 背景事实

以下均为 2026-10-12 / 2026-10-26 只读实测（详见盘点文档）：

- 路线图 P1 项 5/6/7 未达成：领域层仅剩 `employee_auth.py` 依赖 fastapi；无统一 `DomainError`；无单一类型化的请求上下文。
- 全仓 `raise HTTPException` 共 **96 处**，**全部**集中在 `app/main.py`（83）与 `app/employee_auth.py`（13），其余模块为 0；**96 处全部**带 `detail={"code": ..., "message": ...}` 形态，已形成事实错误码词表。
- `app/main.py` 内**唯一**的 `except HTTPException` 在中间件 CSRF 分支（`app/main.py:350`）。
- 身份模型双份：`app/security.py:10` 与 `app/employee_auth.py:48` 各有一个 `Principal`；`demo_principal`、`validate_auth_config` 各两份；由私有函数 `_as_employee_principal`（`app/employee_auth.py:244`）做**字段选择性拷贝**桥接。
- `app/main.py:175` 的 `Principal as ApiPrincipal` 为死导入。
- `app/security.py` **零测试覆盖**（`tests/` 下无文件引用）；`app.employee_auth` 被 16 个测试文件引用。
- `tests/` 中 **0 个文件**引用 `HTTPException` —— 错误形态迁移对现有测试不可见。
- 请求上下文的功能**已存在**：`request_context_middleware`（`app/main.py:299–363`）生成/回传 `request_id`、解析 principal、校验 CSRF、兜底 500。
- `source_mode` 在代码中是**数据级来源**字段（`shopify_live` / `platform` / `replay` / `fixture`，`app/` 下 162 处）。

## 目标与非目标

**目标**
1. 领域/应用层抛出的业务错误统一为单一异常类型，HTTP 形态只在一个地方产生。
2. 请求上下文（workspace / actor / request_id）收敛为**单一类型、单一来源**，身份模型收敛为**一个** `Principal`。
3. 对外 HTTP 契约（状态码、`{"detail": {"code", "message"}}` 外形、错误码词表）**完全不变**。

**非目标**
- 不拆分 `app/main.py` 的 120 个路由（见"决策 3"）。
- 不改变任何错误码的取值、语义或状态码。
- 不引入新的错误码词表或 i18n。

## 假设与未知

- **Assumption：** 96 处迁移是纯机械改写（同 code / 同状态码 / 同 message），可由全量回归（283 用例、零测试修改）充分验证。
- **Assumption：** API 消费方只依赖 `{"detail": {"code", "message"}}` 外形与状态码，不依赖异常类名。
- **Unknown：** 是否存在仓库外的脚本/客户端硬依赖 `HTTPException`（仓库内测试为 0，无法排除仓库外）。
- **Unknown：** `actor=principal.subject` 以可更改的登录串为审计键是否构成问题（未验证 `subject` 不可变性）。

## 候选方案

| 方案 | 说明 | 取舍 |
|---|---|---|
| A（采纳） | 新增 `DomainError` + 单一 exception_handler；96 处机械迁移；`RequestContext` 类型化并收敛 Principal | 行为不变、可增量、有全量测试护栏 |
| B | 只引入 `DomainError`，存量 96 处不迁移 | 改动最小，但项 7 未真正关闭，两套形态长期并存 |
| C | 保持现状，直接进 P2 | 零成本，但多租户隔离缺结构性保证，错误语义继续靠约定 |

## 决策

**决策 1（错误体系，R1 切片）：** 新增 `app/errors.py`，定义 `DomainError(code, message, http_status)`；`app/main.py` 注册**唯一**的 `DomainError` exception handler，输出与现状逐字节等价的 `{"detail": {"code", "message"}}`。96 处 `raise HTTPException(...)` 全部迁移为 `raise DomainError(...)`；中间件 CSRF 分支（`main.py:350`）改为捕获 `DomainError`。

**决策 2（身份上下文，R2 切片）：** `employee_auth.Principal` 为唯一身份类型；删除 `app/security.py` 的重复 `Principal` / `demo_principal` / `validate_auth_config` 与死导入；删除 `_as_employee_principal` 适配层；以类型化的 `RequestContext` 取代裸 `request.state` 透传。**R2 前置：** 补 `app/security.py` 与适配层的 contract test（先固定现状行为，再收敛）。

**决策 3（`main.py` 路由拆分，冻结）：** 移出本轮，不排期。理由：属可维护性收益，不降低业务风险；在"行为不变"约束下单位成本最高。若未来重启，限"按域机械搬迁 + 契约测试"。

**决策 4（`source_mode` 语义）：** `source_mode` 这个名字**保留给数据级来源字段**，现状语义不变；请求级上下文**不使用**该名字。若未来需要请求级来源模式，须另立字段名并在本 ADR 修订记录。理由：避免同名不同义（盘点文档 §6 的阻断项）。

## 取舍与影响

- R1 后 `employee_auth.py` 仍 import fastapi（`Request` 类型注解），项 5 为 ◐；R2 完成后变 ✓。
- 错误处理由"两文件各自抛 HTTPException"变为"抛领域异常 + 单点映射"，新增错误码时不再需要知道 HTTP 细节。
- 一次性迁移 96 处而非增量，是为避免两套形态长期并存（否决方案 B 的理由）；风险由"零测试修改 + 全量回归"覆盖。

## 安全、权限和数据边界

- 不改变鉴权、RBAC、CSRF、workspace 隔离的任何行为；只改变异常的**类型**，不改变**判定结果**。
- 401（`UNAUTHORIZED` / `WORKSPACE_CONTEXT_REQUIRED`）与 403（`FORBIDDEN` / `CSRF_INVALID`）的响应形态与状态码不变。

## 验证证据

- R1 验收：全量 `283 passed / 0 failed`，且 `git diff --stat tests/` 中**既有测试文件零修改**（仅允许新增 `tests/test_domain_error.py`）；`grep -rn "raise HTTPException" app/` 残留为 0。
- R2 验收：同一基线 + 新增 contract test 固定身份行为；`app/` 下 import fastapi 的文件数由 2 → 1（仅 `main.py`）。
- 基线命令与日期随交付说明登记（契约 §4.1）。

## 回滚/退出条件

- 回滚：R1 为单一切片，`git revert` 该提交即恢复原 96 处 `HTTPException`；无数据迁移、无外部副作用。
- 退出条件：全量回归出现任何非本切片引入的失败、或响应外形出现任何差异（状态码 / code / message / 头），立即停止并回滚。

## 关联代码、测试、Runbook

- 代码：`app/errors.py`（新增）、`app/main.py`、`app/employee_auth.py`
- 测试：`tests/test_domain_error.py`（新增）、既有 53 个测试文件（零修改）
- 任务文件：`dev_task.r1-domain-error.md`（R1）、R2 任务文件待 R1 验收后另立
- 来源文档：`docs/p1-boundary-audit-20261012.md`
