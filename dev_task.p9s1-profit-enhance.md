# P9-S1 利润中心补完：环比 + 明细全量导出 + SKU 切片

> 状态：v1.0 定稿待实施
> 立项依据：用户 2026-11-08 裁定「下一步走方向 A」
> 基线：`650 passed / 0 failed`（2026-11-06 实测），head 迁移 `0025_cost_snapshots`，分支 `feat/warehouse-mvp`

---

## §0 范围

| 项 | 做 | 不做 |
|---|---|---|
| 利润环比 | 后端算上一等长窗口的利润，填 `change_ratio` / `change_basis` | 不做利润趋势折线图（`sales_trend` 已有销售口径，利润趋势另立切片） |
| 明细导出 | 新增导出端点，复用 `import_exports.build_download` | 不做异步导出任务、不做导出历史 |
| SKU 切片 | `list_profit_lines` 增 `sku_code` 过滤 | 不做按日/周/月的时间粒度聚合 |
| 权限 | 与既有利润端点**完全一致** | 不为 readonly / customer_service 开放利润（用户 2026-11-06 裁定维持现状） |
| 利润口径 | 一个字不改 | 不改 `FORMULA`、不改 `summarize_profit` 的金额算法、不加运费/仓储成本 |

**本片零新迁移、零新表、零 schema 变更**（`CockpitResponse.profit` 与 KPI 段都是 `dict`，加字段不触发响应模型）。

---

## §1 侦查结论（施工面，行号基于当前 HEAD）

### §1.1 可直接复用的现成基建

| 能力 | 位置 | 说明 |
|---|---|---|
| 上一等长窗口 | `app/cockpit.py:137-139` | `prev_to = as_of - timedelta(days=days)`、`prev_from = prev_to - timedelta(days=days - 1)`。**紧邻且等长**，直接用，不要重算 |
| 环比字段占位 | `app/cockpit.py:751-752` | 利润 KPI 里 `"change_ratio": None, "change_basis": None` —— P7-S3 已预留，**只需填值** |
| 环比渲染 | `app/templates/cockpit.html:31`（`renderKpis`） | 已有 `card.change_ratio !== undefined && card.change_ratio !== null` 判断 → 走 `formatChange`。**前端零改动** |
| 环比文案 | `cockpit.html:29`（`formatChange`） | 已处理正/负/持平；`null` 分支文案是「上一窗口无基数，无法计算环比」，但当前永远走不到（见 §2.1 的处理） |
| 环比公式范式 | `app/cockpit.py:87`（`_change`） | `(cur − prev) / prev`，`prev == 0` 返回 `None`。**只接受 int**，利润是 Decimal，需另立 |
| 导出通用出口 | `app/import_exports.py:188`（`build_download`） | xlsx（表头加粗+浅底、全表居中、列宽自适应、首行冻结）/ csv（UTF-8 BOM） |
| 导出前端范式 | `app/templates/sources.html:55`（`exportRun`） | blob 下载 + `Content-Disposition` 取文件名 + toast 提示，照抄 |
| 导出按钮样式 | `sources.html` 内 `.export-btn` | 已有，cockpit 需自行补一条 |
| 明细筛选骨架 | `app/finance.py:454`（`list_profit_lines`） | 已有 `currency` / `status` / `limit` / `offset` 与 `total` 计数，**加 `sku_code` 是增量** |
| 金额格式化 | `app/finance.py:86`（`_money`） | Decimal → `"12.34"` 字符串，导出与响应统一用它，别用 `str()` |

### §1.2 三个必须避开的坑

**坑 1：负利润基数算环比会给出反向结论 —— 这是财务报表最常见的误导**

设上期利润 −100、当期利润 +50（从亏转盈）：

```
(50 − (−100)) / (−100) = −150%   → 前端显示「↓ 环比下降 150.0%」
```

实际情况是**扭亏为盈**，显示出来却是「下降 150%」。同理上期 −100 → 当期 −50（亏损收窄一半）会算成 `(−50 −(−100)) / (−100) = −50%`，同样显示「下降」，而这是好转。

`_change` 现在只处理 `prev == 0`，**没有处理 `prev < 0`**（现有 KPI 都在用 int 的非负量，所以从没暴露）。利润环比必须显式加上负基数分支，见 §2.1。

**坑 2：多币种的利润环比没有单一数值**

利润 KPI 在多币种时 `value = null`（`cockpit.py:747` 附近注释写明「把不同币种加在一起没有经济意义」）。环比同理：不能拿「人民币利润环比 +12%」代表「美元利润环比 −30%」。多币种时 `change_ratio` 必须为 `None`。

注意 GMV 的处理范式（`cockpit.py:163-168`）是 `if single_currency:` 才计算 `gmv_change`，直接沿用，不要自己发明。

**坑 3：导出权限谓词必须逐字复制，否则形成权限后门**

利润明细端点的权限是**双条件**（`app/main.py:1831`）：

```python
principal = require_principal(request, permission="analytics.read", roles=(ROLE_ADMIN, ROLE_OPERATIONS))
```

只看 `permission` 是不够的 —— `ROLE_READONLY` 和 `ROLE_CUSTOMER_SERVICE` 的权限集里**都有 `analytics.read`**（`app/identity.py:39,42`），能过权限检查，全靠 `roles` 参数拦住。

如果导出端点漏写 `roles=(ROLE_ADMIN, ROLE_OPERATIONS)`，只读成员就能把利润全量导出带走 —— 而用户在页面上依然看不到利润。这是「页面挡住、接口漏出」的典型形态，必须用测试锁死（F-45）。

---

## §2 需求与设计决策

### §2.1 R1 利润环比

#### 取数方式：新增轻量聚合，不重复调用 `summarize_profit`

**不要**为了拿上期总额再调一次 `summarize_profit`。理由：

- `summarize_profit` 是 **2 条查询**（`F-24b` 硬锁：1 条 `GROUP BY (currency, sku, sku_code, cost_status)` + 1 条 missing 订单去重计数）；
- 调两次就是 +2 条，驾驶舱查询从 8 → 10；
- 环比只关心**每币种的利润合计**，不需要 by_currency 的完整结构、不需要 ranking、不需要 missing 计数（missing 行利润恒为 0，不进金额汇总）。

**做法**：在 `app/finance.py` 新增

```python
def profit_totals(
    db: Session,
    *,
    workspace_id: int,
    window_from: date,
    window_to: date,
) -> dict[str, Decimal]:
    """按币种返回窗口内的预估利润合计（1 条查询，只含可计算行）。"""
```

要求：

- 复用 `_window_filters`、`_qty_expression`、`_revenue_expression`、`net_qty` 的计算口径，**与 `summarize_profit` 逐字同源**（成本 `unit_cost × 净数量`、平台费 `round(实付净额 × fee_rate, 2)` 逐行舍入后再 SUM，利润 = 实付净额 − 成本 − 平台费）；
- 只统计 `COMPUTABLE_STATUSES` 且 `unit_cost` / `fee_rate` 均非空的行；
- `GROUP BY currency`，返回 `{currency: Decimal}`；窗口内无可计算行时返回 `{}`（空 dict，不是 `{"CNY": 0}`）；
- 空 workspace_id 抛 `ValueError("WORKSPACE_CONTEXT_REQUIRED")`（与既有函数一致）；
- **绝不写库**（`F-24` 的只读纪律测试会扫 `finance.py` 全文，出现 `db.add(` / `db.commit(` 等即红）。

#### 环比计算规则（写死在 `cockpit.py`）

在 `cockpit.py` 新增（与 `_change` 并列，不改 `_change` —— 它被 GMV/订单数在用，动它风险外溢）：

```python
def _money_change(current: Decimal, previous: Decimal) -> float | None:
    """金额类环比：(cur − prev) / prev。基数 ≤ 0 时返回 None。"""
```

判定表（**这是本片最容易写错的地方，逐条对照**）：

| 上期利润 | 当期利润 | `change_ratio` | `change_basis` | 前端表现 |
|---|---|---|---|---|
| `> 0` | 任意 | `(cur − prev) / prev`（float） | `"上一等长窗口"` | `↑ 环比上升 X%` / `↓ 环比下降 X%` / `— 环比持平` |
| `== 0` | 任意 | `None` | `"上一窗口利润为 0，无基数"` | 不显示环比行 |
| `< 0` | 任意 | `None` | `"上一窗口利润为负，环比无经济意义"` | 不显示环比行 |
| 多币种 | — | `None` | `"多币种无法折算单一环比"` | 不显示环比行 |
| 当期不可计算 | — | `None` | 同 KPI 的 `reason` | 不显示环比行 |

**为什么 `prev < 0` 直接放弃而不是用 `abs(prev)` 做分母**：用绝对值虽然能让「−100 → +50」显示为 `+150%`（方向正确了），但「−100 → −50」会变成 `+50%`，读起来像「增长了 50%」，而实际是**亏损收窄 50%**，语义又错一次。符号不一致的基数**没有任何一种比值能同时正确表达方向与幅度**，正确做法是不给百分比，让 `change_basis` 说明原因。这条写死，不许「优化」。

#### 查询预算

- 当期 `summarize_profit`：2 条（不变）
- 上期 `profit_totals`：1 条（新增）
- 驾驶舱 SELECT 总数：**8 → 9**

必须新增守卫测试（F-43）锁住「≤ 9」，否则后续任何人在驾驶舱里加聚合都会无声膨胀。

#### 输出契约

`CockpitResponse` 的 `kpis.estimated_profit` 段（`app/cockpit.py:744-758`）：

- `change_ratio`：float | None（按上表）
- `change_basis`：str | None（按上表）
- 其余字段（`value` / `unit` / `quality` / `reason` / `note` / `multi_currency`）**一个字不改**

`profit` 段（`cockpit.py:788`，即 `summarize_profit` 的返回值）**不新增字段** —— 环比属于 KPI 卡片层，不属于利润汇总本体。

### §2.2 R2 明细全量导出

#### 端点

```
GET /api/finance/profit/lines/export
```

参数与既有明细端点**完全对齐**（`main.py:1812-1830`）：`days`（1..90）、`as_of`、`currency`、`status`、`sku_code`（本片新增），外加 `format`（`xlsx` 默认 / `csv`）。

**不带 `limit` / `offset`** —— 导出的语义就是全量。带分页参数会让人以为能分片导出，实际每次都从头拉，反而更差。

权限：**逐字复制** `require_principal(request, permission="analytics.read", roles=(ROLE_ADMIN, ROLE_OPERATIONS))`。

#### 护栏

| 护栏 | 值 | 行为 |
|---|---|---|
| 导出行数上限 | `PROFIT_EXPORT_MAX_ROWS = 20000` | 超过即截断，响应头 `X-Export-Truncated: 1`；未超不带该头 |
| 截断提示 | — | 前端 toast：`已导出前 20000 行，请缩小时间窗口或筛选条件` |
| 空结果 | — | **仍然出表头 + 0 数据行**（200，不是 404/422）。导出空表是合法需求（「我确认这窗口没数据」） |
| 文件名 | `profit-lines-YYYYMMDD-YYYYMMDD[-{currency}][-{sku_code}].xlsx` | 让用户一眼知道导的是哪一段 |
| 缓存 | `Cache-Control: no-store` | 由 `build_download` 自带 |

#### 列定义（**顺序写死，测试会对齐**）

| # | 表头 | 取值 |
|---|---|---|
| 1 | 订单号 | `external_order_no` |
| 2 | 下单时间 | `paid_at` 或 `external_created_at`（与明细响应同源） |
| 3 | SKU | `sku_code`，为空显示 `external_sku`，再空显示 `—` |
| 4 | 币种 | `currency` |
| 5 | 净数量 | `net_qty` |
| 6 | 实付净额 | `net_revenue` |
| 7 | 成本 | `cost`（不可计算时留空字符串，**不填 0**） |
| 8 | 平台费 | `platform_fee`（同上） |
| 9 | 利润 | `profit`（同上） |
| 10 | 成本状态 | `cost_status` 中文（已锚定 / 缺成本 / 未映射 SKU / 缺费率） |

**第 7-9 列不可计算时留空**，理由与前端一致：`0` 会被读成「这单不赚钱」，空 + 状态列才是「不知道」。导出文件会被拿去算总和，填 0 直接污染数据。

#### 实现落点

- `app/finance.py` 新增 `export_profit_lines(db, *, workspace_id, window_from, window_to, currency=None, status="all", sku_code=None, max_rows=PROFIT_EXPORT_MAX_ROWS) -> tuple[list[str], list[list[str]], bool]`，返回 `(headers, rows, truncated)`；
- 内部**一条查询 + 一次 `.limit(max_rows + 1)`**：多取 1 行用于判断是否截断，然后 `rows[:max_rows]`；
- 行金额复用 `line_amounts`（`finance.py:411`），**不要另写一套计算** —— 两套迟早漂移；
- `app/main.py` 新增端点，内部调 `import_exports.build_download`；
- 响应头：`X-Export-Row-Count`（实际导出行数）、`X-Export-Truncated`（仅截断时）。

### §2.3 R3 SKU 切片

`list_profit_lines` 增 `sku_code: str | None = None`：

- 过滤条件走 `ExternalOrderLine.internal_sku_id` 关联 `Product.sku_code`（**内部编码**，与 `/orders?sku=` 的预填一致），不要用 `external_sku`；
- 找不到该 SKU 时返回空结果（不是 422）—— 与 SKU 被删、拼写错误的处理一致，让用户自己看空表；
- `total` 必须是过滤后的数量（现有实现是 `select(func.count(...)).where(*filters)`，加进 `filters` 即自动正确，别另算）。

导出也要同步支持 `sku_code`（同一个参数）。

### §2.4 前端

改 `app/templates/cockpit.html` 三处：

1. **明细面板头部加导出按钮**：`利润明细` 标题右侧，`<button class="export-btn">导出 Excel</button>` + `<button class="export-btn import-ghost">导出 CSV</button>`；
2. **补 `.export-btn` 样式**：本模板没有这条样式，从 `sources.html` 复制（`padding:.3rem .55rem;font-size:.75rem;border-radius:.4rem`），**不要新建色值**，全用 `var(--…)`；
3. **下载逻辑**：照抄 `sources.html:55` 的 `exportRun`（blob + `Content-Disposition` 解析 + `URL.revokeObjectURL`），失败走 `toast.error`；截断时读 `X-Export-Truncated` 给 warning toast。

**环比不需要改前端** —— `renderKpis` 已经会渲染 `change_ratio`。但要注意：现有 `formatChange` 的 `null` 分支文案（「上一窗口无基数，无法计算环比」）永远走不到，因为判断是 `!== null` 才渲染。**保持现状，不要改** —— 一旦改成「ratio 为 null 但 basis 有值时也渲染」，会让另外 6 个 KPI（`change_ratio` 与 `change_basis` 都是 None）全部冒出一行「无法计算环比」，属于回归。

`change_basis` 只在 API 层输出，供前端日后展示与排查，本片不渲染。

---

## §3 验收闸（F-39 ~ F-50）

测试文件：`tests/test_p9s1_profit_enhance.py`（新建）。夹具直接复用 `tests/test_p7s3_profit_engine.py` 的 `ctx`（含 `sku_a` / `_snapshot` / `_ingest` / `_order` / cockpit 客户端），不要另造一套。

| 闸 | 场景 | 判定 |
|---|---|---|
| **F-39** | 上期利润 40.00、当期 50.00（成本与售价造两期数据） | `change_ratio == 0.25`（误差 1e-9）；`change_basis == "上一等长窗口"` |
| **F-40** | 上期利润 −40.00、当期 +50.00（扭亏） | `change_ratio is None`；`change_basis` 含「为负」；**并断言不等于 −2.25**（若实现成 `(50−(−40))/(−40)` 会得到 −2.25，这条反向锁死） |
| **F-40b** | 上期 −40.00、当期 −20.00（亏损收窄） | `change_ratio is None`（同样不得算成 +50%） |
| **F-41** | 上期无任何订单（利润 0）、当期有 | `change_ratio is None`；`change_basis` 含「无基数」 |
| **F-42** | 同时存在 CNY 与 USD 订单 | `estimated_profit.multi_currency is True`；`value is None`；`change_ratio is None`；`by_currency` 两组都在 |
| **F-43** | 计数驾驶舱全部 SELECT（`before_cursor_execute`，照抄 `test_p7s3_profit_engine.py:493` 的写法） | `≤ 9`。同时断言 `profit_totals` 自身 `== 1` |
| **F-44** | 造 250 行可计算明细（同窗口） | 导出 xlsx 成功；`X-Export-Row-Count == 250`；**断言明细端点 `limit=200` 时 `total == 250` 但 `items` 只有 200**（证明导出确实突破了页面上限） |
| **F-45** | `readonly` 角色调导出端点 | **403**（与明细端点同谓词）。同时断言 `customer_service` 也 403，`operations` 200 |
| **F-46** | `format=csv` 导出 | `media_type` 含 `text/csv`；body 以 `\xef\xbb\xbf` 开头（UTF-8 BOM，国内 Excel 直开不乱码） |
| **F-47** | 空窗口导出（无任何订单） | 200；`X-Export-Row-Count == 0`；xlsx 可用 openpyxl 打开且**第 1 行是 10 个表头** |
| **F-48** | `max_rows` 调小（如 monkeypatch 成 5）后导出 8 行 | `X-Export-Truncated == "1"`；`X-Export-Row-Count == 5` |
| **F-49** | 两个 SKU 各有订单，`sku_code=SKU-A` | `total` 只算 A 的行；返回项里 `sku_code` 全为 `SKU-A`；导出同样只含 A |
| **F-50** | 环比窗口边界 | 用 `days=7` 断言：当期 `[as_of-6, as_of]`、上期 `[as_of-13, as_of-7]`，**两窗口不重叠、无缝隙**（构造边界日订单，分别落在两端，验证只被计入各自窗口一次） |
| **F-51** | 导出不可计算行的金额列 | 造一行 `cost_status=missing`，导出后第 7-9 列为**空字符串**，第 10 列为「缺成本」；断言 `!= "0"` 且 `!= "0.00"` |

**必须同时保持全绿（回归红线）**：

- `test_p7s3_profit_engine.py` 全部 20 个用例（尤其 `test_f24b_summarize_profit_uses_two_queries`，证明 `summarize_profit` 没有被我改成 3 条）；
- `test_p4_cockpit.py` 全部（KPI 段结构不能变）；
- `test_p8s1_order_merge.py` 全部 29 个（利润口径回归）；
- `test_p5s2_tables_and_empty.py`（**不要写 `.num{text-align:right}`** —— 全站左对齐是 P5-S2 的裁定，加了这条字面串会红）。

---

## §4 已排除的选项（不要自行采用）

| 选项 | 为什么排除 |
|---|---|
| 复用 `_change` 传 Decimal 或 float | 它被 GMV / 订单数在用，改签名会牵连 6 个 KPI；另立 `_money_change` 更安全 |
| 负基数用 `abs(prev)` 做分母 | 「−100 → −50」会显示成 `+50%`（读作增长），语义错；见 §2.1 判定表下方说明 |
| 环比调两次 `summarize_profit` | +2 条查询，且 70% 的计算结果被丢掉；`profit_totals` 只要 1 条 |
| 导出带 `limit` / `offset` | 导出的语义是全量；带分页会产生「导出第 2 页」这种没人用的功能 |
| 导出不可计算行时填 0 | 导出文件会被拿去 SUM，0 会把「不知道」污染成「不赚钱」（F-51 锁死） |
| 为导出单开一条 `catalog.read` 权限 | 会让只读成员拿到利润，与用户 2026-11-06 的裁定冲突 |
| 在 `summarize_profit` 里加 `prev_*` 参数 | 会让这个 2 查询函数变成 3 查询，`F-24b` 直接红 |
| 改 `formatChange` 的 null 分支 | 会让另外 6 个 KPI 冒出「无法计算环比」的噪音行 |
| 新建 CSS token 或硬编码色值 | 会撞 `test_p5s1_tokens.py`；沿用 `sources.html` 的 `.export-btn` |
| 用 `str(Decimal)` 输出金额 | 会出 `"18.5"` 这种一位小数，导出与响应不一致；一律用 `finance._money` |
| 引入 pandas / openpyxl 之外的 Excel 库 | `requirements` 已有 openpyxl，新增依赖会让部署面变大 |

---

## §5 交付要求

1. 改动文件：`app/finance.py`、`app/cockpit.py`、`app/main.py`、`app/templates/cockpit.html`、`tests/test_p9s1_profit_enhance.py`（新建）；
2. 交付说明写到 `.testtmp/delivery.p9s1.md`：改动文件清单 + 行号、未做项、偏差（有偏差必须写原因，不许静默）；
3. **自跑**定向用例（`tests/test_p9s1_profit_enhance.py`）与全量回归，把**实测通过数**写进交付说明。全量命令必须剥垫片：

```bash
env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe -m pytest -q -p no:cacheprovider
```

4. 台账追加 §40（本片小节，含 F-39~F-51 清单与实测数字）；
5. 提交到 `feat/warehouse-mvp`，**不 push**。

---

## §6 预估工作量

| 部分 | 文件 | 规模 |
|---|---|---|
| `profit_totals` | `finance.py` | ~55 行 |
| `export_profit_lines` + `sku_code` 过滤 | `finance.py` | ~70 行 |
| `_money_change` + KPI 填充 + 上期调用 | `cockpit.py` | ~30 行 |
| 导出端点 | `main.py` | ~45 行 |
| 前端按钮 + 下载逻辑 | `cockpit.html` | ~25 行（单行内联写法） |
| 测试 | 新建文件 | ~420 行 / 13 用例 |

一次性可交付，无需拆片。
