# dev_task.p5s7-source-status.md — P5-S7 数据源状态条 + UI 小尾巴清理

> 版本 v1.0（2026-11-03，WorkBuddy 起草）。基线 **525 passed / 0 failed**（`22a5019`+`76321f9`+`448f017` 后，台账提交 `448f017` 为 docs-only）。
> 目标：**528 passed / 0 failed**（+3：T-24/25/26）。提交到当前分支 `feat/warehouse-mvp`，**不要 push**。

## 0. 背景与用户裁定

- 用户裁定「平台目的是汇总」：dashboard 应直接呈现数据源新鲜度，不再要求用户进 /shopify 查同步状态。
- `/shopify` 定位改为「数据源管理/连接器控制台」，保留不动。
- **环境新常态（2026-11-03 用户裁定，台账 §28）**：`.env` 的 `SHOPIFY_LIVE_ENABLED=true` **常开**。实施者**不得改动 `.env`**；全量回归与本切片无关（测试与 .env 解耦）。
- 同片打包：admin 成员页静态解释文案出清（同 P5-S5 用户裁定「解释自己知道就行」）；空态加 SVG 图标。

## 1. 硬约束（违反任何一条 = 返工）

1. **三层 CSS 体系不动摇**：`app/static/workspace-shell.css` 与 `app/main.py` 中与 shell 相关的部分逐字节不动（测试字面串锁死）。新 CSS 一律进 `app/static/css/ui-v2.css`（追加**段 14**，置于段 13 之后、文件末尾）；新 token 只在确有必要时进 `tokens.css`（本任务默认**零新 token、零新 ！important、零裸 hex**）。
2. **禁区文件**（只读）：`app/identity.py`、`app/purchase_approval.py`、`app/platform_adapters/*`、`app/config.py`、`app/runtime_flags.py`。
3. **测试纪律**：定向 + 全量都用剥垫片前缀：
   `env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe -m pytest -q -p no:cacheprovider`
4. 交付说明落 `.testtmp/delivery.p5s7.md`（改动文件+行号、未做项、偏差、实测通过数）。
5. flaky 判定按惯例：首轮失败的单测重跑一次，全绿即过但必须在交付说明登记。
6. ECharts 色值锁（T-7 `==22`）不得变化——本任务不碰任何 `<script>` 色值。

## 2. 切片 A：数据源状态条（dashboard）

### 2a. 后端：`GET /api/dashboard/source-status`（main.py，加在 `/api/dashboard/cockpit` 端点之后、风格同款）

- 权限：`require_principal(request, permission="inventory.read")`，`get_db` 依赖；workspace 作用域过滤（取 principal 的 workspace_id，与 summary/cockpit 端点同款取法——先读 `dashboard_overview` 的取法再照抄，不要自创）。
- 查询：`external_sync_runs` 表按 `platform` 分组取**每个平台最近一条 run**（按 `started_at` desc；单 workspace 内 `platform='shopify'` 实际只有一族，但 SQL 按 platform 分组写法为多平台扩展留口）。
- 响应模型 `SourceStatusResponse`（pydantic，放现有 dashboard schemas 所在文件/区域）：
  ```python
  class SourceItem(BaseModel):
      platform: str
      last_status: str | None        # succeeded/failed/running/…（原样透传 status）
      last_finished_at: str | None   # ISO 字符串，None=进行中或从未跑
      simulated: bool | None         # 最近一次是否模拟
      error_code: str | None
      total: int | None              # 该 run 同步条数
  class SourceStatusResponse(BaseModel):
      sources: list[SourceItem]
  ```
- 无任何 run 时返回 `sources: []`（前端渲染「从未同步」灰徽标）。
- **只读**：绝不写任何表；单条 SQL（或一次 select + python 分组），禁止 N+1。

### 2b. 前端：dashboard.html

- 注入锚点（逐字）：`<p id="message" class="status-line" role="status">正在加载…</p>` 之后、`<section class="cards" id="kpis">` 之前，插入：
  ```html
  <div class="source-status" id="source-status" hidden></div>
  ```
- JS：现有 `loadSummary()`（或等价的 summary 拉取函数）成功后追加拉取 `/api/dashboard/source-status` 并渲染：
  - 每平台一个徽标：`Shopify · 成功 · 3 小时前同步` / `Shopify · 从未同步` / `Shopify · 失败：SHOPIFY_PAGE_LIMIT`
  - 相对时间：`<1 分钟/ N 分钟/ N 小时/ N 天`；`last_finished_at` 缺失且 status=running → 「同步中」
  - **新鲜度**：`last_finished_at` 距今 > 72h 或最近一次失败 → 徽标用警示色（`--color-warning`），其余用成功色（`--color-success`）；从未同步用中性灰（`--color-text-muted`）
  - 渲染失败（接口 4xx/5xx）→ 保持 `hidden`，不影响页面其他功能（console.warn 即可）
- 样式进 ui-v2.css **段 14a**：`.source-status{display:flex;gap:var(--space-2);align-items:center;margin-bottom:var(--space-4)}` + 徽标复用既有 `.status-badge`（若存在）或最小化自定义（仅 var() 色 + var() 间距）。查一下 shell/ui-v2 里已有徽标类名再决定，**禁止重复造轮子**。

## 3. 切片 B：admin_users.html 解释文案出清（5 处，逐字锚点已核验唯一）

删除以下 5 个完整元素（都是 `<p>` 整行元素，直接整体删除，勿留空壳）：

1. `<p class="muted">管理当前 Workspace 成员、角色和仓库数据范围。</p>`
2. `<p class="panel-subtitle">仓库协作员可直接点击多个仓库，不需要按 Ctrl。</p>`
3. `<p class="muted">仓库协作方必须至少选择一个有效仓库；其他角色可稍后授权。</p>`
4. `<p class="panel-subtitle">调整角色、密码和仓库访问范围。</p>`
5. `<p class="panel-subtitle">管理当前商家的仓库，以及需要人工确认的旧仓库绑定。</p>`

**保留**（白名单，禁删）：`class="muted">Workspace / <`（动态数据）、`暂无已绑定仓库`（空态）、所有服务端校验报错文案、`#message` 状态行。

## 4. 切片 C：空态 SVG 图标（ui-v2.css 段 14b）

- 给 `.empty` 加纯 CSS 装饰图标（mask 手法，同侧栏 nav-icon 先例）：
  ```css
  .empty::before{
    content:"";
    display:block;
    width:22px;height:22px;
    margin:0 auto var(--space-2);
    background-color:var(--color-text-muted);
    opacity:.55;
    -webkit-mask:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='1.6'><path d='M4 7a8 3 0 1 0 16 0A8 3 0 0 0 4 7v10c0 1.66 3.58 3 8 3s8-1.34 8-3V7'/><path d='M4 7v10c0 1.66 3.58 3 8 3s8-1.34 8-3V7'/></svg>") center/contain no-repeat;
    mask:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='1.6'><path d='M4 7a8 3 0 1 0 16 0A8 3 0 0 0 4 7v10c0 1.66 3.58 3 8 3s8-1.34 8-3V7'/><path d='M4 7v10c0 1.66 3.58 3 8 3s8-1.34 8-3V7'/></svg>") center/contain no-repeat;
  }
  ```
  （数据库/圆柱体图标，隐喻「暂无数据」；SVG 属性串必须与上例逐字一致，data-URI 内用单引号。）
- 约束：零裸 hex（颜色只走 currentColor/`background-color:var()` + opacity）、零 ！important；`.empty-title/.empty-text/.empty-action` 三个类不得动（test_p5s2 T-6 锁存在性）。
- `cockpit.html` 的 `.chart-empty` 不动（测试锁它在位）。

## 5. 测试（tests/test_p5s3_visual_skeleton.py 追加 3 条 + test_api_dashboard.py 补 marker）

- **T-24 `test_t24_source_status_endpoint`**（test_api_dashboard.py 或同风格文件）：
  - 带 admin 登录态 GET `/api/dashboard/source-status` → 200，响应含 `sources` 数组（空数组或含 shopify 条目均可断言 schema：字段名逐一校验）
  - 无登录 → 401/403（与既有端点断言同款写法）
  - dashboard.html 含 `id="source-status"` 与 `/api/dashboard/source-status` 引用；`hidden` 属性在位
- **T-25 `test_t25_p5s7_admin_copy_cleared`**：admin_users.html 5 条文案逐字 `not in`；白名单 2 条 `in`。
- **T-26 `test_t26_p5s7_section14_hygiene`**：ui-v2.css 含 `段 14` 分界注释；段 14 内（剥注释后）零裸 hex（正则 `#[0-9a-fA-F]{3,8}` 计数 ==0）、`!important` 计数 ==0；`.empty::before` 在位且含 `mask:`；`.empty-title` 等三类的 shell 定义未被触碰（T-6 已覆盖，可引断言补充即可）。
- test_api_dashboard.py 既有 marker 循环（`'sales-trend-panel',…`）不动。

## 6. 验收流程

1. 定向：`pytest tests/test_p5s3_visual_skeleton.py tests/test_api_dashboard.py tests/test_p5s2_tables_and_empty.py tests/test_p5s1_tokens.py -q`（剥垫片前缀）→ 全绿
2. 全量：剥垫片前缀全量 → **528/0**
3. 浏览器核对（如可行）：dashboard 出现数据源徽标、admin 页无解释文案、造一个空态看图标
4. 落 `.testtmp/delivery.p5s7.md` → git add 全部改动 → 提交（信息前缀 `feat(P5-S7):`），**不 push**

## 7. 已排除选项（不要做）

- ❌ 把 Shopify 预览数据嵌入 dashboard/orders 页（破坏「消费面/管理面」边界，用户已认可控制台模式）
- ❌ 动 `/shopify` 页面本身（含其红字逻辑——常开后该文案只在凭证缺失时出现）
- ❌ 新增颜色 token、改 ECharts、动 tokens 测试计数
- ❌ 同步状态条放顶栏（topbar 是 fixed 布局，挤占导航）
- ❌ 触碰 `.env`（已常开，勿动）
