"""Add product media assets and AIGC content production workflow."""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0016_content_production"
down_revision: Union[str, Sequence[str], None] = "0015_replenishment_evaluations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_tables() -> None:
    op.create_table(
        "product_content_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("sku_id", sa.Integer(), nullable=True),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="generating"),
        sa.Column("channel", sa.String(32), nullable=False, server_default="generic"),
        sa.Column("content_json", sa.Text(), nullable=True),
        sa.Column("source_snapshot_json", sa.Text(), nullable=False),
        sa.Column("source_snapshot_hash", sa.String(72), nullable=False),
        sa.Column("content_hash", sa.String(72), nullable=True),
        sa.Column("provider", sa.String(64), nullable=False, server_default="mock"),
        sa.Column("provider_model", sa.String(128), nullable=True),
        sa.Column("provider_request_id", sa.String(128), nullable=True),
        sa.Column("source_mode", sa.String(16), nullable=False, server_default="mock"),
        sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("prompt_snapshot", sa.Text(), nullable=True),
        sa.Column("quality_status", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("quality_issues_json", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("reviewed_by", sa.String(128), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("payload_hash", sa.String(72), nullable=False),
        sa.Column("generation_job_id", sa.Integer(), nullable=True),
        sa.Column("parent_revision_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_content_revision_workspace"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name="fk_content_revision_product"),
        sa.ForeignKeyConstraint(["sku_id"], ["product_skus.id"], name="fk_content_revision_sku"),
        sa.ForeignKeyConstraint(["generation_job_id"], ["crawl_jobs.id"], name="fk_content_revision_job"),
        sa.ForeignKeyConstraint(["parent_revision_id"], ["product_content_revisions.id"], name="fk_content_revision_parent"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "product_id", "revision_no", name="uq_content_revision_scope_no"),
        sa.UniqueConstraint("workspace_id", "idempotency_key", name="uq_content_revision_workspace_key"),
    )
    op.create_index("ix_content_revision_workspace", "product_content_revisions", ["workspace_id"])
    op.create_index("ix_content_revision_product", "product_content_revisions", ["product_id"])
    op.create_index("ix_content_revision_sku", "product_content_revisions", ["sku_id"])
    op.create_index("ix_content_revision_status", "product_content_revisions", ["status"])
    op.create_index("ix_content_revision_source_hash", "product_content_revisions", ["source_snapshot_hash"])
    op.create_index("ix_content_revision_content_hash", "product_content_revisions", ["content_hash"])
    op.create_index("ix_content_revision_generation_job", "product_content_revisions", ["generation_job_id"])

    op.create_table(
        "product_content_actions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("revision_id", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.String(32), nullable=False),
        sa.Column("from_status", sa.String(24), nullable=True),
        sa.Column("to_status", sa.String(24), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("expected_version", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("payload_hash", sa.String(72), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_content_action_workspace"),
        sa.ForeignKeyConstraint(["revision_id"], ["product_content_revisions.id"], name="fk_content_action_revision"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "revision_id", "action_type", "idempotency_key", name="uq_content_action_key"),
    )
    op.create_index("ix_content_action_workspace", "product_content_actions", ["workspace_id"])
    op.create_index("ix_content_action_revision", "product_content_actions", ["revision_id"])
    op.create_index("ix_content_action_idempotency", "product_content_actions", ["idempotency_key"])

    op.create_table(
        "product_content_exports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("revision_id", sa.Integer(), nullable=False),
        sa.Column("format", sa.String(16), nullable=False, server_default="json"),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("artifact_uri", sa.String(512), nullable=True),
        sa.Column("artifact_sha256", sa.String(72), nullable=True),
        sa.Column("artifact_size", sa.Integer(), nullable=True),
        sa.Column("manifest_json", sa.Text(), nullable=True),
        sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("payload_hash", sa.String(72), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_content_export_workspace"),
        sa.ForeignKeyConstraint(["revision_id"], ["product_content_revisions.id"], name="fk_content_export_revision"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "idempotency_key", name="uq_content_export_workspace_key"),
    )
    op.create_index("ix_content_export_workspace", "product_content_exports", ["workspace_id"])
    op.create_index("ix_content_export_revision", "product_content_exports", ["revision_id"])
    op.create_index("ix_content_export_status", "product_content_exports", ["status"])
    op.create_index("ix_content_export_idempotency", "product_content_exports", ["idempotency_key"])

    op.create_table(
        "product_media_assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("revision_id", sa.Integer(), nullable=True),
        sa.Column("role", sa.String(32), nullable=False, server_default="source"),
        sa.Column("storage_uri", sa.String(512), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("provider", sa.String(64), nullable=False, server_default="upload"),
        sa.Column("provider_model", sa.String(128), nullable=True),
        sa.Column("provider_request_id", sa.String(128), nullable=True),
        sa.Column("source_mode", sa.String(16), nullable=False, server_default="upload"),
        sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("prompt_snapshot", sa.Text(), nullable=True),
        sa.Column("params_json", sa.Text(), nullable=True),
        sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_media_asset_workspace"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name="fk_media_asset_product"),
        sa.ForeignKeyConstraint(["revision_id"], ["product_content_revisions.id"], name="fk_media_asset_revision"),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, column in (
        ("ix_media_asset_workspace", "workspace_id"),
        ("ix_media_asset_product", "product_id"),
        ("ix_media_asset_revision", "revision_id"),
        ("ix_media_asset_role", "role"),
        ("ix_media_asset_sha256", "content_sha256"),
        ("ix_media_asset_selected", "selected"),
    ):
        op.create_index(name, "product_media_assets", [column])


def upgrade() -> None:
    bind = op.get_bind()
    if context.is_offline_mode():
        op.add_column("crawl_jobs", sa.Column("idempotency_key", sa.String(128), nullable=True))
        op.add_column("crawl_jobs", sa.Column("payload_hash", sa.String(72), nullable=True))
        op.create_index("ix_crawl_jobs_idempotency_key", "crawl_jobs", ["idempotency_key"])
        op.create_index("ix_crawl_jobs_payload_hash", "crawl_jobs", ["payload_hash"])
        op.create_index("uq_crawl_job_workspace_type_key", "crawl_jobs", ["workspace_id", "type", "idempotency_key"], unique=True)
        _create_tables()
        return

    tables = set(inspect(bind).get_table_names())
    if "crawl_jobs" not in tables:
        raise RuntimeError("0016 要求 crawl_jobs 表存在")
    columns = {item["name"] for item in inspect(bind).get_columns("crawl_jobs")}
    with op.batch_alter_table("crawl_jobs") as batch:
        if "idempotency_key" not in columns:
            batch.add_column(sa.Column("idempotency_key", sa.String(128), nullable=True))
        if "payload_hash" not in columns:
            batch.add_column(sa.Column("payload_hash", sa.String(72), nullable=True))
    indexes = {item["name"] for item in inspect(bind).get_indexes("crawl_jobs")}
    if "ix_crawl_jobs_idempotency_key" not in indexes:
        op.create_index("ix_crawl_jobs_idempotency_key", "crawl_jobs", ["idempotency_key"])
    if "ix_crawl_jobs_payload_hash" not in indexes:
        op.create_index("ix_crawl_jobs_payload_hash", "crawl_jobs", ["payload_hash"])
    if "uq_crawl_job_workspace_type_key" not in indexes:
        op.create_index("uq_crawl_job_workspace_type_key", "crawl_jobs", ["workspace_id", "type", "idempotency_key"], unique=True)

    for table in ("product_content_revisions", "product_content_actions", "product_content_exports", "product_media_assets"):
        if table in tables:
            raise RuntimeError(f"0016 目标表已存在但迁移版本缺失: {table}")
    _create_tables()


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    for table, indexes in (
        ("product_media_assets", ("ix_media_asset_selected", "ix_media_asset_sha256", "ix_media_asset_role", "ix_media_asset_revision", "ix_media_asset_product", "ix_media_asset_workspace")),
        ("product_content_exports", ("ix_content_export_idempotency", "ix_content_export_status", "ix_content_export_revision", "ix_content_export_workspace")),
        ("product_content_actions", ("ix_content_action_idempotency", "ix_content_action_revision", "ix_content_action_workspace")),
        ("product_content_revisions", ("ix_content_revision_generation_job", "ix_content_revision_content_hash", "ix_content_revision_source_hash", "ix_content_revision_status", "ix_content_revision_sku", "ix_content_revision_product", "ix_content_revision_workspace")),
    ):
        if table in tables:
            current = {item["name"] for item in inspect(bind).get_indexes(table)}
            for index in indexes:
                if index in current:
                    op.drop_index(index, table_name=table)
            op.drop_table(table)
    if "crawl_jobs" in tables:
        current = {item["name"] for item in inspect(bind).get_indexes("crawl_jobs")}
        for index in ("uq_crawl_job_workspace_type_key", "ix_crawl_jobs_payload_hash", "ix_crawl_jobs_idempotency_key"):
            if index in current:
                op.drop_index(index, table_name="crawl_jobs")
        with op.batch_alter_table("crawl_jobs") as batch:
            columns = {item["name"] for item in inspect(bind).get_columns("crawl_jobs")}
            if "payload_hash" in columns:
                batch.drop_column("payload_hash")
            if "idempotency_key" in columns:
                batch.drop_column("idempotency_key")
