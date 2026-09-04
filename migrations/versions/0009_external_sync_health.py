"""外部同步健康状态的增量迁移。"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0009_sync_health"
down_revision: Union[str, Sequence[str], None] = "0008_inventory_alerts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    for table in ("external_inventory_snapshots", "external_event_inbox"):
        if table not in tables:
            continue
        columns = {item["name"] for item in inspect(bind).get_columns(table)}
        if "sync_run_id" not in columns:
            with op.batch_alter_table(table) as batch:
                batch.add_column(sa.Column("sync_run_id", sa.String(64), nullable=True))
        indexes = {item["name"] for item in inspect(bind).get_indexes(table)}
        index_name = f"ix_{table}_sync_run_id"
        if index_name not in indexes:
            op.create_index(index_name, table, ["sync_run_id"])

    if "external_sync_runs" in tables:
        return
    op.create_table(
        "external_sync_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("account_ref", sa.String(128), nullable=True),
        sa.Column("store_ref", sa.String(128), nullable=True),
        sa.Column("sync_type", sa.String(16), nullable=False),
        sa.Column("source_mode", sa.String(16), nullable=False, server_default="mock"),
        sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("inserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("no_op", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("conflict", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_external_sync_run_workspace"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "run_id", name="uq_external_sync_run_workspace_id"),
    )
    for name, columns in {
        "ix_external_sync_runs_workspace_id": ["workspace_id"],
        "ix_external_sync_runs_run_id": ["run_id"],
        "ix_external_sync_runs_platform": ["platform"],
        "ix_external_sync_runs_account_ref": ["account_ref"],
        "ix_external_sync_runs_store_ref": ["store_ref"],
        "ix_external_sync_runs_sync_type": ["sync_type"],
        "ix_external_sync_runs_status": ["status"],
    }.items():
        op.create_index(name, "external_sync_runs", columns)


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "external_sync_runs" in tables:
        for name in (
            "ix_external_sync_runs_status", "ix_external_sync_runs_sync_type",
            "ix_external_sync_runs_store_ref", "ix_external_sync_runs_account_ref",
            "ix_external_sync_runs_platform", "ix_external_sync_runs_run_id",
            "ix_external_sync_runs_workspace_id",
        ):
            if name in {item["name"] for item in inspect(bind).get_indexes("external_sync_runs")}:
                op.drop_index(name, table_name="external_sync_runs")
        op.drop_table("external_sync_runs")
    for table in ("external_inventory_snapshots", "external_event_inbox"):
        if table not in tables:
            continue
        if f"ix_{table}_sync_run_id" in {item["name"] for item in inspect(bind).get_indexes(table)}:
            op.drop_index(f"ix_{table}_sync_run_id", table_name=table)
        if "sync_run_id" in {item["name"] for item in inspect(bind).get_columns(table)}:
            with op.batch_alter_table(table) as batch:
                batch.drop_column("sync_run_id")
