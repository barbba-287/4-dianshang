# 电商商品与客服工作台

第一周目标：完成“本地商品页面采集 → 校验/标准化 → SQLite/MySQL 持久化 → FastAPI 查询 → 网页展示”的最小闭环。

## 当前状态

- 项目目录：`D:\\ai\\dianshang`
- 采集数据：仅使用项目内本地 `fixtures/products.html`，不连接真实平台。
- 开发数据库：默认 SQLite，数据库 URL 通过 `DATABASE_URL` 配置；生产演示可切换 MySQL。
- 当前范围：商品采集、幂等 Upsert、价格历史、任务记录、商品查询和极简页面。
- 尚未实现：RAG、Agent、真实订单、支付、自动下单、改价和外部平台反爬适配。

## 启动

在项目虚拟环境中安装依赖：

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

初始化并直接采集本地样例：

```bash
python init_db.py
python crawl_fixture.py
```

启动 API：

```bash
python -m uvicorn app.main:app --reload
```

浏览器访问 <http://127.0.0.1:8000/>。

默认 SQLite 数据文件为 `data/dianshang.db`。切换 MySQL 时，复制 `.env.example` 为 `.env`，设置：

```text
DATABASE_URL=mysql+pymysql://dianshang:dianshang@localhost:3306/dianshang?charset=utf8mb4
```

然后可用 `docker compose up -d mysql` 启动本地 MySQL。

## API

- `GET /health`：服务健康检查
- `POST /api/crawl/fixture`：采集本地 fixture 并幂等写入
- `GET /api/products?page=1&page_size=20&keyword=...&category=...`：商品分页和筛选
- `GET /api/products/{id}`：商品详情
- `GET /api/products/{id}/price-history`：价格历史
- `GET /api/crawl/jobs`：采集任务记录
- `GET /docs`：OpenAPI 文档


## Playwright 浏览器安装（国内网络）

如果直接执行 `playwright install chromium` 下载缓慢或卡住，可以在 PowerShell 临时设置 Playwright 下载镜像，再安装 Chromium：

```powershell
$env:PLAYWRIGHT_DOWNLOAD_HOST = "https://npmmirror.com/mirrors/playwright"
playwright install chromium
```

如果当前终端使用的是项目解释器，也可以明确调用：

```powershell
$env:PLAYWRIGHT_DOWNLOAD_HOST = "https://npmmirror.com/mirrors/playwright"
python -m playwright install chromium
```

安装后可以检查浏览器是否可启动：

```powershell
python -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); print('chromium ok'); b.close(); p.stop()"
```

镜像只用于下载 Playwright 浏览器运行时；采集器本身仍只访问项目内本地 fixture。

## 验收

```bash
python -m pytest -q
```

验收链路：点击“采集样例商品”后写入 3 件本地商品；再次点击不会产生重复商品或重复价格历史；商品列表支持关键词查询和分类筛选；不存在的商品返回结构化 404。
失败采集会在 `artifacts/` 留下截图；真实外部页面接入前必须确认公开/授权范围、访问条款和频率限制。
