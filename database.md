# 本地开发数据库迁移

本项目第一周使用 SQLAlchemy `Base.metadata.create_all()` 管理最小 Schema，适合从零快速启动。表结构定义位于 `app/db.py`，初始化命令为：

```bash
python init_db.py
```

当前表：

- `products`：商品当前快照，`source + external_product_id` 唯一。
- `product_price_history`：价格变化历史。
- `crawl_jobs`：采集任务状态、游标、重试和错误信息。

后续接入真实环境前，再引入 Alembic 管理版本化迁移；第一周不为迁移工具增加额外复杂度。
