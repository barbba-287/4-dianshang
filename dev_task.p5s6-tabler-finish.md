# dev_task.p5s6-tabler-finish.md — P5-S6「Tabler 化收尾」

> 版本 v1.0（2026-11-02，WorkBuddy 起草，待用户评审）。
> 命名说明：P5-S4/S5/S5b 已实施完毕，本片按顺序编号 S6，内容即用户裁定的「Tabler 化收尾」四切片。
> 前置状态：基线 **521 passed / 0 failed**，HEAD = de8265b（已推送 origin/feat/warehouse-mvp）。

## 0. 目标与边界

把后台观感对齐 Tabler 的设计语言：**不引入任何框架/CDN**（Q-P5S3-1 裁定），纯 CSS + 原生 JS，
全部改动在 `app/static/`、`app/templates/dashboard.html`、`app/templates/cockpit.html`、
`app/templates/*.html`（各加 1 行 script）、`app/main.py`（仅登录页内联字符串）和 `tests/`。
**禁止改动后端业务接口逻辑、路由处理函数、数据模型。**

### 硬约束（违反任何一条 = 返工）

1. `app/static/workspace-shell.css` 中这 5 条字面串必须原样存在
   （`tests/test_api_dashboard.py:214-218` 锁死）：`position:fixed`、`height:100vh`、
   `overflow-y:auto`、`margin-left:232px`、`margin-left:76px`。改 shell.css 其他行允许，但**优先不碰**，
   本片所有新样式一律进 `ui-v2.css`（加载顺序 tokens → shell → ui-v2，后者胜出）。
2. **零新增 `!important`**：`tests/test_p5s3_visual_skeleton.py` T-13 锁 ui-v2.css 中 ！important
   总数 == 8（段 10 六条 + reduced-motion 两处）。本片任何新规则不得使用 ！important。
3. `tokens.css` 中 `<script>` 相关色值锁会因本片迁移而变化（见切片 A 的测试迁移条款），
   除该条款外 tokens.css 现有变量值**一律不动**。
4. ECharts 容器高度锚点 `.chart{height:300px}`（dashboard 页内）与 600px 断点响应式规则（T-9 锁）
   不得删除或改名。
5. 每个模板的 `<link>` 引入顺序 `tokens.css → workspace-shell.css → ui-v2.css` 不得破坏；
   新增 `<script>` 一律放在既有 `echarts.min.js` 之后、页内 `<script>` 之前。
6. 交付落 `.testtmp/delivery.p5s6.md`（改动文件+行号、未做项、偏差、实测通过数）；提交不 push。
7. 改完必须跑浏览器实测（Playwright，参照 `.testtmp/p5s3_browser_check.py` 的登录方式——
   **多空间 DB 必须填 `input[name="workspace"]`**），F12 级验证四件事：图表渲染非 0 高、
   表头吸顶不与 topbar 重叠、行 hover 生效、toast 弹出且 3 秒后消失。

## 1. 切片 A：ECharts 主题统一（收益最大，先做）

### 现状（实测）

- 用图表的页面只有 2 个：`dashboard.html`、`cockpit.html`（其余模板无 echarts 引用）。
- 两页各自 `echarts.init(...)` 且 option 里散落色值；`tests/test_p5s1_tokens.py:168` 锁
  「10 个模板 `<script>` 内 hex/rgba 色值合计 == 24」（其中 1 处是 customer_service.html 的
  jQuery 选择器 `$('#add')` 误计入，实为 23 个真色值：cockpit 10 + dashboard 13）。
- `app/static/` 是**扁平结构**（labels.js 在根目录），无 `js/` 子目录。

### 要求

1. 新建 `app/static/echarts-theme.js`（**扁平放根目录**，与 labels.js 约定一致；
   用户原话写的 `app/static/js/` 目录不存在，不为此新建目录——此为有意偏差，交付说明里注明即可）。
   内容：`echarts.registerTheme('wb', {...})`，键名与取值：
   - `color`（palette 数组）：依次取 tokens.css 现值的 hex 字面量——主色、成功（绿）、警示（琥珀）、
     危险（红）、紫（若 tokens 有紫变量）。**以实施时 tokens.css 实际值为准，禁止自造色值**；
     文件头注释注明「色值必须与 tokens.css 同步，改 token 时同步改这里」。
   - 轴系：`categoryAxis`/`valueAxis` 的 `axisLabel.color` 用中性灰（对齐 tokens 文本次级色）、
     `axisLine.lineStyle.color` 与 `splitLine.color` 用极浅灰（对齐 gray-200 量级 #e5e7eb）。
   - `tooltip`：白底、`borderColor:#e5e7eb`、`borderRadius:8`、极淡阴影、`textStyle.color` 主文本色。
   - `legend.textStyle.color` 中性灰。
2. 两页引入：`<script src="/static/echarts-theme.js?v=20261102"></script>` 紧跟 echarts.min.js 之后；
   每个 `echarts.init(` 调用加第三参 `'wb'`（先 `grep -n "echarts.init(" ` 两文件拿到全部调用点，
   逐个加，不许漏）。
3. **模板内色值清理（保守策略）**：只删「与主题默认重复」的声明——axisLabel/axisLine/splitLine/
   legend/tooltip 的颜色配置；**逐个 series 的语义色（如风险橙、成功绿）保留**，但值若仍是旧值
   （如 #eb6834 已在 S4 改过）需与 tokens 同值。清理后 `grep -o` 重数模板内色值，
   得到新计数 N。
4. **测试迁移（test_p5s1_tokens.py:168）**：把 `== 24` 改为 `== N`（N=清理后实测数，写入交付说明），
   并在断言消息中说明「chart 主题色已迁至 echarts-theme.js，剩余 N 处为 series 语义色 + 1 处选择器误计」。
   新增：echarts-theme.js 必须存在、含 `registerTheme('wb'`、含 splitLine/tooltip/palette 键。
   两页每个 `echarts.init(` 调用必须带 `'wb'`（正则断言 `echarts.init(` 与 `'wb'` 数量一致或逐处检查）。

### 已排除选项

- ECharts 官方 CDN / vite 构建主题（无构建链，Q-P5S3-1）。
- 一次性把 series 语义色全塞进主题（语义色逐图不同，塞进去会张冠李戴）。

## 2. 切片 B：登录页精修（关掉 R-59）

### 现状（实测）

- 登录页是 `app/main.py` 内联 HTML 字符串（约 L509 起，`title 登录 - 电商工作台`），自包含
  `<style>`（花括号双写 `{{}}` f-string 转义），当前样式：
  `body{max-width:420px;margin:5rem auto;...}` + `button{background:#2563eb;color:white;border:0}`。
  无任何外链 CSS。已知债 R-59：#2563eb 与站内品牌主色漂移。
- 登录页不进 workspace 布局（无 sidebar/topbar），测试只依赖表单行为
  （`input[name=login/password/workspace]`、`button[type=submit]`、`<p id='message'>`），不锁样式字面串。

### 要求

1. 允许改 `main.py` 该内联字符串的 `<style>` 块与少量结构（**保持**：h1「员工登录」文案、
   三个输入字段的 name、`id='message'`、form action/method、按钮 type=submit）。
2. 视觉目标：全屏浅灰底（对齐 tokens --color-bg 值）+ 居中白卡（radius 16px、大卡片阴影、
   内边距 2rem），品牌主色改为 tokens.css 的 `--color-primary` 实际 hex（消灭 #2563eb），
   按钮 hover 加深 8%、input focus 用 focus-ring 色（对齐 tokens --color-focus-ring）。
   标题上方放一个 40px 圆角方块内白色「电」字或纯色块做 logo 占位（纯 CSS，无图片资源）。
3. 保持内联自包含（登录页在鉴权前，不依赖 /static 是否可达——**不要**给它外链 tokens.css）。
4. 测试：grep 全测试目录确认无断言锁 #2563eb 或旧样式字面串；新增 1 条：main.py 登录字符串
   不含 `2563eb`、含 `--color-primary` 对应 hex（从 tokens 读值比对）。R-59 关闭。

## 3. 切片 C：表格细节（吸顶 + 行 hover）

### 现状（实测）

- `workspace-shell.css`：`.table-wrap{overflow-x:auto}`、`th{color:var(--shell-muted);font-size:.75rem}`。
  全站 23 个 `.table-wrap`（dashboard 8 / cockpit 4 / shopify 3 / index 3 / orders 2 / 其余各 1）。
- **关键坑**：祖先有 `overflow-x:auto` 时，`position:sticky` 的参照系变成该容器而非视口；
  容器不纵向滚动 → 吸顶失效。必须先解 overflow 再吸顶。
- topbar 为 `position:fixed`，ui-v2.css 中高度 3.4rem → 吸顶 `top` 必须 > 3.4rem，否则表头被顶栏盖住。

### 要求（全部写进 ui-v2.css 段 12，不动 shell.css）

```css
/* 段 12a：解 overflow 锁（桌面用视口吸顶；窄屏恢复横向滚动，吸顶在小屏自动放弃） */
.table-wrap{overflow:visible}
@media (max-width:760px){.table-wrap{overflow-x:auto}}
/* 段 12b：表头吸顶（top = topbar 3.4rem + 0.3rem 呼吸） */
thead th{position:sticky;top:3.7rem;z-index:10;background:var(--color-panel,#fff);
  box-shadow:0 1px 0 var(--color-border-subtle,#e5e7eb)}
/* 段 12c：行 hover 淡蓝（Tabler $table-hover-bg-factor 0.075 量级） */
tbody tr:hover td{background:#f0f6ff}
```

- 段 12c 不得影响徽标/按钮自身底色（只作用于 td 背景，徽标有自己的背景，天然安全）。
- 若浏览器实测发现 hover 色与某页内斑马纹冲突，以「页内规则胜出」为准，不修页内。
- 测试 T-21：ui-v2.css 含上述三条关键声明（归一空白后断言）；shell.css 的 5 条字面串仍在（已有测试覆盖，
  跑通即可）；断言 `thead th{` 块内含 `position:sticky` 与 `top:3.7rem`。

## 4. 切片 D：全局 Toast 组件

### 现状（实测）

- 反馈现状 = 各页 `#status` 元素写文字行（orders/shopify 等在用），无任何浮层组件。

### 要求

1. 新建 `app/static/toast.js`（扁平根目录）：
   - `window.toast = { success(msg), error(msg), warning(msg), show(msg, type) }`；
   - 容器 `div#toast-root` 固定右下角（`bottom:1.25rem;right:1.25rem`），`aria-live=polite`；
   - 单条 toast：白底 + 左侧 3px 语义色条（成功=绿/失败=红/警告=琥珀，取 tokens 实际 hex）、
     radius 10px、淡阴影、slide-in 动画 200ms（`prefers-reduced-motion` 下关动画——
     ui-v2 已有 reduced-motion 块，**沿用既有块，不新增 ！important**）；
   - 3.2s 自动消失（淡出后 remove），同屏最多 4 条，超出移除最旧。
2. ui-v2.css 段 13：`.toast` 及 `.toast-success/.toast-error/.toast-warning` 样式（色值用 tokens 变量）。
3. 10 个模板各加 1 行：`<script src="/static/toast.js?v=20261102" defer></script>`
   （放在 ui-v2 link 之后；字符串锚点插入，参考 S3 的做法，插完逐个核验位置）。
4. **示范用例（只做 2 处，旧代码不批量替换）**：
   - `index.html`「采集样例商品」请求的成功/失败分支各加 `toast.success(...)`/`toast.error(...)`；
   - `orders.html` 导出 Excel 成功分支加 `toast.success(...)`。
   原 `#status` 文字行**保留不动**（双通道共存，后续切片再收）。
5. 测试 T-22：toast.js 存在且含三种类型；ui-v2.css 含 `.toast-success` 等；10 模板 script 引入
   计数 == 10；index.html 含 `toast.success(` 与 `toast.error(`。

## 5. 切片 E：tokens 适度调优（Tabler 参数对照）

按 Tabler 实测参数对照现有 token，**只调偏差明显项，宁缺毋滥**：

| 参数 | Tabler 值 | 现状 | 动作 |
|---|---|---|---|
| 卡片内边距 | 1.25rem (20px) | ui-v2 段 5 卡片用 --space-4 (16px) | 不动（16px 更紧凑，用户已验收） |
| 边框 | 1px 半透明 #e5e7eb 量级 | --color-border-subtle | 不动 |
| 表格单元格 padding | 0.75rem | 待实测（浏览器量 th/td） | 若 < 0.6rem 则在 ui-v2 段 12 追加 `th,td{padding:.7rem .75rem}`，否则不动 |
| 行 hover | 7.5% 主色淡蓝 | 无 | 段 12c 已覆盖 |
| 阴影 | 极淡（几乎不可见） | --shadow-panel | 不动 |

**默认零新 token**；若实施中确需新变量，先在交付说明列出「名称/值/用在哪」再落。

## 6. 测试与验收

- 新增测试编号 T-20（ECharts 主题）、T-21（表格细节）、T-22（Toast）+ 登录页 1 条，共 4 条；
  目标 **525 passed / 0 failed**（521 + 4）。
- 定向：`tests/test_p5s1_tokens.py tests/test_p5s3_visual_skeleton.py tests/test_api_dashboard.py tests/test_identity.py`。
- 全量（剥垫片前缀）：
  `env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe -m pytest -q -p no:cacheprovider`
- 浏览器实测：登录页截图 1 张（验证居中卡片/主色）、dashboard + cockpit 截图（图表观感、吸顶滚动测试：
  滚动页面后表头仍在顶栏下方可见）、触发一次采集样例商品看 toast。
- 有 flaky 按「重跑一次全绿即过 + 登记」处理（R-19 判定规则）。

## 7. 交接信息

- 改动面：新增 2 文件（echarts-theme.js / toast.js）、ui-v2.css 追加段 12/13、
  dashboard+cockpit 引入主题并清理轴色、10 模板各 +1 行 script、main.py 仅登录字符串、
  tests 4 条新测试 + 1 处计数迁移。
- 明确不做：批量替换 #status → toast、深色模式、Inter 字体（Q7）、任何后端接口改动、
  shell.css 大改、admin 成员页文案（留下一片）。
