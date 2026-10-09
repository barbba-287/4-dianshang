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

## 竞品公开类目回放（当前离线能力）

`app/catalog_replay.py` 提供 `local_html_replay`：只读取 manifest 明确列出的本地 HTML，验证公开商品字段的 DOM 解析、分页、去重、限页、阻断检测和 Playwright 诊断产物。CLI 入口为 `python -m app.cli replay-catalog --manifest <path>`，默认不访问网络、不写数据库，也不接收任意远程 URL。

这不是淘宝/天猫真实竞品生产接入。未来真实页面 transport 必须单独审查平台规则和法律边界，只读取无需登录的公开商品信息；不登录竞品后台、不使用 Cookie/Token、不抓取订单/库存/销售额等非公开经营数据，不绕过验证码、MFA、风控或访问限制。遇到阻断时 fail-closed 并转人工。自有店铺后台报表自动化属于另一个明确授权的 RPA 范围。
