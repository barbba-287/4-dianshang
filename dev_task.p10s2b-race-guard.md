# dev_task.p10s2b —— 利润趋势请求竞态守卫（R-77）

- 版本：v1.0（2026-11-10）
- 基底：`3c41355`（HEAD，工作区干净，全量基线 **708 passed / 0 failed**）
- 性质：**补丁级**。1 个生产文件（1 处函数），1 个新测试文件。**后端零改动、零新迁移、零样式改动**。
- 预期基线：708 → **711**

---

## §1 背景：这是一个已实测复现的缺陷（不是理论风险）

P10-S2 交付了 `loadProfitTrend()`，它和 `load()` / `loadProfitDetail()` 不同，**没有任何请求序号守卫**。

三个请求函数的守卫现状（`app/templates/cockpit.html`）：

| 函数 | 守卫 | 行 |
|---|---|---|
| `load()` | `const sequence=++requestSequence;` + `if(sequence!==requestSequence)return;`（成功路径 1 处 + catch 1 处） | `:68` |
| `loadProfitDetail()` | `const seq=++profitSeq;` + `if(seq!==profitSeq)return;`（成功路径 1 处 + catch 1 处） | `:63` |
| **`loadProfitTrend()`** | **无** | `:69` |

### 实测复现（已由复核方用 Node 探针跑出，非推测）

```
第 1 次：days=7  → 请求耗时 200ms（慢）
第 2 次：days=30 → 请求耗时 10ms （快）
结果：页面 #days = 30
      趋势图 meta = "1 期 · 每期 7 天"     ← 先发的慢响应覆盖了后发的快响应
      x 轴标签 = ["7天-期"]
      setOption 被调用 2 次（两次都画了，最终留在图上的是旧数据）
```

**为什么这是缺陷而不是视觉瑕疵**：页面控件写着「近 30 天」，趋势图实际是 7 天一拍。
财务口径与控件不一致会直接导致误读。且 `renderProfitTrend(data)` 的 note 文案会跟着写成
「每期为 7 天」—— **界面上自相矛盾**。

### 可达路径（说明为什么真实用户会碰到）

`load()` 的 `requestSequence` 守卫只能拦住「驾驶舱响应过期后**再**去发起趋势请求」，
**拦不住已经发出去的趋势请求**：

1. 用户选 `days=7` → `load()` 快速返回 → `void loadProfitTrend()` 发出趋势请求 A（慢，200ms）
2. 用户立刻改选 `days=30` → `load()` 返回 → 发出趋势请求 B（快，10ms）→ B 先渲染「30 天」
3. A 在 200ms 后返回 → **无守卫，直接渲染** → 图变回「7 天」，note 也变回「每期为 7 天」

网络抖动 / 弱网 / 后端窗口越长越慢（`days=90` 要跑 6 次 `profit_totals`）时更容易命中。

---

## §2 唯一改动：`app/templates/cockpit.html`

### 2.1 声明（`:54`）

现状（整行，共 15 字节）：

```js
let profitSeq=0;
```

改成：

```js
let profitSeq=0,profitTrendSeq=0;
```

> 单行内追加，**不新增物理行**。`profitSeq` 的既有语义不动。

> ⚠️ **切片陷阱（务必知道）**：`html.index("function load")` 会先命中 `function loadProfitDetail`，
> 得到错误的函数体。锚点必须写 `"function load("`（带左括号）。任务书 §3 给出的 `_fn_body`
> 已经处理了这一点，**不要自己另写一个不带括号的版本**。

### 2.2 `loadProfitTrend()`（`:69`）

**现状**（整行，逐字符核对后改动，别重新格式化）：

```js
async function loadProfitTrend(){try{const days=Number($('#days').value||7);const response=await fetch(`/api/finance/profit/trend?days=${encodeURIComponent(days)}&points=6`,{credentials:'same-origin'});if(!response.ok){const body=await response.json().catch(()=>({}));throw new Error(body.detail?.message||'利润趋势加载失败');}const data=await response.json();renderProfitTrend(data);}catch(error){if(profitTrendChart){profitTrendChart.dispose();profitTrendChart=null;}chartEmpty('#profit-trend-chart','利润趋势暂时不可用');const meta=$('#cockpit-profit-trend-meta');if(meta)meta.textContent='加载失败';}}
```

**目标**（三处插入，其余字符一律不动）：

```js
async function loadProfitTrend(){const seq=++profitTrendSeq;try{const days=Number($('#days').value||7);const response=await fetch(`/api/finance/profit/trend?days=${encodeURIComponent(days)}&points=6`,{credentials:'same-origin'});if(!response.ok){const body=await response.json().catch(()=>({}));throw new Error(body.detail?.message||'利润趋势加载失败');}const data=await response.json();if(seq!==profitTrendSeq)return;renderProfitTrend(data);}catch(error){if(seq!==profitTrendSeq)return;if(profitTrendChart){profitTrendChart.dispose();profitTrendChart=null;}chartEmpty('#profit-trend-chart','利润趋势暂时不可用');const meta=$('#cockpit-profit-trend-meta');if(meta)meta.textContent='加载失败';}}
```

三处插入点，**逐个交代清楚**：

| # | 位置 | 插入内容 | 为什么必须在这里 |
|---|---|---|---|
| 1 | 函数体 `{` 之后、`try{` 之前 | `const seq=++profitTrendSeq;` | **必须在第一个 `await` 之前**。若放在 `await` 之后，两个并发请求都会在各自 resolve 时才递增，序号顺序反而被打乱，守卫失效。 |
| 2 | `const data=await response.json();` 之后、`renderProfitTrend(data);` 之前 | `if(seq!==profitTrendSeq)return;` | 在 `response.json()` 之后而非 `fetch` 之后：`json()` 也是 await 点。 |
| 3 | `catch(error){` 之后、`if(profitTrendChart)` 之前 | `if(seq!==profitTrendSeq)return;` | **失败路径同样要守**。否则「A 慢且失败、B 快且成功」时，A 的 catch 会把 B 画好的图 `dispose` 掉并改成「利润趋势暂时不可用」。这条最容易漏。 |

### 2.3 明确**不许**改的东西

- `renderProfitTrend()`（`:70`）**一个字符都不许改**。
- `load()`（`:68`）、`loadProfitDetail()`（`:63`）的既有守卫**不许动**、不许"顺手统一"。
  `load()` 里 `profitSeq++;void loadProfitTrend();` 这个顺序也不许动
  （`loadProfitTrend()` 自己会递增 `profitTrendSeq`，天然使旧趋势请求失效，不需要再手动 `++`）。
- `profitTrendChart` 的声明（`:32`）与 `disposeCharts()`（`:36`）不许动。
- 模板仍是长行内联写法，**不许为了可读性拆行/格式化**（会把 T-6/T-7 与 T-20 的字面计数断言打红）。
- 后端（`app/finance.py` / `app/main.py` / `app/cockpit.py`）**零改动**。本缺陷纯前端。
- `<style>` 段保持 hex=0、`rgba(`=0；`<script>` 内色值合计保持 **8**（本片不新增任何颜色）。
- `echarts.init(` 次数保持 **3**、`'wb')` 次数保持 **3**（`tests/test_p5s3_visual_skeleton.py` 的 T-20 会验）。

---

## §3 测试：新建 `tests/test_p10s2b_race_guard.py`

仓库没有 JS 单测框架，**本片不引入**（不新增 node 依赖、不改 `pytest` 配置）。
因此测试走**结构断言**，但每一条都必须**能抓到一种具体的改坏方式**——不做"字符串存在性"空壳断言。

模板是长行内联，用「按函数名切片」的方式取函数体。切片锚点已实测可用，直接用下面这段：

```python
def _fn_body(html: str, name: str) -> str:
    """取顶层 async/sync 函数体（含 async 前缀），按大括号配对。"""
    start = html.index("function " + name + "(")   # ★ 必须带左括号
    if html[start - 6:start] == "async ":
        start -= 6
    brace = html.index("{", start)
    depth = 0
    for i in range(brace, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return html[start:i + 1]
    raise AssertionError(f"{name} 函数体未闭合")
```

### T-R77-1 守卫序号必须声明在函数之外

```python
body = _fn_body(tpl, "loadProfitTrend")
decl = tpl[:tpl.index("function loadProfitTrend")]
assert "profitTrendSeq=0" in decl          # 声明在全局，不在函数内
```

> 抓的坏法：把 `let profitTrendSeq` 写进函数体内 → 每次调用重置为 0，守卫恒真、等于没有。

### T-R77-2 递增必须早于第一个 `await`

```python
inc = body.index("++profitTrendSeq")
assert body.count("++profitTrendSeq") == 1, "递增点必须唯一"
assert inc < body.index("await "), "递增必须在第一个 await 之前"
```

> 抓的坏法：`const seq=++profitTrendSeq;` 放在 `await fetch(...)` 之后 → 并发时序号顺序错乱。

### T-R77-3 成功路径与失败路径各有一处守卫，且在渲染之前

```python
pos = [i for i in range(len(body)) if body.startswith("seq!==profitTrendSeq", i)]
assert len(pos) == 2, f"守卫应恰好 2 处（成功 + catch），实际 {len(pos)}"
render_at = body.index("renderProfitTrend(")
empty_at = body.index("chartEmpty(")
assert pos[0] < render_at, "成功路径的守卫必须在 renderProfitTrend 之前"
assert pos[1] < empty_at, "catch 里的守卫必须在 chartEmpty 之前"
assert body.index("await response.json()") < pos[0], "守卫应在 json() 之后（json 也是 await 点）"
assert body.index("catch(error)") < pos[1], "第 2 处守卫必须在 catch 块内"
```

> 抓的坏法：① 只守成功路径、漏 catch；② 守卫写在 `renderProfitTrend(...)` 之后（渲染完才 return，等于没守）；
> ③ catch 里先 `dispose` 再守（图已经被清掉了）。

### T-R77-4 既有守卫不许被"顺手统一"改坏

```python
assert _fn_body(tpl, "load").count("sequence!==requestSequence") == 2
assert _fn_body(tpl, "loadProfitDetail").count("seq!==profitSeq") == 2
assert tpl.count("void loadProfitTrend()") == 1
```

> 抓的坏法：把三个函数的守卫合并重构成一个公共 `guard()`，或误删 `load()` 的 catch 守卫。

### 3.5 基线自证（我在 `3c41355` 上实跑过，锚点可用）

| 断言 | 修复前 | 修复后应为 |
|---|---|---|
| T-R77-1 `decl 含 profitTrendSeq=0` | **False**（应红） | True |
| T-R77-2 `++profitTrendSeq` 次数 | **0**（应红） | 1 |
| T-R77-3 `seq!==profitTrendSeq` 次数 | **0**（应红） | 2 |
| T-R77-4 `load` 内 `sequence!==requestSequence` | 2（绿） | 2 |
| T-R77-4 `loadProfitDetail` 内 `seq!==profitSeq` | 2（绿） | 2 |
| T-R77-4 `void loadProfitTrend()` | 1（绿） | 1 |

→ **写完测试先跑一遍，前三条必须是红的**。若一开始就全绿，说明切片切错了函数（见 §2.1 的警告）。

---

## §4 必须交付的行为证据（写进 delivery，不许只贴代码）

结构断言能抓"改坏"，但抓不到"守卫写法对、语义仍错"。**要求你自跑一次 Node 探针**，
证明修复前后行为确实反转。放到 `.testtmp/_p10s2b_race_probe.js`（不入库），
delivery 里贴**完整输出**。

场景（固定，别改）：

```
A：days=7  延迟 200ms   ┐ 先后触发，A 在前
B：days=30 延迟 10ms    ┘
记录：最终 #days、最终 meta 文案、x 轴 label、setOption 调用次数、最终 series 数据
```

判据三条，缺一不可：

1. **修复后**：最终 meta 必须是 `"1 期 · 每期 30 天"`，x 轴 `["30天-期"]`，
   即**慢的旧请求被丢弃**（修复前应为 `"1 期 · 每期 7 天"` / `["7天-期"]`）。
2. **修复后**：`setOption` 调用次数应为 **1**（旧请求不再画）；
   修复前是 2。若修复后仍是 2 而最终图对——说明守卫位置在 render 之后，不达标。
3. **回归**：单请求正常路径（只发 days=30）仍渲染 `"1 期 · 每期 30 天"`，
   且 `connectNulls=false`、`animation=false`、无 `areaStyle`（确认没顺手改坏 `renderProfitTrend`）。

> 提示：探针需要 DOM/ECharts stub。最小可用写法——`document.querySelector` 返回带
> `value` / `textContent` / `innerHTML` / `addEventListener` 的假对象，
> `echarts.init` 返回 `{setOption(o){...}, dispose(){...}}`，
> `fetch` 返回受控延迟的 `{ok:true, json:async()=>(...)}`。
> 从模板里按 §3 的 `_fn_body` 思路原样抽 `loadProfitTrend` / `renderProfitTrend` 两个函数，
> **不要手抄重写**（抄写会把被测代码换掉，结论无效）。

---

## §5 验收

| 项 | 要求 |
|---|---|
| 定向 | `tests/test_p10s2b_race_guard.py` **4 passed**（T-R77-1 ~ 4） |
| 全量 | **712 passed / 0 failed**（708 + 4，一次绿；有 flaky 见下） |
| 命令 | `env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe -m pytest -q -p no:cacheprovider` |
| 既有护栏 | `tests/test_p5s1_tokens.py`（T-6/T-7）、`tests/test_p5s3_visual_skeleton.py`（T-20）**全绿**，不许改这两个文件 |
| 反证 | 把 §2.2 的第 3 处插入（catch 守卫）删掉，T-R77-3 必须变红；只删这一处后还原 |

---

## §6 交付说明 `.testtmp/delivery.p10s2b.md` 必填

1. 改动清单：文件 + 行号 + `git diff --stat`
2. **§2.2 三处插入的逐处确认**（贴 `git diff` 片段，证明没有其他字符变动）
3. 全量实测通过数 + 耗时（自跑，贴 pytest 尾行）
4. 定向 4 条实测通过数
5. `.testtmp/_p10s2b_race_probe.js` 的**完整输出**（修复前 / 修复后各一次），对照 §4 三条判据
6. §5 的反证结果（删 catch 守卫 → T-R77-3 红）
7. 未做项 / 偏差（有就写，没有写"无"）
8. 自查：`git log --format="%h [%s]" -1` 的 subject **必须非空**

---

## §7 提交

```bash
git add app/templates/cockpit.html tests/test_p10s2b_race_guard.py dev_task.p10s2b-race-guard.md
git commit -m "fix(P10-S2b): 利润趋势请求加竞态守卫（R-77）"
```

**不要 push。** commit message 必须带 `-m`，禁止裸 `git commit`（本项目已三次空 subject，台账 §52 O-3）。
