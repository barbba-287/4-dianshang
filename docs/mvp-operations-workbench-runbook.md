# V6-MVP 首个收口切片运行说明

## 范围与证据

本切片只验证“虚拟商家数据可重复生成，运营工作台可以从已有事实聚合今日待办并跳转到现有处理入口”。

- `synthetic/demo` 数据是 E2 证据：可以证明模拟数据下的流程展示行为；
- 自动化 API、权限和回放测试是 E3 证据：可以证明代码契约和边界行为；
- 不证明补货准确率、客服效果、客户采用率、真实平台发货或真实经营收益；
- 当前没有持久化 Todo 状态，待办认领、完成、跨日 SLA 和通知推送仍是后续工作。

## 本地演示步骤

```bash
# .env：仅本地 SQLite 演示库
DATABASE_URL=sqlite:///./data/dianshang.db
EMPLOYEE_AUTH_ENABLED=true
DEMO_MODE_ENABLED=true

python -m app.cli init-db
python -m app.cli seed-demo --workspace demo-shop --workspace-name "[虚构] 电商演示商家" --json
python -m app.cli init-admin --login demo-admin --display-name "演示管理员" --password "DemoPass123!" --tenant-key demo-shop --workspace-name "[虚构] 电商演示商家"
python -m uvicorn app.main:app --reload
```

登录 `/login` 后打开 `/ops`。页面顶部的“今日待办”会聚合：

- 库存告警；
- 补货建议待确认；
- 采购草稿待人工处理；
- 外部同步失败/部分成功/停滞；
- 商品页内容质量或审核事项；
- 仓库实收后的运营确认。

点击“去处理”只会跳转到已有页面或锚点，不会绕过人工审批。客服工单尚未有稳定事实模型时显示为未知，不会伪造为 0。

## API 检查

```bash
curl http://127.0.0.1:8000/api/operations/today
curl "http://127.0.0.1:8000/api/operations/today?limit=20"
curl "http://127.0.0.1:8000/api/operations/today?warehouse_id=<当前仓库ID>"
```

API 结果包含 `source_mode`、`simulated`、`evidence_level`、`data_completeness` 和 `limitations`。所有结果服务端按当前 session 的 Workspace 过滤；非管理员/运营角色只能查询自己的仓库范围。

### 今日待办 Excel 导出

导出复用同一待办查询口径，默认返回格式化 Excel 工作簿，可直接用 Excel/WPS 打开；CSV 兼容接口仍可用：

```bash
curl -OJ "http://127.0.0.1:8000/api/operations/today/export.xlsx?limit=100"
curl -OJ "http://127.0.0.1:8000/api/operations/today/export?limit=100"
```

Excel 包含居中、边框、自动换行、冻结首行、自动筛选和受限自适应列宽；长文本会换行显示。导出列为待办类型、优先级、状态、待办事项、原因和仓库，面向运营直接使用；内部代码、实体 ID、来源、允许动作和处理路径仍保留在 API 结果中，不塞进 Excel，避免重复字段和技术字段干扰。响应头会返回 `X-Export-Sha256`、行数、来源、模拟标记、证据等级和数据完整度。导出是只读动作，不会确认告警、改变库存、提交采购或改变内容审核状态；当前没有持久化下载审计。

## 客服人工工单最小闭环

客服/运营可在 `/customer-service` 手工创建工单、查看内部事件、追加内部备注和处理责任。工单状态由人工流转，AI 仅生成带知识引用的内部建议；没有证据时返回拒答/数据不足。建议需要人工发送，不会自动回复、自动关闭、发送营销消息或改变订单/库存。


登录后打开 `/orders`，使用“待发（外部已支付）”筛选查看 `paid` 外部订单，并打开详情查看订单行和 SKU 映射。页面明确标注外部订单事实和模拟/真实来源；`paid` 不等于已发货，当前不执行发货、面单、付款、退款或平台订单写回。跨 Workspace 的订单 ID 返回 404；不完整订单保留 `partial/unknown`，不补齐为完整事实。

## 待发清单 Proposal 验收

在 `/orders` 使用“待发（外部已支付）”筛选并点击“导出待发清单（人工履约）”。Excel 包含待发订单和商品明细两个运营工作表，明确写出：只读导出、需人工到平台履约、不代表已发货、不会更新订单/库存/平台状态。订单表和商品明细表使用中文字段；未映射 SKU、partial/unknown 数据和取消/退款信息缺失会保留并标记人工核对，不会静默删除或推导可发数量。生成后可能过时，履约前必须回平台复核。当前订单事实不含收件人、电话和地址，不能直接作为快递面单。

## 幂等和安全边界

- `seed-demo` 仅接受 `demo-` Workspace；仅允许 SQLite；关闭 `DEMO_MODE_ENABLED` 时 fail-closed；
- 重复 seed 应复用已有演示商品、SKU、库存、销量、告警、建议和采购草稿；
- 待办 API 是 GET，只读，不新增写操作或 CSRF 例外；
- 外部库存仍是观察事实，不写入内部库存余额；采购草稿不等于采购完成；
- 订单 Proposal 只读，不创建履约单、发货记录或库存流水；
- 不执行平台下单、付款、退款、改价、发货或库存写回。

## 回滚

本切片不新增数据库表或迁移。回滚只需撤销 `operations_workbench` 查询模块、待办 API、订单只读页面、Proposal 导出和对应文档/测试；不得删除或覆盖工作区已有数据库和用户改动。
