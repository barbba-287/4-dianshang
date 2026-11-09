# P9-S3 订单级 UPSERT：关闭 R-68 / R-69（本仓最高风险切片）

> 状态：**v1.0 定稿（2026-11-08 用户裁定三项，可开跑第一轮）**
> 基线：`667 passed / 0 failed`（2026-11-08 实测），分支 `feat/warehouse-mvp`，head 迁移不动
> 定位：**唯一还会「静默改错历史利润」的路径**。不同于 P9-S2 的纯清理，本片动的是写入咽喉
> `ingest_orders`，一旦错，错的不是显示，是已经入账的成本锚定。
> **执行分两轮**：阶段 A（行指纹 + R-68 + 对拍基建 + R-73）→ 独立验收 → 阶段 B（SAVEPOINT + R-69 + 前端开关）。
>
> **用户 2026-11-08 三项裁定（已固化进本文档，见 §2 D3/D4、§10）**：
> 1. R-69 口径开关**采纳方案 A**（`line_mode` 可选参数，默认 `full`）；**方案 C 自动推断绝对禁止**。
> 2. 前端勾选框**必须加**（`sources.html` 导入向导），不勾选不得传 `incremental`。
> 3. `external_line_id` 可用 `csv:SKU|seq`，**前提是双索引兼容**（旧 `line:N` 必须仍能继承锚定）；无外部对账依赖。

---

## §0 为什么这片必须比前几片更小心

前几片的错误模式是「显示不对、多点一次就好」。本片的错误模式是：

> 商家重传一份行序调换的报表 → 系统把已锚定的成本丢掉 → 按**今天**的成本快照重算
> → 三个月前那笔订单的利润被永久改写 → 没有任何告警、没有任何日志、`no_op` 也不报。

利润中心的所有下游（环比、导出、SKU 切片）都建在这条锚定链上。**锚定丢一次就补不回来**：
`anchored_at` 会被刷成新时间，`cost_snapshot_id` 指向新快照，库里没有任何记录说明
「这里曾经锚的是另一份成本」。

所以本片的三条硬约束，任何一个不满足就**必须停手**，不许「先合了再说」：

1. **对拍不通过不许切换**（§5）；
2. **R-3 红线不许碰**（已锚定的行绝不重新 as-of）；
3. **Shopify 路径零行为变化**（`source_mode != "csv"` 的记录走原逻辑，逐字节等价）。

---

## §1 侦查结论（施工面全貌，行号基于 `d8b7b57`）

### §1.1 `ingest_orders` 现在是**删除-重建**

`app/external_orders.py:338-500`。对已存在的订单，流程是：

```
:395-397  读出旧行
:400-410  把旧行的「锚定五件套」抄成普通元组 dict，key = line.external_line_id
:412      db.execute(delete(ExternalOrderLine).where(external_order_id == existing.id))   ← 无条件全删
:446-462  按 record.lines 的顺序重建每一行
:463      previous = inherited.get(line.external_line_id)                                  ← 按 line_id 匹配
:469-483  命中且 SKU 相同 → 继承五件套；否则 → as-of 重查
:491      循环外一次性批量锚定（1 条快照查询，R-2 预算）
:493-494  循环外按 (account, sku) 重建日销
:495      db.commit()
```

「先删后建」+「按位置编号」这两件事组合起来，就是 R-68 的全部成因。

### §1.2 R-68 的精确失效链

**行标识是位置生成的**：

- `app/connectors.py:313`：`line_id = str(line.get("external_line_id") or line.get("line_id") or f"line:{line_index}")`
- 订单的列映射表里**没有** `external_line_id`（`app/column_mappings.py`），所以每条 CSV 行都得到 `line:0`
- `app/order_aggregation.py:241-257`（M5）：只在 id **重复**时才整组重编号成 `line:0..n-1`

**于是商家把 `A,B,C` 重传成 `C,A,B` 时**：

| | 旧库 | 新 payload |
|---|---|---|
| `line:0` | A（已锚定成本 10.00） | **C** |
| `line:1` | B | **A** |
| `line:2` | C | **B** |

`inherited = {"line:0": A的五件套, "line:1": B…, "line:2": C…}`，重建时 `line:0` 的新 SKU 是 C，
`:472` 的 `previous[0] == line.external_sku` **不成立** → 不继承 → 落到 `:484` 的 as-of 重查分支。

**结果不是「用错成本」（这层保护还在），而是「丢弃正确锚定、按今天的快照重算」**：

- 若今天补录了更高的成本 → 历史利润**变小**；
- 若原快照后来被删/软删 → `cost_status` 从 `resolved` 变 `missing` → 该行利润**从有变无**；
- `payload_hash` 因行序变化而变 → 走 `updated` 而非 `no_op`，**没有任何异常迹象**。

### §1.3 R-69 的精确失效链

`:412` 的 DELETE **没有 WHERE 限定本批的行**。所以：

- 文件 1 含订单 O 的 `A` 行 → 入库；
- 文件 2 含订单 O 的 `B` 行（商家分两次导出）→ `existing` 命中 → **删掉 A** → 只重建 B；
- A 的行永久消失，`touched` 触发 `_rebuild_key` 重建日销 → 当日该 SKU 销售额凭空减少；
- 新记录的 `data_completeness` 仍是 `complete`（见 §1.4），库里**看不出任何缺失痕迹**。

### §1.4 一个会误导设计的假线索：`data_completeness` **不能**当作「是否完整快照」

`app/connectors.py:343`：

```python
data_completeness="partial" if reason or row.get("status_reason") else "complete"
```

它的语义是「**这行有没有状态原因**」，**不是**「这份文件是不是订单的全量快照」。
所以 R-69 的修复**不能**用它判断「该不该保留旧行」——全量导入和增量导入都会被标成
`complete`。必须引入显式口径（§2 D3）。

### §1.5 五个调用点（改动的影响面）

| 位置 | 端点 | 路径 |
|---|---|---|
| `app/main.py:3469` | `/api/external/orders/ingest` | JSON 批量 |
| `app/main.py:4482` | `/api/external/imports` | **CSV 提交（本片主战场）** |
| `app/main.py:2684` | `_shopify_sync_run` | Shopify 同步 |
| `app/main.py:3039` | `_shopify_resume_run` | Shopify 续跑 |
| `app/platform_sync.py:65` | 平台同步 | 平台 |

后三个走 `source_mode = "shopify_live"` / `"mock"`，**本片对其行为要求是逐字节不变**。

### §1.6 六个必踩的坑

**坑 1：`pending` / `touched` 是循环外的共享状态**

`:371` 的 `pending` 与 `:369` 的 `touched` 在 `for record in records` 外面声明，
`:491`/`:493` 在循环后统一消费。阶段 B 一旦引入 savepoint，**单笔回滚后必须把该笔
塞进 `pending` / `touched` 的条目剔除**，否则会给已经回滚掉的行做锚定 —— 那些 ORM 对象
已 deassociated，赋值不报错、flush 时静默丢失或抛 `InvalidRequestError`。

**坑 2：`db.begin_nested()` 会隐式 flush**

SQLAlchemy 进入 savepoint 前会 flush 挂起的对象。这会改变 SQL 执行顺序，可能打红
`tests/test_p8s1_order_merge.py::test_f34b_snapshot_query_still_one`（R-2 的「整批 1 条快照查询」）。
savepoint 的位置必须放在**不新增查询**的地方，且阶段 B 要重跑这条用例。

**坑 3：`external_line_id` 改规则会与存量数据打架**

库里现有的 CSV 行 id 是 `line:0` / `line:1`。若只改生成规则不做兼容，存量订单第一次重传时
新旧 id 对不上 → 锚定全部丢失一次（**正好是我们要修的那个 bug，只是从别处冒出来**）。
§2 D2 给了「双索引兜底」的平滑方案，**必须实现**。

**坑 4：唯一约束 `uq_external_order_line_identity`**

`app/db.py:829`：`UniqueConstraint(workspace_id, external_order_id, external_line_id)`。
新的确定性 id 必须保证**同一订单内唯一**。指纹里必须带「同 SKU 内序号」，只带 SKU 会在
「同一订单同一 SKU 出现两次」时撞约束（`IntegrityError` → 500）。

**坑 5：`imports._isolate` 的「记录数 == 行数」不变量**

`app/imports.py:133-169`：解析层用「记录数 == 数据行数」做静默丢行检测。
聚合发生在 `ingest_orders` 入口（`:351`），**在解析之后**，所以不受影响 —— 但**第二轮的
行级 MERGE 会让「记录数」与「最终入库行数」不再相等**，任何拿「行数」做断言的测试都要重新核对。

**坑 6：R-65 的「整批原子」是一条有意契约**

`:354-355` 的注释写得很清楚：整批要么全进要么全不动，因为「半批入库 + 500 会让订单事实与
批次历史对不上」。阶段 B 的 SAVEPOINT **会改变这条契约**（变成「单笔原子 + 批次部分成功」）。
这是**用户明确要求的变更**，但必须同步处理：

- `SalesImportStats` 增加 `rejected_orders` / `rejected_reasons`（不进响应 schema）；
- 批次 run 的状态在有失败时要能表达「部分成功」（`app/external_sync.py:559` 已有 `partial` 语义可复用，
  **但要先核实它在订单导入路径上是否可达**，不可达就退回「run 记 partial 需新增判断」并登记偏差）。

---

## §2 核心设计决策（D1/D2/D3 已定稿；D4 是前端时序约束；D5 是 `incremental` 的四个连带问题）

### D1 —— 行指纹 = `(external_sku, 该 SKU 在订单内的出现序号)`

**不是** `(external_sku)`，也**不是** `(位置)`。

理由：锚定五件套 `(cost_status, cost_snapshot_id, unit_cost, fee_rate, anchored_at)`
全部是 **SKU 级或订单级事实**，与「这一行的数量和金额」无关。所以只要 SKU 对得上，
锚定就是对的；同一 SKU 出现多行时，成本快照本来就相同，行间互换**不影响正确性**。

代价（明确接受）：同一订单同一 SKU 的两行如果数量不同且互换，行级数量归属会跟着位置走。
**这不影响利润**（成本按 SKU、收入按行求和），所以不予处理。

### D2 —— `external_line_id` 稳定化 + 双索引兼容

**新规则**（仅对合成 id，即 `external_line_id.startswith("line:")` 的行）：

```
指纹 id = f"csv:{sku_token}|{seq}"
   sku_token = external_sku 若 len(external_sku) <= 100 且不含 '|'
               否则 = sha1(external_sku)[:16]
   seq       = 该 SKU 在本订单内第几次出现（0-based）
```

- 长度上限 128（`db.py:835` 是 `String(128)`），超长走 hash；
- **平台真实 id 一律不动**（Shopify 的 id 不以 `line:` 开头）；
- 规范化位置：**`ingest_orders` 入口、聚合之后、R-65 判重之前**，对所有
  `source_mode == "csv"` 的记录统一处理（不能只写在 `merge_lines` 里 —— 单行订单不走 merge，
  否则同一订单「3 行聚合」与「1 行」会得到两套 id 规则）。

**双索引兼容（存量数据平滑）**：

```
inherited_by_fp  = {(sku, seq): 五件套}      # 新指纹
inherited_by_id  = {external_line_id: 五件套} # 旧 id 兜底
匹配顺序：先按指纹 → miss 再按旧 external_line_id → 都 miss 才 as-of 重查
```

这样存量订单第一次重传时仍能按旧 id 继承锚定，**不会因为改规则而丢一次锚定**。

⚠️ **旧行的 `seq` 必须从 id 解析，不能靠查询返回顺序**：`:395-397` 的
`select(ExternalOrderLine).where(...)` **没有 `ORDER BY`**，返回顺序不受保证。
正确做法：

- 旧 id 形如 `csv:SKU|seq` → 直接解析出 seq（确定性）；
- 旧 id 形如 `line:N` → **先按 N 数值升序排序**，再对同 SKU 依次累加 seq（确定性）；
- 两种都不匹配（理论上不存在）→ 不计入 `inherited_by_fp`，只能靠 `inherited_by_id` 兜底。

这条若写错，行为会随 SQLite 的返回顺序漂移 —— 测试可能局部绿、线上偶发丢锚定，
是最难查的一类 bug。

### D3 —— R-69 的口径开关：**已裁定采纳方案 A**（用户 2026-11-08）

现状下「本批只有部分行」与「商家真的删了一行」在数据上**不可区分**（§1.4）。

| 方案 | 做法 | 结论 |
|---|---|---|
| **A 加显式开关** | CSV 提交端点（`main.py:4412`）增加可选参数 `line_mode`，取值 `full`（默认，现状）/ `incremental` | ✅ **采纳** |
| B 一律 MERGE | 永不删除旧行，只 upsert | ❌ 真正的删行永远不生效，商家改单后系统保留幽灵行 |
| C 自动推断 | 按「单头金额 vs 行金额之和」猜是否完整 | ❌ **用户明确：绝对禁止**。P8-S1 的 M3 三态已证明金额形态太多，判据不可靠 |

**裁定要点（写死，不许自行发挥）**：

1. `line_mode` 是 **`Form(default="full")`** 的可选参数，**默认值必须保持现状行为**——不传即 `full`，与今天逐字节等价。
2. 取值非法（非 `full`/`incremental`）→ `422`，**不许静默降级到 `full`**（降级 = 悄悄删行，正是 R-69 本身）。
3. **`full` 分支的代码路径一个字符都不改**（仍是删除-重建）。`incremental` 是**新增分支**，不是改写原分支。
4. `line_mode` 仅对 `kind == "orders"` 生效；`inventory` / `costs` 分支**忽略**该参数（不报错、不生效）。

### D4 —— 前端开关的**时序**：语义在阶段 B 才有，勾选框必须在阶段 B 落地

用户裁定「前端必须加」。但这里有一个**会造成假开关的时序陷阱**，必须在任务书里堵死：

> `incremental` 的真正语义（保留旧行）在**阶段 B** 才实现。若阶段 A 就把勾选框挂上去，
> 商家勾了会以为「旧行被保留了」，而后端此时仍是删除-重建 → **比没有开关更危险**：
> 它把一次静默删行包装成了用户主动授权的操作。

因此拆成两步，**两步都必须做**：

| 轮次 | 做什么 | 为什么不能合并 |
|---|---|---|
| **阶段 A** | 端点**预埋** `line_mode` 参数 + 严格校验；对 `incremental` **返回 422 明确拒绝**（错误码 `LINE_MODE_NOT_ENABLED`，文案「增量导入尚未启用」）。前端**零改动**、绝不加载勾选框 | 第一轮冻结接口契约，让第二轮只改内部实现、不改接口签名；且 422 让「假开关」在任何路径上都不可达 |
| **阶段 B** | 实现 MERGE + 把 422 换成真实现 + 前端勾选框上线 | 勾选框与语义同轮上线，不存在中间态 |

**阶段 B 前端要求（`app/templates/sources.html`，与既有 `import-allow-skipped` 范式对齐）**：

- DOM：在 `:29` 的 `<label class="import-skip">…跳过异常行</label>` **之后**加
  `<label class="import-skip"><input type="checkbox" id="import-incremental"> 增量导入（仅更新本批存在的行）</label>`，
  复用既有 `import-skip` 类，**零新增样式**；
- 提示条：仿 `:31` 的 `#import-skip-hint`，加 `<div id="import-incremental-hint" class="import-warn" hidden>本批未出现的旧订单行将保留；如需删除已取消的行，请改用全量模式重传</div>`；
- JS：`importRequest`（`:54`，单物理行）内**只在 commit 且勾选时** append：
  `if(!path.endsWith('/preview')&&$('#import-incremental').checked)form.append('line_mode','incremental');`
  —— **preview 端点不传**（预览无旧行可比，传了是无意义参数）；
- kind 联动：`kind !== 'orders'` 时把勾选框 `disabled` 且强制 `checked=false`、hint 隐藏
  （库存/成本导入不涉及订单行，暴露这个开关会让商家误以为能增量导入库存）；
- **不改动** `import-allow-skipped` 的任何现有行为与文案。

### D5 —— `incremental` 分支的四个连带问题（阶段 B；**其中两个是二次侦查推翻初判的结果**）

#### 先排除两个「看起来像坑、实际不是」的（**不许动它们**）

我第一版任务书写了「日销必须从库里重新读全部行」「`touched` 要补保留行 SKU」——**二次核验证明这两条都不成立**，
写进实现反而会引入 bug：

- **`_rebuild_key`（`:203-243`）本来就是从库里全量聚合的**：`select(ExternalOrder)` 取该 account 全部订单、
  再按 SKU 取全部行逐行累加，**与「本次 record」零关系**。所以保留行天然被计入，日销不会丢。
- **`touched` 本来就已填满**：`:411` 把旧行 SKU 全量加入（full 模式为删行后减额），`:498` 对本批每一行
  无条件 `add`。所以不要「补 SKU」，**尤其不要把 `:411` 当成 full 专属逻辑删掉**。

→ 阶段 B 对 `_rebuild_key` 与 `touched` 的填充逻辑：**零改动**。

#### 真正成立的四个问题

**问题 1：单头金额会与明细之和不一致 → 新登记 R-74**

在 `if existing` 分支（`order.gross_amount = record.gross_amount` 一带）与新建分支
（`ExternalOrder(gross_amount=record.gross_amount, …)`）里，单头都是**按本批 record 写的**。
首次增量导入（从未全量过）时只传 A，单头就只等于 A，而库里实际有 A+B 两行。

- 影响：**利润引擎不读单头**（P8-S1 已证单向隔离）→ 不影响利润；但「单头 ≠ 明细之和」是数据不一致。
- **修法**：`incremental` 分支在写完行之后，单头金额/退款**按库里该订单全部行重算**
  （`sum(line.gross_amount)` / `sum(line.refund_amount)`）。这需要一次「全部行查询」，
  但**必须在 `db.flush()` 之后**才能读到刚插入的行。
- **不许**改 record 的单头 → 不影响 P8-S1 的 M6 delta 与 R-67 分摊。

**问题 2（最容易被漏、后果最直接）：`stale` 判定会把跨批补行整笔拦掉**

`:388-393`：

```python
newer = (record.event_version is not None and (existing.event_version is None or record.event_version > existing.event_version)) or (
    record.event_version is None and _utc_naive(record.external_updated_at) > _utc_naive(existing.external_updated_at)
)
if not newer:
    stats.stale += 1
    continue        # ← 整笔跳过，一行都不写
```

跨批补行的典型形态：商家分两次导出，文件 2 含订单 O 的 B 行，但其 `external_updated_at`
**与文件 1 相同或更早**（同一订单的两次导出时间可以相同/逆序）→ `not newer` → **整笔 stale 跳过**，
B 行永远补不进来，而用户只看到「跳过 N 条」，**没有任何错误提示**。

→ `incremental` 分支的 `newer`/`stale` 判定必须**下沉到行粒度**：

- 本批出现、库里**没有该 `external_line_id`** 的行 → **无条件插入**（新增行不存在「版本更旧」问题）；
- 本批出现、库里**已有该 id** 的行 → 仍按 `newer` 判定，`not newer` 则该行跳过（并发/旧版本保护不变）；
- 整笔 stale 的概念在 `incremental` 下**不再存在**（只有「本批 0 行生效」时才计入 `stale`）。
- `full` 分支的 `stale` 判定**一个字符都不改**（红线 6）。

**问题 3：`payload_hash` 相同 → `no_op` 会吞掉「同文件重传」之外的场景**

`:385-387` 的 `payload_hash` 是**记录级**（整单），incremental 下两个不同批次的 hash 必然不同，
所以 `no_op` 不会误触发——**这一条是核实后确认无风险**，写在这里是为了让实现者别去动它。

**问题 4：`imports` 的「记录数 == 行数」统计口径（§1.6 坑 5）**

`app/imports.py:133-169` 的静默丢行检测发生在解析阶段、`ingest_orders` 之前，
所以**不受影响**。但批次响应里的「入库行数」在 incremental 下会小于「CSV 行数」，
若前端/台账拿「CSV 行数 == 入库行数」做展示会显示异常 → `SalesImportStats` 需如实区分
`inserted_lines` / `updated_lines` / `skipped_lines`（**不进响应 schema**，至少进 delivery 的日志与测试断言）。

---

## §3 M0（顺手）：R-73 修复 —— T-2 补扫 `main.py`

`tests/test_p5s1_tokens.py:92-108` 的 `sources` 目前是 `tokens.css` + `workspace-shell.css` + 10 模板。

改动：把 `APP_MAIN = ROOT / 'app' / 'main.py'` 加入 `sources`。

**必须做的配套断言**：因为 `main.py` 里现在只有登录页有 `var()`，加进去后若将来别处也引
变量则一并被覆盖。跑之前先用 `grep -oP 'var\(\s*--[A-Za-z0-9_-]+' app/main.py | sort -u`
确认当前是 12 个引用、全部有定义（实测已确认 12/12 命中，见台账 §43）。

**验收**：T-2 仍绿；人为把登录页某个 `var(--color-border)` 改成 `var(--color-bordr)` 时 T-2 必须变红
（这条反证写进测试注释或 delivery，不许只口头说）。

---

## §4 阶段 A：行指纹 + R-68 + R-73 + 端点预埋（第一轮，先做这个）

### A-1 冻结旧实现为对拍基线

把当前 `ingest_orders` **原样**搬成 `_ingest_orders_legacy(db, records, *, workspace_id, sync_run_id)`：

- **一个字符都不许改**（包括注释），只改函数名；
- 仅供 `tests/_p9s3_probe.py` 与对拍测试调用；
- `ingest_orders` 保留原签名（5 个调用点零改动）。

### A-2 指纹模块

新建 `app/order_line_identity.py`（约 60-80 行）：

- `line_fingerprint(sku: str, seq: int) -> str`
- `normalize_csv_line_ids(records: list[ExternalOrderRecord]) -> list[ExternalOrderRecord]`
  - 只处理 `source_mode == "csv"` 且 `external_line_id.startswith("line:")` 的行
  - 返回新 tuple（`ExternalOrderRecord` 是 frozen dataclass，用 `replace`）
- `sku_token(sku: str) -> str`（长度/字符兜底到 sha1）
- **`legacy_seq_map(lines) -> dict[str, tuple[str, int]]`**：把库里旧行映射成 `{旧id: (sku, seq)}`。
  `line:N` 按 N 数值升序排序后对同 SKU 依次计数；已是 `csv:SKU|seq` 的直接解析。
  **A-3 的锚定继承与 §5.2 第 5 条的探针归一化必须调用同一个函数**，不许各写一份
  （两套实现会让「对拍通过」失去意义）。

**必须补的单元测试**（纯函数，不需要库）：

- 长度 100 与 101 的 SKU 分别走原样与 hash 分支；
- SKU 含 `|` 时走 hash；
- 同订单 `[A,A,B]` → `csv:A|0`, `csv:A|1`, `csv:B|0`；
- 结果 < 128 字符。

### A-3 锚定继承改指纹 + 双索引

`app/external_orders.py:400-410` / `:463`：

- 建 `inherited_by_fp` 与 `inherited_by_id` 两个 dict；
- **旧行 seq 的解析按 §2 D2 的规则**（解析 id → 按 N 排序兜底），**不要**依赖
  `:395-397` 的查询返回顺序（该 select 无 `ORDER BY`）；
- `seq` 的计数必须在「重建循环」里对**本订单已见过的同 SKU 行数**累加（用 `Counter`）；
- 匹配顺序：指纹 → 旧 id → as-of；
- **R-3 红线原样保留**：命中后仍是「原样继承五件套，绝不重新 as-of」。

### A-4 探针与对拍基建

见 §5。**A 阶段结束时对拍必须跑通并通过**，否则不许进 B。

### A-5 端点预埋 `line_mode` + 明确拒绝（防假开关的护栏）

`app/main.py:4412 external_import_commit` 的签名（`:4416-4423` 一带）加：

```python
line_mode: str = Form(default="full"),
```

紧随 `allow_skipped: bool = Form(default=False)`（`:4423`）之后，**保持默认 `full`**。

阶段 A 的行为（**这是临时契约，阶段 B 会替换，必须在 delivery 里写明**）：

```python
if line_mode not in ("full", "incremental"):
    raise DomainError(code="LINE_MODE_INVALID", message="line_mode 仅支持 full / incremental", http_status=422)
if line_mode == "incremental":
    raise DomainError(code="LINE_MODE_NOT_ENABLED", message="增量导入尚未启用，请使用全量模式", http_status=422)
```

- 仅 `kind == "orders"` 时校验；`inventory`/`costs` 分支直接忽略该参数（**不许报错**）；
- 前端**零改动**（阶段 A 不加载任何勾选框，见 §2 D4）；
- 五个 `ingest_orders` 调用点**本轮零改动**（阶段 A 不改 `ingest_orders` 签名）。

### A-6 阶段 A 的验收线

- R-68 专项：行序倒置重传后，五件套逐字段不变（§6 T-R68-1/2）；
- 组 1 对拍全部一致（§5）；
- `incremental` 被 422 拒绝、非法值被 422 拒绝（§6 T-LM-1/T-LM-2）；
- 全量回归 0 failed。

---

## §4B 阶段 B：SAVEPOINT + 跨批 MERGE + 前端开关（第二轮，A 验收通过才许开）

### B-1 `ingest_orders` 加可选参数（5 个调用点只改 1 个）

```python
def ingest_orders(db, records, *, workspace_id=..., sync_run_id=..., line_mode: str = "full"):
```

- **默认 `full`**，除 CSV 提交外的四个调用点（`main.py:3469` JSON 批量、`:2684`、`:3039`、
  `platform_sync.py:65`）**一个字符都不改**；
- 只有 `main.py:4482`（CSV 提交）传 `line_mode=line_mode`；
- `full` 分支 = 现有删除-重建逻辑**原样保留**（`:412` 的 DELETE 不动），
  `incremental` 是并列的**新分支**。

### B-2 `incremental` 分支的五条实现要求

1. **不 DELETE**：按 `external_line_id` 逐行 upsert（存在则更新字段，不存在则插入），
   本批未出现的旧行**原样保留**；
2. **锚定继承仍走 D2 的双索引**（指纹 → 旧 id → as-of），对**保留行**不重新 as-of（R-3 红线）；
3. **`newer`/`stale` 判定下沉到行粒度**（§2 D5 问题 2）：库里没有该 id 的行无条件插入；
   已存在的行仍按 `newer` 判定；整笔 stale 的概念在 `incremental` 下取消。
   **不要**把这个改动带进 `full` 分支；
4. **单头金额/退款按库里全部行重算**（§2 D5 问题 1，关 **R-74**），必须在 `db.flush()` 之后；
5. **`_rebuild_key` 与 `touched` 的填充零改动**（§2 D5 已核实二者本就基于全库/全行，
   改它们才会引入 bug）。

### B-3 SAVEPOINT 单笔隔离（`full` 与 `incremental` 都要生效）

- 每笔订单包一层 `with db.begin_nested():`（**先核实** `Session` 由谁创建、是否已在事务中，
  `begin_nested()` 在无外层事务时会自己开一个，语义会变——这一点必须在 delivery 里写实测结论）；
- 异常 → savepoint 回滚 → **必须从 `pending`（`:371`）与 `touched`（`:369`）剔除该笔条目**（§1.6 坑 1），
  否则会给已回滚的 ORM 对象做锚定；
- 失败笔计入 `SalesImportStats.rejected_orders` / `rejected_reasons`（**不进响应 schema**）；
- **R-2 必须重跑**：`test_f34b_snapshot_query_still_one`（§1.6 坑 2，`begin_nested()` 隐式 flush 会改查询序）；
- 批次 run 的部分成功语义：先核 `app/external_sync.py:559` 的 `partial` 在该路径是否可达，
  不可达就新增判断，**不许**把部分成功写成 `succeeded`。

### B-4 前端勾选框上线（§2 D4 的完整要求）

`sources.html` 加 DOM + hint + JS + kind 联动，复用 `import-skip`/`import-warn` 既有类，**零新增样式**。

### B-5 把阶段 A 的临时 422 换成真实现

删掉 `LINE_MODE_NOT_ENABLED` 分支（`LINE_MODE_INVALID` 的非法值校验**保留**），
`incremental` 走 B-2。**这一步是阶段 B 完成度的证据**：阶段 A 的 T-LM-1 断言 422，
阶段 B 必须把它改成断言「生效」（有意契约变迁，台账登记）。

---

## §5 探针对拍方案（本片的核心安全网）

### §5.1 一个必须先讲清楚的问题：**「完全一致」在这里是不成立的**

用户要求「新旧逻辑同时跑，比对行数、金额、锚定 ID，完全一致才允许切换」。
但对 **R-68 / R-69 场景**，新旧结果**本来就该不同** —— 完全一致说明**根本没修**。

所以对拍必须分三组，判据不同：

| 组 | 场景 | 判据 | 不一致时 |
|---|---|---|---|
| **组 1 必须完全一致** | 单订单单行 / 多 SKU 聚合 / 同文件重传（`no_op`）/ 金额三态 / 退款分摊 / Shopify 与 mock 路径 / 状态冲突退回 R-65 | **逐字节一致**（行数、每行金额与数量、五件套全部字段、`DailySkuSale` 全部数值）。**唯一例外：`external_line_id` 按 §5.2 第 5 条归一化后比，不做字面比** | **回归，禁止切换** |
| **组 2 必须不同且新逻辑正确** | ① 行序倒置重传 ② 跨批补行（B 阶段） | 旧：锚定丢失（`anchored_at` 被刷新 / `cost_status` 变化）/ 丢行；新：五件套原样保留 / 行数正确 | 若「一致」→ 修复未生效 |
| **组 3 必须完全不变** | R-65 的 422、`_isolate` 记录数不变量、`payload_hash` 语义、响应体键集 | 一致 | 回归 |

### §5.2 实现方式

`tests/_p9s3_probe.py`（下划线开头，pytest 不收集）：

```python
def run_both(ctx, records, *, workspace_id) -> tuple[Snapshot, Snapshot]:
    """在同一份 fixture 上分别跑 legacy 与 new，各返回一个数据库快照。"""
    legacy = _snapshot_after(ctx.db_factory, lambda db: _ingest_orders_legacy(...))
    current = _snapshot_after(ctx.db_factory, lambda db: ingest_orders(...))
    return legacy, current

def snapshot(db) -> dict:
    """五张表的可比较快照：订单头、订单行（含五件套）、日销、成本快照引用、统计。"""
```

**关键实现要求**：

1. **两个独立库/会话**：legacy 与 new 各跑一次必须互不污染。用 `tmp_path` 起两个 SQLite 文件
   （沿用 `tests/test_p8s1_order_merge.py` 的 fixture 范式），不要在同库上「先跑再清」。
2. **快照必须是全字段**：`ExternalOrderLine` 的 `(external_line_id, external_sku, ordered_qty,
   cancelled_qty, refunded_qty, gross_amount, refund_amount, cost_status, cost_snapshot_id,
   unit_cost, fee_rate, anchored_at)` 一个都不能漏 —— 只比金额会漏掉 R-68 本身。
3. **`anchored_at` 的比对用「是否为空 + 是否等于旧值」**，不要比绝对时间（两次运行时间不同）。
4. 探针**不进产品代码**，只活在 tests 下；A 阶段验收后可以保留作为回归资产，
   但 `_ingest_orders_legacy` 若要删除必须另开切片（**本片保留**）。
5. **`external_line_id` 绝不能做字面比对（这条不写清，组 1 永远不可能过）**：
   A-3 是**有意**把 id 从 `line:0` 改成 `csv:SKU|seq`，两侧字面值**必然不同**。
   若拿它进组 1 的「逐字节一致」，实现者唯一出路就是放弃修 R-68。
   → 两侧都**归一化成 `(external_sku, 同 SKU 内序号)` 再比**：
   - new 侧：`csv:SKU|seq` 直接解析（hash 分支无法反解 → 用「按 sku 分组后的出现顺序」）；
   - legacy 侧：`line:N` **按 N 数值升序**排序后，对同 SKU 依次计数（与 A-3 的旧 seq 解析**同一套规则**，
     所以 A-3 要求把它抽成可复用函数，探针直接调用，不许各写一份）。
   - 并强制断言**双射**：两侧归一化后的 id 集合必须**大小相同且一一对应**（不是「随便映射一下」）。
6. **比对前必须排序**：两侧快照按 `(external_sku, seq)` 排序后逐行比 —— `:395-397` 的 select
   **没有 `ORDER BY`**，返回顺序不受保证，按位置比会随机红。
7. **`payload_hash` **含** `external_line_id`，所以「位置」与「不重算」两条必须同时满足**
   （2026-11-09 复核更正：初版写的「不含 id」**是错的**，引用的 `connectors.py:255` 属 inventory 路径）：
   - 行级：`connectors.py:313-320` —— `line_payload = {**line, "external_line_id": line_id}` → `_payload_hash(line_payload)`；
   - 记录级：`:330-332` canonical 的 `lines` 取 `line.__dict__`（含 `external_line_id`）→ `:340` `_payload_hash(canonical)`。
   → **结论仍成立，但理由变了**：`payload_hash` 是**上游载荷**的指纹（来自 CSV 的 `line:N`，不读库），
   所以只要「规范化放在聚合之后」且「**绝不重算 hash**」，存量重传仍是 `no_op`、R-65 分组不漂移。
   **若把规范化提前到聚合之前、或顺手重算 hash，`no_op` 立刻失效** —— 阶段 B 必须守住这两条。

### §5.3 切换许可

**组 1 全一致 + 组 2 全按预期不同 + 组 3 全不变 = 允许切换。**
任何一条不满足 → 停下，在 delivery 里写清哪个用例、哪个字段、新值旧值各是什么。

**补充判据（组 1 的 id 部分）**：`external_line_id` 按 §5.2 第 5 条归一化后必须**双射**
（集合大小相等 + 一一对应）。归一化后少一行 / 多一行 / 两行撞同一键，**都算回归**。

---

## §6 测试清单

### 阶段 A（11 条集成用例 + 4 条纯函数单测）

| 编号 | 用例 | 断言要点 |
|---|---|---|
| T-R68-1 | 行序倒置重传：锚定五件套不丢 | 先传 `A,B,C`（A 锚成本 10.00）→ 再传 `C,A,B` → 三行 `cost_snapshot_id` / `unit_cost` / `fee_rate` / `anchored_at` **逐字段等于第一次** |
| T-R68-2 | 行序倒置后利润不变 | 利润中心该订单利润**一分钱不变**（这是财务意义上的最终判据） |
| T-R68-3 | 行序倒置后 `external_line_id` 稳定 | 第二次的 id 集合 == 第一次（D2 的新规则） |
| T-R68-4 | 存量数据兼容 | 手工把库里的 id 改回 `line:0..` → 重传 → 锚定仍被继承（验证 D2 的双索引兜底） |
| T-R68-5 | 同 SKU 多行不撞唯一约束 | `[A,A,B]` 入库成功，无 `IntegrityError`，id 为 `csv:A\|0`/`csv:A\|1`/`csv:B\|0` |
| T-R68-6 | 平台真实 id 不被改写 | `external_line_id = "99001"`（Shopify 风格）原样保留 |
| T-PROBE-1 | 组 1 对拍 | legacy vs new 逐字段一致 |
| T-PROBE-2 | 组 2 对拍 | 行序倒置下二者**必须不同**，且 new 的五件套未被刷新 |
| T-R73 | T-2 覆盖 `main.py` | 见 §3 |
| T-LM-1 | `incremental` 在阶段 A 被明确拒绝 | CSV 提交带 `line_mode=incremental`（`kind=orders`）→ **422 + `LINE_MODE_NOT_ENABLED`**；**不许** 200/不许静默当 full |
| T-LM-2 | 非法值 + 非订单 kind | `line_mode=xx` → 422 `LINE_MODE_INVALID`；`kind=costs` 带 `line_mode=incremental` → **正常入库、不报错**（该参数对非订单 kind 无效） |

A-2 的 `order_line_identity` 还需 **4 条纯函数单测**（不占集成用例额度，见 §4 A-2）。

> 阶段 A 合计预期新增 **11 条集成 + 4 条纯函数 = 15 条**（667 + 15 = **682**）。
> 用户估的「≥680」按 13 条算；多出的 2 条是 A-5 端点预埋的两条防假开关护栏（T-LM-1/2），
> **不是可选项**——没有它们，「勾了却删行」的假开关无人看守。

### 阶段 B（至少 9 条）

| 编号 | 用例 | 断言要点 |
|---|---|---|
| T-R69-1 | 跨批补行 | 文件 1 传 `A`、文件 2 传 `B`（`line_mode="incremental"`）→ 订单有 2 行，A 的成本锚定未变 |
| T-R69-2 | `full` 模式行为不变 | 同样场景 `line_mode` 缺省 → 仍只有 B（现状），证明默认零变化 |
| T-R69-3 | 跨批补行后日销正确 | `DailySkuSale` 中 A、B 的销售额**都正确**，与「一次性全量导入 A、B」逐值相等（**锁死 `_rebuild_key` 不被改坏**，不是要求改它） |
| T-R69-4 | 单头 == 明细之和（**关 R-74**） | 只增量导入过 A 再补 B 后，`ExternalOrder.gross_amount == sum(行 gross_amount)`，退款同理；且 record 的单头未被改写 |
| T-R69-5 | **`stale` 不拦跨批补行**（D5 问题 2） | 文件 2 的 `external_updated_at` **早于或等于**文件 1 → B 行**仍然入库**；`stats.stale` 不因该笔 +1。**同时**：已存在行的旧版本 payload 仍被 stale 保护（两个方向都要断言） |
| T-SP-1 | 单笔失败不污染批次 | 构造第 2 笔必失败（如 SKU 超长/映射异常）→ 第 1、3 笔仍入库，`rejected_orders == 1`，DB 无第 2 笔残留 |
| T-SP-2 | 失败笔的副作用已清理 | 失败笔的行**不得**出现在 `pending` 锚定结果里（`cost_status` 无残留），`DailySkuSale` 无该笔金额 |
| T-UI-1 | 前端开关存在且只在 commit 传参 | `sources.html` 含 `id="import-incremental"`；`importRequest` 含 `path.endsWith('/preview')` 守卫（**preview 不传**）；未勾选时请求体**无** `line_mode` |
| T-UI-2 | 非订单 kind 禁用 | `kind !== 'orders'` 时勾选框 `disabled`（断言模板含该逻辑，不跑浏览器） |

> T-UI-1/2 是**模板静态断言**（Python 读文件 + 正则），不引入前端测试框架——沿用
> `tests/test_api_dashboard.py:214-218` 的既有范式。**不许**为了这两条引入 Playwright/selenium。

---

## §7 红线

1. **R-3 不可破**：已锚定（`cost_status in ANCHORED_COST_STATUSES` 且 SKU 相同）必须原样继承，
   任何情况下不得重新 as-of。
2. **R-2 查询预算不可破**：`test_f34b_snapshot_query_still_one` 必须仍绿（整批 1 条快照查询）。
   阶段 B 加 savepoint 后**必须重跑这条**。
3. **Shopify / mock 路径零行为变化**：`source_mode != "csv"` 的记录，指纹规范化**不生效**
   （id 保持平台原值），继承逻辑与现状等价。
4. **R-65 的 422 不变**：同批内容冲突仍整批拒绝，错误码与文案不动。
5. **零新迁移**：不新增列、不新增表。
6. **接口变更仅限 `line_mode`**（可选参数、默认 `full`、仅对 `kind=orders` 生效）；其余端点签名不动。
   非法值必须 422，**禁止静默降级到 `full`**。
7. **不删 `_ingest_orders_legacy`**：它是本片的对拍基线，删它要另开切片。
8. **不许为了让对拍通过而放宽快照比对字段**（§5.2 第 2 条）。这是本片最容易自欺的地方。
9. **阶段 A 禁止提前挂前端勾选框**（§2 D4）：没有 `incremental` 实现就暴露开关 = 把静默删行
   包装成用户授权。阶段 A 的 `incremental` 必须**明确 422**，不许「接受但按 full 执行」。
10. **`incremental` 分支的两条硬约束**（§2 D5）：
    - **单头金额/退款必须按库里该订单的全部行重算**（关 R-74），不许沿用「只等于本批」的值；
    - **`stale`/`newer` 判定必须下沉到行粒度**：本批新增的 `external_line_id` 无条件插入，
      否则商家分批导出时第二笔会被整笔 `stale` 静默跳过（D5 问题 2）。
    - 同时**不许**改 `_rebuild_key`（本就全库聚合）与 `touched` 的填充（本就含旧行 + 本批全部行）。
11. **`line_mode="full"` 分支（含 `stale` 判定、DELETE、单头赋值）一个字符都不改**：
    本片所有新逻辑都在 `incremental` 这条并列分支上，任何对 full 分支的「顺手优化」都视为越界。

---

## §8 验收闸

1. **定向**（阶段 A）：

```bash
/d/python/python/python.exe -m pytest -q tests/test_p8s1_order_merge.py tests/test_p9s3_order_upsert.py tests/test_p5s1_tokens.py tests/test_p9s1_profit_enhance.py
```

2. **对拍**：`pytest -q tests/test_p9s3_order_upsert.py -k probe -v`，输出必须能看到组 1/组 2 的逐组结论。

3. **全量**（剥垫片）：

```bash
env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe -m pytest -q -p no:cacheprovider
```

   **阶段 A 预期 `≥ 682 passed / 0 failed`**（667 + 11 条集成 + 4 条纯函数单测）。
   **阶段 B 完成后预期 `≥ 691`**（682 + B 阶段 9 条）。
   **有 flaky 需登记并重跑。** 若实际新增条数与本文档不符，以 delivery 里如实登记的为准，
   但阶段 A **不许少于 15 条**（11 集成 + 4 单测）、阶段 B **不许少于 9 条**。

4. **手工确认项**（写进 delivery）：
   - `grep -c "def _ingest_orders_legacy" app/external_orders.py` == 1；
   - **A-1 的「一个字符都不许改」必须自证**：贴出
     `git diff <A-1 改动前 SHA> -- app/external_orders.py` 中 legacy 函数体部分，
     或给出「用脚本比对 legacy body 与原 `ingest_orders` body 逐行相等」的实测输出；
   - 行序倒置前后 `anchored_at` 的**原始值**抄在 delivery 里（不是「一致」两个字，要贴值）；
   - `git diff --stat` 不含 `app/db.py`（零迁移）；
   - 阶段 A 的 `LINE_MODE_NOT_ENABLED` 是**临时契约**，delivery 必须显式声明「阶段 B 将删除该分支」；
   - **任务书本身必须随第一轮一起提交**（P9-S2 出过这个坑：`dev_task.*.md` 未 `git add` 时
     `git diff --stat` 里不显示，交付说明因此漏报文件）。`git show --stat <HEAD>` 里必须能看到
     本文档与新建的测试文件。

---

## §9 两轮交接

**第一轮（阶段 A）**：

```bash
cd /d/ai/dianshang && claude --permission-mode acceptEdits "读 dev_task.p9s3-order-upsert.md 全文，但只实施 §3（R-73 修复）与 §4（阶段 A：A-1~A-6）。§4B（阶段 B）与 §2 D5 一律不要实施——line_mode 在阶段 A 只做「参数预埋 + 对 incremental 返回 422 拒绝」，不做 MERGE、不加前端勾选框。严格按 §6（阶段 A）的 11 条集成 + 4 条纯函数用例补齐，§5 的探针三组对拍必须跑通（注意 §5.2 第 5 条：external_line_id 按归一化双射比对，不做字面比对），§7 的全部红线与 §8 的验收闸必须满足。完成后：1) 把交付说明写到 .testtmp/delivery.p9s3a.md（含改动文件清单+行号、未做项、偏差、对拍三组逐组结论、行序倒置前后 anchored_at 的原始值、legacy body 与原 ingest_orders 逐行相等的自证）；2) 自跑定向用例和全量回归（预期 ≥682），把实测通过数写进交付说明；3) 提交到当前分支，不要 push。"
```

**第二轮（阶段 B，仅在第一轮验收通过后）**：

```bash
cd /d/ai/dianshang && claude --permission-mode acceptEdits "读 dev_task.p9s3-order-upsert.md 的 §2 D3/D4/D5、§1.6 坑 1/2/6、§4B（B-1~B-5）、§6（阶段 B 9 条）、§7、§8，实施阶段 B。基线是阶段 A 收口后的 HEAD。必须做到：1) line_mode=incremental 真实现，但按 §2 D5 精确落地——只改「单头按库里全部行重算」（关 R-74）与「stale 判定下沉到行粒度」，_rebuild_key 与 touched 的填充零改动；2) SAVEPOINT 单笔隔离，失败笔从 pending/touched 剔除并计入 rejected_orders；3) sources.html 加勾选框（preview 不传参、kind!=orders 时禁用）；4) 删掉 LINE_MODE_NOT_ENABLED 分支并把 T-LM-1 从「断言 422」改成「断言生效」。完成后：1) 交付说明写到 .testtmp/delivery.p9s3b.md（必须包含 test_f34b_snapshot_query_still_one 的重跑结果、begin_nested 外层事务的实测结论、T-LM-1 契约变迁说明）；2) 自跑定向用例和全量回归（预期 ≥691），把实测通过数写进交付说明；3) 提交到当前分支，不要 push。"
```

---

## §10 三项裁定的存档（2026-11-08，已固化，不再是待决项）

| # | 问题 | 用户裁定 | 落点 |
|---|---|---|---|
| 1 | D3 用方案 A 吗？ | ✅ **采纳 A**：可选参数 `line_mode`，**默认 `full` 保持现状**；**绝对禁止方案 C 自动推断** | §2 D3、§4 A-5、§4B B-1/B-5 |
| 2 | 前端要不要加选项？ | ✅ **必须加**：导入界面加勾选框「增量导入/仅更新本批存在的行」，**只有勾选才传 `incremental`** | §2 D4（含时序拆分与 DOM/JS 精确要求）、§4B B-4 |
| 3 | `csv:SKU\|seq` 有无外部依赖？ | ✅ **可以改**：内部系统无外部对账依赖，**但双索引兼容必须实现**（旧 `line:N` 仍能继承锚定） | §2 D2、§4 A-3、§6 T-R68-4 |

**唯一新增的、裁定之外的设计决定**（已在上文说明理由，供复核时质疑）：

- `line_mode` 在**阶段 A 就预埋**并对 `incremental` 返回 422 —— 目的是冻结接口契约、
  并让「勾了却删行」的假开关在阶段 A 不可达（§2 D4、§7 红线 9）。
- 新登记 **R-74**（单头金额与明细之和不一致），随阶段 B 一起关闭（§2 D5 陷阱 3）。
  `grep -rn "R-74"` 全仓零命中，编号未占用。
