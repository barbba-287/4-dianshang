# dev_task.p3s1-purchase-approval — P3-S1 采购单审批（含生成入库单）

> 生成时间：2026-10-29（v1.1 定稿，Q1–Q5 已裁定，可开工）
> 生成方式：WorkBuddy 只读调研（`db.py` PurchaseRequest/PurchaseRequestLine/PurchaseRequestAction、
> `replenishment.py` draft/edit/submit 三个既有服务、`main.py:3119-3210` 六个采购路由、
> `_replenishment_error` 错误映射、`identity.py` ROLE_PERMISSIONS / Principal.can、`employee_auth.py`
> require_principal / require_warehouse_access、`repository.py create_inbound`、`index.html:100`
> 既有采购工作流区块）
> 执行角色：Claude Code（实施）；WorkBuddy（复核）
> 阶段归属：P3 主体 · 切片 1/3 —— **零 live 依赖，全程本地 SQLite**
> 上位依据：`docs/p3-plan-20261029.md`（三切片）；用户 2026-10-29 U1–U6 裁定（见 §0）；
> `docs/review-checklist-20261011.md` §19.4（P2 收官，基线 435/0）

## 0. 用户裁定落位（U1–U6，实施方不得偏离）

| # | 裁定 | 本片落位 |
|---|---|---|
| U1 | 申请与采购单**同实体**，暂不拆分 | **不新建 PurchaseOrder**；只给 `purchase_requests` 加审批列（T1）。禁建新表、禁数据搬迁 |
| U2 | 新增 `purchasing.approve`，**独立于** `purchasing.write` / `purchasing.submit` | T2：approve/reject 端点权限 = `purchasing.approve`；cancel 仍用 `purchasing.submit`；三者互不复用 |
| U3 | 禁止自审（制单人 ≠ 审批人） | T3 红线：`principal.subject ∈ {created_by, submitted_by}` → **409 SELF_APPROVAL_FORBIDDEN**，零写入 |
| U4 | 超收一律拒（received + damaged > 预期 → 409） | **本片只做生成侧对齐**（`expected_qty = requested_qty`，T4）；**实收侧校验闸落 S2**，理由见 §11 Q4 |
| U5 | 只做撤回（cancel），**严禁物理 DELETE** | T2：`status=cancelled` + `cancelled_by/cancelled_at`；全片零 `DELETE` 语句（§8 红线可 grep 验证） |
| U6 | 四级分支**不启用**，维持 `main` + `feat/*` | 本文件不涉及分支操作；推送仍按「阶段收口」由复核方执行 |

### 0.1 二次评审裁定（Q1–Q5，2026-10-29 定稿，实施方不得偏离）

| # | 裁定 | 本片落位 |
|---|---|---|
| Q1 | `purchasing.approve` 授予 **operations + admin** | `identity.py` 的 `ROLE_OPERATIONS` 集合**只增** `"purchasing.approve"` 一项；admin 走 `has_role` 短路（`ROLE_ADMIN` 为空集，`identity.py:72-75`），不加任何权限项 |
| Q1b | **测试必须双账号**：严禁为"一个账号跑通"在测试里去掉 U3 红线 | 新增 **§6.1 测试夹具红线**（`maker_checker_fixture`：两个独立账号 + 两个独立登录会话；唯一允许的等价 = `created_by` 指向不存在的 login） |
| Q2 | `rejected` **终态，不许重开**；重办 = 新建 draft 并**关联旧单** | T2 写死：`rejected` 对任何决策动作 409；新增 `supersedes_request_id` 列承载血缘（T1），**旧单状态永不回拨**、不做级联 |
| Q3 | 撤回后建议回退 `modified` — **采纳** | T3.7 原文不变；代价登记 R-51（`confirmed`/`modified` 区分丢失，审计可见两条 action） |
| Q4 | U4 超收校验落 **S2**；S1 生成侧必须 `expected_qty == requested_qty` **精确相等** | T4 明细规则（已写死）；A-14 逐 SKU 断言相等，不做任何取整/合并/上限裁剪 |
| Q5 | cancel 也要求 `Idempotency-Key`，**approve / reject / cancel 三者同构** | T3 步骤 2 对三者一律必填（缺失/超长 → 422 `IDEMPOTENCY_KEY_REQUIRED`）；既有唯一约束 `(workspace_id, purchase_request_id, action_type, idempotency_key)` 天然容纳三种 action，**不新增约束** |

## 1. 目标（四个，不扩围）

- **T1 审批字段落库**（migration 0023）：`purchase_requests` 加 7 列（6 审批列 + 1 血缘列）+ `inbound_orders`
  加 1 列关联与唯一索引。**不加 status 列**（`status` 早已存在，`String(16)`，现值 draft/submitted）。
- **T2 状态机与三个决策端点**：approve / reject / cancel，全部带幂等 + CAS 乐观锁。
- **T3 生成入库单端点**：`approved` → 调既有 `create_inbound` 服务生成 InboundOrder（含明细），
  自然键幂等（一采购单至多一张入库单）。
- **T4 既有前端接桥**：只改 `index.html` 的 `#purchase-workflow` 区块（`/ops#purchase-workflow`），
  **不新建页面、不新增导航项**。

**明确不做**（超围即停）：新建 PurchaseOrder / GoodsReceipt 表、拆分单据、采购金额/单价建模、
部分收货与拆单、超收校验（S2）、收货进度聚合端点（S2）、库存写入（不碰 InventoryBalance）、
任何 DELETE、审批流多级/会签、邮件或消息通知。

**🚫 禁区（违反即退回）**：

1. **不得改动 `InventoryBalance` / `InventoryTransaction` 的任何写入路径**——库存只在
   `POST /api/inbounds/{id}/confirm`（既有）变化，本片生成入库单**不产生任何库存变动**。
2. **不得改动 `create_purchase_request_draft` / `edit_purchase_request_draft` /
   `submit_purchase_request_draft` / `submit_purchase_request` 四个既有服务的语义**——
   **唯一例外（Q2 裁定，§3 已界定）**：`create_purchase_request_draft` 可追加可选关键字
   `supersedes_request_id=None` 并原样透传；除此之外一行不动，**尤其不得把该字段加进
   `payload_hash` 的 payload 字典**（会让既有 draft 同键重放变成 409，F11）。
3. **不得用 `DELETE`**（U5）：撤回 = 状态位 + 留痕列。
4. **不得给 `create_inbound` 增加必填参数或改变既有参数语义**——只允许追加
   `purchase_request_id: int | None = None` 可选关键字（默认 None，既有 5 处调用零改动）。

## 2. 现状事实（F1–F12，实施前 P4 校验逐条核实）

| # | 事实 | 位置 |
|---|---|---|
| F1 | `purchase_requests` 已有：`status`(String16, default "submitted")、`submitted_by/at`、`created_by`、`version`(default 1)、`request_no`、`supplier_ref`、`expected_arrival_date`、`idempotency_key`+`payload_hash` | `db.py` PurchaseRequest |
| F2 | `purchase_request_actions` 已具备审计能力：`action_type` / `from_status` / `to_status` / `expected_version` / `actor` / `request_id` / `payload_hash` + 唯一约束 `(workspace_id, purchase_request_id, action_type, idempotency_key)` | `db.py` |
| F3 | 既有 action_type 取值：`create`（draft 建单）、`edit`、`submit`（draft 提交）；本条决定新取值为 `approve` / `reject` / `cancel` | `replenishment.py` |
| F4 | 既有并发范式：`request.version != expected_version` → `PURCHASE_REQUEST_VERSION_CONFLICT`；`status != 期望` → `INVALID_PURCHASE_REQUEST_STATE`。**但实现是"先 SELECT 再赋值再 commit"，存在并发窗口**（T3 必须升级为单条 CAS UPDATE） | `replenishment.py:534-541` |
| F5 | 两条建单路径：draft 路径写 `created_by=actor`、`status="draft"`；直接提交路径 `status="submitted"`、**`created_by` 为 NULL**、只写 `submitted_by` → U3 自审校验必须同时看两列 | `replenishment.py:477-484` / `651-660` |
| F6 | 权限点全集无 `purchasing.approve`；`ROLE_PERMISSIONS` 中 operations 含 `purchasing.submit`/`purchase.read`；admin 走 `has_role` 短路全通过 | `identity.py:24-36,72-75` |
| F7 | `require_principal(permission=...)` 对 employee 会话做 `principal.can()`；**demo/web 演示路径在 `auth_type=="demo"` 时提前返回、不校验 permission** | `employee_auth.py:145-172` |
| F8 | 既有入库服务 `create_inbound(db, *, warehouse_id, reference_no, lines[{sku_id, expected_qty}], note, created_by, workspace_id)`，内含仓库校验、`reference_no` 去重、SKU 校验，**自己 commit**；`InboundOrder` 无 `purchase_request_id` | `repository.py` |
| F9 | `/ops` 页已有 `#purchase-workflow` 区块，已渲染采购申请列表（submitted 行显示"已提交，待人工处理"），内联 JS 有 `api()` + `csrfToken()` + `data-purchase-action` 事件委托范式 | `index.html:100-110,315-355` |
| F10 | migration head = `0022_external_sync_dead_letters`；基线全量 **435 passed / 0 failed** | `migrations/versions/`、§19.4 |
| F11 | `create_purchase_request_draft(db, *, workspace_id, suggestion_ids, actor, idempotency_key, note, supplier_ref, expected_arrival_date)` 的 `payload_hash` **只对 4 键取 canonical hash**（suggestion_ids/note/supplier_ref/expected_arrival_date）。**把新字段塞进这个 payload 会让既有 draft 的同键重放从 200 变成 409 `IDEMPOTENCY_KEY_REUSE`** | `replenishment.py:427-451` |
| F12 | `tests/test_identity.py:114` 断言 `ROLE_PERMISSIONS[ROLE_ADMIN] == frozenset()`；`tests/test_p2s1_connector.py:187` 用 `monkeypatch.setitem(ROLE_PERMISSIONS, ...)` 局部改表 —— 本片只增 `ROLE_OPERATIONS` 一项，**不得改这两处** | `tests/` |

## 3. 改动面（允许清单，超清单须偏差声明）

| 文件 | 动作 |
|---|---|
| `app/purchase_approval.py` | **新增**：审批/驳回/撤回/生成入库单四个领域服务 + 状态机常量 + CAS 更新（纯领域，不 import fastapi） |
| `app/db.py` | `PurchaseRequest` 加 **7 列**（6 审批列 + `supersedes_request_id`）；`InboundOrder` 加 1 列 + 唯一索引元数据 |
| `migrations/versions/0023_purchase_approval.py` | **新增**（两表加列 + 唯一索引） |
| `app/repository.py` | `create_inbound` **仅追加可选关键字** `purchase_request_id`（类型 `int`，默认 `None`）并在 `InboundOrder(...)` 透传；**零其他改动** |
| `app/replenishment.py` | **仅 2 处**（Q2 裁定）：① `create_purchase_request_draft` 签名追加可选关键字 `supersedes_request_id: int | None = None`；② 该函数内 `PurchaseRequest(...)` 构造透传该字段。**`payload_hash` 的 payload 字典一个字符都不许动**（F11）。除这 2 处外零改动（增行 ≤4、删行 0） |
| `app/schemas.py` | `PurchaseRequestResponse` 增 **7** 键；`PurchaseRequestDraftCreate` 增 `supersedes_request_id: int | None = None`；新增 approve/reject/cancel 请求体 + `PurchaseRequestInboundOrderResponse` |
| `app/main.py` | 新增 4 端点（approve/reject/cancel/inbound-orders）+ 1 只读端点（inbound-order）；`_purchase_request_response` 补 7 键；`_replenishment_error` 追加 4 个新错误码映射；draft 创建端点**只追加**一句 `supersedes_request_id` 同 workspace 存在性校验（不存在 → 404，复用既有码） |
| `app/identity.py` | `ROLE_OPERATIONS` 权限集**只增** `"purchasing.approve"` |
| `app/templates/index.html` | **仅** `#purchase-workflow` 区块及其内联 JS（新增三个决策按钮 + 生成入库单按钮 + 驳回理由输入） |
| `tests/test_p3s1_purchase_approval.py` | **新增**（A-1..A-20，含 §6.1 `maker_checker_fixture`） |
| `docs/review-checklist-20261011.md` | §20 台账登记（R-51..） |
| ⚠️ 禁止触碰 | `app/external_sync.py`、`app/source_quality.py`、`app/dead_letters.py`、`app/sync_application.py`、`app/replenishment.py` 的**其余四个既有服务**（`edit_purchase_request_draft` / `submit_purchase_request_draft` / `submit_purchase_request` / 建议回退相关逻辑）、`app/identity.py` 的 `Principal` 与 `ROLE_PERMISSIONS` 其他条目、`_WORKSPACE_NAV_ITEMS`、`_PAGE_ANCHORS` |

连带许可（预先授权，仍须偏差声明）：`tests/test_alembic.py` head 断言跟随 0023；
`tests/test_r6_cursor_persistence.py` / `tests/test_p2s2_source_batch.py` / `tests/test_p2s3_quality.py`
中钉死 `upgrade` 目标版本的行**若**需跟随推进（R6-4 同款：只改目标行、不改断言语义）；
`tests/test_frontend_warehouse.py` / `tests/test_api_replenishment.py` 中因 `/ops` 新区块或
`PurchaseRequestResponse` 增键而必须跟进的**键集/标记**断言（只增断言，不删既有断言）。

## 4. 交付内容

### T1 字段（migration 0023）

**`purchase_requests` 新增 7 列（全部 nullable、无 server_default、无回填）**：

| 列 | 类型 | 语义 |
|---|---|---|
| `approver_id` | String(128) NULL | 审批决策人 actor（**approve 与 reject 都写**；与 `created_by` 同口径 = `principal.subject`） |
| `approver_user_id` | Integer NULL | 审批人 `user_accounts.id`（U3 的审计锚点；demo/api_key 路径为 NULL） |
| `decided_at` | DateTime NULL | 决策时刻（approve/reject **共用**——用 `status` 区分结论，不拆 `approved_at`/`rejected_at` 两个各留一半空值的列） |
| `reject_reason` | Text NULL | 驳回理由（reject 必填，1..500 字符） |
| `cancelled_by` | String(128) NULL | 撤回人（与 `approver_id` **不共用**，避免把"撤回"混入"审批"语义） |
| `cancelled_at` | DateTime NULL | 撤回时刻 |
| `supersedes_request_id` | Integer NULL | **Q2 血缘列**：本单取代的旧单 id（典型 = 被驳回后重办）。**软引用不 FK**，只做展示与追溯；**不参与状态机、不参与幂等、不做级联**（旧单状态永不回拨） |

> **status 不加列**：`status` 已存在（F1），本片只新增取值 `approved` / `rejected` / `cancelled`。
> 用户示例中的 `status` 即指该既有列。
>
> **`supersedes_request_id` 不写入 `payload_hash`（写死，违反即退回）**：`create_purchase_request_draft`
> 的 hash payload 必须保持既有 4 键（F11）；该字段只是附加数据，写进 hash 会让**改造前创建的
> draft** 在改造后同键重放时算出不同 hash → 被误判 `IDEMPOTENCY_KEY_REUSE`（409）。
> 校验位置：`main.py` 的 draft 创建端点，若传入则校验同 workspace 存在（不存在/跨租户 → 404
> `PURCHASE_REQUEST_NOT_FOUND`，复用既有码），**校验不通过不写库**。

**`inbound_orders` 新增 1 列**：

| 列 | 类型 | 语义 |
|---|---|---|
| `purchase_request_id` | Integer NULL | 来源采购单（**不 FK 约束，软引用**，与死信表 run_id 同款）；一采购单至多一张入库单 |

**唯一性用 index 而非 constraint（写死，勿改）**：

```python
op.create_index(
    "uq_inbound_order_purchase_request", "inbound_orders",
    ["workspace_id", "purchase_request_id"], unique=True,
)
```

理由：SQLite 的 `DROP CONSTRAINT` 不支持，用 `UniqueConstraint` 会让 down 被迫走
`batch_alter_table`（R-33 先例需要额外说明与逐列断言）；唯一**索引**可用 `DROP INDEX` 干净回退。
SQLite 唯一索引对 NULL 容忍多个 NULL —— 既有非采购来源的入库单（该列为 NULL）不受影响。

**migration 两约束**：
① down = `drop_index` → `drop_column` ×**8**（`purchase_requests` 7 列 + `inbound_orders` 1 列；
先索引后列，顺序写死），down 后 `purchase_requests` 与 `inbound_orders` 的表快照必须与 0022 形态
**整体相等**（R6-4 同款逐列断言：列名/类型/可空/默认值/位置 + 索引 + 唯一约束）；
② 全列 nullable、无回填、**不使用 SQLite batch**（若实施中发现必须用，停下报告按 R-33 先例处理）。

### T2 状态机（写死）

```
draft ──submit（既有）──▶ submitted
直接创建（既有）──────────▶ submitted
submitted ──approve──▶ approved
submitted ──reject───▶ rejected（终态，reject_reason 必填）
draft | submitted | approved(无入库单) ──cancel──▶ cancelled（终态）
approved(已有入库单) ──cancel──▶ 409 PURCHASE_REQUEST_ALREADY_INBOUND
rejected | cancelled ──任何决策动作──▶ 409 INVALID_PURCHASE_REQUEST_STATE
approved ──generate inbound──▶ status 不变（关联派生，**不新增 inbound_created 状态**）
```

**终态语义（Q2 裁定，写死）**：

- `rejected` / `cancelled` 是**绝对终态**：对任何决策动作（approve/reject/cancel）一律
  **409 INVALID_PURCHASE_REQUEST_STATE**；**不提供"重新打开"端点、不提供 `reopen` 状态**。
- **重办 = 新建一张 draft**，通过 `supersedes_request_id` 指向旧单（T1）；旧单 `status`/`version`
  **永不回拨**，新单独立走 draft → submitted → approved 全流程。
- 血缘只做**单向弱引用**：不做级联（旧单改了不影响新单）、不做唯一约束（同一旧单可被多次重办，
  业务上允许"第二次重办覆盖第一次重办"）、不参与幂等（见 T1 的 hash 红线）。

**状态机常量**（模块内写死，测试锁死）：
`APPROVABLE = {"submitted"}`、`REJECTABLE = {"submitted"}`、`CANCELLABLE = {"draft","submitted","approved"}`。

**采购单状态不被入库单反写**（P3 计划裁定）：`approved` 之后状态恒定；收货进度由
S2 的聚合查询派生，本片只提供 `GET .../inbound-order` 的单条关联读取（T4）。

### T3 幂等与并发控制（用户要求第 3 点，本片核心，全文写死）

**approve / reject / cancel 三者同构，按下列顺序执行（顺序不得调换）**：

1. **定位与鉴权**（写库前完成，越权必须保持数据库零变化）：
   按 `(workspace_id, id)` 取单 → 无 → **404 PURCHASE_REQUEST_NOT_FOUND**；
   `require_warehouse_access(principal, request.warehouse_id)`。
2. **幂等键校验**：`Idempotency-Key` 必填，缺失或 >128 → **422 IDEMPOTENCY_KEY_REQUIRED**（复用既有）。
   **approve / reject / cancel 三者一律必填、同一套 header、同一套校验函数（Q5 裁定：同构）**，
   不允许某个动作"因为操作轻量"豁免；`inbound-orders` 是唯一例外（T4 走自然键幂等）。
3. **幂等命中检查（先于一切状态闸）**：按
   `(workspace_id, purchase_request_id, action_type, idempotency_key)` 查 `purchase_request_actions`：
   - 命中且 `payload_hash` 相同 → **直接返回当前 request（200）**，零写入、不校验状态（重放语义）；
   - 命中且 hash 不同 → **409 IDEMPOTENCY_KEY_REUSE**。
   `payload_hash = _canonical_hash({action, request_id, expected_version, reason|note})`。
4. **自审红线（approve / reject，U3）**：
   `principal.subject == request.created_by` 或 `== request.submitted_by` →
   **409 SELF_APPROVAL_FORBIDDEN**，零写入。
   依据：employee 会话的 `subject = user.login`，而 `login` 有唯一约束 `uq_user_account_login`
   —— 字符串相等即同一 User；`approver_user_id` 另存 `principal.user_id` 供审计与未来强校验。
   **无豁免分支**：单人工作空间就是无人可审，这是内控设计（写进偏差声明，不得加"管理员可自审"后门）。
   **测试侧同样不得开口子**：见 §6.1（必须双账号，或把 `created_by` 构造为不存在的 login）。
5. **CAS 乐观锁（唯一正确性来源，必须是单条 UPDATE）**：

```sql
UPDATE purchase_requests
   SET status = :to_status,
       approver_id = :actor,          -- approve/reject 时写
       approver_user_id = :user_id,   -- 可 NULL
       decided_at = :now,             -- approve/reject 时写
       reject_reason = :reason,       -- 仅 reject
       cancelled_by = :actor,         -- 仅 cancel
       cancelled_at = :now,           -- 仅 cancel
       version = version + 1,
       updated_at = :now
 WHERE id = :id AND workspace_id = :ws
   AND status = :from_status
   AND version = :expected_version
```

   - `rowcount == 1` → 成功；
   - `rowcount == 0` → **重读并按固定顺序判定**：行不存在 → 404；`status != from_status` →
     **409 INVALID_PURCHASE_REQUEST_STATE**；`version != expected_version` →
     **409 PURCHASE_REQUEST_VERSION_CONFLICT**。
   **禁止"先 SELECT 校验版本，再 ORM 赋值 commit"**（F4 的既有窗口）——本片必须用 core
   `update()` 的原子条件更新。测试 A-5/A-6 专门打这个窗口。
6. **审计行（与 CAS 同一事务）**：`PurchaseRequestAction(action_type=approve|reject|cancel,
   from_status, to_status, expected_version, actor=principal.subject,
   request_id=<中间件 X-Request-ID>, payload_hash)`。
7. **cancel 的连带（U5 + 可重新提交）**：把该单 `purchase_request_lines` 对应的
   `replenishment_suggestions` 状态回退：`status = "modified"`、`submitted_by/submitted_at = NULL`、
   `active_slot = None`、`version += 1`、`updated_at = now`。
   理由：既有建议被提交后 `status="submitted"`，若不回退，`SUGGESTION_NOT_READY` 会永久
   堵死这批建议的再次申请（死胡同）；`"modified"` 是既有合法前置态（`submit_purchase_request`
   接受 `{confirmed, modified}`），且 `decision_qty` 保留、确认语义已由 `decided_by/decided_at` 记录。
   **代价写进偏差声明**：`confirmed` 与 `modified` 的区分在撤回后退化为 `modified`（R-51 登记）。
8. **事务边界**：CAS + 审计行 + 建议回退**单事务提交**；任一异常整体 `rollback`。

**端点契约**：

| 方法/路径 | 权限 | 请求体 | 成功 |
|---|---|---|---|
| `POST /api/purchase-requests/{id}/approve` | `purchasing.approve` | `{expected_version:int(ge=1), note?:str(≤500)}` | 200 `PurchaseRequestResponse` |
| `POST /api/purchase-requests/{id}/reject` | `purchasing.approve` | `{expected_version:int(ge=1), reason:str(1..500)}` | 200 同上 |
| `POST /api/purchase-requests/{id}/cancel` | `purchasing.submit` | `{expected_version:int(ge=1), reason?:str(≤500)}` | 200 同上 |
| `POST /api/purchase-requests/{id}/inbound-orders` | `inbound.create` | `{note?:str(≤1000)}` | **201** 首次 / **200** 幂等既有，`InboundResponse` |
| `GET /api/purchase-requests/{id}/inbound-order` | `purchase.read` | — | 200 `{inbound_order: InboundResponse \| null}` |

- cancel 的**归属校验**：`principal.subject` 必须是 `created_by` 或 `submitted_by`，否则
  **403 PURCHASE_REQUEST_NOT_OWNER**；`ROLE_ADMIN` 豁免归属校验（admin 因 `can()` 短路本就具备
  `purchasing.submit`，无需另开口子）。
- `inbound-orders` **不读** `Idempotency-Key`（自然键幂等，避免两套幂等语义打架），传了也忽略。

### T4 生成入库单（用户要求第 4 点）

- **前置**：`request.status == "approved"`，否则 **409 INVALID_PURCHASE_REQUEST_STATE**
  （submitted 未审批 / rejected / cancelled 一律拒）；另 `require_warehouse_access`。
- **自然键幂等**：先查 `InboundOrder(workspace_id, purchase_request_id=request.id)`：
  已存在 → **200 + 既有 `InboundResponse`**（重放安全）。
- **调用既有服务（不新写入库逻辑）**：

```python
order = create_inbound(
    db, warehouse_id=request.warehouse_id,
    reference_no=f"INB-{request.request_no}",
    lines=[{"sku_id": l.sku_id, "expected_qty": l.requested_qty} for l in lines],
    note=note, created_by=principal.subject,
    workspace_id=workspace_id, purchase_request_id=request.id,
)
```

- **明细规则**：按 `purchase_request_lines.id` 升序；`expected_qty = requested_qty`
  （U4 的生成侧对齐：预计到货量 = 审批通过量）；不合并、不按 SKU 重算。
- **并发生成**：唯一索引 `uq_inbound_order_purchase_request` 兜底——捕获 `IntegrityError`
  → `rollback` → 重查 → 存在则 **200 既有**（幂等优先于报错）。
- **不做**：不改 `request.status`；不写 `InventoryBalance`/`InventoryTransaction`；
  不校验会不会超收（S2）。

### T5 前端接桥（不新建页面）

只改 `app/templates/index.html` 的 `#purchase-workflow` 区块与其内联 JS：

| 行状态 | 新增操作 | 说明 |
|---|---|---|
| `submitted` | 「通过」「驳回」 | 驳回用 `prompt()` 收理由，空值前端拦一次 |
| `approved` | 「生成入库单」 | 成功后提示入库单号，并调 `GET .../inbound-order` 刷新为"已生成" |
| `draft` / `submitted` / `approved` | 「撤回」 | 二次 `confirm` 确认 |
| `rejected` / `cancelled` | 只显示状态 + 理由 | 无决策按钮；rejected 行附一行小字"重办请走新建申请（既有入口）"——**不做一键重办**（那要重跑建议选择 UI，越界），血缘字段 `supersedes_request_id` 是 API 级能力 |

- 复用既有 `api()` / `csrfToken()` / `data-purchase-action` 事件委托范式（F9），
  新动作值：`approve` / `reject` / `cancel` / `create-inbound`。
- **不做前端权限预判**：按钮一律按状态渲染，403 由 API 兜底并显示错误文案
  （页面不是授权边界；避免为注入权限标记去改共享渲染函数 `_render_workspace_page`）。
- **不新增导航项、不新增模板文件、不动 `_WORKSPACE_NAV_ITEMS` / `_PAGE_ANCHORS`**。

## 5. 数据/契约红线

- **契约**：`PurchaseRequestResponse` 既有 16 键（id/workspace_id/warehouse_id/request_no/status/
  note/submitted_by/submitted_at/created_by/version/supplier_ref/expected_arrival_date/
  idempotency_key/created_at/updated_at/lines）**键名与取值逐字节不变**，仅追加 **7** 键
  （`approver_id`/`approver_user_id`/`decided_at`/`reject_reason`/`cancelled_by`/`cancelled_at`/
  `supersedes_request_id`）。`list` 与 `detail` 两处必须一致（共用 `_purchase_request_response`）。
- **draft 幂等零漂移（红）**：`create_purchase_request_draft` 的 `payload_hash` payload **保持既有
  4 键**，`supersedes_request_id` 不得进入（F11）。A-19 用"改造前形态"的既有 draft 行做同键重放断言。
- **错误码**：新增 4 个，全部进 `_replenishment_error` 显式映射：
  `SELF_APPROVAL_FORBIDDEN`→409、`PURCHASE_REQUEST_NOT_OWNER`→403、
  `REJECT_REASON_REQUIRED`→422、`PURCHASE_REQUEST_ALREADY_INBOUND`→409。
  既有码（`INVALID_PURCHASE_REQUEST_STATE`=409、`PURCHASE_REQUEST_VERSION_CONFLICT`=409、
  `IDEMPOTENCY_KEY_REUSE`=409、`IDEMPOTENCY_KEY_REQUIRED`=422、`*_NOT_FOUND`=404）**语义零改动**。
- **数据**：全列 nullable、无回填、down 逐列还原、不用 batch。
- **零 DELETE**：`grep -rn "DELETE\|\.delete(" app/purchase_approval.py` 必须零命中（A-18 静态断言）。
- **零库存副作用**：生成入库单后 `InventoryBalance.on_hand_qty` 与 `InventoryTransaction` 计数
  必须与操作前完全相等（A-14 断言）。
- **凭据红线**：本片不涉及凭据，日志与错误消息中不得出现 token 形态字符串。

## 6. 用例（最小 20 条）🔒

| # | 用例 | 断言要点 |
|---|---|---|
| A-1 | migration 0023 up/down | up 加 **8 列**（purchase_requests 7 + inbound_orders 1）+ 唯一索引 `uq_inbound_order_purchase_request`；down 后两表快照与 0022 形态**整体相等**（逐列/索引/约束）；未用 batch |
| A-2 | approve 成功 | status=approved、`approver_id`/`approver_user_id`/`decided_at` 落库、version+1、`purchase_request_actions` 增 `approve` 行（含 actor/request_id/payload_hash） |
| A-3 | approve 幂等 | 同 `Idempotency-Key` 二次 → 200 同结果；version 不再增；action 行恰好 1 条 |
| A-4 | 幂等键复用冲突 | 同 key 不同 `expected_version`/`note` → 409 IDEMPOTENCY_KEY_REUSE，状态不变 |
| A-5 | 版本冲突（同一单） | 先 approve（version 2），再以 version 1 调 approve → 409 PURCHASE_REQUEST_VERSION_CONFLICT |
| A-6 | **并发 CAS** | 两个不同 user 用同一 `expected_version` 各调一次 approve → 恰好 1 个 200、1 个 409 INVALID_PURCHASE_REQUEST_STATE；`approver_id` 只落第一个成功者 |
| A-7 | 自审红线（U3） | 制单人本人 approve → 409 SELF_APPROVAL_FORBIDDEN；reject 同；数据库全表零变化（status/version/action 行数） |
| A-8 | 权限闸（U2 + Q1） | warehouse 角色调 approve → 403；operations 调 approve → 通过；只读角色 → 403；**"互不替代"的证明方式写死**：operations 现在同时拥有 submit + approve，内置角色已无法证明互斥，故用 `monkeypatch.setitem(ROLE_PERMISSIONS, "operations", frozenset({"purchasing.submit","purchase.read"}))` 临时收窄（**先例** `tests/test_p2s1_connector.py:187`）→ 同一账号 approve/reject 403、cancel 仍 200；monkeypatch 自动还原，**不得直接改 `identity.py` 的表** |
| A-9 | reject 理由必填 | 缺 `reason` / 空串 / 全空格 → 422 REJECT_REASON_REQUIRED，零写入 |
| A-10 | reject 成功 | status=rejected、`reject_reason` 原文落库、`decided_at` 非空、`approver_id`=审批人、action 行 `reject` |
| A-11 | 状态机负向 | draft→approve 409；rejected→approve 409；cancelled→approve 409；approved→approve 409（四分支一条用例内依次断言 code） |
| A-12 | cancel 成功（U5） | submitted→cancelled、`cancelled_by`/`cancelled_at` 落库、**无 DELETE**（行仍在）、version+1 |
| A-13 | cancel 归属 | 非制单人（另一 operations 用户）→ 403 PURCHASE_REQUEST_NOT_OWNER；admin → 通过；approved 且已生成入库单 → 409 PURCHASE_REQUEST_ALREADY_INBOUND |
| A-14 | 生成入库单成功 | 201；`inbound_orders.purchase_request_id` 落库；lines 的 `expected_qty == requested_qty` 且与采购明细逐 SKU 对齐；`reference_no == "INB-"+request_no`；`created_by`=actor；**库存/流水零变化** |
| A-15 | 生成幂等 | 二次调用 → 200 且 `inbound_order.id` 相同；库中该 `purchase_request_id` 只有 1 行；并发路径（IntegrityError 分支）单测直接构造唯一冲突断言返回 200 |
| A-16 | 生成前置与越权 | draft/submitted/rejected/cancelled → 409 INVALID_PURCHASE_REQUEST_STATE；无 `inbound.create` → 403；跨 workspace id → 404 |
| A-17 | 响应契约 | detail 与 list 的既有 16 键逐字节不变 + 新增 **7** 键（含 `supersedes_request_id`）；`GET .../inbound-order` 两分支（未生成 → `{"inbound_order": null}`；已生成 → 对象，含 reference_no/status/lines） |
| A-18 | 前端与静态红线 | `/ops` HTML 含 `data-purchase-action` 的 `approve`/`reject`/`cancel`/`create-inbound`；`#purchase-workflow` 仍在；`grep` 断言 `purchase_approval.py` 无 `DELETE`/`.delete(`、不 import fastapi；既有 `test_frontend_warehouse` 断言零改动全绿 |
| A-19 | **Q2 终态与重办血缘** | ① reject 成功后：approve/reject/cancel 三者均 → 409 INVALID_PURCHASE_REQUEST_STATE；② 新建 draft 带 `supersedes_request_id=旧单.id` → 成功且响应落值，**旧单 status 仍 rejected、version 不变、无新 action 行**；③ `supersedes_request_id` 指向不存在 id / 跨 workspace → 404 且零写入；④ **不传该字段**的既有 draft 创建路径行为零变化；⑤ 用"改造前形态"行（手写 `payload_hash` 为既有 4 键 hash）做同键重放 → **200**，不得 409（F11 红线） |
| A-20 | **Q1b 双账号与禁绕过** | ① `maker_checker_fixture` 两账号：maker 提交、checker（**不同 login，均 operations**）approve → 200 且 `approver_id==checker.login`、`approver_user_id==checker.id`；② maker 自审 → 409 SELF_APPROVAL_FORBIDDEN、库零变化；③ `ROLE_OPERATIONS` 含 `purchasing.approve`、`ROLE_WAREHOUSE` **不含**、`ROLE_PERMISSIONS[ROLE_ADMIN]==frozenset()` 不变；④ 静态断言：`grep "SELF_APPROVAL_FORBIDDEN" app/purchase_approval.py` 命中数恒定（仅"判定 + 抛错"两处），**不存在 env/settings 开关分支**；测试文件内不得出现针对自审校验的 `monkeypatch`/短路 |

### 6.1 测试夹具红线（Q1b 裁定，违反即退回）

`tests/test_p3s1_purchase_approval.py` 顶部必须提供 **`maker_checker_fixture`**，规格写死：

1. **同 workspace 内两个独立 `UserAccount`**：`maker`（制单 + 提交）与 `checker`（审批），
   两者 role 都是 `operations`（因此各自都同时具备 `purchasing.submit` 与 `purchasing.approve`，
   A-8 的"权限不互代"才有意义）；
2. **两个独立 `TestClient` 实例分别登录**（各自 cookie jar / CSRF token），
   **禁止**同一 client 反复登录切换身份——那样"自审"与"他人审批"的差别会消失，用例失去鉴别力；
3. `checker` 的 `Principal.user_id` 与 `maker` 不同 → A-20 断言 `approver_user_id == checker.id`；
4. 只关心状态机的用例（A-2..A-14）可用单账号，但**必须让 `created_by`/`submitted_by` ≠ 审批者**：
   允许的等价构造 = 把 draft 行的 `created_by`/`submitted_by` 直接写成**不存在的 login**
   （如 `"system:p3s1-fixture-maker"`）——U3 只做字符串比对（F5 / R-53），这不算绕过，是造历史数据；
5. **严禁（出现即退回）**：
   - 给 `SELF_APPROVAL_FORBIDDEN` 加任何 `settings`/环境变量开关；
   - 在测试里 `monkeypatch` 掉自审校验，或伪造 `principal.subject` 等于 `created_by` 来"顺手通过"；
   - 为让单账号跑通而放宽 `ROLE_PERMISSIONS`（含把 `purchasing.approve` 塞进 `ROLE_WAREHOUSE`）；
   - 把 `tests/test_identity.py:114` 的 `ROLE_ADMIN == frozenset()` 断言改掉（F12）。
6. 允许的两种合规写法**仅限**：① 两个真实账号（首选）；② 历史数据构造（`created_by` 指向不存在的 login）。
   二者的共同前提是**产品代码里 U3 是无条件的单一判定**——这也是 A-20 ④ 的静态复核内容。

## 7. 前置校验（开工前逐条执行并贴输出）

| # | 校验 | 命令 | 期望 | 结果 |
|---|---|---|---|---|
| P1 | 串行前提：P3 计划提交为 HEAD 祖先 | `git merge-base --is-ancestor c565123 HEAD && echo OK` | OK | 待贴 |
| P2 | 无并行写者 | R-29 流程（tasklist 查 claude 进程） | 无 | 待贴 |
| P3 | 基线 | `env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe -m pytest -q -p no:cacheprovider` | **435 passed / 0 failed** | 待贴 |
| P4 | F1–F12 逐条核实 | 见 §2 位置列 | 全部属实 | 待贴 |
| P5 | 命名不冲突 | `grep -rn "purchase_approval\|purchasing.approve\|approver_id\|decided_at\|supersedes_request_id" app/ tests/` | 零命中（除本片新增） | 待贴 |
| P6 | migration head | **实跑 `alembic heads`**，输出原文进交付说明 | 0022 唯一 head | 待贴 |

## 8. R-34 分块条款（照模板 v1.1 全文）

每块 <10 分钟、编辑即落盘、块交付说明写 `.testtmp/p3s1-chunk<N>.md`；
进程疑似死亡先 `git status` 再判死（acceptEdits 会落盘）；长验证（全量回归）留给复核方自跑；
收口提交由复核方代笔。建议分块：① 字段+模型+migration → ② 领域服务（approve/reject/cancel 的
幂等+CAS）→ ③ 生成入库单 + `create_inbound` 可选参数 → ④ schemas+main 四端点+错误映射 →
⑤ 前端区块 → ⑥ 测试（A-1..A-20 分两批，A-19/A-20 单列一块：Q2 血缘 + Q1b 双账号夹具）。

## 9. 完成标准与验收口径

- [ ] 基线 435 保持，全量 **435+N** / 0 failed（复核方自跑确认）；
- [ ] §6 **二十条**定向绿（贴条数与用例名），含 A-19（Q2 终态与血缘）、A-20（Q1b 双账号与禁绕过）；
- [ ] `app/replenishment.py` **仅 2 处改动**：`git diff --numstat -- app/replenishment.py`
      增行 ≤4 / 删行 0，且 `payload_hash` 的 payload 字典零变化（A-19 ⑤ 证）；
- [ ] 生成入库单前后 `InventoryBalance` / `InventoryTransaction` 计数相同（A-14）；
- [ ] 零 `DELETE` 证明（grep 原文）；
- [ ] `.testtmp/delivery.p3s1-purchase-approval.md` 五节齐全（改动面/用例与结果/**偏差声明**/
      残留与已知限制/证据路径）+ 6 份分块说明落盘；
- [ ] 偏差声明必须覆盖：① 为何 cancel 把建议回退到 `modified`（T3.7 及其代价）；
      ② `decided_at` 合并 approve/reject 的取舍；③ 唯一性用 index 而非 constraint；
      ④ 自审校验用 subject 字符串（含 login 唯一约束的依据）且无豁免分支；
      ⑤ `create_inbound` 新增可选参数的影响面（既有 5 处调用零改动证据）；
      ⑥ **`supersedes_request_id` 不进 `payload_hash` 的理由（F11）与既有同键重放 200 的证据**；
      ⑦ **`app/replenishment.py` 两行改动的精确位置与其余零改动证据**；
      ⑧ **`maker_checker_fixture` 的形态，以及"没有任何测试为通过而削弱 U3"的自证**（A-20）；
      ⑨ 若触发 SQLite batch（预期不触发），按 R-33 先例说明；
- [ ] R-51/R-52/R-53/R-54 登记进 §20 台账，编号连续（当前最大 R-50）。

## 10. 新残留（预登记，编号接 R-50）

| 编号 | 内容 | 定级 |
|---|---|---|
| R-51 | cancel 后建议状态统一回退为 `modified`，`confirmed`/`modified` 区分丢失（决策量仍保留） | P3 |
| R-52 | 采购单无金额/单价/供应商报价字段，审批无法体现"批多少钱"（U1 同实体的直接后果） | P3 |
| R-53 | 自审红线的身份基准是 `subject`（=login）字符串，`approver_user_id` 仅审计用；api_key/demo 路径 user_id 为 NULL，无法做 id 级强校验 | P3 |
| R-54 | `supersedes_request_id` 是弱血缘：不 FK、不唯一（同一旧单可被多次重办）、不做级联；重办单与旧单之间没有强一致性保证（旧单被误改也不阻断新单） | P3 |

## 11. 裁定记录（Q1–Q5 已全部关闭，2026-10-29，无需再问）

| # | 问题 | 裁定（生效值） |
|---|---|---|
| Q1 | `purchasing.approve` 授予哪些角色 | **operations + admin**（admin 走既有短路）。**连带要求**：审批类用例必须双账号（§6.1），不得为跑通用例削弱 U3 |
| Q2 | 驳回后是否允许重开 | **不允许，rejected 终态**。重办 = 新建 draft 并用 `supersedes_request_id` 关联旧单；单条记录的状态机保持干净 |
| Q3 | 撤回后建议回退到 `modified` | **采纳**（T3.7），代价登记 R-51。理由：不回退会让这批建议永久 `SUGGESTION_NOT_READY`，业务闭环断裂 |
| Q4 | U4 超收校验落片 | **S2**。S1 只负责生成预期单，`expected_qty` 必须**精确等于** `requested_qty`（禁止取整/合并/裁剪） |
| Q5 | cancel 是否要幂等键 | **要**。approve / reject / cancel 三者同构，统一走 `Idempotency-Key` header 与同一校验函数 |
