"""Create the minimal manual customer-service ticket tables."""
from alembic import op
import sqlalchemy as sa

revision = "0017_service_tickets"
down_revision = "0016_content_production"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("service_tickets", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False), sa.Column("ticket_no", sa.String(64), nullable=False), sa.Column("subject", sa.String(255), nullable=False), sa.Column("issue_type", sa.String(32), nullable=False, server_default="other"), sa.Column("description", sa.Text()), sa.Column("sku_ref", sa.String(255)), sa.Column("status", sa.String(24), nullable=False, server_default="open"), sa.Column("priority", sa.String(16), nullable=False, server_default="normal"), sa.Column("channel", sa.String(32), nullable=False, server_default="manual"), sa.Column("customer_ref", sa.String(255)), sa.Column("external_order_ref", sa.String(255)), sa.Column("source_mode", sa.String(16), nullable=False, server_default="manual"), sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("created_by", sa.String(128), nullable=False), sa.Column("assigned_to", sa.String(128)), sa.Column("version", sa.Integer(), nullable=False, server_default="1"), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False), sa.Column("closed_at", sa.DateTime()), sa.UniqueConstraint("workspace_id", "ticket_no", name="uq_service_ticket_workspace_no"))
    op.create_index("ix_service_tickets_workspace_id", "service_tickets", ["workspace_id"])
    op.create_index("ix_service_tickets_status", "service_tickets", ["status"])
    op.create_index("ix_service_tickets_priority", "service_tickets", ["priority"])
    op.create_index("ix_service_tickets_assigned_to", "service_tickets", ["assigned_to"])
    op.create_table("ticket_events", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False), sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("service_tickets.id"), nullable=False), sa.Column("event_type", sa.String(32), nullable=False), sa.Column("body", sa.Text(), nullable=False), sa.Column("author", sa.String(128), nullable=False), sa.Column("visibility", sa.String(16), nullable=False, server_default="internal"), sa.Column("source_mode", sa.String(16), nullable=False, server_default="manual"), sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("idempotency_key", sa.String(128)), sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_index("ix_ticket_events_workspace_id", "ticket_events", ["workspace_id"])
    op.create_index("ix_ticket_events_ticket_id", "ticket_events", ["ticket_id"])
    op.create_table("ticket_assignments", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False), sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("service_tickets.id"), nullable=False), sa.Column("assignee", sa.String(128), nullable=False), sa.Column("actor", sa.String(128), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("ended_at", sa.DateTime()))
    op.create_index("ix_ticket_assignments_workspace_id", "ticket_assignments", ["workspace_id"])
    op.create_index("ix_ticket_assignments_ticket_id", "ticket_assignments", ["ticket_id"])
    op.create_table("ticket_links", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False), sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("service_tickets.id"), nullable=False), sa.Column("entity_type", sa.String(32), nullable=False), sa.Column("entity_id", sa.String(255), nullable=False), sa.Column("relation", sa.String(32), nullable=False, server_default="related"), sa.UniqueConstraint("workspace_id", "ticket_id", "entity_type", "entity_id", name="uq_ticket_link_identity"))
    op.create_index("ix_ticket_links_workspace_id", "ticket_links", ["workspace_id"])
    op.create_index("ix_ticket_links_ticket_id", "ticket_links", ["ticket_id"])


def downgrade():
    op.drop_table("ticket_links")
    op.drop_table("ticket_assignments")
    op.drop_table("ticket_events")
    op.drop_table("service_tickets")
