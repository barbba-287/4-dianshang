# 第二周验收记录

## S1 任务工程化（ThreadPoolExecutor + SQL claim）

### 验收范围

验证：

- 任务异步入队与状态机：`queued → running → succeeded / retry_wait / failed / cancelled`。
- 真正的并发执行（多个任务在 `ThreadPoolExecutor` 中同时运行）。
- SQL claim：原子抢占 + lease + heartbeat 续约 + cancel 标志。
- 取消与重试：失败按指数退避（`min(2**(attempt-1), 60)s + ±10% jitter`）回到 `retry_wait`，达到 `max_retries` 转为 `failed`。
- API：`POST /api/crawl`、`GET /api/crawl/{id}`、`POST /api/crawl/{id}/cancel` 立即返回，不阻塞 FastAPI 请求线程。

### 自动化测试

执行：

```bash
python -m pytest -q
```

S1 切片结果：

```text
24 passed
```

覆盖内容：

- 健康检查、采集 API、幂等写入、价格历史（第一周 9 条无回归）。
- Repository 状态机：`enqueue_job` / `claim_job` / `renew_lease` / `finish_job` / `retry_job` / `request_cancel` / `compute_backoff_seconds`。
- 抢占失败：已 running / 已 cancel 的任务不可被再次抢占。
- API：POST `/api/crawl` 立即返回 queued → 异步执行 succeeded；POST `/api/crawl/{id}/cancel` 写入取消标志；GET 返回 404 不存在的任务。
- 端到端：真实 fixture 采集经后台执行后商品落库。

### 端到端命令

```bash
python init_db.py
python -m uvicorn app.main:app --reload
```

```bash
# 入队一个采集任务，立刻返回 job_id
curl -X POST http://127.0.0.1:8000/api/crawl

# 轮询任务状态
curl http://127.0.0.1:8000/api/crawl/{job_id}

# 取消任务
curl -X POST http://127.0.0.1:8000/api/crawl/{job_id}/cancel

# 最近任务列表
curl http://127.0.0.1:8000/api/crawl/jobs
```

---

## S2 文档导入（PDF + DOCX）

### 验收范围

验证：

- 上传 PDF / DOCX，按 sha256 复用 / 新建 document + version。
- 后台 executor 异步解析，按页 / 段落产出 chunks（含页码 / 段落号）。
- 状态机：`parsing → ready / failed`。
- 文档版本列表 API。
- 启发式商品关联（chunk 文本命中商品 external_product_id / URL / 标题）。
- 边界：超限 413、不支持类型 415、不存在文档 404。

### 自动化测试

执行：

```bash
python -m pytest -q
```

S2 切片完成后：

```text
38 passed
```

新增 14 条：

- Parser：`detect_source_type` 按扩展名 / MIME 推断；`PdfParser` 多页提取、加密 / 空文件抛错；`DocxParser` 段落 + 标题 + 表格抽取；`get_parser` 工厂。
- API：上传 PDF 立即 202 返回 `{document_id, version_id, job_id, version_no, status}`；轮询状态变 `ready` 且 chunk_count ≥ 1；重复上传相同 sha 复用 document + version；不同 sha 创建新 document；DOCX 解析正确；不支持类型 415；超限 413；不存在文档 404。

### 端到端命令

```bash
# 上传 PDF
curl -X POST http://127.0.0.1:8000/api/documents \
  -F "file=@fixtures/sample.pdf" \
  -F "title=茶叶规格"

# 查看版本列表（含 chunk_count）
curl http://127.0.0.1:8000/api/documents/{document_id}/versions

# 上传 DOCX
curl -X POST http://127.0.0.1:8000/api/documents \
  -F "file=@fixtures/rules.docx" \
  -F "title=售后规则"
```

### 设计说明

- `documents` 与 `document_versions` 通过 sha256 唯一约束保证同一字节只入库一次。
- 同一 sha 的重复上传会复用 `document + version` 并直接返回成功，不再入队任务。
- chunks 按 `document_version_id + content_hash` 唯一约束，保证幂等。
- `uploads/` 不进入仓库（已加入 `.gitignore`）。
- 文件大小由 `UPLOAD_MAX_BYTES` 控制（默认 20MB）。

### 结论

S2 文档导入完成。S3 RAG 检索与 S4 演示脚本继续按 `noble-riding-squirrel.md` 推进。

---

## S3 RAG 客服问答（本地 Embedder + 内存向量库）

### 验收范围

验证：

- 文档导入完成后自动入队 `document_index` 任务，把 chunks embed 到向量库。
- 本地假 Embedder（`HashEmbedder`，确定性 + 归一化向量）+ 内存向量库（`InMemoryVectorStore`，JSON 持久化）。
- top_k + 分数阈值检索；命中为空 / 最高分低于阈值时返回 `{no_answer:true, reason:'empty'|'low_score'}`。
- 引用结构：`{chunk_id, document_id, document_version_id, snippet, score, locator}`。
- `/api/rag/query` 与 `/api/settings` 端点。

### 自动化测试

执行：

```bash
python -m pytest -q
```

S3 切片完成后：

```text
53 passed
```

新增 15 条（10 单测 + 5 API 测试）：

- Embedder：确定性 / 归一化 / 维度 / 空文本 / 相似方向。
- 向量库：top_k / 阈值 / payload 过滤 / 删除版本 / JSON 持久化。
- Retriever：Hit 转换 / payload 转 locator。
- Answerer：empty 兜底 / low_score 兜底 / StubLLM 答案拼装。
- API：上传 + 索引 + 查询命中；无证据拒答；DOCX 路径；参数校验；`/api/settings`。

### 端到端命令

```bash
# 启动服务
python -m uvicorn app.main:app --reload

# 上传文档（自动索引）
curl -X POST http://127.0.0.1:8000/api/documents \
  -F "file=@fixtures/sample.pdf" -F "title=Tea"

# 查询（命中商品规格）
curl -X POST http://127.0.0.1:8000/api/rag/query \
  -H "Content-Type: application/json" \
  -d '{"query": "green tea"}'

# 查看运行时配置与规模
curl http://127.0.0.1:8000/api/settings
```

### 设计说明

- `HashEmbedder`：`SHA-384` 分桶 + 归一化，维度 256，**确定性 / 可重放 / 零依赖**。生产请替换 BGE-M3 或云端 embedding。
- `InMemoryVectorStore`：启动时从 `data/vectors.json` 加载；写入追加保存；过滤字段 `chunk_id / document_id / document_version_id / snippet / locator / version_no`。
- `RagAnswerer`：先 `RagRetriever.retrieve` 取 hits，再判定 `empty / low_score` 兜底；命中后调用 `LLMProvider.answer`。当前 `StubLLMProvider` 仅做 snippet 拼接，生产可替换真实 LLM。
- 单例 `app/rag_runtime.py`：用 `RLock` 避免 `get_answerer → get_retriever → get_embedder` 嵌套调用死锁。

### 结论

S3 RAG 完成。下一步 S4 CLI 与设置 API。

---

## S4 CLI + 设置端点

### 验收范围

- `python -m app.cli` 提供 `init-db / seed-docs / enqueue-crawl / query / status` 子命令。
- `GET /api/settings` 展示运行时配置 + 数据库 / 向量库规模。
- `scripts/demo.py` 一键演示（需配合 `uvicorn` 启动）。

### 自动化测试

```bash
python -m pytest -q
```

最终结果：

```text
58 passed
```

CLI 子测试 5 条：

- `init-db` 创建表；
- `status` 输出关键字段；
- `seed-docs` 注入示例 PDF + DOCX 并写入向量库；
- `query` 命中返回 answer / 无证据返回 `[no_answer]`；
- `query` 无参数 / 未知命令返回 2 并打印 usage。

### 端到端命令

```bash
# CLI
python -m app.cli init-db
python -m app.cli seed-docs
python -m app.cli enqueue-crawl
python -m app.cli query "退换货规则"
python -m app.cli status

# 设置端点
curl http://127.0.0.1:8000/api/settings

# 一键演示（需先启动 uvicorn）
python scripts/demo.py
```

### 设计说明

- CLI 用 `subprocess.run(env=..., encoding="utf-8")` 隔离临时数据库，便于多场景脚本化演示。
- `/api/settings` 暴露 `worker_concurrency / embedder_dim / rag_top_k` 等关键参数，便于面试展示“运行时配置 + 规模”而不是只看代码。
- `scripts/demo.py` 仅用于冒烟；真实演示请直接调用 API 与 CLI。

### 结论

第二周全部切片完成：S1 任务工程化 / S2 文档导入 / S3 RAG / S4 CLI + 设置。`pytest -q` 共 58 条全绿，第一周 9 条无回归。后续可继续向 S5（受控 Agent 工具 / 自动下单 / 多模态商品）扩展。

---

## S5 受控 Agent 工具

### 验收范围

- 仅暴露 3 个只读工具：`search_products` / `get_product_detail` / `get_price_history`。
- 任何写入型工具被 `AgentToolRegistry.call` 显式拒绝（`is_readonly=False`）。
- 参数 JSON Schema 服务端校验：必填字段、类型、未知字段。
- 调用次数限流（60 次 / 分钟 / 工具 / tenant），超时由配置驱动。
- 审计日志：每次调用写入 tool / ok / duration_ms / 输入与输出摘要。

### 自动化测试

```bash
python -m pytest -q
```

最终结果：

```text
79 passed
```

S5 切片新增 21 条（14 单测 + 7 API 测试）：

- 协议：`validate_input`（必填 / 类型 / 未知 / payload 类型）；重复注册；未知工具；非只读拒绝；限流。
- 真实工具：seed 商品后 `search_products` 命中、`get_product_detail` 错误返回、`get_price_history` 返回列表。
- 编排器：审计写入 JSONL、未知工具返回错误、`invoke_many` 顺序执行。
- API：`GET /api/agent/tools` 列出 3 个只读工具；`POST /api/agent/invoke` 4 种调用 + 3 种负向（未知工具 / 类型错 / 缺必填）。

### 端到端命令

```bash
# 列出可用工具
curl http://127.0.0.1:8000/api/agent/tools

# 调用 search_products
curl -X POST http://127.0.0.1:8000/api/agent/invoke \
  -H "Content-Type: application/json" \
  -d '{"tool": "search_products", "input": {"category": "茶饮"}}'

# 调用 get_product_detail
curl -X POST http://127.0.0.1:8000/api/agent/invoke \
  -H "Content-Type: application/json" \
  -d '{"tool": "get_product_detail", "input": {"product_id": 1}}'

# 负向：未知工具
curl -X POST http://127.0.0.1:8000/api/agent/invoke \
  -H "Content-Type: application/json" \
  -d '{"tool": "delete_inventory", "input": {}}'
# -> {"ok": false, "error_code": "TOOL_NOT_FOUND"}
```

### 设计说明

- `AgentToolRegistry.call` 在调用 handler 前做四件事：注册校验、is_readonly 拒绝、限流计数、参数校验；失败全部转化为 `ToolCallResult` 而不是异常。
- `AgentOrchestrator.invoke` 在每次调用前后写 `AgentAuditRecord`，可在 `data/agent_audit.jsonl` 中复盘。
- 工具的 `handler` 走独立 `SessionLocal`，不复用请求级 session，避免跨调用污染。
- `LLMProvider` 仍是 Protocol 占位；S5 未集成真实 LLM，仅暴露“受控调用”骨架。生产可对接 Claude function calling。

### 结论

S5 完成。第二周合计 `79 passed`：第一周 9 + S1 任务工程化 15 + S2 文档导入 14 + S3 RAG 15 + S4 CLI 5 + S5 受控 Agent 21。下一步可继续向 S6（限流 / 鉴权 / 评测报告 / 多模态商品 / 商品关联到订单）扩展。

---

## S6 Alembic 数据库迁移

### 验收范围

- `alembic init migrations` 完成；`alembic.ini` + `migrations/env.py` + `migrations/versions/0001_baseline.py`。
- `env.py` 从 `app.config.get_settings().database_url` 注入 URL，导入全部 model；`compare_type=True` 捕获类型变更。
- `init_db.py` 调用 `command.upgrade(cfg, "head")` 升级 schema。
- `cmd_init_db` 走相同路径；`cmd_seed_docs` 自动检测并补齐 schema。
- legacy MySQL：`alembic stamp head` 标记 baseline 不执行 DDL。

### 自动化测试

```bash
python -m pytest -q
```

S6 切片完成后：

```text
84 passed
```

新增 5 条：

- `alembic upgrade head` 创建全部 7 张表 + `alembic_version`；
- `alembic current` 显示 `0001_baseline (head)`；
- `alembic upgrade head` 幂等（重复执行无错误）；
- `alembic stamp head` 在 legacy 库上只标记版本不执行 DDL；
- `python init_db.py` 通过 alembic 创建表。

### 端到端命令

```bash
# 新 SQLite
python init_db.py

# 已有 MySQL（week1 schema）
alembic stamp head

# 改 model 后生成新迁移
alembic revision --autogenerate -m "add orders table"
alembic upgrade head

# 查看版本
alembic current
alembic history --verbose
```

### 设计说明

- `Base.metadata.create_all` 仅在测试 fixture 与 `cmd_seed_docs` 兜底分支使用；生产 schema 由 Alembic 唯一管理。
- `0001_baseline.py` 手写全部 7 张表（`products` / `product_price_history` / `crawl_jobs` / `documents` / `document_versions` / `document_chunks` / `document_product_links`）；autogenerate 适用于 baseline 之后的增量。
- SQLite 用 `render_as_batch=True` 支持列 drop / 改类型；MySQL 用原生 DDL。

### 结论

S6 完成。累计 `84 passed`：第一周 9 + S1 15 + S2 14 + S3 15 + S4 5 + S5 21 + S6 5。后续所有 schema 改动走 Alembic 流程，不再依赖 `create_all` 重建。