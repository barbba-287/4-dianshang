"""新增库存告警表，支持低库存告警去重与确认生命周期。"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0008_inventory_alerts"
down_revision: Union[str, Sequence[str], None] = "0007_workspace_iso"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if "inventory_alerts" in set(inspect(bind).get_table_names()):
        return
    op.create_table(
        "inventory_alerts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False, server_default="warning"),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("dedupe_key", sa.String(255), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=True),
        sa.Column("sku_id", sa.Integer(), nullable=True),
        sa.Column("platform", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_inventory_alert_workspace"),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"], name="fk_inventory_alert_warehouse"),
        sa.ForeignKeyConstraint(["sku_id"], ["product_skus.id"], name="fk_inventory_alert_sku"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "dedupe_key", name="uq_inventory_alert_workspace_dedupe"),
    )
    for name, columns in {
        "ix_inventory_alerts_workspace_id": ["workspace_id"],
        "ix_inventory_alerts_kind": ["kind"],
        "ix_inventory_alerts_severity": ["severity"],
        "ix_inventory_alerts_status": ["status"],
        "ix_inventory_alerts_dedupe_key": ["dedupe_key"],
        "ix_inventory_alerts_warehouse_id": ["warehouse_id"],
        "ix_inventory_alerts_sku_id": ["sku_id"],
        "ix_inventory_alerts_platform": ["platform"],
    }.items():
        op.create_index(name, "inventory_alerts", columns)


def downgrade() -> None:
    bind = op.get_bind()
    if "inventory_alerts" not in set(inspect(bind).get_table_names()):
        return
    for name in (
        "ix_inventory_alerts_platform", "ix_inventory_alerts_sku_id",
        "ix_inventory_alerts_warehouse_id", "ix_inventory_alerts_dedupe_key",
        "ix_inventory_alerts_status", "ix_inventory_alerts_severity",
        "ix_inventory_alerts_kind", "ix_inventory_alerts_workspace_id",
    ):
        op.drop_index(name, table_name="inventory_alerts")
    op.drop_table("inventory_alerts")
