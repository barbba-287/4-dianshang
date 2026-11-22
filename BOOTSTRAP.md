# BOOTSTRAP.md — 冷启动说明书（给几个月后的自己）

> V1.0.0（tag `v1.0.0`）存档时的快照。目标：任何一台新机器、任何一个未来的日子，
> 按本文三节操作即可从零跑起来。细节以 README 与 `docs/live-drill-runbook-20261028.md` 为准。

## 1. 如何跑起来

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium          # 可选，仅 RPA 巡检需要

python -m app.cli init-db                      # 按库状态自动选择 Alembic upgrade / stamp
python -m app.cli init-admin --login admin --display-name 管理员 \
  --password "请替换为强密码" --tenant-key default --workspace-name 默认商家

python -m uvicorn app.main:app --reload
```

- 浏览器打开 <http://127.0.0.1:8000/login>。`EMPLOYEE_AUTH_ENABLED` 默认开启，
  **首次部署必须先跑 `init-admin`**，系统不会自动创建生产账户。
- 默认库为单机 SQLite `data/dianshang.db`（Alembic head `0025_cost_snapshots`）。
  切 MySQL：复制 `.env.example` 为 `.env`，设 `DATABASE_URL=mysql+pymysql://...`，
  再 `docker compose up -d mysql`。试水阶段**建议就留在 SQLite**，跑通再考虑升级数据库。
- 全量回归（解释器无 pytest 垫片时须剥环境变量）：

```bash
env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest -q -p no:cacheprovider
```

## 2. 真实数据怎么接入（接真店试水）

- `.env` 配置三件套：`SHOPIFY_STORE_DOMAIN`、`SHOPIFY_ACCESS_TOKEN`、
  `SHOPIFY_LIVE_ENABLED`。`.env` 永不入库（`.gitignore` 已忽略）。
- **`SHOPIFY_LIVE_ENABLED` 基线保持 `false`**。临时/放开开启必须满足
  `docs/live-drill-runbook-20261028.md` **§5.2 四条件**（事前裁定 / 基线不动 /
  开门复测 Gate 0 / 有人值守 + 急停可用），缺一不开。
- 试水两原则：
  1. **先只读，不写回**。本项目本身是 proposal-only 设计——不自动下单、付款、
     退款、改价、取消订单或写回平台库存；同步侧只是拉取事实。即使同步报错，
     也不影响客户真实生意。
  2. **先开发店/测试店，再真店**；先用 SQLite 单机把数据拉下来对账，
     对得上了再谈 MySQL/PG。
- 已知行为：Shopify 只读查询不含退款明细，订单 `data_completeness=partial`
  且退款金额为 unknown——这是如实标注，不是缺陷。

## 3. 利润链路怎么跑通

利润为空的根因永远是数据链没走完，按顺序补齐四步：

| 步骤 | 做什么 | 在哪 |
|---|---|---|
| ① SKU 映射 | 把外部 SKU ↔ 内部 SKU 挂上（未映射的行 `cost_status=unmapped`，不进利润） | `/product-mappings` |
| ② 录成本 | 双入口任选：入库单行填单价，或导入成本 CSV → 生成 `cost_snapshots`（append-only，带 `effective_time` 生效时间） | `/ops` 入库单 或 成本导入 |
| ③ 导入/同步订单 | 锚定规则是 `effective_time <= 销售时间` 取最近快照。**成本生效时间必须早于订单销售时间**，否则该行 `missing` | 数据源导入 / Shopify 同步 |
| ④ 看板 | 利润中心 / 驾驶舱下钻出数 | `/cockpit` 等 |

注意事项：

- `cost_snapshots` 只 INSERT。改成本 = 追加新快照，历史利润在存储层不可篡改。
- 已锚定的订单行即使事后补成本也不会被静默改写（继承机制），要新利润就录新单。
- **多币种混合（如 CNY+USD 同时存在）时利润趋势返回空态**（`multi_currency=true`，
  `points=[]`），设计如此，不折算硬画。
- 当前存档库 `data/dianshang.db` 的利润链路就是空态：0 条成本快照、9 条订单行全部
  `unmapped/missing`。库存 / 入库 / 采购 / 补货 / 日销模块有演示数据。
