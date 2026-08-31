# 电商 AI 项目执行计划

> 项目目录：`D:\ai\dianshang`
>
> 项目定位：面向中小型电商商家/品牌小团队的轻量电商运营工作台——商品资料、外部信息感知、入库协同、库存台账、客服知识库、RAG 客服问答，以及受控 Agent 工具。

## 0. 当前恢复点（2026-08-31）

- 当前分支：`feat/week2-rag-customer-service`。
- 工作区包含多个阶段的未提交实现，**不要执行 `git reset --hard`、`git clean -fd`、切换分支或强制覆盖**。
- 当前全量测试基线：`99 passed`；以实际命令结果为准，不以 pytest cache 为准。
- 已完成商品、异步任务、PDF/DOCX 文档、RAG、只读 Agent、Alembic 修复迁移、服务稳定性和部分上传安全硬化。
- 当前新增仓储后端已完成模型、迁移、同步 API 的第一步；前端仓储页面、订单/发货/退货模型和真实平台连接器尚未完成。

## 1. 产品定位与业务边界

### 1.1 产品定位

> **面向中小型电商商家和品牌小团队的轻量运营工作台，连接商品资料、外部平台信息感知、仓库入库协同、库存台账和智能客服知识库。**

不做完整 ERP/WMS。中小商家也可能拥有自有小仓库、供应商仓或第三方仓库，因此仓库是核心业务线；但当前聚焦轻量协同和可追溯台账，不实现复杂库位、波次和拣货优化。

### 1.2 角色和数据边界

- **商家/工作空间**：最大的业务边界；未来多个商家之间隔离。
- **用户**：实际操作者，同一商家内通常共享商品、库存和知识库，不按用户默认完全隔离。
- **角色**：管理员/店主、运营、库管/仓库协作方、客服、只读成员。
- **店铺/仓库范围**：控制用户可以操作的店铺或仓库。
- **审批状态**：仓库可以提交实际收货事实，运营/管理员确认后才正式入账。

### 1.3 外部平台边界

淘宝、抖音、京东等平台及第三方仓库是外部事实来源。本项目只做信息感知、状态同步、幂等处理、失败重试、异常发现、对账和内部库存投影；不做自动下单、支付、退款、改价、取消订单或平台高风险写回。当前先用手工/CSV/本地模拟连接器，后续再选一个平台做只读适配；“实时”统一表述为 webhook + 轮询补偿的准实时同步。

## 2. 已完成

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
- [x] S6：Alembic baseline、增量修复迁移、幂等升级和 legacy 库兼容初始化
- [x] S7 第一批：后台 worker 生命周期、lease 恢复、readiness、请求 ID和通用错误兜底
- [x] S7 第二批基础硬化：上传签名/路径/SHA 校验、Agent 限流和审计摘要脱敏
- [x] 仓储 MVP 后端第一步：SKU、仓库、预计入库、实收反馈、差异确认、库存流水与余额；当前全量回归为 `99 passed`，其中仓储 API 3 条测试。

## 3. 当前阶段：轻量仓储协同 MVP

### 3.1 核心流程

```text
运营创建预计入库
→ 仓库反馈实际 SKU/数量/破损
→ 系统计算差异
→ 运营确认
→ 同一事务写库存流水和余额
→ 运营/客服查询库存
```

支持自有小仓库和第三方仓库。仓库反馈事实，不能直接改变最终库存；库存确认必须经明确业务 API 和后续角色权限控制。

### 3.2 已完成后端第一步

- `product_skus`：现有商品与库存 SKU 的明确映射；不把 Product 外部商品 ID 偷换成 SKU。
- `warehouses`：自有/第三方类型，预留 `manual/csv/api` 集成模式。
- `inbound_orders`、`inbound_lines`：预计入库、仓库和数量。
- `receive`：提交实收和破损，状态 `expected → received`，返回差异；审核前库存不变。
- `confirm`：状态 `received → confirmed`，按 `received - damaged` 在一个事务中写 `inventory_transactions` 并更新 `inventory_balances`。
- 稳定幂等键防止重复确认导致库存膨胀。
- `GET /api/inventory` 查询仓库-SKU 当前库存。
- Alembic 增量迁移：`migrations/versions/0003_inventory_mvp.py`。

当前 API：

```text
POST/GET /api/skus
POST/GET /api/warehouses
POST /api/inbounds
GET /api/inbounds/{id}
POST /api/inbounds/{id}/receive
POST /api/inbounds/{id}/confirm
GET /api/inventory
```

### 3.3 下一小步

1. 增加专门仓储 API 测试：创建仓库/SKU、部分实收、破损差异、确认、重复确认和事务回滚。
2. 完善入库收货幂等键和操作人字段，明确一次收货提交的修改策略。
3. 增加轻量仓储前端：仓库选择 → SKU/预计数量 → 模拟实收 → 差异展示 → 运营确认 → 库存查询。
4. 增加内部角色权限：管理员/店主、运营、库管/仓库协作方、客服、只读成员；不按每个用户默认隔离。
5. 之后再加入出库、发货、退货和库存预占模型。

## 4. 外部事件与平台同步（后置）

当前不连接真实淘宝/抖音/京东 API。未来平台订单、发货、退货、补货及第三方仓库回传先进入统一事件 inbox，再转内部状态和库存流水：

- 优先支持本地 JSON/JSONL、CSV 和手工录入。
- 统一事件字段：来源/平台、外部单号、事件类型、发生时间、接收时间、payload hash、幂等键、处理状态。
- 相同 key/hash 重放 no-op；相同 key 不同 hash 进入 conflict/dead-letter。
- 订单创建/支付只影响预占，取消释放预占，发货确认扣实际库存；退货实收先进隔离区，质检后再进入良品/残次品；补货只有仓库反馈并经运营确认后才增加库存。
- Webhook 负责低延迟通知，定时轮询补偿遗漏，定时对账发现差异；系统对外称准实时，不承诺严格实时。
- 不自动下单、付款、退款、改价、取消订单，不向平台回写高风险交易动作。

建议阶段：

1. canonical event envelope、SKU 映射和状态转换矩阵
2. event inbox + 模拟 JSONL connector + 幂等 replay
3. 订单/发货只读投影
4. 退货/质检投影
5. 仓库补货与库存流水整合
6. 选一个真实平台做只读轮询和对账

## 5. 权限与租户原则

- `tenant/workspace` 代表商家/企业空间，不代表用户、部门或角色。
- 角色：管理员/店主、运营、库管/仓库协作方、客服、只读成员。
- 权限按“角色 + 店铺/仓库范围 + 审批状态”控制：运营可建单，仓库可反馈实收，运营/管理员确认入账，客服主要读取。
- API Key 仅是脚本或第三方仓库系统的服务调用凭证，不是员工登录方式。
- 真正多商家隔离需要后续给数据库、任务、文件、向量和 Agent 查询全链路增加 workspace/tenant；只在 header 或 ToolContext 加字段不算完成隔离。
- Agent 保持只读，入库确认、库存调整和平台写操作必须人工审批，不加入 Agent 白名单。

## 6. 验证与恢复

```bash
cd /d D:\\ai\\dianshang
python -m pytest --collect-only -q
python -m pytest -q
python init_db.py
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
```

实施任何新功能前先检查 `git status --short`，保留当前工作区和数据库；服务异常时先保存 traceback，再检查 `.env` 的 `DATABASE_URL`、MySQL、`alembic current` 和端口占用。不要执行 reset、clean、强制覆盖或未经确认的 commit。

## 7. 重要约束

- 不把本地 fixture 的数字表述为真实平台生产指标。
- 不把 Product 的商品级外部 ID 当成变体 SKU。
- 不直接覆盖库存余额或删除库存流水；库存变更必须可追溯、可重算。
- 不把仓库实收等同于运营确认入账。
- 不把平台“实时”状态和本系统投影混为一谈，保留来源、外部单号、同步时间、事件版本和对账状态。
- 不默认开启真实平台交易写入，不做自动下单/支付/退款/改价/取消。
- Schema 变更统一使用 Alembic；生产路径不依赖 `create_all` 重建数据库。
- 任何恢复操作优先保留工作区和数据库，先诊断再修复。
