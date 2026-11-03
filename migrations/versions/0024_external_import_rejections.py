"""P6-S3 数据导出与异常追溯：新增 external_import_rejections 脏行隔离表。

P6-S3（dev_task.p6s3-data-export.md v1.1，Q2 用户裁定"打破零 migration 惯例仅此一次"）：
- up：新建一张表（0023 形态里不存在这张表，故 down 是整表删除 = 还原 0023 形态）：
  - 唯一约束 `(workspace_id, run_id, row_number)`：同文件重传（幂等 no_op）时
    rejected 行不重复膨胀；
  - `run_id` 只做软引用（run 行永不删除，故不设外键，与 0022 死信表同范式）；
  - `raw_data` 存原始行 JSON（键序即上传列序），`row_number` 为 1-based 数据行序号；
  - **全列带默认或无 server_default 依赖的回填**；本表无历史数据，无需回填。
- down：drop 整张表，回到 0023 形态（其余表/列/索引/唯一约束零变化）。

红线：本迁移只建新表，不动任何既有表；**不用 SQLite batch**（batch 会重建成表，
见 R-33 先例）；不引跨版本兼容包袱。
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0024_external_import_rejections"
down_revision: Union[str, Sequence[str], None] = "0023_purchase_approval"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "external_import_rejections"

# 物理列顺序 = 模型声明顺序（逐列断言按此顺序核对）。
_UNIQUE_CONSTRAINT_NAME = "uq_external_import_rejection_row"
_INDEX_WORKSPACE = "ix_external_import_rejections_workspace_id"
_INDEX_RUN_ID = "ix_external_import_rejections_run_id"
_INDEX_KIND = "ix_external_import_rejections_kind"


def _columns() -> list[sa.Column]:
    """列定义工厂（每次调用返回全新实例，避免复用同一 Column 对象）。"""
    return [
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("raw_data", sa.Text(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=False, server_default="EXTERNAL_INPUT_INVALID"),
        sa.Column("error_reason", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    ]


def _create() -> None:
    op.create_table(
        _TABLE,
        *_columns(),
        sa.UniqueConstraint("workspace_id", "run_id", "row_number", name=_UNIQUE_CONSTRAINT_NAME),
    )
    op.create_index(_INDEX_WORKSPACE, _TABLE, ["workspace_id"], unique=False)
    op.create_index(_INDEX_RUN_ID, _TABLE, ["run_id"], unique=False)
    op.create_index(_INDEX_KIND, _TABLE, ["kind"], unique=False)


def upgrade() -> None:
    if context.is_offline_mode():
        # 离线 SQL 导出没有可反射的库：直接输出建表语句。
        _create()
        return

    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if _TABLE in tables:
        # 幂等：表已存在不重复建（create_all 建过测试库的情形）。
        return
    _create()


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
