# P9-S2 视觉债与细节清理：R-72 表名 + R-59 收尾 + ui-v2 叠色 token 化

> 状态：v1.0 定稿待实施
> 立项依据：用户 2026-11-08 裁定「开启 P9-S2：视觉债与细节清理（纯前端 + 极小后端调整，零风险）」
> 基线：`664 passed / 0 failed`（2026-11-08 复核实测，385.65s），head 迁移不动，分支 `feat/warehouse-mvp`
> 定位：**零新迁移、零新表、零 schema 变更、零接口契约变更**。纯清理片。

---

## §0 范围

| 项 | 做 | 不做 |
|---|---|---|
| R-72 表名 | `build_download` 增可选 `sheet_title`，利润导出显式传「利润明细」 | 不改批次导出的既有表名逻辑；不动 `xlsx_download` 签名 |
| R-59 收尾 | 登录页引入 `tokens.css`，11 种裸色值（18 处出现）全量 `var()` 化 | 不改登录页 DOM 结构、表单字段、文案；不动 `/login` 的 POST 逻辑 |
| ui-v2 叠色 | 6 种主色/白色 alpha 叠色提成 token（值一字不差） | 不动 `workspace-shell.css`（O-P5S1-1 继续挂账）；不动 10 个模板的 `<style>` / `<script>` |
| 视觉 | 除 1 处灰度归并外**像素级零变化** | 不做任何布局、间距、字号、圆角、动效调整 |

**本片不产生任何 R 编号关闭之外的编号变更**。若实施中发现新缺陷，先 `grep -n "R-7[0-9]" docs/review-checklist-20261011.md` 确认最大编号再落笔，**禁止凭印象补号**（历史教训：README 曾误编 R-72）。

---

## §1 侦查结论（施工面，行号基于 `e14ad45`）

### §1.1 三个施工点的现状

| 点 | 位置 | 现状 |
|---|---|---|
| R-72 | `app/import_exports.py:188-202` | `build_download` 在 `:201` 写死 `sheet_title="异常明细" if view == "rejected" else "批次结果"`，利润导出（`view="profit"`）落进 else → Sheet 名是「批次结果」 |
| R-59 | `app/main.py:516-524` | 登录页是**单行 f-string 内联 HTML**。`#2563eb` 已于 P5-S6 消灭（主色改 `#2a78d6`，与 `--color-primary` 同值），但仍残留 **11 种裸色值 / 18 处出现、零 `var()` 引用**（实测：`#fff`×4、`#2a78d6`×3、`#2a78d633`×2、`#142033`×2、`#f4f7fb`/`#e4eaf2`/`#cbd5e1`/`#69778b`/`#1f63b5`/`#1018280a`/`#1018282e` 各 1） |
| ui-v2 叠色 | `app/static/css/ui-v2.css:20,27,220,228,230,237` | 7 处出现 / 6 种值，全部是主色或白色的 alpha 变体 |

### §1.2 关键前提（已实测，勿再猜）

1. **`/static/` 在免鉴权白名单内** —— `app/main.py:404`：`is_public_path = request.url.path in public_paths or request.url.path.startswith("/static/")`。未登录访问 `/login` 能正常加载 `tokens.css`，**不会 303 跳登录页**。这是登录页可以引 tokens 的前提，不要另建路由。
2. **`tokens.css` 有一条 `body{}` 全局规则**（`tokens.css:169-174`：`background/color/font-family/font-variant-numeric`）。它**不会**覆盖登录页外观：登录页 `<style>` 位于 `<link>` 之后，同特异性后者胜。且 `--color-bg=#f4f7fb`、`--color-text=#142033`、`--font-sans`（system-ui 超集）与原值一致。
3. **`--color-focus-ring: #2a78d633` 已存在**（`tokens.css:190`）—— 登录页两处 focus 环**直接复用，零新增**。
4. **`--color-primary-hover: #1f63b5`**（`tokens.css:21`）—— 与登录页 `button:hover` 值完全一致。
5. **`xlsx_download` 已支持 `sheet_title`**（`app/import_exports.py:138`，默认 `"导出结果"`），上限 31 字符截断在 `:143`。**不要改动它**，只在 `build_download` 上游加透传。
6. **`build_download` 只有 2 个调用点**：`main.py:1917`（利润导出，`view="profit"`）、`main.py:4582`（批次导出，`format` 为变量名 `format`/`view`）。改动面极小。

### §1.3 四个必踩的坑

**坑 1：登录页在 f-string 里，花括号要双写**

`main.py:524` 的返回值是 `f"""..."""`，CSS 里所有 `{` 都是 `{{`。本次只替换色值字面量、不新增选择器 → 不新增花括号，安全。**但若你决定新增任何 CSS 规则（含 `@media`），必须写成 `{{` `}}`**，否则 f-string 编译期抛 `ValueError: Invalid format specifier`，整个应用起不来。

**坑 2：`#` 开头的 id 选择器会被「裸色值 grep」误伤，且 `grep -E` 会静默假绿**

登录页有 `#message{...}` 选择器；全站还有 `#toast-root`、`#add-event`、`#event-body` 等。`#[0-9a-fA-F]{3,8}\b` 会把 `#add-event` 的 `#add` 判成色值（实测已发生）。

**验收断言必须用带边界的正则，且必须用 `grep -P`（PCRE）**：

```
(?<![0-9A-Za-z_-])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9A-Za-z_-])
```

**`grep -E` 遇到 `(?<!` 这类 lookaround 不会报错，而是静默返回零行** —— 实测 `grep -nE "<上面的正则>" app/static/css/ui-v2.css` 输出为空，而同一正则用 `-P` 能抓出 6 行。也就是说 `-E` 会让「裸色值清零」这个闸门**永远通过**，是本片最容易造假绿的地方。所有相关命令一律 `grep -P`，且要拿改动前的输出做一次对照（改动前 ui-v2 应抓出 6 行、main.py 应抓出 1 行）。

**坑 3：不要「顺手」清理 `workspace-shell.css`**

它有 22 种裸色值（`#fff`×8、`#edf2f7`、`#c4d2e3`、`#38506a`、`#14751a` …），已知债 **O-P5S1-1**，用户 2026-11-08 明确裁定**本期不纳入，继续挂账**。它是单物理行文件且被 `tests/test_api_dashboard.py:211-219` 与 `tests/test_p5s1_tokens.py:139-146` 的字面串断言锁定，改动性价比极低、回归风险极高。**本片对该文件零改动**。

**坑 4：模板里的色值一个都不能动**

`tests/test_p5s1_tokens.py:174` 锁死：`<script>` 内色值合计 **== 22**（dashboard 12 + cockpit 6 + 其余 4）。`test_t6` 锁死 `<style>` 内零 hex / 零 rgba。ECharts 调色板 `colors={blue:'#2a78d6',...}` 属设计既定色板，**不动不查**。

---

## §2 M1：R-72 —— 导出表名参数化

### §2.1 改动

`app/import_exports.py:188-202`：

```python
def build_download(
    *,
    filename_base: str,
    view: str,
    headers: list[str],
    rows: list[list[str]],
    export_format: str = "xlsx",
    sheet_title: str | None = None,        # 新增：None = 沿用原判据
) -> Response:
    """统一出口：xlsx（默认，Excel 友好）/ csv（机器可读）。"""
    if export_format == "csv":
        return csv_download(f"{filename_base}.csv", headers, rows)
    if sheet_title is not None:
        resolved_title = sheet_title
    else:
        resolved_title = "异常明细" if view == "rejected" else "批次结果"
    return xlsx_download(
        f"{filename_base}.xlsx", headers, rows, sheet_title=resolved_title,
    )
```

**默认值必须是 `None` 而不是 `"导出结果"`** —— 后者会让批次导出（`view="raw"`）的 Sheet 名从「批次结果」变成「导出结果」，属于未经裁定的行为变更，且会打红既有断言。

### §2.2 调用点

- `app/main.py:1917`（利润导出）：在 `view="profit"` 同处加 `sheet_title="利润明细"`。
- `app/main.py:4582`（批次导出）：**不加参数**，保持默认判据。

### §2.3 验收

利润导出 `.xlsx` 的 `workbook.sheetnames == ["利润明细"]`；批次导出 `raw → ["批次结果"]`、`rejected → ["异常明细"]` 不变。

---

## §3 M2：R-59 收尾 —— 登录页 token 化

### §3.1 前置：加 `<link>`

在 `main.py:524` 的 `<head>` 内、`<style>` **之前**插入：

```html
<link rel="stylesheet" href="/static/css/tokens.css">
```

层级顺序对齐 T-1 约定（tokens → 组件层 → 页内 style）。登录页不引 `workspace-shell.css` / `ui-v2.css`（那两个是工作台骨架，引进来会改版式）。

### §3.2 色值替换表（逐条对照，值必须等价）

| # | 原值 | 出现位置 | 替换为 | 是否同值 |
|---|---|---|---|---|
| 1 | `#f4f7fb` | `body{background}` | `var(--color-bg)` | ✅ 同值 |
| 2 | `#142033` | `body{color}`、`label{color}` | `var(--color-text)` | ✅ 同值 |
| 3 | `#fff` | `.card{background}`、`input{background}` | `var(--color-surface)` | ✅ 同值 |
| 4 | `#fff` | `.logo{color}`、`button{color}` | `var(--color-on-primary)` | ✅ 同值 |
| 5 | `#e4eaf2` | `.card{border}` | `var(--color-border)` | ✅ 同值 |
| 6 | `#2a78d6` | `.logo{background}`、`input:focus{border-color}`、`button{background}` | `var(--color-primary)` | ✅ 同值 |
| 7 | `#1f63b5` | `button:hover{background}` | `var(--color-primary-hover)` | ✅ 同值 |
| 8 | `#2a78d633` | `input:focus{box-shadow}`、`button:focus-visible{box-shadow}` | `var(--color-focus-ring)` | ✅ 同值（token 已存在） |
| 9 | `#69778b` | `#message{color}` | `var(--color-text-muted)` | ✅ 同值 |
| 10 | `#cbd5e1` | `input{border}` | `var(--color-border-input)` | ⚠️ **#ccd7e5，唯一有意微变** |
| 11 | `#1018280a` + `#1018282e` | `.card{box-shadow}` | `var(--shadow-color-ambient)` + `var(--shadow-color-key)`（M2.3 新增） | ✅ 同值 |

第 10 项说明：`#cbd5e1` 与 `--color-border-input` 的 `#ccd7e5` 是同语义同用途（表单控件边框），灰度差 1~2 阶、肉眼不可辨。**选择归并而非新增近义 token**，符合 P5-S1「单一真源」目标。此项必须在 delivery 里显式登记为「有意微调」。

### §3.3 新增 2 个阴影色 token

追加到 `app/static/css/tokens.css` 的 `:root`（建议放在 `--shadow-panel*` 附近，或新起一段注释块）：

```css
  /* 卡片双层阴影的色值（分离后可按层复用，勿与 --shadow-panel 混用） */
  --shadow-color-ambient: #1018280a;
  --shadow-color-key: #1018282e;
```

命名是**通用色值级**而非「登录页专用组件 token」，符合 P5-S1 G11 的「不为迁就登录页而造专用 token」约束。

### §3.4 T-23 契约升级（重要：这是有意变更，不是改绿测试）

`tests/test_p5s3_visual_skeleton.py:523-536` 现有断言 `m.group(1).lower() in main_py.lower()`（要求 `--color-primary` 的 hex 出现在 `main.py`）**在 var 化后必然为红**。这是 P5-S6 当时的过渡契约（hex 同值），本片把它升级为更强的契约：

保留：

```python
assert '2563eb' not in main_py          # R-59 根因永不回归
for frag in ("name='login'", "name='password'", "name='workspace'",
             "id='message'", "<h1>员工登录</h1>", "action='/login'"):
    assert frag in main_py              # 表单契约不被样式重构破坏
```

新增（对登录页 HTML 切片后断言，避免误伤 `main.py` 其他部分）：

```python
login_html = _login_page_slice(main_py)          # 从 @app.get("/login") 到 return 语句结束
assert 'href="/static/css/tokens.css"' in login_html
assert 'var(--color-primary)' in login_html
assert login_html.index('tokens.css') < login_html.index('<style>')
assert not HARDCODED_HEX.search(login_html)      # 用 §1.3 坑 2 的有边界正则
```

**这条改写必须同时写进 `docs/review-checklist-20261011.md`**，注明「P9-S2 有意升级 T-23 契约：由『hex 同值』升级为『引 tokens.css + 零裸 hex』，原因：hex 同值只能防漂移、不能防再次分叉」。

---

## §4 M3：ui-v2.css 叠色 token 化

### §4.1 现状（7 处 / 6 种值）

| 行 | 上下文 | 现值 |
|---|---|---|
| 20 | 侧栏内阴影 | `inset -1px 0 0 #ffffff0f` |
| 27 | 侧栏品牌块投影 + 内高光 | `0 2px 8px -2px #2a78d680, inset 0 1px 0 #ffffff40` |
| 220 | 卡片默认投影 | `0 1px 2px #2a78d626` |
| 228 | 卡片 hover 投影 | `0 4px 12px -4px #2a78d666` |
| 230 | 按钮 active 投影 | `0 1px 2px #2a78d626` |
| 237 | 危险按钮投影 | `0 1px 2px #c53b4526` |

### §4.2 新增 token（追加到 `tokens.css`）

| token | 值 | 用途 |
|---|---|---|
| `--shadow-tint-primary-15` | `#2a78d626` | 主色 15% 投影（卡片/按钮默认层） |
| `--shadow-tint-primary-40` | `#2a78d666` | 主色 40% 投影（hover 层） |
| `--shadow-tint-primary-50` | `#2a78d680` | 主色 50% 投影（品牌块） |
| `--sheen-on-primary-25` | `#ffffff40` | 主色底上的内高光 |
| `--hairline-on-primary-06` | `#ffffff0f` | 主色底上的 1px 内分隔 |
| `--shadow-tint-danger-15` | `#c53b4526` | 危险色 15% 投影 |

**值必须逐字节一致**（含 8 位 hex 的最后两位 alpha）。命名可按实现者判断微调，但语义必须能脱离本片使用场景，且**不得**出现 `login` / `card` 之类页面级前缀。

注意：`#c53b4526` 的基色是 `#c53b45`，**不等于** `--color-danger`（`#d03b3b`）。不要试图用 `--color-danger` 推导或替换，否则视觉会变。

### §4.3 验收

`ui-v2.css` 用 §1.3 坑 2 的正则 grep 后**裸色值计数为 0**；文件行数仍 ≥ 200（T-2）；无 `@import`、无外链、无 `position:fixed`（T-3/T-4）。

---

## §5 测试要求

**共新增 3 条**（T-23 属改写，不计入新增）：

| 用例 | 落在哪个文件 | 断言 |
|---|---|---|
| `test_f52_profit_export_sheet_title` | **追加到 `tests/test_p9s1_profit_enhance.py`** | 利润导出 xlsx → `load_workbook(...).sheetnames == ["利润明细"]` |
| `test_e15_batch_export_sheet_title_unchanged` | **追加到 `tests/test_p6s3_data_export.py`** | 批次导出 `raw → ["批次结果"]`、`rejected → ["异常明细"]`（**回归保护：默认判据不得漂移**） |
| `test_m1_no_hardcoded_color_in_ui_v2` | **新建 `tests/test_p9s2_visual_debt.py`** | `ui-v2.css` 裸色值计数 == 0；新增 6 个 alpha token 均已定义且值逐字节正确 |

**为什么不全塞进新文件**：M1、M2 的用例要复用既有 `ctx` / `_client` 造数脚手架（`ctx` 是 `tests/test_p9s1_profit_enhance.py` 的本地 fixture，跨文件复用需要 `conftest.py` 迁移，属无谓改造）。**P9-S2 新文件只放不需要业务数据的纯文件级断言**，避免造第二套登录/造数脚手架。

**测试写法要求**：

- xlsx 解析沿用既有范式：`from openpyxl import load_workbook` + `io.BytesIO(response.content)`（见 `tests/test_p6s3_data_export.py:332-336`）。
- 裸色值断言必须用 §1.3 坑 2 的有边界正则（Python 的 `re` 原生支持 lookaround，无 `grep -E` 那种假绿问题）。**禁止** `'#' not in css` 这类会把 `#toast-root` 判红的写法。
- `test_m1_no_hardcoded_color_in_ui_v2` 要**同时对 `tokens.css` 做正向校验**：6 个新 token 的声明值必须与原裸值完全一致（把「值等价」这件事锁进测试，防止后续有人改 token 值把视觉带偏）。

---

## §6 红线（逐条自检）

1. **零迁移**：不新增 Alembic 版本，不改任何模型、schema、响应模型。
2. **不碰 `workspace-shell.css`**：本片对该文件零改动（O-P5S1-1 继续挂账）。
3. **不碰 10 个模板**的 `<style>` 与 `<script>`：`test_p5s1_tokens.py` T-6/T-7/T-4 的断言必须原样通过（`<script>` 内色值仍为 22，10 模板的 link 顺序不变）。
4. **不格式化任何 CSS 文件**：不换行、不展开选择器、不排序属性。`test_t5` 与 `test_api_dashboard.py:211-219` 依赖无空格字面串（`position:fixed` / `height:100vh` / `overflow-y:auto` / `margin-left:232px` / `margin-left:76px`）。
5. **只增不改 tokens**：`tokens.css` 既有 token 的值一个字不动；`--color-primary` 在全文仍只出现 1 次定义（`test_t3`）。
6. **登录页 DOM 与表单契约不变**：`name='login'/'password'/'workspace'`、`id='message'`、`<h1>员工登录</h1>`、`action='/login'`、`type='submit'` 全部保留。
7. **接口契约不变**：导出响应头 `X-Export-Row-Count` / `X-Export-Truncated`、CSV 的 UTF-8 BOM、文件名规则全部不动。
8. **不加依赖**：`openpyxl` 已在 requirements，不引入任何新包。

---

## §7 验收闸

实施完成后**逐条**自跑并写进 `.testtmp/delivery.p9s2.md`：

1. **全量回归**（必须剥垫片）：

```bash
env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe -m pytest -q -p no:cacheprovider
```

   预期 `≥ 667 passed / 0 failed`（基线 664 + 本片新增 3 条；T-23 属改写不计新增）。有 flaky（约 1/3 概率随机 1 条 frontend 错误）时允许重跑一次，**重跑全绿即过，但必须在 delivery 登记是哪条 flaky**。

2. **定向回归**（必须先绿再跑全量）：

```bash
/d/python/python/python.exe -m pytest -q tests/test_p5s1_tokens.py tests/test_p5s2_tables_and_empty.py tests/test_p5s3_visual_skeleton.py tests/test_api_dashboard.py tests/test_p6s3_data_export.py tests/test_p9s1_profit_enhance.py tests/test_p9s2_visual_debt.py
```

3. **裸色值清零核验**（必须 `grep -P`，见 §1.3 坑 2；`-E` 会静默假绿）：

```bash
grep -nP "(?<![0-9A-Za-z_-])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9A-Za-z_-])" app/static/css/ui-v2.css app/main.py
```

   预期：**零输出**。对照基准（改动前实测）：`ui-v2.css` 6 行、`main.py` 1 行（登录页那一条长行）。**若你的命令跑出来直接是零输出而代码尚未改，说明用了 `-E`，判为无效核验。**

4. **不动项核验**：

```bash
git diff --stat                # app/static/workspace-shell.css 与 app/templates/ 均不在变更列表
wc -l app/static/css/ui-v2.css # ≥ 200
```

5. **手动起服务看一眼 `/login`**：主色、focus 环、卡片投影、字体与改动前一致（`#cbd5e1 → #ccd7e5` 的边框微变属预期）。

---

## §8 交付物

1. 代码改动（`app/import_exports.py`、`app/main.py`、`app/static/css/tokens.css`、`app/static/css/ui-v2.css`）。
2. `tests/test_p9s2_visual_debt.py` 新增；`tests/test_p5s3_visual_skeleton.py::test_t23_*` 改写。
3. `.testtmp/delivery.p9s2.md`：改动文件+行号清单 / 未做项 / 偏差 / **实测通过数** / flaky 登记 / `#cbd5e1` 微调确认 / T-23 契约变更说明。
4. `docs/review-checklist-20261011.md` 追加一节（§42）：R-72 关闭、R-59 收尾完成、T-23 契约升级记录、`#cbd5e1` 归并登记。
5. **提交，不要 push**（用户 2026-11-08 明确要求）。
