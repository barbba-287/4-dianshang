# 电商分析指标契约

- 版本：`1`
- 更新时间：2026-10-04
- 范围：当前 Workspace 的只读运营分析
- 业务时区：`UTC`（当前 `sales_date` 为 UTC naive 投影；多时区业务日不在本切片）

## 统一元数据

分析接口尽量在顶层 `meta` 和指标项中提供：

```text
metric / value / numerator / denominator
workspace_id / platform / account_ref / store_ref
warehouse_id / sku_id / business_date / timezone
as_of / source / data_completeness / metric_version / reason
```

`data_completeness` 取值：

- `complete`：所需窗口和来源事实完整；
- `partial`：有可用部分，但窗口不完整；
- `insufficient`：没有足够事实计算指标；
- `unknown`：指标所需的关联或来源不存在，不能合理推断。

缺失日期不补成零，空分母不输出 `0%`，未知值输出 `null` 并附 `reason`。

## 指标

### 销量窗口

- API：`GET /api/analytics/sales`
- 指标：7/14/30 日 `net_qty`、`daily_avg_qty`
- 分子：完整日期上的净销量（`net_qty`）；响应同时保留原始窗口净销量
- 分母：完整覆盖日期数（平均值使用完整窗口要求）
- 来源：`DailySkuSale`
- 口径：由 `resolve_sales_as_of` 决定 `as_of`；缺失日期不当作零销量
- 限制：退款/取消按现有外部订单事实投影；不代表预测准确率

### 库存健康

- API：`GET /api/analytics/inventory-health`
- 指标：库存覆盖天数、SKU 健康状态和建议补货量
- 分子：内部确认库存 `InventoryBalance.on_hand_qty`
- 分母：完整销售日期上的日均净销量
- 来源：`InventoryBalance + DailySkuSale + InventoryPolicy`
- 外部平台库存观察不写入或替代内部库存台账
- 无销量、缺失窗口或缺少内部库存时不得生成虚假覆盖天数

### 商品表现四象限

- API：`GET /api/analytics/product-quadrant`
- 维度：销量增长率 × 内部库存覆盖天数
- 分子：短窗口与基线窗口净销量差额
- 分母：基线窗口净销量；基线为零时增长率为 `null`
- 来源：`DailySkuSale + InventoryBalance`
- 不是销量×毛利 BCG，也不输出毛利结论
- 两个增长窗口不完整时，增长结论为 `partial/insufficient`；可独立保留有证据的库存覆盖信息

### 补货建议评估

- API：`POST/GET /api/analytics/replenishment-evaluation`
- 公式版本：当前 `replenishment.v1`
- `actual_sales_qty`：评估窗口每日销量事实完整时计算，否则为 `null`
- `absolute_error`：仅在 actual 可计算时计算 `abs(actual - suggested_qty)`
- `stockout_days`：当前没有完整每日库存/缺货历史，返回 `null`，原因是未知，不从当前余额倒推
- `post_replenishment_coverage_days`：当前没有 suggestion → purchase request → inbound order 的明确关联，且不能把采购提交/建议确认当作入库，因此返回 `null`
- 重复相同快照重放为 no-op；来源快照 hash、窗口和模拟标记保留

### 补货决策聚合

- API：`GET /api/analytics/replenishment-decisions`
- 采纳率：确认或调整建议数 / 有明确人工决策的建议数
- 忽略率：忽略建议数 / 有明确人工决策的建议数
- 分母为零：`value/numerator/denominator = null`，`data_completeness=unknown`
- 生成到人工入库确认耗时：当前关联证据不足，返回 `unknown`，不猜测跨表时间线
- 来源：`ReplenishmentSuggestion` 及其决策状态；数据只读

## 结算、OMS、售后和活动指标规划

以下指标只在对应事实表、来源批次和真实分母存在后实现；当前缺失证据时返回 `unknown`，不是零：

- 对账匹配率：matched 对账行 / 已进入 matching 的对账行；
- 对账差异金额：按币种和结算主体聚合的 mismatched 金额，不能跨币种直接相加；
- OMS 状态完整度：具有订单、支付、履约、退款状态的统一订单 / 统一订单总数；
- 售后处置完整度：具有退货、质检和处置结果的案件 / 已收货售后案件；
- 活动影响销量：有活动日历、活动规则和归因证据的活动销量，不能把活动期间全部销量当作活动归因；
- 供应商交付率：必须有采购订单、供应商确认和实际到货事实，缺任一事实返回 unknown。

上述指标不代表 OMS、结算、售后、营销或 SCM 已实现。


本契约不证明：AI 预测准确率、真实平台生产收益、补货采纳带来的销售增长、库存写回、自动采购或平台写操作。mock、fixture、Shopify Dev Store 和 simulated 数据必须保留来源与完整度标记。

## 验收要求

固定 fixture 必须能手算并与 API 一致；覆盖完整、partial、insufficient、unknown、空分母、跨 Workspace/仓库权限、重复评估和库存无副作用路径。任何缺失来源、成本、汇率或历史库存的指标均应保持 `unknown/insufficient`。
