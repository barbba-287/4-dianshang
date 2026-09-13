/* Shared display labels for user-facing enum values. Raw values remain for business logic. */
window.DianShangLabels = Object.freeze({
  status: Object.freeze({healthy:'健康',reorder:'建议补货',urgent:'紧急',data_insufficient:'数据不足',no_sales:'无销量',suggested:'待处理',modified:'已调整',confirmed:'已确认',ignored:'已忽略',submitted:'已提交',draft:'草稿',running:'运行中',succeeded:'成功',failed:'失败',partial:'部分成功',stalled:'停滞',expected:'待收货',received:'已收货待确认',cancelled:'已取消',queued:'排队中',retry_wait:'等待重试'}),
  completeness: Object.freeze({complete:'完整',insufficient:'数据不足',partial:'部分完整'}),
  reason: Object.freeze({LOW_STOCK:'库存偏低',STOCK_SUFFICIENT:'库存充足',INCOMPLETE_COVERAGE:'销量覆盖不完整',NO_SALES:'暂无销量'}),
  quadrant: Object.freeze({focal_supplement:'重点补货',healthy:'健康商品',watch:'观察商品',slow_risk:'滞销风险',insufficient:'数据不足'}),
  syncType: Object.freeze({orders:'订单同步',inventory:'库存同步',events:'事件同步',fixture_bundle:'订单与库存同步'}),
  resource: Object.freeze({orders:'订单',inventory:'库存'}),
  platform: Object.freeze({taobao:'淘宝',jd:'京东',pdd:'拼多多',douyin:'抖音',amazon:'亚马逊',mock:'模拟平台'}),
  warehouseType: Object.freeze({own:'自有仓',third_party:'第三方仓'}),
  role: Object.freeze({admin:'管理员',operations:'运营',warehouse:'仓库协作方',customer_service:'客服',readonly:'只读成员'}),
  severity: Object.freeze({critical:'严重',warning:'警告',info:'提示'}),
  alertStatus: Object.freeze({open:'待处理',acknowledged:'已确认',resolved:'已解决'}),
  alertKind: Object.freeze({low_stock:'库存偏低',external_sync_failed:'外部同步失败',external_sync_partial:'外部同步部分成功',external_sync_stalled:'外部同步停滞',external_snapshot_stale:'外部库存快照过期',external_snapshot_clock_skew:'外部库存快照时间异常'}),
  errorCode: Object.freeze({TIMEOUT:'请求超时',RATE_LIMITED:'请求频率受限',UPSTREAM_5XX:'上游服务异常',TEMPORARY_IO_ERROR:'临时读写错误',INCOMPLETE_COVERAGE:'数据覆盖不完整'}),
  orderStatus: Object.freeze({pending:'待处理',paid:'已支付',fulfilled:'已履约',completed:'已完成',cancelled:'已取消',closed:'已关闭',unknown:'未知'}),
  mappingStatus: Object.freeze({mapped:'已映射',unmapped:'未映射'}),
  evaluationStatus: Object.freeze({evaluated:'已评估',insufficient:'数据不足'})
});
window.dianshangLabel = function(value, group, fallback='其他') {
  if (value == null || value === '') return '数据不足';
  return (window.DianShangLabels[group] && window.DianShangLabels[group][value]) || `${fallback}（${value}）`;
};
