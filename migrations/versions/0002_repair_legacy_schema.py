"""补齐被错误标记为 baseline 的旧版数据库结构。

0001 是全量 baseline。旧版数据库如果先被 ``stamp head``，只会写入
alembic_version，不会获得 S1 新增字段。本迁移对这类数据库做条件式修复；
已经完整执行 0001 的新库则不产生任何结构变化。
"""

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "0002_repair_legacy_schema"
down_revision: Union[str, Sequence[str], None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_S1_COLUMNS = {
    "type": sa.Column(
        "type", sa.String(length=32), nullable=False, server_default="crawl_fixture"
    ),
    "max_retries": sa.Column(
        "max_retries", sa.Integer(), nullable=False, server_default="2"
    ),
    "attempt": sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
    "worker_id": sa.Column("worker_id", sa.String(length=64), nullable=True),
    "lease_until": sa.Column("lease_until", sa.DateTime(), nullable=True),
    "next_run_at": sa.Column("next_run_at", sa.DateTime(), nullable=True),
    "run_id": sa.Column("run_id", sa.String(length=32), nullable=True),
    "cancel_requested": sa.Column(
        "cancel_requested", sa.Boolean(), nullable=False, server_default=sa.text("0")
    ),
}


_CRAWL_COLUMNS = {
    # The first ten columns are present in the original week1 table. They are
    # included as a defensive repair for partially-created legacy databases.
    "source": sa.Column("source", sa.String(length=64), nullable=True),
    "keyword": sa.Column("keyword", sa.String(length=255), nullable=True),
    "status": sa.Column("status", sa.String(length=32), nullable=True),
    "cursor": sa.Column("cursor", sa.String(length=255), nullable=True),
    "retry_count": sa.Column("retry_count", sa.Integer(), nullable=True),
    "started_at": sa.Column("started_at", sa.DateTime(), nullable=True),
    "finished_at": sa.Column("finished_at", sa.DateTime(), nullable=True),
    "error_code": sa.Column("error_code", sa.String(length=64), nullable=True),
    "error_message": sa.Column("error_message", sa.Text(), nullable=True),
    **_S1_COLUMNS,
}


_TABLE_COLUMNS = {
    "products": {
        "source": sa.Column("source", sa.String(64), nullable=True),
        "external_product_id": sa.Column("external_product_id", sa.String(128), nullable=True),
        "title": sa.Column("title", sa.String(255), nullable=True),
        "url": sa.Column("url", sa.String(1024), nullable=True),
        "category": sa.Column("category", sa.String(128), nullable=True),
        "description": sa.Column("description", sa.Text(), nullable=True),
        "rating": sa.Column("rating", sa.Numeric(3, 2), nullable=True),
        "current_price": sa.Column("current_price", sa.Numeric(12, 2), nullable=True),
        "currency": sa.Column("currency", sa.String(8), nullable=True),
        "first_seen_at": sa.Column("first_seen_at", sa.DateTime(), nullable=True),
        "last_seen_at": sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        "updated_at": sa.Column("updated_at", sa.DateTime(), nullable=True),
    },
    "product_price_history": {
        "product_id": sa.Column("product_id", sa.Integer(), nullable=True),
        "price": sa.Column("price", sa.Numeric(12, 2), nullable=True),
        "currency": sa.Column("currency", sa.String(8), nullable=True),
        "observed_at": sa.Column("observed_at", sa.DateTime(), nullable=True),
        "source_run_id": sa.Column("source_run_id", sa.String(64), nullable=True),
    },
    "crawl_jobs": _CRAWL_COLUMNS,
    "documents": {
        "source_type": sa.Column("source_type", sa.String(16), nullable=True),
        "title": sa.Column("title", sa.String(255), nullable=True),
        "filename": sa.Column("filename", sa.String(512), nullable=True),
        "sha256": sa.Column("sha256", sa.String(64), nullable=True),
        "created_at": sa.Column("created_at", sa.DateTime(), nullable=True),
    },
    "document_versions": {
        "document_id": sa.Column("document_id", sa.Integer(), nullable=True),
        "version_no": sa.Column("version_no", sa.Integer(), nullable=True),
        "sha256": sa.Column("sha256", sa.String(64), nullable=True),
        "size_bytes": sa.Column("size_bytes", sa.Integer(), nullable=True),
        "storage_uri": sa.Column("storage_uri", sa.String(512), nullable=True),
        "status": sa.Column("status", sa.String(16), nullable=True),
        "parser_version": sa.Column("parser_version", sa.String(32), nullable=True),
        "error_code": sa.Column("error_code", sa.String(64), nullable=True),
        "error_message": sa.Column("error_message", sa.Text(), nullable=True),
        "created_at": sa.Column("created_at", sa.DateTime(), nullable=True),
        "published_at": sa.Column("published_at", sa.DateTime(), nullable=True),
    },
    "document_chunks": {
        "document_version_id": sa.Column("document_version_id", sa.Integer(), nullable=True),
        "chunk_no": sa.Column("chunk_no", sa.Integer(), nullable=True),
        "text": sa.Column("text", sa.Text(), nullable=True),
        "page_no": sa.Column("page_no", sa.Integer(), nullable=True),
        "paragraph_no": sa.Column("paragraph_no", sa.Integer(), nullable=True),
        "token_count": sa.Column("token_count", sa.Integer(), nullable=True),
        "content_hash": sa.Column("content_hash", sa.String(64), nullable=True),
    },
    "document_product_links": {
        "document_version_id": sa.Column("document_version_id", sa.Integer(), nullable=True),
        "product_id": sa.Column("product_id", sa.Integer(), nullable=True),
        "relation": sa.Column("relation", sa.String(16), nullable=True),
        "created_at": sa.Column("created_at", sa.DateTime(), nullable=True),
    },
}


def _inspector():
    if context.is_offline_mode():
        return None
    return inspect(op.get_bind())


def _create_missing_tables() -> None:
    """为极旧的 week1 库创建不存在的后续业务表。

    典型 week1 库已有 products、product_price_history、crawl_jobs；如果只
    有其中一部分，也按外键依赖顺序补齐其余 baseline 表。已有表不会重建。
    """
    inspector = _inspector()
    tables = set(inspector.get_table_names())

    if "products" not in tables:
        op.create_table(
            "products",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=True),
            sa.Column("external_product_id", sa.String(length=128), nullable=True),
            sa.Column("title", sa.String(length=255), nullable=True),
            sa.Column("url", sa.String(length=1024), nullable=True),
            sa.Column("category", sa.String(length=128), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("rating", sa.Numeric(3, 2), nullable=True),
            sa.Column("current_price", sa.Numeric(12, 2), nullable=True),
            sa.Column("currency", sa.String(length=8), nullable=True),
            sa.Column("first_seen_at", sa.DateTime(), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source", "external_product_id", name="uq_product_source_external_id"
            ),
        )
        tables.add("products")

    if "product_price_history" not in tables:
        op.create_table(
            "product_price_history",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("product_id", sa.Integer(), nullable=True),
            sa.Column("price", sa.Numeric(12, 2), nullable=True),
            sa.Column("currency", sa.String(length=8), nullable=True),
            sa.Column("observed_at", sa.DateTime(), nullable=True),
            sa.Column("source_run_id", sa.String(length=64), nullable=True),
            sa.ForeignKeyConstraint(["product_id"], ["products.id"], name="fk_price_history_product"),
            sa.PrimaryKeyConstraint("id"),
        )
        tables.add("product_price_history")

    if "crawl_jobs" not in tables:
        op.create_table(
            "crawl_jobs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=True),
            sa.Column("keyword", sa.String(length=255), nullable=True),
            sa.Column("type", sa.String(length=32), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=True),
            sa.Column("cursor", sa.String(length=255), nullable=True),
            sa.Column("retry_count", sa.Integer(), nullable=True),
            sa.Column("max_retries", sa.Integer(), nullable=True),
            sa.Column("attempt", sa.Integer(), nullable=True),
            sa.Column("worker_id", sa.String(length=64), nullable=True),
            sa.Column("lease_until", sa.DateTime(), nullable=True),
            sa.Column("next_run_at", sa.DateTime(), nullable=True),
            sa.Column("run_id", sa.String(length=32), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("error_code", sa.String(length=64), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("cancel_requested", sa.Boolean(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        tables.add("crawl_jobs")

    if "documents" not in tables:
        op.create_table(
            "documents",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("source_type", sa.String(length=16), nullable=True),
            sa.Column("title", sa.String(length=255), nullable=True),
            sa.Column("filename", sa.String(length=512), nullable=True),
            sa.Column("sha256", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        tables.add("documents")

    if "document_versions" not in tables:
        op.create_table(
            "document_versions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("document_id", sa.Integer(), nullable=True),
            sa.Column("version_no", sa.Integer(), nullable=True),
            sa.Column("sha256", sa.String(length=64), nullable=True),
            sa.Column("size_bytes", sa.Integer(), nullable=True),
            sa.Column("storage_uri", sa.String(length=512), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=True),
            sa.Column("parser_version", sa.String(length=32), nullable=True),
            sa.Column("error_code", sa.String(length=64), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("published_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(
                ["document_id"], ["documents.id"], name="fk_doc_version_document"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("document_id", "version_no", name="uq_doc_version_no"),
            sa.UniqueConstraint("document_id", "sha256", name="uq_doc_sha256"),
        )
        tables.add("document_versions")

    if "document_chunks" not in tables:
        op.create_table(
            "document_chunks",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("document_version_id", sa.Integer(), nullable=True),
            sa.Column("chunk_no", sa.Integer(), nullable=True),
            sa.Column("text", sa.Text(), nullable=True),
            sa.Column("page_no", sa.Integer(), nullable=True),
            sa.Column("paragraph_no", sa.Integer(), nullable=True),
            sa.Column("token_count", sa.Integer(), nullable=True),
            sa.Column("content_hash", sa.String(length=64), nullable=True),
            sa.ForeignKeyConstraint(
                ["document_version_id"], ["document_versions.id"], name="fk_chunk_version"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("document_version_id", "chunk_no", name="uq_chunk_no"),
            sa.UniqueConstraint("document_version_id", "content_hash", name="uq_chunk_hash"),
        )
        tables.add("document_chunks")

    if "document_product_links" not in tables:
        op.create_table(
            "document_product_links",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("document_version_id", sa.Integer(), nullable=True),
            sa.Column("product_id", sa.Integer(), nullable=True),
            sa.Column("relation", sa.String(length=16), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(
                ["document_version_id"], ["document_versions.id"], name="fk_doc_link_version"
            ),
            sa.ForeignKeyConstraint(
                ["product_id"], ["products.id"], name="fk_doc_link_product"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "document_version_id", "product_id", "relation", name="uq_doc_product_link"
            ),
        )


def _add_missing_columns() -> None:
    inspector = _inspector()
    for table_name, definitions in _TABLE_COLUMNS.items():
        if table_name not in inspector.get_table_names():
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        for name, column in definitions.items():
            if name not in columns:
                op.add_column(table_name, column.copy())


def _create_missing_indexes() -> None:
    inspector = _inspector()
    index_specs = {
        "products": {
            "ix_products_source": ["source"],
            "ix_products_external_product_id": ["external_product_id"],
            "ix_products_title": ["title"],
            "ix_products_category": ["category"],
            "ix_products_last_seen_at": ["last_seen_at"],
        },
        "product_price_history": {
            "ix_product_price_history_product_id": ["product_id"],
            "ix_product_price_history_observed_at": ["observed_at"],
        },
        "crawl_jobs": {
            "ix_crawl_jobs_type": ["type"],
            "ix_crawl_jobs_status": ["status"],
            "ix_crawl_jobs_worker_id": ["worker_id"],
            "ix_crawl_jobs_cancel_requested": ["cancel_requested"],
        },
        "documents": {
            "ix_documents_source_type": ["source_type"],
            "ix_documents_sha256": ["sha256"],
        },
        "document_versions": {
            "ix_document_versions_document_id": ["document_id"],
            "ix_document_versions_status": ["status"],
            "ix_document_versions_published_at": ["published_at"],
        },
        "document_chunks": {
            "ix_document_chunks_document_version_id": ["document_version_id"],
        },
        "document_product_links": {
            "ix_document_product_links_document_version_id": ["document_version_id"],
            "ix_document_product_links_product_id": ["product_id"],
        },
    }
    tables = set(inspector.get_table_names())
    for table_name, specs in index_specs.items():
        if table_name not in tables:
            continue
        existing = {index["name"] for index in inspector.get_indexes(table_name)}
        existing_columns = {
            column["name"] for column in inspector.get_columns(table_name)
        }
        for index_name, columns in specs.items():
            # A partially-created legacy table may contain only its primary key.
            # Do not create an index until all referenced columns exist; a later
            # schema repair can add the index once the columns are present.
            if index_name not in existing and set(columns).issubset(existing_columns):
                op.create_index(index_name, table_name, columns)


def upgrade() -> None:
    if context.is_offline_mode():
        # Offline SQL is intended for a new, complete baseline; the legacy
        # state-aware repair requires reflection and is executed online.
        return
    _create_missing_tables()
    _add_missing_columns()
    _create_missing_indexes()


def downgrade() -> None:
    inspector = _inspector()
    indexes = {index["name"] for index in inspector.get_indexes("crawl_jobs")}
    for name in (
        "ix_crawl_jobs_cancel_requested",
        "ix_crawl_jobs_worker_id",
        "ix_crawl_jobs_type",
    ):
        if name in indexes:
            op.drop_index(name, table_name="crawl_jobs")

    columns = {column["name"] for column in _inspector().get_columns("crawl_jobs")}
    for name in reversed(tuple(_CRAWL_COLUMNS)):
        if name in columns:
            op.drop_column("crawl_jobs", name)
