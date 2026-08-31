"""仓储协同与库存 MVP 迁移。"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003_inventory_mvp"
down_revision: Union[str, Sequence[str], None] = "0002_repair_legacy_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "product_skus",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("sku_code", sa.String(128), nullable=False),
        sa.Column("variant_label", sa.String(255), nullable=True),
        sa.Column("barcode", sa.String(64), nullable=True),
        sa.Column("unit", sa.String(32), nullable=False, server_default="件"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name="fk_sku_product"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sku_code", name="uq_product_sku_code"),
    )
    op.create_index("ix_product_skus_product_id", "product_skus", ["product_id"])
    op.create_index("ix_product_skus_sku_code", "product_skus", ["sku_code"])
    op.create_index("ix_product_skus_is_active", "product_skus", ["is_active"])

    op.create_table(
        "warehouses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("warehouse_type", sa.String(16), nullable=False, server_default="own"),
        sa.Column("integration_mode", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("external_ref", sa.String(128), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_warehouse_code"),
    )
    op.create_index("ix_warehouses_code", "warehouses", ["code"])
    op.create_index("ix_warehouses_is_active", "warehouses", ["is_active"])

    op.create_table(
        "inbound_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=False),
        sa.Column("reference_no", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="expected"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"], name="fk_inbound_warehouse"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reference_no", name="uq_inbound_reference_no"),
    )
    op.create_index("ix_inbound_orders_warehouse_id", "inbound_orders", ["warehouse_id"])
    op.create_index("ix_inbound_orders_reference_no", "inbound_orders", ["reference_no"])
    op.create_index("ix_inbound_orders_status", "inbound_orders", ["status"])

    op.create_table(
        "inbound_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("inbound_order_id", sa.Integer(), nullable=False),
        sa.Column("sku_id", sa.Integer(), nullable=False),
        sa.Column("expected_qty", sa.Integer(), nullable=False),
        sa.Column("received_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("damaged_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["inbound_order_id"], ["inbound_orders.id"], name="fk_inbound_line_order"),
        sa.ForeignKeyConstraint(["sku_id"], ["product_skus.id"], name="fk_inbound_line_sku"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("inbound_order_id", "sku_id", name="uq_inbound_line_sku"),
    )
    op.create_index("ix_inbound_lines_inbound_order_id", "inbound_lines", ["inbound_order_id"])
    op.create_index("ix_inbound_lines_sku_id", "inbound_lines", ["sku_id"])

    op.create_table(
        "inventory_balances",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=False),
        sa.Column("sku_id", sa.Integer(), nullable=False),
        sa.Column("on_hand_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"], name="fk_balance_warehouse"),
        sa.ForeignKeyConstraint(["sku_id"], ["product_skus.id"], name="fk_balance_sku"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("warehouse_id", "sku_id", name="uq_inventory_balance_warehouse_sku"),
    )
    op.create_index("ix_inventory_balances_warehouse_id", "inventory_balances", ["warehouse_id"])
    op.create_index("ix_inventory_balances_sku_id", "inventory_balances", ["sku_id"])

    op.create_table(
        "inventory_transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("inbound_order_id", sa.Integer(), nullable=True),
        sa.Column("warehouse_id", sa.Integer(), nullable=False),
        sa.Column("sku_id", sa.Integer(), nullable=False),
        sa.Column("quantity_delta", sa.Integer(), nullable=False),
        sa.Column("movement_type", sa.String(32), nullable=False, server_default="inbound"),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["inbound_order_id"], ["inbound_orders.id"], name="fk_transaction_inbound"),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"], name="fk_transaction_warehouse"),
        sa.ForeignKeyConstraint(["sku_id"], ["product_skus.id"], name="fk_transaction_sku"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_inventory_transaction_key"),
    )
    op.create_index("ix_inventory_transactions_inbound_order_id", "inventory_transactions", ["inbound_order_id"])
    op.create_index("ix_inventory_transactions_warehouse_id", "inventory_transactions", ["warehouse_id"])
    op.create_index("ix_inventory_transactions_sku_id", "inventory_transactions", ["sku_id"])
    op.create_index("ix_inventory_transactions_idempotency_key", "inventory_transactions", ["idempotency_key"])


def downgrade() -> None:
    for index, table in [
        ("ix_inventory_transactions_idempotency_key", "inventory_transactions"),
        ("ix_inventory_transactions_sku_id", "inventory_transactions"),
        ("ix_inventory_transactions_warehouse_id", "inventory_transactions"),
        ("ix_inventory_transactions_inbound_order_id", "inventory_transactions"),
    ]:
        op.drop_index(index, table_name=table)
    op.drop_table("inventory_transactions")
    for index, table in [
        ("ix_inventory_balances_sku_id", "inventory_balances"),
        ("ix_inventory_balances_warehouse_id", "inventory_balances"),
    ]:
        op.drop_index(index, table_name=table)
    op.drop_table("inventory_balances")
    for index, table in [("ix_inbound_lines_sku_id", "inbound_lines"), ("ix_inbound_lines_inbound_order_id", "inbound_lines")]:
        op.drop_index(index, table_name=table)
    op.drop_table("inbound_lines")
    for index, table in [("ix_inbound_orders_status", "inbound_orders"), ("ix_inbound_orders_reference_no", "inbound_orders"), ("ix_inbound_orders_warehouse_id", "inbound_orders")]:
        op.drop_index(index, table_name=table)
    op.drop_table("inbound_orders")
    op.drop_index("ix_warehouses_is_active", table_name="warehouses")
    op.drop_index("ix_warehouses_code", table_name="warehouses")
    op.drop_table("warehouses")
    for index in ["ix_product_skus_is_active", "ix_product_skus_sku_code", "ix_product_skus_product_id"]:
        op.drop_index(index, table_name="product_skus")
    op.drop_table("product_skus")
