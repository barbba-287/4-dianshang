"""创建外部平台离线快照、事件 Inbox 和对账结果表。"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0005_external_sync"
down_revision: Union[str, Sequence[str], None] = "0004_inbound_idempotency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "external_inventory_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("account_ref", sa.String(128), nullable=False),
        sa.Column("store_ref", sa.String(128), nullable=True),
        sa.Column("marketplace", sa.String(64), nullable=True),
        sa.Column("warehouse_ref", sa.String(128), nullable=True),
        sa.Column("external_sku", sa.String(255), nullable=False),
        sa.Column("internal_sku_code", sa.String(128), nullable=True),
        sa.Column("asin", sa.String(32), nullable=True),
        sa.Column("available_qty", sa.Integer(), nullable=False),
        sa.Column("reserved_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("inbound_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("as_of", sa.DateTime(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("payload_hash", sa.String(72), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("raw_ref", sa.String(512), nullable=True),
        sa.Column("source_mode", sa.String(16), nullable=False, server_default="mock"),
        sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(24), nullable=False, server_default="observed"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("platform", "account_ref", "external_sku", "as_of", "payload_hash", name="uq_external_snapshot_identity"),
    )
    for name, cols in {
        "ix_external_inventory_snapshots_platform": ["platform"],
        "ix_external_inventory_snapshots_account_ref": ["account_ref"],
        "ix_external_inventory_snapshots_external_sku": ["external_sku"],
        "ix_external_inventory_snapshots_as_of": ["as_of"],
        "ix_external_inventory_snapshots_payload_hash": ["payload_hash"],
        "ix_external_inventory_snapshots_idempotency_key": ["idempotency_key"],
        "ix_external_inventory_snapshots_status": ["status"],
    }.items():
        op.create_index(name, "external_inventory_snapshots", cols)

    op.create_table(
        "external_event_inbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("account_ref", sa.String(128), nullable=False),
        sa.Column("external_event_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=True),
        sa.Column("external_object_no", sa.String(255), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("payload_hash", sa.String(72), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("raw_ref", sa.String(512), nullable=True),
        sa.Column("source_mode", sa.String(16), nullable=False, server_default="mock"),
        sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(24), nullable=False, server_default="received"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("platform", "account_ref", "idempotency_key", name="uq_external_event_key"),
    )
    for name, cols in {
        "ix_external_event_inbox_platform": ["platform"],
        "ix_external_event_inbox_account_ref": ["account_ref"],
        "ix_external_event_inbox_external_event_id": ["external_event_id"],
        "ix_external_event_inbox_occurred_at": ["occurred_at"],
        "ix_external_event_inbox_payload_hash": ["payload_hash"],
        "ix_external_event_inbox_idempotency_key": ["idempotency_key"],
        "ix_external_event_inbox_status": ["status"],
    }.items():
        op.create_index(name, "external_event_inbox", cols)

    op.create_table(
        "reconciliation_results",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("external_sku", sa.String(255), nullable=False),
        sa.Column("internal_sku_code", sa.String(128), nullable=True),
        sa.Column("sku_id", sa.Integer(), nullable=True),
        sa.Column("warehouse_id", sa.Integer(), nullable=True),
        sa.Column("external_available_qty", sa.Integer(), nullable=False),
        sa.Column("internal_on_hand_qty", sa.Integer(), nullable=True),
        sa.Column("delta", sa.Integer(), nullable=True),
        sa.Column("classification", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["external_inventory_snapshots.id"], name="fk_reconcile_snapshot"),
        sa.ForeignKeyConstraint(["sku_id"], ["product_skus.id"], name="fk_reconcile_sku"),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"], name="fk_reconcile_warehouse"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_id", "external_sku", "warehouse_id", name="uq_reconciliation_result_identity"),
    )
    for name, cols in {
        "ix_reconciliation_results_snapshot_id": ["snapshot_id"],
        "ix_reconciliation_results_platform": ["platform"],
        "ix_reconciliation_results_external_sku": ["external_sku"],
        "ix_reconciliation_results_sku_id": ["sku_id"],
        "ix_reconciliation_results_warehouse_id": ["warehouse_id"],
        "ix_reconciliation_results_classification": ["classification"],
    }.items():
        op.create_index(name, "reconciliation_results", cols)


def downgrade() -> None:
    for name in ("ix_reconciliation_results_classification", "ix_reconciliation_results_warehouse_id", "ix_reconciliation_results_sku_id", "ix_reconciliation_results_external_sku", "ix_reconciliation_results_platform", "ix_reconciliation_results_snapshot_id"):
        op.drop_index(name, table_name="reconciliation_results")
    op.drop_table("reconciliation_results")
    for name in ("ix_external_event_inbox_status", "ix_external_event_inbox_idempotency_key", "ix_external_event_inbox_payload_hash", "ix_external_event_inbox_occurred_at", "ix_external_event_inbox_external_event_id", "ix_external_event_inbox_account_ref", "ix_external_event_inbox_platform"):
        op.drop_index(name, table_name="external_event_inbox")
    op.drop_table("external_event_inbox")
    for name in ("ix_external_inventory_snapshots_status", "ix_external_inventory_snapshots_idempotency_key", "ix_external_inventory_snapshots_payload_hash", "ix_external_inventory_snapshots_as_of", "ix_external_inventory_snapshots_external_sku", "ix_external_inventory_snapshots_account_ref", "ix_external_inventory_snapshots_platform"):
        op.drop_index(name, table_name="external_inventory_snapshots")
    op.drop_table("external_inventory_snapshots")
