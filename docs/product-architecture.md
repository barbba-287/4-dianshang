# 产品架构基线

- 版本：v1.0
- 日期：2026-10-05
- 状态：基线草案，随领域重构和真实运行证据复核
- 适用项目：`D:\ai\dianshang`

## 1. 产品目标

项目长期目标是一个面向多平台商家的智能运营与商品内容工作台，不以短期演示或面试页面数量为完成标准。

核心主线：

```text
外部平台事实
  → 标准化、幂等、来源追踪、数据质量
  → 库存健康和异常
  → 确定性补货建议
  → 人工决策和采购申请
  → 仓库实收
  → 运营确认入账
  → 评估与复盘
```

```text
商品/SKU事实
  → 品类工作流和基础图
  → 图片/文案候选
  → 质量与 claims 证据
  → 人工审核
  → 版本化素材包导出
```

客服知识库独立于实时运营事实：

```text
文档版本 → 解析/切片/Embedding → 权限过滤检索 → 引用回答或拒答 → 评测/反馈
```

## 2. 架构原则

1. 外部观察事实、内部库存账本、内容资产、知识文档和分析指标分开建模。
2. Workspace/RBAC/仓库范围由服务端和领域服务强制执行，不能由前端、模型或 MCP 参数决定。
3. 规则负责权限、状态、金额、库存、幂等、质量门禁和停止条件；AI 负责候选、解释、抽取和辅助排序。
4. 高影响动作默认 proposal-only，必须人工审批、二次确认、审计、幂等、停止和补偿。
5. 外部调用必须区分 mock/replay/simulated/live，凭证只通过 `secret_ref` 或受控 Provider 取得。
6. 相同 payload 重放为 no-op；同幂等键不同 payload 为 conflict；partial、unknown、insufficient 不伪装成成功。
7. 先做模块化单体和可替换 Port，不以拆微服务作为默认目标。
8. 所有事实和指标必须带来源、时间、范围、版本、完整度和证据；不能用目标值替代运行结果。

## 3. 目标分层

```text
接口层
  REST / HTML 页面 / MCP / CLI / 后台入口
        ↓
应用编排层
  Command / Query / Job / 权限校验 / request context
        ↓
领域服务层
  identity / catalog / platform_facts / oms / settlement / after_sales
  pim / campaign / scm / inventory / replenishment
  content / knowledge / assistant / analytics / jobs / connectors
        ↓
事实与账本层
  SQLAlchemy Repository / 事务 / 审计 / Outbox / 领域事件
        ↓
基础设施层
  SQLite/MySQL / 对象存储 / 队列 / Platform Adapter
  Embedding/LLM Provider / 通知 Sink / 观测系统
```

当前实现仍是单体，`app/main.py`、`app/db.py`、`app/schemas.py` 承担过多职责；上述分层是演进目标，不代表当前所有层已独立实现。

## 4. 领域边界与权威事实

| 领域 | 权威事实 | 可写入口 | 明确不拥有 |
|---|---|---|---|
| identity | Workspace、成员、角色、仓库访问、session | identity service/admin API | 商品、库存、平台 token 的业务决策 |
| catalog | Product、ProductSku、主数据、媒体引用和外部映射 | catalog service/人工维护 | 外部平台原始事实、内部库存账本 |
| platform_facts | 外部订单、销量、外部库存观察、同步运行和来源批次 | connector/sync service | `InventoryBalance`、`InventoryTransaction`、平台写接口 |
| oms | 渠道订单、统一订单、审单 proposal、履约计划和履约事实 | OMS service/人工确认 | 支付、平台写操作和内部库存账本 |
| logistics | ShipmentFact、TrackingEvent、DeliveryException、履约 SLA | 物流导入/授权 Provider/人工处理 | 自动打单、平台写回和未授权高频查询 |
| settlement | 平台账单、支付/退款/费用事实、匹配和差异案例 | settlement import/reconciliation service | 付款、自动调账和税务申报 |
| after_sales | 售后案件、退货、质检、退款事实和库存处置 proposal | after-sales service/人工确认 | 自动退款、未经质检的可售入库 |
| pim | 商品属性、类目模板、渠道映射、内容 revision、校验和发布草稿 | PIM/content service | Product/SKU 主数据绕过审核和平台写操作 |
| campaign | 活动计划、规则快照、活动归因和补货影响 | campaign fact service | 优惠券/秒杀/拼团高并发执行 |
| scm | 供应商、报价、交期、采购协同草稿和履约事实 | SCM service/人工确认 | 供应商真实下单、付款和生产写入 |
| inventory | 入库状态、内部余额、库存流水、策略和告警 | inventory service/人工确认 | 外部平台可售量、采购支付 |
| replenishment | 公式版本、建议、人工决策、采购申请、评估 | replenishment service | 自动采购、付款、库存写回 |
| content | source snapshot、内容 revision、媒体资产、QA、审核、导出 | content service/job/review API | Product 主数据真相、平台发布写操作 |
| knowledge | 文档版本、解析产物、切片、向量索引、引用和评测 | knowledge service/job | 实时库存/订单事实、权限决定 |
| tickets | ServiceTicket、分配、事件、关联和 SLA | ticket service/人工处理 | 自动承诺、自动关闭和平台交易写操作 |
| assistant | 只读查询、Skill、工具契约、模型解释 | query API/MCP/Agent | 自行选 workspace、直接写业务表 |
| analytics | 只读指标、完整度、导出契约 | query service | 修改业务事实和账本 |
| jobs | 任务状态、租约、重试、取消、死信、lineage | Job Port/worker | 领域业务状态本身 |
| connectors | 外部平台/provider 协议、能力、认证引用和 transport | adapter registry | Workspace 权限、审核语义、库存账本 |

## 5. 当前实现分级

### 已有底座（仍需长期验收）

- Workspace/RBAC、员工 session、仓库范围；
- Product/SKU、仓库、入库、内部余额和流水；
- 外部订单、每日销量、外部库存观察、离线同步、partial/补偿；
- 确定性补货建议、采购草稿和人工入库确认；
- Dashboard、统一指标元数据和只读助手；
- Alembic 迁移、单进程后台任务、幂等/租约测试。

### 过渡实现

- `app/main.py` 巨型入口和直接拼装 response；
- `app/db.py` 单文件承载多个领域模型；
- `ThreadPoolExecutor` 作为后台任务运行时；
- HashEmbedder、JSON InMemoryVectorStore、Stub LLM；
- mock/local compositor 与单 Provider AIGC 流程；
- MCP 官方入口固定 demo workspace 上下文；
- Shopify 单工作区 `.env` Dev Store Canary；
- 部分功能仍存在未跟踪文件，不代表 Git 发布能力。

### 未知或外部依赖

- MySQL 和 offline SQL 的完整运行验证；
- 多租户 OAuth、secret vault、真实国内平台生产授权；
- 真实 Embedding/LLM、生产向量基础设施和客服渠道；
- 真实 Provider 质量、营销效果、补货收益和业务采纳率；
- 生产容量、SLO、备份恢复和跨区域灾备。

## 6. 数据与副作用边界

外部同步只创建或更新外部事实和同步运行记录：

```text
ExternalOrder / DailySkuSale / ExternalInventorySnapshot
                                  ✕
                    InventoryBalance / InventoryTransaction
```

OMS 的统一订单和履约对象不能覆盖渠道来源事实；物流事实允许来自 API、报表、ERP、manual 或 replay，并必须标记来源和延迟。结算账单行、费用、支付/退款事实不能覆盖订单事实；缺金额、成本、费用或汇率证据时不计算利润。

内部库存只能在明确的入库确认、质检后的退货处置或未来经过审批的账本动作中改变。采购申请不是采购完成，建议确认不是入库完成，退货收到不是可售入库。

AIGC 流程必须冻结 `workspace/product/sku/source_snapshot_hash/workflow_key/workflow_version`；图片和文案绑定同一 revision，审核和导出不修改商品主数据。没有平台 API 时，PIM 产出人工发布草稿，不宣称自动发布。

知识库检索必须带 workspace、文档权限和版本过滤；实时库存、订单、金额、权限和状态优先走结构化查询。工单属于内部协作事实，AI 不自动关闭或替客服承诺。

## 7. 长期演进阶段

1. **基线与治理**：版本清单、架构文档、依赖图、事实/目标/未知、发布和回滚门禁；
2. **模块化边界**：抽 Router、Application Service、Repository、DTO、统一错误和 request context，不改变业务语义；
3. **事实层生产化**：平台账户/授权/来源批次/connector/replay/数据质量/凭证引用；
4. **OMS 与物流**：统一订单、物流轨迹、异常告警和履约 proposal，支持 API/报表/ERP/manual/replay；
5. **结算与售后**：账单匹配、差异、退货质检和库存处置 proposal；
6. **库存闭环深化**：采购到入库明确关联、账本动作、盘点/损耗/退货、通知 outbox、评估；
7. **PIM/AIGC**：属性、渠道草稿/人工导出、品类工作流、Provider job、QA、人工审核和素材包；
8. **经营分析与营销事实**：活动归因、价格快照、导出和有证据的经营指标；
9. **SCM/SRM 与客服工单**：供应商协同草稿、退货索赔草稿、ServiceTicket 和人工接管；
10. **知识与客服生产化**：真实 Embedding/LLM、权限 RAG、评测、会话和人工接管；
11. **任务与运行平台**：持久化队列、观测、容量、备份、恢复、部署和密钥治理；
12. **受控开放**：多租户 OAuth、更多平台、Webhook/自动发布及任何写能力均单独审批和验收。

## 8. 架构变更门禁

任何新增领域或基础设施必须先回答：

- 权威事实和非目标是什么？
- 状态机、合法转移和版本策略是什么？
- Workspace/RBAC/仓库范围在哪里强制？
- 幂等键、payload hash、事务和并发冲突如何处理？
- 外部调用超时、重试、partial、补偿和停止怎么做？
- AI 输出如何经 Schema、规则和人工门禁？
- 指标分子、分母、来源、完整度和时间口径是什么？
- 如何回放、迁移、观测、回滚和移交？

任何一项无法回答时，先做调查或实验，不扩大生产范围。
