# 电商商品与客服工作台



> “本地商品 fixture 采集 → 校验/标准化 → 持久化 → FastAPI 查询 → 网页展示”，
> 在此基础上扩展任务工程化、文档导入、RAG 客服问答与受控 Agent 工具。
> 单机单进程演示，不依赖 Redis / Celery / 外部向量库；接口与生产实现一致。

## 当前能力

| 切片 | 能力 |
|---|---|
| 第一周 | 本地 fixture 采集、字段校验/标准化、SQLite/MySQL 持久化、幂等 Upsert、价格历史、FastAPI 查询、极简网页 |
| S1 任务工程化 | `ThreadPoolExecutor` 真并发、`SQL claim / lease / heartbeat / cancel`、指数退避重试 |
| S2 文档导入 | PDF / DOCX 上传、sha256 版本化、后台解析 + 自动入队索引、商品启发式关联 |
| S3 RAG 客服问答 | 本地假 Embedder + 内存向量库（JSON 持久化）、top_k + 分数阈值、引用与无答案兜底 |
| S4 CLI 与设置 | `python -m app.cli` 子命令、`GET /api/settings` 运行时配置 |
| S5 受控 Agent | 只读工具白名单、JSON Schema 服务端校验、限流、审计日志 |
| S6 数据库迁移 | Alembic baseline、状态感知初始化、幂等升级、legacy 库修复迁移 |
| S7 服务稳定性 | worker 生命周期、lease 恢复、readiness、请求 ID、统一错误兜底 |
| S7 安全硬化 | 上传签名/路径/SHA 校验、Agent 配额、审计摘要脱敏、CSRF 双提交、可选 API Key |
| 员工账户与角色工作台 | session 登录、workspace 成员、运营/仓库/客服/只读角色、仓库授权、停用/启用、密码重置、管理员页面和角色权限页 |
| 仓储协同 MVP | SKU、自有/第三方仓库、预计入库、仓库实收、差异确认、库存流水与余额；`/ops` 负责创建/复核/确认，`/warehouse` 负责实收反馈 |
| 外部平台连接规划 | 国内平台（淘宝/天猫、京东、拼多多、抖音电商）优先作为首个只读连接器候选；Amazon/FBA 作为跨境扩展，统一通过 Adapter/Connector 接入 |
| 运营概览看板 | `/dashboard` 与 `/api/dashboard/summary`：已确认内部库存、入库状态/数量、失败任务、口径限制和模拟数据提示 |

当前版本已完成员工账户与角色工作台基础闭环，但**完整多租户/RBAC 仍未完成**：仓库列表、入库和库存已按当前 workspace/授权仓库限制；商品、文档、任务、向量、外部快照等全链路 workspace 隔离仍在后续阶段。

## 员工登录与角色工作台

员工认证默认开启（`EMPLOYEE_AUTH_ENABLED=true`）。首次部署不会自动创建生产账户，请先在目标数据库执行一次：

```bash
python -m app.cli init-admin --login admin --display-name 管理员 --password "请替换为强密码" --tenant-key default --workspace-name 默认商家
```

登录后，入口会按角色跳转：

- `/ops`：管理员/运营创建商品 SKU、仓库和预计入库；查看仓库收货反馈并确认入账。
- `/warehouse`：仓库协作方查看授权仓库的预计入库单，提交实收总数和破损数量；不直接改库存。
- `/admin/users`：管理员创建运营、仓库、客服和只读账户，分配/撤销仓库授权，停用/启用账户和重置密码。
- `/roles`：管理员查看固定角色及权限说明。
- `/dashboard`：管理员/运营查看运营概览。

浏览器 session 写请求使用 CSRF 双提交校验；页面会自动携带 `X-CSRF-Token`。API Key 是机器调用凭证，不用于员工网页登录；生产环境请按实际部署开启 HTTPS 和安全 Cookie。

## 启动

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

初始化数据库（按状态自动选择 Alembic upgrade/stamp）：

```bash
python init_db.py
# 或：
python -m app.cli init-db
# 直接使用 Alembic（新库或已迁移库）：
DATABASE_URL="sqlite:///./data/dianshang.db" alembic upgrade head
```

启动 API：

```bash
python -m uvicorn app.main:app --reload
```

浏览器访问 <http://127.0.0.1:8000/>。

默认 SQLite 数据库文件为 `data/dianshang.db`。切到 MySQL 时，复制 `.env.example` 为 `.env`，设置：

```text
DATABASE_URL=mysql+pymysql://dianshang:dianshang@localhost:3306/dianshang?charset=utf8mb4
```

然后用 `docker compose up -d mysql` 启动本地 MySQL。新库或已迁移库使用 `alembic upgrade head`；已有 legacy 数据库不要直接执行 `alembic stamp head`，使用 `python -m app.cli init-db`，它会标记 0001 baseline 后执行后续修复迁移。`python init_db.py` 和 `python -m app.cli init-db` 会按数据库状态自动选择初始化方式。

## 数据库迁移（Alembic，S6）

S6 将 Alembic 作为当前 schema 管理规范：`migrations/versions/0001_baseline.py` 定义基础 7 张表，`0003_inventory_mvp.py` 增加仓储协同与库存表，`0004_inbound_idempotency.py` 补齐入库幂等字段，`0005_external_sync.py` 增加外部库存快照、事件 Inbox 和对账结果表；`init_db.py` 负责识别数据库状态并执行对应动作；`Base.metadata.create_all` 仅保留给测试 fixture 或演示兜底，不作为生产迁移路径。当前迁移头为 `0006_workspace_rbac`；员工身份模型和 InventoryPolicy 已进入 ORM/迁移，完整业务对象的 workspace 全链路隔离仍在后续阶段补齐。

```bash
# 新库：创建全部表并写入 alembic_version
alembic upgrade head

# 查看版本与历史
alembic current
alembic history --verbose

# 修改 model 后生成并应用增量迁移
alembic revision --autogenerate -m "add orders table"
alembic upgrade head

# 已有 legacy 数据库：通常由 init-db 自动处理
python -m app.cli init-db
```

迁移配置：
- `alembic.ini`：`sqlalchemy.url` 留空，由 `migrations/env.py` 从 `app.config.get_settings().database_url` 注入。
- `migrations/env.py`：导入全部 model，`target_metadata = Base.metadata`，`compare_type=True` 捕获类型变更。
- `0001_baseline.py`：创建基础 7 张表：`products`、`product_price_history`、`crawl_jobs`、`documents`、`document_versions`、`document_chunks`、`document_product_links`。
- `0003_inventory_mvp.py`：创建 `product_skus`、`warehouses`、`inbound_orders`、`inbound_lines`、`inventory_balances`、`inventory_transactions`。
- `0004_inbound_idempotency.py`：为 `inbound_orders` 增加收货/确认幂等键及 payload hash 字段。
- `0005_external_sync.py`：增加外部库存快照、事件 Inbox、对账结果表。

## CLI 用法

```bash
# 查看可用命令
python -m app.cli --help

# 初始化数据库
python -m app.cli init-db

# 注入示例 PDF + DOCX 并写入向量库（无需先启动 uvicorn；重复执行会复用 ready 版本）
python -m app.cli seed-docs

# CLI：入队后等待 ThreadPoolExecutor 任务完成
python -m app.cli enqueue-crawl

# RAG 客服问答（demo 检索）
python -m app.cli query "green tea"

# 打印运行时配置与数据库 / 向量库规模
python -m app.cli status
```

## API 速查

| Method | Path | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/` | 极简工作台页面 |
| POST | `/api/crawl/fixture` | **第一周同步入口**（演示兼容） |
| POST | `/api/crawl` | **S1 异步入口**，立即 202 返回 `{job_id}` |
| GET | `/api/crawl/{id}` | 任务详情（status / attempt / worker_id / lease_until / cancel_requested） |
| POST | `/api/crawl/{id}/cancel` | 请求取消任务 |
| GET | `/api/crawl/jobs` | 最近任务列表 |
| GET | `/api/products?page=&page_size=&keyword=&category=` | 商品分页与筛选 |
| GET | `/api/products/{id}` | 商品详情 |
| GET | `/api/products/{id}/price-history` | 价格历史 |
| POST | `/api/documents` | **S2 文档上传**（multipart PDF/DOCX） |
| GET | `/api/documents/{id}/versions` | 文档版本列表（含 `chunk_count`） |
| POST | `/api/rag/query` | **S3 RAG 客服问答** |
| GET | `/api/agent/tools` | **S5 受控 Agent 工具列表** |
| POST | `/api/agent/invoke` | **S5 受控工具调用** |
| GET | `/api/settings` | **S4 运行时配置 + 规模** |
| GET | `/api/external/connectors` | 列出国内平台候选及 Amazon 的离线连接器能力（只读/mock） |
| POST | `/api/external/inventory/preview` | 预览并标准化外部库存数据，不落库 |
| POST | `/api/external/inventory/ingest` | 幂等写入外部库存快照，不修改内部库存 |
| POST | `/api/external/events/ingest` | 幂等写入外部事件 Inbox，不自动投影库存 |
| GET | `/api/external/reconciliation/{snapshot_id}` | 对比外部快照和内部库存，生成只读结果 |
| GET | `/api/admin/users` | 管理员查看当前工作空间成员 |
| POST | `/api/admin/users` | 管理员创建运营/仓库/客服/只读账户 |
| PATCH | `/api/admin/users/{id}` | 修改成员名称或角色 |
| POST | `/api/admin/users/{id}/reset-password` | 管理员重置成员密码 |
| POST/DELETE | `/api/admin/users/{id}/warehouses/{warehouse_id}` | 管理员授予/撤销仓库范围 |
| POST | `/api/admin/users/{id}/activate` / `deactivate` | 管理员启用/停用成员 |
| GET | `/api/admin/warehouses` | 查看当前工作空间仓库及未绑定 legacy 仓库 |
| POST | `/api/admin/warehouses/{id}/bind` | 将未绑定 legacy 仓库绑定到当前工作空间 |
| GET | `/api/roles` | 管理员查看固定角色和权限矩阵 |
| GET | `/roles` | 管理员角色权限页面 |
| GET | `/api/dashboard/summary` | 运营概览聚合（已确认库存、入库状态/数量、失败任务和能力边界说明） |
| GET | `/dashboard` | 运营看板页面（仅管理员/运营） |

### 外部连接器离线演示

没有真实平台店铺也可以完整演示第一阶段：

```bash
# 查看候选平台能力；当前全部只读、离线、模拟
python -m app.cli external-list-connectors

# 预览淘宝样例，不写数据库
python -m app.cli external-preview --platform taobao --mode json --file examples/external/taobao_inventory.json

# 预览京东 CSV
python -m app.cli external-preview --platform jd --mode csv --file examples/external/jd_inventory.csv

# API 预览请求（认证关闭的本地演示环境）
curl -X POST http://127.0.0.1:8000/api/external/inventory/preview \\
  -H "Content-Type: application/json" \\
  -d @examples/external/taobao_inventory.json
```

`available_qty` 是平台观察值；导入只保存外部快照，事件只进入 Inbox，对账只生成报告，不会直接改变内部库存。真实淘宝/天猫、京东、拼多多、抖音或 Amazon API 需要后续的官方授权和独立 Adapter，目前不在已完成能力中。


```bash
# S1 异步采集
curl -X POST http://127.0.0.1:8000/api/crawl

# S2 上传 PDF
curl -X POST http://127.0.0.1:8000/api/documents \
  -F "file=@fixtures/sample.pdf" -F "title=茶叶规格"

# S3 RAG 查询
curl -X POST http://127.0.0.1:8000/api/rag/query \
  -H "Content-Type: application/json" \
  -d '{"query": "green tea"}'

# S5 受控 Agent 调用
curl -X POST http://127.0.0.1:8000/api/agent/invoke \
  -H "Content-Type: application/json" \
  -d '{"tool": "search_products", "input": {"category": "茶饮"}}'
```

## 关键设计

### 任务工程化（S1）

- 模块级 `ThreadPoolExecutor(max_workers=WORKER_CONCURRENCY)` 真正并发跑任务。
- SQL claim：`UPDATE crawl_jobs SET status='running', worker_id=?, lease_until=? WHERE id=? AND status IN ('queued','retry_wait') AND cancel_requested=0`；影响 0 行视为被抢占或被取消。
- 心跳线程定期续约 `lease_until`，进程崩溃后其他 worker 可重新抢占。
- 取消通过 `cancel_requested` 标志，状态机在每条记录提交前检查。
- 失败按指数退避回到 `retry_wait`，达到 `max_retries` 转为 `failed`。

### 文档导入（S2）

- `documents` / `document_versions` / `document_chunks` / `document_product_links` 四张表，SQLite / MySQL 兼容。
- `pypdf` 按页抽取 PDF；`python-docx` 按段落 / 标题 / 表格抽取 DOCX。
- sha256 唯一约束保证同一字节只入库一次；重复上传复用 `document + version`。
- 解析完成后自动入队 `document_index` 任务，调用本地 Embedder 写入向量库。

### RAG 检索（S3）

- `HashEmbedder`：`SHA-384` 分桶 + 归一化，维度 256，确定性 / 可重放 / 零依赖；生产替换 BGE-M3 / DashScope 即可，接口不变。
- `InMemoryVectorStore`：启动从 `data/vectors.json` 加载；写入追加保存；过滤字段 `chunk_id / document_id / document_version_id / snippet / locator / version_no`。
- `RagAnswerer`：先 `RagRetriever.retrieve` 取 hits，再判定 `empty / low_score` 兜底；命中后调用 `LLMProvider.answer`。
- 单例 `app/rag_runtime.py` 使用 `RLock`，避免 `get_answerer → get_retriever → get_embedder` 嵌套调用死锁。

### 受控 Agent（S5）

- `AgentTool` dataclass 声明 name / description / JSON Schema / handler / is_readonly / timeout / max_calls_per_minute。
- `AgentToolRegistry.call` 在调用 handler 前做：注册校验、is_readonly 拒绝、限流计数、参数校验；失败全部转化为 `ToolCallResult` 而非异常。
- `AgentOrchestrator.invoke` 每次调用前后写 `AgentAuditRecord`，可持久化到 `data/agent_audit.jsonl`。
- 默认仅暴露 3 个只读工具：`search_products` / `get_product_detail` / `get_price_history`。

## 生产演进路径

业务逻辑（crawler / parsers / repository / agent tools）零改动；仅替换以下实现：

| 当前（demo） | 生产替换 |
|---|---|
| `ThreadPoolExecutor` | Celery worker 进程 + Redis broker |
| `UPDATE … WHERE status` claim | `SELECT … FOR UPDATE SKIP LOCKED` |
| `InMemoryVectorStore` | Qdrant / pgvector |
| `HashEmbedder` | BGE-M3 / DashScope Embedding |
| `StubLLMProvider` | Claude / OpenAI / Qwen function calling |

Alembic 已是当前生产 schema 管理路径；`Base.metadata.create_all` 仅用于测试 fixture 和演示兜底，不列为生产替换项。

## 仓储协同 MVP

项目当前包含轻量仓储协同后端，不做完整 WMS。核心流程为：

```text
运营创建预计入库 → 仓库反馈实际 SKU/数量/破损 → 运营确认 → 库存流水与余额更新
```

支持自有仓和第三方仓的统一记录，库存确认使用事务和稳定幂等键，审核前不改变库存。当前接口：

| Method | Path | 说明 |
|---|---|---|
| POST/GET | `/api/skus` | 创建/查询商品 SKU |
| POST/GET | `/api/warehouses` | 创建/查询自有或第三方仓库 |
| POST | `/api/inbounds` | 创建预计入库单 |
| GET | `/api/inbounds/{id}` | 查看入库明细和差异 |
| POST | `/api/inbounds/{id}/receive` | 提交仓库实收数量和破损数量 |
| POST | `/api/inbounds/{id}/confirm` | 运营确认并更新库存 |
| GET | `/api/inventory` | 查询仓库-SKU 库存余额，支持 `warehouse_id`、`sku_id`、`page`、`page_size` |

### 仓储前端演示

首页 `/` 已提供无需额外前端构建的原生 HTML/JavaScript 仓储工作台。首次使用时按以下顺序操作：

1. 点击“采集样例商品”，准备可关联的商品资料。
2. 在“基础资料”中选择商品并创建 SKU，再创建自有或第三方仓库。
3. 选择仓库和 SKU，填写预计数量，创建预计入库单。
4. 在 `/warehouse` 页面选择入库单，填写实收总数（含破损）和破损数量，提交仓库反馈。
5. 运营回到 `/ops`，点击“刷新待确认列表”，选择并加载已反馈入库单，然后点击“运营确认入账”；确认成功后刷新库存表。

- 收货和确认请求携带当前入库单对应的 `Idempotency-Key`；“实收总数（含破损）”表示仓库现场收到的全部数量，破损数量是其中的组成部分。收货反馈阶段不会更新库存，只有运营确认成功后才写入 `实收总数 - 破损数量` 的合格数量。页面会展示数量差异 `实收总数 - 预计数量`，并根据 `expected → received → confirmed` 状态控制按钮，重复确认不会使库存翻倍。当前页面是轻量演示，不包含复杂库位、波次、拣货，也不连接真实平台。

## 外部平台与运营增效规划

当前不连接真实平台生产 API，先用 `app/connectors.py` 的离线连接器和 `examples/external/` 示例数据验证统一标准化、外部库存快照、事件 Inbox、幂等重放和库存对账。已提供候选平台能力清单以及库存预览/导入/对账 API 基础入口；它们都标记 `simulated=true`、`live_enabled=false`，不访问网络、不需要店铺凭证。

国内平台优先作为求职准备阶段的起步候选：淘宝/天猫、京东、拼多多、抖音电商；Amazon/FBA 作为跨境扩展候选。每个平台未来独立封装认证、签名、限流、分页、重试、错误码和字段映射，核心业务不依赖平台协议；具体接口与能力只以官方文档和实际授权为准。

当前可复现入口：

```bash
python -m app.cli external-list-connectors
python -m app.cli external-preview --platform taobao --mode json --file examples/external/taobao_inventory.json
python -m app.cli external-import --platform taobao --mode json --file examples/external/taobao_inventory.json
```

外部 `available_qty` 只是平台观察值，不直接写入 `inventory_balances` 或 `inventory_transactions`；内部库存仍通过“预计入库 → 仓库实收 → 运营确认”流程更新。没有真实店铺不影响连接器底座、字段映射、幂等和对账能力的演示，但不能把 mock 导入表述为真实平台接入或生产指标。

后续增效模块按以下顺序推进：

1. 外部库存快照与事件对账：平台可售/锁定/在途、店铺/仓库、外部 SKU（国内平台）或 ASIN/seller SKU/marketplace/FBA 字段（Amazon）单独保存，不直接伪装为本系统库存流水。
2. Dashboard 与告警：库存水位、入库趋势、平台与内部库存差异、同步失败和低库存告警；没有真实埋点时只展示演示数据或目标指标。
3. Playwright RPA 巡检：仅访问本人或明确授权页面，复用人工登录态，保存截图和原始快照；遇到验证码/MFA/页面异常时提示人工介入，不绕过平台限制。
4. 飞书/钉钉 Bridge：先发送库存差异和入库审核通知，再实现验签、时间戳校验、事件幂等、用户映射和仓库权限复核后的人工审批回调。
5. 运营分析 Agent/Dify：读取商品、库存、对账和售后资料，输出库存风险、入库差异和运营简报；不直接确认入库、不调整库存、不执行平台交易动作。

这条路线将项目从“被动 CRUD + 客服问答”扩展为“外部信息感知 → 自动对账 → 异常告警 → AI 建议 → 人工审批 → 内部台账”，但不会把未经授权的真实接入或目标指标写成已完成能力。


### 运营概览看板

已提供 `/dashboard` 和 `/api/dashboard/summary`，面向管理员/运营展示已确认内部库存、库存组合数、入库状态与数量、失败任务以及当前不可计算指标。页面保持原生 HTML/JavaScript，不引入图表构建链；外部平台快照暂不跨来源汇总，动态低库存告警和安全库存策略仍待正式迁移与权限接入。


```bash
# 一键演示（需先启动 uvicorn）
python scripts/demo.py

# 全量回归
python -m pytest -q
```

当前统计：第一周 9 + S1 15 + S2 14 + S3 15 + S4 5 + S5 21 + S6 7 + S7 稳定性 5 + 仓储协同 7 + 首页合同 1 + 连接器 4 + 账户与权限 2 = **114 passed**（以当前工作区 `python -m pytest -q` 为准）。

## S7 当前进度

### 已完成：服务稳定性第一批

- FastAPI lifespan 已接入后台 dispatcher / executor 的启动与优雅停止。
- queued、到期 retry_wait 和 lease 过期任务可由 dispatcher 扫描恢复。
- 采集、文档导入和文档索引统一使用后台任务提交入口。
- 新增 `/ready` 依赖就绪检查，并为请求补充 request ID 与通用内部错误兜底。
- 新增任务 lease 恢复、worker 生命周期和 readiness 回归测试。

### 已完成：仓储协同 MVP 后端与首页演示

- 新增 SKU、仓库、预计入库、实收反馈、差异计算、库存流水和库存余额。
- 支持自有仓与第三方仓的统一数据模型。
- 运营确认后才写入库存，确认使用稳定幂等键，重复确认不会重复增加库存。
- 收货请求校验重复 SKU、明细匹配和破损数量；收货/确认写入异常显式回滚。
- Alembic `0004_inbound_idempotency` 补齐收货与确认幂等字段，测试覆盖迁移结构。
- 首页 `/` 已增加商品列表、SKU 创建、仓库创建、预计入库、待确认入库列表和库存查询流程；运营不再直接模拟仓库收货。
- `/warehouse` 页面负责提交实收总数与破损数量；运营在 `/ops` 刷新“待运营确认的入库单”，加载库管反馈后执行“运营确认入账”。
- 首页请求处理包含空状态、错误提示、按钮状态控制、响应转义和确认幂等请求头。

### 后续规划

- 剩余稳定性增强：启动依赖检查、结构化日志、指标和更严格的停机观测。
- 内部权限：员工登录、角色、店铺/仓库数据范围和审批流的进一步完善；当前版本已提供 session 登录、基础角色、仓库授权、管理员账户页和角色权限页，员工认证默认开启，首次部署须先执行 `python -m app.cli init-admin ...`。API Key 仅为服务调用凭证。
- 仓储增强：收货幂等键完整契约、并发状态控制、出库、发货、退货、库存预占和对账。
- 外部信息感知：本地 JSON/CSV/模拟事件 → 国内平台首个只读连接器 → Amazon/FBA 只读扩展；统一快照、事件、重试和对账。
- 运营增效扩展：库存看板、差异告警、授权场景 RPA、飞书/钉钉通知与人工审批、运营分析 Agent/Dify 工作流。
- RAG 固定问答集评测：命中率、无答案率、引用覆盖率和响应耗时。
- 生产适配器：真实 Embedding、LLM 和外部向量库。
- 业务扩展：订单、多模态商品信息；写入型 Agent 动作必须经过显式审批。

## 文档

- [第一周架构图](docs/architecture.md)
- [第一周验收记录](docs/week1-acceptance.md)
- [第二周验收记录](docs/week2-acceptance.md)（S1 / S2 / S3 / S4 / S5 / S6 切片总结）
- [电商 AI 执行计划](电商ai.md)

## Playwright 浏览器安装（国内网络）

如果直接执行 `playwright install chromium` 下载缓慢或卡住，可以在 PowerShell 临时设置 Playwright 下载镜像：

```powershell
$env:PLAYWRIGHT_DOWNLOAD_HOST = "https://npmmirror.com/mirrors/playwright"
playwright install chromium
```

或：

```powershell
$env:PLAYWRIGHT_DOWNLOAD_HOST = "https://npmmirror.com/mirrors/playwright"
python -m playwright install chromium
```

安装后可以检查浏览器是否可启动：

```powershell
python -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); print('chromium ok'); b.close(); p.stop()"
```

镜像只用于下载 Playwright 浏览器运行时；采集器本身仍只访问项目内本地 fixture。