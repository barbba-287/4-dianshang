# P10-S1 利润明细分页 + SKU 筛选 + README 过期信息修正

> 状态：**v1.0 待用户审阅**（按老规矩，任务书写完先给人看，不直接开跑）
> 基线：`693 passed / 0 failed`（2026-11-09 复核实测），分支 `feat/warehouse-mvp`，HEAD `612371b`
> 定位：**纯前端 + 文档修正**。零后端改动、零迁移、零新接口、零新依赖。
> 动因（用户原话）：利润明细「页面上找不到、导出里却有」会直接引发用户信任危机。

---

## §0 一句话说清问题

**后端早就能翻页了，前端从来没翻过。**

用户此前接受的「明细单页上限 200 行」是真的，但那是**后端天花板**。实际情况更差：
前端把 `limit=50` 写死了，且从不传 `offset`，所以**用户实际只能看到 50 行**，
第 51 行以后**只能靠导出**才能看到。README 写的「上限 200 行」比现实宽松。

本片要做的不是"提高上限"，而是**让页面能翻到全部**，并给出 SKU 直达通道。

---

## §1 事实基线（已实测，实现方**不必重新调研**，直接按此执行）

### 1.1 后端：分页能力完整，**一个字符都不用改**

| 事实 | 位置 | 说明 |
|---|---|---|
| `limit` 范围 1~200，`offset` ≥0 任意 | `finance.py:49-50`、`:484-487` | `DETAIL_LIMIT_MAX = 200`、`DETAIL_LIMIT_DEFAULT = 50` |
| `total` 是**真实 count**（独立 count 查询） | `finance.py:496-523` | 不是「本页条数」，可直接算总页数 |
| 排序**稳定** | `finance.py:525` | `order_by(ExternalOrder.external_created_at.desc(), ExternalOrderLine.id.desc())` —— **`id.desc()` 是兜底键**，同时间戳的行不会在两次查询间换位置。**这是翻页不重不漏的前提，不许改** |
| 返回结构含 `items/total/limit/offset/page_profit/page_computed` | `finance.py:555-566` | 前端算页码所需的字段**全都有** |
| 端点已有 `limit`/`offset`/`sku_code` 三个 Query 参数 | `main.py:1818-1846` | `limit` 默认 50 上限 200；`sku_code` 命中不到返回空而非 422 |
| 导出是**另一条独立查询**，与列表共用 `filters` 与 `line_amounts` | `finance.py:655-739` | `max_rows=20000`、`limit(max_rows+1)` 探截断、1 条查询 |

#### 1.1.1 Q4/Q6/Q7 已**预验证通过**（2026-11-09，复核方实测，临时用例跑完已删除）

这不是"理论上可行"，是已经跑出来的：

| 预验证 | 实测输出 |
|---|---|
| **Q4 跨页不重不漏** | 120 行 → 三页条数 `[50, 50, 20]`；`external_line_id` 并集 **120**、去重 **120** ✅ |
| **Q7 offset 越界** | `offset=10000` → `items == []`、`total == 3`、**HTTP 200**（不是 422/500）✅ |
| **Q6 本页口径** | 80 行、每行利润 4.50：`limit=50` → `page_profit=225.00`；`limit=200` → `360.00` ✅ |

**结论：后端分页是可信的，本片确实只需改前端。** 若 Q4 当时红了，本片范围就要扩大到修排序——
现在确认不用。

### 1.2 前端：硬编码，无分页

- `cockpit.html:54` `toggleProfitDetail()`：`fetch('/api/finance/profit/lines?days=X&limit=50')` ——
  **`limit=50` 写死、无 `offset`、无翻页**。
- `cockpit.html:55` `renderProfitDetail(data)`：标题已诚实显示「本页 N / total 行」，
  但**没有任何翻页入口** —— 用户知道还有更多，就是看不到。
- `cockpit.html:17` 容器：`<div id="profit-detail" hidden></div>`（在 `#cockpit-profit-panel` 内）。
- 工具函数已就位：`$`（`:18`）、`escapeHtml`（`:28`）。
- `cockpit.html:58` `load()` 末尾会 `detail.hidden=true; detail.innerHTML=''` ——
  **重载驾驶舱会清空明细，但不会重置分页状态**（本片必须补）。

### 1.3 全站**零分页先例**

`grep -rln "pagination|pager|上一页|下一页|page-btn"` 在 `app/templates/`、`app/static/` 下**零命中**。
所以分页条样式必须新建，且必须遵守 1.4 的色值铁律。

### 1.4 两条样式铁律（违反即测试变红）

| 测试 | 断言 | 对本片的约束 |
|---|---|---|
| `test_t6_no_hardcoded_color_in_template_styles`<br>`tests/test_p5s1_tokens.py:181-190` | 10 个模板 `<style>` 内 **hex = 0 且 `rgba(` = 0** | 新样式**只能用 `var(--…)`**。连 `rgba(0,0,0,.2)` 这种都不行，阴影用 `var(--shadow-*)` |
| `test_t7_script_blocks_unchanged`<br>同上 `:193-210` | 10 个模板 `<script>` 内色值**合计 == 22** | **不许在 `<script>` 里新增任何 `#xxxxxx`**（本片不涉及图表，天然满足） |

### 1.5 README 有 6 处**与现状矛盾**（本片顺手修，见 §3 M2）

| 行 | 现文 | 为什么错 |
|---|---|---|
| `:142` | 「明细下钻（上限 200 行）」 | 漏了分页事实；用户实际只能看 50 |
| `:329` | 「R-59 `/login` 主色 `#2563eb` … 改它须先解 P1-1 冻结」 | **R-59 已于 P9-S2（`1d5c4e6`）关闭**；登录页已 token 化，「须先解 P1-1 冻结」的理由也已不成立 |
| `:330` | 「R-68 … 锚定错位」列为未关闭 | **R-68 已于 P9-S3 阶段 A（`3d1db14`）关闭** |
| `:331` | 「R-69 … 需订单级 UPSERT」列为未关闭 | **R-69 已于 P9-S3 阶段 B（`fdded5b`）关闭** |
| `:333` | 「单页上限 200 行，**无全量导出**」 | 导出已于 P9-S1（`f91ed95`）实现，上限 20000 |
| `:334` | 「利润**无环比**」 | 环比已于 P9-S1 实现（同币种、负基数返回 null） |

> 这 6 条（含 R-59）都是"文档比现实更悲观/更乐观"的典型。**用户会照着 README 判断能不能用**，
> 所以它不是美观问题。

### 1.6 测试范式：跨文件复用 fixture 是**既有约定**

`tests/test_p9s1_profit_enhance.py:19-32` 已确立：

```python
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
p7s3 = importlib.import_module("test_p7s3_profit_engine")
ctx = p7s3.ctx
_order = p7s3._order
_snapshot = p7s3._snapshot
_ingest = p7s3._ingest
_profit_lines = p7s3._profit_lines   # 已封装 limit/offset/sku_code 透传
```

**本片沿用这个范式，不要另造一套造数脚手架。**
`_profit_lines(ctx, limit=50, offset=50)` 可直接用（`:192-197` 逐参数拼 query）。

---

## §2 三项设计决策（**用户已裁定，不再讨论**）

| # | 裁定 | 落点 |
|---|---|---|
| **D1** | 交互 = **翻页 + SKU 过滤** | 后端 `offset`、`sku_code` 均已支持 → 本片**纯前端**，零新接口。订单号搜索**不做**（后端无该参数，会撑大范围） |
| **D2** | 单页上限**保持 200**，默认 **50 不变**；用户可在 **50/100/200** 间切换 | 零后端改动。默认 50 不变 → `test_p7s3_profit_engine.py:534` 的 `page_profit == kpi_total` 断言零风险；`F-44`（250 行 → `items` 200）也可原样保留 |
| **D3** | 保留「本页合计」，但**显式标注页码并指向 KPI 总额** | 零后端改动。文案改为「第 k/n 页 · 本页可计算 X 行合计 Y」，并注明「全窗口合计见上方利润卡」。**不新增 `window_profit` 字段**（与 KPI 卡重复） |

---

## §3 改动清单

### M1 `app/templates/cockpit.html` —— 分页 + SKU 筛选（本片主体）

#### M1-1 新增分页状态（放在 `:46` 的 `// ---------- P7-S3 …` 注释之后）

```js
const profitPage={limit:50,offset:0,sku:'',total:0};
let profitSeq=0;
```

- `limit` 初值 50（与后端默认一致）。
- `profitSeq` 是**请求序列号**，防连点/快速翻页造成「后发先至」。

#### M1-2 改造 `toggleProfitDetail()`（`:54`）

```js
async function toggleProfitDetail(){const node=$('#profit-detail');if(!node)return;if(!node.hidden){node.hidden=true;node.innerHTML='';return;}node.hidden=false;node.innerHTML='<div class="empty">正在加载利润明细…</div>';profitPage.offset=0;await loadProfitDetail();}
```

要点：
- 每次**打开**都从第 1 页开始（不做会话记忆，避免"上次翻到第 8 页、这次进来是空的"的困惑）。
- **打开时重置 `offset`，但不清 `limit` 与 `sku`**？—— **不要保留 `sku`**：换了时间窗口后保留旧筛选会让人以为"没数据"。
  → 打开时 `profitPage.offset=0; profitPage.sku='';`，`limit` 保留用户上次选择。

#### M1-3 新增 `loadProfitDetail()`（紧邻 `toggleProfitDetail`）

```js
async function loadProfitDetail(){const node=$('#profit-detail');if(!node)return;const seq=++profitSeq;const days=Number($('#days').value||7);const params=new URLSearchParams({days:String(days),limit:String(profitPage.limit),offset:String(profitPage.offset)});if(profitPage.sku)params.set('sku_code',profitPage.sku);try{const response=await fetch(`/api/finance/profit/lines?${params.toString()}`,{credentials:'same-origin'});const data=await response.json();if(seq!==profitSeq)return;if(!response.ok)throw new Error(data.detail?.message||'利润明细加载失败');profitPage.total=Number(data.total||0);renderProfitDetail(data);}catch(error){if(seq!==profitSeq)return;node.innerHTML=`<div class="empty">利润明细加载失败：${escapeHtml(error.message||'网络错误')}</div>`;}}
```

**关键要求**：

1. **翻页时不要先把表格清成「正在加载…」** —— 那会让输入框丢焦点、界面闪烁。
   loading 占位只在 `toggleProfitDetail` 首次打开时写（M1-2 已做）。
2. `sku_code` 必须**只在非空时** `set`，不要传空串（空串在 SQL 里等价于 `sku_code=''`，命中不到任何行）。
3. 必须用 `URLSearchParams` + `encodeURIComponent` 语义，**不要手工拼字符串**（SKU 可能含特殊字符）。

#### M1-4 改造 `renderProfitDetail(data)`（`:55`）

渲染顺序（**从上到下**）：

1. **头部**：标题 + 导出按钮（保持现有的 `data-profit-export` 两个按钮与 `bindProfitExport` 调用，不动）
2. **筛选 + 分页条**（新增，见下）
3. **表格**（保持现有 7 列与 `num-gain`/`num-loss` 逻辑，不动）
4. **note**（改造文案，见下）

标题文案改造（原「本页 N / total 行」→ 加页码）：

```
利润明细 · 第 ${page} / ${pages} 页 · 本页 ${items.length} 行 / 共 ${total} 行（按订单时间倒序）
```

其中：

```js
const total=Math.max(0,Number(data.total||0));
const size=Math.max(1,Number(data.limit||profitPage.limit));
const offset=Number(data.offset||0);
const pages=Math.max(1,Math.ceil(total/size));
const page=Math.floor(offset/size)+1;
```

分页条 HTML（**必须用 `escapeHtml` 回填输入框**）：

```js
const pager=`<div class="profit-pager">
  <div class="pager-group">
    <input id="profit-line-sku" class="pager-input" type="search" value="${escapeHtml(profitPage.sku)}" placeholder="按 SKU 内部编码筛选" aria-label="按 SKU 内部编码筛选">
    <button class="export-btn import-ghost" type="button" data-pager="filter">筛选</button>
    <button class="export-btn import-ghost" type="button" data-pager="clear">清除</button>
  </div>
  <div class="pager-group">
    <label class="pager-info" for="profit-line-size">每页 <select id="profit-line-size" class="pager-select">${[50,100,200].map(n=>`<option value="${n}"${n===size?' selected':''}>${n}</option>`).join('')}</select> 行</label>
    <button class="export-btn import-ghost" type="button" data-pager="prev"${offset<=0?' disabled':''}>上一页</button>
    <span class="pager-info" id="profit-line-info">第 ${page} / ${pages} 页 · 共 ${total} 行</span>
    <button class="export-btn import-ghost" type="button" data-pager="next"${offset+size>=total?' disabled':''}>下一页</button>
  </div>
</div>`;
```

> 实际落地时**去掉上面 HTML 里的换行与缩进**——本项目模板是单物理行写法（`cockpit.html` 只有 60 行 / 30KB），
> 加换行会让 `git diff` 变成整文件重写，也让后续 `test_t6`/`T-23` 类字面串断言难读。

note 文案改造（D3 的落点）：

```
本页可计算行利润合计 ${page_profit}（共 ${page_computed} 行）；全窗口合计见上方利润卡，切换页码不会改变它。
```

**必须带这句「全窗口合计见上方利润卡，切换页码不会改变它」** —— 这是 D3 的核心：
`page_profit` 是**本页**口径，翻页会变；不加这句，用户会以为「利润总额在缩水」。

#### M1-5 新增 `bindProfitPager(node)`，并并入 `bindProfitExport`

```js
function bindProfitPager(node){node.querySelectorAll('[data-pager]').forEach(button=>{button.onclick=()=>{const action=button.dataset.pager;if(action==='prev')profitPage.offset=Math.max(0,profitPage.offset-profitPage.limit);else if(action==='next')profitPage.offset=profitPage.offset+profitPage.limit;else if(action==='filter'){profitPage.sku=(node.querySelector('#profit-line-sku')?.value||'').trim();profitPage.offset=0;}else if(action==='clear'){profitPage.sku='';profitPage.offset=0;}void loadProfitDetail();};});const sizeNode=node.querySelector('#profit-line-size');if(sizeNode)sizeNode.onchange=()=>{profitPage.limit=Number(sizeNode.value)||50;profitPage.offset=0;void loadProfitDetail();};const skuNode=node.querySelector('#profit-line-sku');if(skuNode)skuNode.onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();profitPage.sku=(skuNode.value||'').trim();profitPage.offset=0;void loadProfitDetail();}};}
```

- 每页行数改变、筛选、清除 → **`offset` 一律归零**（否则会落在不存在的页上）。
- 现有 `bindProfitExport(node)` 是 `node.querySelectorAll('[data-profit-export]')`，**两者互不干扰**，
  在 `renderProfitDetail` 里**两处都调用**（对应现有的两个 `bindProfitExport` 调用点，以及新增的 `bindProfitPager`）。

#### M1-6 空结果的**死锁陷阱**（最容易做错的一处）

| 情形 | 必须渲染 | 绝不能 |
|---|---|---|
| 无筛选且 `total==0` | 筛选行 + 分页条 + `<div class="empty">窗口内没有订单行</div>` | 只渲染 empty |
| **有筛选且 `total==0`** | 筛选行（**输入框保留用户输入**） + `<div class="empty">没有匹配「${escapeHtml(profitPage.sku)}」的订单行</div>` | **只渲染 empty** — 用户将永远无法清除筛选，只能刷新整页 |

**这是本片唯一的"功能性死锁"风险点，必须按上表实现。**

#### M1-7 新增样式（`cockpit.html` `<style>` 段，`:5-15` 之后）

**全部 `var()`，零 hex、零 `rgba(`**（T-6 铁律）：

```css
.profit-pager{display:flex;flex-wrap:wrap;gap:.5rem .8rem;align-items:center;justify-content:space-between;margin:.7rem 0 0}
.pager-group{display:flex;gap:.4rem;align-items:center;flex-wrap:wrap}
.pager-input{padding:.3rem .5rem;font-size:.75rem;border-radius:.4rem;border:1px solid var(--color-border-input);background:var(--color-surface);color:var(--ink);min-width:11rem;min-height:0}
.pager-select{padding:.2rem .3rem;font-size:.75rem;border-radius:.35rem;border:1px solid var(--color-border-input);background:var(--color-surface);color:var(--ink);min-height:0}
.pager-info{font-size:.74rem;color:var(--muted);font-variant-numeric:tabular-nums}
.profit-pager .export-btn[disabled]{opacity:.45;cursor:not-allowed}
```

> `--color-border-input`、`--color-surface`、`--ink`、`--muted` 均已在 `tokens.css` 定义（P9-S2 已核对）。
> **若要新增 token，必须先确认 `tokens.css` 里没有等价项**，不许造近义词。

#### M1-8 `load()` 重置（`:58`）

现有：

```js
const detail=$('#profit-detail');if(detail){detail.hidden=true;detail.innerHTML='';}
```

改为：

```js
const detail=$('#profit-detail');if(detail){detail.hidden=true;detail.innerHTML='';}profitPage.offset=0;profitPage.sku='';profitPage.total=0;profitSeq++;
```

- **`profitSeq++` 必须有**：`load()` 期间若有在飞的明细请求返回，会把旧数据画到已清空的容器里。
- `profitPage.limit` **不重置**（用户选的每页行数应保持）。

#### M1-9 明确**不改**的部分

- `renderProfit`（`:49-53`）、`profitTable`/`profitRow`（`:47-48`）、ECharts 相关（`:20-33`）**一行不动**。
- `exportProfitLines`（`:57`）**不动**：它导的是**全窗口**（只有 `days`），与分页无关，这是正确行为。
  但要在 note 里让用户明白「导出是全窗口、页面是本页」。

### M2 `README.md` —— 修正 6 处过期/不准确信息

| 行 | 改成 |
|---|---|
| `:142` | `GET /api/finance/profit/lines?days=7&sku_code=SKU-A&limit=50&offset=50  # 明细下钻（分页，每页 50/100/200）` |
| `:329`（R-59 行） | **删除该行**（已关闭），或并入已关闭行：「R-59 / R-66 / R-67 / R-71 已关闭」 |
| `:330`（R-68 行） | **删除**，并入已关闭行（P9-S3 阶段 A） |
| `:331`（R-69 行） | **删除**，并入已关闭行（P9-S3 阶段 B） |
| `:333` | 「利润明细页面分页（每页 50/100/200，单页上限 200）；导出为全窗口，上限 20000 行（超出时 `X-Export-Truncated=1`）」 |
| `:334` | 删掉「利润**无环比**」，保留「只读客服角色看不到利润（有意为之，未来单开 `finance.read`）」 |

改完 `README.md` 里**不应再出现**：`无全量导出`、`无环比`、`#2563eb`、`R-68`、`R-69` 作为**未关闭项**。

> 复核方式（写进 delivery）：
> ```bash
> grep -nE "无全量导出|无环比|#2563eb" README.md    # 期望零输出
> grep -n "R-68\|R-69" README.md                     # 期望只出现在"已关闭"行
> ```

### M3 `tests/test_p10s1_profit_paging.py` —— 新建，**7 条**

沿用 §1.6 的复用范式（`importlib.import_module("test_p7s3_profit_engine")`）。

| 编号 | 用例 | 断言要点 |
|---|---|---|
| **Q1** | 前端分页元素齐备 | `cockpit.html` 含 `id="profit-line-sku"`、`id="profit-line-size"`、`id="profit-line-info"`、`data-pager="prev"`、`data-pager="next"`、`data-pager="filter"`、`data-pager="clear"`、`class="profit-pager"` |
| **Q2** | 不再硬编码单页 50 且真的传 `offset` | 模板**不含** `&limit=50`（旧写法）；**含** `offset` 与 `profitPage.limit`；`toggleProfitDetail` 打开时重置 `offset` |
| **Q3** | SKU 输入框回填走 `escapeHtml` | 模板含 `value="${escapeHtml(profitPage.sku)}"` —— 防 XSS（筛选词会回显到 HTML） |
| **Q4** | **跨页不重不漏**（本片最重要的用例） | 造 **120 行**，`limit=50` 依次取 `offset=0/50/100` → `items` 长度 **50/50/20**，`total` 三次恒为 **120**，三页 `external_line_id` 集合**并集 == 120 且无重复**（用 `set` 长度校验） |
| **Q5** | SKU 过滤 + 分页组合 | 造 A 80 行 / B 40 行 → `sku_code=A&limit=50&offset=50` → `items` 30 行且全为 A，`total==80` |
| **Q6** | `page_profit` 是**本页**口径（D3 的口径锁） | 同窗口下 `limit=50&offset=0` 的 `page_profit` **不等于** `limit=200&offset=0` 的 `page_profit`（数据 ≥51 行且利润各不相同时）—— 这条钉死「翻页会变本页合计」这个事实，防未来有人把它当总额用 |
| **Q7** | `offset` 越界 | `offset=10000` → `items == []`、`total` 仍为真实值、HTTP 200（**不许 422/500**） |

**Q4 的实现提示**（造数与断言骨架）：

```python
records = [p7s3._order(order_no=f"ORD-{i}", lines=[{"external_line_id": f"L-{i}", "external_sku": "EXT-SKU-A", "qty": 1, "gross": "10.00"}]) for i in range(120)]
p7s3._ingest(ctx, *records)
seen = []
for offset in (0, 50, 100):
    body = p7s3._profit_lines(ctx, limit=50, offset=offset).json()
    assert body["total"] == 120
    seen.extend(item["external_line_id"] for item in body["items"])
assert len(seen) == 120 and len(set(seen)) == 120
```

> 注意：`_profit_lines` 的签名是 `(ctx, **params)`，`as_of` 缺省用模块常量 `AS_OF`，与造数时间要一致
> —— 照抄 `test_p7s3_profit_engine.py` 里现有用例的 `AS_OF` 用法，不要自己编时间。

**Q4/Q6 会真正暴露排序不稳定的问题**，这正是它的价值：如果 `order_by` 少了 `id.desc()` 兜底，
同一 `external_created_at` 的 120 行在三页之间可能重复或漏掉，`set` 长度立刻 ≠120。

---

## §4 关键陷阱（**逐条对照，都不是直觉能想到的**）

1. **空结果必须保留筛选行**（§M1-6）。只渲染 empty → 用户清不掉筛选 → 只能刷新整页。这是本片唯一的死锁点。
2. **翻页时不要清空容器写「正在加载」**（§M1-3）。输入框丢焦点、界面闪烁；loading 只在首次打开时写。
3. **`profitSeq` 防竞态**（§M1-3/M1-8）。快速连点「下一页」会发出多个请求，返回顺序不确定；没有序列号就会出现「显示第 2 页数据、页码写着第 3 页」。
4. **`sku_code` 空串不要传**。空串是有效值，会让查询命中不到任何行；必须 `if(profitPage.sku)` 才 `set`。
5. **`offset` 归零的四个触发点**：打开明细、改筛选、清除筛选、改每页行数。漏一个就会落在不存在的页上（表现为「明明有 120 行，翻到某页空了」）。
6. **T-6 铁律**：新样式零 hex、零 `rgba(`。阴影用 `var(--shadow-*)`，不要自己写 `rgba(16,24,40,.08)`。
7. **模板单物理行写法**（§M1-4 末尾）。不要为了可读性加换行。
8. **`load()` 里要 `profitSeq++`**，否则在飞请求会把旧数据画进已清空的容器。
9. **`DETAIL_LIMIT_MAX = 200` 是后端硬上限**，前端下拉只能 50/100/200。**不许**为了"一页看完"把它改成 500——那会让 `F-44`（250 行 → `items` 200）变红，且单页 500 行的多表 join 耗时未评估。
10. **`exportProfitLines` 一行不动**：它导全窗口是正确的。不要"顺手"给它加 `sku_code`/`offset` —— 那会改变导出语义（导出应当始终是当前窗口全集）。
11. **不许动后端**：`finance.py`、`main.py` 本片**零改动**。若实现中发现必须改后端，**停下来回报**，不要自行扩范围。

---

## §5 测试与验收

### 5.1 定向

```bash
env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe -m pytest -q -p no:cacheprovider tests/test_p10s1_profit_paging.py
```

期望 **7 passed**。

### 5.2 既有护栏必须仍绿（本片最容易碰红的四条）

```bash
env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe -m pytest -q -p no:cacheprovider \
  tests/test_p5s1_tokens.py tests/test_p5s3_visual_skeleton.py tests/test_p7s3_profit_engine.py tests/test_p9s1_profit_enhance.py
```

重点：

- `test_t6_no_hardcoded_color_in_template_styles` —— 新样式零 hex/rgba
- `test_t7_script_blocks_unchanged` —— `<script>` 内仍 22 处
- `tests/test_p7s3_profit_engine.py:534` `page_profit == kpi_total`（默认 limit 不变 → 应仍绿）
- `tests/test_p9s1_profit_enhance.py` 的 `F-44`（250 行 → `items` 200）

### 5.3 全量

```bash
env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe -m pytest -q -p no:cacheprovider
```

预期 **≥ 700 passed / 0 failed**（693 + Q1~Q7 共 7 条）。
**有 flaky 需重跑一次；重跑全绿即过，但必须在 delivery 登记是哪条。**
已知观察项：`tests/test_api_jobs.py::test_enqueue_returns_job_id_immediately`（台账 §51 O-1，再复现 2 次升正式编号）。

### 5.4 delivery 必填项

写到 `.testtmp/delivery.p10s1.md`：

1. 改动文件清单 + 行号；
2. **Q4 三页的 `external_line_id` 实际条数**（50/50/20）与并集去重后的数量（120）—— 贴数字，不要只写「通过」；
3. **空结果场景的实测截图级描述**：给一个不存在的 SKU，确认筛选行**仍在**（这是死锁点的自证）；
4. README 六处改动的 `grep` 前后对照（`无全量导出|无环比|#2563eb` 期望零输出）；
5. 全量实测通过数 + 耗时；
6. 未做项与偏差（有就写，没有写「无」）。

---

## §6 红线

1. **后端零改动**：`app/finance.py`、`app/main.py`、`app/schemas.py` **不得出现在 `git diff --stat` 里**。
2. **零迁移**：`app/db.py`、`migrations/` 不得出现。
3. **零新依赖**：不引入分页库、不引 UI 框架。
4. **模板 `<style>` 零 hex / 零 `rgba(`**（T-6）。
5. **`<script>` 内色值仍 == 22**（T-7），不许新增色值。
6. **排序不许改**：`finance.py:525` 的 `order_by(...)` 一个字符不动。
7. **`DETAIL_LIMIT_MAX`/`DETAIL_LIMIT_DEFAULT` 不许改**。
8. **`exportProfitLines` 不许改语义**（仍导全窗口）。
9. **不改 10 个模板的其它任何页面**，只动 `cockpit.html`（+ `README.md`）。
10. **不许把 `page_profit` 当总额展示**，必须保留「全窗口合计见上方利润卡」这句限定。

---

## §7 交接命令

```bash
cd /d/ai/dianshang && claude --permission-mode acceptEdits "读 dev_task.p10s1-profit-paging.md 全文，严格按其中每一条实施。本片是纯前端 + 文档修正：只改 app/templates/cockpit.html 与 README.md，新增 tests/test_p10s1_profit_paging.py。后端 app/finance.py、app/main.py 一个字符都不许动。特别注意 §4 的 11 条陷阱，尤其是第 1 条（空结果必须保留筛选行，否则用户清不掉筛选）和第 3 条（profitSeq 防竞态）。模板是单物理行写法，不要加换行。新样式全部用 var()，零 hex 零 rgba（否则 test_t6 变红）。完成后：1) 把交付说明写到 .testtmp/delivery.p10s1.md（含 §5.4 的六项必填内容）；2) 自跑定向、§5.2 的四个护栏文件、全量回归，把实测通过数写进交付说明；3) 提交到当前分支，不要 push。"
```

---

## §8 本片明确**不做**的事

| 不做 | 理由 |
|---|---|
| 订单号搜索 | 后端无该 query 参数，需改 SQL；用户已裁定本轮纯前端 |
| 提高单页上限到 500+ | 会让 `F-44`（200）断言变红，且多表 join 单页 500 行耗时未评估 |
| 新增 `window_profit` 字段 | 与 KPI 卡重复（D3 已裁定靠文案解决） |
| 明细页内排序（点列头排序） | 排序键在 SQL 层，需新参数 + 稳定序保证；属下一片 |
| 明细页虚拟滚动 | 每页最多 200 行，无必要 |
