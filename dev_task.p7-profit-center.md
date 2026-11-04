# dev_task.p7-profit-center — P7 利润核算与对账中心（Profit Center）

- 版本：**v1.3（S1/S2 已交付，S3 施工规格与口径见 §15）**
- 基线：`feat/warehouse-mvp@7cf2f09`，全量 **596 passed / 0 failed**（S2 交付后）
- alembic head：`0025_cost_snapshots`（S2/S3 均无需新迁移）
- 状态：**S1 完成；S2 完成；S3 实施中。**

## 0.3 S3 裁定记录（2026-11-04 10:19）

用户四条核心口径（原话要点，全部写死在 §15.2 并从代码常量 `app/finance.py :: FORMULA` 对外暴露）：

1. **净数量**：直接用 `ExternalOrderLine` 的有效数量（净数），即
   `ordered_qty − cancelled_qty − refunded_qty`，下限 0；
2. **平台费**：按**实付金额**扣除（不按商品原价），公式写进任务书并在前端加 `title` 悬浮说明；
3. **前端**：利润卡片可点击下钻到明细列表；排行表可点击跳转到该 SKU 的销售记录；
4. **验收闸**：F-17（手算对拍）与 F-18（改费率后历史不变）。

R-65 / R-66 裁定（同日）：

| 编号 | 内容 | 裁定 |
|---|---|---|
| R-65 | 批内重复订单号撞唯一约束 → 500「报表导入失败」 | 采纳**方案 A**：入口检测，422 + 明确文案，零数据语义变化 |
| R-66 | 系统按订单号自动预聚合（把「一行一个 SKU」的展平 CSV 合成带 `lines` 的数字订单） | 登记为后续优化项（P8+）。理由：真实电商 CSV 全是一行一个 SKU，让商家手工合并反人类；但合并规则（金额求和 or 取首行、行项目 ID 怎么造）猜错会安静写错账，必须单独设计 |

## 0.2 S2 裁定记录（2026-11-04 09:48）

用户四条红线（原话要点，已逐条落入代码注释与测试）：

1. `ingest_orders` 对新订单行执行 as-of 抓取（`effective_time <= T_sale` 最近一条），
   锚定 `cost_snapshot_id` / `unit_cost` / `fee_rate`；
2. **严防「删除重建导致锚定丢失」**：已锚定行必须**直接继承**，绝不能重新查成本表；
3. 回填端点必须支持 `dry_run`、只补 `missing`，并验证历史 `resolved` 行利润不变；
4. **F-11 / F-12 一字不差跑通**：F-11 = 锚定后插入新快照利润绝对不变；
   F-12 = 订单二次 ingest 锚定五件套不丢。

**编号重排（本版生效，覆盖 v1.1 的 S2 编号）**：

| 新编号 | 含义 | v1.1 旧编号 |
|---|---|---|
| F-11 | 锚定固化（★核心红线） | 原 F-12 |
| F-12 | 重建继承（★必踩坑） | 原 F-13 |
| F-13 | as-of 时间对齐 | 原 F-11 |
| F-14 | 回填（dry_run / 只补 missing / 幂等 / 日志） | 原 F-14 |
| F-15 | unmapped 与 missing 归因分离 | 原 F-15 |
| F-16 | 同步路径查询预算 ≤1 | 原 F-16 |

## 0.1 裁定记录（2026-11-04 09:17，七项全采纳推荐值）

| # | 裁定 | 备注 |
|---|---|---|
| Q1 | 入库报单价（主）+ 手工 CSV（兜底）双入口 | — |
| Q2 | 按需查询计算，同步路径只锚定不聚合 | 不引 background 任务 |
| Q3 | **费率锚定进快照** | 用户原话：「费率变更是财务大忌，必须写入快照」 |
| Q4 | 允许回填，但显式触发 + `dry_run` + 只补 missing | — |
| Q5 | `Settings` 全局 5% + `platform` override | — |
| Q6 | `unmapped` **不计入**黄色成本提示 | 单独小字说明 |
| Q7 | 三刀：S1 成本入口 → S2 锚定+回填 → S3 引擎+看板 | — |

S1 开工时用户追加两条硬要求（已落入 §13.2 / §13.3）：
1. `InboundReceiveLine` 加 `unit_cost` 的同时，**前端必须有「预估总成本」实时计算展示**；
2. 手工 CSV **必须完全复用 P6 刚修好的「静默丢行」护栏**（计数判定 + 二分隔离），绝不允许脏数据污染成本库。

---

## 0. 一句话前提（必须先看到）

> **本仓库当前不存在任何成本数据。**
>
> `InboundLine` 只有数量、`InventoryTransaction` 只有 `quantity_delta`、
> `PurchaseRequestLine` 只有 `requested_qty`，`Product.current_price` 是**爬虫抓的售价**。
> 全仓 `grep -rn "unit_price|cost_price|unit_cost|purchase_price|成本"` → **零命中**（含 `.py` 与 `templates/`）。

所以「采购单实收确认入库时记录该 SKU 的入库成本单价」这句话，缺的不是代码位置，
而是**成本的输入源**。P7 若不先补入口，上线首日所有订单利润必然是 Unknown。
本草案把「成本入口」提到 S1 首位，不是可选项，是地基。

---

## 1. 侦查结论（2026-11-04 只读核实）

### 1.1 成本数据的真实归属：不存在

| 表 | 现有字段 | 是否含成本 | 结论 |
|---|---|---|---|
| `inbound_lines`（db.py:351） | `expected_qty / received_qty / damaged_qty / note` | ✗ | 只有数量，**无单价** |
| `inventory_transactions`（db.py:371） | `quantity_delta / movement_type / occurred_at` | ✗ | 只有流水数量，**无金额** |
| `purchase_request_lines`（db.py:990） | `requested_qty / source_suggestion_version` | ✗ | 补货系统生成，**无报价** |
| `products.current_price`（db.py:108） | `current_price` | ✗（是售价） | 爬虫写入的对外售价 |
| `product_price_history`（db.py:139） | `price / observed_at` | ✗（是售价） | 售价历史，非成本 |

**结论：用户设想的「成本在 InventoryTransaction 里」不成立。**
`InventoryTransaction` 是纯数量台账（`quantity_delta` 整数），P7 不能从它推导成本，
只能从它推导「何时入库了」这个**时间锚点**。

### 1.2 时间锚点资源充足（这是好消息）

- `inbound_orders.confirmed_at`（db.py:319）—— 确认入库时刻，**语义精确且已存在**
- `inbound_lines.received_qty / damaged_qty` —— 实收与破损，`accepted = received − damaged`
- `inventory_transactions.occurred_at` —— 台账流水时刻
- `external_orders.external_created_at` / `paid_at` —— 销售时刻（UTC naive）

即：**"什么时候发生的"数据齐全，缺的只是"多少钱"**。所以本切片的重心是
「补成本入口 + 钉住时点」，而不是重建时间轴。

### 1.3 订单写入的唯一咽喉：`ingest_orders`

`app/external_orders.py:212 ingest_orders` 是**所有**订单进入系统的唯一入口：

```
main.py:2282  Shopify/mock 同步      → ingest_orders
main.py:2637  平台适配器同步          → ingest_orders
main.py:3056  订单 ingest API        → ingest_orders
main.py:4040  P6 CSV 报表导入        → ingest_orders
platform_sync.py:9                  → ingest_orders
```

**只要在 `ingest_orders` 内挂钩，同步路径与 CSV 导入路径一次性全覆盖，零分叉。**
这正是「订单同步/导入时抓取当时成本」最干净的落点，也满足「不在同步实时路由里做重计算」
——因为这里只做**一次单行 as-of 查询**（O(1)），不做任何利润聚合。

### 1.4 ⚠️ 致命坑：订单更新会 DELETE 订单行再重建

`external_orders.py:247`：

```python
db.execute(delete(ExternalOrderLine).where(ExternalOrderLine.external_order_id == existing.id))
```

`:288` 再逐行 `db.add(ExternalOrderLine(...))` 重建。

**后果**：若把成本锚定字段写在 `external_order_lines` 上，而重建时不继承，
**同一笔订单每被平台更新一次（改价、改状态、补退款），历史成本就丢一次，利润立刻翻成 Unknown。**
这是本切片最容易踩且最难发现的地雷——现象是「上线当天正常，跑几天后订单神秘变 Unknown」。
任务要求：删除前按 `external_line_id` 建旧行 map，重建时继承
`cost_snapshot_id / unit_cost / fee_rate / cost_status / anchored_at`（见 §5.3）。

### 1.5 看板扩展的安全边界（已核实）

- `cockpit.py:114 build_cockpit`：只读聚合，**固定 6 条查询 + as_of 0~2 条，上限 8 条**（模块 docstring 明文约束）。
- `cockpit.py:656` `kpis` 为 `dict`，新增 key 不破坏 `CockpitResponse.kpis: dict[str, CockpitKpi]`（schemas.py:147）。
- `cockpit.html` `renderKpis()` 用**硬编码数组** `['gmv','orders','out_of_stock_skus','overstock_skus','pending_purchase_approvals','knowledge_chunks']` 决定渲染顺序 → 新卡片必须追加到该数组。
- `test_p4_cockpit.py`（770 行）**没有** `kpis` 键集锁、**没有**卡片数量锁 → 扩展不会撞既有断言。
- `main.py:275 _PAGE_ANCHORS` 是页面锚点注册表（`:317` 消费），新增面板需登记 `("cockpit-profit-panel", "单品利润排行")`。

### 1.6 入库接口零锁，可安全加列

`schemas.py:701 InboundReceiveLine` 加 `unit_cost: Decimal | None = None` 向后兼容；
`tests/` 内零处引用 `InboundReceive` / `unit_cost`（grep 已验）→ **加列不撞任何既有测试**。
`InboundLineResponse`（schemas.py:712）同步加可选 `unit_cost` 即可。

---

## 2. 时间对齐：设计（本切片的核心，请重点审）

### 2.1 三个时刻必须先分清

| 符号 | 含义 | 来源 |
|---|---|---|
| `T_cost` | 成本**生效**时刻 | 确认入库的 `confirmed_at`；手工录入时显式指定 |
| `T_sale` | 订单**销售**时刻 | `external_orders.external_created_at` |
| `T_anchor` | 订单行**锚定**时刻（写入系统时） | 系统当前时间，仅留痕 |

用户提的两个难题，本质是两个方向的时间错配：

- **先有销售，后有成本**（`T_cost > T_sale`）：锚定时找不到快照 → Unknown。
- **销售时有成本，后来又进货**（新的 `T_cost` 出现，但 `T_sale` 已过去）：**绝不能回改**。

### 2.2 解法：append-only 快照 + 写入时物化锚定（双保险）

**第一层：`cost_snapshots` 只 INSERT，永不 UPDATE / DELETE。**

成本变更 = 追加一条新快照（新的 `effective_time`），旧快照原样保留。
这样「历史成本事实」在存储层就不可篡改。修正录入错误也只能追加（留痕、可审计）。

**第二层：订单行写入时物化锚定（用户明确要求的做法）。**

`ingest_orders` 内，每写一条 `ExternalOrderLine` 前，执行一次 as-of 抓取：

```sql
SELECT id, unit_cost FROM cost_snapshots
WHERE workspace_id = :ws AND sku_id = :internal_sku_id
  AND effective_time <= :T_sale
ORDER BY effective_time DESC, id DESC
LIMIT 1
```

命中 → 把 `cost_snapshot_id / unit_cost / fee_rate / cost_status='resolved' / anchored_at`
**冗余写进订单行**。此后：
- 平台再进货（新快照 `T_cost > T_sale`）→ 不在 as-of 窗口内，**天然不影响**
- 快照被追加修正（app 层不允许改旧行）→ 已锚定的行**不受影响**

**为什么两层都要？**
只有 append-only 没有锚定 → 万一将来允许"回溯补录早于销售的快照"，历史会变；
只有锚定没有 append-only → 快照行被就地改写时，`cost_snapshot_id` 指向的成本与订单行冗余值不一致，
对账时无法解释差异。两层叠加才能满足「历史利润不因今天的进货而改变」这条红线。

**批量约束（防 N+1）**：`ingest_orders` 单批可能几百行。**禁止逐行发 SQL**。
要求：进入循环前，把本批全部 `(internal_sku_id, T_sale_earliest)` 收集成一次
`IN (...)` 查询（或按 sku 分组各取 max(T_sale) 后一条窗口函数查询），
内存里对每行做 as-of 二分。**本批新增查询条数硬上限 1 条**（守 §8 红线 R-2）。

### 2.3 「先销售后成本」怎么办：回填通道（显式、可选、留痕）

锚定时未命中 → `cost_status='missing'`，利润 Unknown。之后成本补录进来，
**默认不自动回改**（历史不变原则）。若业务需要补救，走显式端点：

```
POST /api/finance/cost-backfill   （权限：catalog.write）
body: { "mode": "next_after_sale", "sku_ids": [...], "dry_run": true|false }
```

- `mode=next_after_sale`（推荐默认值）：取 `T_sale` **之后**最早的一条快照，视为"当次销售的实际补货成本"
- `mode=nearest`：取 `T_sale` 前后时间差最小的一条（更激进，会引入未来成本）
- `dry_run=true` 只返回将被改写的行数与样例，不落库（先看再改）
- 只处理 `cost_status='missing'` 的行，**已 resolved 的行永不覆盖**
- 每次回填写结构化日志：`logger.info("利润成本回填 actor=%s mode=%s scanned=%d updated=%d dry_run=%s", ...)`
- 回填后 `cost_status='backfilled'`（**与 `resolved` 区分**，看板可说明"其中 N 笔为回填成本"）

这一步是本切片唯一允许改写历史成本的路径，且必须人工显式触发。

---

## 3. 数据模型（migration `0025_cost_snapshots`）

### 3.1 新表 `cost_snapshots`

```python
class CostSnapshot(Base):
    __tablename__ = "cost_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "sku_id", "effective_time", "source", "source_ref",
            name="uq_cost_snapshot_identity",
        ),
        Index("ix_cost_snapshot_lookup", "workspace_id", "sku_id", "effective_time"),
    )
    id: int
    workspace_id: int          # FK workspaces.id, not null, index
    sku_id: int                # FK product_skus.id, not null, index
    unit_cost: Decimal         # Numeric(12, 2), not null, >= 0（CHECK 由 app 层校验，SQLite 不加 CHECK）
    currency: str              # String(8), default "CNY"
    effective_time: datetime   # DateTime, not null —— 业务生效时刻（T_cost）
    source: str                # String(24) —— "inbound_confirm" | "manual"
    source_ref: str            # String(128) —— 入库单号 / 手工批次号，幂等键组成
    note: str | None           # Text
    created_at: datetime       # 写入时刻（与 effective_time 区分！）
    created_by: str | None     # String(128)
```

**唯一约束的四个分量缺一不可**：同一 SKU 同一时刻允许「入库」与「手工」两条来源并存
（手工修正值可以覆盖语义，但不能顶掉事实）；`source_ref` 让重复确认入库不产生第二行。

**幂等**：`source_ref` 形如 `inbound:{inbound_order_id}:line:{inbound_line_id}`，
与 `inventory_transactions.idempotency_key` 同一范式。

### 3.2 `inbound_lines` 加列

```python
unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
```
追加在末尾（与 migration 物理列顺序一致，沿用 P3-S1 的 `purchase_request_id` 做法）。

### 3.3 `external_order_lines` 加列（锚定五件套）

```python
cost_snapshot_id: Mapped[int | None]   # 软引用，不设 FK（快照可归档不阻塞订单）
unit_cost: Mapped[Decimal | None]      # Numeric(12, 2)，冗余固化值
fee_rate: Mapped[Decimal | None]       # Numeric(6, 4)，锚定时的平台费率
cost_status: Mapped[str] = "missing"   # "resolved" | "missing" | "backfilled" | "unmapped"
anchored_at: Mapped[datetime | None]
```

**为什么 `unit_cost` 与 `cost_snapshot_id` 双写**：快照是"事实引用"，冗余值是"当时的结论"。
对账时两者不一致 = 有人绕过了 append-only，能立刻被测试/巡检抓到。

**为什么费率也要锚定**：见 §4.3。费率若读当前配置，改一次 5%→8%，历史利润全变 ——
这跟「历史不因今天的变化而变」是同一条红线。

### 3.4 migration 要求

- 单笔 `0025`，upgrade 幂等（`inspect(bind).get_table_names()` 判表存在，沿用 0024 写法）
- downgrade 可回退（drop 表 + drop 三张表的追加列；SQLite 列回退用 `batch_alter_table`）
- `init_db` 走 Alembic 链路，**禁止手写 CREATE TABLE 绕过**
- 测试里 alembic head 断言从 `0024` 更新到 `0025`（这是已知连带点，登记到台账）

---

## 4. 成本入口与费率

### 4.1 入口一：实收入库报单价（主路径，Q1）

- `InboundReceiveLine` 加 `unit_cost: Decimal | None = Field(default=None, ge=0)`
- `repository.receive_inbound` 落 `InboundLine.unit_cost`
- `repository.confirm_inbound`（repository.py:724）在**同一事务内**、同一 `if transaction is None` 分支里
  写 `CostSnapshot`：`effective_time = order.confirmed_at`、`source="inbound_confirm"`、
  `source_ref = f"inbound:{order.id}:line:{line.id}"`
- **幂等**：`confirm_inbound` 首行 `if order.status == "confirmed": return` 已挡住重放；
  唯一约束作为第二道闸
- 存量入库单 `unit_cost` 为 NULL → 确认时不写快照，**不报错、不阻塞入库**（避免上线即阻断业务）

### 4.2 入口二：手工成本录入（存量兜底，Q1）

没有它，上线第一天 100% Unknown，黄条直接糊满屏。端点：

```
POST /api/finance/cost-snapshots      （catalog.write）单条录入
POST /api/finance/cost-snapshots/import  （catalog.write）CSV 批量（复用 P6 编码探测范式）
GET  /api/finance/cost-snapshots      （catalog.read）按 sku / 时间窗查询，分页
```

CSV 列：`sku_code, unit_cost, effective_time, currency, note`
- 复用 `app/imports.py` 的 `detect_encoding`（BOM→utf-8→gb18030）
- 行级错误不整体回滚，返回 `{ok, rejected:[{row_number, reason}]}`（沿用 P6-S3 容错范式）
- `source="manual"`、`source_ref = f"manual:{batch_id}:{row_number}"`，保证重传不膨胀

### 4.3 费率（Q3 / Q5）

`app/config.py` 的 `Settings` 新增：

```python
profit_platform_fee_rate: float = 0.05          # 全局默认 5%
profit_platform_fee_overrides: str = ""         # JSON: {"shopify": 0.06}，空=不用
```

- 计算时按订单 `platform` 取 override，无则用默认（读取集中在 `app/finance.py` 的 `resolve_fee_rate(platform)`）
- **锚定时把取到的费率写进 `external_order_lines.fee_rate`**，此后配置变更不影响已锚定行
- 费率区间校验 `0 <= rate <= 1`，非法配置在**启动期**抛错（fail-fast，不静默降级成 0）

---

## 5. 利润引擎（`app/finance.py` 新建）

### 5.1 口径（逐条锁死，避免后期扯皮）

只对这些行计算利润：

| 条件 | 取值 | 依据 |
|---|---|---|
| 订单状态 | `paid / fulfilled / completed` | 复用 `cockpit.SALE_ORDER_STATUSES`，**与 GMV 同谓词**（测试对拍 `external_orders._is_sale`） |
| 净收入 | `gross_amount − refund_amount` | 行级字段，不取订单级分摊 |
| 净数量 | `max(ordered_qty − cancelled_qty − refunded_qty, 0)` | 退款/取消不参与成本 |
| 成本 | `unit_cost × 净数量`（锚定值，非当前快照） | 红线 |
| 平台费 | `(gross_amount − refund_amount) × fee_rate`（锚定费率） | 红线 |
| 利润 | `净收入 − 成本 − 平台费` | Decimal，quantize 到 0.01 |

**金额运算一律整数分**（沿用 `cockpit._net_cents_expression` 范式），
禁止 float 累加；返回时 `/100` 转字符串两位小数。

### 5.2 三态归因（黄条与排行都要说清楚）

| `cost_status` | 含义 | 是否计入利润 | 是否计入黄条 X |
|---|---|---|---|
| `resolved` | 锚定时命中快照 | ✓ | ✗ |
| `backfilled` | 事后回填补齐 | ✓ | ✗（单独计数展示） |
| `missing` | 有内部 SKU 但无成本快照 | ✗ → Unknown | ✓ |
| `unmapped` | 订单行无 `internal_sku_id`（P2 映射缺失） | ✗ → Unknown | ✗（**单独计数**，属另一类问题） |

**黄条文案严格按用户原文**：`有 X 笔订单因缺失入库成本未能计算利润`，X = `cost_status='missing'` 的**去重订单数**。
`unmapped` 另给一行小字（否则用户会以为是成本问题，实际是商品映射没做）—— 这是 Q6 的裁定项。

### 5.3 锚定实现（`ingest_orders` 内，唯一改动点 + 唯一必踩坑）

**改动点 A：批量预取（进循环前）**

```python
# 1) 收集本批 (internal_sku_id, T_sale) —— 需要先算出 mapping，否则拿不到 internal_sku_id
# 2) 一次查询取回候选快照（按 sku_id IN (...) AND effective_time <= max(T_sale) 升序）
# 3) 内存按 sku 分组后二分/线性取「<= T_sale 的最近一条」
```
查询条数：**≤ 1**（不含既有 mapping 查询）。

**改动点 B（坑）：更新订单前的旧行继承**

```python
old_lines = db.scalars(select(ExternalOrderLine).where(...)).all()
inherit = {l.external_line_id: (l.cost_snapshot_id, l.unit_cost, l.fee_rate,
                               l.cost_status, l.anchored_at) for l in old_lines}
db.execute(delete(ExternalOrderLine)...)
...
# 重建时：
cfg = inherit.get(line.external_line_id)
if cfg and cfg[3] in ("resolved", "backfilled"):
    # 已锚定 → 原样继承，不重新抓取
else:
    hit = lookup(line.internal_sku_id, order.external_created_at)
```

**规则：已锚定（`resolved`/`backfilled`）的行，更新订单时优先继承旧值，而不是重新 as-of**。
否则「今天补的成本」会把一笔三天前更新过的订单的成本改掉 = 违反红线。
新行（平台新增的 line）才走 as-of；`unmapped → mapped` 状态变化时才允许从 `unmapped` 升级为 as-of 抓取。

### 5.4 查询接口（按需计算，不做异步任务）

```
GET /api/finance/profit?days=7&as_of=...        （analytics.read）
GET /api/finance/profit/ranking?days=7&limit=10 （analytics.read）
```

**为什么选按需查询而不是异步任务（Q2）**：
- 数据量级 = 窗口内订单行（千级），一条 `GROUP BY` 聚合即可，耗时毫秒级
- 项目既有 `background.py` 是 ThreadPoolExecutor + `CrawlJob` 表 + 心跳 + 重启恢复，引入成本远大于收益
- 用户红线「严禁在订单同步的实时路由做重计算」——按需查询天然不碰同步路径
- 代价：无预聚合缓存，超大数据量会慢。已写进 `LIMITATIONS` 作为已知限制

---

## 6. 看板扩展（`/cockpit`）

### 6.1 新增 KPI：`estimated_profit`

`cockpit.build_cockpit` 内新增**一条**聚合查询（按 `(internal_sku_id, currency)` 分组，
内存里同时算总额与排行，避免两条）：

```
period_profit   = Σ(净收入 − 成本 − 平台费)   # 仅 resolved/backfilled
missing_orders  = COUNT(DISTINCT order_id)   WHERE cost_status='missing'
unmapped_orders = COUNT(DISTINCT order_id)   WHERE cost_status='unmapped'
backfilled_rows = COUNT(*)                   WHERE cost_status='backfilled'
```

KPI 形态（**严格照 `gmv` 的范式，别自创**）：

```json
{
  "key": "estimated_profit", "label": "预估总利润",
  "value": "1234.56", "unit": "CNY",
  "change_ratio": 0.32, "change_basis": "上一等长窗口",
  "quality": "complete | partial | insufficient",
  "reason": "NO_COST_SNAPSHOTS | PARTIAL_COST_COVERAGE",
  "source": "external_order_lines × cost_snapshots（锚定值）",
  "note": "含平台费扣除（锚定费率）；多币种不折算",
  "multi_currency": false,
  "coverage": {"priced_orders": 12, "missing_orders": 3, "unmapped_orders": 1}
}
```

- `quality=partial`：`missing_orders > 0`（利润是"已能算的部分"，必须标注）
- `quality=insufficient`：窗口内无任何 `resolved/backfilled` 行
- 多币种：`value=null` + `unit=null` + `multi_currency=true`（与 GMV 一致，**绝不折算**）

### 6.2 新增黄条警示

`profit_warning`:

```json
{ "level": "warning",
  "missing_cost_order_count": 3,
  "unmapped_order_count": 1,
  "message": "有 3 笔订单因缺失入库成本未能计算利润" }
```

- `missing_cost_order_count == 0` → 前端**不渲染**黄条
- 文案严格用用户原文（金额/笔数由后端拼，前端只显示 `message`）
- 样式复用 `workspace-shell.css` 既有 warning token，**新增 class 必须走 tokens**（P5-S1 红线）

### 6.3 新增表格：单品利润排行

```json
"profit_ranking": [
  { "currency": "CNY",
    "winners": [ {"sku_code":"A-001","title":"…","net_qty":12,"net_revenue":"600.00",
                  "cost":"300.00","platform_fee":"30.00","profit":"270.00",
                  "margin_ratio":0.45}, … ≤10 ],
    "losers":  [ … ≤10 ] }
]
```

- 按 SKU 聚合窗口内利润，`winners` 降序 Top10、`losers` 升序 Top10
- `losers` 只取 `profit < 0` 的（亏钱榜里混进正利润没意义）；不足 10 条就少给
- 空态：`winners=[]` → 前端显示「窗口内暂无可计算利润的订单」
- 多币种 → 数组按币种分组（每组各 10 条），**不做跨币种合并排行**

### 6.4 前端改动（`cockpit.html`）

1. `renderKpis()` 硬编码数组追加 `'estimated_profit'`（**位置：`gmv` 之后**，视觉上紧跟 GMV）
2. 新面板 `cockpit-profit-panel`（放在「销售趋势」之后、「库存分布」之前）：
   - 上半部：黄条（`profit_warning`，有内容才渲染）
   - 下半部：双列排行表（赚钱 / 亏钱），复用 P5-S2 的表格数字样式
3. `main.py:275 _PAGE_ANCHORS["/cockpit"]` 追加 `("cockpit-profit-panel", "单品利润排行")`
4. 空态必须有文案与图标（P5-S7 范式），禁止空白 div

---

## 7. 切片拆分（建议三刀，每刀可独立验收）

| 切片 | 内容 | 预估改动 | 验收 |
|---|---|---|---|
| **P7-S1** | `0025` 全部结构 + `CostSnapshot` 模型 + `app/costs.py` + `inbound_lines.unit_cost` + `InboundReceiveLine` + `confirm_inbound` 落快照 + 手工录入/CSV 导入/查询三端点 + `warehouse.html` 单价与预估总成本 + `sources.html` 成本导入入口 | 10 文件 | 见 §13.6 |
| **P7-S2** | `ingest_orders` 锚定 + 旧行继承 + 回填端点 | 3 文件 | **锚定固化测试**（F-11/F-12 是验收闸） |
| **P7-S3** | `app/finance.py` 利润引擎 + cockpit KPI/黄条/排行 + 前端面板 + 锚点注册 | 6 文件 | 口径对拍 + 前端渲染 + 全量绿 |

每刀 < 1.5h。若 S1 与 S2 合并超时，按 `p7s1a` / `p7s1b` 再拆（沿用 P6-S1 做法）。

---

## 8. 红线（守卫）

- **R-1 快照 append-only**：`cost_snapshots` 只有 INSERT；全仓禁止 `update(CostSnapshot)` / `delete(CostSnapshot)`。
  测试用 `grep` 守死（照 E-11 禁 pandas 的写法）。
- **R-2 同步路径零重计算**：`ingest_orders` 内新增查询 **≤ 1 条**，禁止循环内发 SQL；
  禁止在 `ingest_orders` / `platform_sync` / CSV 导入里 import `app.finance` 的聚合函数。
- **R-3 已锚定不重抓**：`resolved` / `backfilled` 行在订单更新时继承旧值，**永不重新 as-of**。
  （违者历史利润可变 = 本切片唯一不可救的架构级错误）
- **R-4 cockpit 只读**：`cockpit.py` 内禁止 `db.add` / `db.commit` / `db.delete` / `db.flush`。
- **R-5 多币种不折算**：禁止出现跨币种求和；KPI 与排行都按币种分组。
- **R-6 金额整数分**：利润链路禁止 float 中间值；Decimal 仅用于最终展示。
- **R-7 不加第三方依赖**：不引入 pandas / babel / money 类库。
- **R-8 不改 `connectors.py`**：成本与订单结构解耦，`ExternalOrderRecord` 不加成本字段
  （成本是内部事实，不该由平台 payload 携带）。
- **R-9 幂等**：重复确认入库、重传手工 CSV、重复回填，均不得产生重复快照/重复改写。
- **R-10 live 红线不动**：`SHOPIFY_LIVE_ENABLED` 基线 false，本切片不触碰。

---

## 9. 测试计划（F-1..F-24，按切片归属；基线 574 → 预期 ~595）

### S1 成本入口（F-1..F-10，预期 +10 → ~584）

| 编号 | 断言 | 说明 |
|---|---|---|
| F-1 | 确认入库（带单价）→ `cost_snapshots` 落 1 行，`effective_time == confirmed_at`，`source='inbound_confirm'`，`source_ref` 格式正确 | 主入口 |
| F-2 | 重复确认入库（幂等重放）→ 快照仍 1 行；并发重复确认靠唯一约束兜住 | 幂等 |
| F-3 | 存量入库单无 `unit_cost` → 确认入库**不报错、不写快照**，`received/confirmed` 流转照旧 | 上线安全 |
| F-4 | 手工单条录入：负数/非法 SKU/超长备注被拒（422），合法落库 `source='manual'` | 校验 |
| F-5 | 成本 CSV 正常路径：中文列头（商品编码/成本单价/生效时间）自动映射，3 行全进，`encoding` 正确 | 复用 P6-S2 |
| F-6 | **静默丢行护栏**：`unit_cost=abc` 行进 `rejected` 且 `reason_code` 有值；断言 `imported + rejected == rows_total`（计数不变量） | ★用户硬要求 |
| F-7 | 成本 CSV 重传幂等：同文件二次导入不产生第二组快照（`source_ref` 唯一） | 幂等 |
| F-8 | 查询分页 + 权限：`catalog.read` 可读、无权限 403、`catalog.write` 才能写入 | 权限 |
| F-9 | 页面契约：`warehouse.html` 含 `data-unit-cost` 输入与预估总成本节点；`sources.html` 含成本导入入口 | 前端 |
| F-10 | 全量回归 574 → ~584，0 failed | 收口 |

### S2 锚定与回填（F-11..F-16 → ~590）

| 编号 | 断言 | 说明 |
|---|---|---|
| F-11 | as-of 抓取：有 T-10天/T-2天/未来 三条快照，T_sale=T-5天 → 命中 T-10天那条 | 时间对齐 |
| F-12 | **锚定固化**：锚定后再插新快照 → 订单行 `unit_cost` 与利润**完全不变** | ★核心红线 |
| F-13 | **重建继承**：同一订单二次 ingest（改价）→ 锚定五件套原样保留，`cost_status` 不退回 missing | ★必踩坑 |
| F-14 | 回填：`dry_run=true` 零写库；`next_after_sale` 只补 missing、不覆盖 resolved；重复回填幂等；结构化日志可捕获 | 回填 |
| F-15 | `unmapped` 与 `missing` 归因分离：前者不计入黄条 X | Q6 |
| F-16 | 同步路径查询预算：`ingest_orders` 新增查询 ≤1（SQL 计数断言） | 红线 R-2 |

### S3 引擎与看板（F-17..F-24 → ~595）

| 编号 | 断言 | 说明 |
|---|---|---|
| F-17 | 利润公式对拍：手算 = 接口值（净数量、退款、平台费扣除） | 口径 |
| F-18 | 费率锚定：改 `profit_platform_fee_rate` 后已锚定行利润不变，新订单用新费率 | 财务红线 |
| F-19 | 多币种：KPI `value=null`、`multi_currency=true`、排行按币种分组 | 不折算 |
| F-20 | 排行：`winners` 降序 Top10、`losers` 仅负利润升序 Top10、空态返回空数组不报错 | 排行 |
| F-21 | 黄条：`missing>0` 才出现，文案严格「有 X 笔订单因缺失入库成本未能计算利润」，X 按订单去重 | 文案 |
| F-22 | 只读纪律：`grep` 守 `cockpit.py` 无 `db.add/commit/delete/flush` | 红线 R-4 |
| F-23 | 存量兼容：零快照时 `/api/dashboard/cockpit` 不 500，KPI `quality=insufficient` | 上线安全 |
| F-24 | 前端渲染：KPI 卡片插在 GMV 之后、排行双列渲染、空态有文案 | 前端 |

**连带修改（已知，实施时一并登记台账）**：
1. alembic head 断言 `0024` → `0025`
2. `cockpit.html` `renderKpis` 数组追加 `'estimated_profit'`（S3）
3. `_PAGE_ANCHORS` 新增锚点（S3）
4. cockpit 查询条数上限 6→7（S3，`LIMITATIONS` 文案 + 模块 docstring 同步）

---

## 10. 交接五要素（实施者必读）

1. **目标文件与精确位置**
   - `app/db.py`：`InboundLine`(351)、`ExternalOrderLine`(788)、新 `CostSnapshot`（建议置于 `ExternalImportRejection` 之后）
   - `app/repository.py`：`receive_inbound`(657)、`confirm_inbound`(724，循环体 745-760)
   - `app/external_orders.py`：`ingest_orders`(212)，删除点 247、重建点 288
   - `app/cockpit.py`：`build_cockpit`(114)、`kpis`(656)、`return`(767)
   - `app/templates/cockpit.html`：`renderKpis()` 硬编码数组
   - `app/main.py`：`_PAGE_ANCHORS`(275)、cockpit 路由(1963/1969)
   - `app/schemas.py`：`InboundReceiveLine`(701)、`InboundLineResponse`(712)、`CockpitResponse`(137)
   - `app/config.py`：`Settings`

2. **现状代码片段**：见 §1.3（ingest_orders 五处入口）、§1.4（DELETE 重建两行）、§1.6（入库模型）

3. **改什么 / 不改什么**
   - 改：上述文件
   - **不改**：`connectors.py`、`imports.py`、`external_sync.py`、Shopify/淘宝适配器、`workspace-shell.css` 既有 class 语义

4. **验收标准**：§9 全绿 + 全量 `574 → ~590` 且 0 failed + `xlsx` 导出等 P6 功能零回归

5. **已排除的选项**
   - ✗ 从 `InventoryTransaction` 推导成本（它只有 `quantity_delta`，无金额）
   - ✗ 用 `Product.current_price` 当成本（那是爬取的售价）
   - ✗ 引入 `background.py` 异步任务做利润预聚合（收益不抵复杂度，Q2）
   - ✗ 在 `ExternalOrderRecord` / CSV 模板里加成本列（成本是内部事实，Q1）
   - ✗ 计算时实时 as-of 而不锚定（用户明确要求锚定；且无法解释"当时用的是哪条成本"，Q2）
   - ✗ 多币种折算成单币种（P4 已有红线 `fx_todo`）

---

## 11. 待裁定 Q 项（默认推荐值即 v1.1 落地值）

| # | 问题 | 推荐 | 备选 |
|---|---|---|---|
| **Q1** | 成本从哪进来？ | **入库报单价（主）+ 手工 CSV/表单（兜底）双入口** | 仅入库报单价（存量全 Unknown）／仅手工维护（脱离实收事实） |
| **Q2** | 利润在哪算？ | **按需查询**（cockpit 聚合 + `/api/finance/profit`），同步路径只锚定不算 | 异步任务预聚合（需引 job 表 + worker） |
| **Q3** | 费率是否锚定？ | **锚定**（写 `external_order_lines.fee_rate`），改配置不动历史 | 计算时读当前配置（历史利润随配置漂移，违反红线） |
| **Q4** | 回填是否允许？ | **允许，但必须显式触发 + dry_run 预演 + 只补 missing** | 完全不回填（Unknown 永久）／自动回填（历史可变） |
| **Q5** | 费率配置位置？ | **`Settings` 全局默认 + `platform` override JSON** | 新建费率表（更灵活但多一张表） |
| **Q6** | `unmapped` 是否计入黄条 X？ | **不计入**，黄条严格按用户原文只报"缺失成本"，unmapped 用小字单列 | 合并计数（用户会误判成成本问题） |
| **Q7** | 切片节奏？ | **S1 成本入口 / S2 锚定+回填 / S3 引擎+看板** | 一刀做完（>1.5h，CLI 易超时） |

---

## 12. 风险登记（预期新增 R 编号）

| 编号 | 风险 | 触发条件 | 处置 |
|---|---|---|---|
| R-65 | 订单更新重建丢失成本锚定 | 未按 §5.3 做旧行继承 | F-5 守死；上线后可用"利润突然 Unknown"巡检 |
| R-66 | 快照就地改写导致对账不可解释 | 有人绕过 append-only | R-1 grep 守卫 + 双写不一致巡检 |
| R-67 | cockpit 查询条数超预算 | 利润切两条查询 | 合并为一条 `GROUP BY (sku, currency)` |
| R-68 | 手工 CSV 成本时间早于历史订单 → 利润大面积变动 | 用户导入回溯数据 | `dry_run` 先看影响面；导入返回"将影响 N 笔历史订单" |

---

---

## 13. P7-S1 施工规格（成本入口）——定稿，可直接实施

### 13.1 改动清单（10 文件）

| # | 文件 | 位置 | 动作 |
|---|---|---|---|
| 1 | `app/db.py` | `InboundLine`(351) 尾 | 加 `unit_cost: Mapped[Decimal \| None] = mapped_column(Numeric(12, 2), nullable=True)` |
| 1 | `app/db.py` | 新类 | 加 `CostSnapshot`（§3.1 全字段） |
| 1 | `app/db.py` | `ExternalOrderLine`(788) 尾 | 加锚定五列（S1 只建列，不写逻辑；S2 启用） |
| 2 | `migrations/versions/0025_cost_snapshots.py` | 新建 | 建表 + 两处 `add_column`（幂等，照 0024 写法） |
| 3 | `app/costs.py` | 新建 | `CostRecord` / `load_cost_rows` / `create_cost_snapshot` / `ingest_cost_records` / `list_cost_snapshots` |
| 4 | `app/column_mappings.py` | `FIELD_ALIASES`(20) 尾、`REQUIRED_FIELDS`(47) | 加 `costs` kind |
| 5 | `app/imports.py` | `IMPORT_KINDS`(30)、`_load`(108)、`parse_upload`(171) | 加 `COST_KIND` 与分派；平台校验放宽 |
| 6 | `app/schemas.py` | `InboundReceiveLine`(701)、`InboundLineResponse`(712) | 加 `unit_cost`；新增成本快照四个模型 |
| 7 | `app/repository.py` | `receive_inbound`(657)、`confirm_inbound`(724)、`_inbound_response_data`(525) | 落单价 / 落快照 / 回显单价 |
| 8 | `app/main.py` | `receive_inbound` 路由(1485) | `model_dump()` 已自动带 `unit_cost`，仅确认透传 |
| 8 | `app/main.py` | 新端点区（建议紧邻入库路由） | 三个成本端点（§13.4） |
| 9 | `app/templates/warehouse.html` | 渲染行（POS 5773）、提交（POS 6673） | 单价输入 + 预估总成本（§13.2） |
| 10 | `app/templates/sources.html` | 导入区 kind 下拉与提交分派 | 「成本快照」导入项（§13.3） |

### 13.2 前端「预估总成本」实时展示（用户硬要求 1）

**渲染行改造**（当前模板为单物理行，改后仍需保持行内联写法）：

```js
`<div class="row" data-receive-line="${line.sku_id}">
   <strong>SKU #${escapeHtml(line.sku_id)}</strong>
   <label>实收<input data-received type="number" min="0" value="..."></label>
   <label>破损<input data-damaged type="number" min="0" value="..."></label>
   <label>单价<input data-unit-cost type="number" min="0" step="0.01" placeholder="选填" value="${line.unit_cost ?? ''}"></label>
   <span class="num" data-line-cost>—</span>
 </div>`
```

**汇总节点**：`#receive-lines` 之后加

```html
<p id="receive-total-cost" class="status-line">预估总成本：—</p>
```

**计算规则（口径必须写死，别自创）**：

- 行小计 = `(实收 − 破损) × 单价`，保留 2 位，无单价或单价非法显示 `—`
- 汇总 = Σ行小计；全部为空 → `预估总成本：—`
- **口径：按合格数量（实收 − 破损）**，与 `accepted_qty` / 库存台账同源；不得用 `实收` 直接乘
  （破损不进库存，若按实收算会高估库存资产）
- 标签加 `title="按实收减破损计算；破损商品不进库存"`，避免财务口径争议
- 事件：`#receive-lines` 上做 `input` 委托（模板现有代码是内联赋值风格，不允许引框架）
- **提交易错点**：`unit_cost` 为空必须提交 `null`，不能提交 `0`
  （`0` 是"免费成本"，会污染成本库并让利润虚高）

**提交语句改造**（当前 POS 6673）：

```js
.map(row=>({sku_id:Number(...), received_qty:Number(...), damaged_qty:Number(...),
            unit_cost: row.querySelector('[data-unit-cost]').value === ''
                       ? null : Number(row.querySelector('[data-unit-cost]').value)}))
```

### 13.3 成本 CSV：完全复用 P6「静默丢行」护栏（用户硬要求 2）

**做法：不新写解析器，把 `costs` 接成 import 流水线的第三种 kind。**
这样 P6 刚修好的护栏是**结构性继承**，不是"照着抄一遍"。

| 复用点 | 来源 | 代价 |
|---|---|---|
| 编码探测 BOM→utf-8→gb18030 | `imports.decode_upload`(41) | 0 |
| 中文列头自动映射 / 全角空格归一 | `column_mappings.suggest_mapping`(76) | 0 |
| 必填列缺失检测 | `missing_required`(93) | 0 |
| **计数判定 + 二分隔离** | `imports._isolate`(123) | 0 |
| **`记录数 + 异常数 == 数据行数` 收尾不变量** | `parse_upload` 尾部 | 0 |
| 脏行 100% 收集（不截断） | `parse_upload` `bad` 全量 | 0 |

**改法（三处，极小）**：

1. `column_mappings.py`

```python
FIELD_ALIASES["costs"] = {
    "sku_code":       ("商品编码","sku","商家编码","规格代码","货号","商品编号","商品代码","内部编码"),
    "unit_cost":      ("成本单价","成本价","进货单价","采购单价","单位成本","入库成本","单价","unit_cost","cost"),
    "effective_time": ("生效时间","成本时间","入库时间","生效日期","统计时间","effective_time"),
    "currency":       ("币种","货币","currency"),
    "note":           ("备注","说明","note"),
}
REQUIRED_FIELDS["costs"] = ("sku_code", "unit_cost")   # effective_time 可空 → 导入时刻
```
> 加载期别名冲突自检会自动覆盖新 kind（`_build_reverse_index`），无需额外代码。

2. `imports.py`

```python
COST_KIND = "costs"          # 刻意不加入 IMPORT_KINDS → 外部导入端点继续拒绝 costs
# _load():
if kind == COST_KIND:
    return load_cost_rows(text)
# parse_upload 首行校验改为：
if kind != COST_KIND and platform not in PLATFORMS:
```
> **为什么不动 `IMPORT_KINDS`**：`/api/external/imports` 用它做白名单，
> 加进去会让成本 CSV 从外部导入端点溜进来（走错 ingest、写错表）。保持原值 = 零回归。

3. `app/costs.py::load_cost_rows(text) -> list[CostRecord]`

**契约（护栏成立的前提，必须遵守）**：
> **一条数据行 → 必须产出一条 `CostRecord`，或抛 `ConnectorError`。绝不允许 `continue` 静默跳过。**

违反契约不会造成数据污染（`_isolate` 的计数判定会把它抓出来进 `rejected`），
但会导致合法行被误判，故测试 F-6 必须实证。

字段校验：

| 字段 | 规则 | 失败 |
|---|---|---|
| `sku_code` | 非空；先匹配 `ProductSku.sku_code`；未命中回落 `ExternalProductMapping.external_sku`（**一次批量查询，禁 N+1**） | `ConnectorError("SKU_NOT_FOUND: {code}")` |
| `unit_cost` | 可转 `Decimal`；`0 <= v <= 1e9`；最多 2 位小数（多余位四舍五入到 2 位，不报错） | `ConnectorError("成本单价非法: {raw}")` |
| `effective_time` | 空 → `None`（由 ingest 补 `datetime.utcnow()`）；非空支持 `YYYY-MM-DD` / `YYYY-MM-DD HH:MM:SS` / ISO8601 | `ConnectorError("生效时间非法: {raw}")` |
| `currency` | 空 → `CNY`；长度 ≤8 | `ConnectorError("币种非法: {raw}")` |

### 13.4 端点（三个）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| `POST` | `/api/finance/cost-snapshots` | `catalog.write` | 单条录入。`source="manual"`，`source_ref=f"manual:{uuid}"`。返回 201 + 快照对象 |
| `POST` | `/api/finance/cost-snapshots/import` | `catalog.write` | multipart CSV。`parse_upload(kind="costs")` → `ingest_cost_records`。返回 `{imported, rejected_total, rows_total, encoding, mapping, rejected:[…≤500]}` |
| `GET` | `/api/finance/cost-snapshots` | `catalog.read` | 按 `sku_code`/`sku_id`/`from`/`to` 过滤，`limit`(≤200)+`offset`，返回 `{items, total}` |

**导入幂等**：`source_ref = f"manual:{sha256(raw_bytes)[:16]}:{row_number}"`，
同文件重传命中唯一约束 → 跳过（`imported=0, skipped=n`），不报错、不膨胀。

**错误码**：
- `SKU_NOT_FOUND` → 行级（进 rejected，不整体失败）
- 文件级：非 CSV / 空文件 / 缺必填列 → 422 `COST_IMPORT_INVALID`（带 `missing` 列表）
- 全部行被拒 → 仍 200 + `imported=0`（与 P6 容错语义一致，不整单 422）

### 13.5 migration `0025`

```
upgrade():
  - create_table cost_snapshots（含 UniqueConstraint uq_cost_snapshot_identity + Index ix_cost_snapshot_lookup）
  - inbound_lines: batch_alter_table add_column unit_cost NUMERIC(12,2) NULL
  - external_order_lines: add cost_snapshot_id INT NULL / unit_cost NUMERIC(12,2) NULL
                          / fee_rate NUMERIC(6,4) NULL / cost_status VARCHAR(16) DEFAULT 'missing'
                          / anchored_at DATETIME NULL
downgrade():
  - 逆序 drop
```
- 全量幂等：`inspect(bind).get_table_names()` / `get_columns()` 判存在后再建（照 0024）
- **禁止**手写 `CREATE TABLE` 绕过 Alembic
- 测试连带：alembic head 断言 `0024` → `0025`（唯一已知的既有测试改动，登记台账）

### 13.6 S1 验收标准

1. §9 的 **F-1..F-10 全绿**，全量 `574 → ~584` 且 0 failed（有 flaky 按「重跑一次全绿 + 登记」判）
2. `confirm_inbound` 带单价 → 快照落库；不带单价 → 静默跳过，入库流程零影响
3. 成本 CSV 里 `unit_cost=abc` 的行 100% 进 `rejected` 且 `imported + rejected == rows_total`
4. `warehouse.html` 实收页输入单价后，「预估总成本」实时变化（F-9 页面契约断言）
5. `connectors.py` / `imports.py` 既有 inventory/orders 分支**逐字节不变**
6. `/api/external/imports` 传 `kind=costs` 仍被拒（证明白名单没被污染）

### 13.7 S1 明确不做（防止范围蔓延）

- ✗ 不写 `ingest_orders` 的任何锚定逻辑（S2）
- ✗ 不写利润计算、不碰 `cockpit.py` / `cockpit.html`（S3）
- ✗ 不改 `connectors.py`，不改 inventory/orders 的别名与必填（只**新增** costs kind）
- ✗ 不引入新第三方依赖（openpyxl 已有，本片也用不到）
- ✗ 不做成本快照的删除/修改端点（append-only，R-1）

---

**Q1–Q7 已裁定，v1.1 定稿。首刀 P7-S1 规格见 §13，可按其直接实施。**

---

## 14. P7-S2 施工规格（锚定 + 继承 + 回填）——已实施，留档

### 14.1 改动清单（6 文件，零新迁移）

| 文件 | 改动 |
|---|---|
| `app/config.py` | 新增 `profit_platform_fee_rate` / `profit_platform_fee_overrides`；`model_validator` 启动期校验（0~1 与 JSON 合法性，非法直接炸进程）；`resolve_fee_rate(platform, settings=None) -> Decimal` |
| `app/external_orders.py` | `_anchor_pending_lines()` 批量 as-of（**1 条查询**）；`ingest_orders` 增待锚定收集 + 旧行继承 |
| `app/costs.py` | `backfill_missing_costs()`（只补 missing、dry_run 零写库、置 `backfilled`） |
| `app/schemas.py` | `CostBackfillRequest` / `CostBackfillSample` / `CostBackfillResponse` |
| `app/main.py` | `POST /api/finance/cost-backfill`（`catalog.write`，结构化日志） |
| `tests/test_p7s2_cost_anchoring.py` | F-11..F-16 + **F-12b**（missing 也继承）+ **F-14b/c**（权限、无候选）+ 费率漂移附带 |

**无需 0026**：`external_order_lines` 的锚定五列在 `0025` 内已建好（S1 一并建列不落数据）。

### 14.2 锚定的执行顺序（顺序本身就是正确性）

```
循环内：每行只做「判定」——unmapped / 继承 / 加入 pending
循环后：_anchor_pending_lines() 一次 SELECT 取全部候选快照 → 内存二分 → 回填
        db.flush()
```

**为什么不在循环内锚定**：循环内逐行查成本表 = N+1，直接违反红线 R-2（新增查询 ≤1）。
把「判定」与「抓取」拆开，是同时满足「1 条查询」与「每行独立 as-of」的唯一办法。

### 14.3 继承判定的三个分支

| 情形 | 行为 | 理由 |
|---|---|---|
| `internal_sku_id is None` | `cost_status='unmapped'`，只写 `fee_rate` + `anchored_at` | 没有内部 SKU 谈不上「缺成本」（Q6 归因分离） |
| 已锚定（`resolved`/`backfilled`）且 `external_sku` 未变 | **原样继承五件套** | 红线 R-3，绝不重抓 |
| 已锚定但 `external_sku` 变了 | 重新 as-of | 旧锚定锚的是**别的 SKU** 的成本，沿用等于用错成本 |
| 其余（新行 / `missing` / `unmapped→mapped`） | 进 pending 走 as-of | — |

**注意 `missing` 也在「继承」范围内**：首次没成本，事后补了成本再遇平台更新，
仍然保持 `missing`。自动变 `resolved` 等于静默改写历史利润——要补只能走回填（F-12b 锁死）。

### 14.4 费率与成本分开锚定

即使成本未命中（`missing`），`fee_rate` 与 `anchored_at` **照样写入**。理由：
费率是配置事实，当时就能确定；先落住，回填时就不必再读「当时的配置」——那时配置早变了。
若费率也留空，回填那天读到的 8% 会污染一笔 5% 时代的历史订单。

### 14.5 回填的安全边界

- `dry_run` 默认 **True**。财务动作先预演；调用方必须显式 `dry_run=false`。
- `mode=next_after_sale`（默认）：取销售**之后**最早一条；`mode=nearest`：绝对时间差最小，
  并列取更早一条（不引入未来成本）。
- 只扫 `cost_status='missing' AND internal_sku_id IS NOT NULL`；`resolved`/`backfilled` 永不覆盖。
- 回填后置 `backfilled`（与 `resolved` 区分），看板可说明「其中 N 笔为回填成本」。
- 幂等：补过的行不再是 `missing`，第二次 `scanned=0`。
- 日志：`利润成本回填 actor=%s mode=%s scanned=%d updated=%d skipped=%d dry_run=%s`，
  dry_run 也记（要能审计「谁预演过什么」）。

### 14.6 验收标准（实测全部满足）

- F-11 锚定后插入 99 元新快照 → 订单行 `unit_cost` / `cost_snapshot_id` / `fee_rate` /
  `anchored_at` 四项零变化。
- F-12 二次 ingest（改价 + `event_version=2`）→ 行数仍为 1、五件套与首次完全一致、
  `cost_status` 不退回 `missing`。
- F-16 一批 3 行（跨 2 个 SKU）只发 **1 条** `cost_snapshots` 查询；
  已锚定行重建时该查询数为 **0**。

---

## 15. P7-S3 施工规格（利润引擎 + 看板）——已实施，留档

### 15.1 改动清单（9 文件，零新迁移）

| 文件 | 改动 |
|---|---|
| `app/finance.py`（新建） | 口径常量 `FORMULA`、`net_qty`、`line_amounts`、`summarize_profit`（2 条查询）、`list_profit_lines`（2 条查询） |
| `app/cockpit.py` | import `SALE_ORDER_STATUSES`/`summarize_profit`；新增 `estimated_profit` KPI + 顶层 `profit` 段；查询预算 6→8 |
| `app/schemas.py` | `CockpitResponse.profit: dict`（宽松结构，避免每加一个财务字段就动响应模型） |
| `app/main.py` | `GET /api/finance/profit/lines`（`analytics.read` + admin/operations）；`/api/orders` 增 `sku_code`；`_PAGE_ANCHORS` 增 `cockpit-profit-panel` |
| `app/order_workbench.py` | `_filters` / `list_orders` 增 `sku_code`（EXISTS 子查询，避免 JOIN 导致订单重复） |
| `app/external_orders.py` | R-65：`BatchDuplicateOrderError` + `find_duplicate_order_numbers`，`ingest_orders` 入口判重 |
| `app/templates/cockpit.html` | 利润卡片可点击下钻、`title` 口径说明、利润排行面板、黄色提示条、明细表 |
| `app/templates/orders.html` | `SKU` 过滤框 + `?sku=` 预填（排行跳转落点） |
| `tests/test_p7s3_profit_engine.py`（新建） | F-17..F-25 + R-65，18 用例 |

**无需 0026**：利润按需计算、不落库；`external_order_lines` 的锚定五列已在 `0025` 建好。

### 15.2 口径（本片最核心的一节，改这里等于改历史利润）

```
净数量   = max(0, ordered_qty − cancelled_qty − refunded_qty)
实付净额 = max(0, gross_amount − refund_amount)
成本     = 锚定 unit_cost × 净数量
平台费   = 实付净额 × 锚定 fee_rate        ← 按实付扣，退款部分不抽佣
利润     = 实付净额 − 成本 − 平台费
```

三条必须理解的取舍：

- **平台费按实付而非原价**：平台按结算金额抽佣，退款部分不抽。若按原价扣，退款订单会被
  双重惩罚（收入冲减了、佣金还在），利润被系统性低估。前端卡片 `title` 原文：
  「利润 = 实付净额 − 成本 − 平台费；平台费按实付扣除，退款部分不抽佣」。
- **成本按净数量**：退款/取消件视为退货回仓，成本同步冲减。局限已写进
  `PROFIT_LIMITATIONS`：若实际未回仓，真实成本会高于本口径。
- **不可计算 ≠ 0 利润**：`cost_status ∈ {missing, unmapped}` 或单价/费率缺失时，
  该行**不进任何金额汇总**，只计数。把 unknown 当 0 是利润类报表最典型的失真来源。

### 15.3 返回结构（`/api/dashboard/cockpit` 的 `profit` 段）

```
profit.by_currency[]   currency / revenue / cost / platform_fee / profit / profit_margin
                       / net_qty / computed_lines / missing_lines / unmapped_lines / sku_count
profit.total_profit    单币种且有可算行时给值，否则 null（多币种或零可算行）
profit.multi_currency  bool
profit.quality         complete / partial（有 missing/unmapped）/ insufficient（无可算行）
profit.reason          NO_ORDERS / NO_COST_SNAPSHOTS / MISSING_COST_SNAPSHOTS
profit.warning         {show, level:"warning", message:"有 X 笔订单因缺失入库成本未能计算利润",
                        missing_cost_order_count}
profit.ranking         {winners[], losers[], currencies[], limit:10}
profit.formula         口径原文（前端 tooltip 与审计共用一份文案）
```

`by_currency` 的 `revenue/cost/platform_fee/profit` 只统计**可计算行**——这是
「收入也可能未知」的直接后果，必须在字段语义上明确，否则读者会以为漏算。

### 15.4 查询预算

| 位置 | 条数 | 说明 |
|---|---|---|
| `summarize_profit` | 2 | 1 条 `GROUP BY (currency, sku, sku_code, cost_status)` + 1 条 missing 订单去重计数 |
| `list_profit_lines` | 2 | 明细 1 条 + 总数 1 条（独立请求，不计入驾驶舱预算） |
| 驾驶舱合计 | 6 → 8 | 加 `resolve_sales_as_of` 的 0~2 条 as_of，上限 8 → 10 |

缺成本订单数**必须单独一条查询**：分组聚合里做不了跨组去重，用 SUM 会把一笔订单
按 SKU 数重复计数（「3 个 SKU 都缺成本」会被显示成 3 笔）。

### 15.5 测试（18 用例，实测全绿）

| 编号 | 断言 |
|---|---|
| F-17 | 两笔订单（含取消 1 / 退款 1）逐项对拍到分：100−20−5=75；60−10−3=47；合计 122.00 |
| F-17b | 退款额 > 商品额 → 实付净额夹到 0，利润 −10.00，不产生负收入 |
| F-18 | 费率 5%→8%：历史订单利润仍 75.00；新订单按 8% 扣；合计 147.00 |
| F-19 | missing 按订单去重（B、C 两 SKU 同单 → 1 笔）；unmapped 不计入 X；黄条文案逐字一致 |
| F-19b | 无缺成本时 `warning.show=false`、quality=complete |
| F-20 | winners 仅正利润、losers 仅负利润、利润 0 两边都不进 |
| F-20b | 无可排行时返回空数组而非报错 |
| F-21 | 多币种 KPI `value=null`、`multi_currency=true`、按币种分组各算各的 |
| F-22 | admin/operations 200；readonly 403（沿用驾驶舱角色闸，不另开口子） |
| F-23 | 零成本快照时 cockpit 200、KPI `insufficient`/`NO_COST_SNAPSHOTS`、value=null |
| F-23b | 空工作区 `NO_ORDERS`，`by_currency=[]` |
| F-24 | `finance.py` 零写操作（grep 守 `db.add/commit/delete/flush/merge/bulk_`） |
| F-24b | `summarize_profit` 实测恰 2 条 SELECT（事件监听计数） |
| F-25 | 下钻 status/currency 过滤、逐行之和 == KPI 总额 |
| F-25b | 非法 status / limit 越界 → 422 |
| F-25c | `/api/orders?sku_code=` 内部/外部编码都能命中（排行跳转的接口支撑） |
| R-65a | 批内重复 → `BatchDuplicateOrderError`，且**一行都不落库** |
| R-65b | HTTP 层 422 + `code=DUPLICATE_ORDER_NO_IN_BATCH` + 可操作文案 |

### 15.6 明确不做（防范围蔓延）

- 不做利润落库/物化表：落库就要面对「哪天回填了成本、哪天改了费率」的重算问题，
  而这已由 P7-S2 的锚定解决，重算只会引入新的不一致。
- 不做跨币种折算（`fx_todo` 仍归财务模块）。
- 不做环比（上一窗口利润）：需要另一组权重与可比性定义，留 P8。
- 不在同步路径算利润（Q2 裁定：同步只锚定）。
- R-66 自动预聚合不在本片实施（见 §0.3）。
