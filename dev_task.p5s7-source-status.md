# dev_task.p5s7-source-status.md — P5-S7 数据源状态条 + UI 小尾巴清理

> 版本 **v1.1**（2026-11-03，WorkBuddy 起草；v1.0 的新建端点方案经侦查作废——dashboard 已有
> 完整「数据同步健康」面板与 `GET /api/external/sync/runs` 数据源，本切片**零后端改动**）。
> 基线 **525 passed / 0 failed**。目标：**528 passed / 0 failed**（+3：T-24/25/26）。
> 提交到当前分支 `feat/warehouse-mvp`，**不要 push**。

## 0. 背景与用户裁定

- 用户裁定「平台目的是汇总」：dashboard 应一眼看到数据源新鲜度，不进 /shopify 也能知道数据新不新鲜。
- `/shopify` 定位 = 「数据源管理/连接器控制台」，**保留不动**。
- **环境新常态（2026-11-03 用户裁定，台账 §28）**：`.env` 的 `SHOPIFY_LIVE_ENABLED=true` **常开**。实施者**不得改动 `.env`**。
- 同片打包：admin 成员页静态解释文案出清（同 P5-S5「解释自己知道就行」裁定）；空态加 SVG 图标。

## 1. 硬约束（违反任何一条 = 返工）

1. **三层 CSS 体系不动摇**：`app/static/workspace-shell.css` 逐字节不动（测试字面串锁死）；`app/main.py`、`app/dashboard.py`、`app/schemas.py` **本切片零改动**。新 CSS 一律追加到 `app/static/css/ui-v2.css` **段 14**（置于段 13 之后、文件末尾）。**零新 token、零 ！important、零裸 hex**。
2. **禁区文件**（只读）：`app/identity.py`、`app/purchase_approval.py`、`app/platform_adapters/*`、`app/config.py`、`app/runtime_flags.py`、`app/dashboard.py`、`app/schemas.py`。
3. 测试前缀：`env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe -m pytest -q -p no:cacheprovider`
4. 交付说明落 `.testtmp/delivery.p5s7.md`（改动文件+行号、未做项、偏差、实测通过数）。
5. flaky 判定：首轮失败单测重跑一次全绿即过，**必须登记**。
6. T-7 色值锁（`<script>` 内 `==22`）不得变化——本任务不碰任何 `<script>` 色值。
7. 既有全局 `body`（dashboard.html 的 summary 缓存）**不得改动其赋值语义**——`loadSyncHealth` 对 `body?.sync_health || {counts:{},latest:data}` 的双源兜底是既有行为，保持原样。

## 2. 切片 A：顶部数据源状态条（dashboard.html，纯前端）

### 2a. 注入锚点（逐字，已核验唯一）

`<p id="message" class="status-line" role="status">正在加载…</p>` 之后、`<section class="cards" id="kpis">` 之前插入：

```html
<div class="source-status" id="source-status" hidden></div>
```

### 2b. 渲染逻辑（复用既有 loadSyncHealth 数据，零新请求）

- 新增函数 `renderSourceStatus(health)`：
  - 输入 = loadSyncHealth 成功分支里的 `health` 对象（两种形态都可能是：summary 的 sync_health `{counts,latest:[…]}`，或兜底 `{counts:{},latest:<runs 列表>}`——两种 latest 条目都含 `platform`/`status`；summary 形态条目另含 `heartbeat_age_seconds`，runs 形态条目含 `started_at`（ISO 字符串））
  - 按 `platform` 分组（同平台多 sync_type 行合并）：`age = min(各条 heartbeat_age_seconds)`（runs 形态用 `now - started_at` 现算，秒）
  - 每平台一个徽标，**复用既有 `.badge` 类**（shell 已有 badge-success/warning/danger/neutral，勿新造）：
    - 任一行 `failed/partial` → `badge-warning`，文案 `Shopify · 注意 · 上次同步失败`
    - 否则全 `succeeded` 且 age ≤ 72h → `badge-success`，文案 `Shopify · 正常 · <相对时间>同步`
    - age > 72h → `badge-warning`，文案 `Shopify · 数据较旧 · N 天前同步`
    - `counts.running > 0` → 追加一枚 `badge-info`「同步中」
    - `latest` 为空 → 单枚 `badge-neutral`「外部数据 · 从未同步」
  - 相对时间：`<1 分钟/N 分钟/N 小时/N 天`
  - 相对时间无法判定（age 为 null 且无 started_at）→ 文案省略时间部分
- **调用点**：`loadSyncHealth` 成功分支里、`$('#sync-health-meta').textContent=…` 之后追加 `renderSourceStatus(health);`；catch 分支保持 `#source-status` `hidden`（顶部条随面板失败一起静默，不阻塞页面）
- 徽标容器追加一个「管理」链接到 `/shopify`（`<a href="/shopify">管理</a>`，样式用既有 muted 链接样式即可，不加新类）

### 2c. 样式（ui-v2.css 段 14a）

```css
/* ===== 14. 数据源状态条与空态图标（P5-S7）===== */
/* 14a 顶部数据源状态条：徽标复用 shell 的 .badge，这里只管容器排布 */
.source-status{display:flex;flex-wrap:wrap;gap:var(--space-2);align-items:center;margin-bottom:var(--space-4)}
.source-status .source-manage{font-size:.75rem;color:var(--color-text-muted);text-decoration:none}
.source-status .source-manage:hover{color:var(--color-primary);text-decoration:underline}
```

## 3. 切片 B：admin_users.html 解释文案出清（5 处，逐字锚点已核验唯一）

删除以下 5 个完整 `<p>` 元素（整体删除，勿留空壳）：

1. `<p class="muted">管理当前 Workspace 成员、角色和仓库数据范围。</p>`
2. `<p class="panel-subtitle">仓库协作员可直接点击多个仓库，不需要按 Ctrl。</p>`
3. `<p class="muted">仓库协作方必须至少选择一个有效仓库；其他角色可稍后授权。</p>`
4. `<p class="panel-subtitle">调整角色、密码和仓库访问范围。</p>`
5. `<p class="panel-subtitle">管理当前商家的仓库，以及需要人工确认的旧仓库绑定。</p>`

**保留白名单（禁删）**：`class="muted">Workspace / <`（动态数据）、`暂无已绑定仓库`（空态）、全部服务端校验报错文案、`#message` 状态行。

## 4. 切片 C：空态 SVG 图标（ui-v2.css 段 14b）

```css
/* 14b 空态图标：数据库/圆柱体隐喻，mask+currentColor，零裸色值 */
.empty::before{
  content:"";
  display:block;
  width:22px;height:22px;
  margin:0 auto var(--space-2);
  background-color:var(--color-text-muted);
  opacity:.55;
  -webkit-mask:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='1.6'><path d='M4 7c0 1.66 3.58 3 8 3s8-1.34 8-3-3.58-3-8-3-8 1.34-8 3z'/><path d='M4 7v10c0 1.66 3.58 3 8 3s8-1.34 8-3V7'/><path d='M4 12c0 1.66 3.58 3 8 3s8-1.34 8-3'/></svg>") center/contain no-repeat;
  mask:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='1.6'><path d='M4 7c0 1.66 3.58 3 8 3s8-1.34 8-3-3.58-3-8-3-8 1.34-8 3z'/><path d='M4 7v10c0 1.66 3.58 3 8 3s8-1.34 8-3V7'/><path d='M4 12c0 1.66 3.58 3 8 3s8-1.34 8-3'/></svg>") center/contain no-repeat;
}
```

- SVG 属性串与上例逐字一致（data-URI 内单引号）；颜色只走 `background-color:var()` + `opacity`；`.empty-title/.empty-text/.empty-action` 三个子类**不得动**（test_p5s2 T-6 锁）；`cockpit.html` 的 `.chart-empty` 不动。

## 5. 测试（tests/test_p5s3_visual_skeleton.py 追加 3 条）

- **T-24 `test_t24_p5s7_source_status_bar`**：dashboard.html 含 `id="source-status"`、`hidden` 属性在位、`renderSourceStatus` 已定义且在 `loadSyncHealth` 成功分支被调用（断言 `'renderSourceStatus(health);' in t`）、`href="/shopify"` 管理链接在位、ui-v2.css 含 `.source-status{` 容器规则。
- **T-25 `test_t25_p5s7_admin_copy_cleared`**：admin_users.html 中 5 条文案逐字 `not in`；白名单 2 条（`Workspace / `、`暂无已绑定仓库`）`in`。
- **T-26 `test_t26_p5s7_section14_hygiene`**：ui-v2.css 含 `段 14` 分界注释；段 14（剥注释后）裸 hex 计数 ==0、`!important` 计数 ==0；`.empty::before` 在位且含 `mask:`；`@media` 内无段 14 内容（不碰响应式）。

## 6. 验收流程

1. 定向：`tests/test_p5s3_visual_skeleton.py tests/test_api_dashboard.py tests/test_p5s2_tables_and_empty.py tests/test_p5s1_tokens.py` 全绿
2. 全量 → **528/0**
3. 浏览器核对（如可行）：dashboard 顶部出现 Shopify 徽标、admin 页无解释文案、任一空态显示圆柱图标
4. 落 `.testtmp/delivery.p5s7.md` → 提交（`feat(P5-S7):` 前缀），**不 push**

## 7. 已排除选项（不要做）

- ❌ 新建任何后端端点/改 main.py、schemas.py、dashboard.py（侦查确认既有 sync_health 数据链路完备）
- ❌ 把 Shopify 预览数据嵌入 dashboard/orders 页（破坏消费面/管理面边界）
- ❌ 动 `/shopify` 页面（红字逻辑常开后只在凭证缺失时出现）
- ❌ 移动 `#sync-health-panel` 面板位置（改动面大、收益不明；顶部条已解决「一眼看新鲜度」）
- ❌ 新增颜色 token、改 ECharts、动 tokens 测试计数、改全局 `body` 赋值语义
- ❌ 状态条放顶栏（topbar fixed，挤占导航）
- ❌ 触碰 `.env`（常开新常态，勿动）
