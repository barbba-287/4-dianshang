# P3-S2 任务文件：入库单关联采购单 + U4 超收闸 + 收货进度派生 + 实收页显示来源

- **版本**：v1.0 草案（2026-10-30，待用户评审，评审通过前严禁开跑）
- **前置**：P3-S1 已收口（`e4e728f` 实施 + `533f3d1` 复核结论），基线 **455 passed / 0 failed**。
- **切片目标**：把 S1 建好的「采购单 → 入库单」血缘接通语义：实收环节按采购单明细拦超收（U4），
  提供只读的收货进度派生端点（严禁反写），实收页显示来源采购单。
- **硬约束**：**不新建表、不写 migration、不碰 inventory 入账核心逻辑**（用户裁定，见 §5）。

---

## 1. 现状盘点（2026-10-30 复核方亲验，行号为当前 HEAD `533f3d1`）

### 1.1 实收链路（U4 闸的落点）

`app/repository.py:632-675` `receive_inbound(db, *, inbound_id, lines, idempotency_key, payload_hash, workspace_id)`，
校验序列依次为：

| 行号 | 校验/动作 | 失败错误码 |
|---|---|---|
| 641-642 | Idempotency-Key 非空 | `IDEMPOTENCY_KEY_REQUIRED` |
| 643-645 | 定位入库单（含 workspace） | `INBOUND_NOT_FOUND` |
| 647-650 | **幂等重放闸**：status ≠ expected 时，三元组（key+hash）匹配则原样返回，否则 `INBOUND_ALREADY_RECEIVED` | — |
| 652-654 | 请求行 SKU 去重 | `DUPLICATE_SKU` |
| 655-657 | 实收 SKU 集合 == 入库单明细 SKU 集合 | `INBOUND_LINES_MISMATCH` |
| 658-660 | 每行 `damaged_qty > received_qty` 拒绝 | `DAMAGED_QTY_INVALID` |
| 662-674 | **mutation 全部在此 try 块内**：写 received_qty/damaged_qty/status="received"/received_at/幂等三元组，单次 commit | — |

关键事实：
- **数量语义**：`received_qty` 是实收**总数**，`damaged_qty` 是其中破损部分（**子集**），
  `damaged ≤ received` 由 658-660 强制；合格量 `accepted = received − damaged`
  （`repository.py:537`、`repository.py:699`）。**"received + damaged" 会把破损算两遍。**
- `expected_qty` 在关联单上恒等于采购明细 `requested_qty`（见 1.3），两表值创建后均不可变。
- `confirm_inbound`（`repository.py:678-724`）只认 status=="received"，入账核心在 698-717
  （幂等键 `inbound:{order}:line:{line}:confirm`、`InventoryBalance.on_hand_qty += accepted`）。**本片只读不改。**

### 1.2 血缘列与写入路径

- `app/db.py:348` `InboundOrder.purchase_request_id`（S1 已落，nullable）；
  `app/db.py:324` 唯一索引 `uq_inbound_order_purchase_request (workspace_id, purchase_request_id)`
  → **一采购单至多一张入库单**（S1 语义，本片依赖且不得破坏）。
- `purchase_request_id` 的**唯一写入路径**是 S1 生成端点
  （`app/purchase_approval.py:375-437` `generate_inbound_order_for_request`）；
  公开 `POST /api/inbounds`（`main.py:1224-1235`）**不透传**该参数 → 手工单必然无边（purchase_request_id 为 NULL）。
- `app/db.py:965-980` `PurchaseRequestLine`：唯一约束 `uq_purchase_request_line_sku (workspace_id, purchase_request_id, sku_id)`
  → 一采购单内 SKU 不重复，可安全建 `{sku_id: requested_qty}` 映射；
  `requested_qty` 在 978 行。

### 1.3 错误码映射（409 缺口）

`app/main.py:1161-1179` `_warehouse_error`：
```python
status = 404 if code.endswith("NOT_FOUND") else 409 if code.endswith(("EXISTS", "RECEIVED", "STATE")) else 422
```
→ 新码 `OVER_RECEIVING_FORBIDDEN` 不以这三者结尾，**不显式加分支会被错映射成 422**。

### 1.4 响应契约现状

- `app/schemas.py:551-560` `InboundResponse`：现 9 键，**无** purchase_request 相关键；
  `_inbound_response_data`（`repository.py:523-545`）同样不含。
- `app/schemas.py:580-589` `InboundListItem`（列表页）与列表接口**本片不动**。
- `_inbound_response_data` 调用点共 **6 处**：`main.py:1233 / 1246 / 1314 / 1335 / 3349 / 3369`。
- 先例：S1 向 `_purchase_request_response` 追加 7 键（`main.py:2944-2951`），既有键逐字节不变，复核通过。

### 1.5 权限与前端

- `GET /api/purchase-requests/{id}/inbound-order`（S1 只读）权限点 = **`purchase.read`**（`main.py:3251` 注释）；
  定位+仓库范围校验复用 `_load_purchase_request_for_action`（`main.py:3253-3264`）。
- 实收页 = `app/templates/warehouse.html`（21 行，单文件）：
  - line 9：静态结构，「仓库实收反馈」面板含 `#state`、`#details`、`#receive-lines`；
  - line 17：`selectInbound()` 渲染明细表（SKU/预计数量/状态三列）与实收输入行；
  - line 19：`receive.onclick` 提交 `POST /api/inbounds/{id}/receive`。
  - 页面数据源是 `GET /api/inbounds/{id}`（即 `_inbound_response_data`）——**前端拿不到 PR 编号的唯一原因是响应里没有**。

---

## 2. 设计决策（已锁定项，实施方不得偏离）

### D1. U4 超收闸落在 `receive_inbound`（服务层，单点）

位置：`app/repository.py` `receive_inbound` 内、**`DAMAGED_QTY_INVALID` 循环（658-660）之后、try 块（662）之前**。

逻辑（当且仅当 `order.purchase_request_id` 非空）：
1. 读 `PurchaseRequestLine`（`workspace_id == order.workspace_id` 且 `purchase_request_id == order.purchase_request_id`），
   建 `{sku_id: requested_qty}` 映射；
2. 对每条实收行：`requested = 映射.get(sku_id, 0)` —— **缺行按 0 处理（fail-closed）**
   （构造上不可达：1.2 已证 SKU 集合恒等；若可达则宁可误拦不可漏放）；
3. `received_qty > requested` → `raise ValueError("OVER_RECEIVING_FORBIDDEN")`。

放该位置的三点理由（验收时逐条核对）：
- **幂等重放闸（647-650）在前** → 重放不重复触发校验，语义干净；
- **在 try 块之前** → 抛错时零写入（mutation 全在 try 内），status 仍 expected，无半态；
- **在 `confirm_inbound` 之前**（receive 先于 confirm 才有 received）→ 满足"校验在入库确认前完成"。

### D2. 错误码与映射

- 错误码：`OVER_RECEIVING_FORBIDDEN`；HTTP **409**。
- `main.py:1161-1179` `_warehouse_error` 必须显式补：status 判定加分支（如
  `if code == "OVER_RECEIVING_FORBIDDEN": status = 409`）+ messages 加
  `"OVER_RECEIVING_FORBIDDEN": "超收被拒绝：实收数量不得超过采购申请数量"`。
- **不新增**独立 Exception 类型，沿用 `ValueError` 通道（全仓库既有惯例）。

### D3. 收货进度派生端点（只读聚合，严禁反写）

- 端点：`GET /api/purchase-requests/{request_id}/receipt-progress`；
  权限 `purchase.read`；复用 `_load_purchase_request_for_action`（404 语义与 S1 一致）。
- 服务函数：`get_receipt_progress_for_request(db, *, workspace_id, request_id)` 放 **`app/repository.py`**
  （入库聚合属仓储域；`purchase_approval.py` 本片**零改动**）。函数只读，不 commit、不写任何表。
- 聚合规则：
  - 入库单：按唯一索引取该 PR 关联单（至多一张）；**只聚合 status ∈ {"received","confirmed"} 的明细**
    （expected 单尚未报实收，不计入）；
  - 每行输出：`sku_id` / `requested_qty`（来自 PurchaseRequestLine）/ `received_qty` / `damaged_qty` /
    `accepted_qty`（= received − damaged，未报实收为 null）/ `remaining_qty`（= requested − received，下限 0）；
  - 汇总 `totals` 与整体状态 `progress_status`：
    - PR 无明细 或 received 总量 == 0 → `"pending"`；
    - 0 < received 总量 < requested 总量 → `"partial"`；
    - received 总量 ≥ requested 总量 → `"completed"`；
  - 附 `inbound_order_id` / `inbound_reference_no` / `inbound_status`（无关单时为 null）。
- schemas 新增 `ReceiptProgressResponse`（含 lines / totals / progress_status / inbound 概要）。
- **严禁**在 `purchase_requests` / `purchase_request_lines` 表加任何进度字段或写库动作（用户裁定 3）。

### D4. 来源采购单显示（响应契约 + 前端）

- `InboundResponse`（schemas.py:551-560）追加两个**可选**键（默认 None，追加在末尾）：
  - `purchase_request_id: int | None = None`
  - `purchase_request_no: str | None = None`
- `_inbound_response_data`（repository.py:523）签名扩展为
  `_inbound_response_data(order, lines, *, db: Session | None = None)`；
  当 `order.purchase_request_id` 非空且 db 不为 None 时，查一次
  `PurchaseRequest.request_no` 填入两键。**6 个调用点（main.py:1233/1246/1314/1335/3349/3369）同步补 `db=db`**；
  既有键的输出值逐字节不变（S1 先例）。
- `InboundListItem` 与列表接口**不动**。
- `warehouse.html`：在「仓库实收反馈」面板（line 9 静态结构或 line 17 `selectInbound()` 渲染处）
  增加一条提示：`current.purchase_request_id` 非空时显示 **`来源采购单：PR-{purchase_request_no}`**；
  无关联单不显示任何痕迹。明细表**不**加列（实收输入行结构不动，降低回归面）。

### D5. 无边路径逐字节不变（用户裁定 6）

`purchase_request_id is None` 的入库单：**不**做超收校验（无基准可依，维持现状语义——
这正是现状：实收可与 expected 任意偏差），进度端点不受影响，前端无提示。
验收必须包含无边单回归（R-3）。

---

## 3. 待裁定（评审时拍板，当前按推荐默认写死）

### Q-S2-1【必须裁定】超收判定口径：damaged 是否重复计入

您的要求原文是「received + damaged 必须 <= requested_qty」。但本仓库语义是
**damaged ⊆ received**（`damaged ≤ received` 由 `DAMAGED_QTY_INVALID` 强制，合格量 = received − damaged，
见 §1.1）——字面公式会把破损算两遍。例如 requested=100、received=100、damaged=30：
- 口径 A（**推荐**）：`received_qty ≤ requested_qty`。破损已含于实收总数，上例通过；
  "实收+破损超过采购量"的业务意图（不许多收货）同样被满足。
- 口径 B（字面）：`received_qty + damaged_qty ≤ requested_qty`。上例 130 > 100 被拦；
  等价于要求合格量 + 2×破损 ≤ 采购量，与「破损是实收的一部分」的现状语义冲突，
  且与 S1 生成的 `expected_qty = requested_qty` 在收货页展示的基准不一致。

**本文件按口径 A 写死（D1 第 3 步只比对 received_qty）。若您裁口径 B，仅改 D1 第 3 步不等式与 R-2 用例预期，其余不动。**

另确认两点（已按无异议写死）：① 无容差配置项（您已裁定"若无则严格等于"，不做 settings 开关）；
② 只拦超收，**短收不拦**（短收是正常业务态，由进度端点呈现 partial）。

---

## 4. 改动清单（预期 6 文件）

| 文件 | 改动 | 量级 |
|---|---|---|
| `app/repository.py` | ① `receive_inbound` 加 U4 闸（D1）；② 新增只读 `get_receipt_progress_for_request`（D3）；③ `_inbound_response_data` 加可选 db 参数与两键（D4） | ~60 行 |
| `app/main.py` | ① `_warehouse_error` 补 409 分支+文案（D2）；② 新增 receipt-progress 端点（D3）；③ 6 处 `_inbound_response_data` 调用点补 `db=db`（D4） | ~40 行 |
| `app/schemas.py` | `InboundResponse` 追加 2 可选键；新增 `ReceiptProgressResponse` | ~25 行 |
| `app/templates/warehouse.html` | 实收面板来源提示（D4） | ~3 行 |
| `tests/test_p3s2_inbound_linkage.py` | 新建，§6 用例 | 新文件 |
| `docs/review-checklist-20261011.md` | §21 P3-S2 台账占位（复核阶段填） | 少量 |

**不碰**：`app/db.py`（零改动）、无新 migration、`app/purchase_approval.py`（零改动）、
`app/identity.py`（无新权限点）、`app/replenishment.py`、`app/confirm_inbound` 入账核心（repository.py:678-724 的 698-717 段）。

---

## 5. 守卫与禁区（违反任一条 = 复核直接打回）

1. **不新建表、不写 migration、不改 `app/db.py`**（用户裁定 5）。
2. **不碰 inventory 入账核心**：`confirm_inbound` 函数体逐字节不变；U4 只在 receive 拦截，confirm 不重复校验。
3. **严禁进度反写**：receipt-progress 全链路只读；不得在 purchase_requests/purchase_request_lines 加列、
   不得 UPDATE 任何采购表字段（用户裁定 3）。
4. **无边路径逐字节不变**（D5）：不引入"无边单也按 expected 拦"的隐藏语义。
5. **无新权限点、无角色变更**：receive 仍 `inbound.receive`，progress 用既有 `purchase.read`。
6. **无 settings/environ 开关**（含无容差配置）；不自审式绕过、无任何环境驱动的行为分支。
7. **禁止**用 `reference_no` 的 `INB-` 前缀反推 PR 编号（脆弱，已排除）。
8. 既有 455 条用例零改动、零删除；`_inbound_response_data` 既有键逐字节不变。

---

## 6. 验收用例（8 条，全部落 `tests/test_p3s2_inbound_linkage.py`）

命名 `R-1`..`R-8`；链路构造复用 S1 测试的成熟路径（draft→submit→approve→inbound-orders，
参考 `tests/test_p3s1_purchase_approval.py`；**双账号夹具红线继续适用**——审批用独立审批人，
严禁 monkeypatch 自审或加开关）：

| # | 用例 | 断言要点 |
|---|---|---|
| R-1 | 关联单正常实收 | received == requested → 200；status="received"；received_at 落值 |
| R-2 | 超收被拦（U4 主用例） | received = requested+1 → **409 `OVER_RECEIVING_FORBIDDEN`**；status 仍 "expected"；received_qty/damaged_qty 未落库（重查为 null/原值）；InventoryTransaction / InventoryBalance 零新行 |
| R-3 | 无边单不受影响 | 手工 `POST /api/inbounds`（无边）实收超 expected → 200（现状回归，逐字节旧行为） |
| R-4 | 进度派生三态 | 同一 PR 构造 pending（未报）→ partial（短收）→ completed（足额）三断言；数值与 §D3 公式一致 |
| R-5 | 破损计入口径 | received == requested 且 damaged>0 → 200（口径 A 实锤；若裁 B 则本例预期翻转） |
| R-6 | 幂等重放先于 U4 闸 | 合法实收后同 key+hash 重放 → 200 原样返回（不因再走一遍校验而异）；不同 key → 409 `INBOUND_ALREADY_RECEIVED` |
| R-7 | 来源键契约 | 关联单 `GET /api/inbounds/{id}` 响应含 `purchase_request_id` + `purchase_request_no`；无边单两键为 null；既有 9 键逐字节不变 |
| R-8 | 进度端点边界 | ① PR 无明细（draft 态）→ 200 空数组 + totals 全 0 + "pending"；② 不存在的 PR → 404 `PURCHASE_REQUEST_NOT_FOUND`；③ expected 态入库单不计入 received |

前端 `warehouse.html` 为模板改动，不做自动化断言；交付说明中附改动片段 + 手工冒烟路径
（登录 → 选授权仓库 → 选关联单 → 确认提示条出现；选手工单 → 无提示条）。

**基线预期：455 → 463 passed / 0 failed。**

---

## 7. 已排除的选项（实施方不得重新试错）

1. **校验放 `confirm_inbound`** —— 违反"确认前完成"裁定，且 confirm 无法携带实收输入。
2. **校验放端点层（main.py receive 端点）** —— 服务层单点原则（S1 经验）；CLI/未来调用方绕过端点即失守。
3. **用 `InboundLine.expected_qty` 作基准** —— 用户点名读采购明细 `requested_qty`；二者当前恒等，
   但语义基准必须钉在采购事实层（防未来 expected 可编辑时静默漂移）。
4. **PR 无明细时 404** —— PR 存在即可查进度，空明细返回 200 空态（R-8①）。
5. **进度写 purchase_requests 冗余列** —— 用户明令禁止；聚合查询成本可忽略（一 PR 一单，索引直取）。
6. **InboundListItem/列表页加来源列** —— 本片只做实收页提示，列表不动（控制回归面）。
7. **为超收加 CAS/加锁** —— receive 现状无 CAS（SQLite 单写锁），本片不引入（与 S1 A-6 同理）。
8. **`OVER_RECEIVING_FORBIDDEN` 映射成 422** —— 语义是状态冲突拒绝，409（§1.3 缺口必须显式补）。

---

## 8. 交付要求

1. 完成即把交付说明写到 **`.testtmp/delivery.md`**（落盘，防进程失联；含提交号、全量结果、
   偏差清单、warehouse.html 改动片段与冒烟说明）。
2. 全量回归命令（**必须剥离会话注入垫片**，R-15）：
   ```
   env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe -m pytest -q -p no:cacheprovider
   ```
3. 提交信息前缀 `p3s2:`；**完成后提交并停住，不 push**（阶段收口策略：P3 三片全清后一起推）。
4. 交付说明中如实列出所有偏差（哪怕自认合理），供复核裁决；预估偏差不会发生——本片无 migration、
   无幂等键新增、无错误码新增之外的通道变更。
