"""Add purchase request draft lifecycle fields and audit actions.

Revision ID: 0014_purchase_request_drafts
Revises: 0013_external_snapshot_scope
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0014_purchase_request_drafts"
down_revision: Union[str, Sequence[str], None] = "0013_external_snapshot_scope"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if context.is_offline_mode():
        op.add_column("purchase_requests", sa.Column("created_by", sa.String(128), nullable=True))
        op.add_column("purchase_requests", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        op.add_column("purchase_requests", sa.Column("supplier_ref", sa.String(128), nullable=True))
        op.add_column("purchase_requests", sa.Column("expected_arrival_date", sa.Date(), nullable=True))
        op.alter_column("purchase_requests", "submitted_by", existing_type=sa.String(128), nullable=True)
        op.alter_column("purchase_requests", "submitted_at", existing_type=sa.DateTime(), nullable=True)
        op.create_table(
            "purchase_request_actions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("workspace_id", sa.Integer(), nullable=False),
            sa.Column("purchase_request_id", sa.Integer(), nullable=False),
            sa.Column("action_type", sa.String(24), nullable=False),
            sa.Column("from_status", sa.String(16), nullable=True),
            sa.Column("to_status", sa.String(16), nullable=True),
            sa.Column("idempotency_key", sa.String(128), nullable=False),
            sa.Column("payload_hash", sa.String(72), nullable=False),
            sa.Column("expected_version", sa.Integer(), nullable=True),
            sa.Column("actor", sa.String(128), nullable=False),
            sa.Column("request_id", sa.String(128), nullable=True),
            sa.Column("result", sa.String(16), nullable=False, server_default="succeeded"),
            sa.Column("error_code", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_purchase_request_action_workspace"),
            sa.ForeignKeyConstraint(["purchase_request_id"], ["purchase_requests.id"], name="fk_purchase_request_action_request"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("workspace_id", "purchase_request_id", "action_type", "idempotency_key", name="uq_purchase_request_action_idempotency"),
        )
        for name, column in (
            ("ix_purchase_request_actions_workspace_id", "workspace_id"),
            ("ix_purchase_request_actions_purchase_request_id", "purchase_request_id"),
            ("ix_purchase_request_actions_idempotency_key", "idempotency_key"),
        ):
            op.create_index(name, "purchase_request_actions", [column])
        return
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "purchase_requests" not in tables or "purchase_request_lines" not in tables:
        raise RuntimeError("0014 要求采购申请表存在，无法静默跳过")

    columns = {item["name"] for item in inspect(bind).get_columns("purchase_requests")}
    additions = (
        ("created_by", sa.Column("created_by", sa.String(128), nullable=True)),
        ("version", sa.Column("version", sa.Integer(), nullable=True)),
        ("supplier_ref", sa.Column("supplier_ref", sa.String(128), nullable=True)),
        ("expected_arrival_date", sa.Column("expected_arrival_date", sa.Date(), nullable=True)),
    )
    for name, column in additions:
        if name not in columns:
            with op.batch_alter_table("purchase_requests") as batch:
                batch.add_column(column)
    bind.execute(sa.text("UPDATE purchase_requests SET version = 1 WHERE version IS NULL"))
    with op.batch_alter_table("purchase_requests") as batch:
        batch.alter_column("version", existing_type=sa.Integer(), nullable=False, server_default="1")
        batch.alter_column("submitted_by", existing_type=sa.String(128), nullable=True)
        batch.alter_column("submitted_at", existing_type=sa.DateTime(), nullable=True)
        batch.alter_column("created_by", existing_type=sa.String(128), nullable=True)

    tables = set(inspect(bind).get_table_names())
    if "purchase_request_actions" not in tables:
        op.create_table(
            "purchase_request_actions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("workspace_id", sa.Integer(), nullable=False),
            sa.Column("purchase_request_id", sa.Integer(), nullable=False),
            sa.Column("action_type", sa.String(24), nullable=False),
            sa.Column("from_status", sa.String(16), nullable=True),
            sa.Column("to_status", sa.String(16), nullable=True),
            sa.Column("idempotency_key", sa.String(128), nullable=False),
            sa.Column("payload_hash", sa.String(72), nullable=False),
            sa.Column("expected_version", sa.Integer(), nullable=True),
            sa.Column("actor", sa.String(128), nullable=False),
            sa.Column("request_id", sa.String(128), nullable=True),
            sa.Column("result", sa.String(16), nullable=False, server_default="succeeded"),
            sa.Column("error_code", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_purchase_request_action_workspace"),
            sa.ForeignKeyConstraint(["purchase_request_id"], ["purchase_requests.id"], name="fk_purchase_request_action_request"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("workspace_id", "purchase_request_id", "action_type", "idempotency_key", name="uq_purchase_request_action_idempotency"),
        )
        for name, column in (
            ("ix_purchase_request_actions_workspace_id", "workspace_id"),
            ("ix_purchase_request_actions_purchase_request_id", "purchase_request_id"),
            ("ix_purchase_request_actions_idempotency_key", "idempotency_key"),
        ):
            op.create_index(name, "purchase_request_actions", [column])


def downgrade() -> None:
    bind = op.get_bind()
    if "purchase_request_actions" in set(inspect(bind).get_table_names()):
        for name in (
            "ix_purchase_request_actions_idempotency_key",
            "ix_purchase_request_actions_purchase_request_id",
            "ix_purchase_request_actions_workspace_id",
        ):
            if name in {item["name"] for item in inspect(bind).get_indexes("purchase_request_actions")}:
                op.drop_index(name, table_name="purchase_request_actions")
        op.drop_table("purchase_request_actions")
    if "purchase_requests" in set(inspect(bind).get_table_names()):
        columns = {item["name"] for item in inspect(bind).get_columns("purchase_requests")}
        with op.batch_alter_table("purchase_requests") as batch:
            for name in ("expected_arrival_date", "supplier_ref", "version", "created_by"):
                if name in columns:
                    batch.drop_column(name)
            if "submitted_by" in columns:
                batch.alter_column("submitted_by", existing_type=sa.String(128), nullable=False)
            if "submitted_at" in columns:
                batch.alter_column("submitted_at", existing_type=sa.DateTime(), nullable=False)
