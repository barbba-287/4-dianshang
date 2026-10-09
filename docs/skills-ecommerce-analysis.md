# 电商分析 Skill 使用说明

## 目标

面向运营人员提供只读分析，不执行下单、采购、改价、广告修改、库存写回或平台写操作。

## 自然语言示例

- “分析 SKU 101 最近 7 天销量有没有波动。”
  - 调用 `query_sales_trend`。
- “看 campaign-001 的广告 ROI 是否正常。”
  - 调用 `get_ad_performance`。
- “检查仓库 1 有没有低库存预警。”
  - 调用 `check_inventory_alert`。
- “找出茶饮类目前值得关注的商品。”
  - 调用 `find_product_opportunities`。

## 结论边界

Skill 返回：

- metric/value；
- numerator/denominator（适用时）；
- source/source_mode；
- as_of/timezone；
- data_completeness；
- evidence；
- stable error code。

缺失日期不当作零；缺归因成本不计算广告收益；没有竞争、利润或真实平台数据时不编造结论。

## 权限与审计

- workspace 从登录 session 或可信 Webman gateway 上下文派生；
- 模型不能在参数中覆盖 workspace/tenant；
- 工具注册为 read-only；
- 每次调用记录 actor、workspace、request_id、tool、输入摘要、结果码和耗时；
- 写操作需要另一个 proposal + 人工审批链路，本 Demo 不暴露写工具。

## Demo 证据级别

当前使用本地 Mock fixture，属于 E2（模拟数据）和 E3（可复现代码/测试）证据，不代表真实 Webman 接入或经营收益。
