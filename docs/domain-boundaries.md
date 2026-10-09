# 领域边界与事实登记

- 日期：2026-10-05
- 状态：架构基线草案
- 目标：防止模块扩展时混淆外部事实、内部账本、AI 候选和人工决定

## 领域登记表

| 领域 | 当前入口/代码 | 权威数据 | 状态 | 关键风险 |
|---|---|---|---|---|
| Identity/RBAC | `app/employee_auth.py`、`app/account_admin.py`、`app/security.py` | `Workspace`、成员、session、仓库授权 | 已有底座 | API、页面、MCP 和后台任务必须统一 scope；凭证治理仍未生产化 |
| Catalog | `app/repository.py`、`app/crawler.py`、`Product`/`ProductSku` | 商品/SKU 主数据 | 已有底座 | PIM 属性、媒体、渠道字段和版本尚未完整 |
| Platform Facts | `app/connectors.py`、`app/external_orders.py`、`app/external_sync.py`、`app/platform_sync.py` | 外部订单、销量、库存观察、同步运行 | 已有/过渡 | 连接器、授权、来源批次、原始 payload 和生产观测需统一 |
| OMS | 规划领域；当前以 `ExternalOrder` 为输入 | `UnifiedOrder`、审单 proposal、履约计划/事实 | 未实现 | 外部渠道订单不能直接当内部履约订单 |
| Settlement | 规划领域；当前无结算事实表 | 账单、支付/退款/费用事实、对账差异 | 未实现 | 缺成本/费用/汇率时不能算利润 |
| After Sales | 规划领域；当前只有订单退款字段和库存底座 | 售后、退货、质检、库存处置 proposal | 未实现 | 退货不能未经质检进入可售库存 |
| PIM/Campaign | `app/content_production.py`、`app/visual_workflows.py`；营销事实尚无模型 | 属性、渠道映射、内容版本、活动事实 | 部分过渡/未实现 | AIGC 不等于完整 PIM；营销执行高风险且未实现 |
| SCM/SRM | `PurchaseRequest` 为采购入口 | 供应商、报价、交期、协同草稿 | 未实现 | 不自动供应商下单、付款或生产写入 |
| Inventory | `app/repository.py`、`app/db.py`、`app/alerts.py` | 入库状态、`InventoryBalance`、`InventoryTransaction` | 已有底座 | 采购到入库缺明确关系；盘点、损耗、退货后置 |
| Replenishment | `app/replenishment.py`、分析 API | 建议、决策、采购申请、评估 | 已有/过渡 | 评估字段和业务时间线仍需补证据；不做预测收益承诺 |
| Content | `app/content_production.py`、`app/visual_workflows.py`、`app/quality_gate.py` | Content revision、媒体资产、审核、导出 | 过渡实现 | 品类模板、Provider job、成本/耗时、人工审核和真实商品验收不足 |
| Knowledge | `app/importers/`、`app/rag/`、`app/rag_runtime.py` | 文档版本、切片、索引、引用 | Demo/过渡 | Hash 向量、JSON 存储、Stub LLM 不能当生产 RAG |
| Assistant/Tools | `app/assistant.py`、`app/agent/`、`app/mcp_server*.py`、`app/skills_ecommerce*.py` | 只读分析结果和审计 | Demo/过渡 | MCP 固定 workspace demo context；工具 scope 必须由可信上下文派生 |
| Analytics | `app/analytics.py`、`app/dashboard.py`、`docs/metrics.md` | 只读指标快照/响应 | 已有/过渡 | 元数据已开始统一；CSV、数据产品和指标版本还需治理 |
| Jobs | `app/background.py`、`app/jobs.py`、`CrawlJob` | 任务状态、租约、重试、取消 | 单进程过渡 | 无持久队列和独立 worker；容量、死信、运营重放需补齐 |
| Connectors/Providers | `app/platform_adapters/`、AIGC Provider、RAG Provider | 能力声明、request ID、来源和错误 | 过渡 | Provider 不应拥有权限/审核；secret、限流、成本和能力测试需统一 |

## 权威事实规则

### 外部平台事实

允许写入：

- 外部账户和 SKU 映射；
- `ExternalOrder`、`DailySkuSale`；
- `ExternalInventorySnapshot`；
- `ExternalSyncRun`、事件 Inbox、重试和来源信息。

禁止直接写入：

- `InventoryBalance`；
- `InventoryTransaction`；
- 入库确认状态；
- 平台订单、付款、退款、改价、库存写回。

### 内部库存账本

只允许通过库存领域服务和明确状态转移写入。仓库反馈只是 `received`，运营确认才可以进入 `confirmed` 并写流水；重复确认必须 no-op 或明确冲突。

### 结算与售后事实

- `ExternalOrder` 是订单来源事实，不是结算账单；Settlement 必须保存账单行、费用/支付/退款来源和匹配证据；
- 对账状态应区分 `matched/partial/mismatched/unknown`，无分母或缺金额证据不输出比例/利润；
- After Sales 的退货收货、质检和库存处置必须分阶段记录；`restock` 需要人工确认后才可影响内部库存；
- 退款事实可以被结算和销量聚合引用，但不能绕过退款/库存领域服务直接修改账本。

### OMS 与履约事实

- `ExternalOrder`/`ExternalOrderLine` 只代表渠道输入；`UnifiedOrder`、支付状态、履约状态、ShipmentFact 需要独立对象；
- 审单、分仓、拆合包裹先生成 proposal；未有审批、幂等和补偿契约前不执行平台写操作。

### PIM、营销与 SCM

- Product/SKU 主数据、Content Revision、渠道发布草稿和 AIGC 候选分离；
- Campaign 先保存活动计划/规则快照/销量归因事实，不先执行优惠券、秒杀或拼团；
- Supplier/报价/交期/采购协同草稿与内部库存、采购申请分离；供应商真实下单和付款后置。### AIGC 内容

Product/SKU 是输入事实，Content Revision 是派生版本；图片、文案、claims、QA 和导出 manifest 必须绑定同一 source snapshot。Provider 返回候选，不决定审核、发布或商品事实。

### RAG/Assistant/MCP

- 结构化库存、订单、金额、权限和状态不以向量检索为权威；
- 检索必须做 Workspace/文档权限过滤；
- 没有证据时拒答或返回 `unknown`；
- 只读工具不能调用写服务；
- 工具上下文中的 workspace、actor 和权限从服务端可信上下文派生，不接受模型覆盖。

## 新功能准入模板

```text
领域：
用户/业务问题：
权威事实：
派生事实：
状态机：
写入入口：
非目标：
Workspace/RBAC 边界：
幂等键/payload hash：
外部调用与重试：
审计/请求 ID：
指标与完整度：
失败、停止、补偿：
测试与回放：
迁移/回滚：
```

## 明确非目标

当前不因架构重梳理而自动增加：完整 ERP/WMS、自动下单/付款/退款、平台库存写回、真实多平台 OAuth、跨境税务物流、通用工作流画布、模型训练、真实营销收益承诺。
