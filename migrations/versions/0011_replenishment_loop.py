"""创建补货建议和内部采购申请闭环表。"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0011_replenishment_loop"
down_revision: Union[str, Sequence[str], None] = "0010_external_sales_facts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _indexes(table: str, values: dict[str, list[str]]) -> None:
    for name, columns in values.items():
        op.create_index(name, table, columns)


def upgrade() -> None:
    op.create_table(
        "replenishment_suggestions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=False),
        sa.Column("sku_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="suggested"),
        sa.Column("suggested_qty", sa.Integer(), nullable=True),
        sa.Column("decision_qty", sa.Integer(), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("decision_by", sa.String(128), nullable=True),
        sa.Column("decision_at", sa.DateTime(), nullable=True),
        sa.Column("submitted_by", sa.String(128), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("formula_version", sa.String(32), nullable=False, server_default="replenishment.v1"),
        sa.Column("coverage_days", sa.Integer(), nullable=False, server_default="14"),
        sa.Column("daily_avg_qty", sa.Numeric(14, 6), nullable=True),
        sa.Column("on_hand_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("safety_stock_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reorder_point_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sales_window_days", sa.Integer(), nullable=False, server_default="14"),
        sa.Column("sales_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("effective_sale_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("data_completeness", sa.String(16), nullable=False, server_default="insufficient"),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("source_hash", sa.String(72), nullable=False),
        sa.Column("source_snapshot_json", sa.Text(), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("active_slot", sa.String(16), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_replenishment_suggestion_workspace"),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"], name="fk_replenishment_suggestion_warehouse"),
        sa.ForeignKeyConstraint(["sku_id"], ["product_skus.id"], name="fk_replenishment_suggestion_sku"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "warehouse_id", "sku_id", "source_hash", name="uq_replenishment_suggestion_source"),
        sa.UniqueConstraint("workspace_id", "warehouse_id", "sku_id", "active_slot", name="uq_replenishment_suggestion_active_slot"),
    )
    _indexes("replenishment_suggestions", {
        "ix_replenishment_suggestions_workspace_id": ["workspace_id"],
        "ix_replenishment_suggestions_warehouse_id": ["warehouse_id"],
        "ix_replenishment_suggestions_sku_id": ["sku_id"],
        "ix_replenishment_suggestions_status": ["status"],
        "ix_replenishment_suggestions_source_hash": ["source_hash"],
        "ix_replenishment_suggestions_as_of_date": ["as_of_date"],
        "ix_replenishment_suggestions_active_slot": ["active_slot"],
    })

    op.create_table(
        "replenishment_suggestion_actions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("suggestion_id", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.String(24), nullable=False, server_default="decision"),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("payload_hash", sa.String(72), nullable=False),
        sa.Column("from_status", sa.String(16), nullable=False),
        sa.Column("to_status", sa.String(16), nullable=False),
        sa.Column("decision_qty", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_replenishment_action_workspace"),
        sa.ForeignKeyConstraint(["suggestion_id"], ["replenishment_suggestions.id"], name="fk_replenishment_action_suggestion"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "suggestion_id", "idempotency_key", name="uq_replenishment_action_idempotency"),
    )
    _indexes("replenishment_suggestion_actions", {
        "ix_replenishment_suggestion_actions_workspace_id": ["workspace_id"],
        "ix_replenishment_suggestion_actions_suggestion_id": ["suggestion_id"],
        "ix_replenishment_suggestion_actions_idempotency_key": ["idempotency_key"],
    })

    op.create_table(
        "purchase_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=False),
        sa.Column("request_no", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="submitted"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("submitted_by", sa.String(128), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("payload_hash", sa.String(72), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_purchase_request_workspace"),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"], name="fk_purchase_request_warehouse"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "request_no", name="uq_purchase_request_workspace_no"),
        sa.UniqueConstraint("workspace_id", "idempotency_key", name="uq_purchase_request_workspace_key"),
    )
    _indexes("purchase_requests", {
        "ix_purchase_requests_workspace_id": ["workspace_id"],
        "ix_purchase_requests_warehouse_id": ["warehouse_id"],
        "ix_purchase_requests_request_no": ["request_no"],
        "ix_purchase_requests_status": ["status"],
        "ix_purchase_requests_idempotency_key": ["idempotency_key"],
    })

    op.create_table(
        "purchase_request_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("purchase_request_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=False),
        sa.Column("sku_id", sa.Integer(), nullable=False),
        sa.Column("suggestion_id", sa.Integer(), nullable=False),
        sa.Column("requested_qty", sa.Integer(), nullable=False),
        sa.Column("source_suggestion_version", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_purchase_request_line_workspace"),
        sa.ForeignKeyConstraint(["purchase_request_id"], ["purchase_requests.id"], name="fk_purchase_request_line_request"),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"], name="fk_purchase_request_line_warehouse"),
        sa.ForeignKeyConstraint(["sku_id"], ["product_skus.id"], name="fk_purchase_request_line_sku"),
        sa.ForeignKeyConstraint(["suggestion_id"], ["replenishment_suggestions.id"], name="fk_purchase_request_line_suggestion"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "purchase_request_id", "sku_id", name="uq_purchase_request_line_sku"),
        sa.UniqueConstraint("workspace_id", "suggestion_id", name="uq_purchase_request_line_suggestion"),
    )
    _indexes("purchase_request_lines", {
        "ix_purchase_request_lines_workspace_id": ["workspace_id"],
        "ix_purchase_request_lines_purchase_request_id": ["purchase_request_id"],
        "ix_purchase_request_lines_warehouse_id": ["warehouse_id"],
        "ix_purchase_request_lines_sku_id": ["sku_id"],
        "ix_purchase_request_lines_suggestion_id": ["suggestion_id"],
    })


def downgrade() -> None:
    op.drop_table("purchase_request_lines")
    op.drop_table("purchase_requests")
    op.drop_table("replenishment_suggestion_actions")
    op.drop_table("replenishment_suggestions")
