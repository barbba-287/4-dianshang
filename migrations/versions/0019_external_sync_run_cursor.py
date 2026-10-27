"""为外部同步运行增加分页游标持久化列（cursor_json）。

R6（dev_task.r6-cursor-persistence.md）：
- up：加一列 TEXT NOT NULL DEFAULT '{}'。既有行由 server_default 回填为 '{}'
  （"未开始分页"的合法初始态，见 R6-8）。
- down：**只丢 cursor_json 一列**，其余列/索引/约束零变化，不重建表。
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0019_external_sync_run_cursor"
down_revision: Union[str, Sequence[str], None] = "0018_repair_service_ticket_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    column = sa.Column("cursor_json", sa.Text(), nullable=False, server_default="{}")
    if context.is_offline_mode():
        # 离线 SQL 导出没有可反射的库：直接输出已知的加列语句。
        op.add_column("external_sync_runs", column)
        return
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "external_sync_runs" not in tables:
        # 该表由 0009 创建。缺失只出现在"手工造的最小 legacy 库"（如 0018 的修复场景）：
        # 没有这张表就没有游标列可言，按 0018 的修复式迁移惯例静默跳过，不阻断升级。
        return
    columns = {item["name"] for item in inspect(bind).get_columns("external_sync_runs")}
    if "cursor_json" in columns:
        # 该库由模型先行建出（Base.metadata.create_all）并 stamp 到本版本：
        # 不重复加列，只回填空值。
        bind.execute(sa.text(
            "UPDATE external_sync_runs SET cursor_json = '{}' WHERE cursor_json IS NULL"
        ))
        return
    op.add_column("external_sync_runs", column)


def downgrade() -> None:
    # 红线：downgrade 只允许这一条 drop_column；不重建表、不动其余列/索引/约束。
    op.drop_column("external_sync_runs", "cursor_json")
