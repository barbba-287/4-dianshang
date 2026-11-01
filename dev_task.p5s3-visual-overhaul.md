# dev_task.p5s3 — 全局视觉骨架重构（Visual Overhaul）

| 项 | 值 |
|---|---|
| 切片 | P5-S3 |
| 版本 | **v1.1（定稿）** — Q-P5S3-1..7 七条裁定全部回填（§9），新增「删除前等效声明 + 图表尺寸验证」硬条款（§6.4） |
| 前置 | P5-S1 `0df5dc9`（token 层）、P5-S2 `17aa757`（表格/徽标/空态）均已收口 |
| 基线 | **502 passed / 0 failed**（`17aa757` 实测 287.69s） |
| 目标 | 502 → **514**（新增 `tests/test_p5s3_visual_skeleton.py` T-1..T-12） |
| 分支 | `feat/warehouse-mvp`（当前分支，不新建） |
| 提交 | 完成后提交，**不 push** |

> **定稿记录（2026-11-01）**：用户全盘接受七条裁定建议，并追加一条硬要求 ——
> 「删除页内 `<style>` 前，必须把依赖这些样式的 DOM 节点（尤其 ECharts 容器与 `.cards` 网格）
> 在全局层做好等效声明；删完必须人工在 F12 确认布局不坍塌、ECharts 尺寸不为 0」。
> 已落为 **§6.4 硬条款 + G17 机器守卫 + T-11/T-12 用例 + §11 R1 处置**，四处同时生效。

---

## §1 目标与边界

### 1.1 本片要解决的

用户对当前界面的评价是「原生、古朴、直男审美，缺现代 SaaS 的精致感」。本片做**视觉骨架层**重构，不碰任何业务逻辑。

### 1.2 硬边界（违反即失败）

| 编号 | 边界 | 说明 |
|---|---|---|
| B-1 | **零业务代码改动** | 不碰任何路由、鉴权、DB、API、CLI |
| B-2 | **零 JavaScript 改动** | 10 个模板 `<script>` 块内**逐字节不变**（P5-S1 T-7 锁死色值 24） |
| B-3 | **零 DOM ID 改动** | 所有 `id=` 属性不得增删改；业务 JS 全部靠 `getElementById` |
| B-4 | **零外部资源** | 严禁任何 `http(s)://` 外链、CDN、`@import url(http…)`。内网环境（P5-S1 Q-2 已裁定） |
| B-5 | **不改 `app/main.py`** | 图标不走主程序常量，走 CSS 属性选择器（见 §4.3） |
| B-6 | **不改 `workspace-shell.css`** | 逐字节不变（P5-S1 T-5 的 5 个字面串锁死，动它有风险） |

> B-6 是本片的核心策略：**新开一层，旧层冻结**。好处是回滚只需删 10 行 `<link>`。

---

## §2 现状盘点（2026-11-01 实测，勿再凭印象）

### 2.1 ⚠️ 先纠正三个前提假设

任务发起时的三条判断，实测**均不成立**或已部分成立。按错误前提动手会返工，故先钉死事实：

| 假设 | 实测结论 | 证据 |
|---|---|---|
| 侧边栏「纯文字列表，没有图标」 | **已有图标**，但是 9 个**几何 Unicode 字符**：`◎ ▦ ▣ ◫ ✦ ◇ ▤ ♙`，客服知识组是 **`?`（一个问号）** | `app/main.py:246-256`（`_WORKSPACE_NAV_ITEMS`）+ `:265`（`_SYSTEM_NAV_ITEMS`）。这些字符笔画细、光学尺寸不一、随字体渲染漂移 → 这是「寒酸感」的**真实来源** |
| 侧边栏「没有选中高亮」 | **已有**：`box-shadow:inset 3px 0 0 var(--shell-brand)` + 背景 `#ffffff14` + 文字变白 | `workspace-shell.css` 第 1 行第 22-23 段 |
| 侧边栏「没有深色背景」 | **已有**：`--color-nav-bg: #14243a`（深海军蓝），文字 `#dbe7f5` | `tokens.css` + `workspace-shell.css` `.sidebar` 规则 |
| 「没有卡片包裹」 | `.panel` / `.card` **已存在**（白底 + 1px 边框 + `--shadow-card`），**但 `index.html` 的 5 个 `<section>` 一个 `.panel` 都没有**（panel=0，table=3） | §2.3 计数表 |
| 「字体需要加 Inter」 | **系统栈已含** `system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei"` | `tokens.css` `--font-sans`。**Inter 是外链字体，内网不可用，本片不加** |

**结论：本片的真实病灶不在「缺什么」，而在「有，但做得很糙 + 有一套并行的旧副本在覆盖它」。**

### 2.2 DOM 骨架：10 页**完全统一**（好消息）

全部 10 个模板的 body 结构一致，**无需 DOM 重构**：

```
div.shell
├── aside.sidebar          ← 由 main.py 正则替换注入导航
└── main.main
    ├── header.topbar      ← 10 页都有
    └── div.content
        └── div.page-heading  (h1 + 说明 + 主按钮)
            └── section.panel × N  /  div.step / 表格
```

`_render_workspace_page`（`app/main.py:328-338`）：

```python
def _render_workspace_page(template_name: str, principal, active_path: str) -> HTMLResponse:
    template = (Path(__file__).parent / "templates" / template_name).read_text(encoding="utf-8")
    navigation = _visible_navigation(principal, active_path)
    template = re.sub(
        r'<aside class="sidebar">.*?</aside>',
        f'<aside class="sidebar">{navigation}</aside>',
        template, count=1, flags=re.DOTALL,
    )
    return HTMLResponse(content=template, headers={"Cache-Control": "no-store, max-age=0"})
```

**这条正则只认 `<aside class="sidebar">` 这串字面量** → 侧边栏 DOM 属性**一个字符都不能动**（守卫 G6）。

### 2.3 页内样式副本表（本片真正的工作量）

10 个模板每个都有一个 `<style>` 块，**其位置在 `<link rel=stylesheet>` 之后** → 同特异性时**页内规则胜出**。

用 `.testtmp/p5s3_survey.py` 实测（脚本已落盘，可复跑）：

| 模板 | `<style>` 体积 | 去重选择器 | 与 shell 重复 | 页内自带骨架副本 | 危害 |
|---|---:|---:|---:|---|---|
| **dashboard.html** | 6867 B | 84（实际 94 条规则，含 `@media`） | **26** | `body` `.sidebar` `.main` `.content` `.panel` `.topbar` `.avatar` | 🔴 **改 shell 对它完全无效** |
| **cockpit.html** | 5612 B | 63（实际 74 条规则） | **26** | 同上 | 🔴 **改 shell 对它完全无效** |
| **admin_users.html** | 2263 B | 31 | 3 | `body` `.sidebar` `.content` | 🟠 布局被页内接管 |
| **warehouse.html** | 140 B | 3 | 2 | `body` `.sidebar` `.content` | 🟠 同上（3 条全是骨架副本） |
| orders.html | 1492 B | 19 | 14 | `.panel` 控件 | 🟡 组件样式分叉 |
| content.html | 1760 B | 20 | 10 | `.panel` 控件 | 🟡 |
| shopify.html | 961 B | 13 | 11 | `.panel` | 🟡 |
| customer_service.html | 4763 B | 48 | 2 | 控件 | 🟡 |
| index.html | 1840 B | 16 | 2 | 无（仅 `.step`） | 🟢 |
| **assistant.html** | 995 B | 10 | **0** | 无 | 🟢 **唯一干净页** |

**这是本片头号障碍**：dashboard / cockpit 两页各带着一份**完整的旧版骨架 CSS 副本**（含自己的 `.sidebar` 深色背景、`.topbar`、`.panel`）。P5-S1 只把色值换成了 `var()`，**规则结构原封不动**。

→ 只要这两份副本在，任何对 shell 的改造都会被它们覆盖。**不清理副本，本片无法交付。**

### 2.4 四项真实缺口（有实测计数）

| 编号 | 缺口 | 实测 |
|---|---|---|
| **D-1** | **零动效** | 全站 CSS `transition` 仅 **1 处**（`workspace-shell.css` 内 `.nav-group-toggle::after` 的箭头旋转 `.15s`；10 个模板内 `transition` 计数 **0**）。按钮/输入框/链接/菜单一律瞬时切换 |
| **D-2** | **零焦点态** | CSS `:focus` 伪类全站仅 **2 处**（均在 shell：`.nav-group-toggle:focus-visible` 与 `.nav a:focus-visible`），且都是 `outline:none` —— 把浏览器默认焦点环干掉却没给替代（无障碍反模式）。`input` / `button` 无任何 focus 视觉反馈。（`assistant.html` 另有 1 处 JS `message.focus()` 调用，非样式，不计） |
| **D-3** | **顶栏分裂** | `.topbar` 在 shell 有 1 条规则（`display:flex` + `height:4.3rem` + 白底 + 下边框）。但 `.crumb` / `.top-actions` / `.avatar` **只在 dashboard/cockpit 页内定义**，其余 8 页顶栏是裸 `.muted` 灰字 + 原生按钮 → **同一个顶栏，两种长相** |
| **D-4** | **表格观感是「表格堆」** | 全站 23 张表。`th,td{border-bottom:1px solid var(--shell-line)}` 横线密集；`index.html` 的 3 张表**完全没有卡片包裹**，直接铺在灰底上（`#warehouse` / `#purchase-workflow` / `#warehouse-step-inventory` 裸奔） |

### 2.5 规模与控件基数

| 模板 | panel | table | input | select | button | 内联 style= |
|---|---:|---:|---:|---:|---:|---:|
| admin_users | 3 | 2 | 4 | 3 | 9 | 0 |
| assistant | 2 | 0 | 0 | 0 | 9 | 2 |
| cockpit | 7 | 4 | 0 | 2 | 4 | 1 |
| content | 2 | 1 | 1 | 6 | 9 | 2 |
| customer_service | 5 | 2 | 6 | 3 | 16 | 4 |
| dashboard | 9 | 7 | 1 | 3 | 8 | 2 |
| **index** | **0** | 3 | 9 | 5 | 26 | 2 |
| orders | 2 | 2 | 4 | 2 | 5 | 1 |
| shopify | 3 | 1 | 0 | 0 | 3 | 0 |
| warehouse | 3 | 1 | 2 | 2 | 5 | 0 |
| **合计** | **36** | **23** | **27** | **26** | **94** | **14** |

→ 控件改动（94 按钮 / 53 输入框与下拉）走 **CSS 统一覆盖**，不逐个改 HTML。
→ 14 处内联 `style="…"` 保持不动（逐条改风险高、收益低，登记为 O-P5S3-1）。

---

## §3 方案总览

三层推进，**逐层可独立回滚**：

```
L0  清理页内副本（让 shell 成为唯一真源）      ← 必须先做，否则 L2 全部失效
L1  扩充 token（只追加，不改既有值）           ← 全部新色值/时长在这里定义
L2  新建 ui-v2.css 骨架覆盖层（10 个 <link>）  ← 全部视觉改造在这里
```

### 3.1 为什么是「新开一层」而不是「改 shell」

| 方案 | 评估 |
|---|---|
| ① 直接改 `workspace-shell.css` | ❌ 该文件被 P5-S1 T-5 / P5-S2 T-1/T-5/T-7 共 5 条用例以**字面串**锁死；且它是单物理行，改动极易误伤。回滚成本高 |
| ② **新建 `ui-v2.css`，加载序 tokens → shell → ui-v2** | ✅ **推荐**。shell 逐字节冻结（G1 可机器验证）；回滚 = 删 10 行 `<link>`；失败不影响既有 502 条 |
| ③ 引 Tailwind / Bootstrap / DaisyUI CDN | ❌ 见 §9「已排除选项」 |

### 3.2 特异性策略（关键）

页内 `<style>` 在 `<link>` **之后**，同特异性时页内赢。因此 ui-v2.css 里所有覆盖规则统一加 `body` 前缀，把特异性从 `(0,1,0)` 提到 `(0,1,1)`：

```css
body .panel { … }      /* (0,1,1) > 页内 .panel (0,1,0) */
body .sidebar { … }    /* 即使漏删副本也能压住 —— 双保险 */
```

> 这层「双保险」很重要：万一 L0 漏删某条副本，L2 仍然生效，不会出现「改了一半没反应」的调试黑洞。

---

## §4 token 扩充（`app/static/css/tokens.css`，**只追加**）

### 4.1 追加位置

文件末尾（第 161 行之后的 P5-S2 追加层之后），新建一段：

```css
/* ==========================================================================
   P5-S3 视觉骨架层（L1 补充：动效时长 / 焦点环 / 侧栏深色阶 / 面板阴影）
   --------------------------------------------------------------------------
   依据 dev_task.p5s3-visual-overhaul.md §4；只追加，不改任何既有 token 值。
   ========================================================================== */
```

### 4.2 新增变量（14 个，逐个照抄，勿自创）

```css
/* 侧边栏深色阶（现有 --color-nav-bg:#14243a 保留为基色不动） */
--color-nav-bg-deep: #0e1b2c;
--color-nav-hover-bg: #ffffff12;
--color-nav-active-bg: #ffffff1f;
--color-nav-section-ink: #7d90a8;

/* 焦点环（主色 20% alpha，用于 input/button 的 focus-visible） */
--color-focus-ring: #2a78d633;

/* 面板 */
--color-panel-border: #e8eef6;
--shadow-panel: 0 1px 2px #1018280a, 0 10px 28px -14px #1018281f;
--shadow-panel-hover: 0 1px 2px #1018280d, 0 16px 36px -16px #10182826;

/* 顶栏 */
--topbar-height: 4rem;
--color-topbar-bg: #ffffffd9;
--shadow-topbar: 0 1px 0 #e8eef6;

/* 动效 */
--dur-fast: 120ms;
--dur-base: 200ms;
--ease-out: cubic-bezier(.4, 0, .2, 1);
```

### 4.3 复用既有（**不要重复定义**，否则 P5-S1 T-3 会红）

`--color-primary:#2a78d6` / `--color-primary-hover:#1f63b5` / `--color-primary-soft:#eaf2fc` /
`--color-primary-border:#a9c9ec` / `--color-border:#e4eaf2` / `--color-border-subtle:#eef2f6` /
`--color-border-input:#ccd7e5` / `--color-bg-tint:#f7fafe` / `--color-bg-muted:#edf0f4` /
`--color-text:#142033` / `--color-text-muted:#69778b` / `--neutral-100:#f4f7fb` /
`--radius-sm:4px` `--radius-md:8px` `--radius-lg:12px` `--radius-pill:999px` /
`--space-1..8`（4/8/12/16/24/32） / `--font-sans` / `--shadow-card` `--shadow-overlay`

---

## §5 新建 `app/static/css/ui-v2.css`

**新建文件，约 280 行。** 下面 **9 段**（段 0 + 段 1..8）按顺序写，**不要自创选择器，不要加 `!important`**。
落盘顺序：§5.1 的文件头注释放文件最顶 → §5.0 的 reset → §5.2..§5.8 依次。

### 5.0 段 0 — 全局 reset（**新增，必读**）

⚠️ **实测坑（务必先看）**：`tokens.css:161-172` 只有 `body{background;color;font-family;font-variant-numeric}`，
**明确注释「不设 body{margin}，保持现状」**。而全站只有 4 个模板带 `body{margin:0}`：

| 有 `body{margin:0}` | **没有** |
|---|---|
| admin_users.html、cockpit.html、dashboard.html、warehouse.html | **index、orders、content、shopify、assistant、customer_service（6 个）** |

→ 这 6 个页面长期存在 **浏览器默认 8px 白边**（内容四周内缩 8px、可用宽度少 16px）。
这是现存缺陷，不是本片引入的。**本片顺手修掉**：

```css
/* ---------- 段 0：全局 reset（修 6 个页面缺 body{margin:0} 的 8px 白边） ---------- */
body{margin:0}
```

> 加了这一条后，上述 6 个页面会整体位移 8px（内容左移/上移、变宽 16px）—— **这是修 bug，不是破版**。
> 交付说明里必须写明「这 6 个页面有 8px 位移属预期」。

### 5.1 段 1 — 文件头与图标变量占位

```css
/* ==========================================================================
   P5-S3 视觉骨架覆盖层（L2：只在 tokens.css + workspace-shell.css 之后加载）
   --------------------------------------------------------------------------
   依据 dev_task.p5s3-visual-overhaul.md §5。
   硬约束：
     ① 全部颜色/圆角/时长取自 tokens.css，本文件不出现裸色值（Guard G14）
     ② 覆盖规则统一加 `body` 前缀，特异性 (0,1,1) 压过模板页内 <style>（§3.2）
     ③ 不得出现 position:fixed（Guard G9）；顶栏吸顶用 position:sticky
     ④ 不得出现 @import、不得出现 http(s)://（Guard G8）
   ========================================================================== */
```

### 5.2 段 2 — 侧边栏（深色质感 + 分组层级）

```css
/* ---------- 侧边栏：保留既有 232px/76px 固定布局，只改质感 ---------- */
body .sidebar{
  background:linear-gradient(180deg, var(--color-nav-bg-deep), var(--color-nav-bg));
  box-shadow:inset -1px 0 0 #ffffff0f;
}

/* 品牌区：mark 加渐变与内高光 */
body .brand{margin-bottom:1.6rem;letter-spacing:-.01em}
body .brand-mark{
  background:linear-gradient(160deg, var(--color-primary), var(--color-primary-hover));
  box-shadow:0 2px 8px -2px #2a78d680, inset 0 1px 0 #ffffff40;
  font-size:.95rem;
}

/* 分组标题：降为小号中性色小节标题（现代后台惯例） */
body .nav-group-toggle{
  padding:.5rem .75rem;
  color:var(--color-nav-section-ink);
  font-size:.7rem;font-weight:650;letter-spacing:.09em;text-transform:uppercase;
  border-radius:var(--radius-sm);
  transition:color var(--dur-fast) var(--ease-out);
}
body .nav-group-toggle:hover,
body .nav-group-toggle:focus-visible{color:var(--color-text-inverse);background:var(--color-nav-hover-bg)}

/* 导航项：圆角 + 平滑过渡 + hover 浮起 */
body .nav a{
  border-radius:var(--radius-md);
  color:var(--color-text-nav-link);
  transition:background-color var(--dur-fast) var(--ease-out),
             color var(--dur-fast) var(--ease-out),
             box-shadow var(--dur-fast) var(--ease-out);
}
body .nav-group>.nav a.nav-page-link,
body .nav-group>.nav a.nav-sub{padding-top:.55rem;padding-bottom:.55rem;font-size:.86rem;font-weight:500}
body .nav a:hover{background:var(--color-nav-hover-bg);color:#fff}

/* 选中态：左侧品牌色条 + 提亮背景 + 文字加粗 */
body .nav-group>.nav a.nav-page-link.active{
  background:var(--color-nav-active-bg);
  color:#fff;font-weight:650;
  box-shadow:inset 3px 0 0 var(--color-primary);
}
body .nav-group>.nav a.nav-sub.active{background:var(--color-nav-active-bg);color:#fff;box-shadow:none}
```

### 5.3 段 3 — 图标（20 条规则，**本片视觉提升最大的一处**）

原理：`.nav-icon` 里的几何字符用 `font-size:0` 隐掉，改用 **CSS mask + data-URI SVG + `background-color:currentColor`**，
这样图标颜色自动跟随 hover/active 的文字色，且**完全不依赖字体、不依赖网络、不改一行 HTML**。

```css
/* ---------- 图标：mask-image + currentColor（零外链、零 DOM 改动） ---------- */
body .nav a .nav-icon{
  display:inline-block;
  width:1.15rem;height:1.15rem;flex:0 0 1.15rem;
  font-size:0;line-height:0;letter-spacing:0;
  background-color:currentColor;
  -webkit-mask-image:var(--nav-icon);mask-image:var(--nav-icon);
  -webkit-mask-repeat:no-repeat;mask-repeat:no-repeat;
  -webkit-mask-position:center;mask-position:center;
  -webkit-mask-size:contain;mask-size:contain;
  opacity:.88;
  transition:opacity var(--dur-fast) var(--ease-out);
}
body .nav a:hover .nav-icon,
body .nav a.active .nav-icon{opacity:1}

/* 逐个路由绑定（href 是稳定路由，不会随菜单顺序变化） */
body .nav a[href="/cockpit"] .nav-icon{--nav-icon:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M3.5 17.5a9 9 0 1 1 17 0'/><path d='M12 13.2 16.2 9'/><circle cx='12' cy='13.5' r='1.5'/></svg>")}
body .nav a[href="/dashboard"] .nav-icon{--nav-icon:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><rect x='3.5' y='3.5' width='7' height='7' rx='1.6'/><rect x='13.5' y='3.5' width='7' height='7' rx='1.6'/><rect x='3.5' y='13.5' width='7' height='7' rx='1.6'/><rect x='13.5' y='13.5' width='7' height='7' rx='1.6'/></svg>")}
body .nav a[href="/orders"] .nav-icon{--nav-icon:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M6 3.5h8l4 4v13H6z'/><path d='M14 3.5v4h4'/><path d='M9 12.5h6M9 16h4'/></svg>")}
body .nav a[href="/ops"] .nav-icon{--nav-icon:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M4 10.6 12 4l8 6.6V20H4z'/><path d='M9.6 20v-5.2h4.8V20'/></svg>")}
body .nav a[href="/content"] .nav-icon{--nav-icon:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M12.5 4H5v16h7'/><path d='M15 4h4v4'/><path d='M20.2 10.4 13 17.6l-3 .7.7-3 7.2-7.2z'/></svg>")}
body .nav a[href="/shopify"] .nav-icon{--nav-icon:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M6 8h12l-1.1 12.5H7.1z'/><path d='M9.2 8V6.2a2.8 2.8 0 0 1 5.6 0V8'/></svg>")}
body .nav a[href="/assistant"] .nav-icon{--nav-icon:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><rect x='4' y='7' width='16' height='11.5' rx='3'/><path d='M12 7V3.8'/><circle cx='9.4' cy='12.8' r='1.1'/><circle cx='14.6' cy='12.8' r='1.1'/></svg>")}
body .nav a[href="/warehouse"] .nav-icon{--nav-icon:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M3.5 8.4 12 4l8.5 4.4v7.2L12 20l-8.5-4.4z'/><path d='M3.5 8.4 12 12.8l8.5-4.4M12 12.8V20'/></svg>")}
body .nav a[href="/admin/users"] .nav-icon{--nav-icon:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><circle cx='9.2' cy='8' r='3.2'/><path d='M3.6 20c0-3.2 2.5-5.4 5.6-5.4s5.6 2.2 5.6 5.4'/><path d='M16.2 5.6a3 3 0 0 1 0 5.4'/><path d='M17.6 14.9c1.9.6 3 2.4 3 5.1'/></svg>")}
body .nav a[href="/customer-service"] .nav-icon{--nav-icon:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M4 13.5a8 8 0 0 1 16 0'/><rect x='2.8' y='13.2' width='4.2' height='6.2' rx='2'/><rect x='17' y='13.2' width='4.2' height='6.2' rx='2'/></svg>")}
```

> **实施要点**：`--nav-icon` 只在 `.nav a[href=…]` 上赋值，实际应用写在 `.nav-icon` 上，所以 10 条绑定 + 1 条应用 = 11 条规则，不会互相覆盖。
> `stroke='black'` 是 mask 通道色，**不是显示色**（显示色来自 `background-color:currentColor`），不要改成白色。

**极旧内核回落（R3）**，接在上面 11 条之后：

```css
@supports not ((-webkit-mask-image:none) or (mask-image:none)){
  body .nav a .nav-icon{font-size:.92rem;line-height:1;background-color:transparent}
}
```

> 即：不支持 mask 的浏览器回落到原来的几何字符，页面不会出现「图标全空白」。**这 4 行不可省。**

### 5.4 段 4 — 顶栏（吸顶 + 毛玻璃 + 补齐 crumb/top-actions）

```css
/* ---------- 顶栏：sticky 吸顶（禁 fixed，Guard G9）+ 半透明毛玻璃 ---------- */
body .topbar{
  position:sticky;top:0;z-index:15;
  height:var(--topbar-height);
  padding:0 var(--space-8);
  background:var(--color-topbar-bg);
  -webkit-backdrop-filter:saturate(180%) blur(12px);
  backdrop-filter:saturate(180%) blur(12px);
  border-bottom:1px solid var(--color-panel-border);
  box-shadow:var(--shadow-topbar);
}
/* 补齐只在 dashboard/cockpit 页内定义过的两个类 → 10 页顶栏统一 */
body .crumb{font-size:.86rem;color:var(--color-text-muted)}
body .crumb strong{color:var(--color-text);font-weight:650}
body .top-actions{display:flex;align-items:center;gap:var(--space-3);font-size:.86rem}
body .avatar{
  display:inline-grid;place-items:center;width:2rem;height:2rem;
  border-radius:var(--radius-pill);
  background:var(--color-primary-soft);color:var(--color-primary-deep);
  font-weight:700;font-size:.82rem;
}
```

### 5.5 段 5 — 内容区节奏 + 卡片化

```css
/* ---------- 内容区：加大留白，建立纵向节奏 ---------- */
body .content{
  max-width:1380px;
  padding:var(--space-8) var(--space-8) 3.5rem;
}
body .page-heading{margin-bottom:var(--space-6)}
body .page-heading h1{font-size:1.6rem;font-weight:700;letter-spacing:-.022em}

/* ---------- 卡片：统一到一套「面」语言 ----------
   §2.3 的 index.html 5 个 <section> 无 .panel 类 → 用 id 选择器兜底，
   避免改 DOM（守卫 G7）。已带 .panel 的页面重复命中无害（样式等价）。
   ⚠️ 白名单只有下面两个特例（实测均为无 class 的裸 <section>）：
      #warehouse        index.html  <section id="warehouse">
      #purchase-workflow index.html <section id="purchase-workflow">
   ⚠️ 严禁把 #kpis / #kpi-grid / #knowledge 加进来 ——
      它们分别是 <section class="cards"> 与 <section class="cs-panel">，
      .cards 是指标卡网格容器（本身无背景），加卡片外框会变成一个巨大的白块，
      .cs-panel 已有自己的面样式。 */
body .panel,
body section[id$="-panel"],
body section#warehouse,
body section#purchase-workflow,
body .step{
  background:var(--color-surface);
  border:1px solid var(--color-panel-border);
  border-radius:var(--radius-lg);
  box-shadow:var(--shadow-panel);
}
body .panel{padding:var(--space-6)}
body .step{padding:var(--space-4) var(--space-6);margin-top:var(--space-4)}
body .panel-head{margin-bottom:var(--space-4)}
body .panel h2{font-size:.98rem;font-weight:650;letter-spacing:-.005em}
body .panel-subtitle{font-size:.78rem;color:var(--color-text-muted)}

/* 指标卡（.cards > .card）：主数值强化 */
body .card{border:1px solid var(--color-panel-border);border-radius:var(--radius-md);box-shadow:var(--shadow-panel)}
body .card .value{font-weight:700;letter-spacing:-.02em}
```

> ⚠️ `body .panel{padding:var(--space-6)}` 会让 36 个面板的内边距从 18.4px → 24px。**这是本片最明显的观感变化之一**，也是唯一会让「表格左右可用宽度变窄」的改动。若某页表格出现横向溢出，优先接受（`.table-wrap{overflow:auto}` 本来就会滚），**不要去改 `min-width`**。

### 5.6 段 6 — 表格（减噪，不加粗任何线）

```css
/* ---------- 表格：表头着色 + 线降噪（禁加粗，P5-S2 T-5 语义延续） ---------- */
body th{
  background:var(--color-bg-tint);
  font-size:.74rem;font-weight:650;letter-spacing:.02em;
  color:var(--color-text-muted);
  border-bottom:1px solid var(--color-panel-border);
}
body th,body td{border-bottom:1px solid var(--color-border-subtle)}
body table tbody tr:last-child td{border-bottom:0}
body .table-wrap{
  border:1px solid var(--color-border-subtle);
  border-radius:var(--radius-md);
  overflow:auto;
  background:var(--color-surface);
}
```

> **不要**把 `border-bottom` 加到 2px 或改 `medium/thick` —— P5-S2 T-5 的语义约束延续到本片（G11 会复查）。
> `body table tbody tr:last-child td{border-bottom:0}` 与外层 `.table-wrap` 边框配合，避免「双线」。

### 5.7 段 7 — 控件现代化 + 焦点环 + 动效

```css
/* ---------- 表单控件：浅灰底 + 圆角 + hover 边框 + focus 外发光 ---------- */
body input,
body select,
body textarea{
  min-height:2.35rem;
  padding:.42rem .7rem;
  border:1px solid var(--color-border-input);
  border-radius:var(--radius-sm);
  background:var(--color-surface);
  color:var(--color-text);
  font:inherit;
  transition:border-color var(--dur-fast) var(--ease-out),
             box-shadow var(--dur-fast) var(--ease-out);
}
body input:hover,body select:hover,body textarea:hover{border-color:var(--color-primary-border)}
body input:focus,body select:focus,body textarea:focus{
  outline:none;
  border-color:var(--color-primary);
  box-shadow:0 0 0 3px var(--color-focus-ring);
}

/* ---------- 按钮：主色 + 阴影 + 平滑过渡 ---------- */
body button{
  min-height:2.35rem;
  padding:.42rem .85rem;
  border:0;border-radius:var(--radius-sm);
  background:var(--color-primary);color:var(--color-on-primary);
  font:inherit;font-weight:600;
  box-shadow:0 1px 2px #2a78d626;
  cursor:pointer;
  transition:background-color var(--dur-fast) var(--ease-out),
             box-shadow var(--dur-fast) var(--ease-out),
             transform var(--dur-fast) var(--ease-out);
}
body button:hover:not(:disabled){
  background:var(--color-primary-hover);
  box-shadow:0 4px 12px -4px #2a78d666;
}
body button:active:not(:disabled){transform:translateY(1px);box-shadow:0 1px 2px #2a78d626}
body button:focus-visible{outline:none;box-shadow:0 0 0 3px var(--color-focus-ring)}
body button.secondary{
  background:var(--color-bg-muted);color:var(--color-text);
  box-shadow:inset 0 0 0 1px var(--color-border);
}
body button.secondary:hover:not(:disabled){background:var(--neutral-100);box-shadow:inset 0 0 0 1px var(--color-border-input)}
body button.danger{background:var(--color-danger);box-shadow:0 1px 2px #c53b4526}
body button.danger:hover:not(:disabled){background:var(--color-danger-strong)}
body button:disabled{background:var(--color-bg-muted);color:var(--color-text-subtle);box-shadow:none;cursor:not-allowed}

/* ---------- 链接类按钮（.btn-link，P5-S2 引入） ---------- */
body .btn-link{transition:color var(--dur-fast) var(--ease-out),border-color var(--dur-fast) var(--ease-out)}

/* ---------- 全局焦点可见性（修 D-2：不再 outline:none 了事） ---------- */
body a:focus-visible,body summary:focus-visible{
  outline:2px solid var(--color-primary);
  outline-offset:2px;
  border-radius:var(--radius-sm);
}

/* ---------- 动效降级（无障碍） ---------- */
@media (prefers-reduced-motion: reduce){
  body *,body *::before,body *::after{
    transition-duration:.01ms !important;
    animation-duration:.01ms !important;
  }
}
```

> ⚠️ `!important` 只允许出现在这一条 `prefers-reduced-motion` 里（G13 例外项），其余任何地方不得使用。

### 5.8 段 8 — 响应式收口（与 shell 既有断点对齐，勿新增断点）

```css
@media (max-width:1200px){
  body .content{padding:var(--space-6)}
}
@media (max-width:600px){
  body .topbar{height:3.4rem;padding:0 var(--space-4)}
  body .content{padding:var(--space-4) var(--space-3) var(--space-8)}
  body .panel{padding:var(--space-4)}
}
```

---

## §6 逐文件改动清单

### 6.1 `app/static/css/tokens.css`
- **只追加** §4.2 的 14 个变量 + 段注释，位置在文件末尾。
- 既有 87 个变量的**值一个都不许改**（P5-S1 T-3 / T-8 会红）。

### 6.2 `app/static/css/ui-v2.css`（新建）
- 按 §5 的 9 段写（段 0 + 段 1..8），约 280 行。
- 文件头注释必须含 §5.1 的 4 条硬约束。

### 6.3 10 个模板（`app/templates/*.html`）
每个模板改两处：

**(a) `<head>` 内加第三个 `<link>`**，位置**紧跟 `workspace-shell.css` 之后**：

```html
<link rel="stylesheet" href="/static/css/ui-v2.css">
```

必须满足顺序：`tokens.css` → `workspace-shell.css` → `ui-v2.css` → `<style>`。

**(b) 删除页内骨架/组件副本**（§2.3 表）。**不得凭「看起来是骨架」就删 —— 必须走 §6.4 的判定规则。**

| 模板 | 删什么 | 保留什么 |
|---|---|---|
| **dashboard.html** | 删 `.shell` `.sidebar` `.brand*` `.nav*` `.main` `.topbar` `.content` `.page-heading` `.panel-head` `.panel-subtitle` `.muted` `.field` `input/select/button` `table/th/td` `.table-wrap` `.empty` `body` | **`.chart*` `.quadrant-*` `.cards`(列数) `.card*` `.sync-*` `.toolbar` `.legend` 及全部 `@media`** —— 见 §6.4 白名单 |
| **cockpit.html** | 同上 | **`.chart*` `.chart-refreshing` `.error-panel` `.cards`(列数) `.card*` `.todo-grid` `.metric-definitions` 及全部 `@media`** —— 见 §6.4 白名单 |
| **admin_users.html** | 删 `body{}` `.sidebar{}` `.content{}` 三条（`.content{max-width:1360px}` 属 Q5 统一范围，一并删） | `.admin-grid` `.member-*` 全部 |
| **warehouse.html** | 删**整个 `<style>` 块**（140 B / 3 条全是骨架副本），连 `<style>` 标签一起移除 | 无 |
| **orders.html** | 删 `.panel` `.card` `.cards` `.table-wrap` `table/th/td` `input/select/button` `.muted` `.field` | `.filters` `.value` `.detail` `.tag/.warn/.good`（O-P5S3-2） |
| **content.html** | 删 `.panel` `.cards` `.field` `.muted` `input/select/button` | `.grid` `.workflow-*` `.status` 的状态色变体 |
| **shopify.html** | 删 `.panel` `.cards` `.card` `.table-wrap` `table/th/td` `.muted` | `.tag` `.warn`（O-P5S3-2） |
| **customer_service.html** | 只删 `button` `button:disabled` 两条 | `.cs-*` 全部（含 `.cs-table{text-align:left}` —— P5-S2 靠高特异性处理，**不动**） |
| **index.html** | 只删重复的 `.step` `.step h3` 两条（可选） | 全部 |
| **assistant.html** | **零改动**（除加 `<link>`） | 全部 |

### 6.4 ⚠️ 硬条款：删除前等效声明 + 图表尺寸验证（用户追加要求，**不可省**）

本片要动 dashboard / cockpit 的页内样式：两页合计 **168 条规则**（dashboard 94 / cockpit 74，含 `@media` 内的），
其中约 **26 个选择器与 shell 重名**。**这两页承载全部 ECharts 图表，删错一条就是白屏。**

#### 6.4.1 判定规则（逐条过，不满足就保留）

一条页内规则 **可删**，当且仅当**同时**满足：

| 条件 | 说明 |
|---|---|
| (a) `workspace-shell.css` 中存在同名或等价选择器 | 例如 `.panel{…}` 在 shell 有 |
| (b) 它的每条声明，要么被 shell 同名规则等价覆盖，要么被 `ui-v2.css` 的 `body .xxx` 更高特异性覆盖 | 逐声明比对，不是整个块看一眼 |
| (c) **它不含任何尺寸/布局锚点属性** —— `height` `min-height` `max-height` `width` `max-width` `grid-template-columns` `grid-template-rows` `position` `inset` `flex-basis` | ★ 这是白屏的根源 |

**任何一条不满足 → 保留，并在交付说明「保留清单」里登记原因。**

#### 6.4.2 必保白名单（machine-checked，G17 / T-11 会验）

以下是实测确认的「尺寸锚点」，**一个字都不许删**：

**dashboard.html**
```
.chart{height:300px;min-height:240px}
.quadrant-chart{height:360px;min-height:280px}
.chart-grid{grid-template-columns:minmax(0,1.35fr) minmax(340px,1fr)}
.quadrant-layout{grid-template-columns:minmax(0,1.5fr) minmax(280px,1fr)}
.quadrant-summary{grid-template-columns:repeat(5,minmax(0,1fr))}
.sync-health-kpi{grid-template-columns:repeat(6,minmax(0,1fr))}
.sync-retry-grid{grid-template-columns:1fr 1fr}
.cards{grid-template-columns:repeat(4,minmax(0,1fr))}   ← shell 是 auto-fit，删了会变列数
.chart-empty{…height:100%…}
```

**cockpit.html**
```
.chart{height:320px;min-height:260px;position:relative}   ← position:relative 是 .chart-refreshing 的定位锚点
.chart-refreshing{position:absolute;top:.4rem;right:.5rem;…}
.chart-grid{grid-template-columns:minmax(0,1.35fr) minmax(340px,1fr)}
.cards{grid-template-columns:repeat(3,minmax(0,1fr))}
.todo-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
.error-panel{display:flex;…}
.chart-empty{…height:100%…}
```

**两页共同**：所有 `@media(max-width:…)` 块内的 `.chart{height:260px}`、`.cards{grid-template-columns:repeat(2,…)}`、`.chart-grid{grid-template-columns:1fr}` 等**响应式值全部保留**。

> **为什么这么严**：`.chart` 的 `height` 是 ECharts 容器的**唯一高度来源**。
> `echarts.init(el)` 在高度为 0 的容器上会渲染空图且**不报错** —— 页面看着"没崩"，图表却全没了。
> 这类故障在 CI 里 100% 检测不到（502 条用例没有一条执行页面 JS），只能靠本节纪律 + 人工核验。

#### 6.4.3 删完必须做的人工验证（F12，写入交付说明）

用浏览器（Chrome/Edge）逐页核验，**截图存档**：

| 检查项 | 合格标准 |
|---|---|
| ① `/dashboard` 图表区 | 4 张图（销售趋势 / 库存风险 / 商品四象限 / 同步健康）**都有内容**，`document.querySelector('.chart').offsetHeight` 返回 **300**（窗口 >1200px 时） |
| ② `/dashboard` 四象限图 | `document.querySelector('.quadrant-chart').offsetHeight` 返回 **360** |
| ③ `/cockpit` 图表区 | 全部图表有内容，`.chart` 高度 **320** |
| ④ `/cockpit` 刷新徽标 | `.chart-refreshing` 出现在图表**右上角内部**，不是飘到页面角落 |
| ⑤ `.cards` 网格 | dashboard 一行 4 张、cockpit 一行 3 张（不是挤成一行 8 张或塌成一列） |
| ⑥ 控制台 | **零** `Uncaught` 报错、零 404（含 ECharts 的 `[ECharts] ...` 警告要记录） |
| ⑦ 其余 8 页 | 布局无坍塌、表格无横向撕裂、侧边栏高亮正常 |

验证脚本模板（粘进 F12 Console 直接跑）：

```js
// 图表容器尺寸核验
[...document.querySelectorAll('.chart,.quadrant-chart')].map(el => ({
  id: el.id || el.className, h: el.offsetHeight, w: el.offsetWidth,
  hasCanvas: !!el.querySelector('canvas'),
  canvasSize: (()=>{const c=el.querySelector('canvas');return c?[c.width,c.height]:null})()
}))
```

合格判据：**每项 `h > 0` 且 `hasCanvas === true`**。任何一项 `h === 0` 或 `hasCanvas === false` → **立刻停止，恢复该条规则**。

#### 6.4.4 等效声明的落点

如果某条尺寸规则**确实**更适合放在全局层（例如希望 10 页统一），可以在 `ui-v2.css` 里写等效声明**之后**再删模板里的 —— 但必须：

1. 新声明用 `body` 前缀（保证赢）；
2. 数值**逐字相同**（`.chart{height:300px}` → `body .chart{height:300px}`，不许"顺手"改成 320px）；
3. 在交付说明里成对登记「删了哪条 / 加在哪行」。

**没有等价声明，不许删。** 宁可留着重复规则，也不要图表白屏。

### 6.5 `tests/test_p5s3_visual_skeleton.py`（新建）

10 条用例，见 §8。

### 6.6 明确不动的文件

`app/main.py`、`app/static/workspace-shell.css`、`app/static/format.js`、`app/static/labels.js`、
`app/static/echarts.min.js`、`tests/test_p5s1_tokens.py`、`tests/test_p5s2_tables_and_empty.py`、
`tests/test_api_dashboard.py`、`docs/**`、`migrations/**`、任何 `app/*.py`。

---

## §7 守卫（G1–G18，交付前必须逐条自查并在交付说明里给结果）

| 编号 | 守卫 | 自查方法 |
|---|---|---|
| G1 | `workspace-shell.css` 逐字节未变 | `git diff --stat -- app/static/workspace-shell.css` 必须为空 |
| G2 | `app/main.py` 逐字节未变 | `git diff --stat -- app/main.py` 必须为空 |
| G3 | 10 个模板的 `<script>` 块逐字节未变 | `git diff` 只看 `<style>` 与 `<head>` 部分；或跑 `test_p5s1_tokens.py::test_t7` |
| G4 | 模板 `<style>` 内 hex / rgba **仍为 0** | `test_p5s1_tokens.py::test_t6` |
| G5 | 10 个模板都注入 `ui-v2.css`，顺序 tokens → shell → ui-v2，且都在 `<style>` 之前 | T-1（§8） |
| G6 | `<aside class="sidebar">` 与 `class="nav-icon"` 字面量仍在 10 个模板 | `grep -c` |
| G7 | 所有 `id="…"` 未增删改 | `git diff -U0 | grep '^-.*id='` 必须为空 |
| G8 | 全仓 `app/static/css/ui-v2.css` 内无 `http://` `https://` `//cdn` `@import` | `grep -E` 返回空 |
| G9 | `ui-v2.css` 内无 `position:fixed`（顶栏用 sticky） | `grep -c 'position:fixed'` = 0 |
| G10 | 全站 `.num{` 定义仍恰好 1 处 | `test_p5s2_tables_and_empty.py::test_t1` |
| G11 | `ui-v2.css` 内无 `border-bottom:2px/3px/medium/thick` | `grep -E` 返回空 |
| G12 | 无 `!important`（`prefers-reduced-motion` 块除外） | `grep -n '!important'` 只允许出现在 §5.7 末尾 |
| G13 | 模板内不出现 `:root` | `test_p5s1_tokens.py::test_t3` |
| G14 | `ui-v2.css` 内无裸 hex 色值（`#xxx` / `#xxxxxx`），颜色一律 `var(--…)` | 例外：§5.2/§5.7 的 6 处阴影 alpha 与 `#ffffff0f` 等半透明分隔线，须在交付说明列清单 |
| G15 | 页面宽度统一后，9 个模板内不再有 `max-width:1360px/1440px/1120px` | `grep -E` 返回空 |
| G16 | 未引入 Tailwind / Bootstrap / DaisyUI / FontAwesome 任何文件或链接 | `git status` 无新增第三方资源 |
| **G17** | **图表尺寸锚点仍在**：`dashboard.html` 含 `.chart{height:300px`、`.quadrant-chart{height:360px`、`.chart-grid{grid-template-columns`、`.cards{grid-template-columns:repeat(4`；`cockpit.html` 含 `.chart{height:320px`、`.chart-refreshing{position:absolute`、`.cards{grid-template-columns:repeat(3`；两页 `.chart-empty` 仍在 | `grep -F` 逐串核验；T-11 会机器验 |
| **G18** | **`body{margin:0}` 已由 `ui-v2.css` 段 0 提供**（补 6 个模板缺失的 reset） | `grep -c 'body{margin:0}' ui-v2.css` ≥ 1；T-12 会验 |

> **G14 的例外处理**：`box-shadow` 里的半透明色值（`#2a78d680`、`#1018280a` 等）无法用 token 表达。
> 逐条写进交付说明「裸色值清单」，说明用途与位置。**正文颜色、边框、背景一律走 var()。**
>
> **G17 是本片的红线级守卫**：它保护的是「图表不白屏」。任何一条不过，本片视为失败，不接受「视觉上看起来还行」的解释。

---

## §8 验收用例（`tests/test_p5s3_visual_skeleton.py`，T-1..T-12）

沿用 `test_p5s1_tokens.py` 的写法（`TPL_DIR` / `_read` / `_style_blocks` 工具函数可直接复用或复制）。

| 用例 | 断言 | 说明 |
|---|---|---|
| **T-1** | 10 个模板都含 `ui-v2.css` 的 `<link>`，且索引顺序 `tokens < shell < ui-v2 < <style>` | G5 |
| **T-2** | `ui-v2.css` 存在且 ≥ 200 行；含 `.sidebar`、`.topbar`、`.panel`、`button`、`input` 五类规则 | 层完整性 |
| **T-3** | `ui-v2.css` 内 `http://` / `https://` / `@import` 计数为 0 | G8 |
| **T-4** | `ui-v2.css` 内 `position:fixed` 计数为 0；`.topbar` 规则含 `position:sticky` | G9 |
| **T-5** | `ui-v2.css` 内 `transition` 出现 ≥ 6 次，且含 `var(--dur-fast)` 或 `var(--dur-base)` | D-1 修复 |
| **T-6** | `ui-v2.css` 内 `focus-visible` 出现 ≥ 2 次，且 `input:focus` 规则含 `box-shadow` | D-2 修复 |
| **T-7** | 图标：`ui-v2.css` 内 `[href="/xxx"]` 绑定 ≥ 10 条，且都含 `--nav-icon`；每条 `data:image/svg+xml` 合法前缀 | §5.3 |
| **T-8** | 9 个模板（除 assistant）内不再含 `max-width:1360px` / `1440px` / `1120px`；且 `dashboard`/`cockpit` 的 `<style>` 内不再出现 `.sidebar{` / `.main{` / `body{` | §6.3(b) 清理完成 |
| **T-9** | 卡片化覆盖：`ui-v2.css` 的选择器组必须包含 `section[id$="-panel"]`、`section#warehouse`、`section#purchase-workflow` 三串；且**不含** `#kpis` / `#kpi-grid` / `#knowledge`（防误伤网格容器） | §5.5 |
| **T-10** | 回归锁：`workspace-shell.css` 仍含 P5-S1 的 5 个字面串；`app/main.py` 的 `_WORKSPACE_NAV_ITEMS` 图标字符未被修改（断言仍含 `◎` `▦` `▣`） | G1 + B-5 |
| **T-11** | **图表尺寸锚点未丢（G17 的机器实现）**：`dashboard.html` 的 `<style>` 必须含 `.chart{height:300px`、`.quadrant-chart{height:360px`、`.chart-grid{grid-template-columns`、`.cards{grid-template-columns:repeat(4`、`.chart-empty`；`cockpit.html` 必须含 `.chart{height:320px`、`.chart-refreshing{position:absolute`、`.cards{grid-template-columns:repeat(3`、`.chart-empty`。逐串 `in` 断言 | §6.4.2 |
| **T-12** | **全局 reset 到位**：`ui-v2.css` 含 `body{margin:0}`；且含 `body .panel` 与 `body .card` 两条等效声明 | G18 + §6.4.4 |

**预期总数：502 + 12 = 514 passed / 0 failed。**

> 关于 T-11 的写法：直接对模板文本做字面串 `in` 断言（与 P5-S1 T-5 同风格），
> **不要**解析 CSS —— 解析器会被合并选择器与重复键骗（P5-S1 复核时踩过，R-20 教训）。

---

## §9 裁定记录（Q-P5S3-1..7，**用户 2026-11-01 已全部拍板，勿再议**）

| 编号 | 问题 | 裁定结果 | 理由 |
|---|---|---|---|
| **Q-P5S3-1** | 是否引 **Tailwind CDN / Bootstrap 5 / DaisyUI**？ | ❌ **否决，纯手写 CSS**（用户已确认「推翻我之前的外部框架建议」） | ① **内网环境**，CDN 加载失败 = 整页裸奔（P5-S1 Q-2 已就同一问题裁定「外链致命」）；② Tailwind **Play CDN 是运行时 JIT**，需拉 ~120KB JS 在浏览器编译，首屏 FOUC 闪烁；③ Bootstrap/DaisyUI 会引入**第二套变量体系**，与 `tokens.css` 冲突；④ 要发挥 `btn btn-primary` 就得**改写全部 94 个按钮的 class**，直接踩 B-3 红线；⑤ 现有 5 条测试以字面串锁死样式文件，换框架 = 全红 |
| **Q-P5S3-2** | 图标怎么换？ | ✅ **CSS 属性选择器 + `mask-image` data-URI SVG**（§5.3） | 零后端改动（不碰 `main.py`）、零 DOM 改动（`<span class="nav-icon">` 保留，测试 `test_frontend_warehouse.py:70` 依赖它）、不依赖字体与网络、颜色自动跟随 `currentColor` |
| **Q-P5S3-3** | 新建 `ui-v2.css` 还是改 `workspace-shell.css`？ | ✅ **新建 `ui-v2.css`** | shell 被 5 条用例字面串锁死；新层可一键回滚（删 `<link>`）；失败不影响 502 条基线 |
| **Q-P5S3-4** | dashboard/cockpit 的**完整骨架副本**怎么处理？ | ✅ **彻底清理**（用户原话「家规大于国法必须靠 L0 根治」），但**受 §6.4 白名单约束** | 不删则本片对这两页**完全无效**；但删前必须做等效声明 + 图表尺寸验证 |
| **Q-P5S3-5** | 页面宽度统一（现 dashboard 1440 / admin 1360 / warehouse 1120） | ✅ **统一 1380px** | 视觉一致性优先；三档宽度差异肉眼可见 |
| **Q-P5S3-6** | `index.html` 5 个无 `.panel` 的 section 怎么卡片化？ | ✅ **不改 DOM**，用 `section[id$="-panel"]`（覆盖 3 个）+ 2 个特例 id（`#warehouse` / `#purchase-workflow`）兜底（§5.5） | index 是首页、26 个按钮、4 处 `querySelectorAll`，改 DOM 风险最高而收益最低。**另 3 个（`#kpis`/`#kpi-grid`/`#knowledge`）是网格容器，明确排除** |
| **Q-P5S3-7** | 是否加 `"Inter"` 字体？是否做暗色模式？ | ❌ **都不做** | Inter 需外链（内网死）；系统栈已含 Segoe UI / PingFang SC。暗色模式是独立工程（23 张表 + 24 处 ECharts 色值），不在本片 |
| **Q-P5S3-8**（用户追加） | 删页内 `<style>` 会不会把图表搞塌？ | ✅ **强制"先等效声明、后删除"，且删完必须 F12 人工验证** | 已落成 §6.4 硬条款 + G17 守卫 + T-11/T-12 用例 |

---

## §10 已排除的选项（**不要再试这些**）

| 已排除 | 原因 |
|---|---|
| Tailwind / Bootstrap / DaisyUI / FontAwesome 任何 CDN 或本地包 | Q-P5S3-1 |
| Google Fonts / Inter / 任何 `@import url()` | 内网不可达；P5-S1 Q-2 已裁定 |
| 改 `app/main.py` 的图标常量或导航 HTML | 正则 `r'<aside class="sidebar">.*?</aside>'` 只认字面量；且 B-5 |
| 给 `.nav-icon` 换 Emoji | Emoji 在 Windows / 不同字重下渲染差异极大，比几何字符更糟 |
| 把侧边栏加宽到 240px / 260px | P5-S1 T-5 锁死 `margin-left:232px` 与 `margin-left:76px` |
| 顶栏用 `position:fixed` | P5-S2 T-7 会红（模板内非 `.sidebar` 的 fixed 一律失败）；用 sticky |
| 表格线加粗到 2px（做「描边感」） | P5-S2 T-5 语义；G11 |
| 给 `.panel` 加 hover 抬升动效 | 卡片不是可点元素，抬升会造成「可点」的误读 |
| 动 `.card` 的圆角（P5-S2 Q-3 已裁定保持 `.6rem`） | 裁定记录 |
| 重写 DOM 结构 / 引入组件化 | 无构建环境（无 Node），且 B-3 |
| 用 CSS `:nth-child` 定位图标 | 菜单顺序一变就错位；`[href=…]` 稳定 |
| 批量改 14 处内联 `style="…"` | 收益低风险高，登记 O-P5S3-1 |

---

## §11 风险与回滚

| 风险 | 表现 | 处置 |
|---|---|---|
| R1 删页内副本后 **ECharts 图表白屏/尺寸归零** | `.chart` / `.quadrant-chart` 的高度被删 → `echarts.init` 得到 0 高容器，**渲染空白且不报错**，CI 完全检测不到 | **§6.4 三道锁**：判定规则(c) 禁删尺寸属性 → §6.4.2 白名单 → G17 + T-11 机器守卫 → §6.4.3 F12 人工验证（`offsetHeight > 0` 且 `hasCanvas === true`）。任一项不过即视为失败 |
| R1b 删页内副本后某页变丑（非图表） | 例如 dashboard `.cards` 从 4 列变自适应、`.card-value` 字号变化 | 白名单已含 `.cards{grid-template-columns}` 与 `.card-*`；其余「页内值 ≠ shell 值」的规则保留并在交付说明登记 |
| R2 `body .panel{padding:24px}` 让宽表格横向溢出 | 23 张表里 `.table-wrap` 出现横向滚动条 | **接受**（本就 `overflow:auto`）。不得改 `min-width` |
| R3 `font-size:0` 后图标不显示 | 浏览器不支持 `mask-image`（极旧内核） | `@supports not (…mask-image…)` 回落显示原字符（§5.3 末尾 4 行） |
| R4 顶栏 sticky 遮挡锚点跳转 | `#xxx-panel` 跳过去被顶栏压住 | shell 已有 `section[id]{scroll-margin-top:5.2rem}`（顶栏 4rem，够）。**不要动它** |
| R5 `body{margin:0}` 引入后 6 个页面位移 8px | index/orders/content/shopify/assistant/customer_service 内容整体外移 | **预期行为**（修的是长期存在的 8px 白边缺陷），交付说明须写明 |
| R6 全量 flaky | R-19，约 1/3 概率 1 个随机错误 | 重跑一次全绿即过，但**必须登记** |

**回滚方案**：`git revert <commit>`；或临时删掉 10 个模板的 `ui-v2.css` `<link>` —— 页面立即回到当前状态（shell 未被改过，G1 保证）。

---

## §12 完成标准

1. `.testtmp/delivery.p5s3.md` 落盘，含：
   - 改动文件清单 + 每个文件的关键改动位置（行号）
   - **删掉的页内规则逐条清单**（哪条、原值、shell 对应值、是否等价）
   - **保留的页内规则清单**（按 §6.4.1 判定为「不可删」的，逐条给原因）
   - **G1–G18 逐条自查结果**（G17 必须逐串给出 `grep -F` 的命中证据）
   - **§6.4.3 F12 验证结果**：每页的 `offsetHeight` / `hasCanvas` 实测值 + 截图
   - 裸色值清单（G14 例外项）
   - 6 个页面 8px 位移的确认（R5）
   - 未做项 / 偏差说明
2. 自跑 `pytest tests/test_p5s3_visual_skeleton.py -q` → **12 passed**
3. 自跑全量（**必须用带 `env -u` 前缀的命令**，见下）→ **514 passed / 0 failed**
4. 提交到当前分支，**不要 push**
5. 交付说明里附**页面截图对照**（至少 `/ops`、`/dashboard`、`/cockpit` 三页的前后对比），
   且必须包含 §6.4.3 的图表容器核验输出

> **不通过判据（任一命中即视为未完成）**：任一张图 `offsetHeight === 0` 或 `hasCanvas === false`；
> G17 任一字面串缺失；514 未达；交付说明缺 F12 实测值。

全量回归命令（逐字照抄，不要省 `env -u`）：

```bash
cd /d/ai/dianshang && env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe -m pytest -q -p no:cacheprovider
```

---

## §13 变更记录

| 版本 | 日期 | 内容 |
|---|---|---|
| v1.0 | 2026-11-01 | 初稿：现状盘点（纠正三条前提假设）+ 三层方案 + 15 个新 token + ui-v2.css 8 段草案 + G1–G16 + T-1..T-10 + Q-P5S3-1..7 |
| **v1.1** | 2026-11-01 | **定稿**。Q-P5S3-1..7 全部回填为用户裁定（§9），新增 Q-P5S3-8（删样式风险）。新增 §5.0 全局 reset（修 6 页 8px 白边）、**§6.4 删除前等效声明硬条款**（判定规则 / 必保白名单 / F12 验证 / 等效声明落点）、G17–G18、T-11–T-12，用例 10→12，目标 512→**514** |
