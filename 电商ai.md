# 电商 AI 项目执行计划

> 项目目录：`D:\\ai\\dianshang`
>
> 项目定位：电商商品与客服工作台——商品采集、标准化入库、文档知识库、RAG 客服问答，以及受控 Agent 工具。

## 0. 当前恢复点（2026-08-30）

- 服务已恢复：`http://127.0.0.1:8000/health` 返回 `{"status":"ok"}`。
- 当前分支：`feat/week2-rag-customer-service`。
- 第二周代码仍在工作区，**不要执行 `git reset --hard`、`git clean -fd` 或切换分支**，否则会丢失尚未提交的实现。
- 全量测试：`86 passed`。
- 当前运行配置使用 MySQL；数据库中已有商品、文档、切片和向量数据。
- 本文件此前不在项目目录中，现按当前代码和验收记录重新建立，作为后续唯一执行计划。

## 1. 已完成

### 第一周：商品工作台

- [x] 本地 fixture 商品采集
- [x] 字段校验、标准化和幂等 Upsert
- [x] 商品查询、详情和价格历史 API
- [x] 极简网页展示
- [x] SQLite / MySQL 配置切换

### 第二周：客服知识库与工程化

- [x] S1：ThreadPoolExecutor 异步任务、SQL claim、lease、heartbeat、取消和指数退避重试
- [x] S2：PDF / DOCX 上传、SHA-256 文档版本化、后台解析、chunk 和商品关联
- [x] S3：本地确定性 Embedder、JSON 持久化向量库、top-k / 阈值检索、引用和无答案兜底
- [x] S4：CLI（`init-db`、`seed-docs`、`enqueue-crawl`、`query`、`status`）和 `/api/settings`
- [x] S5：只读 Agent 工具白名单、Schema 校验、限流和审计日志
- [x] S6：Alembic baseline、升级、幂等升级和 legacy 库 stamp
- [x] 全量回归测试通过（当前 86 条）

## 2. 崩溃后的标准恢复步骤

每次服务异常退出后，按以下顺序恢复，不清理工作区：

```bash
cd /d D:\\ai\\dianshang
python -m pytest -q
python init_db.py
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

启动后检查：

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api/settings
```

如果服务再次退出，先保留终端最后一段 traceback，再检查：

1. `.env` 中的 `DATABASE_URL` 是否可连接；
2. MySQL 服务是否正在运行；
3. `alembic current` 是否显示 `0001_baseline (head)`；
4. 是否有端口 `8000` 被其他进程占用；
5. `python -m pytest -q` 是否仍然全绿。

## 3. 当前代码保存策略

当前第二周实现包含大量未提交文件和修改。下一步先做以下安全动作：

- [ ] 复核 `git status --short`
- [ ] 运行 `python -m pytest -q` 并保存结果
- [ ] 运行 API 健康检查和关键接口冒烟
- [ ] 经确认后再创建检查点提交；未经确认不执行 reset、clean 或强制覆盖

## 4. 下一阶段（S7，第二周恢复后的继续工作）

按以下顺序推进，每一步都先补测试再改实现：

1. **服务稳定性**：统一异常处理、启动依赖检查、结构化日志、优雅停机和后台 worker 生命周期。
2. **安全边界**：API 鉴权、tenant 隔离、Agent 调用配额，以及上传文件内容安全校验。
3. **RAG 评测**：建立固定问答集，记录命中率、无答案率、引用覆盖率和响应耗时。
4. **生产替换接口**：保留当前零依赖实现作为测试替身，预留真实 Embedding / LLM / 外部向量库适配器。
5. **业务扩展**：商品与订单关联、多模态商品信息；写入型动作必须经过显式审批，不直接开放给 Agent。

## 5. 常用命令

```bash
cd /d D:\\ai\\dianshang
python -m pytest -q
python -m app.cli status
python -m app.cli query "退换货规则"
alembic current
```

## 6. 重要约束

- 不把本地 fixture 的测试数字表述为真实平台生产指标。
- 不默认开启真实下单、支付、改价、删除等写入动作。
- Schema 变更统一使用 Alembic；生产路径不依赖 `create_all` 重建数据库。
- 任何恢复操作优先保留工作区和数据库，先诊断再修复。
