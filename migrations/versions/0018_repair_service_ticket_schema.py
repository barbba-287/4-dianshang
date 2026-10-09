"""Repair service-ticket columns and indexes omitted by a stamped legacy schema."""

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0018_repair_service_ticket_schema"
down_revision = "0017_service_tickets"
branch_labels = None
depends_on = None

_COLUMNS = {
    "issue_type": sa.Column(
        "issue_type", sa.String(length=32), nullable=False, server_default="other"
    ),
    "description": sa.Column("description", sa.Text(), nullable=True),
    "sku_ref": sa.Column("sku_ref", sa.String(length=255), nullable=True),
}

_INDEXES = {
    "ix_service_tickets_workspace_id": ["workspace_id"],
    "ix_service_tickets_ticket_no": ["ticket_no"],
    "ix_service_tickets_issue_type": ["issue_type"],
    "ix_service_tickets_status": ["status"],
    "ix_service_tickets_priority": ["priority"],
    "ix_service_tickets_external_order_ref": ["external_order_ref"],
    "ix_service_tickets_assigned_to": ["assigned_to"],
}


def upgrade() -> None:
    # This repair depends on reflection. Offline SQL must not guess which columns
    # or indexes exist in the target database.
    if context.is_offline_mode():
        return

    inspector = inspect(op.get_bind())
    if "service_tickets" not in set(inspector.get_table_names()):
        return

    columns = {column["name"] for column in inspector.get_columns("service_tickets")}
    for name, column in _COLUMNS.items():
        if name not in columns:
            op.add_column("service_tickets", column.copy())
            columns.add(name)

    existing_indexes = {
        index["name"] for index in inspector.get_indexes("service_tickets")
    }
    for name, indexed_columns in _INDEXES.items():
        if name not in existing_indexes and set(indexed_columns).issubset(columns):
            op.create_index(name, "service_tickets", indexed_columns)


def downgrade() -> None:
    # Keep repaired columns and indexes: removing them could discard data written
    # after the upgrade and would make this compatibility repair destructive.
    pass
