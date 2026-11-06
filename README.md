# 电商商品与客服工作台

> 单机可复现的电商运营中台：本地 fixture / 真实平台只读接入 → 校验标准化 → 事实落库 →
> FastAPI 查询 → 网页工作台。覆盖**仓储协同、外部订单事实、报表导入、成本核算、利润看板、
> 补货闭环、商品页生产与客服知识**。
>
> 单进程运行，不依赖 Redis / Celery / 外部向量库；接口形状与生产实现一致，替换点集中在少数几个实现类。

## 一分钟了解当前状态

| 项 | 当前值 |
|---|---|
| 阶段 | **P8-S1 收口**（对应 P1～P8 八个阶段，详见「当前能力」） |
| 分支 | `feat/warehouse-mvp` |
| Alembic head | `0025_cost_snapshots` |
| 全量回归 | **650 passed / 0 failed**（`376.73s`，2026-11-06） |
| 规模 | 63 个 `app` 模块 / 约 2.65 万行 `app` 目录代码 / 82 个测试文件 / 142 个端点 / 10 个工作台页面 |
| 依赖 | FastAPI + SQLAlchemy + Alembic + SQLite（可切 MySQL）；无消息队列、无外部向量库 |

## 当前能力

| 阶段 | 能力 |
|---|---|
| 第一周 | 本地 fixture 采集、字段校验/标准化、SQLite/MySQL 持久化、幂等 Upsert、价格历史、FastAPI 查询 |
| S1 任务工程化 | `ThreadPoolExecutor` 真并发、SQL claim / lease / heartbeat / cancel、指数退避重试 |
| S2 文档导入 | PDF / DOCX 上传、sha256 版本化、后台解析 + 自动入队索引、商品启发式关联 |
| S3 RAG 客服问答 | 本地确定性 Embedder + 内存向量库（JSON 持久化）、top_k + 分数阈值、引用与无答案兜底 |
| S4 CLI 与设置 | `python -m app.cli` 子命令、`GET /api/settings` 运行时配置 |
| S5 受控 Agent | 只读工具白名单、JSON Schema 服务端校验、限流、审计日志 |
| S6 数据库迁移 | Alembic baseline、状态感知初始化、幂等升级、legacy 库修复迁移 |
| S7 服务稳定性 | worker 生命周期、lease 恢复、readiness、请求 ID、统一错误兜底、上传安全、CSRF 双提交 |
| 员工账户与 RBAC | session 登录、workspace 成员、管理员/运营/仓库/客服/只读角色、仓库级授权、停用启用、密码重置 |
| 仓储协同 | SKU、自有/第三方仓库、预计入库、仓库实收、差异确认、库存流水与余额 |
| 外部事实层 | 外部账户、SKU 映射、订单/明细、每日 SKU 销量、workspace 幂等隔离；外部库存与内部台账分离 |
| 补货闭环 | 单仓 SKU 健康、确定性补货建议、确认/修改/忽略、采购申请幂等提交 |
| 淘宝只读 Adapter | 只读协议、脱敏 fixture、能力查询、离线 preview；真实网络默认关闭 |
| Shopify 只读 Canary | Admin GraphQL 只读接入商品/订单/库存，多 location 库存、资源级 partial 与重试 |
| **P2 同步生产化** | 连接器能力真实化（未实现平台如实标 `implemented=false`）、授权引用指纹、来源批次归因、数据质量门禁（观察态）、死信隔离与人工重放 |
| **P3 采购审批** | 采购单草稿 → 提交 → 批准/驳回/取消（CAS 单条 UPDATE + 幂等键）、审批通过生成入库单、入库单关联采购单与到货进度 |
| **P4 经营驾驶舱** | `/cockpit`：销售趋势、库存分布、**单品利润排行**、缺货/积压待办、采购待审批、未完成入库 |
| **P5 前端设计系统** | 四层样式（`tokens.css` → `workspace-shell.css` → `ui-v2.css` 覆盖层 → 页面 `<style>`）、深色侧边栏、卡片化、统一表格/空态/徽标、页内锚点导航 |
| **P6 报表导入与导出** | 多平台 CSV/JSON 导入、中文列头智能映射、两步预览、行级容错与脏行隔离、结果/异常 xlsx 导出、统一 xlsx 导出美化 |
| **P7 成本与利润** | 入库单价 + 成本 CSV 双入口、append-only 成本快照、订单行 as-of 成本锚定、缺失回填、利润引擎与看板下钻 |
| **P8 展平报表预聚合** | 同名订单多行自动合成 1 单 N 明细、金额三态判定、按数量分摊（商品与退款）、明细行级成本独立锚定 |

> **一句话业务闭环**：平台报表（一行一个 SKU）→ 自动聚合成订单 → 明细行各自 as-of 锚定成本 →
> 利润引擎按行算钱 → 驾驶舱看单品赚亏 → 下钻到订单事实。整条链路可离线跑通，不依赖真实店铺。

## 快速开始

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium          # 仅 RPA 巡检需要

python -m app.cli init-db                      # 按库状态自动选择 Alembic upgrade / stamp
python -m app.cli init-admin --login admin --display-name 管理员 \
  --password "请替换为强密码" --tenant-key default --workspace-name 默认商家

python -m uvicorn app.main:app --reload
```

浏览器打开 <http://127.0.0.1:8000/login>。默认 SQLite 库为 `data/dianshang.db`；
切 MySQL 时复制 `.env.example` 为 `.env` 并设 `DATABASE_URL=mysql+pymysql://...`，
再用 `docker compose up -d mysql` 启动本地 MySQL。

`EMPLOYEE_AUTH_ENABLED` 默认开启，首次部署**必须先跑一次 `init-admin`**，系统不会自动创建生产账户。

### 登录与角色

| 角色 | 可见入口 |
|---|---|
| 管理员 | 全部页面 + `/admin/users` 成员权限 + `/roles` 角色矩阵 |
| 运营 | 经营驾驶舱、运营看板、订单事实、运营工作台、商品页生产、数据源、AI 助手、仓库收货、客服知识 |
| 仓库 | 仅 `/warehouse`（且限于被授权仓库） |
| 客服 / 只读 | 仅 `/customer-service` |

侧边栏由服务端按 session 的角色与权限裁剪（`main.py:_WORKSPACE_NAV_ITEMS`），**后端路由鉴权独立保留**——
隐藏入口不等于放开权限。同一账户属于多个 Workspace 时，登录需填 Workspace 数字 ID 或 `tenant_key`。
**利润数据不对只读客服开放**（未来若财务需要，单开 `finance.read` 权限项）。

## 四条主业务链路

### 1. 仓储协同（内部库存的唯一写入口）

```text
运营创建预计入库 → 仓库反馈实收/破损 → 运营确认 → 写 inventory_transactions + inventory_balances
```

- 收货阶段**不改库存**；只有运营确认才写入 `实收总数 − 破损数量` 的合格数量。
- 确认使用稳定幂等键，重复确认不会让库存翻倍；审核前库存不变。
- 页面：`/ops`（创建/复核/确认）、`/warehouse`（实收反馈）。
- 端点：`/api/skus`、`/api/warehouses`、`/api/inbounds`（含 `/receive`、`/confirm`）、`/api/inventory`。

### 2. 外部事实与同步

```text
Shopify Admin GraphQL（只读）/ JSON / CSV / mock bundle
  → 连接器 → ExternalSyncRun（成功/失败/资源级 partial）
  → ExternalOrder / ExternalOrderLine / DailySkuSale
  → ExternalInventorySnapshot
  → 驾驶舱 / 利润引擎 / AI 助手
```

- 外部 `available_qty` 只是**平台观察值**，绝不写入 `inventory_balances` / `inventory_transactions`。
- 运行可收尾为 `succeeded` / `failed` / 资源级 `partial`；失败资源走死信隔离 + 人工重放（四道闸门：状态 → 分类 → 退避 → 幂等）。
- 归因四件套（`actor` / `request_id` / `credential_ref_id` / `upstream_request_ids`）随运行落库；凭证只存 env 键名 + 8 位指纹，**不做任何可逆编码入库**。
- 页面：`/sources`（数据源，`/shopify` 已 301 重定向至此）。端点前缀：`/api/external/*`。

### 3. 报表导入 → 成本 → 利润（核心链路）

**导入**（`/sources` 页面，两步：先预览再确认）：

```bash
# 预览（不落库，返回标准化结果 + 缺列/坏行提示）
POST /api/external/imports/preview
# 确认入库（strict 默认：有脏行则 422；勾选 allow_skipped 则好行入库、脏行进隔离表）
POST /api/external/imports
# 批次异常导出（原列序 + 「系统拦截原因」列）
POST /api/external/imports/{run_id}/export
```

- 支持中文列头智能识别（`app/column_mappings.py`，17 个字段约 70 个别名，加载期做冲突自检）。
- 行级容错：默认 strict 整批拒绝；勾选跳过后坏行写入 `external_import_rejections`（唯一约束保证重传不膨胀），**全量收集不截断**，截断只作用于预览响应明细。
- 编码三档探测；CSV 导出带 UTF-8 BOM，公式注入转义（含负数豁免）。

**成本**（两条入口，都写 append-only 快照）：

```bash
# 入口一：入库单实收时填单价
POST /api/inbounds/{id}/receive        # 明细带 unit_cost
# 入口二：手工 CSV / 单条录入
POST /api/finance/cost-snapshots/preview
POST /api/finance/cost-snapshots/import
POST /api/finance/cost-snapshots
GET  /api/finance/cost-snapshots
```

**利润**：

```bash
GET /api/dashboard/cockpit?days=7&as_of=2026-11-06   # profit 段：KPI / by_currency / ranking / 缺失计数
GET /api/finance/profit/lines?days=7&sku_code=SKU-A  # 明细下钻（上限 200 行）
POST /api/finance/cost-backfill                       # 回填缺失成本（默认 dry_run=true）
```

### 4. 商品页生产与客服知识

- `/content`：内容 revision、审核、导出、媒体资产（`/api/content/*`）。
- `/customer-service`：人工工单、内部事件、知识建议（`/api/tickets`、`/api/rag/query`）；AI 建议需引用或拒答，**不自动回复、不自动关单**。
- `/assistant`：按 workspace 查询库存健康、销量波动、商品机会、补货建议与同步状态；只读。

## 关键设计

### 任务工程化（S1）

- 模块级 `ThreadPoolExecutor` 真并发；SQL claim：`UPDATE crawl_jobs SET status='running', … WHERE status IN ('queued','retry_wait') AND cancel_requested=0`，影响 0 行视为被抢占。
- 心跳线程续约 `lease_until`；进程崩溃后其他 worker 可重新抢占。取消走 `cancel_requested` 标志；失败按指数退避。

### 文档导入与 RAG（S2 / S3）

- `documents` / `document_versions` / `document_chunks` / `document_product_links` 四表，SQLite / MySQL 兼容。
- `HashEmbedder`（SHA-384 分桶，维度 256）确定性可重放；`InMemoryVectorStore` 启动从 `data/vectors.json` 加载。
- `RagAnswerer`：取 hits → 判定 `empty` / `low_score` 兜底 → 命中才调 `LLMProvider`。

### 受控 Agent（S5）

- `AgentTool` 声明 name / JSON Schema / handler / is_readonly / timeout / 限流；`call` 前做注册校验、只读拒绝、限流、参数校验。
- 每次调用写 `AgentAuditRecord`（可落 `data/agent_audit.jsonl`）；默认仅 3 个只读工具。

### 成本 as-of 物化锚定（P7）

这是本项目最容易做错、也最值得看的一段设计。

- **append-only 成本事实层**：`cost_snapshots` 只 INSERT。成本修正 = 追加新快照，历史利润在存储层不可篡改。
- **单一咽喉锚定**：`ingest_orders` 是 Shopify 同步 / 适配器 / ingest API / CSV 导入的**唯一汇合点**。写入时按 `effective_time <= 销售时间` 抓最近一条快照，把 `cost_snapshot_id + unit_cost + fee_rate + anchored_at + cost_status` **冗余固定到订单行上**。
- **已锚定行只继承、绝不重查**：平台更新订单时会删行重建，代价是锚定丢失。因此 delete 前先把五件套抄成普通元组（ORM 对象随 delete 失效），重建时按三路判定：无内部 SKU → `unmapped`；已锚定且外部 SKU 未变 → **原样继承**；其余 → as-of 重取。`missing` 也在继承范围内 —— 否则事后补成本再重传，历史 profit 会被静默改写。
- **执行顺序即正确性**：循环内只做判定（unmapped / 继承 / 入 pending），循环外一次 `_anchor_pending_lines` 完成 SELECT → 内存二分 → 回填 → flush。这样既满足「每行独立 as-of」，又满足「新增查询 ≤ 1」。
- **红线**：历史利润不允许因「今天进货」而变化 —— 由存储层 append-only + 行级物化冗余共同保证，不靠查询时重算。

### 利润口径（P7-S3）

| 项 | 公式 |
|---|---|
| 净数量 | `max(0, ordered_qty − cancelled_qty − refunded_qty)` |
| 实付净额 | `max(0, gross_amount − refund_amount)` |
| 成本 | `unit_cost × 净数量` |
| 平台费 | `实付净额 × fee_rate`（默认 5%，可按平台 override） |
| **利润** | `实付净额 − 成本 − 平台费` |

- 平台费按**实付**扣（退款不抽佣）；逐行 round 后再 SUM，保证「明细逐行之和 == KPI 总额」。
- **不可计算 ≠ 0 利润**：`missing` / `unmapped` / 缺单价费率的行不进金额汇总，只计数（黄条提示「有 X 笔订单未能计算利润」）；可算行数为 0 时 KPI value 为 `null` 而非 `"0.00"`。
- 缺失成本订单数**单开查询**：分组聚合无法跨组去重，SUM 会把一笔订单按 SKU 数重复计。
- 前端公式说明由后端 `note` 驱动 `title`，避免前后端口径漂移。

### 展平报表预聚合（P8）

淘宝/拼多多报表是「一行一个 SKU」，而内部订单是「一单多行」。预聚合把手工合并变成系统行为：

| 规则 | 内容 |
|---|---|
| 触发 | 分组键相同**且组内全部** `source_mode == "csv"`。Shopify live 走 adapter 直接造 `shopify_live` 记录，**物理上碰不到这条分支** |
| 订单级字段 | `order_status` / `currency` / `created_at` / `updated_at` 不一致 → **放弃合并**，交回重复订单号 422。`created_at` 是成本 as-of 的时间锚，取首行等于替商家编造事实 |
| 金额三态 | `SAME`（取值唯一）取该值 / `SINGLE_NONZERO`（一非零其余 0）取非零 / `SUM`（≥2 个不同非零）取各行之和 |
| 金额分摊 | 每行都等于单头（商家把订单总额复制到每一行）时，按 `单头 × 单行数量 / 总数量` 分摊，**末行补齐**保证分毫不差；商品与退款**各自独立判定** |
| 明细 id | 出现重复则整组重编号 `line:0..n-1`（CSV 无该列，不重编号必撞唯一约束） |
| 打点 | 无条件记录 `main_gross / lines_sum / delta`，`delta != 0` 升级 warning |
| 失败降级 | 任何判定不通过返回 None、**绝不抛异常** —— 增强失败不得扩大故障面 |

关键护栏：聚合发生在**行级隔离之后**（`ingest_orders` 入口），因为 `imports._isolate` 有「记录数 == 行数」不变量，在 `load_orders` 里聚合会把 3 行全判成坏行。重复订单号判重（R-65）也**移到聚合之后**，否则合法展平文件会被拦死。

### 幂等、并发与审批（P3）

- 决策三端点（approve / reject / cancel）同构：一律要求 `Idempotency-Key`，载荷哈希为 sha256 规范化 5 键；同键同载荷 → 幂等重放，同键不同载荷 → 409。
- **CAS 单条 UPDATE + rowcount 顺序判定**（禁止「先 SELECT 再赋值」）；`rowcount != 1` 按 **404 → 状态 → 版本** 分流，让并发方看到「状态已变」而不是版本号差异。
- 审批通过生成入库单；入库单回链采购单，提供到货进度。采购提交/审批**都不会自动下单、付款或改库存**。

## 前端与设计系统

- 10 个页面：`/cockpit`、`/dashboard`、`/orders`、`/ops`、`/warehouse`、`/sources`、`/content`、`/assistant`、`/customer-service`、`/admin/users`。
- 样式按加载顺序四层：`app/static/css/tokens.css`（设计 token 唯一真源，含兼容别名层）→
  `app/static/workspace-shell.css`（组件层）→ `app/static/css/ui-v2.css`（P5-S3 视觉骨架**覆盖层**，
  规则统一加 `body` 前缀把特异性提到 (0,1,1) 以压过页面内联样式，禁止裸色值与 `position:fixed`）
  → 页面内 `<style>`（页面级）。10 个模板全部按此顺序引用三份 CSS。
- 无 Jinja2：`_render_workspace_page` 读模板文件 + 正则替换 `<aside class="sidebar">…</aside>` 注入导航。模板是长行内联写法，**改动前用 `wc -c` 看体积，`wc -l` 无意义**。
- 中文化：`/static/labels.js` 为公共枚举中文映射；数据库/API 保留原始 code，页面显示中文；不直接暴露英文枚举。
- 表格数字全站左对齐 + `tabular-nums`；状态用徽标而非文字色；空态必须有明确文案，不用「0」冒充「未知」。

## 端点索引（按域分组）

| 域 | 主要端点 |
|---|---|
| 健康与登录 | `GET /health`、`GET /ready`、`GET /login`、`POST /login`、`POST /logout`、`GET /me` |
| 采集与任务 | `POST /api/crawl`（202 + job_id）、`GET /api/crawl/{id}`、`POST /api/crawl/{id}/cancel`、`GET /api/crawl/jobs` |
| 商品与文档 | `/api/products`、`/api/products/{id}/price-history`、`POST /api/documents`、`/api/documents/{id}/versions` |
| RAG 与 Agent | `POST /api/rag/query`、`GET /api/agent/tools`、`POST /api/agent/invoke`、`POST /api/assistant/query` |
| 仓储与库存 | `/api/skus`、`/api/warehouses`、`/api/inbounds`（`/receive`、`/confirm`）、`/api/inventory`、`/api/inventory/alerts`、`/api/inventory/policies` |
| 外部事实与同步 | `/api/external/accounts`、`/product-mappings`、`/orders/preview`、`/orders/ingest`、`/sales/daily`、`/sync/runs`（`/retry`、`/resume`、`/cancel`）、`/sync/dead-letters`（`/replay`、`/ignore`）、`/snapshots/freshness`、`/reconciliation/{id}` |
| 连接器 | `/api/external/connectors`、`/taobao/capabilities`、`/taobao/preview`、`/shopify/capabilities`、`/shopify/preview`、`/shopify/sync` |
| 报表导入 | `POST /api/external/imports`、`POST /api/external/imports/preview`、`POST /api/external/imports/{run_id}/export` |
| 成本与利润 | `GET/POST /api/finance/cost-snapshots`、`/cost-snapshots/preview`、`/cost-snapshots/import`、`POST /api/finance/cost-backfill`、`GET /api/finance/profit/lines` |
| 补货与采购 | `/api/replenishment/suggestions`（`/generate`、`/{id}/decision`）、`/api/purchase-requests`（`/drafts`、`/submit`、`/approve`、`/reject`、`/cancel`、`/inbound-orders`、`/receipt-progress`） |
| 看板与分析 | `GET /api/dashboard/cockpit`、`GET /api/dashboard/summary`、`/api/analytics/sales`、`/inventory-health`、`/product-quadrant`、`/replenishment-evaluation`、`/replenishment-decisions` |
| 运营与导出 | `GET /api/operations/today`、`/today/export.xlsx`、`GET /api/orders`、`/api/orders/fulfillment-proposal.xlsx` |
| 内容生产 | `/api/content/workflows`、`/api/content/runs`、`/api/content/revisions/{id}/review`、`/export`、`/api/content/assets/{id}` |
| 工单与知识 | `/api/tickets`、`/api/tickets/{id}` |
| 管理与权限 | `/api/admin/users`（`/activate`、`/deactivate`、`/reset-password`、`/warehouses`）、`/api/admin/warehouses`、`/api/admin/live-switch`、`/api/roles`、`GET /api/settings` |

## 测试

### 全量回归（标准命令）

```bash
env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest -q -p no:cacheprovider
```

**必须剥掉 `PYTHONPATH` / `PYTHONHOME`**：仓库内 `cli/vendor/shim` 的 `sitecustomize.py` 会在
pytest 清理阶段 `SystemExit(1)`，表现为全量出现 error 而非断言失败。

当前基线：**650 passed / 0 failed**（`376.73s`）。定向运行把测试路径附在命令末尾即可。

### 口径与注意事项

- **存在 flaky**：约 1/3 概率出现 1 个随机 frontend 错误，单跑通过。判定口径为「重跑一次全绿即过」，但**必须登记**。
- 测试与仓库外 `.env` 已解耦：`conftest.py` 固定 Shopify 基线，不随本机 `.env` 漂移。
- SQLite WAL 已对测试进程与子进程统一开启（`conftest.py` + `tests/_wal_bootstrap/sitecustomize.py`），把本机全量从约 39 分钟压到约 6 分钟；**仅作用于测试进程**，不改生产库配置。
- 若运行环境存在劫持 `shutil.rmtree` 的 `sitecustomize.py`，fixture 清理会报 error（非断言失败），换干净环境复跑。也可用仓库内目录作 basetemp：

```bash
python -m pytest -q -p no:cacheprovider --basetemp=./.testtmp
```

### 测试纪律

- CSS 相关测试用**无空格字面串**断言（如 `position:fixed`、`margin-left:232px`），格式化 CSS 会让测试变红。
- `<script>` 内 ECharts 色值有 24 处硬编码并被测试锁死，改动前先确认。
- 测试失败不许改测试迁就实现；确属契约变更时，在台账写明「有意的契约变更」。

## 数据库迁移

`0001_baseline` 定义 7 张基础表，其后按切片增量：

| 版本 | 内容 |
|---|---|
| `0003`–`0009` | 仓储协同、入库幂等、外部同步、员工 RBAC 与 workspace 隔离、库存告警、同步健康 |
| `0010`–`0014` | 外部账户/映射/订单/每日销量、补货建议与采购申请、同步补偿与快照来源、采购草稿 |
| `0015`–`0018` | 补货评估快照、内容生产控制平面、客服工单及其修复 |
| `0019`–`0022` | 同步游标持久化、归因四件套与授权引用、数据质量门禁（观察态）、死信表 |
| `0023` | 采购审批（7 列审批/血缘列 + 入库单来源列与唯一索引） |
| `0024` | `external_import_rejections` 脏行隔离表 |
| **`0025`（head）** | `cost_snapshots` 成本快照 + 入库行单价 + 订单行成本锚定五件套 |

```bash
alembic upgrade head          # 新库
alembic current               # 查看当前版本
alembic history --verbose
python -m app.cli init-db     # 已有 legacy 库（自动识别并 stamp 后修复）
```

- `alembic.ini` 的 `sqlalchemy.url` 留空，由 `migrations/env.py` 从 `app.config.get_settings()` 注入。
- `Compare_type=True`；`Base.metadata.create_all` **仅**用于测试 fixture 与演示兜底，不是生产迁移路径。
- 已有 legacy 库**不要**直接 `alembic stamp head`，会跳过修复迁移。
- 每个迁移的 `up`/`down` 都按「inspect 判存在」写成幂等，可双向重复执行。

## 红线与已知限制

### 不做的事（架构性约束，不是「还没做」）

- **不实现任何平台写操作**：不下单、不付款、不退款、不改价、不写回库存、不发货。
- **不把外部库存观察值写入内部台账**：`available_qty` 与 `InventoryBalance` 永远分离。
- **不用假数据冒充真实收益**：Dev Store 是官方样例数据；无广告事实表时 ROI 正确结果是 `unknown`。
- **不在订单同步的实时路径做重计算**：成本锚定只做「取快照 + 冗余固定」，利润按需查询。

### Shopify live 当前状态

本机 `.env` 按用户 2026-11-03 裁定**常开**（`SHOPIFY_LIVE_ENABLED=true`），范围**仅限官方开发店**
（`dianshang-dev.myshopify.com`，Snowboard 样例数据），全链路只读。`.env.example` 默认 `false`，
新环境不会自动放开。凭证只从 `.env` 读取，不存库、不入 git。

### 已登记的残留（摘要，完整清单见台账）

| 编号 | 内容 |
|---|---|
| R-25 / R-39 / R-40 | 热开关、live 判定、归因均依赖**进程内状态**；多 worker / 多副本部署前必须外部化 |
| R-37 | `jd`/`pdd`/`douyin`/`amazon` 仅在常量中声明，**无适配器**，能力清单如实标 `implemented=false` |
| R-45 | 数据质量门禁为**观察态**，`failed` 不拦截入库 |
| R-48 / R-49 | 死信无跨进程调度，重放靠人工触发；补偿上限 3 与退避 30s 是拍板值非 SLA |
| R-52 | 采购单**无金额/单价/供应商报价**字段，审批无法体现「批多少钱」 |
| R-59 | `/login` 主色 `#2563eb` 与全站主色 `#2a78d6` 不一致（登录页是 `main.py` 内联 HTML，改它须先解 P1-1 冻结） |
| R-66 / R-67 / R-71 | 已关闭：预聚合、商品金额分摊、退款分摊 |
| R-68 | 合并后 `external_line_id` 按**位置**重编号，商家调整行序会导致锚定错位 |
| R-69 | 同一订单的行**分散在两个文件**时不会合并，需「订单级 UPSERT + 行级 MERGE」 |
| — | 利润明细单页上限 200 行，无全量导出（已接受，留后续切片） |
| — | 利润**无环比**；只读客服角色看不到利润（有意为之，未来单开 `finance.read`） |

### 其他限制

- 单进程假设：取消 token、单飞闸门、热开关均不跨进程。
- MySQL 迁移与 offline SQL 未在本机环境验证；日常验证走 SQLite。
- 广告 ROI、客服工单等未接入真实渠道的能力，一律返回 `unknown` 或空态，不伪造 0。

## 演示数据

`seed-demo` 提供**完全虚构、仅限本地 SQLite** 的演示 Workspace（商品、SKU、两仓、已确认库存、
30 天销量、低库存告警、补货建议、采购草稿），只接受 `demo-` 前缀且拒绝 MySQL：

```bash
python -m app.cli init-db
python -m app.cli seed-demo --workspace demo-shop --workspace-name "[虚构] 电商演示商家"
python -m app.cli init-admin --login demo-admin --display-name "演示管理员" \
  --password "DemoPass123!" --tenant-key demo-shop --workspace-name "[虚构] 电商演示商家"
```

命令幂等；可加 `--as-of-date 2026-09-07` 固定数据日期，`--json` 查看完整结果。
页面上会明确显示「演示数据，非真实业务」；未接入的事实显示空态而非 0。
需要重来时删掉本地 `data/dianshang.db` 与 `data/vectors.json` 重新初始化（**只对本地演示库**）。

## 生产演进路径

业务逻辑（connectors / parsers / repository / agent tools）零改动，仅替换实现：

| 当前（单机） | 生产替换 |
|---|---|
| `ThreadPoolExecutor` | Celery / RQ worker + Redis broker |
| `UPDATE … WHERE status` claim | `SELECT … FOR UPDATE SKIP LOCKED` |
| `InMemoryVectorStore` | Qdrant / pgvector |
| `HashEmbedder` | BGE-M3 / DashScope Embedding |
| `StubLLMProvider` | Claude / OpenAI / Qwen function calling |
| 进程内热开关与归因 | 外部配置中心 + 跨进程 trace（须先解决 R-25/R-39/R-40） |
| `.env` 单凭证 | 多租户 OAuth + secret vault（`external_credential_refs` 已留指纹位） |

Alembic 已是生产 schema 管理路径，不列为替换项。

## 开发协作

本项目按「规划方（需求拆解、接口契约、用例设计、独立复核）+ 实现方（代码编写）」分工推进：

- 开发规范：`CLAUDE.md`
- 交接契约：`docs/claude-handoff-contract.md`（§5 复核方法、§6.1 分支模型：只有 `main` 与 `feat/<slice>`）
- 复核台账：`docs/review-checklist-20261011.md`（按 § 编号连续登记，含每片的实测数字与残留）
- 任务文件：`dev_task.<slice>.md`（用户规则写死在任务书里，验收闸逐条给判定口径）

约定：任务以仓库内文件下发；交付必须附**原始测试输出**与用例映射；**无原始证据不得标记「完成」**。
缺陷按 `R-<n>` 连续编号、不跳号不复用，关闭时在台账写明实测前后对比。

## 文档

- [第一周架构图](docs/architecture.md) / [第一周验收](docs/week1-acceptance.md) / [第二周验收](docs/week2-acceptance.md)
- [交接契约](docs/claude-handoff-contract.md) / [工程长期路线](docs/engineering-roadmap.md) / [ADR 索引](docs/adr-index.md)
- [领域边界与事实登记](docs/domain-boundaries.md) / [产品架构基线](docs/product-architecture.md) / [架构盘点](docs/architecture-inventory.md)
- [指标契约](docs/metrics.md) / [运营工作台 Runbook](docs/mvp-operations-workbench-runbook.md) / [P0-0 发布审查](docs/p0-0-release-review.md)
- [Shopify Canary Runbook](docs/shopify-canary-runbook.md) / [live 演练记录](docs/live-drill-runbook-20261028.md) / [MCP 工具说明](docs/mcp-tools.md) / [电商 Skill 分析](docs/skills-ecommerce-analysis.md)
- [复核台账](docs/review-checklist-20261011.md) / [竞品回放采集说明](docs/catalog-replay.md)

## Playwright 浏览器安装（国内网络）

```powershell
$env:PLAYWRIGHT_DOWNLOAD_HOST = "https://npmmirror.com/mirrors/playwright"
python -m playwright install chromium
```

验证可启动：

```powershell
python -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); print('chromium ok'); b.close(); p.stop()"
```

镜像只用于下载浏览器运行时；采集器本身仍只访问项目内本地 fixture。
