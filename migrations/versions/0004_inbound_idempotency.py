"""补齐仓储入库幂等字段。"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_inbound_idempotency"
down_revision: Union[str, Sequence[str], None] = "0003_inventory_mvp"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = (
    ("receive_idempotency_key", sa.String(128)),
    ("receive_payload_hash", sa.String(64)),
    ("confirm_idempotency_key", sa.String(128)),
    ("confirm_payload_hash", sa.String(64)),
)


def upgrade() -> None:
    with op.batch_alter_table("inbound_orders") as batch_op:
        for name, column_type in _COLUMNS:
            batch_op.add_column(sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("inbound_orders") as batch_op:
        for name, _column_type in reversed(_COLUMNS):
            batch_op.drop_column(name)
