# 外部平台离线示例

这些文件是用于求职演示的 mock/离线数据，不代表已经获得淘宝、京东、拼多多、抖音或 Amazon 的生产授权，也不访问网络。

- `taobao_inventory.json`、`jd_inventory.csv`、`pdd_inventory.json`、`douyin_inventory.json`：库存快照示例。
- `pdd_events.jsonl`：事件 Inbox 示例。
- `amazon_fba_inventory.json`：保留 ASIN、seller SKU、marketplace、FBA 数量字段的跨境示例。

统一字段使用 `external_sku`、`available_qty`、`reserved_qty`、`inbound_qty`；其中 `available_qty` 是外部平台观察值，不会直接修改本系统库存。对账必须通过明确的内部 SKU/仓库映射，并由内部入库确认流程写入库存流水。
