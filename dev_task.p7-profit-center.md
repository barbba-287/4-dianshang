# dev_task.p7-profit-center — P7 利润核算与对账中心（Profit Center）

- 版本：v1.0（草案，2026-11-04）
- 基线：`feat/warehouse-mvp@91ee44c`，全量 **574 passed / 0 failed**
- alembic head：`0024_external_import_rejections`（本切片顺延 `0025`）
- 状态：**Q1–Q7 待裁定**，裁定后升 v1.1 定稿开工

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
| **P7-S1** | `0025` migration + `CostSnapshot` 模型 + `inbound_lines.unit_cost` + `InboundReceiveLine` + `confirm_inbound` 落快照 + 手工录入/导入/查询三端点 | 5 文件 | 快照幂等、入库不异常、零回归 |
| **P7-S2** | `ingest_orders` 锚定 + 旧行继承 + 回填端点 + `external_order_lines` 五列落地 | 3 文件 | **锚定固化测试**（F-4/F-5 是验收闸） |
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

## 9. 测试计划（F-1..F-16，基线 574 → 预期 ~590）

| 编号 | 断言 | 说明 |
|---|---|---|
| F-1 | 确认入库 → `cost_snapshots` 落 1 行，`effective_time == confirmed_at`，`source='inbound_confirm'` | 主入口 |
| F-2 | 重复确认入库（幂等重放）→ 快照仍 1 行 | 幂等 |
| F-3 | as-of 抓取：有 T-10天/T-2天/未来 三条快照，T_sale=T-5天 → 命中 T-10天那条 | 时间对齐 |
| F-4 | **锚定固化**：锚定后再插一条新快照 → 订单行 `unit_cost` 与利润**完全不变** | ★核心红线 |
| F-5 | **重建继承**：同一订单二次 ingest（改价）→ 锚定五件套原样保留，`cost_status` 不退回 missing | ★必踩坑 |
| F-6 | 无快照 → `cost_status='missing'`，利润 Unknown，计入 `missing_cost_order_count`（**按订单去重**） | 黄条口径 |
| F-7 | 无内部 SKU 映射 → `cost_status='unmapped'`，**不计入**黄条的 X | 归因分离 |
| F-8 | 回填：`dry_run=true` 零写库；`mode=next_after_sale` 只补 missing、不覆盖 resolved；重复回填幂等 | 回填 |
| F-9 | 回填写结构化日志（自带 handler 捕获，照 E-6 范式） | 可审计 |
| F-10 | 利润公式对拍：手算 = 接口值（含退款/取消净数量、平台费扣除） | 口径 |
| F-11 | 改 `profit_platform_fee_rate` 后，已锚定行利润不变；新订单用新费率 | 费率锚定 |
| F-12 | 多币种：KPI `value=null`、`multi_currency=true`、排行按币种分组 | 不折算 |
| F-13 | 排行：`winners` 降序 Top10、`losers` 仅负利润升序 Top10、空态返回空数组不报错 | 排行 |
| F-14 | 权限：`analytics.read` 可读利润，无权限 403；`catalog.write` 才能录入/回填 | 权限 |
| F-15 | 只读纪律：`grep` 守 `cockpit.py` 无写操作；`ingest_orders` 新增查询 ≤1（SQL 计数断言） | 红线 |
| F-16 | 存量兼容：零 `cost_snapshots` 数据时 `/api/dashboard/cockpit` 不 500，KPI `quality=insufficient` | 上线安全 |

**连带修改（已知，实施时一并登记台账）**：
1. alembic head 断言 `0024` → `0025`
2. `cockpit.html` `renderKpis` 数组（若有测试断言顺序则连带）
3. `_PAGE_ANCHORS` 新增锚点（若有锚点自检测试则连带）
4. cockpit 查询条数上限 6→7（`LIMITATIONS` 文案 + 模块 docstring 同步）

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

**请审核 §11 的 Q1–Q7。全部采纳推荐值则直接回复「按推荐值开工」，我升 v1.1 并给交接命令。**
