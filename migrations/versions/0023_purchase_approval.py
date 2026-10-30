"""P3-S1 采购单审批：purchase_requests 追加 7 列审批/血缘列，inbound_orders 追加 1 列来源列 + 唯一索引。

P3-S1（dev_task.p3s1-purchase-approval.md）：
- up：
  - `purchase_requests` 追加 7 个**可空**列（6 审批列 + 1 血缘列），无 server_default、无回填：
    `approver_id` / `approver_user_id` / `decided_at` / `reject_reason` / `cancelled_by` /
    `cancelled_at` / `supersedes_request_id`；
  - `inbound_orders` 追加 1 个可空列 `purchase_request_id`（软引用，不设外键）；
  - 建唯一**索引** `uq_inbound_order_purchase_request(workspace_id, purchase_request_id)`：
    一采购单至多一张入库单。用索引而非 UniqueConstraint，是因为 SQLite 的 DROP CONSTRAINT
    不支持，约束会迫使 down 走 batch_alter_table（R-33 先例禁止）。
- down：先 DROP INDEX → 再逐列 DROP COLUMN（7 + 1），回到 0022 形态；
  两表其余列/索引/唯一约束零变化。

红线：只做"加可空列 + 加索引"，不改既有列语义、不回填数据、不引入默认值；
**不用 SQLite batch**（batch 会重建成表，本片红线禁止，见 R-33 先例）。
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0023_purchase_approval"
down_revision: Union[str, Sequence[str], None] = "0022_external_sync_dead_letters"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 追加到 purchase_requests 的 7 个可空列（顺序即迁移后的物理列顺序，也与模型声明顺序一致）。
_PURCHASE_REQUEST_COLUMNS: tuple[tuple[str, sa.Column], ...] = (
    ("approver_id", sa.Column("approver_id", sa.String(128), nullable=True)),
    ("approver_user_id", sa.Column("approver_user_id", sa.Integer(), nullable=True)),
    ("decided_at", sa.Column("decided_at", sa.DateTime(), nullable=True)),
    ("reject_reason", sa.Column("reject_reason", sa.Text(), nullable=True)),
    ("cancelled_by", sa.Column("cancelled_by", sa.String(128), nullable=True)),
    ("cancelled_at", sa.Column("cancelled_at", sa.DateTime(), nullable=True)),
    ("supersedes_request_id", sa.Column("supersedes_request_id", sa.Integer(), nullable=True)),
)

# 追加到 inbound_orders 的 1 个可空列。
_INBOUND_ORDER_COLUMNS: tuple[tuple[str, sa.Column], ...] = (
    ("purchase_request_id", sa.Column("purchase_request_id", sa.Integer(), nullable=True)),
)

_INDEX_NAME = "uq_inbound_order_purchase_request"
_INDEX_TABLE = "inbound_orders"
_INDEX_COLUMNS = ["workspace_id", "purchase_request_id"]


def upgrade() -> None:
    if context.is_offline_mode():
        # 离线 SQL 导出没有可反射的库：直接输出已知的加列/建索引语句。
        for _name, column in _PURCHASE_REQUEST_COLUMNS:
            op.add_column("purchase_requests", column)
        for _name, column in _INBOUND_ORDER_COLUMNS:
            op.add_column(_INDEX_TABLE, column)
        op.create_index(_INDEX_NAME, _INDEX_TABLE, _INDEX_COLUMNS, unique=True)
        return

    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "purchase_requests" in tables:
        columns = {item["name"] for item in inspect(bind).get_columns("purchase_requests")}
        for name, column in _PURCHASE_REQUEST_COLUMNS:
            if name not in columns:
                op.add_column("purchase_requests", column)
    if _INDEX_TABLE in tables:
        columns = {item["name"] for item in inspect(bind).get_columns(_INDEX_TABLE)}
        for name, column in _INBOUND_ORDER_COLUMNS:
            if name not in columns:
                op.add_column(_INDEX_TABLE, column)
        indexes = {item["name"] for item in inspect(bind).get_indexes(_INDEX_TABLE)}
        if _INDEX_NAME not in indexes:
            op.create_index(_INDEX_NAME, _INDEX_TABLE, _INDEX_COLUMNS, unique=True)


def downgrade() -> None:
    # 红线：downgrade 只丢本迁移加的索引与列；不动其余列/索引/约束，不重建表。
    # 顺序写死：先索引后列。
    if context.is_offline_mode():
        op.drop_index(_INDEX_NAME, table_name=_INDEX_TABLE)
        for name, _column in reversed(_INBOUND_ORDER_COLUMNS):
            op.drop_column(_INDEX_TABLE, name)
        for name, _column in reversed(_PURCHASE_REQUEST_COLUMNS):
            op.drop_column("purchase_requests", name)
        return

    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if _INDEX_TABLE in tables:
        indexes = {item["name"] for item in inspect(bind).get_indexes(_INDEX_TABLE)}
        if _INDEX_NAME in indexes:
            op.drop_index(_INDEX_NAME, table_name=_INDEX_TABLE)
        columns = {item["name"] for item in inspect(bind).get_columns(_INDEX_TABLE)}
        for name, _column in reversed(_INBOUND_ORDER_COLUMNS):
            if name in columns:
                op.drop_column(_INDEX_TABLE, name)
    if "purchase_requests" in tables:
        columns = {item["name"] for item in inspect(bind).get_columns("purchase_requests")}
        for name, _column in reversed(_PURCHASE_REQUEST_COLUMNS):
            if name in columns:
                op.drop_column("purchase_requests", name)
