# 第一周验收记录

## 验收范围

目标是验证：商品采集、字段校验与标准化、数据库持久化、幂等写入、价格历史、任务记录、FastAPI 查询和网页入口。

## 自动化测试

执行：

```powershell
python -m pytest -q
```

当前结果：

```text
9 passed
```

覆盖内容：

- 健康检查
- 商品采集接口
- 重复采集不重复写入商品
- 分类筛选和商品不存在错误
- 相同价格不重复写历史
- 价格变化新增历史
- 采集失败任务记录
- 商品字段清洗、价格/评分解析和非法数据拦截

## 真实 MySQL + Playwright 验收

环境：

- MySQL 8.4 Docker 容器：`dianshang-mysql`
- 容器状态：`Up (healthy)`
- Playwright Chromium：已通过国内镜像安装
- 数据源：`fixtures/products.html`

执行：

```powershell
python init_db.py
python crawl_fixture.py
```

结果：

```text
采集完成: 3 件商品，任务 #1，状态=succeeded
products= 3
price_history= 3
jobs= 1
job_status= succeeded
```

API 和页面验证：

```text
health = {"status": "ok"}
首页 HTTP 状态 = 200
连续两次采集 HTTP 状态 = 201, 201
商品总数 = 3
相同价格重复采集后的茶叶价格历史 = 1 条
```

## 结论

第一周最小闭环已完成。当前成果是可运行的演示版本，不将本地 fixture 验收数字表述为真实电商平台生产指标。第二周再增加文档导入、版本管理和 RAG 客服知识库。
