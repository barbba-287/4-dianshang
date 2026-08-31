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
| S7 安全硬化 | 上传签名/路径/SHA 校验、Agent 配额、审计摘要脱敏、可选 API Key |
| 仓储协同 MVP | SKU、自有/第三方仓库、预计入库、仓库实收、差异确认、库存流水与余额 |

未实现（明确剔除）：云 LLM / Embedding、Redis/Celery、外部向量库、完整 ERP/WMS、真实平台交易写入、自动下单/支付/退款/改价/取消、完整多租户/RBAC、真实平台反爬。

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

S6 将 Alembic 作为当前 schema 管理规范：`migrations/versions/0001_baseline.py` 定义 7 张业务表，`init_db.py` 负责识别数据库状态并执行对应动作；`Base.metadata.create_all` 仅保留给测试 fixture 或演示兜底，不作为生产迁移路径。

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
- `0001_baseline.py`：创建 `products`、`product_price_history`、`crawl_jobs`、`documents`、`document_versions`、`document_chunks`、`document_product_links`。

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
| GET | `/docs` | OpenAPI 文档 |

### 关键 API 速用

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
| GET | `/api/inventory` | 查询仓库-SKU 库存余额 |

当前只做手工/模拟闭环，不连接真实淘宝、抖音或京东 API。平台订单、发货、退货和第三方仓库回传后续通过统一事件协议接入，系统只感知状态并更新内部库存，不自动下单、退款、改价或取消订单。

## 演示与验收

```bash
# 一键演示（需先启动 uvicorn）
python scripts/demo.py

# 全量回归
python -m pytest -q
```

当前统计：第一周 9 + S1 15 + S2 14 + S3 15 + S4 5 + S5 21 + S6 7 + S7 稳定性 5 + 仓储协同 3 = **99 passed**（以当前工作区 `python -m pytest -q` 为准）。

## S7 当前进度

### 已完成：服务稳定性第一批

- FastAPI lifespan 已接入后台 dispatcher / executor 的启动与优雅停止。
- queued、到期 retry_wait 和 lease 过期任务可由 dispatcher 扫描恢复。
- 采集、文档导入和文档索引统一使用后台任务提交入口。
- 新增 `/ready` 依赖就绪检查，并为请求补充 request ID 与通用内部错误兜底。
- 新增任务 lease 恢复、worker 生命周期和 readiness 回归测试。

### 已完成：仓储协同 MVP 后端

- 新增 SKU、仓库、预计入库、实收反馈、差异计算、库存流水和库存余额。
- 支持自有仓与第三方仓的统一数据模型。
- 运营确认后才写入库存，确认使用稳定幂等键，重复确认不会重复增加库存。
- 首页已增加仓储协同演示区域。

### 后续规划

- 剩余稳定性增强：启动依赖检查、结构化日志、指标和更严格的停机观测。
- 内部权限：员工登录、角色、店铺/仓库数据范围和审批流；当前 API Key 仅为可选服务调用凭证。
- 仓储增强：收货幂等键完整契约、出库、发货、退货、库存预占和对账。
- 外部信息感知：本地 JSON/CSV/模拟事件 → 一个真实平台的只读轮询与 webhook 补偿。
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