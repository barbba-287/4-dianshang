"""创建统一外部账户、映射、订单和日销量事实表。"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0010_external_sales_facts"
down_revision: Union[str, Sequence[str], None] = "0009_sync_health"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _indexes(table: str, values: dict[str, list[str]]) -> None:
    for name, columns in values.items():
        op.create_index(name, table, columns)


def upgrade() -> None:
    op.create_table(
        "external_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("account_ref", sa.String(128), nullable=False),
        sa.Column("store_ref", sa.String(128), nullable=False, server_default="default"),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Shanghai"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("source_mode", sa.String(16), nullable=False, server_default="mock"),
        sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_sync_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_external_account_workspace"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "platform", "account_ref", "store_ref", name="uq_external_account_workspace_identity"),
    )
    _indexes("external_accounts", {
        "ix_external_accounts_workspace_id": ["workspace_id"],
        "ix_external_accounts_platform": ["platform"],
        "ix_external_accounts_account_ref": ["account_ref"],
        "ix_external_accounts_store_ref": ["store_ref"],
        "ix_external_accounts_status": ["status"],
    })

    op.create_table(
        "external_product_mappings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("external_account_id", sa.Integer(), nullable=False),
        sa.Column("external_sku", sa.String(255), nullable=False),
        sa.Column("internal_sku_id", sa.Integer(), nullable=True),
        sa.Column("mapping_status", sa.String(16), nullable=False, server_default="unmapped"),
        sa.Column("source", sa.String(32), nullable=False, server_default="manual"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_external_mapping_workspace"),
        sa.ForeignKeyConstraint(["external_account_id"], ["external_accounts.id"], name="fk_external_mapping_account"),
        sa.ForeignKeyConstraint(["internal_sku_id"], ["product_skus.id"], name="fk_external_mapping_sku"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "external_account_id", "external_sku", name="uq_external_mapping_workspace_account_sku"),
    )
    _indexes("external_product_mappings", {
        "ix_external_product_mappings_workspace_id": ["workspace_id"],
        "ix_external_product_mappings_external_account_id": ["external_account_id"],
        "ix_external_product_mappings_external_sku": ["external_sku"],
        "ix_external_product_mappings_internal_sku_id": ["internal_sku_id"],
        "ix_external_product_mappings_mapping_status": ["mapping_status"],
    })

    op.create_table(
        "external_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("external_account_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("account_ref", sa.String(128), nullable=False),
        sa.Column("store_ref", sa.String(128), nullable=False, server_default="default"),
        sa.Column("external_order_no", sa.String(255), nullable=False),
        sa.Column("order_status", sa.String(32), nullable=False),
        sa.Column("external_created_at", sa.DateTime(), nullable=False),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("external_updated_at", sa.DateTime(), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=True),
        sa.Column("gross_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("refund_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(8), nullable=False, server_default="CNY"),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(72), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("source_mode", sa.String(16), nullable=False, server_default="mock"),
        sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sync_run_id", sa.String(64), nullable=True),
        sa.Column("data_completeness", sa.String(16), nullable=False, server_default="complete"),
        sa.Column("status_reason", sa.String(255), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_external_order_workspace"),
        sa.ForeignKeyConstraint(["external_account_id"], ["external_accounts.id"], name="fk_external_order_account"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "external_account_id", "external_order_no", name="uq_external_order_workspace_account_no"),
        sa.UniqueConstraint("workspace_id", "external_account_id", "idempotency_key", name="uq_external_order_workspace_account_key"),
    )
    _indexes("external_orders", {
        "ix_external_orders_workspace_id": ["workspace_id"],
        "ix_external_orders_external_account_id": ["external_account_id"],
        "ix_external_orders_platform": ["platform"],
        "ix_external_orders_account_ref": ["account_ref"],
        "ix_external_orders_store_ref": ["store_ref"],
        "ix_external_orders_external_order_no": ["external_order_no"],
        "ix_external_orders_order_status": ["order_status"],
        "ix_external_orders_external_created_at": ["external_created_at"],
        "ix_external_orders_external_updated_at": ["external_updated_at"],
        "ix_external_orders_payload_hash": ["payload_hash"],
        "ix_external_orders_idempotency_key": ["idempotency_key"],
        "ix_external_orders_sync_run_id": ["sync_run_id"],
        "ix_external_orders_data_completeness": ["data_completeness"],
    })

    op.create_table(
        "external_order_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("external_order_id", sa.Integer(), nullable=False),
        sa.Column("external_line_id", sa.String(128), nullable=False),
        sa.Column("external_sku", sa.String(255), nullable=False),
        sa.Column("internal_sku_id", sa.Integer(), nullable=True),
        sa.Column("ordered_qty", sa.Integer(), nullable=False),
        sa.Column("cancelled_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("refunded_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gross_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("refund_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(8), nullable=False, server_default="CNY"),
        sa.Column("mapping_status", sa.String(16), nullable=False, server_default="unmapped"),
        sa.Column("data_completeness", sa.String(16), nullable=False, server_default="complete"),
        sa.Column("payload_hash", sa.String(72), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_external_order_line_workspace"),
        sa.ForeignKeyConstraint(["external_order_id"], ["external_orders.id"], name="fk_external_order_line_order"),
        sa.ForeignKeyConstraint(["internal_sku_id"], ["product_skus.id"], name="fk_external_order_line_sku"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "external_order_id", "external_line_id", name="uq_external_order_line_identity"),
    )
    _indexes("external_order_lines", {
        "ix_external_order_lines_workspace_id": ["workspace_id"],
        "ix_external_order_lines_external_order_id": ["external_order_id"],
        "ix_external_order_lines_external_sku": ["external_sku"],
        "ix_external_order_lines_internal_sku_id": ["internal_sku_id"],
        "ix_external_order_lines_mapping_status": ["mapping_status"],
    })

    op.create_table(
        "daily_sku_sales",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("external_account_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("account_ref", sa.String(128), nullable=False),
        sa.Column("store_ref", sa.String(128), nullable=False, server_default="default"),
        sa.Column("external_sku", sa.String(255), nullable=False),
        sa.Column("internal_sku_id", sa.Integer(), nullable=True),
        sa.Column("sales_date", sa.Date(), nullable=False),
        sa.Column("gross_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cancelled_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("refunded_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("net_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gross_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("refund_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("net_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("order_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("data_completeness", sa.String(16), nullable=False, server_default="complete"),
        sa.Column("calculated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_daily_sku_sales_workspace"),
        sa.ForeignKeyConstraint(["external_account_id"], ["external_accounts.id"], name="fk_daily_sku_sales_account"),
        sa.ForeignKeyConstraint(["internal_sku_id"], ["product_skus.id"], name="fk_daily_sku_sales_sku"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "external_account_id", "external_sku", "sales_date", name="uq_daily_sku_sale_workspace_account_sku_date"),
    )
    _indexes("daily_sku_sales", {
        "ix_daily_sku_sales_workspace_id": ["workspace_id"],
        "ix_daily_sku_sales_external_account_id": ["external_account_id"],
        "ix_daily_sku_sales_platform": ["platform"],
        "ix_daily_sku_sales_account_ref": ["account_ref"],
        "ix_daily_sku_sales_store_ref": ["store_ref"],
        "ix_daily_sku_sales_external_sku": ["external_sku"],
        "ix_daily_sku_sales_internal_sku_id": ["internal_sku_id"],
        "ix_daily_sku_sales_sales_date": ["sales_date"],
        "ix_daily_sku_sales_data_completeness": ["data_completeness"],
    })


def downgrade() -> None:
    for table in ("daily_sku_sales", "external_order_lines", "external_orders", "external_product_mappings", "external_accounts"):
        op.drop_table(table)
