# 本地开发数据库迁移

本项目使用 Alembic 管理版本化迁移，命令入口见 `README.md` 的“数据库迁移”段落。`app/db.py` 顶层 ORM 是单一事实源；`Base.metadata.create_all()` 仅在测试 fixture 或本地 seed 阶段作为兜底，绝不替代生产迁移链路。

当前 Alembic head：`0018_repair_service_ticket_schema`。

P0-0 验证记录（2026-10-04）：历史记录中的唯一 head 为 `0016_content_production`；本次工单兼容迁移新增 `0018_repair_service_ticket_schema`。临时 SQLite 旧工单表、字段修复和列表 API 回归需按当前验证命令重新执行。MySQL 和 offline SQL 仍未在本次环境验证。
