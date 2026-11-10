# P10-S2 利润趋势折线 + 导出跟随 SKU 筛选

> 版本 v1.0（2026-11-09）。起草方：WorkBuddy（复核方）。实施方：Claude Code。
> 基线：`700 passed / 0 failed`（`883e47b` + 台账 `bb4a124`，2026-11-09 已推送）。
> 分支：`feat/warehouse-mvp`。
> 定位：**一个中等切片，两个互不耦合的模块**。M1 纯前端零风险；M2 新增一个只读端点 + 一个图表面板。

---

## §0 一句话说清问题

**问题 1（M1，体验缺口 O-4）**：页面筛了 `SKU-A` 显示「共 80 行」，点「导出 Excel」得到的却是**全窗口 120 行**，界面上没有任何提示。后端导出端点**早就支持 `sku_code`**，前端从来没传过。

**问题 2（M2，功能缺口）**：驾驶舱 KPI 卡上有「利润环比 +12.3%」这**一个数字**，但没有任何随时间变化的曲线。用户看不到「利润这 6 个窗口是怎么走到今天的」——环比只回答「比上期如何」，不回答「趋势往上还是往下」。

**本片要的结果**：① 导出行为与屏幕一致；② 利润有一个 6 期趋势折线，口径与环比**逐字同源**。

---

## §1 事实基线（已实测，实现方**不必重新调研**，直接按此执行）

### 1.1 导出端点：能力完整，**后端一个字符都不用改**（M1 的核心事实）

`app/main.py:1865` `profit_detail_export(...)` 的签名：

```python
days: int = Query(default=7, ge=1, le=90),
as_of: date | None = Query(default=None),
currency: str | None = Query(default=None, max_length=8),
status: str = Query(default="all", max_length=16),
sku_code: str | None = Query(default=None, max_length=64),
export_format: Literal["xlsx", "csv"] = Query(default="xlsx", alias="format"),
```

`sku_code` 一路透传到 `app/finance.py:697`：

```python
if sku_code:
    filters.append(ProductSku.sku_code == sku_code)
```

**已实测**：该过滤真实生效（`export_profit_lines` 与 `list_profit_lines` 共用同一套 filter 构造）。
→ **M1 是纯前端改动，禁止碰 `app/main.py` / `app/finance.py`。**

### 1.2 前端导出现状：只传两个参数

`app/templates/cockpit.html` 的 `exportProfitLines`：

```js
const days=Number($('#days').value||7);
const response=await fetch(`/api/finance/profit/lines/export?days=${encodeURIComponent(days)}&format=${encodeURIComponent(format)}`,{credentials:'same-origin'});
```

`profitPage` 状态（P10-S1 已建）已有 `profitPage.sku`，**就是没被用上**。

### 1.3 利润环比：只有「本期 vs 上一期」两期，画不出趋势

`app/cockpit.py:490-510` 的取数链路：

```python
prev_profit_totals = profit_totals(db, workspace_id=..., window_from=prev_from, window_to=prev_to)
...
previous_profit = prev_profit_totals.get(profit_currency, Decimal("0"))
if previous_profit > 0:
    profit_change_ratio = _money_change(Decimal(profit["total_profit"]), previous_profit)
```

**只有 2 个数据点**（本期、上期），折线至少要 3 期才有意义。→ **必须新增多期取数端点，不存在"前端画一下"的捷径。**

### 1.4 现成的口径函数（M2 必须复用，不许重写）

```python
# app/finance.py:600
def profit_totals(db, *, workspace_id, window_from, window_to) -> dict[str, Decimal]:
    """按币种返回窗口内的预估利润合计（1 条查询，只含可计算行）。"""
```

三条**已写在 docstring 里**的关键语义，本片全部继承：

1. 只统计**可计算行**（`cost_status` 已锚定 + `unit_cost` / `fee_rate` 均非空）
2. 窗口内**无可计算行返回空 dict `{}`**，不是 `{"CNY": 0}` ——「没有行」与「利润是 0」是两件事
3. 口径与 `summarize_profit` 逐字同源（成本 = 单价 × 净数量、平台费 = 逐行 round 后汇总）

窗口谓词 `app/finance.py:130`：

```python
def _window_filters(workspace_id, window_from, window_to) -> list:
    """销售窗口谓词，与 cockpit 的 GMV 同一边界（UTC 日界，右开区间）。"""
    start_dt = datetime(window_from.year, window_from.month, window_from.day)
    end_dt = datetime(window_to.year, window_to.month, window_to.day) + timedelta(days=1)
    return [..., ExternalOrder.external_created_at >= start_dt,
                 ExternalOrder.external_created_at < end_dt]
```

### 1.5 窗口推导（驾驶舱既有算法，M2 必须照抄，不许自创）

`app/cockpit.py:157-160` 里本期 / 上期的算法（**逐字照抄**）：

```python
window_from = as_of - timedelta(days=days - 1)      # 含 as_of 当天
window_to   = as_of
prev_to     = as_of - timedelta(days=days)
prev_from   = prev_to - timedelta(days=days - 1)
```

即**每个窗口都是 `days` 天、右开区间、相邻窗口首尾相接不重叠**。第 i 期（i=0 为最新）：

```
to_i   = as_of - i * days
from_i = to_i - (days - 1)
```

`as_of` 必须走 `app/business_dates.py` 的 `resolve_sales_as_of(db, workspace_id=..., explicit_as_of=...)`——
**不许用 `date.today()`**（业务日期锚定是本项目反复踩过的口径坑）。

### 1.6 前端图表范式（既有，M2 照抄）

- 容器：`<div id="xxx-chart" class="chart"></div>`（`.chart` = `height:320px`）
- 初始化：`echarts.init(node,'wb')` —— `'wb'` 主题来自 `app/static/echarts-theme.js`，**系列色由主题提供**
- 空态：`chartEmpty('#xxx-chart','说明文案')`（定义在模板 `<script>` 前部，已存在）
- 重建前必须 `if(instance){instance.dispose();instance=null;}`
- `setOption({animation:false,...})` —— 全站图表都关动画

`cockpit.html` 顶部的色值对象（**这 6 个裸 hex 属于 T-7 的 22 处之一，只能引用、不能新增**）：

```js
const colors={blue:'#2a78d6',orange:'#d58b00',aqua:'#1baf7a',purple:'#8b5cf6',critical:'#d03b3b',ink:'#142033'}
```

### 1.7 两条样式铁律（违反即测试变红）

| 编号 | 铁律 | 守它的测试 |
|---|---|---|
| T-6 | 所有模板 `<style>` 内 **hex = 0 且 `rgba(` = 0**，只能用 `var(--…)` | `test_p5s1_tokens.py::test_t6_...` |
| T-7 | 10 个模板 `<script>` 内**色值合计 == 22**（cockpit 占 8） | `test_p5s1_tokens.py::test_t7_...` |

→ **新图表的 `lineStyle` / `itemStyle` 不许写任何裸 hex 或 `rgba(`**。
要么用 `colors.blue` 等（引用已有的，不新增计数），要么不给（让 `'wb'` 主题决定）。
**特别注意**：`areaStyle` 的 `rgba(...)` 也算色值，`renderSalesTrend` 里已经用了 2 处，
你**不能**再写第 3 处——否则 T-7 从 22 变 23，直接红。

### 1.8 `.chart-grid` 是 2 列，**不要往里塞第三个面板**

```css
.chart-grid{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(340px,1fr);gap:1rem}
@media(max-width:1100px){.chart-grid{grid-template-columns:1fr}}
```

`tests/test_p5s3_visual_skeleton.py:282` 断言 `.chart-grid{grid-template-columns` 这个字面串存在。
→ **新面板放在 `.chart-grid` 结束标签之后、`#cockpit-profit-panel` 之前**，独占整行。
这样 `chart-grid` 的 CSS 一动不用动，测试零风险。

### 1.9 测试范式：跨文件复用 fixture 是**既有约定**

`tests/test_p10s1_profit_paging.py:23-30` 已经有现成的复用写法：

```python
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
p7s3 = importlib.import_module("test_p7s3_profit_engine")
ctx = p7s3.ctx; _order = p7s3._order; _snapshot = p7s3._snapshot; _ingest = p7s3._ingest
```

**M2 照抄这个范式**，不另造造数脚手架。

---

## §2 三项设计决策（**已定，不再讨论**）

### 决策 1：多期取数用 **N 次 `profit_totals`**，不写分桶 SQL

被否方案：一条 SQL 按 `floor((window_to - external_created_at)/days)` 分桶。
否掉的理由：
- 需要 `julianday()`（SQLite 专有），MySQL 侧没有对应写法 —— 项目有 MySQL 迁移路径，不能埋方言雷
- 分桶表达式会与 `_window_filters` 的右开日界**产生第二套口径**，两套口径迟早漂移
- `profit_totals` 的 docstring 已经把口径写死，复用它是唯一能保证「趋势与环比同源」的方式

**代价与上限**：`points` 最大 **12**，即最多 12 条聚合查询。这是**独立端点**，
不并入 `/api/dashboard/cockpit`，因此不违反「驾驶舱 ≤ 9 条查询」的既有断言
（`test_p9s1_profit_enhance.py:199`）。

### 决策 2：多币种**不折算、直接不画**

与环比 `profit_change_basis = "多币种无法折算单一环比"` 保持同一立场
（`app/cockpit.py:498`）。多币种时端点返回 `points: []` + `reason: "多币种无法折算单一趋势"`，
前端显示空态。**禁止**把不同币种加总成一根线——那是在绘制一个不存在的数。

### 决策 3：M1 与 M2 **互不耦合，可分别独立验收**

M1 全部落在 `exportProfitLines` 一个函数内；M2 是新端点 + 新面板 + 新测试。
M2 若因故受阻（例如趋势口径需要产品再确认），**M1 仍可单独交付**，不影响基线推进。

---

## §3 改动清单

### M1 `cockpit.html` 的 `exportProfitLines` —— 导出跟随 SKU 筛选（**约 4 行**）

现状（**逐字**）：

```js
async function exportProfitLines(format){try{const days=Number($('#days').value||7);const response=await fetch(`/api/finance/profit/lines/export?days=${encodeURIComponent(days)}&format=${encodeURIComponent(format)}`,{credentials:'same-origin'});if(!response.ok){const body=await response.json().catch(()=>({}));throw new Error(body.detail?.message||'导出失败');}const blob=await response.blob();const disposition=response.headers.get('Content-Disposition')||'';const hit=/filename="([^"]+)"/.exec(disposition);const link=document.createElement('a');const url=URL.createObjectURL(blob);link.href=url;link.download=hit?hit[1]:`profit-lines.${format}`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);const count=response.headers.get('X-Export-Row-Count');if(response.headers.get('X-Export-Truncated')==='1'){toast.warning(`已导出前 ${count} 行，请缩小时间窗口或筛选条件`);}else{toast.success(`利润明细已开始下载（${count} 行）`);}}catch(error){toast.error(`导出失败：${error.message||'网络错误'}`);}}
```

改成（**要点，不是逐字照抄，按此语义实现**）：

1. 用 `URLSearchParams` 拼参（**不许手工拼字符串**，SKU 可能含 `/`、`&`、中文）：
   ```js
   const params=new URLSearchParams({days:String(days),format:String(format)});
   if(profitPage.sku)params.set('sku_code',profitPage.sku);
   ```
2. **成功 toast 必须说明导出范围**（这是 M1 的核心价值，不只是修 bug）：
   - 有筛选且未截断：`已导出「${profitPage.sku}」的全部 ${count} 行（不受分页限制）`
   - 无筛选且未截断：`已导出全窗口 ${count} 行（不受分页限制）`
   - 截断时（`X-Export-Truncated==='1'`）保留现有警告，但把范围也带上：
     `已导出${profitPage.sku?`「${profitPage.sku}」`:'全窗口'}的前 ${count} 行，请缩小时间窗口或筛选条件`
3. **不要**改 `renderProfitDetail` 里那句 `全窗口合计见上方利润卡` —— 那句说的是**合计**（`page_profit` 口径），
   与导出无关，仍然正确。

**一个既有能力，别重复实现**：`main.py:1915-1916` 已经有

```python
if sku_code:
    filename_base += f"-{_safe_filename_part(sku_code)}"
```

→ 前端一传 `sku_code`，导出的文件名就自动变成 `profit-lines-20261029-20261104-SKU-A.xlsx`。
**不需要你在前端拼文件名**，也不要改这段后端逻辑。用户从文件名就能看出导出的是哪个 SKU。

**M1 同时要改的一处文案**：`renderProfitDetail` 的 note（P10-S1 写的）：

```
本页可计算行利润合计 X（共 N 行）；全窗口合计见上方利润卡，切换页码不会改变它。
```

在**有 SKU 筛选时**追加一句：`当前筛选：${sku}；导出将只包含该 SKU。`
位置：note 文案的末尾。**无筛选时不追加**（不留空话）。
→ 目的：让用户在点导出之前就知道导出范围，而不是靠 toast 事后发现。

### M2-1 `app/finance.py` —— 新增 `profit_trend()`

插在 `profit_totals`（`:600-644`）与 `_export_cost_status_label`（`:645`）**之间**，
即紧接 `profit_totals` 之后，**不改动 `profit_totals` 的任何一行**。

```python
PROFIT_TREND_MAX_POINTS = 12


def profit_trend(
    db: Session,
    *,
    workspace_id: int,
    as_of: date,
    days: int,
    points: int,
) -> dict:
    """最近 ``points`` 个等长窗口的利润合计序列（每窗口 1 条查询）。

    窗口语义与驾驶舱本期/上期**逐字一致**（见 app/cockpit.py 的推导）：
    第 i 期（i=0 最新）= [as_of - i*days - (days-1), as_of - i*days]，UTC 日界右开。

    返回::

        {
          "as_of": "2026-11-09",
          "days": 7,
          "points": [
             {"from": "2026-10-29", "to": "2026-11-04", "label": "11-04",
              "profit": "123.45", "computed": 0, "has_data": true},
             ...
          ],
          "currency": "CNY" | None,
          "multi_currency": false,
          "reason": None | "多币种无法折算单一趋势",
        }

    ``points`` 数组**按时间正序**（最早在前、最新在后），前端 x 轴直接用。
    """
```

实现要点（**每一条都要落实**）：

1. **参数钳制，一个错误码**：
   ```python
   if points < 1 or points > PROFIT_TREND_MAX_POINTS or days < 1 or days > 90:
       raise ValueError("INVALID_TREND_PARAMS")
   ```

   > **不要复用 `INVALID_PAGINATION`**。它在 `_PROFIT_ERROR_MESSAGES`（`main.py:1805`）里的文案是
   > 「分页参数无效：limit 需在 1~200、offset 需 ≥ 0」——趋势端点**根本没有** `limit`/`offset`，
   > 复用会让用户看到一条描述不存在参数的错误。
2. **workspace 校验**：`if not workspace_id: raise ValueError("WORKSPACE_CONTEXT_REQUIRED")`
3. **逐期调用 `profit_totals`**，每个窗口一次，**不做单条拼接 SQL**：

   ```python
   for i in range(points):
       to_i = as_of - timedelta(days=i * days)
       from_i = to_i - timedelta(days=days - 1)
       totals = profit_totals(db, workspace_id=workspace_id,
                              window_from=from_i, window_to=to_i)
   ```

4. **缺失期必须是 `None`，不是 `"0.00"`**：
   `profit_totals` 返回 `{}` 时该期 `profit = None`、`has_data = False`。
   理由与 `profit_totals` 自己的 docstring 一致——「没有行」和「利润是 0」不是一回事。
   折线因此会**断开**（`connectNulls:false`），这正是我们要的视觉语义。
5. **多币种判定**：汇总所有期中出现过的币种集合。
   - 空集合（全期无数据）→ `currency = None`、`multi_currency = False`、`points` 照常返回（全 `None`）
   - 恰好 1 个 → `currency = 该币种`、`multi_currency = False`，每期取该币种的值（该期没有 → `None`）
   - ≥2 个 → `multi_currency = True`、`reason = "多币种无法折算单一趋势"`、**`points = []`**
6. **`computed` 字段**：本期可计算行数。`profit_totals` 不返回行数——
   **本片不为此新增查询**。直接给 `0` 并在 docstring 里写明「保留字段，暂不填充」，
   **或者干脆不要这个字段**。→ **决定：不要 `computed` 字段**，少一个没说法的数字。
7. **金额用 `_money()` 同款两位小数字符串**：`profit_totals` 返回的是 `Decimal`，
   序列化前统一 `f"{value:.2f}"`（参考 `app/finance.py` 里 `_cent` / 明细端点的处理方式）。
   **不许返回 float** —— 金额落 float 是本项目明令禁止的。

### M2-2 `app/main.py` —— 新增 `GET /api/finance/profit/trend`

放在 `profit_detail_export`（`:1865`）**之前或之后均可**，但必须紧邻利润相关端点。

```python
@app.get("/api/finance/profit/trend")
def profit_trend_endpoint(
    request: Request,
    days: int = Query(default=7, ge=1, le=90),
    points: int = Query(default=6, ge=1, le=12),
    as_of: date | None = Query(default=None),
    db: Session = Depends(get_db),
):
```

**权限谓词必须与 `/api/finance/profit/lines` 逐字一致**：

```python
principal = require_principal(request, permission="analytics.read",
                              roles=(ROLE_ADMIN, ROLE_OPERATIONS))
```

> 这是既有红线（`main.py:1876-1884` 的注释已经写死）：`readonly` 与 `customer_service`
> 的权限集里**都有** `analytics.read`，只写 `permission` 等于开一个「页面挡住、接口漏出」
> 的利润后门，全靠 `roles` 拦住。**照抄，不要自己简化。**

`as_of` 走 `resolve_sales_as_of`（与明细端点同一行写法）。`ValueError` → `DomainError` 422，
错误码映射照抄明细端点的 `_PROFIT_ERROR_MESSAGES`（`:1805`）用法，
**新增一条**映射：`INVALID_TREND_PARAMS` → 「趋势参数无效：days 需在 1~90、points 需在 1~12」。
（只往那个 dict 里加一条，不删不改已有的三条。）

### M2-3 `cockpit.html` —— 新增「利润趋势」面板

**位置**：`<div class="chart-grid">…</div>` 的**闭合之后**、`<section class="panel" id="cockpit-profit-panel">` **之前**。

```html
<section class="panel" id="cockpit-profit-trend-panel"><div class="panel-head"><div><h2>利润趋势</h2></div><span class="tag" id="cockpit-profit-trend-meta">等待数据</span></div><div id="profit-trend-chart" class="chart"></div><p id="profit-trend-note" class="profit-note"></p></section>
```

**新增函数 `loadProfitTrend()` + `renderProfitTrend(data)`**（命名与前两个图表函数对齐）：

- `loadProfitTrend()`：`fetch('/api/finance/profit/trend?days=…&points=6')`，
  **`try/catch` 必须吞掉错误**——趋势图失败**不许**影响驾驶舱主流程（`load()` 已经成功渲染的部分不能被它带崩）
- 失败时的表现：`chartEmpty('#profit-trend-chart','利润趋势暂时不可用')` + meta 显示「加载失败」，
  **不弹 toast、不写 `#message`、不碰 `cockpit-error`**
- `renderProfitTrend(data)`：
  - `data.multi_currency` 为真 → `chartEmpty(...,'多币种无法折算单一趋势（与利润环比同一口径）')`，
    meta 显示「多币种」
  - 全期无数据（`points.every(p=>p.profit===null)`）→ `chartEmpty(...,'所选窗口内没有可计算利润的订单行')`
  - 正常：单折线，`xAxis.data = points.map(p=>p.label)`，`series.data = points.map(p=>p.profit===null?null:Number(p.profit))`，
    `connectNulls:false`、`symbol:'circle'`、`symbolSize:7`、`animation:false`
  - **`lineStyle` / `itemStyle` 只准写 `color:colors.blue`**（引用已有的，不新增色值计数）
  - **不准写 `areaStyle`**（`renderSalesTrend` 已用掉 2 处 `rgba(`，再写就 T-7 变红）
  - note 文案（`#profit-trend-note`）必须包含这句限定：
    `每期为 ${days} 天，与上方 KPI 卡「利润环比」同一口径；缺失期断开表示该期无可计算行，不等于利润为 0。`
- **调用时机**：`load()` 成功渲染完其余部分之后，`void loadProfitTrend();`（不 `await`）。
  `#days` 变更时 `load()` 会重跑 → 趋势自动跟随窗口。
- **不要**把趋势并进 `/api/dashboard/cockpit`（见 §2 决策 1）。

### M2-4 新增样式（只在必要时）

`.chart` 已有 320px 高度，面板复用 `.panel`，**理论上零新增样式**。
若确实需要（例如 note 的上边距），**只能加在 `cockpit.html` 的 `<style>` 段，且全部 `var(--…)`，零 hex、零 `rgba(`**。

### M3 `tests/test_p10s2_profit_trend.py` —— 新建

沿用 §1.9 的复用范式。建议 **8 条**（数量可上下浮动 1 条，但**这 8 个行为各必须有一条**）：

| 编号 | 用例 | 断言要点 |
|---|---|---|
| T1 | 端点基本结构 | 200；`points` 长度 == 请求的 `points`；按时间**正序**；每项含 `from/to/label/profit` |
| T2 | 期边界正确 | 相邻两期的 `to` 与 `from` 相差 1 天（首尾相接、不重叠）；最新一期 `to == as_of` |
| T3 | 口径与环比同源 | 构造某期数据 → 该期 `profit` **等于** `profit_totals` 对同一窗口的返回值（**直接对拍，不手算**） |
| T4 | 缺失期为 null 不是 0 | 只在最新期造数据 → 其余期 `profit is None`、`has_data is False`；**不许出现 `"0.00"`** |
| T5 | 多币种不折算 | 两个币种各有数据 → `multi_currency is True`、`points == []`、`reason` 非空 |
| T6 | 权限与明细端点一致 | 只读角色（readonly / customer_service）调用 → **403**（与 `/api/finance/profit/lines` 同判据） |
| T7 | 参数钳制 | `points=0` / `points=13` / `days=0` / `days=91` → **422**，不得 500 |
| T8 | 前端模板契约 | 含 `id="profit-trend-chart"`、`id="cockpit-profit-trend-panel"`、`loadProfitTrend`；**且**（锁 M1）**`exportProfitLines` 函数体内**含 `sku_code` 与 `URLSearchParams` |

> **T8 必须按函数体切片断言，不许全文 `in`**：模板里 `loadProfitDetail`（P10-S1 写的）
> 早就含 `URLSearchParams`，全文搜 `"URLSearchParams" in template` **恒为真、抓不到任何东西**。
> 正确写法：
>
> ```python
> export_fn = template.split("async function exportProfitLines")[1].split("async function load()")[0]
> assert "sku_code" in export_fn
> assert "URLSearchParams" in export_fn
> ```
>
> 切片的两个锚点都存在于模板中（`exportProfitLines` 与 `load` 都是顶层函数声明）。
> **已实测基线**：该切片长度 934 字符，当前 `'sku_code' in fn` 与 `'URLSearchParams' in fn`
> **均为 `False`** —— 因为 M1 还没做。**你做完 M1 后两者必须变 `True`**。
> 若你一开始运行 T8 就是绿的，说明切片错了，别当成"通过"。
> **顺带反证一次**：把 `if(profitPage.sku)params.set('sku_code',profitPage.sku)` 注释掉，
> T8 必须变红——不变红说明切片切错了。

**T6 是最重要的一条**——利润数据外泄是安全事故，不是体验问题。

---

## §4 关键陷阱（**逐条对照，都不是直觉能想到的**）

| # | 陷阱 | 后果 | 正确做法 |
|---|---|---|---|
| 1 | 用 `date.today()` 当 `as_of` | 业务日期锚定失效，测试与生产口径分裂（本项目踩过多次） | 走 `resolve_sales_as_of` |
| 2 | 缺失期填 `"0.00"` | 折线在没数据的日子扎到 0，视觉上等于「那天亏光了」，且导出/汇总若复用会污染 | 填 `None` + `has_data:false` |
| 3 | 多币种加总成一根线 | 绘制一个不存在的数字（1 USD + 7 CNY） | `multi_currency=true` → 不画 |
| 4 | 新图表写 `areaStyle:{color:'rgba(...)'}` | **T-7 从 22 变 23，直接红** | 不写 areaStyle；颜色用 `colors.blue` |
| 5 | 新样式写裸 hex | **T-6 直接红** | 只用 `var(--…)` |
| 6 | 把趋势塞进 `.chart-grid` 当第三列 | 2 列网格挤成 3 列；`test_p5s3_visual_skeleton.py:282` 的字面断言可能连带受影响 | 放在 `chart-grid` **外面**，独占整行 |
| 7 | 趋势失败时 `throw` 冒泡到 `load()` 的 catch | 整页显示「加载失败」，其余图表全被清空 | `loadProfitTrend` 内部 `try/catch` 全吞，只降级自己那块 |
| 8 | 导出手工拼 `&sku_code=${sku}` | SKU 含 `&` / `/` / 中文时参数被截断，导出了错误的行 | `URLSearchParams` |
| 9 | 顺手把趋势并进 `/api/dashboard/cockpit` | 驾驶舱查询数从 9 涨到 15+，`test_p9s1_profit_enhance.py:199` 的 ≤9 断言红 | 独立端点 |
| 10 | 自己写一遍成本/平台费/利润的聚合 | 与 `profit_totals` 口径漂移，某天两边对不上没人知道哪个对 | 只调 `profit_totals`，逐期一次 |
| 11 | 导出改完忘了 toast 仍是「全窗口」 | 用户筛了 SKU 却被告知导出全窗口，反而更困惑 | toast 文案按是否筛选分支（见 M1 要点 2） |
| 12 | 在 `renderProfitDetail` 里把「全窗口合计见上方利润卡」改掉 | 那句说的是 `page_profit` 口径，与导出无关，改了反而是错 | 保留原句，只**追加**筛选提示 |

---

## §5 测试与验收

### 5.1 定向

```bash
env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe \
  -m pytest -q -p no:cacheprovider tests/test_p10s2_profit_trend.py
```

### 5.2 既有护栏必须仍绿（本片最容易碰红的五条）

```bash
env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe \
  -m pytest -q -p no:cacheprovider \
  tests/test_p5s1_tokens.py tests/test_p5s3_visual_skeleton.py \
  tests/test_p4_cockpit.py tests/test_p9s1_profit_enhance.py \
  tests/test_p7s3_profit_engine.py tests/test_p10s1_profit_paging.py
```

逐条盯：

| 护栏 | 盯什么 |
|---|---|
| `test_t6_no_hardcoded_color_in_template_styles` | 新增样式零 hex、零 `rgba(` |
| `test_t7_script_blocks_unchanged` | **仍 == 22**（cockpit 仍 8） |
| `test_p9s1_profit_enhance.py:199` | 驾驶舱 SELECT **仍 ≤ 9 条**（趋势没并进 cockpit 就不会动） |
| `test_p5s3_visual_skeleton.py:282` | `.chart-grid{grid-template-columns` 字面串仍在 |
| `test_p10s1_profit_paging.py` Q1~Q7 | M1 改了 `exportProfitLines`，Q1/Q2 里有 `renderProfitDetail` / `bindProfitExport` 相关断言，**不许连带改绿** |

### 5.3 全量

```bash
env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe \
  -m pytest -p no:cacheprovider
```

**基线 700。预期 ≥ 708**（M3 的 8 条 + 零回归）。若出现 `test_api_jobs` 失败，
按台账 §51 O-1 处理：**单跑 + 整文件跑各一次**，都通过即判为已知 flaky，如实写进 delivery。

### 5.4 delivery 必填项（缺一条视为未交付）

1. 改动文件清单 + 精确行号
2. **`profit_trend` 的实测返回样例**（一个真实 JSON，含至少一期 `profit:null`）
3. **T3 的对拍证据**：某期趋势值与 `profit_totals` 同窗口返回值逐值相等（贴出两个数）
4. **T6 的实测**：只读角色调趋势端点的状态码与响应体
5. 全量通过数 + 耗时，剥垫片命令原文
6. 未做项与偏差逐条
7. **若 M2 有任一项没做到，必须显式说明，不要把 M1 的完成算成整片完成**

---

## §6 红线

| # | 红线 |
|---|---|
| 1 | **不改 `profit_totals` 的任何一行**（它被环比依赖，改了等于同时改驾驶舱 KPI） |
| 2 | **不改 `app/cockpit.py`**（本片不碰驾驶舱主查询，趋势是独立端点） |
| 3 | **不把趋势并进 `/api/dashboard/cockpit`** |
| 4 | 权限谓词与 `/api/finance/profit/lines` **逐字一致**（`analytics.read` + roles admin/operations） |
| 5 | `<style>` 零 hex / 零 `rgba(`；`<script>` 内色值合计**仍 == 22** |
| 6 | 金额**不落 float**，一律 Decimal → 两位小数字符串 |
| 7 | 缺失期是 `None`，**不是 `"0.00"`** |
| 8 | 多币种不折算、不画 |
| 9 | 零迁移、零新依赖 |
| 10 | `app/db.py`、`migrations/` 不动 |
| 11 | 不动除 `cockpit.html` 外的其余 9 个模板 |
| 12 | **提交必须带 `-m "..."` 的完整 subject**（台账 §52 O-3：空 commit 已发生 3 次） |

---

## §7 交接命令

```bash
cd /d/ai/dianshang && claude --permission-mode acceptEdits "读 dev_task.p10s2-profit-trend.md 全文，严格按其中每一条要求实施。完成后：1) 把交付说明写到 .testtmp/delivery.p10s2.md（含 §5.4 七项必填）；2) 自跑定向用例和全量回归，把实测通过数写进交付说明；3) 提交到当前分支（commit message 必须非空），不要 push。"
```

---

## §8 本片明确**不做**的事

- 趋势的**按天**粒度（只做等长窗口聚合，7/14/30 天一拍）
- 趋势的**时间范围选择器**（固定 6 期，跟随 `#days`）
- 趋势的**导出**（利润明细导出已有，趋势不单独导出）
- 明细页的**订单号搜索**、**排序**、**虚拟滚动**
- 导出跟随 `status` / `currency` 筛选（UI 上没有这两个选择器；做了就是给不存在的控件配参数）
- `profit_totals` 增加"返回可计算行数"的能力（那会改变被环比依赖的函数签名）
- O-5（排序兜底键的测试守卫）—— 需要 PG 或字面断言，价值低，继续挂账
- README 补充趋势端点说明 —— **本片不动 README**（等趋势稳定后再写，避免又一次写错）
