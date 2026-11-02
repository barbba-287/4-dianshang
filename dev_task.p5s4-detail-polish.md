# dev_task.p5s4 — P5-S4 细节精修（标准精修 + 图表微调）

> 前置状态：P5-S3 已收口（提交 74198e0，全量 516 passed / 0 failed，分支 feat/warehouse-mvp）。
> 用户复核裁定：布局已达标但仍显廉价，选「标准精修 + 图表微调」。
> 性质：纯 CSS（ui-v2.css）+ 模板内 3 处色值字面量替换。**零 DOM 改动、零后端、零 tokens.css 改动。**

## 0. 诊断结论（改动依据，实测于 2026-11-02 全页截图）

1. 「退出登录」是 10 页统一的高饱和蓝实心按钮，位置在顶栏（次要操作占主色）→ 降级 ghost。
2. assistant 页 8 个快捷提问 chip 全是蓝实心（候选选项占主色）→ 降级描边 chip。
3. /ops「今日待办」整块米黄底（--color-bg-warm）→ 改白底 + 左侧 3px 警示色条。
4. 按钮/输入框圆角 4px 与卡片 12px 脱节 → 控件统一 8px。
5. KPI 指标卡无 hover 反馈 → 补阴影抬升。
6. 图表橙 #eb6834 与页面警示色脱节 → 对齐 --color-warning 的 #d58b00。

## 1. 改动清单

### 1.1 app/static/css/ui-v2.css —— 圆角统一（就地改 2 处声明，其余行不动）

- 行 199（`body input,body select,body textarea` 规则内）：
  `border-radius:var(--radius-sm);` → `border-radius:var(--radius-md);`
- 行 217（`body button` 规则内）：
  `border:0;border-radius:var(--radius-sm);` → `border:0;border-radius:var(--radius-md);`

### 1.2 ui-v2.css 末尾（现文件 323 行之后）追加「段 11」

```css

/* ---------- 段 11：细节精修（P5-S4，全走既有 token，不新增任何色值/!important） ---------- */

/* 11.1 退出登录降级 ghost：10 页统一 <button id="logout">，特异性 (1,0,1) 压过 body button (0,0,2) */
body #logout{
  background:var(--color-surface);
  color:var(--color-text-muted);
  box-shadow:inset 0 0 0 1px var(--color-border-input);
  font-weight:500;
}
body #logout:hover:not(:disabled){
  background:var(--color-danger-muted);
  color:var(--color-danger-strong);
  box-shadow:inset 0 0 0 1px var(--color-danger-border);
}
body #logout:focus-visible{
  outline:none;
  box-shadow:inset 0 0 0 1px var(--color-border-input),0 0 0 3px var(--color-focus-ring);
}

/* 11.2 assistant 快捷提问 chip：属性选择器按 class 级计，(0,1,2) 压过 body button (0,0,2) */
body button[data-question]{
  background:var(--color-surface);
  color:var(--color-text-muted);
  box-shadow:inset 0 0 0 1px var(--color-border-input);
  font-weight:500;
}
body button[data-question]:hover:not(:disabled){
  background:var(--color-primary-soft);
  color:var(--color-primary-deep);
  box-shadow:inset 0 0 0 1px var(--color-primary-border);
}

/* 11.3 今日待办：米黄底 → 白卡 + 左侧警示色条
   特异性 (1,0,1) 压过页内 #operations-today-panel (1,0,0)；
   padding 不在此声明（T-14 与页内 1rem 各自生效，勿碰）。 */
body section#operations-today-panel{
  background:var(--color-surface);
  border:1px solid var(--color-panel-border);
  border-left:3px solid var(--color-warning);
  border-radius:var(--radius-lg);
  box-shadow:var(--shadow-panel);
}

/* 11.4 KPI 指标卡 hover 抬升（只加阴影不加位移，避免图表页抖动） */
body .card{transition:box-shadow var(--dur-base) var(--ease-out)}
body .card:hover{box-shadow:var(--shadow-panel-hover)}
```

### 1.3 图表橙色对齐警示色（模板内 3 处字符串替换，色值总数不变）

- `app/templates/cockpit.html`（单行文件，行 7）：
  - `orange:'#eb6834'` → `orange:'#d58b00'`
  - `rgba(235,104,52,.35)` → `rgba(213,139,0,.35)`
- `app/templates/dashboard.html`（单行文件，行 8）：
  - `#eb6834` → `#d58b00`（1 处）

依据：#d58b00 == tokens.css 的 --color-warning（行 24），图表语义（库存风险柱/风险区着色）与全站警示色统一。
**其余图表色值一律不动**：#2a78d6 / #1baf7a / #8b5cf6 / #898781 / #e1e0d9 / #ffffff / #142033 / #d03b3b / #9aa7b5 / rgba(42,120,214,.10) / customer_service.html 的 #add。
改后 test_p5s1_tokens.py T-7 的 `<script>` 色值计数仍必须 ==24（替换不增减个数）。

## 2. 新增测试（追加到 tests/test_p5s3_visual_skeleton.py，+3 条 → 目标全量 519）

断言前先 `re.sub(r'\s+',' ',…)` 归一空白（与 T-14 同法）。注意 T-13（行 286-295）锁 !important ==8，段 11 不得使用 ！important。

- **T-15**：ui-v2.css 含 `body #logout` 与 `body button[data-question]` 两组 ghost 规则；hover 声明分别含 `--color-danger-muted` 与 `--color-primary-soft`。
- **T-16**：ui-v2.css 含 `body section#operations-today-panel` 且该规则含 `border-left:3px solid var(--color-warning)`；dashboard.html 与 cockpit.html 均不再含 `#eb6834`，且均含 `#d58b00`。
- **T-17**：段 11 注释分界之后的 CSS 无 `!important`、无裸 hex 色值（`re.findall(r'#[0-9a-fA-F]{3,8}', …)` 为空，G14 延续）。

## 3. 不改什么（红线）

- `workspace-shell.css`、`app/main.py`、`tokens.css`：**本片零改动**（不新增 token；shell 有 5 条字面串测试锁，见 tests/test_api_dashboard.py:214-218）。
- 10 个模板：除 §1.3 的 3 处色值字符串外零改动——DOM、class、`<style>`、JS 全不动。
- `.chart{height:…}` 等图表尺寸锚点（T-11）、@media 结构（T-9）、`.chart-empty`（cockpit.html）一律不动。
- 段 0–段 10 既有规则除 §1.1 两处 radius 声明外不改；T-13 的 ！important 计数保持 8。
- 表格行内按钮（data-action / data-purchase-action / data-order-id 等）**不动**——留待后续切片。

## 4. 验收标准

1. 定向：`env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe -m pytest -q -p no:cacheprovider tests/test_p5s3_visual_skeleton.py tests/test_p5s1_tokens.py tests/test_api_dashboard.py` 全绿。
2. 全量回归（同前缀，不带文件参数）：**519 passed / 0 failed**（基线 516 + 新增 3）。有 flaky：单跑通过即登记后判过。
3. 运行时：`/d/python/python/python.exe .testtmp/p5s3_browser_check.py` 输出 ALL CHECKS PASSED；
   `/d/python/python/python.exe .testtmp/p5s3_design_shots.py` 重截 10 页，人工核对：退出登录=描边灰按钮、hover 泛红；assistant chips=描边；今日待办=白底+左黄条；dashboard/cockpit 无亮橙 #eb6834。
4. 交付说明写 `.testtmp/delivery.p5s4.md`（改动文件清单+行号、未做项、偏差、实测通过数）；提交到当前分支，**不要 push**。

## 5. 已排除的选项（不要重新评估）

- 全局按钮默认色反转（shadcn 式中性默认）——需逐页判定主操作并改 DOM class，风险/收益不成比例。
- 表格行内按钮 ghost 化——涉及 JS 渲染的模板字符串，单独切片。
- 其余图表色（绿/紫/灰）对齐语义 token——无投诉，且牵动 T-7 计数以外的视觉回归面。
- 修改 --radius-sm 的定义本身——4px 仍被 badge/chip 类小元素使用。
- 新增任何 token 或新色值——段 11 全部复用既有变量。
