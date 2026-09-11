"""Create replenishment evaluation snapshots."""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0015_replenishment_evaluations"
down_revision: Union[str, Sequence[str], None] = "0014_purchase_request_drafts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_table() -> None:
    op.create_table(
        "replenishment_evaluations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("suggestion_id", sa.Integer(), nullable=False),
        sa.Column("sku_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=False),
        sa.Column("formula_version", sa.String(32), nullable=False),
        sa.Column("suggested_qty", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("window_end", sa.Date(), nullable=False),
        sa.Column("actual_sales_qty", sa.Integer(), nullable=True),
        sa.Column("stockout_days", sa.Integer(), nullable=True),
        sa.Column("post_replenishment_coverage_days", sa.Numeric(14, 6), nullable=True),
        sa.Column("absolute_error", sa.Integer(), nullable=True),
        sa.Column("evaluation_status", sa.String(16), nullable=False),
        sa.Column("data_completeness", sa.String(16), nullable=False),
        sa.Column("source_snapshot_hash", sa.String(72), nullable=False),
        sa.Column("source_snapshot_json", sa.Text(), nullable=False),
        sa.Column("source_mode", sa.String(16), nullable=False, server_default="mock"),
        sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_replenishment_evaluation_workspace"),
        sa.ForeignKeyConstraint(["suggestion_id"], ["replenishment_suggestions.id"], name="fk_replenishment_evaluation_suggestion"),
        sa.ForeignKeyConstraint(["sku_id"], ["product_skus.id"], name="fk_replenishment_evaluation_sku"),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"], name="fk_replenishment_evaluation_warehouse"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "suggestion_id", "window_start", "window_end", "formula_version", "source_snapshot_hash",
            name="uq_replenishment_evaluation_identity",
        ),
    )
    for name, column in (
        ("ix_replenishment_evaluations_workspace_id", "workspace_id"),
        ("ix_replenishment_evaluations_suggestion_id", "suggestion_id"),
        ("ix_replenishment_evaluations_sku_id", "sku_id"),
        ("ix_replenishment_evaluations_warehouse_id", "warehouse_id"),
        ("ix_replenishment_evaluations_window_start", "window_start"),
        ("ix_replenishment_evaluations_window_end", "window_end"),
        ("ix_replenishment_evaluations_status", "evaluation_status"),
        ("ix_replenishment_evaluations_completeness", "data_completeness"),
        ("ix_replenishment_evaluations_source_hash", "source_snapshot_hash"),
    ):
        op.create_index(name, "replenishment_evaluations", [column])


def upgrade() -> None:
    if context.is_offline_mode():
        _create_table()
        return
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "replenishment_evaluations" not in tables:
        _create_table()


def downgrade() -> None:
    bind = op.get_bind()
    if "replenishment_evaluations" not in set(inspect(bind).get_table_names()):
        return
    indexes = {item["name"] for item in inspect(bind).get_indexes("replenishment_evaluations")}
    for name in (
        "ix_replenishment_evaluations_source_hash",
        "ix_replenishment_evaluations_completeness",
        "ix_replenishment_evaluations_status",
        "ix_replenishment_evaluations_window_end",
        "ix_replenishment_evaluations_window_start",
        "ix_replenishment_evaluations_warehouse_id",
        "ix_replenishment_evaluations_sku_id",
        "ix_replenishment_evaluations_suggestion_id",
        "ix_replenishment_evaluations_workspace_id",
    ):
        if name in indexes:
            op.drop_index(name, table_name="replenishment_evaluations")
    op.drop_table("replenishment_evaluations")
