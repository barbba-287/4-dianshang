# 任务文件：P5-S2 表格可读性 + 全局空态/加载态

> 切片：P5 第二阶段（UI 界面美化）｜性质：**纯前端**，零后端逻辑改动
> 依据：`docs/ui-reference-checklist.md` v5 §1.6（表格可读性）、§1.4（空态与加载态）、§1.5（token 已就位）、§2（不可借 4 条）、§2b（本期不动的页面）
> 基线：**494 passed / 0 failed**（P5-S1 提交 `0df5dc9` 后；复核方 2026-11-01 在提交态独立自跑 **291.36s** 实测确认）→ 本片目标 **502 passed / 0 failed**
> 状态：**v1.1（复核方已审核：修正 5 处事实错误 + 3 处设计缺口；6 条裁定待用户拍板，拍板前不开工）**
> 修订记录：v0.1 草案 → v1.1 复核修订（见 §12）

---

## 0. 结论摘要（先看这 12 条）

1. **目标不是"重做表格"，是"补齐 + 统一"。** 现状（复核方实测，口径见 §2.1）：`<table>` 标签共 **23 处**——**9 个静态 HTML 表** + **14 个在 `<script>` 内生成**，另有 `assistant.html` 的 `makeTable()` **DOM 工厂 1 个（不产生 `<table>` 字面量）**；**数字列右对齐只有 2 处实现**（`cockpit.html:4` 的 `.num`、`dashboard.html:5` 的 `.quadrant-num`），其余全部左对齐。
2. **空态目前有 7 种形态并存**：`.empty`、`<td colspan class="muted">`、`<td colspan class="error">`、`<div class="muted">`、裸文本、`innerHTML=''`、`.assistant-empty`、`.chart-empty`。`colspan` 实测取值 **5/7/9/10 四种**（**无 4**，v0.1 误写为五种）→ 本片收敛为**一个组件**（§1.4 结论："空态不是从零补，而是把已有的分散文案收敛成一个组件"）。
3. **空态样式有真实缺陷**：`.cs-error`（`customer_service.html:93` 使用）**在任何 CSS 里都没有定义** → 该错误文案当前无任何样式；`orders.html:6` 的「暂无行项目」**无 class**；`content.html:7` 的「暂无历史任务」是裸文本。
4. **加载态无整页遮罩**（全站零 overlay），唯一"遮挡可操作区域"的是 `dashboard.html:31` / `cockpit.html:31` 的 `showSkeleton()`（覆盖 KPI 区 + 2 张图表，仅首载触发）。
5. **6 处表格首屏完全空白**（无占位行）：`index.html:97`（库存）、`index.html:105`（补货/采购）、`orders.html:4` 的 `#orders`、`customer_service.html:68/80` 两个 tbody、`cockpit.html` 四个 `.table-wrap`。
6. **§1.6 的"呼吸感"与"行高紧凑"直接冲突**，本片裁定：**呼吸感只落在容器（panel/card/gap/圆角），表格行高维持紧凑 36–40px**。加表格 padding 会突破 40px 上限并减少单屏行数 → 归入 §2「不可借」。
7. **§1.6 要求的"金额固定两位小数"无法靠 CSS 实现**，必须改前端 JS 格式化（`toFixed(2)`）。这是**展示层格式化，不改接口返回结构**，但会改变页面显示值 → 列入本片范围并单独设守卫（G8）。
8. **涨跌色：本期不定义**（§1.6 超期规则）。表格只用 `success / warning / danger` 表达状态，不用颜色表达涨跌。token 中不新增涨跌色变量，且需在交付说明中标注"本期不定义"。
9. **禁区（§2b）**：`/admin/users`、`/roles`、`/shopify` 接入配置页、设置类页面、日志/同步运行记录页、业务表单结构、接口返回结构、**表格列增减、筛选器增减**。→ 本片**不改 `admin_users.html` 与 `shopify.html`**。注意 `/shopify` 空态示例文案里提到的"去 Shopify 同步"是**指引链接指向该页**，不是改该页。
10. **最大测试风险是 `tests/test_p5s1_tokens.py`**：模板 `<script>` 内色值合计必须 **== 24**（T-7）、模板 `<style>` 内 hex/rgba 必须 **== 0**（T-6）、`var()` 零悬空（T-2）、模板数 **== 10**（T-4）。→ 本片**禁止在 `<script>` 里写任何颜色字面量**（badge 一律走 class，不走 inline style），**禁止在模板 `<style>` 里写 hex/rgba**（一律 `var(--color-*)`）。
11. **字符串 marker 雷区**（改动文案会直接红）：`test_frontend_customer_service.py:28` 的 marker `只有需要跟进` 唯一命中工单空态文案；`test_frontend_orders.py:31` 的 `待人工处理`/`待发（外部已支付）` 命中订单卡片标签；`test_admin_users.py:220` 的 `class="member-table"`；`test_p4_cockpit.py:759-760` 的 `无订单事实`/`无库存策略`。
12. **本片只做"看得见"的三件事**：① 数字列右对齐 + 等宽 + 金额两位小数；② 状态文字 → Badge；③ 空态/加载态统一组件 + 斑马纹。**不加列、不加筛选器、不改接口、不改业务表单。**

---

## 1. 背景与目标

P5-S1 把 180 处硬编码色值收敛进 `app/static/css/tokens.css`，建立单一真源，并让 8 个此前变量悬空的页面的边框/配色**第一次真正生效**（该页视觉变化最明显）。但 P5-S1 的任务边界明确写着："表格可读性零改动"——那是留给本片的。

本片把 UI 清单 §1.6（表格可读性）与 §1.4（空态与加载态）落地到**具体页面**，目标是**肉眼可见的提升**：用户在 `/orders`、`/ops`、`/dashboard` 上扫一列金额时，数字列能对齐、金额能对齐小数位、状态是一枚可扫读的胶囊标签；列表没数据时看到的是一段说明 + 一个下一步动作，而不是一行灰字或整块白。

**不做什么（防范围蔓延）**：
- 不加表格列、不加筛选器、不改任何接口返回结构、不改业务表单结构（§2b 硬约束）。
- 不定义涨跌色（§1.6 超期规则）。
- 不重排 `/dashboard` 首屏、不做图表规范（§1.1/§1.3 属后续切片）。
- 不做骨架屏大规模铺开（只补"首屏空白表格"的最小占位，不做全站骨架重写）。
- 不改 `/admin/users`、`/shopify`、设置类页、日志/同步运行记录页。

---

## 2. 现状事实（Fact，全部实测，附复核方法）

> 取证方式：模板为长行内联写法，`wc -l` 无意义；下文行号指**模板文件行号**（这些文件虽为长行，但 `<style>`/DOM/JS 均在少数行内，行号可用），CSS 用**字节偏移**。所有事实已由实施方逐条 grep/Read 核对，复核命令附于各节。

### 2.1 表格版图（实测口径：`<table>` 标签 23 处 = 静态 9 + `<script>` 内 14；另有 DOM 工厂 1）

> **口径统一（v1.1 修正）**：v0.1 的"静态列数 `<table>` 标签、JS 列数调用点"两种口径混用，导致总数无法核对（明细表相加为 30，而全仓 `<table>` 只有 23 个）。本节统一为**「`<table>` 标签出现次数」**，按「是否位于 `<script>` 区间内」二分；`assistant.html` 的 `makeTable()` 用 DOM API 建表、**不产生 `<table>` 字面量**，单列一行说明。

| 页面（路由） | 静态 `<table>` | `<script>` 内 `<table>` | 数字列现状 |
|---|---|---|---|
| `/ops`（`index.html`） | 3（`:51` 跨境事实、`:97` 库存、`:105` 补货/采购） | 0（tbody 由 `:367`/`:372`/`:462`/`:513` 填充） | **全部左对齐** |
| `/dashboard` | 1（`:7` SKU 库存健康） | **6**（四象限 `:19`、数据不足 `:21`、同步健康 `:32`、外部事实 `:38`、告警 `:39`、入库 `:39`） | 仅四象限明细用 `.quadrant-num` 右对齐；其余**左对齐** |
| `/cockpit` | **0** | 4（`:26`/`:27`/`:28`/`:29`） | **已用 `.num` 右对齐** ✅ |
| `/orders` | 1（`:4` 主表 9 列） | 1（`:6` 详情行项目表 7 列） | **左对齐** |
| `/warehouse` | 0 | 1（`:18` 入库明细） | 继承 shell，**左对齐** |
| `/customer-service` | 2（`:68` 工单、`:80` 知识文档） | **0** | **左对齐** |
| `/content` | 0 | 1（`:7` 历史任务） | 版本号内嵌在文本里，**非独立数字列** |
| `/assistant` | **0 字面量** | **0 字面量** | **无对齐机制**：`makeTable()` 用 `document.createElement` 建表，`th.className` 赋值（见 §2.9） |
| `/admin/users` | 2（`:24`/`:25`） | 0 | 本片**禁区**，不动 |
| `/shopify` | 0 | **1**（`:2` `table()` 工厂） | 本片**禁区**，不动 |
| **合计** | **9** | **14** | **23** |

> 复核命令（可直接复现本表）：
> ```bash
> python -c "
> import re,pathlib
> for p in sorted(pathlib.Path('app/templates').glob('*.html')):
>     s=p.read_text(encoding='utf-8')
>     js=[(m.start(),m.end()) for m in re.finditer(r'<script[^>]*>(.*?)</script>',s,re.S)]
>     st=jn=0
>     for m in re.finditer(r'<table',s):
>         (jn:=jn+1) if any(a<=m.start()<b for a,b in js) else (st:=st+1)
>     print(p.name, 'static=',st,'js=',jn)
> "
> grep -n "text-align" app/templates/*.html app/static/workspace-shell.css
> ```
> 注意：`grep -c "<table"` 数的是**行数**（`dashboard.html` 单行内含 2 个），会得到 22 而非 23，不得用于本表核对。

### 2.2 全局表格样式现状（`app/static/workspace-shell.css`，单行文件，字节偏移）

```
偏移 4527  .table-wrap{overflow-x:auto}
偏移 4555  table{width:100%;min-width:760px;border-collapse:collapse}
偏移 4613  th,td{padding:.68rem .6rem;border-bottom:1px solid var(--shell-line);text-align:left;vertical-align:top;font-size:.84rem}
偏移 4734  th{color:var(--shell-muted);font-size:.75rem}
偏移 4779  .tag{display:inline-flex;align-items:center;gap:.25rem;padding:.2rem .48rem;border-radius:999px;background:#edf2f7;font-size:.72rem}
偏移 4911  .empty{padding:1.2rem;color:var(--shell-muted);text-align:center;border:1px dashed #cad6e5;border-radius:.5rem}
```

- **shell 中没有** `.num`、`.skeleton`、`tbody tr:hover`、`thead` 单独样式、任何斑马纹规则、任何 `td` 右对齐规则。
- `th,td` 的 `text-align:left` 是**全局默认左对齐**——这正是"数字列不右对齐"的根因。
- **`.num` 只在 `cockpit.html:4` 一处页内定义**（`.num{text-align:right;white-space:nowrap}`）。`dashboard.html` 里**没有 `.num`**，只有 `.quadrant-num`，且其 `<style>` 从 **`:5`** 开始。
  → v0.1 写的"`dashboard.html:4` 与 `cockpit.html:4` 两处重复"**不成立**：真正重复的只有 cockpit 一处；"两处重复"的论据作废，但"在 shell 建全局 `.num` 并删除 cockpit 页内那份"的结论仍然有效。
- **`.error` 是硬编码**：`app/static/workspace-shell.css:1` 中为 `.error{color:#a72727}`——**不是** `var(--color-danger-strong)`。
  该值与 `tokens.css:51` 的 `--color-danger-strong:#a72727` **恰好同值**，所以"改用 `.error`"视觉零变化；但**不得**顺手把它改成 `var()`（违反 G6「shell 既有规则一个字符都不改」）。

### 2.3 空态现状（7 种形态并存）

| 形态 | 出现位置（示例） |
|---|---|
| `<div class="empty">…</div>` | `dashboard.html:21/23/32/33/38/39`、`cockpit.html:26/27/28/29`、`index.html:294/303` |
| `<tr><td colspan="N" class="muted">…</td></tr>` | `orders.html:5`（colspan=9）、`customer_service.html:92`（10）/`:103`（5）、`admin_users.html:39/40`（5）、`index.html:374/461/509`（5/5/7） |
| `<tr><td colspan="N" class="error">…</td></tr>` | `orders.html:7`（9）、`index.html:516`（7） |
| `<div class="muted">…</div>` | `shopify.html:2`（`table()` 工厂空分支） |
| 裸文本（无类名） | `content.html:7`（「暂无历史任务」）、`orders.html:6`（「暂无行项目」，连 `<td>` 都没有 class） |
| `.assistant-empty` | `assistant.html:59/91-96` |
| `.chart-empty` | `cockpit.html`/`dashboard.html` 的 `chartEmpty()` |

**样式缺失**：
- `.cs-error`（`customer_service.html:93` 的「加载失败：…」）**全仓无定义** → 当前无任何样式。
- `.empty` 只有一个视觉形态，**无法表达"标题 + 说明 + 操作引导"三层**。

### 2.4 加载态现状

- **全站零整页遮罩**（grep 无 overlay/mask/backdrop 类）。
- 唯一"覆盖可操作区域"：`dashboard.html:31` / `cockpit.html:31` 的 `showSkeleton()`——首载时把 `#kpi-grid`（6 卡）+ `#sales-chart` + `#inventory-pie` 整体替换为 `.skeleton`；**刷新时不触发**（`firstLoad` 标志）。
- `.chart-refreshing` **只在 `cockpit.html:4` 定义**（`grep -rn "chart-refreshing" app/` 全仓仅此 1 处命中），`dashboard.html` 里没有；且 JS 从未使用（死规则）。
  → v0.1 写的"`dashboard.html:4`、`cockpit.html:4`"高估了一处，本片不启用该规则（D2.3 用 `.status-line`/`.muted` 文案即可）。
- 轮询 3 处：`index.html:438`（250ms×60）、`content.html:9`（`setInterval` 500ms）、`customer_service.html:105`（2000ms×15，最长 30s）。
- **首屏空白表格 6 处**（无任何占位行）：`index.html:97`、`index.html:105`、`orders.html:4` 的 `#orders`、`customer_service.html:68`、`customer_service.html:80`、`cockpit.html` 的 4 个 `.table-wrap`。
- 已有占位行的只有 1 处：`index.html:51` 的 `<tr><td colspan="7" class="muted">加载中…</td></tr>`。

### 2.5 状态标签（Badge）现状

| 页面 | 实现 | 是否有语义色 |
|---|---|---|
| `/orders` | `<span class="tag warn">` / `<span class="tag good">`（`orders.html:5`） | ✅ 但 `.warn/.good` 在**模板 `<style>` 里**定义，与 shell 的 `.status.warning,.warn{color:#946500}` **同名冲突**（模板层覆盖 shell 层，语义不统一） |
| `/cockpit` | `statusBadge()`（`cockpit.html:26-29` 的映射表）+ `.tag.good/.warn/.critical/.info/.insufficient` | ✅ 最完整，**但只在页内 `<style>`**，其他页拿不到 |
| `/dashboard` | `.tag.good/.warn/.critical/.insufficient`（`dashboard.html:4`） | ✅ 页内定义，与 cockpit **重复实现** |
| `/shopify` | `<span class="tag good">` | ✅ 禁区页，不动 |
| `/content` | `<span class="tag good/warn">` + 模板 `.good/.warn` | ✅ 页内定义 |
| `/admin_users` | `<span class="tag member-status active/inactive">` | ✅ 禁区页，不动 |

→ **语义色分散在 5 个模板的 `<style>` 里各自实现**，无全局单一真源。

### 2.6 金额与数字的现状

| 位置 | 字段 | 类型 | 当前显示 |
|---|---|---|---|
| `orders.html:5` 主表「金额」 | `gross_amount` + `currency` | `Decimal`（`app/schemas.py:906` `ExternalOrderListItem`） | `esc(item.gross_amount)` 直出，**无固定小数位**（如 `12.5` 而非 `12.50`） |
| `orders.html:5` 「商品件数」 | `net_item_qty` + `line_count` | int | `12（3 行）` |
| `orders.html:6` 详情表 | `ordered_qty`/`cancelled_qty`/`refunded_qty`/`gross_amount` | int / Decimal | 直出 |
| `warehouse.html:18` | `expected_qty` | int | 直出 |
| `index.html:97` | `on_hand_qty` | int | 直出 |
| `dashboard.html:7` | `on_hand_qty`/`daily_avg_qty`/`days_of_inventory`/`suggested_replenishment_qty` | 数值 | 直出 |

> `body` 已全局 `font-variant-numeric: tabular-nums`（`tokens.css:160`），**等宽这一项已满足**；缺的是**右对齐**与**小数位固定**。
> 金额是**多币种**（`currency` 字段独立，`cockpit` 甚至有 `by_currency`），→ **禁止跨币种求和或折算**，只做格式化。

### 2.7 token 可用性核查（`app/static/css/tokens.css`）

- 语义色 13 + 主色衍生 4 + 成功态 4 + 警示态 5 + 危险态 4 + 表面/背景 8 + 边框 6 + 文字 5 + 中性 11 + 字号 7 + 行高 2 + 间距 6 + 圆角 4 + 阴影 2 + 字体 2 **均已就位**。
- **状态 Badge 所需的色值组合已全部存在**，本片**不需要新增任何颜色 token**：
  - success：`--color-success-soft` + `--color-success-strong`
  - warning：`--color-warning-soft` + `--color-warning-strong`
  - danger：`--color-danger-soft` + `--color-danger-strong`
  - info：`--color-primary-soft` + `--color-primary-deep`
  - neutral：`--color-bg-muted` + `--color-text-subtle`
- 间距 `--space-1..8`（4 的倍数）、圆角 `--radius-sm/md/lg/pill` 均已就位，本片**直接引用，不新增**。
- **斑马纹候选**：`--neutral-50` = `#fbfcfe`（= `--color-surface-subtle`）、`--color-bg-tint` = `#f7fafe`、`--neutral-100` = `#f4f7fb`（= `--color-bg`）。三者与 `--color-surface`(#fff) 的对比度依次递增。→ 见 §3 裁定 Q-P5S2-1。

### 2.8 既有测试对改动的敏感点（全部实测）

| 文件 | 行 | 断言 | 本片影响 |
|---|---|---|---|
| `tests/test_p5s1_tokens.py` | 168 | `<script>` 内色值合计 **== 24** | ⚠️ **禁止在 `<script>` 里写任何 hex/rgba**（badge 走 class，不走 inline style） |
| 同上 | 154/156 | 模板 `<style>` 内 hex=0、rgba=0 | ⚠️ 新样式一律 `var(--color-*)` |
| 同上 | 108 | `var(--x)` 零悬空 | 新样式引用的 token 必须已定义 |
| 同上 | 128 | 模板数 **== 10** | ⚠️ **不得新增/删除 `app/templates/*.html`** |
| 同上 | 117 | 模板内无 `:root` | 新样式不得写 `:root` |
| 同上 | 144 | shell 5 个字面串 | 重写 shell 时**不得格式化**这 5 处 |
| `tests/test_frontend_customer_service.py` | 28 | marker **`只有需要跟进`** | ⚠️ 唯一命中 `customer_service.html:92` 的工单空态文案 → **改写必须保留该子串** |
| `tests/test_frontend_orders.py` | 31 | `订单事实`/`外部订单事实`/`待人工处理`/`待发（外部已支付）`/`导出待发清单（人工履约）`/`人工履约 Proposal`/`不执行发货`/`付款`/`退款` | ⚠️ 卡片标签与页头文案**不得删改**；空态文案改写须避开这些词或保留 |
| `tests/test_frontend_warehouse.py` | 79 | `已提交，待人工处理`（`index.html:195` `statusLabel` 兜底） | ⚠️ 该函数不动 |
| `tests/test_admin_users.py` | 220 | `class="member-table"` | 禁区页，不动 |
| `tests/test_p4_cockpit.py` | 735-737/759-763 | `chartEmpty` 必须在 `echarts.init` 之前；`无订单事实`/`无库存策略`；空态文案前 200 字符含「无」或「暂无」 | ⚠️ cockpit 的图表空态**不得改动**；本片对 cockpit 只做一致性核对（见 §3 D7） |
| `tests/test_api_dashboard.py` | 169-218 | 大量精确 JS 字符串 + 侧栏 5 字面串 | dashboard 的**函数体与常量不得删改**，只能追加 |
| `tests/test_demo_path_contract.py` | 254-256 | 未登录 303 → `/login?next=…` | 路由不动即安全 |

> 复核命令：
> ```bash
> grep -n "只有需要跟进\|待人工处理\|已提交，待人工处理\|member-table" tests/*.py
> ```

### 2.9 `assistant.html` 的表格与空态是 **DOM API 生成**，不是字符串拼接（v1.1 新增，决定 §4.2 范围）

实测（`app/templates/assistant.html`）：

```js
function empty(text) {
  const el = document.createElement('div');
  el.className = 'assistant-empty';     // ← class 由 JS 赋值，非 class="..." 字面量
  el.textContent = text;
  return el;
}
function makeTable(headers, rows) {     // ← 只有 2 个参数，无「列类型」概念
  const head = table.createTHead().insertRow();
  headers.forEach(text => { const th = document.createElement('th'); th.textContent = text; ... });
  rows.forEach(row => { row.forEach(value => { const td = tr.insertCell(); ... }); });
}
```

`makeTable(` 全文件 **9 处**：1 处定义 + **8 个调用点**（商品列表 / SKU 健康 / 补货建议 / 字段结果 / 告警 / 机会 / 证据 / 同步记录）。
`empty(` 全文件 **6 处**：1 处定义 + **5 个调用点**。

**两个后果，必须写进实施范围**：

1. **数值列右对齐在本页无法"加一个 class 属性"完成**——必须给 `makeTable` 增加第三个参数（列类型数组）并**改全部 8 个调用点**；或者改成 headers 传对象。这是**签名级改动**，风险远高于其它页的"改 HTML 属性"。
   → **本片裁定（Q-P5S2-5）：`assistant.html` 只做空态统一，不做数值列对齐。**
   空态统一可在**不改签名、不动 5 个调用点**的前提下完成：只把 `empty()` 的**函数体**改为输出 `<div class="empty"><div class="empty-text">…</div></div>`（保留 `assistant-empty` 类名以兼容既有样式）。
2. **T-2 的"断言模板含 `class="num"` 字面量"对本页不可用**——这里没有字面量。若日后要给 assistant 做数值列，T-2 必须改用 `className`/`'num'` 断言。本片因范围已收敛，**不在 T-2 里覆盖 assistant**。

### 2.10 `<script>` 内生成表格的页，加 class 的可行性（v1.1 新增）

| 页面 | 表格生成方式 | 加 `.num` / `.badge` 是否安全 |
|---|---|---|
| `dashboard.html` | 5 个用模板字符串 `${...}` 拼 `<th>`/`<td>` | ✅ 可以，但**不得触碰** `test_api_dashboard.py` 的 2 条精确单行断言（见 §4.3） |
| `cockpit.html` | 模板字符串 | 本片只核对，不改 |
| `orders.html` | 主表静态；详情表在 `${...}` 里拼 | ✅ 可以 |
| `warehouse.html` / `content.html` | 模板字符串 | ✅ 可以 |
| `shopify.html` | 模板字符串 | ❌ 禁区 |
| `assistant.html` | **DOM API** | ❌ 需改签名，本片不做（§2.9） |

---

## 3. 目标（Target）

**一句话**：在不动接口、不动列、不动表单的前提下，让**数字能对齐、状态能扫读、空了有话说、加载不白屏**。

### D1 表格可读性：三层落地

**D1.1 数值列右对齐 + 等宽（全局能力 + 逐列标注）**

- 在 `workspace-shell.css` 增设**全局数值列类** `.num`（与 `cockpit.html:4`、`dashboard.html:4` 现有定义**语义一致**）：
  ```css
  .num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
  ```
  - 该类在 shell 定义后，**`cockpit.html:4` 的页内 `.num` 定义变成冗余** → **允许删除这一处**（值完全相同，删除后视觉不变；沿 P5-S1 已确立的"删除重复 L2 规则"同法）。
  - **`dashboard.html` 本来没有 `.num`**（v1.1 实测修正，见 §2.2）：它只有 `.quadrant-num`，本片**保留不动**。对本片而言 dashboard 的改动仅限于给表格加 class，**不涉及任何删样式动作**。
  - `.quadrant-num`（`dashboard.html:4`）**保留不动**——它还有 `white-space:nowrap` 之外的表格专属作用域，改它风险高于收益。
- 逐页把**纯数值语义列**标注为 `class="num"`（详见 §4.2 表）。判定标准（三条全中才算数值列）：
  1. 该列内容是**可比较的数量/天数/比率**，不是 ID、不是状态、不是混排文本；
  2. 该列**不含**需要左对齐阅读的长文本；
  3. 该列**同一行内只有一个值**（如「12（3 行）」这类混排列**不**整列右对齐，见 Q-P5S2-3）。
- **明确不改**：`table`/`th,td` 的 `padding`、`font-size`、`border-bottom`、`min-width`、`vertical-align`（G6），以及**任何 `text-align` 之外的既有属性**。

**D1.2 金额固定两位小数（仅展示层）**

- 新增一个**全局格式化函数**，放在 `app/static/` 下的独立 JS 文件？→ **否**。无模板引擎，每个模板自带 `<script>`；为避免 6 个模板各写一份，**在 `app/static/labels.js` 追加一个格式化函数**？→ `labels.js` 是既有外部文件，P5-S1 把它划入"禁区：所有 `<script>` 块（含 `app/static/labels.js`）"。
  → **裁定 D1.2a**：格式化函数**在 `tokens.css` 无关，故放在一个新的静态文件 `app/static/format.js`**，由需要它的模板在 `<script>` 标签处引入（与 `labels.js` 同模式：`/static/labels.js?v=...`）。
  - 风险：`labels.js` 的测试契约（若有）不受影响；新增静态文件不触发 `StaticFiles` 路由改动（`app/main.py:243` 已挂载 `/static`）。
  - **替代方案 D1.2b**：每个模板内联一个 3 行小函数（重复 6 份）。
  - → **Q-P5S2-2 待复核方拍板**（见 §6）。**推荐 D1.2a**（单一真源，符合 P5-S1 精神）。
- 函数契约（两个函数）：
  ```js
  // 金额：保留两位小数，非数值 → '—'；不折算、不换币种
  dianshangAmount(value, currency)  // → '12.50' / '12.50 CNY' / '—'
  // 数量：整数直接用；小数保留两位；非数值 → '—'
  dianshangQty(value)               // → '12' / '12.50' / '—'
  ```
- **适用范围**：仅 `/orders`（主表金额列、详情表金额列）。`/cockpit` 的 GMV 是**多币种聚合**（`by_currency`），**不做小数位统一**（避免暗示可加总）→ 不改。
- **不做**跨币种换算、不做单位换算、不改接口返回的 `gross_amount` 原值。

**D1.3 状态文字 → Badge**

- 在 `workspace-shell.css` 定义**全局 Badge 组件**（复用现有语义色 token，**不新增颜色**）：
  ```css
  .badge{display:inline-flex;align-items:center;gap:.25rem;padding:.16rem .5rem;border-radius:var(--radius-pill);
         font-size:.72rem;line-height:1.5;white-space:nowrap;
         background:var(--color-bg-muted);color:var(--color-text-subtle)}
  .badge-success{background:var(--color-success-soft);color:var(--color-success-strong)}
  .badge-warning{background:var(--color-warning-soft);color:var(--color-warning-strong)}
  .badge-danger {background:var(--color-danger-soft);color:var(--color-danger-strong)}
  .badge-info   {background:var(--color-primary-soft);color:var(--color-primary-deep)}
  .badge-neutral{background:var(--color-bg-muted);color:var(--color-text-subtle)}
  ```
- **状态居中**（§1.6「状态居中」）：状态列 `th`/`td` 加 `text-align:center`（通过 `class="col-status"` 或直接在 `th` 上加类）。
- **映射规则**（不发明新语义，映射表见 §4.2）：
  - 成功/完成/已确认/真实来源 → `success`
  - 待处理/待发/待审批/预警/模拟回放 → `warning`
  - 失败/驳回/取消/危险 → `danger`
  - 正常/中性/未分类 → `neutral`（默认）
  - 进行中/已读/信息 → `info`
- **`.badge` 与既有 `.tag` 的关系**：`.tag` 保留原样不动（多处在用），**本片新增 `.badge` 作为状态专用组件**；把**状态语义**从 `.tag` 迁移到 `.badge`（`.tag` 继续承担"分类标签"角色，如来源/类型）。→ 避免改动既有 `.tag` 语义带来的回归。
- 迁移范围仅限本片实施页（§4.2）；`.cs-status`（`customer_service.html`）已有专属类，改为基于 `.badge` 的实现或保留（见 §4.2）。

**D1.4 斑马纹（极浅底色）**

- 在 `workspace-shell.css` 增加：
  ```css
  table tbody tr:nth-child(even){background:var(--neutral-50)}
  ```
  → 色值选择见 **Q-P5S2-1**（`--neutral-50` #fbfcfe 对白底几乎不可辨；`--color-bg-tint` #f7fafe 稍可辨；`--neutral-100` #f4f7fb 明显但接近页面底色）。
- **不描重边框**（§1.6）：`border-bottom` 保持 `1px solid var(--shell-line)` 不变，**不得**加粗、不得给行加外框。
- **不得**给 `thead` 上底色导致"斑马纹从第二行起算"错位——`nth-child(even)` 以 `tbody` 内行序号计，`thead` 独立，天然正确。
- `tbody tr:hover` 现状：`admin_users.html:10` 有 `.member-table tbody tr:hover{background:var(--color-bg-faint)}`（禁区页）。实施页若加 hover 底色，**必须与斑马纹共存且 hover 优先级更高**。

**D1.5 表格行高维持紧凑（§1.6 硬约束）**

- `th,td` 的 `padding`（现状 `.68rem .6rem`）**一律不改**；`font-size:.84rem` 不改。
- 当前行高 ≈ `13.4px×1.4 + 2×10.9px ≈ 40px`，落在 §1.6 的 36–40px 区间内 → **现状已达标，本片不得破坏**。
- 呼吸感只允许加在**容器**（见 D3）。

### D2 空态与加载态：统一为一个组件

**D2.1 组件定义（全局，扩展 `.empty`）**

- 保留既有 `.empty` 类名与视觉（`dashboard.html`/`cockpit.html` 已在用，**不得删**），**扩展**为三层结构：
  ```html
  <div class="empty">
    <div class="empty-title">暂无订单事实</div>
    <div class="empty-text">没有数据不代表接口失败；当前 Workspace 还没有同步到任何外部订单。</div>
    <div class="empty-action"><a class="btn-link" href="/shopify">去 Shopify 同步订单</a></div>
  </div>
  ```
- 新增样式（全部用现有 token）：
  ```css
  .empty-title{font-weight:650;color:var(--color-text);margin-bottom:.25rem}
  .empty-text{color:var(--color-text-muted);max-width:46ch;margin:0 auto}
  .empty-action{margin-top:.6rem}
  .btn-link{color:var(--color-primary);text-decoration:none;border-bottom:1px solid var(--color-primary-border)}
  .btn-link:hover{color:var(--color-primary-hover);border-bottom-color:var(--color-primary-hover)}
  ```
- **表格内的空态**：保持 `<tr><td colspan="N">` 结构（`colspan` 必须与列数一致），但 `td` 内放**同一套 `.empty` 内容**（`.empty` 在 `td` 内可用），并统一用 `class="empty-cell"` 而非 `class="muted"`/`class="error"`：
  ```html
  <tr><td colspan="9" class="empty-cell"><div class="empty">…</div></td></tr>
  ```
- **错误态**：`<td colspan="N" class="error">` 保留（错误 ≠ 空态，语义要分开），但**必须带上重试入口**（§1.4「出错：中文错误 + 重试入口」）。
- **`.cs-error` 缺陷修复**：`customer_service.html:93` 使用的 `.cs-error` **全仓零定义**（`grep -rn "\.cs-error" app/ --include=*.css --include=*.html` 只命中使用处）→ 本片改为 shell 已有的 `.error`。
  **注意（v1.1 修正）**：shell 里是 `.error{color:#a72727}`——**硬编码**，不是 `var(--color-danger-strong)`。两者恰好同值，视觉零变化；但**只能引用、不得修改该规则**（违反 G6）。也不新增 `.cs-error` 定义（减少专有类）。

**D2.2 五个场景的落地口径（§1.4 表格逐行对齐）**

| 场景 | 本片口径 | 实施页 |
|---|---|---|
| 列表无数据 | `.empty` 三层：标题 + 说明 + **操作引导**（有下一步动作时必须有） | `/orders` `/ops` `/warehouse` `/dashboard` `/customer-service` `/assistant` `/content` |
| 筛选后无结果 | 明确「按当前筛选无结果」+ **一键清除筛选**（`cockpit` 已有此范式：`cockpit.html:26`「可点击『清除筛选』查看全部」） | `/dashboard`（SKU 搜索）/ `/cockpit`（已达标） |
| 首次加载 | **骨架屏不做全站铺开**；对 6 处首屏空白表格补**最小占位行** `<tr><td colspan="N" class="empty-cell">加载中…</td></tr>`（对齐 `index.html:51` 现有写法） | `/ops`(2) `/orders`(1) `/customer-service`(2) `/cockpit`(4 容器) |
| 局部刷新 | **不遮挡可操作区域**：刷新时**保留旧数据** + 在容器内显示"刷新中"角标（启用已存在但未使用的 `.chart-refreshing` 思路），**不得**整块替换为 skeleton | `/dashboard` `/cockpit` |
| 出错 | 中文错误 + **重试入口**；禁止英文/裸报错 | 全部实施页 |

**D2.3 加载态不遮挡可操作区域的具体含义（针对 `showSkeleton()`）**

- `dashboard.html:31` / `cockpit.html:31` 的 `showSkeleton()` 首载覆盖 KPI 区 + 2 张图表。
- **本片不删除 skeleton**（首载占位有防抖动价值），但**必须**：
  1. `showSkeleton()` 的调用**仅保留在首次加载**（现状已是如此，不放大）；
  2. **刷新时**（`firstLoad=false`）不再整块替换，改为在**面板内**显示轻量"刷新中…"文案（复用已有的 `.status-line`/`.muted`）；
  3. **不得**引入任何 `position:fixed` 全屏遮罩。
- `cockpit` 受 `test_p4_cockpit.py` 强约束（`chartEmpty` 必须在 `echarts.init` 之前等），→ **cockpit 只做 §2 一致性核对，不改代码**（见 D7）。

### D3 "呼吸感"的落点（严格限定，避免踩 §2）

**只允许加在**：`.panel` 内边距、`.card` 内边距、卡片/栅格 `gap`、`.empty` 的上下留白、`.badge` 的内边距、按钮/表单控件的间距、圆角改用 `var(--radius-*)`。

**明令禁止**（踩 §2「任何让单屏信息量下降的改动默认归入不可借」）：
- ❌ 加大表格 `th,td` 的 `padding`（会突破 §1.6 的 36–40px 行高上限并减少单屏行数）
- ❌ 增加表格列、增删筛选器（§2b 硬约束）
- ❌ 加大正文字号（会减少单屏行数）
- ❌ 用图表替代明细表（§2 不可借第 2 条）
- ❌ 隐藏筛选器（§2 不可借第 3 条）
- ❌ 引入任何动效/过渡（§2 不可借第 4 条；且不得新增 `@keyframes`）

**圆角**：`.panel/.card/.empty` 的 `border-radius` 现状为 `.7rem/.6rem/.5rem`（实测于 `workspace-shell.css:1`）。**v1.1 修正 v0.1 的映射错误**：

| 类 | 现状 | 应映射到 | 目标值 | 差异 | 判定 |
|---|---|---|---|---|---|
| `.panel` | `.7rem` = 11.2px | `--radius-lg` | 12px | **+0.8px** | ✅ P5-S1 的 ≤1.5px 容差内 |
| `.empty` | `.5rem` = **8px** | `--radius-md`（**不是 sm**） | 8px | **0px** | ✅ **零差异**（v0.1 误映射到 `--radius-sm` 4px） |
| `.card` | `.6rem` = 9.6px | `--radius-md` | 8px | **−1.6px** | ⚠️ 超容差 → **Q-P5S2-3** |

→ 结论修正：**`.empty` 不构成裁定问题**（映射后零视觉变化，直接改即可）；真正待裁的只有 `.card` 一项。

### D4 颜色语义先定死（§1.6）

- 本片**不定义涨跌色**（§1.6 超期规则：未确认前 token 中不定义，表格只用 success/warning/danger 表达状态）。
- **须在 `tokens.css` 内以注释显式登记"涨跌色本期不定义"**，并在交付说明中标注依据（§1.6 §"⚠️涨跌色的方向：待确认"）。
- Badge 五档语义（success/warning/danger/info/neutral）在 `tokens.css` 注释中写明"全站一致，此后不再讨论"。

### D5 零后端改动

- `app/main.py` 一行不动（含 `_render_workspace_page:328-338`、`login_page:497`）。
- **不新增/删除任何 API 路由**、不改任何响应模型、不改 schema、不改 migration、不改 `app/static/labels.js`。
- 新增文件仅限 `app/static/format.js`（若 Q-P5S2-2 选 D1.2a）与 `tests/test_p5s2_tables_and_empty.py`。

### D6 测试面

- 新增 `tests/test_p5s2_tables_and_empty.py`，**8 条**契约用例（T-1..T-8），全静态校验，不启动服务、不碰数据库。
- **不得修改既有任何测试文件**（含 `test_p5s1_tokens.py`）。

### D7 `cockpit` 与禁区的处置

- **`cockpit`**：表格右对齐（`.num`）、Badge（`statusBadge` + `.tag.*`）、空态（`.empty`）、骨架屏**四项均已达标**，是全站最佳实践样板。本片对它**只做核对**（确认 `.num` 删除页内定义后视觉不变），**不改逻辑**。
- **禁区页**（`admin_users` / `shopify` / 设置类 / 日志页）：**零改动**。

---

## 4. 改动清单

### 4.1 新建

| 文件 | 内容 | 预估 |
|---|---|---|
| `app/static/format.js` | `dianshangAmount()` / `dianshangQty()`（**仅当 Q-P5S2-2 选 D1.2a**） | ~15 行 |
| `tests/test_p5s2_tables_and_empty.py` | T-1..T-8 契约用例 | ~200 行 |

### 4.2 修改（`workspace-shell.css` + **7** 个实施页模板）

> 实施页 = `/ops`(index) `/dashboard` `/orders` `/warehouse` `/customer-service` `/content` `/assistant` —— **共 7 个**（v0.1 标题误写为 6）
> **不含** `admin_users` / `shopify` / `cockpit`（cockpit 只核对，见 D7）。
> 其中 **`assistant` 只做空态、不做数值列与 badge 迁移**（依据 §2.9 的 DOM API 事实与 Q-P5S2-5 裁定）——它的任务性质与其余 6 页不同，不得套用同一套改法。

**A. `app/static/workspace-shell.css`（新增规则，不改既有规则）**

| 新增 | 说明 |
|---|---|
| `.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}` | 全局数值列类（与 cockpit/dashboard 现有 `.num` 语义一致） |
| `table tbody tr:nth-child(even){background:<斑马纹色>}` | 色值见 Q-P5S2-1 |
| `.badge` + `.badge-success/.badge-warning/.badge-danger/.badge-info/.badge-neutral` | 状态胶囊组件 |
| `.empty-title` / `.empty-text` / `.empty-action` / `.btn-link` / `.empty-cell` | 空态三层结构 + 表格内空态容器 |
| `.col-status{text-align:center}` | 状态列居中 |
| `.loading-cell{color:var(--color-text-muted);text-align:center;padding:.9rem}` | 加载占位行（可选，见 Q-P5S2-4） |

**禁区（shell 内）**：既有 81 条规则**一个字符都不改**；G2 的 5 个字面串（`position:fixed` / `height:100vh` / `overflow-y:auto` / `margin-left:232px` / `margin-left:76px`）**不得格式化**。

**B. 模板逐页改动表**

| 页面 | 表格 | 数值列 → `.num` | 状态列 → `.badge` | 空态/加载态 |
|---|---|---|---|---|
| `index.html` | `:51` 跨境事实 | 库存快照 / 可售数量 / 有库存商品 | — | `:509` 空态 → `.empty` 三层（含"去 Shopify 同步"引导）；`:516` 错误 → 加重试 |
| | `:97` 库存 | 库存 | — | `:461` 空态 → `.empty`；**补首屏加载占位行** |
| | `:105` 补货/采购 | 数量 | 状态 → `.badge` + 居中 | `:374` 空态 → `.empty`；**补首屏加载占位行** |
| `dashboard.html` | `:7` SKU 健康 | 库存 / 近N日销量 / 日均 / 可售天数 / 建议补货 | 状态·数据完整度 → `.badge` + 居中 | `:27` 空态 → `.empty`（区分"无匹配" vs "无数据"） |
| | `:21` 数据不足 | 增长率 / 可售天数 | 数据完整度 → `.badge` | `:23` 错误 → 加重试 |
| | `:32` 同步健康 | 尝试 / 耗时 / 心跳 | 状态 → `.badge` | `:33` 错误 → 加重试 |
| | `:38` 外部事实 | 库存快照 / 可售数量 / 有库存商品 | — | `:38` 空态 → `.empty` 三层 |
| | `:39` 入库 | 预计 / 实收 | 状态 → `.badge` | 空态 → `.empty` |
| `orders.html` | `:4` 主表 | 金额（**两位小数**）/ 商品件数 | 状态 → `.badge` + 居中；完整度 → `.badge` + 居中 | `:5` 空态 → `.empty` 三层（**保留"没有数据不代表接口失败"语义**）；`:7` 错误 → 加重试；**补首屏加载占位行** |
| | `:6` 详情表 | 订购数量 / 取消 / 退款 / 金额（**两位小数**） | 映射状态 → `.badge` + 居中 | 「暂无行项目」补 `.empty` |
| `warehouse.html` | `:18` 入库明细 | 预计数量 | 状态 → `.badge` + 居中 | 未选单时的空白 → `.empty`（说明 + 引导"先选择入库单"） |
| `customer_service.html` | `:68` 工单 | — | 状态 / 优先级 → `.badge` + 居中 | `:92` 空态 → `.empty` 三层，**必须保留"只有需要跟进"子串**；`:93` `.cs-error` → `.error` + 重试；**补首屏加载占位行** |
| | `:80` 知识文档 | 版本数 | 最新状态 → `.badge` | `:103` 空态 → `.empty`；**补首屏加载占位行** |
| `content.html` | `:7` 历史任务 | —（版本号内嵌文本，不整列右对齐） | 状态 → `.badge` | 「暂无历史任务」→ `.empty`（补 class）；「正在加载历史任务…」→ 加载占位 |
| `assistant.html` | `makeTable()` 8 表（DOM API） | **不做**（需改签名 + 8 个调用点，风险不划算） | **不做** | **只改 `empty()` 函数体**：返回 `<div class="empty"><div class="empty-text">{text}</div></div>`，**保留 `assistant-empty` 类名**；签名 `empty(text)` 与 **5 个调用点零改动** |

**C. `cockpit.html`（只核对，可能零改动）**

- 页内 `.num{text-align:right;white-space:nowrap}` 与 shell 新增的 `.num` **值不同**（新类多 `font-variant-numeric:tabular-nums`，但 body 已全局开启 → **计算值相同**）→ **删除页内 `.num` 定义**，视觉不变。
- **删除前必须实测确认**：删掉后 `cockpit.html` 的 `.num` 单元格计算样式仍为右对齐 + nowrap + tabular-nums。

### 4.3 禁区（明确不动）

- `app/main.py`、`app/*.py`、`app/schemas.py`、`app/rag/**`、`db.py`、`migrations/**`、`app/demo_seed.py`、`app/static/labels.js`、`app/static/echarts.min.js`
- `app/templates/admin_users.html`、`app/templates/shopify.html`（§2b）
- `app/templates/cockpit.html` 的 **JS 逻辑与 DOM**（仅允许删除冗余 `.num` 样式定义，且需实测）
- `app/templates/dashboard.html` / `cockpit.html` 的**被断言的字符串**。v1.1 精确化（v0.1 写成"函数体与常量字符串不得删改"过严，会挡住必要的 `<th>` 加 class）：
  `test_api_dashboard.py` 中只有 **2 条是整行精确**断言 ——
  `function quadrantIsInsufficient(item){return item.quadrant==='insufficient';}` 与 `const points=items.filter(quadrantCanPlot)`；
  其余全部是**子串包含**（`id="xxx"`、`growth_window:'7'`、`/api/external/sync/runs?limit=20`、`X-CSRF-Token` 等）。
  → **结论：可以给 `<th>`/`<td>` 加 class**，前提是上列精确/子串全部原样保留。
- **所有既有测试文件**
- 表格列增减、筛选器增减、业务表单结构、接口返回结构
- `docs/` 下所有文档（本片不改文档；如需更新由复核方决定）

---

## 5. 守卫与红线（实施方逐条自检，复核方逐条实测）

| # | 守卫 | 实测方法 |
|---|---|---|
| **G1** | 零后端改动 | `git diff --name-only` 与 `app/*.py`、`app/schemas.py`、`migrations/`、`db.py` **零交集** |
| **G2** | shell 五个字面串仍在 | `grep -c "position:fixed\|height:100vh\|overflow-y:auto\|margin-left:232px\|margin-left:76px" app/static/workspace-shell.css` 各 ≥1（**不得格式化**） |
| **G3** | `<script>` 内零色值 | 10 个模板 `<script>` 内 `#[0-9a-f]{3,8}` + `rgba(` 合计 **仍 == 24**（`test_p5s1_tokens.py::test_t7` 必须绿）→ **Badge 禁止 inline style 写色** |
| **G4** | 模板 `<style>` 内零 hex/rgba | 同上 T-6 必须绿 → 新样式一律 `var(--color-*)` |
| **G5** | `var()` 零悬空 | T-2 必须绿；新增样式引用的 token 必须在 `tokens.css` 已定义 |
| **G6** | 表格结构属性未改 | `table` / `th,td` / `.table-wrap` 的 `padding` / `font-size` / `border-bottom` / `min-width` / `vertical-align` **与改动前逐字节一致**（只允许新增 `.num`/`.badge`/`.empty-*`/斑马纹规则） |
| **G7** | 模板数不变 | `len(app/templates/*.html) == 10`（T-4 必须绿）→ **不新增/删除模板** |
| **G8** | 金额格式化只改显示 | `dianshangAmount` 不做币种换算、不做求和；`gross_amount` 原值仍从接口原样读取；**不得**在 `<script>` 中出现任何 `parseFloat(...)*rate` 类换算逻辑 |
| **G9** | 状态语义五档固定 | Badge 只用 success/warning/danger/info/neutral 五类；**不引入涨跌色**；`tokens.css` 内有"涨跌色本期不定义"注释 |
| **G10** | 行高不破 36–40px | `th,td` 的 `padding` 与 `font-size` 未改（G6）；实测计算行高仍 ∈ [36,40]px |
| **G11** | 无新增列/筛选器/接口 | `git diff` 中不出现 `<th>` 新增、不出现 `<select` 新增、`app/main.py` 路由数不变 |
| **G12** | 无外部依赖 | 全仓无 `cdn.` / `unpkg` / `jsdelivr` / `@font-face` / `googleapis` / 新 `package.json` / 新构建配置 |
| **G13** | 既有测试全绿 | `test_p5s1_tokens.py`、`test_p4_cockpit.py`、`test_api_dashboard.py`、`test_frontend_orders.py`、`test_frontend_customer_service.py`、`test_admin_users.py`、`test_frontend_warehouse.py` 全绿 |
| **G14** | 字符串 marker 保住 | `grep -n "只有需要跟进\|待人工处理\|待发（外部已支付）\|已提交，待人工处理" app/templates/*.html` 的命中位置与改动前**一致或为超集** |
| **G15** | 禁区页零改动 | `git diff --name-only` 不含 `admin_users.html`、`shopify.html`；`cockpit.html` 的 diff 仅限删除冗余 `.num` 样式（若删） |
| **G16** | 交付说明落盘 | `.testtmp/delivery.p5s2.md` 存在且含 §8 五节 |
| **G17**（v1.1） | `format.js` 加载顺序正确 | 引入 `/static/format.js` 的模板中，该标签**必须同步、位于调用它的内联 `<script>` 之前、不得带 `defer`/`async`**；`grep -n "format.js"` 命中行号 < 首个 `dianshangAmount(` 调用行号。**这是本片唯一无法被静态测试覆盖的运行时风险**（§10 风险 6） |
| **G18**（v1.1） | `assistant.html` 只改函数体 | 该文件 diff 仅限 `empty()` 函数体；`makeTable(` 与 `empty(` 的**出现次数不变**（9 / 6）、签名未变、8 个调用点未动 |

---

## 6. 待拍板的 **6** 条（**实施前必须回填，实施时不得自行变通**）
> Q-1..Q-4 为 v0.1 原有；Q-5、Q-6 为 v1.1 复核时新增（对应 §2.9 与 P5-S1 复核前置）。

| 编号 | 问题 | 选项 | 推荐 |
|---|---|---|---|
| **Q-P5S2-1** | 斑马纹底色用哪个 token？**（追加：hover 色）** | (a) `--neutral-50` #fbfcfe —— 对白底**几乎不可辨**（≈1.02:1），视觉提升≈0<br>(b) `--color-bg-tint` #f7fafe —— 极浅且可辨<br>(c) `--neutral-100` #f4f7fb —— 明显，但与页面底色同色，易读成"表头底色"<br>**追加子问题**：斑马纹若用 `--color-bg-tint`(#f7fafe)，与既有 hover 色 `--color-bg-faint`(#f8fbff) **几乎同色** → 偶数行 hover 会"看不见"。建议 hover 改用 `--neutral-100`(#f4f7fb) 深一档 | **(b)** + hover 用 `--neutral-100` |
| **Q-P5S2-2** | 金额格式化函数放哪？ | (a) 新增 `app/static/format.js`，模板引用（单一真源）<br>(b) 每个模板内联一份 3 行函数（重复 6 份，零新增文件） | **(a)**（已核实：`/static` 已挂载 `main.py:243`，新增文件零后端改动；且**无任何测试**断言 static 目录文件清单） |
| **Q-P5S2-3** | 圆角怎么归？ | (a) 三个都改（`.card` 差 −1.6px **超** P5-S1 的 ≤1.5px 容差）<br>(b) **只改容差内/零差异的两个**：`.panel`→`--radius-lg`(+0.8px)、`.empty`→`--radius-md`(**0px**)，`.card` 保持 `.6rem` 留待后续切片<br>(c) 本片完全不碰圆角 | **(b)**（v1.1 修正：`.empty` 实为 **8px = `--radius-md`，零差异**；v0.1 把它算成 −4px 是映射错误，选项因此收窄） |
| **Q-P5S2-4** | 加载占位行用哪种？ | (a) 复用 `.empty` 样式（"加载中"与"没数据"同形，语义混淆）<br>(b) 独立 `.loading-cell`（弱化、无虚线框，与空态明确区分） | **(b)**，但须同时定义 `.empty-cell` 并**写清分工**：`empty-cell` = 表格内空态容器（包 `.empty` 三层）；`loading-cell` = 加载占位（弱化、无框）。v0.1 在 D2.2 与 §4.2 里混用了这两个类名 |
| **Q-P5S2-5**（v1.1 新增） | `assistant.html` 是否收敛为"仅空态"？ | (a) 仅空态（改 `empty()` 函数体，不动签名、不动 5 个调用点）<br>(b) 同时做数值列对齐（须给 `makeTable` 加第 3 个参数并改 **8 个调用点**） | **(a)**（依据 §2.9 实测：该页是 DOM API 建表，不是 HTML 字面量；做数值列的性价比极低，且 T-2 无法用字面量断言覆盖） |
| **Q-P5S2-6**（v1.1 新增，流程） | **P5-S1 尚未正式复核**（git log：`0df5dc9` 之后无复核台账提交；v0.1 头部写的"S1 复核方实测 243.55s"目前是虚引用）。本片所有改动都建立在 S1 的 token 层上 | (a) 先补 S1 正式复核（复核方执行，约 15 分钟），再开本片<br>(b) 用户明确以"已自行验收 0df5dc9"记录在案，直接进 S2 | **(a)**：S1 若有问题，S2 会把它放大到 7 个页面 |

> **另需知悉（不需拍板，仅告知）**：§1.6 的"金额固定两位小数"已超出 CSS 能力范围，本片以 §D1.2 的前端格式化实现；`/cockpit` 的多币种 GMV **不做**小数位统一，避免暗示可加总。

---

## 7. 用例设计（T-1..T-8，基线 494 → 502）

> 新文件 `tests/test_p5s2_tables_and_empty.py`，全静态校验，不启动服务、不触碰数据库。

| # | 用例 | 断言要点 |
|---|---|---|
| **T-1** | 全局数值列类存在且**唯一** | `workspace-shell.css` 含 `.num{` 且含 `text-align:right` 与 `tabular-nums`；**全站 `.num{` 定义恰好 1 处**（`cockpit.html` 页内那份已删除；`dashboard.html` **本来就没有** `.num`，其 `.quadrant-num` 保留不动） |
| **T-2** | 数值列标注覆盖 | 逐页统计 `class="num"` 出现次数：`orders`≥4、`index`≥3、`dashboard`≥10（SKU 健康 5 列 ×2）、`warehouse`≥2。**`assistant.html` 不在覆盖范围**（DOM API 无字面量，§2.9）；实现对 `<script>` 模板字符串内的 class 也要计入 |
| **T-3** | 金额两位小数 | `format.js`（或内联实现）含 `toFixed(2)`；`/orders` 主表与详情表的金额渲染路径调用该函数；**不含**任何币种换算表达式 |
| **T-4** | Badge 组件与语义五档 | `workspace-shell.css` 含 `.badge` 与 success/warning/danger/info/neutral 五档；五档色值全部来自 `tokens.css` 已有 token（**不新增颜色字面量**）；实施页状态单元格使用 `.badge` |
| **T-5** | 斑马纹且不描重边框 | `workspace-shell.css` 含 `tbody tr:nth-child(even)` 且背景取自 token；`th,td` 的 `border-bottom` 与改动前一致（仍为 `1px solid var(--shell-line)`，未加粗） |
| **T-6** | 空态组件统一 | `.empty-title` / `.empty-text` / `.empty-action` 存在于 shell；**§4.2 的 7 个实施页**空态输出统一使用 `.empty`，且这些页**不再出现** `<td colspan class="muted">` 形态的空态；`colspan` 与该表列数一致。<br>⚠️ **扫描必须排除禁区页**：`admin_users.html:39/40` 有 2 处 `<td colspan="5" class="muted">`、`shopify.html:2` 有 6 处 `class="muted"` —— 它们是禁区不许改，**全站扫描必然误红**。实现只遍历 7 个实施页，或显式跳过 `{admin_users.html, shopify.html}` |
| **T-7** | 加载占位且不遮挡 | 6 处首屏空白表格均有加载占位行；全仓**无** `position:fixed` 的全屏遮罩（除 shell 既有的 sidebar 5 字面串外不得新增 `position:fixed`）；`.chart-refreshing` 不得被用于整块替换 |
| **G-附加** | 回归守卫（复用既有测试） | `test_p5s1_tokens.py`（T-1..T-8 全绿，含脚本色值 == 24）、`test_p4_cockpit.py`、`test_api_dashboard.py`、`test_frontend_orders.py`、`test_frontend_customer_service.py`、`test_admin_users.py` 全绿 |

> 验收口径：**502 passed / 0 failed**（既有 494 + 新增 8）。不接受降级为人工验收；交付说明须逐条列出 T-1..T-8 与全量的实测结果。

---

## 8. 执行与验证要求（实施方必读）

1. **开工前先回填 §6 的 6 条裁定**（未拍板则停下报告，不自行变通）。其中 **Q-P5S2-6 是流程前置**：P5-S1（`0df5dc9`）未正式复核前不得开工。
2. **开工前实跑基线**：
   ```bash
   env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 \
     /d/python/python/python.exe -m pytest -q -p no:cacheprovider
   ```
   必须 `494 passed / 0 failed`。不绿不开工，先报告。
3. **R-55 节奏**：每完成一个文件立刻落盘，禁止在内存里攒批量。
4. **顺序建议**：`workspace-shell.css`（新增规则）→ `format.js`（若需要）→ `orders.html` → `index.html` → `dashboard.html` → `warehouse.html` → `customer_service.html` → `content.html` → `assistant.html`（**只改 `empty()` 函数体**）→ `tests/test_p5s2_tables_and_empty.py`。
   - `cockpit.html` 若要删冗余 `.num`，**单独一步**，删后立即实测右对齐仍生效。
5. **每个模板改完立即自查**：
   ```bash
   grep -c "#[0-9a-fA-F]\{3,8\}" app/templates/<file>.html   # <style> 内必须为 0
   grep -oE "var\(--[a-z0-9-]+" app/templates/<file>.html | sort -u | while read v; do
     grep -q -- "${v#var(}" app/static/css/tokens.css app/static/workspace-shell.css || echo "悬空 $v"
   done
   ```
6. **完成后写交付说明**到 `.testtmp/delivery.p5s2.md`，必须含 §8 模板要求的五节，另加：
   - 改动文件清单 + 行号；
   - **逐页视觉变化说明**（哪一列右对齐了、哪一处状态变胶囊、哪一处空态多了引导）；
   - §6 四条裁定的实际执行结果；
   - T-1..T-8 与全量 494→502 的实测通过数；
   - 未做项与偏差。
7. **提交到当前分支，不要 push。** 停下等复核。

---

## 9. 已排除的选项（禁止重新试错）

| 选项 | 为什么排除 |
|---|---|
| 给表格加 `border-spacing`/重边框实现斑马纹 | §1.6 明确"极浅底，不描重边框"；且会改 `table` 既有属性 → 踩 G6 |
| 加大 `th,td` 的 padding 做"呼吸感" | 突破 §1.6 行高 36–40px 上限，减少单屏行数 → 踩 §2 不可借 |
| 全站骨架屏铺开 | 改动面过大、易抖；§1.4 验收口只要求"1 个页面做出示范实现"。本片只补最小占位行 |
| 用 CSS `content`/`::after` 生成金额小数位 | CSS 无法做数值格式化与条件渲染 |
| 把状态做成 `<select>`/可点击筛选 | §2b「筛选器增减不做」；且改变交互语义 |
| 引入图表库新实例做表格摘要 | §2 不可借第 2 条（图表替代明细） |
| 全站统一 `.tag` 语义（改 `.tag` 而非新增 `.badge`） | `.tag` 有 8 处既有用法 + 3 套页内变体；改它必然牵连禁区页与既有测试。**新增 `.badge` 专用组件**风险最低 |
| 改 `app/static/labels.js` 加格式化函数 | P5-S1 已把 `labels.js` 划入禁区 |
| 在模板 `<script>` 里写 inline style 的 badge 颜色 | 踩 G3（脚本色值合计必须 == 24），且违反"颜色走 token" |
| 给 `cockpit` 重写空态/加载态 | `test_p4_cockpit.py` 对 `chartEmpty` 与 `echarts.init` 的顺序有强断言；且 cockpit 已是最佳实践 |
| 顺手定义涨跌色 | §1.6 超期规则：未确认前不定义 |
| 顺手修 `.cs-error` 之外的其它历史样式缺陷 | 范围蔓延；本片只修**空态链路**上的 `.cs-error` |

---

## 10. 预期风险与回滚

- **风险 1（表格变窄/列挤）**：数字列右对齐 + `white-space:nowrap` 可能让列宽变化。缓解：只标注**纯数值列**，不动 `min-width`/列宽；实测 320/768/1024/1600px 四档无横向溢出恶化。
- **风险 2（金额显示变化被误读为"数据变了"）**：两位小数是**展示层**变化。缓解：交付说明显著标注；G8 守卫；`/cockpit` 多币种聚合**不动**。
- **风险 3（badge 语义漂移）**：五档映射表若各页理解不一，会出现"同一状态两种颜色"。缓解：映射表写死在 §4.2，交付说明列出逐页对照；T-4 固化五档。
- **风险 4（空态文案改动踩 marker）**：customer_service 的 `只有需要跟进`、orders 的 `待人工处理` 等。缓解：G14 守卫 + 改动前后各跑一次 marker grep 对照。
- **风险 5（首屏空白表格补占位行造成闪一下）**：占位行在数据到达后被替换，正常；缓解：占位行用弱化色、无虚线框（`.loading-cell`），与真实空态视觉区分。
- **风险 6（v1.1 新增·最大的质量盲区）：项目没有任何"运行时"测试。** 502 条用例全部是静态字符串/文件断言，**没有一条会真正执行页面 JS**。因此：
  - `format.js` 加载顺序错 → `dianshangAmount is not defined` → 页面整块空白，**全量测试照样全绿**；
  - `badge`/`num` 的 class 拼错 → 视觉无效，测试也绿。

  **缓解手段（本片强制）**：① G17 的源码顺序守卫；② `node --check app/static/format.js` 语法校验（若环境有 node）；③ 交付说明必须贴**至少 3 个页面的实际渲染截图或人工核对记录**（`/orders`、`/ops`、`/dashboard`），明确写"已人工打开页面确认无 JS 报错"；
  **复核方义务**：这一条我不能只靠跑 pytest 判定，必须要求实施方留人工证据，否则"502 全绿"是个假安全感。
- **回滚**：全部改动集中在 1 个 CSS + 最多 7 个模板 + 1 个静态 JS + 1 个测试文件，`git checkout` 单个提交即可完全回退；无数据、无接口影响。

---

## 11. 复核清单（复核方用）

| 检查 | 判据 |
|---|---|
| §6 裁定 | 4 条均已回填，且实施与裁定一致 |
| 范围 | 实际 diff 与 §4 完全一致；禁区页零改动（G15） |
| 守卫 | §5 的 G1..G16 逐条实测，全绿 |
| 用例 | T-1..T-8 全绿；全量 **502 passed / 0 failed** |
| 视觉 | 抽查 3 个列表页（`/orders`、`/ops`、`/dashboard`）：数字右对齐 + 等宽 + 金额两位小数 + 状态 badge + 斑马纹 + 空态带引导，六项符合（§1.6 验收口） |
| 单屏信息量 | 表格行高仍 ∈ [36,40]px；一屏行数未下降（G10） |
| 语义边界 | 没顺手定义涨跌色、没加列、没改接口、没改表单 |
| 交付说明 | `.testtmp/delivery.p5s2.md` 五节齐全 + 本片附加四节齐全 |
| 人工证据（v1.1） | 交付说明含至少 3 页的人工核对记录（无 JS 报错、右对齐/斑马纹/badge 肉眼可见）——静态测试无法覆盖 |

---

## 12. v0.1 → v1.1 修订记录（复核方，2026-11-01）

> 复核方式：草案每一条 Fact 均用 grep/Read 独立复现，不以草案自述为准。基线由复核方在提交态（`0df5dc9`）独立自跑 **494 passed / 0 failed / 291.36s** 确认。

### 12.1 修正的事实错误（5 处，均会误导实施）

| # | v0.1 原述 | 实测 | 影响 |
|---|---|---|---|
| **F1** | "8 个静态 `<table>` + 约 20 处 JS 拼表"；§2.1 明细相加为 30 | `<table>` 标签共 **23**：静态 **9** + `<script>` 内 **14**；另 assistant 的 DOM 工厂 1 个（无字面量）。`grep -c` 得 22 是因为按**行**计数（dashboard 单行 2 个） | 表清单口径不统一，T-2 覆盖范围无法核对 |
| **F2** | "`colspan` 取值 4/5/7/9/10 五种" | 实测 **5/7/9/10 四种，无 4** | 摘要数字错 |
| **F3** | "`.num` 在 `dashboard.html:4` 与 `cockpit.html:4` 两处定义，两处重复" | **dashboard 没有 `.num`**，只有 `.quadrant-num`，且 `<style>` 从 **`:5`** 起。`.num` 全站仅 `cockpit.html:4` 一处 | §2.2 论据、§4.2C、T-1 全部要改（v0.1 的 T-1 会断言一个不存在的删除） |
| **F4** | "`.chart-refreshing`（`dashboard.html:4`、`cockpit.html:4`）" | 全仓**仅 `cockpit.html:4` 一处** | 高估一处 |
| **F5** | "shell 已有的 `.error`（`color:var(--color-danger-strong)`）" | 实际 `.error{color:#a72727}`——**硬编码**（与 `--color-danger-strong` 恰好同值） | 若实施方按此去"token 化"该规则，直接违反 G6 |

### 12.2 补上的设计缺口（3 处，v0.1 未识别）

| # | 缺口 | 处置 |
|---|---|---|
| **D1** | `assistant.html` 的 `makeTable()`/`empty()` 是 **DOM API**（`th.className`），不是 HTML 字面量；加数值列需改签名 + **8 个调用点** | 新增 §2.9；范围收敛为**仅空态**（Q-P5S2-5），T-2 排除 assistant，新增 G18 |
| **D2** | T-6「不再出现 `<td colspan class="muted">`」与**禁区页直接冲突**：`admin_users:39/40`（2 处）、`shopify:2`（6 处） | T-6 写明排除清单，否则用例**必然误红** |
| **D3** | 把 dashboard 的禁令写成"函数体与常量字符串不得删改"——过严，会挡住必要的 `<th>` 加 class | 精确化为"仅 2 条整行断言 + 其余子串必须保留"，并列出这 2 条 |

### 12.3 新增裁定与守卫

- **Q-P5S2-5**（assistant 范围）、**Q-P5S2-6**（S1 复核前置）
- **Q-P5S2-1 追加子问题**：斑马纹 `#f7fafe` 与既有 hover `#f8fbff` 几乎同色 → hover 需深一档
- **Q-P5S2-3 选项收窄**：`.empty` 实为零差异，原 −4px 是映射错误
- **Q-P5S2-4 补分工**：`.empty-cell` 与 `.loading-cell` 职责必须分开（v0.1 在 D2.2/§4.2 混用）
- **G17**（format.js 加载顺序）、**G18**（assistant 只改函数体）
- **§10 风险 6**：本片**没有运行时测试**，502 全绿 ≠ 页面可用；强制人工证据

### 12.4 复核方对草案的整体评价

结构完整度高于 P5-S1（含禁区表、已排除选项、风险与回滚），**防范围蔓延写得尤其好**（§9 十二条排除项可直接照用）。主要问题是**事实核对不彻底**——5 处硬错全部集中在"现有多处重复"这类需要跨文件交叉验证的判断上，凡单点 grep 能确认的（`.cs-error` 零定义、token 就位、色值 24、format.js 不存在）全部准确。这也解释了 v1.1 的修订集中在 §2.1/§2.2/§2.4 三节。
