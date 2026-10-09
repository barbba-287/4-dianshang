# ADR 索引

本目录用于记录影响领域边界、数据来源、Provider、部署、安全和回滚的架构决定。每个 ADR 必须包含：背景事实、目标/非目标、候选方案、决策、取舍、验证、回滚、复核日期和来源。

## 当前登记

| 编号 | 标题 | 状态 | 范围 |
|---|---|---|---|
| ADR-0001 | 模块化单体优先，不立即拆微服务 | accepted | 总体架构 |
| ADR-0002 | 外部库存观察与内部库存账本分离 | accepted | platform_facts/inventory |
| ADR-0003 | 高影响动作 proposal-only | accepted | replenishment/inventory/platform |
| ADR-0004 | AIGC 采用 Provider + 自有业务控制平面 | accepted | content |
| ADR-0005 | RAG 结构化事实优先、证据不足拒答 | accepted | knowledge/assistant |
| ADR-0006 | P0-1 指标统一元数据和 unknown 语义 | accepted | analytics/replenishment |
| ADR-0007 | 当前工作区发布候选必须经过依赖闭包审查 | accepted | release governance |
| ADR-0008 | 渠道订单事实与内部 OMS 订单分离 | proposed | oms |
| ADR-0009 | 结算事实与订单事实分离 | proposed | settlement |
| ADR-0010 | 退货必须经过质检和处置确认后才能影响可售库存 | proposed | after_sales/inventory |
| ADR-0011 | 活动事实先于营销执行引擎 | proposed | campaign/analytics |

## ADR-0001 摘要：模块化单体优先

在真实容量、团队边界和隔离需求未证明前，不拆微服务。通过领域服务、Repository、Job Port、Provider Port 和 contract tests 保留未来替换空间。

## ADR-0002 摘要：外部事实与内部账本分离

平台可售/锁定/在途是观察事实，不直接覆盖 `InventoryBalance` 或 `InventoryTransaction`。内部库存只由人工确认入库或明确账本动作更新。

## ADR-0003 摘要：高影响动作 proposal-only

采购、库存、付款、平台写操作和账号权限变更均须人工确认、二次确认、幂等、停止、审计和补偿。Agent/MCP/LLM 不得绕过领域服务。

## ADR-0004 摘要：Provider 与控制平面分离

第三方 Provider 负责生成能力；Workspace、source snapshot、工作流版本、质量门禁、审核、导出和审计由项目控制平面负责。Provider 不拥有业务权限。

## ADR-0005 摘要：结构化事实优先

库存、订单、金额、权限和状态优先使用 SQL/领域服务；RAG 只用于知识内容；没有支持答案的证据时拒答或返回 unknown。

## ADR-0006 摘要：指标必须有来源和完整度

公开指标带 metric/value/numerator/denominator/scope/as_of/timezone/source/data_completeness/version。缺失数据不补零，空分母不输出 0%。

## ADR-0007 摘要：发布候选依赖闭包

未跟踪运行时模块、迁移、模板、测试和技术文档必须作为闭包审查；个人资料、恢复文件和面试材料不自动进入运行时发布候选。

## ADR-0008 摘要：渠道订单事实与内部 OMS 订单分离

`ExternalOrder` 只代表渠道输入；UnifiedOrder、支付/履约/退款状态和物流事实独立建模。无 API 时支持报表/ERP/manual/replay 适配，不把导入事实写成平台实时能力。

## ADR-0009 摘要：结算事实与订单事实分离

平台账单行、支付、退款和费用必须保存来源与匹配证据；匹配失败不修改订单事实，不以缺失成本/费用/汇率计算利润。

## ADR-0010 摘要：退货必须质检后处置

退货收货、质检和 `restock/repair/scrap/dispute` 分阶段；未经人工确认和质检的退货不得增加可售库存。

## ADR-0011 摘要：活动事实先于营销执行引擎

先记录活动计划、规则快照、活动日历和销量归因；优惠券/秒杀/拼团/积分执行需要独立的高并发、幂等、反作弊和资金门禁。


```markdown
# ADR-XXXX 标题

- 状态：proposed / accepted / superseded / rejected
- 日期：
- owner：
- scope：
- 复核日期：

## 背景事实

## 目标与非目标

## 假设与未知

## 候选方案

## 决策

## 取舍与影响

## 安全、权限和数据边界

## 验证证据

## 回滚/退出条件

## 关联代码、测试、Runbook
```
