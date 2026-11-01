# 任务文件：P4 前端经营工作台与数据看板（老板/运营全局驾驶舱）

- **版本**：v1.1 定稿（2026-11-01，用户已评审 + Q-P4-1/2/3 三项裁定落定，可作为实施唯一依据）
- **切片**：P4（单切片，不分片）
- **基线**：`475 passed / 0 failed`（P3-S3 收口后，P3 全部 15 笔已推远端 `fa99bb2`）；本片目标 **486 passed / 0 failed**
- **前置**：`docs/ui-reference-checklist.md` v5（本文引用其 §1.1–§1.6 作验收口）
- **交付说明落盘路径**：`.testtmp/delivery.p4.md`（沿用 R-17/R-22/R-55 协议，完成即写盘）

---

## 0. 结论摘要（先看这 9 条）

1. **现状**：`/dashboard` 已是「库存运营驾驶舱」（5 面板 + 3 图 + 2 表），但它**没有销售金额视角**——
   `app/dashboard.py:459` 明确把 `gmv` 列为 `unsupported_metrics`。老板视角缺的不是样式，是**金额与待办**。
2. **本片新建 `/cockpit` 页面 + `GET /api/dashboard/cockpit` 只读聚合接口**，
   **不改 `dashboard.html` / `dashboard.py` / `analytics*.py`**（理由见 D1：UI 清单 §1.1 已把 `/dashboard`
   重排标为暂缓；且 `tests/test_api_dashboard.py` 对 8 个 panel id 逐条断言，改造=自找回归）。
3. **7 项指标中 6 项有真实数据源且口径可追**（见 §1.3 盘点表）；**1 项（积压）无既有定义 → 已裁定口径 A（Q-P4-1）：滞销 + 慢周转，明细带 reason**。
4. **GMV 与订单数必须同源同谓词**（`external_orders` + `paid/fulfilled/completed`），
   **严禁用 `daily_sku_sales.order_count` 求和**——它按 (SKU, 日) 计数，跨 SKU 求和会把多 SKU 订单重复计（F-P4-1）。
5. **demo 环境没有订单事实**（`app/demo_seed.py` 只造 `DailySkuSale`，零 `ExternalOrder`，已实测）→
   GMV/订单数卡在 demo 下为 0，走空状态。**已裁定不补 seed（Q-P4-2）：demo 显示 0 与空态即正确行为，严禁改 demo_seed.py**。
6. **多币种混加是错的**（`external_orders.currency` 存在多值）→ **已裁定按币种分组、绝不折算（Q-P4-3）**，响应体留 `fx_todo` TODO 注记。
7. **涨跌色不定义**：UI 清单 §1.6 的超期规则已写死「未确认前 token 不定义涨跌色」，
   故环比一律用 **箭头 + 文字**，不上红绿（本片遵守，不另行讨论）。
8. **零 migration、零写入、零既有接口改动**；唯一触碰既有文件是 `app/main.py`（**纯追加**导航项与 2 条路由）。
9. **【硬性】图表空态防御**：本环境很可能没有足够订单量，聚合结果全 0 是**常态而非异常**。
   **如果没有数据，ECharts 必须显示 Empty 占位，不能报错或白屏。**
   即：任一张图在 `echarts.init()` **之前**先判「是否有可绘制数据」，无数据直接渲染占位块并 `return`，
   **绝不**先 init 再画空坐标系。范式抄 `app/templates/dashboard.html:11` 的 `chartEmpty(id, text)`。
   详见 D8 与 §5 第 11 条、P-9/P-10。

---

## 1. 现状盘点（Fact，全部实测，带行号）

### 1.1 页面与静态资源

| 项 | 事实 | 位置 |
|---|---|---|
| 模板 | `dashboard.html`（运营驾驶舱，5 面板 + 3 图 + 2 表 + 异常收件箱） | `app/templates/dashboard.html` |
| 静态 | `echarts.min.js`（**本地**，非 CDN）、`workspace-shell.css`、`labels.js` | `app/static/` |
| 页面路由范式 | `_render_workspace_page(template, principal, active_path)`，按角色守门 | `app/main.py:317-327`、`:1925-1928` |
| 导航 | `_WORKSPACE_NAV_ITEMS`（8 项）+ `_SYSTEM_NAV_ITEMS`（1 项）+ `_PAGE_ANCHORS`（页内锚点） | `app/main.py:245-279` |
| 导航渲染 | `_visible_navigation`：demo 全放行；否则 `has_role AND can(permission)` | `app/main.py:280-300` |

**既有驾驶舱 API**：`GET /api/dashboard/summary`（`app/main.py:1896-1919`，角色 `(ADMIN, OPERATIONS)`）
→ `AnalyticsQueryService.dashboard_summary`（`app/analytics_queries.py`）→ `build_dashboard_summary`（`app/dashboard.py:310`）。
**其 `unsupported_metrics` 显式包含 `gmv`（`app/dashboard.py:459`）**——本片不动它，见 D3 注。

### 1.2 指标可行性盘点（本片核心）

| 指标 | 数据源（表.列） | 现状判定 | 可行性 |
|---|---|---|---|
| **GMV（7/30 天）** | `external_orders.gross_amount / refund_amount / currency / external_created_at / order_status / data_completeness` | **无任何接口提供**；`dashboard.py:459` 明标 unsupported | ✅ 可上（新增只读聚合） |
| **订单数趋势** | `external_orders` 按 `external_created_at` 日聚合 `count(distinct id)` | 无 | ✅ 可上 |
| **销售额语义基准** | `_is_sale()` = `{"paid","fulfilled","completed"}`（`app/external_orders.py:120-121`）；行净额 = `max(gross−refund, 0)`（`:124-127`） | 既有唯一真值 | ✅ 复用其语义（不改该文件，见 D3） |
| **缺货预警** | `inventory_balances.on_hand_qty` + `inventory_policies.safety_stock_qty / reorder_point_qty` | 判定语义既有：`on_hand ≤ reorder_point` → warning；`on_hand ≤ safety_stock` → critical（`app/alerts.py:53-55`） | ✅ 可上（直接聚合，不依赖 alert 行） |
| **积压预警** | 需「库存量 + 销售速度」；`inventory_balances` 只有 `on_hand_qty`（`db.py:658-676`），速度来自 `daily_sku_sales.net_qty` | **无既有定义** | ✅ 已裁定口径 A（Q-P4-1）：`NO_SALES`（近 30 天净销量 0 且 on_hand>0）或 `SLOW_MOVING`（可售 ≥90 天） |
| **采购待审批数** | `purchase_requests.status == 'submitted'`（状态机：`draft/submitted/approved/rejected/cancelled`，`purchase_approval.py:35-37`） | 可查 | ✅ 可上 |
| **未完成入库数** | `inbound_orders.status ∈ {'expected','received'}`（`db.py:333` 默认 `expected`；`received`@`repository.py:713`、`confirmed`@`:764`；**无取消态**） | 可查 | ✅ 可上 |
| **知识库健康** | `document_versions.published_at IS NOT NULL`（S3 发布位）+ `document_chunks`（`db.py:237-255`）+ `document_versions.status ∈ {parsing,ready,failed}` | S3 已建发布门控 | ✅ 可上 |
| 达人合作漏斗 | 无任何表 | — | ❌ 不上首屏（UI 清单 §1.2 已登记为「依赖 P3+/不可实现」） |

### 1.3 关键陷阱（实施前必读，写死）

- **F-P4-1 订单数不可用 `daily_sku_sales.order_count` 求和。**
  该列是 `(external_sku, sales_date)` 维度的 **distinct 订单数**（`external_orders.py:188`），
  一张含 3 个 SKU 的订单会在 3 行各记 1 次 → 求和 = 3 倍虚高。
  **订单数只能来自 `external_orders` 的去重计数。**
- **F-P4-2 GMV 的两种可能来源不可混用。**
  `daily_sku_sales.net_amount` 是**行级**金额汇总且**含未映射 SKU 行**（`external_sku` 维度重建，`external_orders.py:154-196`）；
  `external_orders.gross_amount` 是**订单头**金额。二者理论上相等但**未验证**（可能有平台级折扣/运费差）。
  本片统一取 `external_orders`（与订单数同源，口径最简）；**不得**两处各取一半拼数。
- **F-P4-3 旧接口与新接口的口径表述会不一致（接受）。**
  `app/dashboard.py:459` 仍声明 `gmv` 不支持，而新接口支持。
  处置：**不改旧接口**（契约冻结 + 有测试守着），在 `/cockpit` 的口径说明里注明
  「旧 `/api/dashboard/summary` 为库存口径聚合，不含 GMV；GMV 由本接口提供」。
- **F-P4-4 demo 环境零订单事实。** `app/demo_seed.py:101-118` 只写 `DailySkuSale`（含 `net_amount=39×qty`），
  **不写 `ExternalOrder`**（已 grep 确认 `ExternalOrder` 在 demo_seed 中 0 命中）。
  → demo 下 GMV/订单数为 0 是**正确行为**（不是 bug），卡片走空状态并写明原因。
- **F-P4-5 时间基准统一 UTC 日界。** `external_created_at` 是 naive UTC，既有 `daily_sku_sales.sales_date`
  也取自 `external_created_at.date()`（`external_orders.py:153`）；`resolve_sales_as_of`（`app/business_dates.py:12-40`）
  是既有 as_of 解析口径。本片 **as_of 必须复用该函数**，`meta.timezone` 固定声明 `"UTC"`。
- **F-P4-6 无 policy 的 SKU 不能算「缺货」。** `inventory_policies` 按 `(workspace, warehouse, sku)` 唯一
  （`db.py:616-631`），存在零 policy 的 SKU。缺货判定必须有 policy 才成立，
  无 policy 的 SKU 进 `unknown_policy` 计数并计入 `limitations`，**不得**默认按 0 阈值判成缺货。

---

## 2. 设计决定（D1–D7）

### D1 新建 `/cockpit`，不改 `/dashboard`

- 新增页面 `app/templates/cockpit.html` + 路由 `GET /cockpit`（`(ROLE_ADMIN, ROLE_OPERATIONS)` + `analytics.read`）。
- 导航：`_WORKSPACE_NAV_ITEMS` 追加一项 `/cockpit`（放在 `/dashboard` 之前，图标 `◎`），
  `_PAGE_ANCHORS` 追加其锚点。**这是对既有文件的唯一修改，且是纯追加。**
- **排除的做法**：改造 `dashboard.html` 首屏。
  理由：① UI 清单 §1.1 明确「`/dashboard` 重排已标暂缓，本轮不动」；
  ② `tests/test_api_dashboard.py:193-204` 逐条断言 8 个 panel id、标题、quadrant 函数名与字符串，
  任何重排都会连带改一堆既有断言（= 改性质，不是加功能）；
  ③ 老板视角与运营视角的默认筛选、指标口径不同，混在一个页面会长出第二套筛选逻辑。

### D2 六张指标卡（≤6，符合 §1.2）

| # | 卡片 | 主值 | 环比对象 | 口径要点 |
|---|---|---|---|---|
| 1 | GMV | 窗口内 `Σ(gross_amount − refund_amount)`，`_is_sale` 过滤，分币种 | 上一等长窗口 | 「外部平台订单聚合、只读、不等于内部履约收入」（UI 清单原文） |
| 2 | 订单数 | 窗口内 distinct `external_order_no` | 上一等长窗口 | 同谓词；与 GMV 同源 |
| 3 | 缺货 SKU | `on_hand ≤ safety_stock` 的 SKU 数 | 不设（当前态） | 有 policy 才判；无 policy 进 `unknown_policy` |
| 4 | 积压 SKU | Q-P4-1 口径 A | 不设 | 明细 `reason ∈ {NO_SALES, SLOW_MOVING}`；前端文案「因近 30 天无销量滞销」/「预计可售超 90 天」 |
| 5 | 采购待审批 | `status='submitted'` 的单数 | 不设 | 采购单状态机现状 |
| 6 | 知识库可用切片 | 已发布版本的 chunk 数（`published_at IS NOT NULL` 的版本下 `document_chunks`） | 不设 | 与 RAG 检索池同源（S3 门控后「已发布=可检索」） |

**每张卡必须满足 §1.2 四件套**：主数字（等宽、2.5–3 倍字号）、标签、变化值（1/2 卡有，箭头+文字不上色）、
辅助信息（口径来源一句话）+ **可点开的口径说明**（`<details>`，零新依赖）。
「未完成入库」**不做卡片**，放底部待办列表（第 4 项要求正是「底部异常/待办列表」）。

### D3 GMV/订单数口径：`external_orders` + 本地常量 + 对拍用例

- 新模块内定义 `SALE_ORDER_STATUSES = frozenset({"paid", "fulfilled", "completed"})`，
  **不改** `app/external_orders.py`（`_is_sale` 是私有函数，import 私有名会制造耦合）。
- 防漂移：本片用例中**新增一条对拍断言**——用 source 里的 `_is_sale`（测试内 import 私有名**允许**，
  测试是唯一可以碰私有的地方）逐状态断言与 `SALE_ORDER_STATUSES` 语义一致。
- 金额返回**字符串**两位小数（避免 float 精度与前端二次舍入），大额用 `Decimal` 计算。

### D4 缺货/积压：直接聚合，不依赖 alert 行

- 缺货：`JOIN inventory_policies ON (sku_id)`，`on_hand ≤ safety_stock` → `out_of_stock`；
  `safety_stock < on_hand ≤ reorder_point` → `below_reorder`（与 `alerts.py:53-55` 语义一致）。
- 积压：按 Q-P4-1 口径 A 实现；速度基准复用 `daily_sku_sales.net_qty` 近 30 天，
  日均口径沿用 `build_sku_health` 的「仅 complete 日参与日均」规则（`app/dashboard.py:170-200`），
  不自己发明第二套算法。
- **分布饼图四类**：`out_of_stock / below_reorder / healthy / overstock`（+`unknown_policy` 单列角标，不占饼）。

### D5 知识库健康

- `documents` 总数、`published_versions`（`published_at` 非空版本数）、
  `available_chunks`（上述版本下 chunk 数）、`parsing` / `failed` 版本数。
- **不读向量库**（不调 `get_vector_store()`）——避免与 RAG 运行时耦合，DB 即真值
  （S3 后「已发布 ⇒ 允许入向量库」，最终一致的窗口由 `limitations` 声明）。

### D6 前端结构（严格照 UI 清单）

- **三段式（§1.1）**：顶部 6 卡 → 中部 2 图左右分栏 → 底部 4 个待办列表。
- **两张真实图表（§1.3）**：
  1. 销售趋势：GMV 折线 + 订单数柱（双 Y 轴，左金额右单数），**缺失日留 `null` 断开，不补 0**（沿用既有 `_sales_trend` 语义）；
  2. 库存分布饼图：四类 SKU 计数，语义色取 token（success/warning/danger/brand），**不用涨跌红绿**。
  图表数 = 2，符合「单页 ≤4」。
- **表格（§1.6）**：数字列右对齐 + `font-variant-numeric: tabular-nums`，金额两位小数，状态用 badge 不用纯文字。
- **空态（§1.4）**：五场景各一条——列表无数据（带解释）、筛选无结果（可一键清除）、首次加载（骨架块）、
  局部刷新（不遮挡）、出错（中文 + 重试按钮）。**禁止英文/裸报错**。
- **token（§1.5）**：仅在本页 `<style>` 内定义所选变量集，色值沿用现有事实 token
  （`--brand:#2a78d6` / `--ink:#142033` / `--line:#e4eaf2` / `--good` / `--warning` / `--critical`），
  **不新建全局 token 文件**（那是 §1.5 的独立改造项，本片不扩范围）。
- 图表库：只用 `/static/echarts.min.js`（§1.3 规则 1：全站单库）。

### D7 数据质量与诚实性（§1.2 硬约束的落实）

- 每张卡带 `quality`（`complete / partial / insufficient`）与 `reason`；
  窗口内存在 `data_completeness != 'complete'` 的订单 → `partial`。
- `meta.limitations` 至少四条：UTC 日界、GMV≠履约收入、无 policy SKU 未判缺货、
  发布→索引最终一致（KB 切片数为 DB 口径）。
- **0 值不等于无数据**：接口必须能区分「算出来是 0」与「没有事实」（返回 `quality`/`missing_days`）。

### D8 图表空态防御（硬性，逐图判定）

当前环境大概率**没有足够订单数据**（demo 零订单事实、dev 店也可能空），所以「图无数据」是常态路径，
必须按**一等公民**实现，不能靠 ECharts 画个空坐标系糊过去。

**统一实现规则（两图都必须遵守）：**

```
渲染每张图前，先算「可绘制判定」：
  折线/柱（销售趋势）：dates 长度 > 0 且 至少一个 gmv 或 order_count 非 null
  饼图（库存分布）  ：四类计数之和 > 0
判定为假 → chartEmpty('#容器id', '中文说明 + 可能原因')
         → 该图函数 return（不调 echarts.init，不留 canvas，不留白框）
判定为真 → 先 dispose 旧实例 → echarts.init → setOption
```

**四条禁止项（出现任一即视为不合格）：**

1. 禁止先 `echarts.init()` 再「数据为空就什么都不画」——会留下一个空白坐标系，视觉上像渲染失败。
2. 禁止把 `null` / 缺失日当 0 画（趋势图必须 `connectNulls:false`，缺失段断开）。
3. 禁止 `try/catch` 吞异常后不渲染占位（catch 里必须落到 `chartEmpty` 并写中文原因）。
4. 禁止占位区只写「暂无数据」四个字——必须带**原因**（如「近 7 天无订单事实」「当前 Workspace 无库存策略」）
   与**可操作提示**（如「请先在 Shopify 页同步订单」）。

**占位样式**：复用既有 `.chart-empty`（灰底、居中、`color:var(--muted)`），
在 `cockpit.html` 内 `<style>` 中重新定义一份同义变量（不引外部文件，遵守 §5 第 6 条）。
占位文案必须中文，且写明是「无数据」而非「加载失败」（加载失败走 §1.4 的独立错误态 + 重试按钮）。

**加载态区分**（§1.4 场景 3/4）：首次加载显示骨架块（与图表同高，避免布局跳动）；
刷新时只在容器右上角显示「刷新中」，不清空已有图表。

---

## 3. 裁定记录（2026-11-01 用户已拍板，实施按此为准）

### Q-P4-1 积压（overstock）口径 → **采纳口径 A（滞销 + 慢周转组合）**

系统没有任何「积压/滞销」的既有定义（已 grep：`safety_stock/reorder_point` 只用于缺货方向）。

**裁定**：`overstock = (近 30 天净销量 == 0 且 on_hand > 0)` **或** `days_of_inventory ≥ 90`
（阈值常数 `OVERSTOCK_DAYS_THRESHOLD = 90`）。

- **每条积压明细必须带 `reason ∈ {NO_SALES, SLOW_MOVING}`**，前端按 reason 显示不同中文提示：
  - `NO_SALES` → **「因近 30 天无销量滞销」**
  - `SLOW_MOVING` → **「预计可售超 90 天」**
  - 两句文案为用户钦定原文，**不得改写、不得只显示 reason 英文枚举**。
- 一个 SKU **同时命中两个 reason 时只出一条**（`NO_SALES` 优先，因为零销量已蕴含慢周转，
  两条并列会让老板重复处理同一 SKU）；`reason` 为单值字符串，不是数组。
- 已排除：口径 B（只判滞销，会漏掉日均 0.01 的慢销品）；口径 C（只判可售天数，零销量
  算不出 `days_of_inventory` 会被误判 `data_insufficient`，与业务直觉相反）。

### Q-P4-2 demo 环境的 GMV 空状态 → **不补 seed**

`app/demo_seed.py` 不造 `ExternalOrder`（F-P4-4），demo 登录后 GMV/订单数卡为 0 + 空状态。

**裁定：本片不补 seed。** demo 显示 0 与空态**是完全正确的行为**；
「改数据脚本」不属于前端切片范畴，**严禁越界修改 `app/demo_seed.py`**（写入 §5 禁区）。
空状态文案写「当前 Workspace 无订单事实（demo 环境未生成订单）」。

### Q-P4-3 多币种 GMV → **分组返回、分币种展示、绝不折算** + 响应体留 TODO 注记

`external_orders.currency` 为自由列（默认 `CNY`），系统**无汇率源**。

**裁定**：
- 按 `currency` 分组返回 `by_currency: [{currency, gmv, order_count}]`；
- 前端单币种时只显示一张卡；多币种时主卡显示分币种明细，**不做折算、不显示跨币种合计**；
- **响应 JSON 必须留 TODO 注记**，字段名 `fx_todo`，值为字符串原文：
  **「汇率折算依赖财务模块后续提供」**
  （位置：`by_currency` 同级的顶层键；`len(by_currency) >= 2` 时字符串按上式原样返回，
  单币种时同样返回该键以保证前端可无差别渲染——本字段是给未来财务模块的锚点，
  不是给用户看的错误信息，前端**不展示**，仅在 `console.debug` 输出）。

已排除：只统计 CNY 并把其他币种塞 `limitations`（静默丢数据，老板会看到假总额）。

---

## 4. 改动清单（文件级，纯新增为主）

### 4.1 新建

| 文件 | 内容 | 关键约束 |
|---|---|---|
| `app/cockpit.py` | 只读聚合 service：`build_cockpit(db, *, workspace_id, days, as_of)` → dict；积压明细带 `reason`（Q-P4-1 口径 A）；多币种 `by_currency` + 顶层 `fx_todo`（Q-P4-3） | **仅 SELECT**；零 `db.add/commit/delete`；零 `settings`/`environ` 开关；不 import fastapi |
| `app/templates/cockpit.html` | 三段式页面（6 卡 / 2 图 / 4 列表）+ 口径 `<details>` + `chartEmpty()` 空态占位（D8） | 复用 `_render_workspace_page` 注入侧栏；插值必须 `escapeHtml`；两图必须先判空再 init |
| `tests/test_p4_cockpit.py` | P-1..P-11（11 例） | 双账号夹具非必需（本片无 maker-checker），但**跨租户隔离必测** |

### 4.2 追加（既有文件，纯 additive）

| 文件 | 改动 | 边界 |
|---|---|---|
| `app/main.py` | ① `_WORKSPACE_NAV_ITEMS` 追加 `/cockpit`；② `_PAGE_ANCHORS` 追加锚点；③ `GET /cockpit` 页面路由；④ `GET /api/dashboard/cockpit` 接口 | 既有 4 个路由函数体**零改动**；不重排 `_WORKSPACE_NAV_ITEMS` 既有顺序（只插入） |
| `app/schemas.py` | 追加 `Cockpit*Response` 模型 | 既有模型零改动 |

### 4.3 明确不动（禁区）

`app/db.py`、`app/dashboard.py`、`app/analytics.py`、`app/analytics_queries.py`、`app/business_dates.py`、
`app/external_orders.py`、`app/replenishment.py`、`app/purchase_approval.py`、`app/knowledge_admin.py`、
`app/rag/*`、`app/rag_runtime.py`、`app/storage.py`、`app/config.py`、
**`app/demo_seed.py`（Q-P4-2 裁定）**、`app/templates/dashboard.html` 及全部既有模板、
`migrations/`、全部既有测试文件（含 `tests/test_api_dashboard.py`、`tests/test_frontend_warehouse.py`）。

---

## 5. 守卫与红线

1. **零 migration、零新表、零新列。**
2. **零写入**：`app/cockpit.py` 内不得出现 `db.add(` / `db.commit(` / `db.delete(` / `delete(` / `update(`。
3. **不改任何既有接口的请求/响应结构**（新增接口除外）。
4. **不改任何业务写入逻辑**（订单入账、库存入账、采购审批、RAG 发布/检索）。
5. **不新增权限点、不加开关、不读环境变量**。
6. **前端只新建一页**；不改 `/dashboard`、`/ops`、`/warehouse`、`/customer-service` 任何模板。
7. **多租户隔离**：所有查询强制带 `workspace_id`，跨工作区数据一条都不得出现（P-8 专项验证）。
8. **既有导航用例不得破**：`tests/test_frontend_warehouse.py:87-115` 只对 6 个已知路径断言存在/不存在，
   新增 `/cockpit` **理论上不破**；实施后必须实跑该文件确认（实测优先于推断）。
9. **不定义涨跌色**（UI 清单 §1.6 超期规则）；不用红绿表达金额涨跌。
10. **金额不落 float 汇总**：SQL 聚合用 `Decimal` / `cast(Numeric)`，返回字符串两位小数。
11. **图表空态必须显式占位（硬红线）**：**没有数据时 ECharts 不得报错、不得白屏、不得留空坐标系**，
    必须渲染 `.chart-empty` 占位块（中文原因 + 可操作提示）并 `return`。判定必须在 `echarts.init()` 之前（D8 四条禁止项）。
12. **图表容器必须常驻且可判定**：两个图表容器 `id="sales-chart"` / `id="inventory-pie"` 在 HTML 中始终存在
    （空态是「容器内有占位块」，不是「容器不存在」），以便 P-9 做结构断言。
13. **`app/demo_seed.py` 严禁触碰**（Q-P4-2 裁定：demo 显示 0 与空态即正确行为；
    补 seed 属数据脚本改动，不在前端切片范畴）。同属禁区的还有 `tests/test_api_dashboard.py`
    与 `app/dashboard.py`——旧接口与旧页面的既有断言一条都不许改。
14. **积压 reason 与文案必须一一对应**：`NO_SALES` ↔「因近 30 天无销量滞销」，
    `SLOW_MOVING` ↔「预计可售超 90 天」。**不得改写成同义句、不得把 reason 英文直接显示给用户**。
15. **`fx_todo` 必须逐字返回**「汇率折算依赖财务模块后续提供」（Q-P4-3），
    不得改写、不得省略、不得仅在多币种时返回（单币种也返回，前端可无差别渲染）。
16. **【性能硬红线】禁止 N+1 查询**（用户开跑时点名要求）：
    - 每个指标必须用 **一条 SQL 聚合**完成（`GROUP BY` / `SUM` / `COUNT(DISTINCT ...)` /
      `func.coalesce` 等），**严禁**「先查 SKU 列表、再逐个 SKU 查余额/销量」这类 Python 循环内查询；
    - 积压/缺货明细需要 SKU 维度的量时，用 **单条 `JOIN + GROUP BY sku_id`** 或
      **一次 `IN (...)` 批量捞取后内存组装**，两者都算合规；**循环里发 `db.execute` 不可接受**；
    - 趋势图数据用 **`GROUP BY 日`** 一次出全窗口数组，严禁按天循环查 7/30 次；
    - `build_cockpit` 全程 **查询条数上限 = 8 条**（6 个 KPI 各 1 + 知识库 1 + 明细 1，允许合并更少，
      不允许更多）；交付说明必须列「实际查询条数」，复核按此核对；
    - 若确需额外查询，必须在交付说明里逐条说明理由，不得默默超限。

---

## 6. 用例设计（P-1..P-11，基线 475 → 486）

| # | 用例 | 断言要点 |
|---|---|---|
| P-1 | GMV 与订单数基础聚合 | 造 3 单：2 单 `paid`（含 1 单部分退款）+ 1 单 `cancelled` → GMV = 两单净额之和，**cancelled 不计**；订单数 = 2 |
| P-2 | 窗口与环比 | 7 天窗口 vs 前 7 天；`window/days/from/to/prev_from/prev_to` 精确；环比 = `(cur−prev)/prev`，prev=0 时返回 `null` 而非 `Infinity` |
| P-3 | 稀疏日期不补 0 | 中间缺 2 天 → `gmv[i] is None`、`order_counts[i] == 0`、`missing_days == 2`（**不得**把缺失日写成 0） |
| P-4 | 多币种不混加（Q-P4-3） | 造 CNY 2 单 + USD 1 单 → `by_currency` 两组，主值不出现「跨币种合计」；响应顶层含 `fx_todo == "汇率折算依赖财务模块后续提供"`（原文逐字相等） |
| P-5 | 缺货 / 积压判定 | 有 policy 且 `on_hand ≤ safety_stock` → 计缺货；`safety < on_hand ≤ reorder` → `below_reorder`；**无 policy 的 SKU 进 `unknown_policy` 且不计缺货**；积压按 Q-P4-1 口径 A 命中 `NO_SALES` 与 `SLOW_MOVING` 各一，且各条 `reason` 单值、前端文案为用户钦定两句 |
| P-6 | 待办计数状态边界 | `submitted` 采购单计、`approved/rejected/cancelled/draft` 不计；入库 `expected/received` 计、`confirmed` 不计 |
| P-7 | 知识库健康只认已发布 | 造 draft（`published_at IS NULL`）+ published 各一版本，各 3 chunk → `available_chunks == 3`、`published_versions == 1`；`failed` 版本单独计数 |
| P-8 | 权限与跨租户隔离 | `customer_service` / `readonly` 访问 → 403；`operations` / `admin` → 200；另造 workspace 2 的订单/库存，**断言不出现在 workspace 1 的响应里** |
| P-9 | 页面渲染契约 | `GET /cockpit` 200；含 `id="kpi-grid"`、`id="sales-chart"`、`id="inventory-pie"`、4 个待办列表 id；导航含 `href="/cockpit"`；不出现 `id="logout-link"`（与既有单点约定一致） |
| P-10 | 空工作区不炸 | 无订单/无库存/无文档 → 200，所有 KPI 为 0 或 `null` + `quality="insufficient"`，前端文案中文；`missing_days == 窗口天数` |
| P-11 | **图表空态防御契约（硬性）** | ① 模板含 `function chartEmpty` 定义与 `.chart-empty` 样式；② 两图函数体内 `chartEmpty(` 调用出现在 `echarts.init` **之前**（按源码位置断言：`index('chartEmpty') < index('echarts.init')` 或按函数体切片断言），且 `echarts.init` 位于判定分支内/之后；③ 空数据响应下页面仍含 `id="sales-chart"` 与 `id="inventory-pie"` 两个容器（空态=容器内占位，不是容器消失）；④ 模板中不得出现「先 init 再判空」的写法（断言 `setOption` 所在函数体内存在前置 `return` 或 guard 片段）；⑤ 占位文案必须中文且含原因词（断言含「无」「暂无」与原因关键字之一） |

**状态谓词对拍（并入 P-1）**：测试内 import `app.external_orders._is_sale` 与
`app.cockpit.SALE_ORDER_STATUSES` 逐状态断言一致（防口径漂移，D3）。

---

## 7. UI 清单符合性（评审对照表）

| UI 清单条目 | 本片落实 | 验收口 |
|---|---|---|
| §1.1 三段式 | 顶卡 → 中图（左右）→ 底列表 | 页面结构可截图核对 |
| §1.2 指标卡四件套 | 6 卡 = 上限值；每卡主数字/标签/变化值/辅助信息 + 口径 `<details>` | 抽 1 卡与接口 JSON 对账一致 |
| §1.2「无数据源不上首屏」 | 达人漏斗不上；GMV 有源且标注口径 | 无源指标数 = 0 |
| §1.3 图表 5 规则 | 单库（echarts 本地）、语义色、Y 轴从 0、时间轴 `MM-DD`、空态灰占位（D8 硬红线）；图 2 张 | 空数据时显示 `.chart-empty` 中文占位，无空白坐标系（P-11） |
| §1.4 空态 5 场景 | 逐场景实现 + 中文错误 + 重试 | 可复现 |
| §1.5 token | 本页内定义，沿用既有色值 | 本页无新硬编码色（除 ECharts 需 JS 色值，抽一处常量） |
| §1.6 表格 | 数字右对齐 + 等宽 + 金额两位小数 + badge | 抽查底部 4 个列表 |
| §1.6 涨跌色 | **本期不定义**，环比用箭头+文字 | token 中无涨跌色 |

---

## 8. 执行与验证要求（实施方必读）

1. **开工前实跑基线**：`env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe -m pytest -q -p no:cacheprovider`
   必须 `475 passed / 0 failed`；不符即停手报告，不许带病开工。
2. **交付后必须给出**：定向（`tests/test_p4_cockpit.py tests/test_frontend_warehouse.py tests/test_api_dashboard.py`）
   与全量两条实跑结果，预期 **486 passed / 0 failed**。
3. **每个文件写完立即落盘**（R-55 教训：两次 claude -p 静默挂死，必须留可收口产物）。
4. **禁止**用 `monkeypatch` 改 `SALE_ORDER_STATUSES` 迁就测试数据；测试数据要造成真实状态。
5. **禁止**为了让页面「看起来有数据」而写死任何值（含示例 JSON、mock 常量）。
6. 完成后 `git commit`（前缀 `p4:`，**不 push**），并把交付说明写到 **`.testtmp/delivery.p4.md`**：
   提交号、定向/全量结果、`app/cockpit.py` 的 SELECT 清单（逐条列出查了哪些表）、
   `cockpit.html` 三段式结构片段、11 条用例与本文 §6 的对应关系、偏差清单（无则写"无"）；
   另需贴出：积压 reason 两处前端文案原文（须与 Q-P4-1 逐字一致）、`fx_todo` 字符串原文、
   **`build_cockpit` 的实际 SQL 条数（须 ≤8，逐条注明服务哪个指标）**。
7. **不要**跑 `git push`；**不要**动 `.env`；**不要**改 `migrations/`。

---

## 9. 已排除的选项（禁止重新试错）

1. **改造 `dashboard.html` 首屏** —— UI 清单已标暂缓 + 8 个 panel 断言会连带破（D1）。
2. **在 `/api/dashboard/summary` 上加字段** —— 旧接口契约冻结，`tests/test_api_dashboard.py` 断言其结构。
3. **用 `daily_sku_sales.order_count` 求和算订单数** —— 多 SKU 订单重复计（F-P4-1）。
4. **GMV 取 `daily_sku_sales.net_amount`** —— 本片统一取 `external_orders`，两源混用不可接受（F-P4-2）；
   若将来要做，必须单独验证两源差额并写进口径说明。
5. **为 demo 造订单数据／改 `app/demo_seed.py`** —— Q-P4-2 已裁定不补，属越界（demo 显示 0 与空态即正确）。
6. **新建全局 token 文件** —— 属 §1.5 独立改造项，本片不扩范围。
7. **读向量库统计切片数** —— 与 RAG 运行时耦合；DB 即真值（D5）。
8. **引入第二个图表库 / CDN 加载 ECharts** —— 违反 §1.3 规则 1，且离线环境不可用。
9. **默认把无 policy 的 SKU 判成缺货** —— 语义错误（F-P4-6）。
10. **前端做币种折算／显示跨币种合计** —— 无汇率源，折算即编造（Q-P4-3 已裁定分组返回、绝不折算）。
11. **空数据时让 ECharts 自己画空坐标系** —— 视觉上等同白屏/渲染失败；必须先判空再决定是否 init（D8）。
