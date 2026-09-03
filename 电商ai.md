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

淘宝/天猫、京东、拼多多、抖音电商以及 Amazon/FBA 和第三方仓库都是外部事实来源。本项目只做信息感知、状态同步、幂等处理、失败重试、异常发现、对账和内部库存投影；不做自动下单、支付、退款、改价、取消订单或平台高风险写回。平台接入统一通过独立 Adapter/Connector，先用手工/CSV/JSON/本地模拟数据验证，后续优先选择一个国内平台做只读适配，同时为 Amazon ASIN、seller SKU、marketplace、FBA 库存等字段保留扩展口；“实时”统一表述为 webhook + 轮询补偿的准实时同步。不同平台的认证、签名、限流、推送方式、数据模型和资质要求以官方文档为准，不在核心业务中写死。

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
- [x] 收货数量语义与仓储首页演示：实收总数（含破损）、合格入库和数量差异展示；独立仓库收货页面基础入口。
- [x] 外部平台连接底座第一步：离线 JSON/CSV/mock 连接器、统一库存字段、外部快照/事件 Inbox/对账模型基础和候选平台能力清单；当前不连接真实店铺或生产 API。专项测试已通过，当前全量回归为 `108 passed`。
- [x] 运营概览看板第一步：`/dashboard`、`/api/dashboard/summary`、内部库存/入库聚合与数据口径说明；动态低库存告警和安全库存策略留待后续迁移。

## 3. 当前阶段：外部信息感知底座（离线/mock）

当前先不连接真实店铺。通过统一 Adapter/Connector 读取国内平台候选（淘宝/天猫、京东、拼多多、抖音电商）和 Amazon/FBA 示例数据，验证外部库存观察、事件去重、快照留存和内部库存对账。真实 API、OAuth/签名、店铺授权和生产凭证列为后续工作。


## 3.1 内部仓储核心流程

```text
运营创建预计入库
→ 仓库反馈实际 SKU/数量/破损
→ 系统计算差异
→ 运营确认
→ 同一事务写库存流水和余额
→ 运营/客服查询库存
```

支持自有小仓库和第三方仓库。仓库反馈事实，不能直接改变最终库存；库存确认必须经明确业务 API 和后续角色权限控制。

- `external_inventory_snapshots`、`external_event_inbox`、`reconciliation_results` 已加入 ORM，离线导入服务与 API 已可用；迁移文件为 `migrations/versions/0005_external_sync.py`。外部观察值不直接修改内部库存流水或余额。


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

1. 正式落地员工登录、workspace、角色、仓库多对多授权和后端范围校验（此前计划，列为稍后执行）。
2. 完善外部快照/事件的正式连接账户、原始载荷留存、事件冲突原文、同步运行记录和更严格的对账分类。
3. 完善运营看板：接入已持久化对账结果、快照新鲜度、固定阈值安全库存策略和告警生命周期；暂不计算销量/GMV/周转/动态补货。
4. 增加授权场景的 Playwright RPA 巡检 demo：人工登录态复用、抓取报表、截图留证，验证码/MFA 时人工介入，不绕过平台限制。
5. 增加飞书/钉钉库存差异告警，后续再做验签、幂等和权限复核后的人工审批回调。
6. 增加运营分析 Agent/Dify 可选工作流，只输出建议，不直接写库存。
7. 有资质后选择一个国内平台做真实只读适配，最后接 Amazon SP-API/FBA 只读报告。
8. 之后再加入出库、发货、退货和库存预占模型。

## 4. 外部事件与平台同步（规划中）

本阶段不直接连接真实平台生产 API，先通过统一连接器和离线数据完成可复现验证。连接器至少覆盖 `manual/csv/json/mock`，未来再接国内平台或 Amazon 只读 API：

- 平台适配器独立实现认证、签名、限流、分页、重试、错误码和字段映射，核心业务不依赖任何单个平台协议。
- 统一事件字段：来源/平台、店铺/marketplace、外部单号、事件类型、发生时间、接收时间、payload hash、幂等键、处理状态、原始快照引用。
- 统一外部库存快照：平台可售/锁定/在途、仓库或 FBA 位置、seller SKU/ASIN（如有）、观测时间和同步状态；外部快照不直接伪装成本系统库存流水。
- 相同 key/hash 重放 no-op；相同 key 不同 hash 进入 conflict/dead-letter；乱序和漏事件通过版本检查、重试、轮询补偿与定时对账发现。
- 淘宝/天猫、京东、拼多多、抖音电商先作为国内平台连接器候选；Amazon/FBA 作为跨境扩展候选。平台具体接口、签名算法、计费、QPS、Webhook/MQTT/WebSocket 能力和资质门槛，实施时只依据对应官方文档确认。
- 订单创建/支付只影响预占，取消释放预占，发货确认扣实际库存；退货实收先进隔离区，质检后再进入良品/残次品；补货只有仓库反馈并经运营确认后才增加库存。
- Webhook 负责低延迟通知，定时轮询补偿遗漏，定时对账发现差异；系统对外称准实时，不承诺严格实时。
- 不自动下单、付款、退款、改价、取消订单，不向平台回写高风险交易动作。

建议阶段：

1. canonical event envelope、平台账户/商品映射、SKU 映射和状态转换矩阵。
2. external inventory snapshots + event inbox + JSON/CSV/mock connector + 幂等 replay。
3. 库存对账、低库存/同步失败/差异告警和 dashboard summary。
4. 国内平台选择一个真实只读适配；有明确授权后再接入平台凭证。
5. Playwright 授权页面/RPA 巡检 demo，保留人工登录态，验证码/MFA 优雅暂停并留证。
6. 飞书/钉钉出站告警；后续做验签、事件幂等、角色/仓库权限复核后的人工审批回调。
7. 运营分析 Agent 或 Dify 可选工作流：读取对账、库存、客服数据并输出建议，不直接确认入库或写库存。
8. 最后接 Amazon SP-API/FBA 只读报告适配，扩展 ASIN、seller SKU、marketplace、available/reserved/inbound。

## 5. 权限与租户原则

- `tenant/workspace` 代表商家/企业空间，不代表用户、部门或角色。
- 角色：管理员/店主、运营、库管/仓库协作方、客服、只读成员。
- 权限按“角色 + 店铺/仓库范围 + 审批状态”控制：运营可建单，仓库可反馈实收，运营/管理员确认入账，客服主要读取。
- API Key 仅是脚本或第三方仓库系统的服务调用凭证，不是员工登录方式。
- 真正多商家隔离需要后续给数据库、任务、文件、向量和 Agent 查询全链路增加 workspace/tenant；只在 header 或 ToolContext 加字段不算完成隔离。
- `products`、`product_skus`、`warehouses`、`inbound_orders`、`inventory_balances` 等业务对象当前尚未完成 workspace 全链路隔离，后续权限迁移必须覆盖数据库、后台任务、文件、向量和 Agent 查询。
- 外部平台连接器不直接修改平台订单或交易状态；外部库存与订单事件先进入独立快照/inbox，经过幂等、对账和必要人工确认后才影响内部台账。
- RPA、IM 和平台 API 的业务收益指标（节省时长、覆盖率、发现延迟等）在真实埋点前只作为目标，不作为生产实测数据。

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
