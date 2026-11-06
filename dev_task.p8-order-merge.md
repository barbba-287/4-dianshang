# dev_task.p8-order-merge — P8-S1：展平报表自动预聚合（R-66）

- 版本：**v1.0（可实施）**
- 基线：`feat/warehouse-mvp@e5bc311`，全量 **616 passed / 0 failed**（P7-S3 交付后）
- alembic head：`0025_cost_snapshots`（**本片零新迁移**）
- 分支：`feat/warehouse-mvp`（沿用，不新建分支）

## 0. 一句话前提（必须先看到）

商家上传的淘宝/拼多多报表是**一行一个 SKU**，而系统契约是**一行一个订单**。
P7-S3 的 R-65 把这种文件从「500」修成了「422 + 让商家手工合并」——**能报错，但业务仍然走不通**。
本切片把「手工合并」变成「系统自动合并」：同一订单号的多行，在入库前合成一个订单 + 多条明细。

**没有新端点、没有新表、没有新迁移**。改动集中在「订单记录的入库前预处理」这一层。

## 0.1 用户裁定记录（2026-11-04 11:06）

| # | 规则 | 落地位置 |
|---|---|---|
| 1 | **触发条件**：仅针对通过 **CSV 导入**且订单号相同的行；**Shopify API 导入不受影响** | §2 M1（判据用 `record.source_mode == "csv"`，不是"猜"） |
| 2 | **合并规则**：主表金额取第一行（或多行必须一致，否则报错）；状态/时间取第一行；明细（lines）合并成数组 | §2 M3 / M4 / M5 |
| 3 | **安全底线**：合并后主表金额与明细之和的关系必须**打点记录** | §2 M6 + §4.3 |
| 4 | **必须补测试**：3 个 SKU 的同一订单号 CSV → 自动聚合成 1 单 3 明细 → 成功入库 | §5 F-27 |

**M3 的一处细化（请复核，非偏离）**：用户的选项是「取第一行」或「不一致就报错」。
两者都会在真实文件上出问题——取第一行会**静默丢账**，报错会让业务闭环继续卡住。
本片采用**三态判定**（详见 M3），既不解锁猜测，也不丢金额。理由与反例写在 §2.3。

## 1. 侦查结论（2026-11-04 11:06 只读核实）

### 1.1 ⚠️ 硬冲突一：`_isolate` 的「记录数 == 行数」不变量

`app/imports.py:159`：`if exc is None and len(records) == size: return records`。
P6-S3 修「静默丢行」时把判定改成**计数比对**（一数据行必产出一记录），二分隔离靠它成立。

**若在 `load_orders` 内部聚合并名订单**：3 行 → 1 记录 → `len(records) != size` → 3 行全被判坏行 → 全进 `rejected`；
`parse_upload` 末尾的 `len(records) + len(rejected) != len(mapped_rows)` 还会直接抛错整单失败（`imports.py:242`）。

→ **结论：聚合绝对不能发生在 `load_orders` / `_isolate` 之内。** 聚合必须在「行级隔离完成之后」，
即 `ingest_orders` 入口处。这样 `_isolate` 的 1:1 假设原样保留，P6 的静默丢行护栏一行都不用改。

### 1.2 ⚠️ 硬冲突二：CSV 的单行包装让每行都叫 `line:0`

`app/connectors.py:369-376`：CSV 行若没有 `lines` 列但有 `external_sku`，会被包成单行 lines：
`[{"external_sku": ..., "ordered_qty": ..., "gross_amount": ...}]`。
而订单的列映射表（`app/column_mappings.py:30-43`）**没有 `external_line_id`** ——
所以 `normalize_order_row` 给每行都造 `external_line_id = "line:0"`（`connectors.py:315`）。

合并 3 行后若直接拼接 lines → 3 个 `line:0` → 撞
`uq_external_order_line_identity (workspace_id, external_order_id, external_line_id)`（`db.py:829`）→ IntegrityError → 500。

→ **合并时必须保证组内 `external_line_id` 唯一**（M5）。这是本片第二个必踩坑。

### 1.3 ⚠️ 硬冲突三：`payload_hash` 决定幂等，合并后必须重算

`ingest_orders` 用 `existing.payload_hash == record.payload_hash` 判 `no_op`（`external_orders.py:370`），
`idempotency_key` 另有唯一约束 `uq_external_order_workspace_account_key`（`db.py:792`）。
合并出的 record 是**新对象**，必须重新算 hash 与 key，否则：
- hash 沿用首行 → 第二行内容变了也判 `no_op` → **静默丢更新**；
- hash 每次算得不一样 → 同文件重传永远 `updated` → **幂等失效、锚定反复重建**。

→ **hash 用 `_payload_hash(record.as_dict())` 一次性重算**（`as_dict` 是确定性的，`_payload_hash` 已 `sort_keys=True`）；
key **取首行的 `idempotency_key` 原值**（不重算公式）。理由：这样「先传只含第 1 行的文件、后传完整文件」
会命中 `uq_external_order_workspace_account_key` 的同一行，走 update 分支而不是插重复；
若按公式重算，商家若在 CSV 里显式给了 `idempotency_key` 列（normalize 允许），合并就会把它丢掉。

⚠️ 注意：`as_dict()` 的键集与 `normalize_order_row` 内部的 `canonical` **不同**（前者固定 15 键，后者含原始 CSV 列）。
所以**只有合并出来的 record 才用 `as_dict` 重算**；单行 record 一律原样保留，hash 不动（否则会打挂既有幂等测试）。

### 1.4 成本锚定不能被"合并"污染（P7-S2 红线的延续）

合并后每个明细行仍然带着自己的 `external_sku` → `ingest_orders` 里逐行查映射 + 逐行 as-of。
**合并不得让多个 SKU 共享同一个 `unit_cost`**。这条必须写成测试（F-33）。

### 1.5 入口盘点（4 个，判据只有一个）

| 入口 | source_mode | 是否聚合 |
|---|---|---|
| `POST /api/external/imports/commit`（文件上传，商家主路径） | `csv` | ✅ |
| `POST /api/external/orders/ingest`（content 版） | payload 指定 | 仅 `csv` |
| `POST /api/external/orders/preview`（content 版） | payload 指定 | 仅 `csv` |
| `platform_sync.ingest_fixture`（测试/回放夹具） | 指定 | 仅 `csv` |
| `POST /api/shopify/sync`（live） | `shopify_live` | ❌ **绝不聚合** |
| 适配器 `platform_adapters/*` → `load_orders(json)` | `json` / `mock` | ❌ |

判据统一为**记录自带的 `source_mode == "csv"`**，不比较平台名、不看端点数。Shopify live 走 adapter 直接构造
`ExternalOrderRecord(source_mode="shopify_live")`，物理上碰不到这条分支。

## 2. 合并规则（写死，实现与测试都必须逐条对齐）

### M1 触发条件

同一批 `records` 内，分组键 `(platform, account_ref, store_ref, external_order_no)` 相同的记录数 ≥ 2，
**且组内所有记录 `source_mode == "csv"`**，才尝试合并。其余一律原样返回。

### M2 主表字段：必须一致，不一致则**放弃合并**（不猜、不报错）

下列字段组内出现**多于一个不同取值** → 该组不合并，原样交回，由 R-65 判重逻辑发 422（文案已可操作）：

| 字段 | 理由 |
|---|---|
| `order_status` | 状态决定这单算不算销售（`SALE_ORDER_STATUSES`），猜错直接影响利润 |
| `currency` | 混币种求和是财务事故 |
| `external_created_at` | 它是成本 as-of 的时间锚（P7-S2 §5.3），猜错会锚到错成本 |
| `external_updated_at` | 决定"新旧"判定 |

**不做「取第一行」**：这 4 个字段是订单级事实，同一订单在真实报表里必然逐行重复且相同；
不同说明文件本身自相矛盾，此时合并等于替商家编造事实。**R-65 的 422 在这里是正确的兜底**。

### M3 金额：三态判定（对裁定的细化，见 §2.3）

对 `gross_amount` 与 `refund_amount` **各自独立**判定：

| 态 | 条件 | 主表取值 | 说明 |
|---|---|---|---|
| `SAME` | 组内取值去重后只有 1 个（含全为 0） | 该值 | = 用户裁定的「取第一行」，此时取第一行与取唯一值等价 |
| `SINGLE_NONZERO` | 去重后恰有 1 个非零值，其余全为 0 | 那个非零值 | 订单级金额挂在首行的常见形态 |
| `SUM` | 存在 ≥2 个**不同的非零值** | 各行之和 | 每行填的是本行金额，求和才自洽（与明细逐行之和天然相等） |

三者互斥且覆盖全部情况。**不允许出现第四种处理**（尤其不允许「取第一行」把 SUM 态吞掉）。

### M3.1 R-67 金额分摊（用户 2026-11-06 裁定方案②，已实施）

`SAME` 态存在一个真实脏形态：商家把**订单总额复制到每一行**。此时主表 90 而明细之和 270，
利润引擎按行计算会把收入计三倍。处置：**按数量分摊**，判据四条同时成立才触发
（`order_aggregation.should_allocate`）：

1. M3 判成 `SAME`（各行取值唯一）；
2. **每行金额都等于单头金额**（"复制到每行"的指纹）；
3. 明细逐行之和 `!=` 单头金额（真的对不上才动）；
4. 至少两行。

分摊公式：`单行 = 单头 × 单行数量 / 总数量`；**末行用 `单头 − 前面各行之和` 补齐**，
保证分毫不差。分摊后 `gross_rule` 记为 `same_allocated`，`delta` 恒为 0，M6 校验通过后
才进入 P7 利润引擎。**其他形态（SUM / SINGLE_NONZERO / 本来就填对）一律不动**。
总数量为 0 时无法分摊 → 保持原值 + 保留 delta warning，绝不猜、绝不抛错。

`refund_amount` **本轮不分摊**（用户只裁定了 gross）：但 M6 新增 `refund_delta` 观测，
商家若把退款总额复制到每行会打 `R-71` warning。待裁定（§7 R-71）。

### M4 其余主表字段：取第一行，空则向后找第一个非空

`paid_at` / `event_version` / `status_reason` / `raw_ref` / `schema_version` / `simulated`。
这些不参与金额与成本锚定，取首行无风险（且 `event_version` 取首行可保证同文件重传的"新旧判定"稳定）。

`data_completeness`：任一行 `partial` → 合并后 `partial`（取最差，不取首行）。

### M5 明细合并

1. 按记录原顺序拼接所有行的 `lines`（行内顺序也保留）；
2. `external_line_id` 去重：
   - 全部唯一 → **保留原值**；
   - 出现任何重复 → **整组统一重编号** `line:0 .. line:n-1`（按拼接后的位置）。
     统一重编号而非"只改重复项"，是为了保证结果**确定性**：同一文件重传 → 同样的 id → 同样的 hash → `no_op`。
3. 明细行的 `payload_hash` 保持各自原值（它是行内容指纹，与订单级 hash 无关）。

### M6 安全底线：金额对账打点

合并成功后计算 `delta = 主表 gross_amount − Σ(明细 gross_amount)`，并**无条件** 记一条结构化日志：

```
R-66 预聚合 order=%s rows=%d lines=%d rule=%s main_gross=%s lines_sum=%s delta=%s
```

- `delta == 0` → `logger.info`；
- `delta != 0` → `logger.warning`。R-67 分摊上线后，正常路径下 **delta 恒为 0**（分摊会把
  "复制到每行"的形态摊平），仍非零只剩两种可能：全部行数量为 0（无法分摊）、或形态不满足
  分摊判据——两种都值得排查。
- `refund_delta = 主表 refund − Σ(明细 refund)` 非零 → 额外打 `R-71` warning（只观测不分摊）。

**不因 delta != 0 拒绝入库**：`SAME` 态下 delta 非零是数据形态问题，拒绝会让大批合法文件再次卡死；
本片的承诺是「不再静默」，不是「替商家判定哪种写法对」。

### M7 合并失败 = 静默降级为原样（不抛错）

`merge_group` 内部任何判定不通过，都返回「不合并」，绝不抛异常、绝不丢记录。
记录原样交回后：R-65 会拦内容冲突的重复（422）；内容完全相同的重复会走 `no_op`。

**为什么不让聚合层自己抛错**：`ingest_orders` 的失败语义是"整批回滚"，
而聚合是**尽力而为的增强**；把增强失败升级成整批失败，等于用新功能扩大故障面。

## 3. 数据流（实施者按此顺序写，顺序即正确性）

```
CSV 文件
  → imports.parse_upload（列映射 + _isolate 行级隔离，1 行 1 记录，**不聚合**）
  → ExternalOrderRecord × N（source_mode="csv"）
  → ★ ingest_orders 内：aggregate_order_records()（纯内存，无 DB 访问）
  → find_duplicate_order_numbers()（R-65，聚合后残留的真冲突才报 422）
  → 既有写入循环（逐行映射 + 逐行 as-of 锚定，**完全不动**）
```

**聚合必须在 R-65 判重之前**，否则：3 行合法展平数据在判重处就被 422 拦掉，永远走不到合并。

## 4. 施工规格

### 4.1 新文件 `app/order_aggregation.py`（约 180 行）

```python
"""R-66 展平报表自动预聚合：同一订单号的多行 CSV → 一个订单 + 多条明细。

只做纯内存变换，不碰数据库、不碰 ORM、不抛异常（失败即降级为原样）。
调用方：ingest_orders（入库前）、两个 preview 端点（展示一致）。
"""

MERGE_RULE_SAME = "same"
MERGE_RULE_SINGLE_NONZERO = "single_nonzero"
MERGE_RULE_SUM = "sum"

@dataclass(frozen=True)
class MergeReport:
    order_no: str
    row_count: int
    line_count: int
    gross_rule: str
    refund_rule: str
    main_gross: Decimal
    lines_gross_sum: Decimal
    delta: Decimal

    def as_log_fields(self) -> dict[str, Any]: ...

def order_group_key(record) -> tuple[str, str, str, str]: ...

def merge_group(group: list[ExternalOrderRecord]) -> tuple[ExternalOrderRecord, MergeReport] | None:
    """合并一组同名记录；不可合并返回 None（绝不抛错）。"""

def aggregate_order_records(
    records: list[ExternalOrderRecord],
) -> tuple[list[ExternalOrderRecord], list[MergeReport]]:
    """返回 (聚合后记录按原顺序排列, 合并报告)。非 csv / 单条组原样返回。"""
```

**实现要点（按顺序踩）**：

1. 分组用 `dict`（Python 3.7+ 保序），输出顺序按组内首条出现位置排列——保证确定性。
2. `merge_group` 全部字段判定用 `dataclasses.replace` 构造新 record，**不 mutate 入参**（frozen dataclass）。
3. 重算 hash：`from app.connectors import _payload_hash`（若嫌私有，在 connectors 加
   `def record_payload_hash(record) -> str: return _payload_hash(record.as_dict())`，**只供合并路径调用**）。
4. `idempotency_key` 取首行原值（见 §1.3 末段）。
5. `simulated` / `source_mode` / `schema_version` 取首行。

### 4.2 `app/external_orders.py`（2 处）

```python
# ingest_orders 开头，R-65 之前
original_total = len(records)
records, merge_reports = aggregate_order_records(records)
if merge_reports:
    _log_merge_reports(merge_reports)          # M6 打点
stats = SalesImportStats(total=original_total, merged_orders=len(merge_reports),
                         merged_rows=sum(r.row_count for r in merge_reports), ...)
duplicates = find_duplicate_order_numbers(records)   # ← 位置不变，只是输入变了
```

`SalesImportStats` 增两个带默认值的字段（`merged_orders: int = 0` / `merged_rows: int = 0`），
**不改任何响应 schema**（避免打挂键集断言测试）。

### 4.3 `app/main.py`（2 处 preview）

- `external_orders_preview`（content 版，`main.py:3333` 附近）
- `external_import_preview`（file 版，`main.py:4277` 附近）

两处都要在返回前调 `aggregate_order_records(records)`，保证「预览看到的条数 == 入库后的订单数」。
`total` 字段**保持数据行数不变**（`outcome.rows_total`）——"3 行 → 1 单"由 `len(normalized_rows)==1` 体现。

### 4.4 前端（`app/templates/sources.html`，选做但建议做）

导入预览处加一行灰字提示，仅当 `total > normalized_rows.length` 时显示：

> 已自动合并 {total - rows.length} 行同名订单，合并后共 {rows.length} 个订单

不新增接口字段，纯前端计算。**不得改动任何既有 id / class 名**（P5 契约测试锁着）。

## 5. 验收闸（F-27..F-35，全部必须有测试）

| 编号 | 内容 | 判定 |
|---|---|---|
| **F-27** ★用户指定 | CSV 含 3 个 SKU 的同一订单号（3 行）→ 走 `/api/external/imports/commit` → **成功**，DB 里 1 个订单、3 条明细 | 订单数==1、明细数==3、明细 SKU 集合完整 |
| F-28 | 同一 CSV 重传 → 全部 `no_op`，订单仍 1 个、明细仍 3 条（幂等） | `inserted==0 && no_op==1`，明细数不变 |
| F-29 | M3 三态各一例：`SAME` / `SINGLE_NONZERO` / `SUM` 的主表金额 == 期望值 | 逐态断言 + `gross_rule` 字段 |
| F-30 | `order_status` 不一致 → **不合并** → 422 `DUPLICATE_ORDER_NO_IN_BATCH`，零写入 | 错误码 + orders==0 |
| F-31 | 非 CSV 渠道不受影响：`source_mode="json"` 的两条同名冲突记录仍 422；`shopify_live` records 即使同名也原样通过 | 两条独立断言 |
| F-32 | 3 行都缺 `external_line_id`（CSV 无该列）→ 合并后 3 个 id 互不相同、入库不撞唯一约束 | 明细 id 集合长度==3 |
| F-33 | 合并后逐 SKU 成本锚定各自独立：3 个 SKU 三个不同 `unit_cost`，明细 `unit_cost` 与 SKU 一一对应 | 不得共享成本 |
| F-34 | 查询预算不增：`ingest_orders` 处理合并批次时，新增 SELECT 数 == 未合并批次（聚合是纯内存） | 计数器断言，允许 ±0 |
| F-35 | 明细逐行金额之和 == 主表 `gross_amount`（`SUM` 态下必须精确相等，两位小数） | `sum(line.gross) == order.gross` |

## 6. 红线

1. **不得在 `load_orders` / `_isolate` 内聚合**（§1.1 会把 P6 的静默丢行护栏打挂）。
2. **不得让聚合抛异常**（M7）：聚合是增强，不是校验。
3. **只对 `source_mode == "csv"` 生效**（M1）：Shopify live 与 JSON 路径一字节都不许变。
4. **合并后必须重算 `payload_hash` 与 `idempotency_key`**（§1.3），且结果必须确定性。
5. **不得让多个明细行共享同一个 `unit_cost`**（§1.4）。
6. **不得新增迁移**：本片零 schema 变更。
7. **不得改动 R-65 的判重语义**：聚合只是把"可合并的"提前合并，剩下的仍然 422。

## 7. 已知限制（如实登记，不偷偷留坑）

| 编号 | 限制 | 影响 | 处置 |
|---|---|---|---|
| **R-67**（已关闭 · 方案②） | `M3-SAME` 态：若商家把**订单总额复制到每一行**，主表取该值而明细每行都是总额 → 利润引擎按行计算会**重复计入收入** | 修复前：利润虚高至 3 倍（收入 270 而非 90） | 用户 2026-11-06 裁定方案②「导入时按数量分摊」→ 已实现 M3.1 + F-37 系列（含末行补齐、确定性重传、零数量降级）。`gross_rule=same_allocated`，`delta` 归零 |
| **R-71**（新登记） | `refund_amount` **未**分摊：若商家把订单退款总额复制到每行，利润引擎按行的退款冲减会重复扣 | 利润偏低（收入被多扣） | 本轮只加 `refund_delta` 观测 + `R-71` warning，自动分摊待用户裁定（判据可与 M3.1 完全同构） |
| **R-68** | 合并后的 `external_line_id` 是**按位置**重编号的（`line:0..n`）。若商家重传时调整了行顺序，旧锚定会按 id 错位继承 | 极端情况下某行继承了另一行的成本 | 概率低（同文件重传顺序一致）；若要根治需引入「按 SKU + 行序号」的稳定指纹，属 P9 |
| **跨批** | 同一订单的行**分散在两个文件**里时不会合并（第一份只有第 1 行，第二份有第 2、3 行）→ 第二次导入覆盖第一次，丢失第 1 行 | 数据不全 | 需「订单级 UPSERT + 行级 MERGE」才能根治，登记 **R-69**（P9+） |
| **R-70**（实施中发现并已修） | `main.py` 两处 `ImportStats(total=..., inserted=..., no_op=..., conflict=...)` **漏传 `updated` / `stale`** → 订单导入响应里「更新 N 条」永远是 0，商家无法判断重传是「更新了」还是「被跳过」 | 可观测性丢失（不影响数据） | 已补 `updated=result.updated, stale=result.stale`（`main.py:3394` / `:4407`）。P6 起就存在，因本片「重传必定走 updated」才暴露 |

## 8. 明确不做（防范围蔓延）

- 不做「订单级 UPSERT + 行级增量合并」（那是跨批问题，R-69）。
- 不做 `refund_amount` 的自动分摊（R-71 待裁定，本轮只观测）。
- 不改前端导入表格结构，不加导出。
- 不动 `finance.py` / `cockpit.py` 的任何口径。
- 不动 Shopify 适配器。

## 9. 交接五要素（实施者必读）

1. **目标文件**：新建 `app/order_aggregation.py`；改 `app/external_orders.py`（`ingest_orders` 开头 + `SalesImportStats`）、
   `app/main.py`（两个 preview 端点）、`app/templates/sources.html`（提示条，选做）。
2. **现状代码**：`ingest_orders` 见 `external_orders.py:333-481`（R-65 判重在 344-347）；`as_dict` 见 `connectors.py:101-120`。
3. **改什么不改什么**：只在「records 已就绪、尚未判重」的窗口里插入聚合；写入循环、锚定逻辑、`_isolate` 全部不动。
4. **验收标准**：616 基线不回退；F-27..F-35 全绿；`test_r65_*` 三个用例必须仍然全绿。
5. **已排除的选项**：在 `load_orders` 内聚合（打挂 `_isolate`）；聚合失败抛错（扩大故障面）；
   按平台名判断（应看 `source_mode`）；新增迁移。
