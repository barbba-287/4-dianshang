# V6.2 业务域扩展文档

- 更新时间：2026-10-05
- 状态：长期范围评估，未代表已实现
- 关联计划：`电商ai.md` V6.2

## 1. 四条业务闭环

```text
运营与库存：外部事实 → 库存健康 → 补货 → 采购 → 入库
订单与履约：渠道订单 → 统一订单 → 审单/分仓 proposal → 履约事实
资金与售后：订单/支付/退款/平台账单 → 对账差异 → 退货/质检/库存处置
商品与增长：PIM → 内容分发 → 活动事实 → 经营分析 → 反哺补货
```

项目当前首先是运营决策中台；新增领域采用只读事实、差异识别和人工 proposal 逐步扩展，不一次性建设完整 ERP/OMS/财务/营销平台。

## 2. 领域优先级

1. OMS 统一订单只读 + 财务对账只读：最接近当前外部订单事实；
2. 售后/退款事实与逆向库存处置：连接订单、库存和财务；
3. PIM 深化 + AIGC：把内容生产纳入商品主数据和渠道草稿；
4. 经营分析 + 活动影响事实：补齐资金/营销证据后再算高级经营指标；
5. SCM/SRM 协同草稿：以采购申请为起点，真实供应商协同后置。

## 3. OMS：全渠道订单管理

### 权威事实与对象

`ExternalOrder/ExternalOrderLine` 是渠道来源事实，不是内部履约订单。目标对象：

```text
ChannelOrder
→ UnifiedOrder / UnifiedOrderLine
→ OrderReviewProposal
→ FulfillmentPlan
→ ShipmentFact / DeliveryFact
```

订单状态、支付状态、履约状态、退款状态必须分别建模；不能用一个 `order_status` 承载全部生命周期。

### 第一切片

- 回放多个渠道订单 fixture 或官方只读订单；
- 统一订单身份、订单行、SKU 映射和数据完整度；
- 订单异常收件箱；
- 确定性审单规则生成 proposal；
- 人工确认后生成分仓/拆包草稿；
- JSON 只读导出和审计。

### 不做

不自动发货、下快递单、改平台订单、付款、预占库存或执行拆合单。库存预占和 ATP 需要独立状态机、并发策略和补偿设计后再做。

## 4. 财务对账与结算

### 权威事实与对象

```text
SettlementStatement
→ SettlementLine
→ SettlementAllocation
→ ReconciliationCase
```

可关联 `ExternalOrder`、支付流水、退款事实、佣金、推广费、运费险和其他费用，但结算事实不能覆盖订单事实。

### 状态机

```text
imported → normalized → matching
                         ├→ matched
                         ├→ partial
                         ├→ mismatched
                         └→ unknown
```

### 第一切片

- JSON/CSV/mock 平台账单导入；
- 订单号、平台流水号、金额、币种、费用类型、状态和时间标准化；
- 确定性匹配与差异分类；
- 差异列表、证据引用和人工处理草稿；
- 只读 JSON 导出。

### 不做

不自动付款、自动调账、税务申报、利润承诺或真实平台账单下载。缺成本、费用、税率、汇率或流水证据时必须返回 `unknown`。

## 5. 售后与逆向物流

目标对象：

```text
AfterSaleCase
→ ReturnRequest
→ ReturnShipmentFact
→ ReturnInspection
→ InventoryDisposition
```

建议状态：

```text
requested → reviewing → approved/rejected
approved → awaiting_return → received → inspected
inspected → restock / repair / scrap / dispute
refund：proposed → approved → recorded
```

第一切片是退款/退货事实回放、售后案件、质检结果和库存处置 proposal。`restock` 只有在质检和人工确认后才能进入内部库存；不能收到退货就直接增加可售库存。不自动退款，不先接真实物流写接口。

## 6. PIM 深化与 AIGC

目标对象：

```text
Product/SKU
→ CategoryTemplate
→ Attribute/ChannelMapping
→ ContentRevision
→ ValidationReport
→ PublicationDraft
```

当前 `Product/ProductSku` 是目录底座，`ProductContentRevision` 是派生内容，不等于完整 PIM。AIGC 只能生成候选内容；属性、渠道必填、claims 证据、版本和人工审核优先。真实多平台发布需要官方授权、字段契约和停止开关。

## 7. 营销事实与促销执行

先做事实和分析输入：

```text
CampaignPlan
PromotionRuleSnapshot
CampaignCalendarFact
CampaignSalesAttributionFact
```

用途：标记活动销量窗口、活动成本、归因来源和补货参数 proposal。优惠券、满减、秒杀、拼团、赠品、积分等高并发/资金执行域后置，必须单独设计规则版本、核销幂等、限流、反作弊、回滚和渠道契约。

## 8. 经营分析

按角色提供只读视图：

- 老板：GMV、实收收入、退款率、贡献毛利和结算差异；
- 运营：平台/店铺/品类/SKU 下钻、销量、覆盖天数、活动影响和异常；
- 财务：账单匹配率、差异金额、退款、费用和未知项；
- 商品：内容完整度、审核通过率、渠道草稿和素材采用情况。

GMV、实收、贡献毛利、广告费用率、平台费用率和复购率必须分别定义来源、分子、分母、成本/费用/客户/归因证据；任何证据不足都返回 `unknown`，不能从当前订单金额和库存数量推断利润。

## 9. SCM/SRM 协同

目标对象：

```text
Supplier
SupplierSku
SupplierQuote
SupplierConfirmation
PurchaseOrderDraft
SupplierShipmentFact
```

先做供应商主数据、报价/最小起订量/交期快照、采购订单草稿和供应商确认草稿。真实供应商下单、付款、门户和生产协同后置；共享给供应商的数据必须经过权限和敏感度审查。

## 10. V6.2 阶段顺序

```text
P0 治理
→ P1 模块化单体/Application Service
→ P2 外部事实/Connector
→ P3 OMS 统一订单只读
→ P4 财务对账只读
→ P5 售后与逆向处置
→ P6 库存/补货/采购深化
→ P7 PIM + AIGC
→ P8 经营分析/营销影响
→ P9 SCM/SRM 草稿
→ P10 知识客服
→ P11 任务/通知/观测/部署
→ P12 OAuth/更多平台/受控写能力
```

阶段可以在稳定边界后并行做低风险调查，但不并行启动多个真实写入或高并发执行域。

## 11. 统一门禁

### Definition of Ready

业务 owner、对象、状态机、权威事实、非目标、权限/敏感级别、幂等/版本/事务、并发、重试、补偿、停止、回滚、真实/模拟范围、固定 fixture/replay 和证据位置均已明确。

### Definition of Done

迁移/API/DTO/领域服务/测试齐备；正负、越权、重复、并发、故障恢复和回放通过；审计、request ID、来源、完整度可查询；高影响动作有审批/停止/补偿；Runbook、回滚和限制已记录。

## 12. 第一批明确不做

- 自动付款、调账、退款和平台交易写回；
- 自动发货、快递下单和订单拆合执行；
- 真实优惠券核销、秒杀、拼团、积分资金系统；
- 税务申报、跨境结算、复杂物流和履约；
- 供应商真实下单、付款和生产系统写入；
- 没有成本、费用、汇率、客户或归因证据的利润结论。
