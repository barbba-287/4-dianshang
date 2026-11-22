# dev_task.p10s3 —— 利润趋势窗口选择器（按天粒度 / 时间范围）+ 顺带修 O-7

- 版本：v1.0（2026-11-22）
- 基底：`1f60353`（HEAD = 远端，工作区干净，全量基线 **712 passed / 0 failed**）
- 性质：**前端为主 + 极小后端参数放宽 + 1 行测试护栏**。**零新迁移、零新依赖、不动 `profit_totals`**。
- 预期基线：712 → **≥ 724**（新增约 12 条；含一次既有测试的契约升级，见 §1.4）
- 范围来源：P10-S2 任务书 §8 明列「本片明确不做」的两项 —— 趋势的**按天粒度**、趋势的**时间范围选择器**；
  以及台账 §54 的 **O-7**（顺带修，不单开轮次）。

> **本任务书的口径由复核方裁定**。若「时间范围选择器」在你预期里是**起止日期输入框**
> 而不是下面的「预设窗口组合下拉」，请在开跑前否决本片 —— 那会是另一个形态（涉及时区、日界、
> `as_of` 钳制、日期合法性），不能靠改几个 option 完成。不要开跑后再改需求。

---

## §1 侦查结论：六个必须先知道的事实

### 1.1 ★ 「按天粒度」在后端**已经天然支持**，本片不是重写聚合

`app/finance.py:648` 的 `profit_trend()` 里，`days` 就是**每期天数**：

```python
window_to   = as_of - timedelta(days=index * days)
window_from = window_to - timedelta(days=days - 1)
```

`days=1` ⇒ 每期恰好 1 天 ⇒ **就是按天粒度**。`main.py:1935` 的 `Query(ge=1, le=90)` 也允许 `days=1`。

真正缺的只有三件事，全部在前端或参数上限上：

| # | 缺口 | 位置 |
|---|---|---|
| 1 | 趋势的 `days` **绑死到驾驶舱 `#days`**（选项只有 7/14/30），用户永远选不到 1 | `cockpit.html:69` `Number($('#days').value||7)` |
| 2 | `points` **写死 6** | `cockpit.html:69` `&points=6` |
| 3 | `points` 上限 **12** | `finance.py:645` + `main.py:1936` |

→ 与 P10-S1 同谱系：**后端能力已在那儿，前端从没用**。本片主体是「解绑 + 放宽上限 + 加组合护栏」，
**不是**新增聚合逻辑、不是分桶 SQL、不是新端点。

### 1.2 一次查询 / 一期，所以放宽上限必须配新预算

`profit_totals`（`finance.py:602`）docstring 明确「**1 条查询**」，`profit_trend` 逐期调它。
所以：

- `points=12` → 12 条聚合查询（P10-S2 定的预算）
- `points=31` → 31 条
- `points=90` → 90 条 ← **90 次 round-trip，不采纳**

P10-S2 §2 特意把趋势做成**独立端点**，理由就是「驾驶舱有 ≤9 条查询上限」。
本片放宽期数后这条理由仍然成立，**不要顺手把趋势并进 `/api/dashboard/cockpit`**。

### 1.3 ★ 现有 note 文案在解绑之后会变成**错的**

`cockpit.html:70` 的 `renderProfitTrend()` 现在写：

```
每期为 ${days} 天，与上方 KPI 卡「利润环比」同一口径；缺失期断开表示该期无可计算行，不等于利润为 0。
```

「同一口径」在 P10-S2 成立，是因为趋势的 `days` 就等于驾驶舱 `#days`。**解绑之后两者窗口不再相同**：
KPI 卡按 `#days`（7/14/30 天），趋势按独立选的窗口。**口径（同一个 `profit_totals`）仍相同，
窗口不再相同**。原文案会让财务用户以为图上每一期就是 KPI 卡的环比窗口 —— 这是误导，必须改（§3.2 M2-3）。

### 1.4 ★ 既有 T7 会**必红**，这是一次契约升级（不是实施方改坏了）

`tests/test_p10s2_profit_trend.py:219` 断言：

```python
"days=7&points=13&as_of=2026-09-10",   # 期望 422
```

把上限从 12 提到 31 后，`points=13` 会变成 **200**，这条必红。

**处理方式（写死，不许含糊）**：把 T7 的边界样例从 `points=13` 改成 **`points=32`**，
`points=0` / `days=0` / `days=91` 三条**保持不动**。
并在该文件里补一行注释说明「上限由 12 提到 31 是 P10-S3 的有意变更」。

> 这是**第三次**「数量快照式断言被合法新增打红」（前两次：P9-S2 的 T-23、P10-S2 的 T-20）。
> 接受判据不是「改个数字让它绿」，而是：**改后的断言仍在同一数量级上守住新的边界**（32 是 31 的越界值）。
> 台账里要登记这次升级，别让它看起来像偷偷放宽。

### 1.5 新增的组合护栏是**活代码**（与 O-6 不同）

台账 §53 的 O-6 说 `INVALID_TREND_PARAMS` 在 HTTP 路径上是死代码 —— 因为
`Query(ge/le)` 在参数校验层就拦掉了单参数越界。

但本片要加的 `points * days > MAX_SPAN` 是**跨参数组合约束**，FastAPI 的 `Query` 拦不住，
只有 `profit_trend()` 能拦 ⇒ **HTTP 路径能真实触发**，所以必须补一条检查 `body.detail.code` 的测试
（不能只断言 422，那会重蹈 O-6 的覆辙）。

### 1.6 已有可复用资产（别重复实现）

| 资产 | 位置 | 用途 |
|---|---|---|
| `as_of` 端点参数 | `main.py:1937` `Query(default=None)` | 已支持回放；**本片不暴露 UI**，仅保留 |
| `.pager-select` class | `cockpit.html` `<style>` 内 | 下拉样式，全用 `var()`，零裸色 |
| `.field` / `.panel-head` | `.panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:1rem;...}`（L2） | 头部布局 |
| `pager-select` 非法值回落先例 | P10-S1 `profit-line-size`（`size=""` / `"abc"` 回落 50） | 新控件的值解析照抄这个防御风格 |
| SQL 计数手法 | `tests/test_p9s1_profit_enhance.py:219-225` 的 `event.listen(engine,"before_cursor_execute",…)` | §5 S6 用 |

---

## §2 设计决策（我定的口径，附否掉的选项）

| # | 决策 | 否掉的选项与理由 |
|---|---|---|
| D1 | **一个下拉**「趋势窗口」，每个 option 的 value 携带 `(days,points)` 预设组合 | 两个独立下拉（粒度 + 范围）会产生非法组合（如「按天 × 90 天」= 90 期超预算），得再加钳制/禁用/提示，UI 复杂度陡增 |
| D2 | 期数上限 12 → **31**；新增总跨度上限 **90 天**（`points*days ≤ 90`） | 上限 90（90 次查询）；单条按日分桶 SQL（P10-S2 已否：`julianday()` 跨库 + 与 `_window_filters` 右开日界形成**第二套口径**） |
| D3 | 时间范围 = **预设的「每 N 天 × 共 M 期」组合**，不做起止日期输入 | 起止日期：时区 / 日界 / `as_of` 钳制 / 日期合法性 / 与右开日界第二套口径，工作量与风险都翻几倍 |
| D4 | **默认项保持 `days=7,points=6`（现状行为），一进页面零变化** | 默认切按天：一进页面就 30 条查询，且改变既有观感与 T8 契约 |
| D5 | 保持逐期 `profit_totals`、保持窗口推导、保持金额两位小数字符串 | 任何重写聚合 / 返回 float / 改 `profit_totals` 签名 |
| D6 | 只修 **O-7**；**O-6 继续挂账** | 顺手补 O-6：宽限后 `INVALID_TREND_PARAMS` 仍是死代码，价值低；本片新增的 `INVALID_TREND_SPAN` 有活代码测试，够用 |
| D7 | 本片 **不动 README** | P10-S2 §8 写「等趋势稳定后再写」；本片已有 5 个文件改动，范围不再扩 |

### D1 的五个预设（全部满足 `points*days ≤ 90`，文案与 meta 口径逐字对齐）

| `value` | 显示文案 | 总跨度 | 说明 |
|---|---|---|---|
| `7,6` | 每 7 天 · 共 6 期 | 42 天 | **默认（selected）**，等于现状 |
| `1,7` | 每 1 天 · 共 7 期 | 7 天 | 按天粒度 · 短窗口 |
| `1,30` | 每 1 天 · 共 30 期 | 30 天 | **本片主诉求：按天看一个月** |
| `7,12` | 每 7 天 · 共 12 期 | 84 天 | 沿用旧上限 12 的最长视图 |
| `30,3` | 每 30 天 · 共 3 期 | 90 天 | 最长跨度，恰好压在上限 |

> **文案不用「按周 / 按月」**：`days=7` 是「每 7 天」不是日历周（周一~周日），
> `days=30` 更不是自然月。财务产品里这两个词会引起口径误解。**一律写「每 N 天 · 共 M 期」**，
> 与 `renderProfitTrend` 的 meta（`${points.length} 期 · 每期 ${days} 天`）完全对齐。

---

## §3 改动清单

### M1 `app/finance.py` —— 放宽上限 + 组合护栏（约 6 行）

**M1-1** `:645`：

```python
PROFIT_TREND_MAX_POINTS = 12
```
→
```python
PROFIT_TREND_MAX_POINTS = 31
PROFIT_TREND_MAX_SPAN_DAYS = 90
```

**M1-2** `:692` 的校验块，在现有 `INVALID_TREND_PARAMS` 分支**之后**追加组合校验：

```python
if not workspace_id:
    raise ValueError("WORKSPACE_CONTEXT_REQUIRED")
if points < 1 or points > PROFIT_TREND_MAX_POINTS or days < 1 or days > 90:
    raise ValueError("INVALID_TREND_PARAMS")
if points * days > PROFIT_TREND_MAX_SPAN_DAYS:
    raise ValueError("INVALID_TREND_SPAN")
```

**M1-3** `profit_trend()` 的 docstring 与返回结构注释里凡提到「12」「最多 12 条聚合查询」的地方，
同步改成 31，并补一句总跨度说明。**其余逻辑一行不动**（`series` / `currencies` / `reversed` /
多币种分支 / `f"{value:.2f}"` 全部保持）。

**红线**：不许动 `profit_totals`、不许动 `for index in range(points)` 的窗口推导、
不许把 `profit` 改成 float、不许加 `computed` 字段（P10-S2 §2 裁定过不要）。

### M2 `app/main.py` —— 两处同步放宽 + 一条错误码（约 3 行）

**M2-1** `:1936`：`points: int = Query(default=6, ge=1, le=12)` → `le=31`。
> ⚠️ 改了 `finance.py` 的常量却忘了这里，`points=13` 仍会被 FastAPI 在参数层拦成 422，
> 组合护栏测试会拿不到预期的错误码。**两处必须同步。**

**M2-2** `:1809` 的错误文案：

```python
"INVALID_TREND_PARAMS": "趋势参数无效：days 需在 1~90、points 需在 1~12",
```
→
```python
"INVALID_TREND_PARAMS": "趋势参数无效：days 需在 1~90、points 需在 1~31",
"INVALID_TREND_SPAN": "趋势跨度无效：每期天数 × 期数不得超过 90 天",
```

**M2-3** 权限谓词、`as_of` 解析、`DomainError` 422 映射、**独立端点**这四件事**一字不动**。
端点 docstring 里「趋势最多 12 条聚合查询」改 31。

### M3 `app/templates/cockpit.html` —— 前端解绑（3 处）

> 模板是**长行内联**，`:23` 是 3338 字符的单物理行。
> **不许格式化、不许拆行、不许重排 DOM 顺序**（会打红 T-6/T-7/T-20 的字面计数断言）。

**M3-1** 趋势面板 DOM（`:23`）——在 `panel-head` 左侧 `<div>` 的 `<h2>` **之后**插入一个 `<label>`：

现状片段：

```html
<section class="panel" id="cockpit-profit-trend-panel"><div class="panel-head"><div><h2>利润趋势</h2></div><span class="tag" id="cockpit-profit-trend-meta">等待数据</span></div><div id="profit-trend-chart" class="chart"></div><p id="profit-trend-note" class="profit-note"></p></section>
```

目标片段（只改 `<div><h2>…` 那一处）：

```html
<section class="panel" id="cockpit-profit-trend-panel"><div class="panel-head"><div><h2>利润趋势</h2><label class="field">趋势窗口<select id="profit-trend-window" class="pager-select"><option value="7,6" selected>每 7 天 · 共 6 期</option><option value="1,7">每 1 天 · 共 7 期</option><option value="1,30">每 1 天 · 共 30 期</option><option value="7,12">每 7 天 · 共 12 期</option><option value="30,3">每 30 天 · 共 3 期</option></select></label></div><span class="tag" id="cockpit-profit-trend-meta">等待数据</span></div><div id="profit-trend-chart" class="chart"></div><p id="profit-trend-note" class="profit-note"></p></section>
```

- 复用既有 `.field` + `.pager-select`，**零 CSS 改动、零裸色值**。
- `.panel-head` 已是 `display:flex; align-items:flex-start`，左侧 div 内部 h2 + label 竖排即可，不需要新样式。
- `<span class="tag" id="cockpit-profit-trend-meta">` **必须原样保留**（`load()` 的 catch 与 `renderProfitTrend` 都写它）。

**M3-2** `loadProfitTrend()`（`:69`）——**解绑 `#days` 与写死的 `points=6`**。

现状（整行，逐字符核对）：

```js
async function loadProfitTrend(){const seq=++profitTrendSeq;try{const days=Number($('#days').value||7);const response=await fetch(`/api/finance/profit/trend?days=${encodeURIComponent(days)}&points=6`,{credentials:'same-origin'});if(!response.ok){const body=await response.json().catch(()=>({}));throw new Error(body.detail?.message||'利润趋势加载失败');}const data=await response.json();if(seq!==profitTrendSeq)return;renderProfitTrend(data);}catch(error){if(seq!==profitTrendSeq)return;if(profitTrendChart){profitTrendChart.dispose();profitTrendChart=null;}chartEmpty('#profit-trend-chart','利润趋势暂时不可用');const meta=$('#cockpit-profit-trend-meta');if(meta)meta.textContent='加载失败';}}
```

只改 `try{` 之后到 `fetch(...)` 之前的取参部分，改成：

```js
const sel=$('#profit-trend-window');const raw=sel?String(sel.value||'7,6').split(','):['7','6'];const days=Number(raw[0]),points=Number(raw[1]);const safeDays=Number.isFinite(days)&&days>=1&&days<=90?days:7;const safePoints=Number.isFinite(points)&&points>=1&&points<=31?points:6;const response=await fetch(`/api/finance/profit/trend?days=${encodeURIComponent(safeDays)}&points=${encodeURIComponent(safePoints)}`,{credentials:'same-origin'});
```

**不许动**：`const seq=++profitTrendSeq;`、两处 `if(seq!==profitTrendSeq)return;`、`catch` 里的
`dispose` / `chartEmpty` / meta 文案。**R-77 刚修好，这里是最容易顺手改坏的地方**（见 §4 陷阱 2）。

值解析的回落规则照抄 P10-S1 的 `profit-line-size`：控件缺失 / 值为空 / 非数字 / 越界 →
一律回落到 `days=7, points=6`（即默认项），**不发非法请求、不弹错**。

**M3-3** 事件绑定（`:71`）——在新控件上挂 `onchange`，**只调 `loadProfitTrend()`**：

在 `$('#days').onchange=load;` 之后追加：

```js
$('#profit-trend-window').onchange=()=>{void loadProfitTrend();};
```

⚠️ **绝不能写 `onchange=load`**：`#days` 的 `onchange=load` 会刷新整个驾驶舱（KPI、排行、
待办、明细全部重来），趋势窗口切换不该有这个副作用。`load()` 里本来就有 `void loadProfitTrend();`，
所以「刷新驾驶舱」路径不受影响，不需要改 `load()` 任何一行。

**M3-4** `renderProfitTrend()`（`:70`）的 note 文案 —— 修正 §1.3 的误导。

现状：

```js
if(note)note.textContent=`每期为 ${days} 天，与上方 KPI 卡「利润环比」同一口径；缺失期断开表示该期无可计算行，不等于利润为 0。`;
```

目标：

```js
if(note)note.textContent=`每期为 ${days} 天，共 ${points.length} 期；与上方 KPI 卡「利润环比」同一利润口径，但统计窗口独立（由上方「趋势窗口」选择）；缺失期断开表示该期无可计算行，不等于利润为 0。`;
```

meta 文案 `${points.length} 期 · 每期 ${days} 天` **保持不变**（T8 与探针都依赖它）。
`renderProfitTrend` 的**其余部分**（dispose / note / 多币种 / 空态 / `innerHTML=''` / `echarts.init` /
`connectNulls:false` / `animation:false` / 无 `areaStyle`）**一字不动**。

### M4 `tests/test_p10s2b_race_guard.py` —— 顺带修 O-7（**1 行**）

在 T-R77-3 里 `empty_at` 之后补一条：

```python
assert pos[1] < body.index("dispose()"), \
    "catch 里的守卫必须在 dispose 之前（否则会顺手把新请求建好的实例 dispose 掉）"
```

理由（台账 §54.4）：原断言只卡 `pos[1] < body.index("chartEmpty(")`，
而 `dispose()` 在 `chartEmpty()` **之前** ⇒ 「先 dispose 再守」的变异 4 条全绿。
实测危害：共享全局 `profitTrendChart` 被过期请求 dispose 掉，**有文字没图**。

同时把该测试的 docstring 第 ③ 条从「catch 里先 dispose 再守（图已经被清掉了）」改成
「catch 里先 dispose 再守（会 dispose 掉新请求刚建好的实例）」—— 原措辞不准确。

### M5 `tests/test_p10s2_profit_trend.py` —— T7 契约升级（**1 行 + 1 行注释**）

按 §1.4：`:219` 的 `"days=7&points=13&as_of=2026-09-10"` → `"days=7&points=32&as_of=2026-09-10"`，
并补注释说明这是 P10-S3 上限由 12→31 的有意变更。**其余三条参数样例不动**。

### M6 新建 `tests/test_p10s3_trend_window.py`

规格见 §5。

---

## §4 陷阱清单（每一条都踩过，别再踩）

1. **两处上限必须同步**：`finance.py:645` 常量 与 `main.py:1936` 的 `Query(le=...)`。
   只改一处 → `points=13` 仍被 FastAPI 拦成 422，S3 拿不到预期结果。
2. **R-77 守卫不许顺手重构**：§3 M3-2 只改取参那一段。两处 `if(seq!==profitTrendSeq)return;`
   一旦被挪动或合并，T-R77-1~4 全红（且 O-7 新补的 dispose 断言也会红）。
3. **`#days` 不许碰**：`onchange=load` 是驾驶舱全局刷新入口。新控件的 onchange 只能调
   `loadProfitTrend()`。改了 `load()` 会波及其余 8 个渲染函数。
4. **函数切片锚点必须带左括号**：`html.index("function load")` 会先命中 `loadProfitDetail`。
   写一 `"function "+name+"("`（`tests/test_p10s2b_race_guard.py:29-43` 的 `_fn_body` 是正确样板，直接 import 或照抄）。
5. **模板里 `params.set('sku_code',…)` 出现 2 次**、`URLSearchParams` 出现多次 —— 反证时
   **按函数体切片 + 按位置删**，不要 `count==1` 断言，不要全文 `in`。
6. **色值基线**：`<style>` 内 hex=0、`rgba(`=0；`<script>` 内色值合计 **8**（全站 T-7 恒为 22）；
   `echarts.init(` = **3** 且 `'wb')` = **3**（T-20）。本片**不新增任何图表实例与色值**，
   新控件只用既有 `.pager-select` / `.field`（全 `var()`）。改完自己按段抽取正则核一遍。
7. **还原自证别用裸 `md5sum`**：本仓 `core.autocrlf=true`。判据用
   `git hash-object <file>` == `git rev-parse HEAD:<path>`，或 `tr -d '\r' | md5sum` 对比（台账 §52.6）。
8. **反证与全量不能交叠**：全量跑动期间改源码，带 `importlib.reload` 的 fixture 会读盘。
   先跑完干净工作区的全量，再做变异反证。
9. **静态断言不算验证**：T8 之类只证明「字符串在模板里」。前端的取值、切窗、竞态必须用
   Node 探针**实跑**（见 §6），从模板原样抽函数 + DOM/ECharts stub，不许手抄重写。
10. **权限谓词逐字不变**：`analytics.read` + `roles=(ROLE_ADMIN, ROLE_OPERATIONS)` 双条件。
    readonly / customer_service 也有 `analytics.read`，漏 roles 就是利润后门。
11. **金额不落 float**：`f"{value:.2f}"` 保持；缺失期仍是 `None` 不是 `"0.00"`（T4 会验）。

---

## §5 测试用例规格：`tests/test_p10s3_trend_window.py`

用 `tests/test_p10s2_profit_trend.py` 的 `ctx` fixture 与 `_trend()` helper 同款写法。
`AS_OF = "2026-09-10"` 与既有造数保持一致。

### 后端（S1 ~ S7）

| # | 用例 | 断言要点 |
|---|---|---|
| S1 | 默认组合零回归 | `days=7&points=6` → 200、`len(points)==6`、`days==7`；最新期 `profit` == 直接调 `profit_totals(as_of-6d, as_of)` 的两位小数字符串（对拍，证明没重写聚合） |
| S2 | 按天粒度窗口正确 | `days=1&points=30` → 200、`len==30`；每期 `from == to`（恰好 1 天）；相邻期 `from == 上期 to + 1 天`；最新期 `to == as_of`；**按时间正序** |
| S3 | 新边界 | `days=1&points=31` → 200 且 `len==31`；`days=1&points=32` → **422**；`points=0` 仍 422 |
| S4 | ★ 组合护栏是活代码 | `days=90&points=2`（180 天）→ **422**。硬判据：`body["detail"]` **必须是 dict**（`isinstance(..., dict)`）且 `["code"] == "INVALID_TREND_SPAN"`、`["message"]` 含「90」。**只断言 422 不算**（重蹈 O-6）。<br>依据：`app/main.py:458-466` 的 `DomainError` handler 输出 `{"detail": {"code","message"}}`；而 `:450-455` 的 `RequestValidationError`（`Query` 越界）输出 `{"detail": [...]}`，是 **list**。所以若 `detail` 是 list ⇒ 请求在参数层就被拦了 ⇒ 组合护栏根本没执行，测试必须红 |
| S5 | 边界内侧 | `days=90&points=1`（恰好 90 天）→ 200、`len==1` |
| S6 | 查询预算 | **硬断言**：`monkeypatch` 一个 spy 包住 `app.finance.profit_totals`（**必须调用原函数**，不许替换成假数据，否则 S1/S2 的对拍会失真），跑 `days=1&points=31` 后断言 **调用次数 == 31**，且每次 `window_from == window_to`（按天粒度铁证）、31 个窗口首尾相接不重不漏。<br>**辅助证据**（不作硬断言）：用 `event.listen(engine,"before_cursor_execute",…)`（先例 `test_p9s1_profit_enhance.py:219-225`）统计总 SQL 条数并贴进 delivery。传显式 `as_of` 时 `resolve_sales_as_of` 不查库（`business_dates.py:24-25`），理论上是 31 条 + 固定鉴权开销；**别把总条数写成 `==31`**，中间件路径可能多几条。<br>这证明「放宽期数」没有引入 N+1 膨胀 |
| S7 | 权限不回退 | readonly / customer_service → **403**（与 T6 同款），admin / operations → 200 |

### 前端契约（F1 ~ F5，静态断言；F6 用 Node 探针）

| # | 用例 | 断言要点 |
|---|---|---|
| F1 | 新控件齐备 | `id="profit-trend-window"` 存在；5 个 option 的 `value` **集合精确等于** `{"7,6","1,7","1,30","7,12","30,3"}`；`value="7,6"` 带 `selected`（默认零变化） |
| F2 | 已解绑 | 按 `_fn_body` 切 `loadProfitTrend` 函数体：断言 `"$('#days')" not in body`、`"points=6" not in body`（精确字面，冒号写法 `:6` 不算）、`"profit-trend-window" in body`、`"points=${encodeURIComponent" in body` |
| F3 | onchange 只调趋势 | 用**精确切片**：`stmt = tpl[tpl.index("$('#profit-trend-window').onchange"):]` 再截到第一个 `;`（含）。断言 `"loadProfitTrend()" in stmt`、`"=load;" not in stmt`、`"=load " not in stmt`。<br>⚠️ **不要**切整行 `:71` —— 那一行同时有 `$('#days').onchange=load`，会把 `=load;` 带进来造成假红。也别用全文搜（`$("#days").onchange=load` 恒在） |
| F4 | note 口径已修正 | 按 `_fn_body` 切 `renderProfitTrend`：断言旧句 `"与上方 KPI 卡「利润环比」同一口径；"`（**带结尾分号**）**不在**函数体内；`"统计窗口独立"`、`"同一利润口径"` 均在。<br>带分号是为了不与新文案的「同一**利润**口径」混淆 |
| F5 | 护栏事实 | `<style>` 段 hex=0 / `rgba(`=0；`<script>` 段色值=**8**；`echarts.init(`=**3** 且 `'wb')`=**3**；`areaStyle` 全程仍为 **1**（只有 `renderSalesTrend` 原有那处） |
| F6 | **行为探针** | 见 §6 |

> F1 的「集合精确等于」用切片 + 正则抽取 `option value="..."`，别用全文 `in`。

### O-7 回归（M4 的验收）

新建一条或直接扩 `test_p10s2b_race_guard.py`：把 `catch` 里的守卫**挪到 `dispose()` 之后**，
T-R77-3 **必须变红**。这是 §54 那次落空的反证，本片补上断言后应当有效。

---

## §6 行为证据（写进 delivery，不许只贴代码）

写一个 `.testtmp/_p10s3_window_probe.js`（不入库），从模板**原样抽函数**
（`loadProfitTrend` / `renderProfitTrend`，带 `async` 前缀、大括号配对），
配 DOM / ECharts / fetch stub。场景与判据：

| # | 场景 | 期望 |
|---|---|---|
| P1 | 默认控件值 `7,6` | 请求 URL = `?days=7&points=6`（**与现状逐字一致**） |
| P2 | 切到 `1,30` | URL = `?days=1&points=30`；30 个点全渲染；meta = `30 期 · 每期 1 天` |
| P3 | 切到 `30,3` | URL = `?days=30&points=3`；note 含「统计窗口独立」 |
| P4 | 控件缺失 / `value=""` / `value="abc"` / `value="999,999"` | **全部回落** `days=7&points=6`，不抛异常、不发非法请求 |
| P5 | **连切两次**（A=`1,30` 慢 200ms 先发，B=`30,3` 快 10ms 后发） | 最终 meta = `3 期 · 每期 30 天`，`setOption` 次数 = **1** ⇒ R-77 守卫没有回归 |
| P6 | `load()` 路径不受影响 | `void loadProfitTrend()` 仍被调用一次；`#days` 的变化**不**直接触发趋势请求以外的副作用（探针里检查没有额外的 `fetch` 发往 `/api/dashboard/cockpit`） |
| P7 | 请求失败 | 不弹 toast、不写 `#message`、不碰 `cockpit-error`，趋势图走空态、meta「加载失败」（P10-S2 陷阱 7 不回退） |

**交付说明里贴完整输出**，并做一次**修复前 / 修复后对照**（用 `git show 1f60353:app/templates/cockpit.html`
取旧模板）：修复前 P2/P3 应恒定 `points=6`，修复后按控件变化 —— 差异只能来自本片改动。
> 探针若需要新增全局 stub（如 `profitTrendSeq`），先从模板核对声明再等值 stub，
> 别手写一份与模板漂移的变量（§54.2 的教训）。

### 必做反证（改源码 → 跑定向 → 还原 → `git hash-object` 自证）

| # | 变异 | 期望 |
|---|---|---|
| 反证 1 | 删掉 `if points * days > PROFIT_TREND_MAX_SPAN_DAYS:` | **S4 红**（且 S4 必须检查 `detail.code`，否则抓不到） |
| 反证 2 | `main.py` 的 `le=31` 改回 `le=12` | **S3 红**（证明两处同步的必要性） |
| 反证 3 | note 改回旧文案「同一口径」 | **F4 红** |
| 反证 4 | catch 守卫挪到 `dispose()` 之后 | **T-R77-3 红**（O-7 已补断言，这次必须有效） |

---

## §7 验收

| 项 | 要求 |
|---|---|
| 定向 | 新 `test_p10s3_trend_window.py` 全绿；`test_p10s2b_race_guard.py` 4 绿；`test_p10s2_profit_trend.py` 8 绿（含 T7 契约升级） |
| 全量 | **≥ 724 passed / 0 failed**（712 + 新增用例数，以实测为准并写明；若数量不同，逐条列出新增数，不许含糊） |
| 命令 | `env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe -m pytest -q -p no:cacheprovider` |
| 既有护栏 | `test_p5s1_tokens.py`（T-6/T-7）、`test_p5s3_visual_skeleton.py`（T-20）**全绿且零改动** |
| 反证 | §6 四条全部有效；还原后 `git hash-object` == `git rev-parse HEAD:<path>`，工作区干净 |
| 提交 | `git commit -m "..."` **必须带 -m**（台账 §52 O-3，已连着两片生效）；任务书随交付入库，**不要 push** |

---

## §8 本片明确**不做**的事

- **起止日期输入框**（时间范围用预设组合下拉，见 §2 D3 与开头裁定提示）
- **把趋势并入 `/api/dashboard/cockpit`**（≤9 条查询上限仍成立）
- **单条按日分桶 SQL / `julianday()`**（第二套口径，P10-S2 已否）
- **趋势导出**（P10-S2 §8 已否）
- **改写 `profit_totals`**、新增 `computed` 字段、金额落 float
- **改 `#days` 及其 `onchange=load`**、改 `load()` 函数体、改权限谓词
- **改 README**（P10-S2 §8 的约定保持；作为下一片候选）
- **O-6**（`INVALID_TREND_PARAMS` 死代码测试）、**O-5**、**O-1**、O-P5S1-1/3 —— 继续挂账
- **改历史任务书** `dev_task.p10s2-profit-trend.md`（里面写着 12，是历史事实，append-only 保留）

---

## §9 交付说明 `.testtmp/delivery.p10s3.md` 必填

1. 改动清单：文件 + 精确行号 + `git diff --stat`
2. §3 各 M 项的逐处确认（贴 `git diff` 片段，证明没有额外字符变动）
3. 全量实测通过数 + 耗时（贴 pytest 尾行）
4. 定向实测通过数（分文件列）
5. §6 Node 探针的**完整输出**（修复前 / 修复后对照）
6. §6 四条反证结果 + 还原自证（`git hash-object` 对比值）
7. **契约升级登记**：T7 的 `points=13 → 32` 理由，以及 `PROFIT_TREND_MAX_POINTS 12 → 31` 的新预算说明
8. 色值 / ECharts 实例计数自测值（F5）
9. 未做项 / 偏差（有就写，没有写「无」）
10. 自查：`git log --format="%h [%s]" -1` 的 subject **必须非空**

---

## §10 交接命令（git bash，逐字照此，我出命令后不代跑）

```bash
cd /d/ai/dianshang && claude --permission-mode acceptEdits "读 dev_task.p10s3-trend-window.md 全文，严格按其中每一条要求实施。完成后：1) 把交付说明写到 .testtmp/delivery.p10s3.md（含 §9 十项必填）；2) 自跑定向用例和全量回归，把实测通过数写进交付说明；3) 提交到当前分支（commit message 必须非空），不要 push。"
```

提交前记得把任务书一起 `git add`：

```bash
git add app/finance.py app/main.py app/templates/cockpit.html \
        tests/test_p10s3_trend_window.py tests/test_p10s2b_race_guard.py \
        tests/test_p10s2_profit_trend.py dev_task.p10s3-trend-window.md
git commit -m "feat(P10-S3): 利润趋势窗口选择器（按天粒度/时间范围）+ 组合跨度护栏 + 修 O-7"
```
