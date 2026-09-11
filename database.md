# 本地开发数据库迁移

本项目使用 Alembic 管理版本化迁移，命令入口见 `README.md` 的“数据库迁移”段落。`app/db.py` 顶层 ORM 是单一事实源；`Base.metadata.create_all()` 仅在测试 fixture 或本地 seed 阶段作为兜底，绝不替代生产迁移链路。

当前 Alembic head：`0015_replenishment_evaluations`。
