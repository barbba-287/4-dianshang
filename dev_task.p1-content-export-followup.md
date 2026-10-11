# 任务

> 生成时间：2026-10-11
> 生成方式：基于终审复核结论（`docs/review-checklist-20261011.md` §4）
> 执行角色：本文件只做需求拆解与验收定义，代码实现由 Claude Code 负责
> 阶段归属：P1 模块边界（`docs/engineering-roadmap.md` 第二轮）收尾
> 上位依据：`CLAUDE.md`、`docs/claude-handoff-contract.md`

## 任务标题

**P1 内容导出切片收尾：修复 `CONTENT_EXPORT_PERSIST_FAILED` 错误码映射一致性，并为导出后台失败补齐可观测性（不改变对外 HTTP 契约的错误码集合，仅调整失败态记录）。**

## 任务目标

1. 消除 `ContentApplicationService` 与 `SyncApplicationService` 同类持久化/执行失败的 HTTP 状态码不一致（前者 422，后者 500）。
2. 为「导出后台执行失败」补齐可观测状态，使 `ProductContentExport.status` 不再永久停留在 `queued`。
3. 全程不改变已核验通过的对外契约：不得改变 `87000`+ 既有错误码语义、不得改变成功路径响应字段、不得改变鉴权与幂等行为。

## 选择理由

| 依据 | 说明 |
|---|---|
| 终审复核遗留的唯一功能性 P2 | `docs/review-checklist-20261011.md` §4.4 P2-3：同一协作批次两个 Application Service 对同类失败给出不同状态码，属契约一致性缺口 |
| 上两轮已连续两轮挂账 | 首轮复核列为 P3，本轮复核再次确认未修 → 继续顺延会被"看起来完整"掩盖 |
| 后台失败态是真实可观测性缺口 | `execute_export_job` 失败时 `export.status` 保持 `queued`（`app/content_production.py:313-336`），运维无法区分"排队中"与"已失败" |
| 改动面小、可验证性高 | 涉及 2 处错误码映射 + 1 处失败态赋值，无 schema 变更、无迁移 |
| 不阻塞的内容已排除 | 测试证据入库与 shim 说明已由用户修复完成（`.gitignore` 白名单 + 契约 §6.2 环境限制说明） |

## 当前事实

### 已实现内容

| 项 | 事实 | 证据 |
|---|---|---|
| 基线 | commit `87533ce`（16 文件），工作区仅 `docs/review-checklist-20261011.md`、`artifacts/test-full-run-20261011.txt` 未跟踪（本轮已按白名单入库） | `git log`、`git status` |
| 终审结论 | **有保留通过**，上轮 P0/P1/P2 三项全部闭合 | `docs/review-checklist-20261011.md` §4.1/§4.5 |
| 导出编排已下沉 | `ContentApplicationService.export_revision()`，`flush()` 已在 `try` 内 | `app/content_application.py:224-264` |
| 并发兜底已生效 | 独立探针实测：真实约束 + 真实 HTTP → `409 IDEMPOTENCY_KEY_REUSE`，非 500 | 复核 §4.3 |
| 错误码映射现状 | 导出：`CONTENT_NOT_APPROVED`409 / `REVISION_MISMATCH`422 / `IDEMPOTENCY_KEY_REUSE`409，其余默认 422 | `app/main.py:2662-2668` |
| 同步同类错误映射 | `EXTERNAL_SYNC_FAILED` → 500，其余 422 | `app/main.py`（`ingest_inventory` 路由） |
| 后台失败态缺口 | `execute_export_job` 抛错时无 `export.status` 失败赋值，停留在 `queued` | `app/content_production.py:313-336`、复核清单已知限制 |
| 已有失败态字段 | `ProductContentExport` 已有 `error_code` / `error_message` 列（当前未被后台写入） | `app/db.py:950-951` |
| 服务层失败码 | `CONTENT_EXPORT_PERSIST_FAILED` 已是稳定错误码 | `app/content_application.py:258` |
| 测试基线 | `280 passed / 0 failed / 0 error`（交付方证据，stdout 哈希已独立验证；全量独立复跑未完成，见复核 §4.2） | `artifacts/test-full-run-20261011.txt` |

### 当前未提交改动

| 状态 | 文件 | 说明 |
|---|---|---|
| `M` | `.gitignore` | 新增 `!artifacts/test-*.txt` 白名单（用户已修 P2-1） |
| `M` | `docs/claude-handoff-contract.md` | 新增 §6.2 环境限制说明（用户已修 P2-2） |
| `??` | `docs/review-checklist-20261011.md` | 终审清单（含复核结论 §4） |
| `??` | `artifacts/test-full-run-20261011.txt` | 测试证据（现已可入库） |

### 相关文件

| 文件 | 角色 |
|---|---|
| `app/main.py` | 错误码映射（`export_content_revision`） |
| `app/content_application.py` | 服务层错误码产出 |
| `app/content_production.py` | `execute_export_job` 失败态（**本次不修改，需评审决定**） |
| `app/background.py` | 任务失败记录 |
| `tests/test_api_content_export.py` | API 回归（追加失败态用例） |
| `tests/test_content_application.py` | 服务契约（追加映射用例） |

### Fact / Target / Assumption / Unknown

**Fact**

| 编号 | 事实 |
|---|---|
| F1 | 导出服务层已产出稳定错误码 `CONTENT_EXPORT_PERSIST_FAILED`，但路由将其映射为 422（`status_by_code.get(exc.code, 422)` 兜底） |
| F2 | 同批 `SyncApplicationService` 对服务端执行失败（`EXTERNAL_SYNC_FAILED`）映射 500 |
| F3 | `ProductContentExport` 已有 `error_code` / `error_message` 列，当前无写入点 |
| F4 | `execute_export_job` 失败时 `export.status` 保持 `queued`，无失败态 |
| F5 | 后台任务失败由 `app/background.py` 记录到 `CrawlJob`，与 `ProductContentExport.status` 解耦 |
| F6 | H6 并发兜底已在真实 HTTP + 真实约束下独立验证通过，本次不得改动该逻辑 |

**Target**

| 编号 | 目标 |
|---|---|
| T1 | 同类持久化失败的状态码在两个服务间一致（选定一个口径并同步） |
| T2 | 导出后台失败可观测：`export.status` 有失败态，`error_code` 有值 |
| T3 | 对外成功路径契约零变化（字段、状态码、幂等重放行为） |

**Assumption**

| 编号 | 假设 | 若不成立 |
|---|---|---|
| A1 | 用户接受「持久化失败归 422（客户端可重试）」或「归 500（服务端错误）」其中一种口径 | 需先拍板口径 |
| A2 | 新增失败态取值（如 `failed`）不与既有消费方冲突（前端/MCP 均未消费导出状态） | 需追加兼容处理 |
| A3 | `execute_export_job` 失败态写入不改变其"最终权威校验 approved"行为 | 需重新界定 |

**Unknown**

| 编号 | 未知项 |
|---|---|
| U1 | 生产/开发库是否已有 `queued` 状态的僵尸导出行需要回填（本地无法证明） |
| U2 | 导出失败是否需要自动补偿或重试（涉及产品决策，本次不做） |
| U3 | 失败态是否需要指标/告警（建议另开任务） |

## 本次范围

### 允许修改的文件

| 文件 | 允许改动 |
|---|---|
| `app/main.py` | `export_content_revision()` 的错误码映射表 |
| `app/content_application.py` | 仅在需要调整失败码命名/语义时改动（不改变成功路径） |
| `tests/test_api_content_export.py` | 追加失败态与映射用例 |
| `tests/test_content_application.py` | 追加映射一致性用例 |

### 不允许修改的文件

- `app/db.py`（`error_code` / `error_message` 列已存在，无迁移）
- `app/background.py`、`app/repository.py`、`app/employee_auth.py`、`app/security.py`
- `migrations/`、`alembic.ini`
- 已核验通过的并发兜底逻辑（`app/content_application.py:236-261`）
- `docs/claude-handoff-contract.md`、`CLAUDE.md`、`docs/review-checklist-20261011.md`（已完成本轮修订）

### 明确不做的事情

1. 不改 `execute_export_job` 的产物生成与 `approved` 复检逻辑。
2. 不新增 `revision.status → exported` 迁移（仍属独立决策，U5）。
3. 不引入自动重试/补偿/死信队列。
4. 不做 `queued` 僵尸行回填（U1，需生产数据确认）。
5. 不改前端与 MCP（导出端点无消费方）。
6. 不触发任何外部平台或真实 AIGC 调用。

## 实施要求

### 数据流和状态边界

```text
成功路径（保持不变）：
POST export → 服务 S1-S6 → ProductContentExport(status=queued) → job → 后台
  → execute_export_job 成功 → status=succeeded, artifact_sha256/size, manifest_json

本次新增（失败路径可观测）：
  → execute_export_job 失败（如 revision 非 approved）
      → export.status = failed
      → export.error_code = <稳定码>        （复用已有列，不新增迁移）
      → export.finished_at 记录时间
  ⚠ 不得改变 CrawlJob 的失败记录行为（F5）
```

### Workspace / RBAC 边界

1. 不改变任何鉴权与租户过滤逻辑；本次不触碰 `require_principal`。
2. 失败态写入必须带 `workspace_id` 定位，禁止跨租户写入。

### 幂等、版本、事务和审计要求

| 要求 | 规则 |
|---|---|
| 幂等 | 不得改变 `(workspace_id, idempotency_key)` 唯一约束行为与重放语义（R1/R2/R4 必须仍通过） |
| 版本 | `ProductContentExport` 无版本列，不新增乐观锁 |
| payload_hash | 不得改动哈希算法（沿用默认 `json.dumps` 分隔符） |
| 事务 | 失败态写入必须独立事务提交，避免与产物写入耦合导致部分提交 |
| 审计 | `error_code` / `error_message` 不得包含 token、密钥、PII；`error_message` 需截断长度 |
| 幂等键重放 | 已失败导出再次同键请求的处理语义需明确并测试（见完成标准 N3） |

### 失败、重试、取消和人工接管要求

| 场景 | 要求 |
|---|---|
| 失败 | `export.status` 必须反映真实结果，不得停留在 `queued` |
| 重试 | 本次不实现自动重试；同键重放语义必须明确（返回既有失败行 or 稳定错误），并写入契约说明 |
| 取消 | 本次不新增取消入口（维持现状，属 U 项） |
| 人工接管 | 交付说明中需给出只读排查命令（查 export 行状态/error_code、查对应 job、查产物目录） |

## 验证命令

```bash
cd D:/ai/dianshang
# 定向
PYTHONDONTWRITEBYTECODE=1 D:/python/python/python.exe -m pytest -q -p no:cacheprovider \
  tests/test_content_application.py tests/test_api_content_export.py

# 相邻回归（同步服务映射一致性）
PYTHONDONTWRITEBYTECODE=1 D:/python/python/python.exe -m pytest -q -p no:cacheprovider \
  tests/test_sync_application.py tests/test_api_sync_compensation.py tests/test_api_inventory.py

# 全量
PYTHONDONTWRITEBYTECODE=1 D:/python/python/python.exe -m pytest -q -p no:cacheprovider \
  --basetemp=D:/ai/dianshang/.testtmp
```

> 环境限制：若运行环境存在 `sitecustomize.py` 类 shim 劫持 `shutil.rmtree`，`--basetemp` 清理阶段会报
> error（非断言失败）。参见 `docs/claude-handoff-contract.md` §6.2。出现该现象时须在无 shim 环境复跑，
> 且**不得**把此类 error 当作测试失败或将 `0 errors` 写成"已独立复现"。

## 完成标准

### 1. 正向测试

| 编号 | 用例 | 预期 |
|---|---|---|
| P1 | 成功导出（同 `87533ce` 既有用例） | 202；`status=queued`；后台后 `succeeded`；响应字段集合不变 |
| P2 | 后台成功时 `error_code` 保持 `None` | 不得因本次改动被写入 |

### 2. 负向测试

| 编号 | 用例 | 预期 |
|---|---|---|
| N1 | 映射一致性：同类持久化失败在导出与同步两侧状态码一致 | 二者相同（口径见 A1） |
| N2 | 后台失败（revision 被改为非 approved） | `execute_export_job` 仍抛 `CONTENT_NOT_APPROVED`；`export.status=failed`；`error_code` 非空 |
| N3 | 已失败导出的同键重放 | 语义明确且稳定（返回既有失败行 或 稳定错误），不得产生新行/新 job |
| N4 | `error_message` 长度与敏感信息 | 截断生效；无 token/密钥/PII |

### 3. 越权测试

| 编号 | 用例 | 预期 |
|---|---|---|
| A1 | 跨租户导出仍 `409 CONTENT_NOT_APPROVED` | 无新增 export 行 |
| A2 | 他租户 `GET /api/content/exports/{id}` 仍 404 | 不泄露存在性 |
| A3 | readonly 角色仍 403 | 不变 |

### 4. 重复 / 幂等测试

| 编号 | 用例 | 预期 |
|---|---|---|
| R1 | 同键同内容重放 | 返回同一 export；`replayed=True`；行数=1；job 数=1 |
| R2 | 同键不同内容 | `409 IDEMPOTENCY_KEY_REUSE`；无新行 |
| R3 | 并发同键（真实约束） | **不得回归**：仍 `409 IDEMPOTENCY_KEY_REUSE`，非 500（复核 §4.3 已建立的基线） |

### 5. 故障 / 回放测试

| 编号 | 用例 | 预期 |
|---|---|---|
| F1 | 后台失败后导出状态可查 | `status=failed` + `error_code`；运维可区分 `queued`/`failed` |
| F2 | 失败态写入独立事务 | 产物写入与状态写入互不产生部分提交 |
| F3 | 全流程无真实网络 | 不触发平台请求与真实 AIGC（`provider ∈ {local}`） |

### 6. 真实、模拟和未知范围的区分

| 类别 | 内容 |
|---|---|
| **真实** | SQLite 上的 HTTP 与服务行为、后台线程、真实唯一约束冲突、状态写入 |
| **模拟** | `local` Provider 生成物，仅证明流程 |
| **未知** | 生产 `queued` 僵尸行分布（U1）；是否需要自动补偿（U2）；失败率指标/告警（U3）；MySQL 并发锁行为 |

## 禁止操作

| 禁止项 | 说明 |
|---|---|
| `git reset` | 任何形式 |
| `git clean` | 任何形式 |
| 删除或覆盖未跟踪文件 | 尤其 `docs/review-checklist-20261011.md`、`artifacts/test-full-run-20261011.txt`（本轮待入库） |
| `commit` / `push` | 由人工按 `develop → staging → temp-prod → main` 推进 |
| 外部平台写入 | 禁止任何平台侧写入与真实网络 |
| 其它 | 不安装依赖/MCP 包；不改 `.env`；不做 Alembic 迁移；不改已核验通过的并发兜底逻辑；不把 mock/fixture/local 描述为生产能力 |
