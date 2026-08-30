"""baseline schema (全部表)

包含当前 model 的全部 7 张表：products / product_price_history /
crawl_jobs / documents / document_versions / document_chunks /
document_product_links。本迁移作为 baseline，后续 schema 改动通过
`alembic revision --autogenerate` 生成新版本。

使用方式
--------

**全新 SQLite / MySQL：**

```bash
alembic upgrade head   # 创建全部表
```

**已有 MySQL 且包含 week1 的 products / product_price_history / crawl_jobs：**

```bash
# 不跑 upgrade，仅标记当前状态为已迁移
alembic stamp head
# 后续每次改 model
alembic revision --autogenerate -m "..."
alembic upgrade head
```

**新加 S2/S3/S4/S5 表的 MySQL：**

```bash
alembic upgrade head   # 会跳过已存在的 week1 表，仅创建新表
```
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("external_product_id", sa.String(length=128), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("url", sa.String(length=1024), nullable=True),
        sa.Column("category", sa.String(length=128), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("rating", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("current_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "external_product_id", name="uq_product_source_external_id"),
    )
    op.create_index("ix_products_source", "products", ["source"])
    op.create_index("ix_products_external_product_id", "products", ["external_product_id"])
    op.create_index("ix_products_title", "products", ["title"])
    op.create_index("ix_products_category", "products", ["category"])
    op.create_index("ix_products_last_seen_at", "products", ["last_seen_at"])

    op.create_table(
        "product_price_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=True),
        sa.Column("source_run_id", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name="fk_price_history_product"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_product_price_history_product_id", "product_price_history", ["product_id"])
    op.create_index("ix_product_price_history_observed_at", "product_price_history", ["observed_at"])

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
    op.create_index("ix_crawl_jobs_type", "crawl_jobs", ["type"])
    op.create_index("ix_crawl_jobs_status", "crawl_jobs", ["status"])
    op.create_index("ix_crawl_jobs_worker_id", "crawl_jobs", ["worker_id"])
    op.create_index("ix_crawl_jobs_cancel_requested", "crawl_jobs", ["cancel_requested"])

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
    op.create_index("ix_documents_source_type", "documents", ["source_type"])
    op.create_index("ix_documents_sha256", "documents", ["sha256"])

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
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], name="fk_doc_version_document"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "version_no", name="uq_doc_version_no"),
        sa.UniqueConstraint("document_id", "sha256", name="uq_doc_sha256"),
    )
    op.create_index("ix_document_versions_document_id", "document_versions", ["document_id"])
    op.create_index("ix_document_versions_status", "document_versions", ["status"])
    op.create_index("ix_document_versions_published_at", "document_versions", ["published_at"])

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
    op.create_index("ix_document_chunks_document_version_id", "document_chunks", ["document_version_id"])

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
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name="fk_doc_link_product"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_version_id", "product_id", "relation", name="uq_doc_product_link"
        ),
    )
    op.create_index("ix_document_product_links_document_version_id", "document_product_links", ["document_version_id"])
    op.create_index("ix_document_product_links_product_id", "document_product_links", ["product_id"])


def downgrade() -> None:
    op.drop_index("ix_document_product_links_product_id", table_name="document_product_links")
    op.drop_index("ix_document_product_links_document_version_id", table_name="document_product_links")
    op.drop_table("document_product_links")
    op.drop_index("ix_document_chunks_document_version_id", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index("ix_document_versions_published_at", table_name="document_versions")
    op.drop_index("ix_document_versions_status", table_name="document_versions")
    op.drop_index("ix_document_versions_document_id", table_name="document_versions")
    op.drop_table("document_versions")
    op.drop_index("ix_documents_sha256", table_name="documents")
    op.drop_index("ix_documents_source_type", table_name="documents")
    op.drop_table("documents")
    op.drop_index("ix_crawl_jobs_cancel_requested", table_name="crawl_jobs")
    op.drop_index("ix_crawl_jobs_worker_id", table_name="crawl_jobs")
    op.drop_index("ix_crawl_jobs_status", table_name="crawl_jobs")
    op.drop_index("ix_crawl_jobs_type", table_name="crawl_jobs")
    op.drop_table("crawl_jobs")
    op.drop_index("ix_product_price_history_observed_at", table_name="product_price_history")
    op.drop_index("ix_product_price_history_product_id", table_name="product_price_history")
    op.drop_table("product_price_history")
    op.drop_index("ix_products_last_seen_at", table_name="products")
    op.drop_index("ix_products_category", table_name="products")
    op.drop_index("ix_products_title", table_name="products")
    op.drop_index("ix_products_external_product_id", table_name="products")
    op.drop_index("ix_products_source", table_name="products")
    op.drop_table("products")