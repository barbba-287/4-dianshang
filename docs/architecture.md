# 第一周架构图

## 业务闭环

```text
本地商品 HTML Fixture
        │
        ▼
Playwright Chromium 采集器
        │  locator / auto-wait / timeout / screenshot / trace
        ▼
原始商品快照
        │
        ▼
Pydantic 字段校验与标准化
        │  商品 ID / 标题 / 价格 / 评分 / 分类 / URL
        ▼
SQLAlchemy 数据写入服务
        │  唯一键 + 事务 + 幂等 Upsert
        ├──────────────────────┐
        ▼                      ▼
products                 product_price_history
        │
        └──────────────┐
                       ▼
                 crawl_jobs
                       │
                       ▼
                 FastAPI API
        ├──────────────┼──────────────┐
        ▼              ▼              ▼
商品列表/筛选       价格历史       任务状态/健康检查
        │
        ▼
极简网页工作台
```

## 模块职责

| 模块 | 职责 |
|---|---|
| `app/crawler.py` | 使用 Playwright 读取 fixture，抽取商品字段，生成快照和失败产物 |
| `app/schemas.py` | 定义采集输入和 API 输出模型，执行字段质量校验 |
| `app/db.py` | 管理 SQLite/MySQL 连接和三张核心表 |
| `app/repository.py` | 负责商品幂等写入、价格变化记录和采集任务状态 |
| `app/main.py` | 提供 FastAPI 接口和演示页面 |
| `fixtures/products.html` | 可重复运行的本地数据源，不依赖真实平台 |

## 数据约束

- 商品业务唯一键：`source + external_product_id`。
- 首次写入商品时创建一条价格历史。
- 重复采集且价格不变时不新增价格历史。
- 价格发生变化时更新当前价格并新增历史记录。
- 采集失败保留 `crawl_jobs` 失败状态、错误码和错误信息。
- `.env`、数据库文件、截图、Trace 和原始快照不进入公开仓库。

## 当前边界

当前版本只验证本地 fixture 和本地数据库闭环，不代表已接入淘宝、京东等真实平台，也不执行订单、支付、下单、改价或删除操作。后续新增真实数据源前，应先确认公开/授权范围和访问规则。
