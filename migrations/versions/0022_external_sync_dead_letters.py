"""P2-4 补偿机制：新增 external_sync_dead_letters 死信表（run 级失败的隔离带）。

P2-4（dev_task.p2s4-compensation.md）：
- up：新建一张表（0021 形态里不存在这张表，故 down 是整表删除 = 还原 0021 形态）：
  - 唯一约束 `(workspace_id, run_id)`：一个 run 至多一条死信（自动登记幂等、
    重试链不产生重复行）；
  - `run_id` 只做软引用（run 行永不删除，故不设外键）；
  - **全列 nullable 或带默认**（含 server_default），**无数据回填**；
  - `next_replay_at` = 退避门槛（auto 类写死，manual 类恒 NULL）；
  - `exhausted` = open 上的标记位（不是状态）。
- down：drop 整张表，回到 0021 形态（其余表/列/索引/唯一约束零变化）。

红线：本迁移只建新表，不动任何既有表；**不用 SQLite batch**（batch 会重建成表，
本片红线禁止，见 R-33 先例）；无回填；不引入跨版本兼容包袱。
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0022_external_sync_dead_letters"
down_revision: Union[str, Sequence[str], None] = "0021_external_sync_quality"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "external_sync_dead_letters"

# 物理列顺序 = 模型声明顺序（逐列断言按此顺序核对）。
_UNIQUE_CONSTRAINT_NAME = "uq_external_sync_dead_letter_workspace_run"
_INDEX_WORKSPACE = "ix_external_sync_dead_letters_workspace_id"
_INDEX_RUN_ID = "ix_external_sync_dead_letters_run_id"


def _columns() -> list[sa.Column]:
    """列定义工厂（每次调用返回全新实例，避免复用同一 Column 对象）。"""
    return [
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=True),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("platform", sa.String(32), nullable=True),
        sa.Column("sync_type", sa.String(16), nullable=True),
        sa.Column("source_mode", sa.String(16), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column("retry_class", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("next_replay_at", sa.DateTime(), nullable=True),
        sa.Column("exhausted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("quality_status", sa.String(16), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("replayed_run_id", sa.String(64), nullable=True),
        sa.Column("actor", sa.String(128), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("ignore_reason", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    ]


def upgrade() -> None:
    if context.is_offline_mode():
        # 离线 SQL 导出没有可反射的库：直接输出建表语句。
        op.create_table(
            _TABLE,
            *_columns(),
            sa.UniqueConstraint("workspace_id", "run_id", name=_UNIQUE_CONSTRAINT_NAME),
        )
        op.create_index(_INDEX_WORKSPACE, _TABLE, ["workspace_id"], unique=False)
        op.create_index(_INDEX_RUN_ID, _TABLE, ["run_id"], unique=False)
        return

    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if _TABLE in tables:
        # 幂等：表已存在不重复建。
        return
    op.create_table(
        _TABLE,
        *_columns(),
        sa.UniqueConstraint("workspace_id", "run_id", name=_UNIQUE_CONSTRAINT_NAME),
    )
    op.create_index(_INDEX_WORKSPACE, _TABLE, ["workspace_id"], unique=False)
    op.create_index(_INDEX_RUN_ID, _TABLE, ["run_id"], unique=False)


def downgrade() -> None:
    # 红线：downgrade 只丢本迁移建的表；不动其余表/列/索引/约束，不重建表。
    if context.is_offline_mode():
        op.drop_table(_TABLE)
        return
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if _TABLE not in tables:
        return
    op.drop_table(_TABLE)
