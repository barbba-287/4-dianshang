# 任务文件：P5-S1 设计 Token 与全局样式收敛

> 切片：P5 第一阶段（UI 界面美化）｜性质：**纯前端**，零后端逻辑改动
> 依据：`docs/ui-reference-checklist.md` v5 §1.5（设计 token）、§1.4（空态）、§1.3（图表规范不在本轮）
> 基线：**486 passed / 0 failed**（P4 收口后，复核方提交态实测 277.73s）→ 本片目标 **494 passed / 0 failed**
> 状态：**已定稿 v1.0（2026-11-01，四条裁定已回填 §3），可开跑**

---

## 0. 结论摘要（先看这 10 条）

1. **模板是 10 个，不是 8 个。** `/cockpit`（P4 新增）与 `admin_users` 均在列，实测清单见 §1.1。
2. **项目没有 Jinja2。** `_render_workspace_page`（`app/main.py:328-338`）是「读文件 + 正则替换 `<aside class="sidebar">`」，
   没有 `TemplateResponse`、没有模板继承。→ **`base.html` + 继承方案在本仓库不可用**，
   要落地只能引入 Jinja2 并改渲染函数（＝改后端，违反本片约束）。本片改用 §D2 的 link 注入。
3. **仓库里已经存在两套并行的 CSS 变量体系**：
   - `--shell-*`（8 个）：定义在 `app/static/workspace-shell.css:1`，被全局组件库使用；
   - `--page / --panel / --ink / --muted / --line / --brand / --brand-soft / --good / --warning / --critical / --shadow`（11 个）：
     只在 3 个模板的局部 `:root` 里定义，且三处**不一致**（见 §1.2）。
4. **7 个模板引用 `var(--brand)` 等变量但自己没定义**，浏览器按「变量未定义 → 该声明失效」处理。
   这是当前最实质的缺陷：**页面看着有样式，实际边框/颜色已经静默失效**。收敛之后这些引用才第一次真正生效。
5. `--danger` 被引用 1 次（`index.html`），**全仓零定义**。
6. 全局 CSS 已是 82 条规则的组件库（`.panel/.card/.tag/.table/.step/button/.muted/.status.*` + 响应式），
   而各模板又各自重定义了一遍同类组件，内联 `<style>` 合计 **26,895 字符**、style 内硬编码色值 **180 处**。
7. **测试红线（必须遵守，否则 486 立刻挂）**：`tests/test_api_dashboard.py:214-218` 用**无空格字面串**断言
   `workspace-shell.css` 含 `position:fixed`、`height:100vh`、`overflow-y:auto`、`margin-left:232px`、`margin-left:76px`。
   一旦把这些声明格式化（加空格）或改为 `var()`，测试直接红。
8. **零后端改动**：`app/main.py` 一行不动，包括 `_render_workspace_page` 与 `login_page` 的内联 HTML（`app/main.py:497`）。
9. `<script>` 块内的色值（**24 处**）**不在本片范围**——那是 ECharts 图表配色，属 §1.3 图表规范（后续逐页）。
   本片只收敛 `<style>` 块内的 180 处。
10. **四条裁定已拍板（2026-11-01，见 §3）**：① 验收口径 `494 passed`（既有 486 + 新增 8）；
    ② **坚决不引外部字体**（系统栈 + `tabular-nums`）；③ `/login` 主色漂移本期不动，登记 **R-59**；
    ④ 圆角 `rem→px` 归一接受 ≤1.5px 差异。**实施时不得再就此提问或自行变通。**

---

## 1. 现状盘点（Fact，全部实测，附复核命令）

### 1.1 模板与内联样式清单

| 模板 | 字节 | `<style>` 字符 | style 内硬编码色值 | script 内色值 | 有 `:root` |
|---|---:|---:|---:|---:|---|
| `index.html`（`/ops`） | 42,101 | 1,791 | 3 | 0 | 无 |
| `dashboard.html`（`/dashboard`） | 38,866 | 7,683 | 35 | 13 | **有**（:5） |
| `cockpit.html`（`/cockpit`） | 24,022 | 6,251 | 47 | 10 | **有**（:4） |
| `customer_service.html` | 21,909 | 4,389 | 32 | 1 | **有**（:9，只定义 4 个变量） |
| `admin_users.html` | 14,299 | 2,133 | 9 | 0 | 无 |
| `content.html` | 14,214 | 1,503 | 19 | 0 | 无 |
| `assistant.html` | 13,878 | 900 | 5 | 0 | 无 |
| `orders.html` | 9,904 | 1,246 | 18 | 0 | 无 |
| `warehouse.html` | 7,148 | 192 | 0 | 0 | 无 |
| `shopify.html` | 5,694 | 807 | 12 | 0 | 无 |
| **合计** | **192,035** | **26,895** | **180** | **24** | 3 个 |

> 复核命令（勿用 `wc -l`，本仓库模板为长行内联写法，行数无意义）：
> ```bash
> wc -c app/templates/*.html
> python - <<'EOF'
> import re, pathlib
> pat=re.compile(r'#[0-9a-fA-F]{3,8}\b|rgba?\(')
> for p in sorted(pathlib.Path('app/templates').glob('*.html')):
>     s=p.read_text(encoding='utf-8')
>     st=re.findall(r'<style>(.*?)</style>', s, re.S); sc=re.findall(r'<script[^>]*>(.*?)</script>', s, re.S)
>     print(p.name, sum(len(pat.findall(x)) for x in st), sum(len(pat.findall(x)) for x in sc))
> EOF
> ```

### 1.2 变量体系断裂（本片存在的理由）

| 变量 | 谁在引用 | 谁在定义 | 结论 |
|---|---|---|---|
| `--page/--panel/--ink/--muted/--line/--brand/--brand-soft/--good/--warning/--critical/--shadow` | 全站（`--brand` 9 次、`--muted` 32 次、`--line` 13 次、`--ink` 7 次…） | **仅** `cockpit.html:4`、`dashboard.html:5`（两处逐字节相同） | 其余 **8 个模板全部悬空** |
| 同上 4 个（`--line/--muted/--ink/--brand`） | `customer_service.html` 自身 | `customer_service.html:9`（**只定义 4 个**，缺 `--panel/--page/--good/--warning/--critical/--shadow/--brand-soft`） | 部分悬空 |
| `--danger` | `index.html`（1 次） | **全仓无定义** | 悬空 |
| `--shell-page/--shell-panel/--shell-ink/--shell-muted/--shell-line/--shell-brand/--shell-nav/--shell-shadow` | `workspace-shell.css` 全局组件库 | `workspace-shell.css:1` | 正常，但**与上一套语义重叠、值重复维护** |

> 复核命令：
> ```bash
> grep -l ":root" app/templates/*.html          # → 只有 cockpit / customer_service / dashboard
> grep -ohE "var\(--[a-z0-9-]+" app/templates/*.html | sed 's/var(//' | sort | uniq -c | sort -rn
> grep -rn -- "--danger:" app/                  # → 空
> ```

**实证含义**：`index.html` 里 `border:1px solid var(--line)` 这类声明当前**从未生效过**（`--line` 未定义 → 声明在计算值阶段被丢弃）。
因此本片不是「换个色」，而是**把一套只在 3 个页面生效的变量，第一次真正铺到全站**。
→ 视觉上必然有变化（边框出现、色块着色），这是**修复**而非回归，需在交付说明中逐页列出「首次生效的声明」。

### 1.3 渲染机制（决定注入方式）

```python
# app/main.py:328-338（本片不得修改）
def _render_workspace_page(template_name: str, principal, active_path: str) -> HTMLResponse:
    template = (Path(__file__).parent / "templates" / template_name).read_text(encoding="utf-8")
    navigation = _visible_navigation(principal, active_path)
    template = re.sub(r'<aside class="sidebar">.*?</aside>', f'<aside class="sidebar">{navigation}</aside>',
                      template, count=1, flags=re.DOTALL)
    return HTMLResponse(content=template, headers={"Cache-Control": "no-store, max-age=0"})
```

- 无模板引擎、无继承、无 include。**每个模板必须自带 `<html><head>…<body>` 全骨架**。
- 唯一被服务端改写的片段是 `<aside class="sidebar">…</aside>`（导航）。**改样式时不得改动该标记的存在形式**。
- `/login` 页面是 `app/main.py:497` 的 f-string 内联 HTML，**不在本片范围**（改它＝改后端）。

### 1.4 全局组件库现状（`workspace-shell.css`，5,956 字符 / 82 条规则）

已具备：`:root`(8 变量)、`*`、`.shell`、`.sidebar`、`.brand`、`.nav*`（含 details/summary 折叠与锚点子项）、
`.main`、`.topbar`、`.content`、`.page-heading`、`.panel`、`.panel-head`、`.panel-subtitle`、`.muted`、`.row`、`.field`、
`input/select/button`、`button.secondary`、`button.danger`、`button:disabled`、`.actions`、`.visually-hidden`、
`.status(.error/.success/.warning)`、`.table-wrap`、`table/th,td/th`、`.tag`、`.empty`、`.step`、`.cards`、`.card`、
`section[id]` 锚点偏移、以及 `@media(max-width:900px|600px)` 两档响应式。

→ **意味着**：大部分页面内联样式是**重复定义**，可以直接删掉并复用全局规则；任务不是「从零建体系」。

---

## 2. 设计决定（D1–D8）

### D1 Token 落点为单一文件：`app/static/css/tokens.css`

- 唯一真源 = **语义层**（下表）。所有页面与组件只能引用语义层或兼容别名，**不得再写裸色值**。
- 通过既有 `/static` 挂载访问（`app/main.py:243` 已挂载 `StaticFiles`），无需新增路由。

### D2 注入方式：`<link>` 显式引入，不用 `@import`，不改渲染函数

在每个模板 `<head>` 内、**`workspace-shell.css` 之前**插入：

```html
<link rel="stylesheet" href="/static/css/tokens.css">
```

**为什么不选另外两条路**：
- `@import url("/static/css/tokens.css")` 写在 `workspace-shell.css` 首行：改动面最小（1 个文件），
  但 `@import` 会串行阻塞后续样式解析，且把「页面依赖」藏进 CSS 里，排查困难。**已排除**。
- 改 `_render_workspace_page` 由服务端统一注入 `<link>`：改后端，**违反本片约束，已排除**。

### D3 三层结构（职责边界，实施时不得越层）

| 层 | 文件 | 只放什么 |
|---|---|---|
| L1 Token 层 | `app/static/css/tokens.css`（**新建**） | `:root` 变量定义 + 极少量全局重置（`html`/`body` 字体与背景）。**不含任何组件样式** |
| L2 组件层 | `app/static/css/workspace-shell.css`（**改 `:root` 值来源**，其余规则不动） | 全站组件类（`.panel/.card/.tag/table/button/.status` 等） |
| L3 页面层 | 各模板 `<style>`（**大幅瘦身**） | 仅该页独有的布局微调；**不得再出现色值字面量与 `:root` 定义** |

### D4 收敛范围：只动 `<style>` 块，`<script>` 块零改动

- 目标：`<style>` 内硬编码色值 **180 → 0**（hex 归零；`rgba()` 仅允许出现在 `tokens.css` 的 `--shadow`/半透明 token 中）。
- **`<script>` 内 24 处色值一律不动**（cockpit 10 / dashboard 13 / customer_service 1）：
  那是 ECharts 配色（`const colors={blue:'#2a78d6'…}`）与图表 series 着色，属 §1.3 图表规范，后续切片处理。
  本片改它会直接破坏图表且绕过 P-11 断言保护范围。
- **三角色删除**：`cockpit.html:4`、`dashboard.html:5`、`customer_service.html:9` 的 `:root` 块**整块删除**
  （值已收敛进 `tokens.css`；模板层再定义＝双源，违反 §1.5「唯一来源」）。

### D5 色板（值全部取自现状，不发明新色）

**语义层**

| Token | 值 | 来源 / 语义 |
|---|---|---|
| `--color-primary` | `#2a78d6` | 现状 `--brand` / `--shell-brand` |
| `--color-primary-hover` | `#1f63b5` | 主色按 -12% 明度推导（**新增**，仅用于 `:hover`，需在交付说明标注推导依据） |
| `--color-primary-soft` | `#eaf2fc` | 现状 `--brand-soft` |
| `--color-success` | `#0ca30c` | 现状 `--good` |
| `--color-warning` | `#d58b00` | 现状 `--warning` |
| `--color-danger` | `#d03b3b` | 现状 `--critical`（**同时补齐悬空的 `--danger`**） |
| `--color-info` | `#2a78d6` | 复用主色（现状无独立 info 色，避免新造色相） |
| `--color-bg` | `#f4f7fb` | 现状 `--page` / `--shell-page` |
| `--color-surface` | `#ffffff` | 现状 `--panel` / `--shell-panel` |
| `--color-border` | `#e4eaf2` | 现状 `--line` / `--shell-line` |
| `--color-text` | `#142033` | 现状 `--ink` / `--shell-ink` |
| `--color-text-muted` | `#69778b` | 现状 `--muted` / `--shell-muted` |
| `--color-nav-bg` | `#14243a` | 现状 `--shell-nav`（仅侧栏） |

**中性色阶**（`--neutral-0` … `--neutral-900`，11 档；值必须落在现状已出现过的灰阶上，禁止新造色相）

| 档 | 值 | 现状出处 |
|---|---|---|
| `--neutral-0` | `#ffffff` | `.panel` 背景 |
| `--neutral-50` | `#fbfcfe` | `index.html` `.step` 背景 |
| `--neutral-100` | `#f4f7fb` | `--page` |
| `--neutral-200` | `#edf2f7` | `.tag` 背景（7 个文件命中，实测存在） |
| `--neutral-300` | `#e4eaf2` | `--line` |
| `--neutral-400` | `#cbd5e1` | `login` 输入框边框（`main.py:497`，**只取值不改文件**） |
| `--neutral-500` | `#9fb2c8` | `workspace-shell.css` `.nav-sub` 文字 |
| `--neutral-600` | `#69778b` | `--muted` |
| `--neutral-700` | `#1e3a5f` | 现状阴影色基（4 个文件命中） |
| `--neutral-800` | `#14243a` | 现状 `--shell-nav`（侧栏底色） |
| `--neutral-900` | `#142033` | `--ink` |

### D6 字号 / 行高 / 间距 / 圆角 / 阴影

```
字号（7 级，不超 7）：--text-xs:12px  --text-sm:13px  --text-base:14px  --text-md:16px
                      --text-lg:20px  --text-xl:24px   --text-2xl:32px
行高（2 级）        ：--leading-tight:1.4（表格/密集）  --leading-normal:1.6（正文）
间距（4 的倍数）    ：--space-1:4px  --space-2:8px  --space-3:12px  --space-4:16px  --space-6:24px  --space-8:32px
圆角（4 级）        ：--radius-sm:4px  --radius-md:8px  --radius-lg:12px
                      --radius-pill:999px（现状 999px 5 处 / 99px 4 处，统一取 999px）
阴影（2 级）        ：--shadow-card:0 8px 24px rgba(30,58,95,.05)   --shadow-overlay:0 16px 40px rgba(30,58,95,.16)
字体                ：--font-sans: system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif
                      --font-mono: ui-monospace,SFMono-Regular,Menlo,Consolas,monospace
```

- **圆角归一说明**：现状大量使用 `.45rem/.55rem/.6rem/.7rem`（16px 基数 ≈ 7.2/8.8/9.6/11.2px）。
  归一到 4/8/12 后会有 **±1.5px 以内**的视觉差异，属允许范围（见 §3 Q-P5S1-4）。
- **金额等宽**用 `font-variant-numeric: tabular-nums`，**不需要**引入字体文件（§1.6 要求「数字等宽」由此满足）。

### D7 字体：系统字体栈，不引入外部字体

理由：本仓库为私有部署后台，外链字体在内网/离线环境会阻塞首屏；中文字体包体积达数百 KB～数 MB，
与「保持原生轻量」冲突。**结论：`--font-sans` 用系统栈（现状即 `system-ui`），`--font-mono` 仅用于金额/代码**。
**用户 2026-11-01 已裁定：坚决不引外部字体**（§3 Q-P5S1-2）。本片**不产生任何 `@font-face`、不新增任何字体文件、不引用任何字体 CDN**；
数字等宽通过 `font-variant-numeric: tabular-nums` 实现，不需要字体资源。

### D8 涨跌色：**本片不定义**

依据 `docs/ui-reference-checklist.md` §1.6：系统面向群体未确认，涨跌色方向写反会导致全站语义反向。
**token 中不得出现任何"涨/跌"语义变量**（如 `--color-up/--color-down`）。
状态表达只用 `success/warning/danger` 三色，**不用于表达价格涨跌**。

---

## 3. 裁定记录（2026-11-01 用户已拍板，实施按此为准，勿再议）

| # | 问题 | 裁定 | 对实施的影响 |
|---|---|---|---|
| **Q-P5S1-1** | 测试数：本片新增 T-1..T-8 会使总数超过 486 | **接受 `494 passed`（既有 486 + 新增 8）**。用户原话：「零后端改动不等于零测试新增」 | 硬闸门 = **既有 486 全绿 + 新增 8 条全绿 + 0 failed**；§6 用例表**全额生效**，不得降级为人工验收 |
| **Q-P5S1-2** | 是否引入 Google Fonts / 本地字体 | **坚决不引**（原话：「内网环境外链字体是致命的」）。用系统栈 + `font-variant-numeric: tabular-nums` | D7 定死；`tokens.css` 与所有模板**不得出现任何 `@font-face` 或 `fonts.` / `googleapis` 外链**（G4 扩展覆盖）；不需要字体文件 |
| **Q-P5S1-3** | `/login` 主色 `#2563eb` 与全站 `#2a78d6` 漂移 | **本期不动**，严守零后端红线；登记缺陷 **R-59** 留待后续切片 | `app/main.py` 仍为禁区（G1）；`tokens.css` **不得**为迁就该页而新增 `#2563eb` 相关 token |
| **Q-P5S1-4** | 圆角 `rem → px` 归一产生 ≤1.5px 视觉差异 | **接受**（原话：「视觉统一比绝对尺寸更重要」） | 圆角统一用 `--radius-sm/md/lg`，沿 D6 的四级方案，无需保留 rem 兼容值 |

> 裁定结论已同步至 §0 摘要、D7、§6、§9；缺陷 R-59 已登记在 `docs/review-checklist-20261011.md`。

---

## 4. 改动清单

### 4.1 新建

| 文件 | 内容 | 预估 |
|---|---|---|
| `app/static/css/tokens.css` | D5 色板 + D6 尺度 + 兼容别名 + 极简重置 | ~150 行 |
| `tests/test_p5s1_tokens.py` | T-1..T-8 契约用例 | ~220 行 |

**`tokens.css` 必须包含兼容别名层**（`docs/ui-reference-checklist.md` §1.5 明确允许「保留兼容别名并标注」）：

```css
/* ===== 兼容别名（deprecated）：仅为既有引用保留，禁止新增引用 ===== */
--page: var(--color-bg);          --panel: var(--color-surface);
--ink: var(--color-text);          --muted: var(--color-text-muted);
--line: var(--color-border);       --brand: var(--color-primary);
--brand-soft: var(--color-primary-soft);
--good: var(--color-success);      --warning: var(--color-warning);
--critical: var(--color-danger);   --danger: var(--color-danger);   /* 补齐悬空 */
--shadow: var(--shadow-card);
```

> 别名是**必需**的，不是偷懒：全站 `--brand` 9 处、`--muted` 32 处、`--line` 13 处、`--ink` 7 处引用，
> 逐个改名等于 60+ 处高风险替换，收益为零。

### 4.2 修改（10 个模板 + 1 个全局 CSS）

| 文件 | 改动 | 限制 |
|---|---|---|
| 10 个 `app/templates/*.html` | ① `<head>` 内加 `tokens.css` 的 `<link>`（在 `workspace-shell.css` **之前**）；② `<style>` 内色值→`var(--color-*)`；③ 删除与 L2 重复的组件规则；④ 删除 3 处 `:root` | `<script>` 块零改动；DOM 结构（id/class/区块顺序）零改动 |
| `app/static/workspace-shell.css` | 仅改 `:root`：8 个 `--shell-*` 的值改为 `var(--color-*)` 等语义层别名 | **其余 81 条规则一个字符都不改**（见 G2） |

### 4.3 禁区（明确不动）

- `app/main.py`（含 `_render_workspace_page:328-338`、`login_page:497`）—— **禁止任何修改**
- `app/*.py` 全部、`app/rag/**`、`db.py`、`migrations/**`、`app/demo_seed.py`
- 所有 `<script>` 块（含 `app/static/labels.js`、`echarts.min.js`）
- `tests/` 下**既有**任何文件（含 `test_api_dashboard.py:214-218`、`test_p4_cockpit.py:731-770`）
- 页面 DOM 结构、路由、导航项、锚点 id

---

## 5. 守卫与红线（实施方逐条自检，复核方逐条实测）

| # | 守卫 | 实测方法 |
|---|---|---|
| **G1** | 零后端改动 | `git diff --name-only` 与 `app/*.py`、`migrations/`、`db.py` **零交集** |
| **G2** | `workspace-shell.css` 五个字面串仍在 | `grep -c "position:fixed\|height:100vh\|overflow-y:auto\|margin-left:232px\|margin-left:76px"` 各 ≥1（**无空格写法，不得格式化**） |
| **G3** | `<script>` 块零改动 | 10 个模板 script 内 `#[0-9a-f]{3,8}\|rgb(` 计数合计仍为 **24**；`git diff` 不得出现 script 段变更 |
| **G4** | 不引入外部 UI 框架 / 外部字体 | 全仓无 `tailwind` / `bootstrap` / `cdn.` / `unpkg` / `jsdelivr` / `@font-face` / `googleapis` / `fonts.` |
| **G5** | 既有 class 名不得删除 | 实施前快照 `workspace-shell.css` 选择器集合，实施后为**超集**（允许新增，禁止删除） |
| **G6** | 模板 DOM 结构不变 | 10 个模板的 `id="..."` 集合、`class="..."` 属性集合为超集；`<aside class="sidebar">` 仍存在且唯一 |
| **G7** | P4 的 D8 断言仍绿 | `tests/test_p4_cockpit.py::test_p11_chart_empty_state_contract` 通过（`chartEmpty` 仍早于 `echarts.init`） |
| **G8** | 无 `:root` 残留于模板 | `grep -c ":root" app/templates/*.html` 全为 0（`color-scheme` 可保留在 `tokens.css`） |
| **G9** | 无新增页面/路由 | `grep -c "@app\." app/main.py` 不变 |
| **G10** | 无构建步骤 | 不新增 `package.json` / `*.scss` / `postcss.config`；`tokens.css` 为可直接服务的纯 CSS |
| **G11** | token 值「取自现状」 | 除 `--color-primary-hover`、`--shadow-overlay` 的 alpha 两个**推导值**（须在交付说明标注推导依据）外，每个色值都必须能在改动前的仓库 `grep` 到。验证：`for c in <值列表>; do grep -ril "$c" app/ ; done` 均有命中 |
| **G12** | 硬编码色值收敛达标 | 10 个模板 `<style>` 内 hex **= 0**；`rgba(` 仅在 `tokens.css` |
| **G13** | **表格可读性零改动** | §1.6（数字右对齐 / 等宽）属「P3 第一轮后逐页」，**不在本片范围**。`table`、`th,td`、`.table-wrap` 的 `padding` / `text-align` / `font-*` 一律不改（只允许把色值换成 token） |
| **G14** | 空态样式仍可用 | `.empty` 定义保留在 `workspace-shell.css`；**`.chart-empty` 定义必须留在 `cockpit.html` 的 `<style>` 内**——`test_p4_cockpit.py::test_p11` 断言的是模板文本含 `".chart-empty"`，一旦迁到 `tokens.css` 该用例立刻红 |

---

## 6. 用例设计（T-1..T-8，基线 486 → 494）

| # | 用例 | 断言要点 |
|---|---|---|
| **T-1** | token 完整性 | `tokens.css` 存在且含：语义色 13 个、`--neutral-0..900` 11 档、字号 7 个、行高 2 个、间距 6 个、圆角 4 个、阴影 2 个、字体 2 个 |
| **T-2** | **零悬空引用**（核心） | 解析 `tokens.css` + `workspace-shell.css` 得到已定义变量集合；扫描 10 个模板所有 `var(--x)`；断言**全部已定义**。该用例在实施前必然失败（现状缺陷），实施后必须绿 |
| **T-3** | 单一来源 | 3 处模板 `:root` 已删除（`grep -c ":root" app/templates/*.html` 全 0）；`--color-primary` 在 `tokens.css` 中**恰好定义 1 次** |
| **T-4** | 注入完整且有序 | 10 个模板都含 `tokens.css` 的 `<link>`，且其位置**早于** `workspace-shell.css` 的 `<link>` |
| **T-5** | 既有测试红线回归 | `workspace-shell.css` 含 G2 的 5 个字面串（本条作为「防格式化」守卫固化，避免后续有人美化 CSS 时无告警） |
| **T-6** | 色值收敛 | 10 个模板 `<style>` 内 `#[0-9a-f]{3,8}` 计数 **= 0**；`rgba(` 计数 = 0（均迁入 token） |
| **T-7** | 脚本零回归 | 10 个模板 `<script>` 内色值合计 **= 24**（与实施前一致）；`echarts.init` 出现次数不变 |
| **T-8** | 语义一致 | `--brand`/`--shell-brand` 均解析到 `--color-primary`；`--danger` 已定义且 = `--color-danger`；`--line`/`--shell-line` 均 = `--color-border` |

> Q-P5S1-1 已裁定 **接受 494 passed**（既有 486 + 新增 8），故本表**全额生效**，不接受降级为人工验收；
> 交付说明须逐条列出 T-1..T-8 的实测结果（绿 / 红 / 未执行，未执行须说明原因）。

---

## 7. 与 UI 清单 §1.5 验收口对照

| 清单要求 | 本片对应 | 达成判据 |
|---|---|---|
| token 文件落库 | `app/static/css/tokens.css` | T-1 |
| 唯一来源 | 3 处模板 `:root` 删除 | T-3 |
| 任意页面不出现硬编码色值（抽查 3 个文件，允许兼容别名并标注） | `<style>` hex 归零 | T-6 + G12 |
| 兼容别名标注 | `tokens.css` 内 `/* deprecated */` 注释段 | 人工核 |
| 最小集合齐备（颜色/字号/行高/间距/圆角/阴影） | D5 + D6 | T-1 |

---

## 8. 执行与验证要求（实施方必读）

1. **开工前实跑基线**：`env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe -m pytest -q -p no:cacheprovider` → 必须 `486 passed / 0 failed`。不绿不开工，先报告。
2. **R-55 节奏**：每完成一个文件立刻落盘，禁止在内存里攒一批改动再一起写。
3. **顺序建议**：`tokens.css` → `workspace-shell.css` 的 `:root` → 逐个模板（先 `warehouse.html`（0 色值，最小验证）→ `index.html` → 其余）→ `tests/test_p5s1_tokens.py`。
4. **逐页自查**：每改完一个模板，确认该页 `<script>` 未被误改（`git diff` 中 script 段无变化）。
5. **完成后**：把交付说明写到 `.testtmp/delivery.p5s1.md`，必须包含：
   - 改动文件清单 + 行号；
   - **每页「首次生效的声明」清单**（因为变量此前悬空，收敛后会真正生效，需逐页说明视觉变化）；
   - T-1..T-8 与既有 486 的实测通过数；
   - 未做项与偏差。
6. **提交到当前分支，不要 push。** 停下等复核。

---

## 9. 已排除的选项（禁止重新试错）

| 选项 | 为什么排除 |
|---|---|
| 引入 Jinja2 + `base.html` 继承 | 项目无模板引擎，引入需改 `_render_workspace_page` 与依赖 → 违反零后端；且 10 个模板都要改成 `{% extends %}`，风险与收益倒挂 |
| `@import` 在 `workspace-shell.css` 首行注入 | 串行阻塞；依赖隐藏在 CSS 内部；本片采用显式 `<link>` |
| 改 `_render_workspace_page` 服务端注入 `<link>` | 改后端 |
| 引入 Tailwind / Bootstrap / 任何 CDN | 用户明令禁止；且与「无构建步骤」冲突 |
| 全局改名 `--brand` → `--color-primary`（60+ 处替换） | 高风险零收益；用兼容别名 |
| 让 `<script>` 里 ECharts 配色读 CSS 变量 | 属 §1.3 图表规范，后续切片；本片改会破坏图表且无断言保护 |
| 给所有页面加骨架屏 / 重排布局 | §1.4 空态与 §1.1 布局是后续切片（P5-S2/S3） |
| 修改 `tests/test_api_dashboard.py` 的 5 条断言以「配合」格式化 | 测试是既有契约，不得为改样式而改测试 |
| 顺手修 `/login` 主色不一致 | 需改 `main.py`，违反零后端；**已裁定本期不动**（Q-P5S1-3），缺陷登记为 **R-59** 留待后续切片 |
| 引入 Google Fonts / 任何字体文件 | **已裁定坚决不引**（内网环境外链字体是致命的，Q-P5S1-2）；等宽用 `tabular-nums` 即可 |

---

## 10. 预期风险与回滚

- **风险 1（视觉突变）**：7 个页面此前边框/色块实际失效，收敛后「突然出现」→ 属修复，但用户可能感知为「页面变重」。缓解：交付说明逐页列「首次生效的声明」。
- **风险 2（表格密度）**：新增 `tabular-nums` + 右对齐会改变表格观感。缓解：仅加两条属性，不动 padding/行高。
- **风险 3（CSS 级联顺序）**：`tokens.css` 若被放到 `workspace-shell.css` 之后，`:root` 变量定义顺序不影响解析（变量在计算期解析），但为确定性统一放前面（T-4 断言）。
- **回滚**：全部改动集中在 11 个文件（1 新建 + 10 模板）+ 1 个 CSS，`git checkout` 单个提交即可完全回退；无数据、无接口影响。
