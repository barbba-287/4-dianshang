"""为外部同步运行增加资源状态和人工补偿 lineage。"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0012_external_sync_compensation"
down_revision: Union[str, Sequence[str], None] = "0011_replenishment_loop"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if context.is_offline_mode():
        # The conditional legacy repair requires live inspection. Offline SQL
        # export remains safe for the normal, known schema path.
        op.add_column("external_sync_runs", sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"))
        op.add_column("external_sync_runs", sa.Column("retry_of_run_id", sa.String(64), nullable=True))
        op.add_column("external_sync_runs", sa.Column("resource_status_json", sa.Text(), nullable=False))
        op.add_column("external_sync_runs", sa.Column("retryable_resources_json", sa.Text(), nullable=False))
        op.add_column("external_sync_runs", sa.Column("retry_idempotency_key", sa.String(128), nullable=True))
        op.add_column("external_sync_runs", sa.Column("retry_payload_hash", sa.String(72), nullable=True))
        op.add_column("external_sync_runs", sa.Column("updated", sa.Integer(), nullable=False, server_default="0"))
        op.add_column("external_sync_runs", sa.Column("stale", sa.Integer(), nullable=False, server_default="0"))
        op.create_index("ix_external_sync_runs_retry_of_run_id", "external_sync_runs", ["retry_of_run_id"])
        # SQLite cannot ALTER TABLE to add a constraint in offline mode; emit
        # an index with equivalent uniqueness semantics for the known schema.
        op.create_index("uq_external_sync_run_retry_idempotency", "external_sync_runs", ["workspace_id", "retry_of_run_id", "retry_idempotency_key"], unique=True)
        return
    bind = op.get_bind()
    if "external_sync_runs" not in set(inspect(bind).get_table_names()):
        raise RuntimeError("0012 要求 external_sync_runs 表存在，无法静默跳过")
    columns = {item["name"] for item in inspect(bind).get_columns("external_sync_runs")}
    additions = (
        ("attempt", sa.Column("attempt", sa.Integer(), nullable=False, server_default="1")),
        ("retry_of_run_id", sa.Column("retry_of_run_id", sa.String(64), nullable=True)),
        # TEXT defaults are rejected by some MySQL versions. Add nullable,
        # backfill, then enforce NOT NULL without a server-side text default.
        ("resource_status_json", sa.Column("resource_status_json", sa.Text(), nullable=True)),
        ("retryable_resources_json", sa.Column("retryable_resources_json", sa.Text(), nullable=True)),
        ("retry_idempotency_key", sa.Column("retry_idempotency_key", sa.String(128), nullable=True)),
        ("retry_payload_hash", sa.Column("retry_payload_hash", sa.String(72), nullable=True)),
        ("updated", sa.Column("updated", sa.Integer(), nullable=False, server_default="0")),
        ("stale", sa.Column("stale", sa.Integer(), nullable=False, server_default="0")),
    )
    for name, column in additions:
        if name not in columns:
            with op.batch_alter_table("external_sync_runs") as batch:
                batch.add_column(column)
    if "resource_status_json" in columns or any(name == "resource_status_json" for name, _ in additions):
        bind.execute(sa.text(
            "UPDATE external_sync_runs SET resource_status_json = '{}' "
            "WHERE resource_status_json IS NULL"
        ))
    if "retryable_resources_json" in columns or any(name == "retryable_resources_json" for name, _ in additions):
        bind.execute(sa.text(
            "UPDATE external_sync_runs SET retryable_resources_json = '[]' "
            "WHERE retryable_resources_json IS NULL"
        ))
    columns = {item["name"] for item in inspect(bind).get_columns("external_sync_runs")}
    with op.batch_alter_table("external_sync_runs") as batch:
        if "resource_status_json" in columns:
            batch.alter_column("resource_status_json", existing_type=sa.Text(), nullable=False)
        if "retryable_resources_json" in columns:
            batch.alter_column("retryable_resources_json", existing_type=sa.Text(), nullable=False)
    indexes = {item["name"] for item in inspect(bind).get_indexes("external_sync_runs")}
    if "ix_external_sync_runs_retry_of_run_id" not in indexes:
        op.create_index("ix_external_sync_runs_retry_of_run_id", "external_sync_runs", ["retry_of_run_id"])
    # SQLite/MySQL both support the nullable-key uniqueness semantics needed here.
    constraints = {item["name"] for item in inspect(bind).get_unique_constraints("external_sync_runs")}
    if "uq_external_sync_run_retry_idempotency" not in constraints:
        with op.batch_alter_table("external_sync_runs") as batch:
            batch.create_unique_constraint(
                "uq_external_sync_run_retry_idempotency",
                ["workspace_id", "retry_of_run_id", "retry_idempotency_key"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    if "external_sync_runs" not in set(inspect(bind).get_table_names()):
        return
    indexes = {item["name"] for item in inspect(bind).get_indexes("external_sync_runs")}
    if "ix_external_sync_runs_retry_of_run_id" in indexes:
        op.drop_index("ix_external_sync_runs_retry_of_run_id", table_name="external_sync_runs")
    with op.batch_alter_table("external_sync_runs") as batch:
        constraints = {item["name"] for item in inspect(bind).get_unique_constraints("external_sync_runs")}
        if "uq_external_sync_run_retry_idempotency" in constraints:
            batch.drop_constraint("uq_external_sync_run_retry_idempotency", type_="unique")
        columns = {item["name"] for item in inspect(bind).get_columns("external_sync_runs")}
        for name in ("retry_payload_hash", "retry_idempotency_key", "retryable_resources_json", "resource_status_json", "retry_of_run_id", "attempt"):
            if name in columns:
                batch.drop_column(name)
