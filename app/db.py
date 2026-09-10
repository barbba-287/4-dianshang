"""电商商品与客服工作台的数据模型和数据库连接模块。

用途：定义商品、价格历史和采集任务三类核心表，提供 SQLAlchemy 会话、
外键及唯一约束，支撑采集结果的幂等持久化和 API 查询。
"""

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.config import get_settings


settings = get_settings()
database_url = settings.database_url
connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (UniqueConstraint("tenant_key", name="uq_workspace_tenant_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_key: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class UserAccount(Base):
    __tablename__ = "user_accounts"
    __table_args__ = (UniqueConstraint("login", name="uq_user_account_login"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(128), index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class WorkspaceMembership(Base):
    __tablename__ = "workspace_memberships"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_membership"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    role: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class WarehouseAccess(Base):
    __tablename__ = "warehouse_access"
    __table_args__ = (
        UniqueConstraint("user_id", "warehouse_id", name="uq_warehouse_access"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), index=True)
    role_override: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_auth_session_token_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), index=True)
    csrf_token_hash: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    membership_id: Mapped[int] = mapped_column(ForeignKey("workspace_memberships.id"), index=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "source",
            "external_product_id",
            name="uq_product_workspace_source_external_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    source: Mapped[str] = mapped_column(String(64), default="fixture", index=True)
    external_product_id: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    url: Mapped[str] = mapped_column(String(1024))
    category: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), nullable=True)
    current_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ProductPriceHistory(Base):
    __tablename__ = "product_price_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    source_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class CrawlJob(Base):
    __tablename__ = "crawl_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    source: Mapped[str] = mapped_column(String(64), default="fixture")
    keyword: Mapped[str | None] = mapped_column(String(255), nullable=True)
    type: Mapped[str] = mapped_column(String(32), default="crawl_fixture", index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    cursor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(default=0)
    max_retries: Mapped[int] = mapped_column(default=2)
    attempt: Mapped[int] = mapped_column(default=0)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


# ---------- S2 文档导入 ----------


DOCUMENT_SOURCE_TYPES = ("pdf", "docx")
DOCUMENT_STATUSES = ("parsing", "ready", "failed")
LINK_RELATIONS = ("mention", "spec")


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("workspace_id", "sha256", name="uq_document_workspace_sha256"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    source_type: Mapped[str] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(String(255))
    filename: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_no", name="uq_doc_version_no"),
        UniqueConstraint("document_id", "sha256", name="uq_doc_sha256"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_uri: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), default="parsing", index=True)
    parser_version: Mapped[str] = mapped_column(String(32), default="v1")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_version_id", "chunk_no", name="uq_chunk_no"),
        UniqueConstraint("document_version_id", "content_hash", name="uq_chunk_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    document_version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id"), index=True)
    chunk_no: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paragraph_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str] = mapped_column(String(64))


class DocumentProductLink(Base):
    __tablename__ = "document_product_links"
    __table_args__ = (
        UniqueConstraint(
            "document_version_id", "product_id", "relation", name="uq_doc_product_link"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    document_version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    relation: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProductSku(Base):
    __tablename__ = "product_skus"
    __table_args__ = (
        UniqueConstraint("workspace_id", "sku_code", name="uq_product_workspace_sku_code"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    sku_code: Mapped[str] = mapped_column(String(128), index=True)
    variant_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unit: Mapped[str] = mapped_column(String(32), default="件")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class Warehouse(Base):
    __tablename__ = "warehouses"
    __table_args__ = (
        UniqueConstraint("workspace_id", "code", name="uq_warehouse_workspace_code"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    warehouse_type: Mapped[str] = mapped_column(String(16), default="own")
    integration_mode: Mapped[str] = mapped_column(String(16), default="manual")
    external_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class InboundOrder(Base):
    __tablename__ = "inbound_orders"
    __table_args__ = (
        UniqueConstraint("workspace_id", "reference_no", name="uq_inbound_workspace_reference_no"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), index=True)
    reference_no: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), default="expected", index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    receive_idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    receive_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirm_idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    confirm_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class InboundLine(Base):
    __tablename__ = "inbound_lines"
    __table_args__ = (
        UniqueConstraint("inbound_order_id", "sku_id", name="uq_inbound_line_sku"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    inbound_order_id: Mapped[int] = mapped_column(
        ForeignKey("inbound_orders.id"), index=True
    )
    sku_id: Mapped[int] = mapped_column(ForeignKey("product_skus.id"), index=True)
    expected_qty: Mapped[int] = mapped_column(Integer)
    received_qty: Mapped[int] = mapped_column(Integer, default=0)
    damaged_qty: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class InventoryTransaction(Base):
    __tablename__ = "inventory_transactions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_inventory_transaction_workspace_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    inbound_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("inbound_orders.id"), nullable=True, index=True
    )
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), index=True)
    sku_id: Mapped[int] = mapped_column(ForeignKey("product_skus.id"), index=True)
    quantity_delta: Mapped[int] = mapped_column(Integer)
    movement_type: Mapped[str] = mapped_column(String(32), default="inbound")
    idempotency_key: Mapped[str] = mapped_column(String(160), index=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class ExternalSyncRun(Base):
    __tablename__ = "external_sync_runs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "run_id", name="uq_external_sync_run_workspace_id"),
        UniqueConstraint("workspace_id", "retry_of_run_id", "retry_idempotency_key", name="uq_external_sync_run_retry_idempotency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("workspaces.id"), nullable=True, index=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    account_ref: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    store_ref: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    sync_type: Mapped[str] = mapped_column(String(16), index=True)
    source_mode: Mapped[str] = mapped_column(String(16), default="mock")
    simulated: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    retry_of_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    resource_status_json: Mapped[str] = mapped_column(Text, default="{}")
    retryable_resources_json: Mapped[str] = mapped_column(Text, default="[]")
    retry_idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    retry_payload_hash: Mapped[str | None] = mapped_column(String(72), nullable=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    inserted: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    no_op: Mapped[int] = mapped_column(Integer, default=0)
    conflict: Mapped[int] = mapped_column(Integer, default=0)
    stale: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ExternalInventorySnapshot(Base):
    __tablename__ = "external_inventory_snapshots"
    __table_args__ = (
        UniqueConstraint("workspace_id", "platform", "account_ref", "store_ref_key", "warehouse_ref_key", "external_sku", "as_of", "payload_hash", name="uq_external_snapshot_workspace_identity"),
        UniqueConstraint("workspace_id", "platform", "account_ref", "store_ref_key", "warehouse_ref_key", "idempotency_key", name="uq_external_snapshot_workspace_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    platform: Mapped[str] = mapped_column(String(32), index=True)
    account_ref: Mapped[str] = mapped_column(String(128), index=True)
    store_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    store_ref_key: Mapped[str] = mapped_column(String(128), nullable=False, server_default="__default_store__", index=True)
    marketplace: Mapped[str | None] = mapped_column(String(64), nullable=True)
    warehouse_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    warehouse_ref_key: Mapped[str] = mapped_column(String(128), nullable=False, server_default="__default_warehouse__", index=True)
    external_sku: Mapped[str] = mapped_column(String(255), index=True)
    internal_sku_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    asin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    available_qty: Mapped[int] = mapped_column(Integer)
    reserved_qty: Mapped[int] = mapped_column(Integer, default=0)
    inbound_qty: Mapped[int] = mapped_column(Integer, default=0)
    as_of: Mapped[datetime] = mapped_column(DateTime, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    payload_hash: Mapped[str] = mapped_column(String(72), index=True)
    sync_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), index=True)
    raw_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_mode: Mapped[str] = mapped_column(String(16), default="mock")
    simulated: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(24), default="observed", index=True)


class ExternalEventInbox(Base):
    __tablename__ = "external_event_inbox"
    __table_args__ = (
        UniqueConstraint("workspace_id", "platform", "account_ref", "idempotency_key", name="uq_external_event_workspace_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    platform: Mapped[str] = mapped_column(String(32), index=True)
    account_ref: Mapped[str] = mapped_column(String(128), index=True)
    external_event_id: Mapped[str] = mapped_column(String(255), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    event_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_object_no: Mapped[str | None] = mapped_column(String(255), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    payload_hash: Mapped[str] = mapped_column(String(72), index=True)
    sync_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    raw_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_mode: Mapped[str] = mapped_column(String(16), default="mock")
    simulated: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(24), default="received", index=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReconciliationResult(Base):
    __tablename__ = "reconciliation_results"
    __table_args__ = (
        UniqueConstraint("workspace_id", "snapshot_id", "external_sku", "warehouse_id", name="uq_reconciliation_workspace_identity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("external_inventory_snapshots.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    external_sku: Mapped[str] = mapped_column(String(255), index=True)
    internal_sku_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sku_id: Mapped[int | None] = mapped_column(ForeignKey("product_skus.id"), nullable=True, index=True)
    warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"), nullable=True, index=True)
    external_available_qty: Mapped[int] = mapped_column(Integer)
    internal_on_hand_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delta: Mapped[int | None] = mapped_column(Integer, nullable=True)
    classification: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class InventoryPolicy(Base):
    __tablename__ = "inventory_policies"
    __table_args__ = (
        UniqueConstraint("workspace_id", "warehouse_id", "sku_id", name="uq_inventory_policy_workspace_warehouse_sku"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), index=True)
    sku_id: Mapped[int] = mapped_column(ForeignKey("product_skus.id"), index=True)
    safety_stock_qty: Mapped[int] = mapped_column(Integer, default=0)
    reorder_point_qty: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class InventoryAlert(Base):
    __tablename__ = "inventory_alerts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "dedupe_key", name="uq_inventory_alert_workspace_dedupe"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="warning", index=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"), nullable=True, index=True)
    sku_id: Mapped[int | None] = mapped_column(ForeignKey("product_skus.id"), nullable=True, index=True)
    platform: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class InventoryBalance(Base):
    __tablename__ = "inventory_balances"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "warehouse_id", "sku_id", name="uq_inventory_balance_workspace_warehouse_sku"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), index=True)
    sku_id: Mapped[int] = mapped_column(ForeignKey("product_skus.id"), index=True)
    on_hand_qty: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ExternalAccount(Base):
    __tablename__ = "external_accounts"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "platform", "account_ref", "store_ref",
            name="uq_external_account_workspace_identity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    account_ref: Mapped[str] = mapped_column(String(128), index=True)
    store_ref: Mapped[str] = mapped_column(String(128), default="default", index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    source_mode: Mapped[str] = mapped_column(String(16), default="mock")
    simulated: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ExternalProductMapping(Base):
    __tablename__ = "external_product_mappings"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "external_account_id", "external_sku",
            name="uq_external_mapping_workspace_account_sku",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    external_account_id: Mapped[int] = mapped_column(ForeignKey("external_accounts.id"), index=True)
    external_sku: Mapped[str] = mapped_column(String(255), index=True)
    internal_sku_id: Mapped[int | None] = mapped_column(ForeignKey("product_skus.id"), nullable=True, index=True)
    mapping_status: Mapped[str] = mapped_column(String(16), default="unmapped", index=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ExternalOrder(Base):
    __tablename__ = "external_orders"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "external_account_id", "external_order_no",
            name="uq_external_order_workspace_account_no",
        ),
        UniqueConstraint(
            "workspace_id", "external_account_id", "idempotency_key",
            name="uq_external_order_workspace_account_key",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    external_account_id: Mapped[int] = mapped_column(ForeignKey("external_accounts.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    account_ref: Mapped[str] = mapped_column(String(128), index=True)
    store_ref: Mapped[str] = mapped_column(String(128), default="default", index=True)
    external_order_no: Mapped[str] = mapped_column(String(255), index=True)
    order_status: Mapped[str] = mapped_column(String(32), index=True)
    external_created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    external_updated_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    event_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    refund_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    payload_json: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(String(72), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), index=True)
    source_mode: Mapped[str] = mapped_column(String(16), default="mock")
    simulated: Mapped[bool] = mapped_column(Boolean, default=True)
    sync_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    data_completeness: Mapped[str] = mapped_column(String(16), default="complete", index=True)
    status_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ExternalOrderLine(Base):
    __tablename__ = "external_order_lines"
    __table_args__ = (
        UniqueConstraint("workspace_id", "external_order_id", "external_line_id", name="uq_external_order_line_identity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    external_order_id: Mapped[int] = mapped_column(ForeignKey("external_orders.id"), index=True)
    external_line_id: Mapped[str] = mapped_column(String(128))
    external_sku: Mapped[str] = mapped_column(String(255), index=True)
    internal_sku_id: Mapped[int | None] = mapped_column(ForeignKey("product_skus.id"), nullable=True, index=True)
    ordered_qty: Mapped[int] = mapped_column(Integer)
    cancelled_qty: Mapped[int] = mapped_column(Integer, default=0)
    refunded_qty: Mapped[int] = mapped_column(Integer, default=0)
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    refund_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    mapping_status: Mapped[str] = mapped_column(String(16), default="unmapped", index=True)
    data_completeness: Mapped[str] = mapped_column(String(16), default="complete")
    payload_hash: Mapped[str] = mapped_column(String(72))


class DailySkuSale(Base):
    __tablename__ = "daily_sku_sales"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "external_account_id", "external_sku", "sales_date",
            name="uq_daily_sku_sale_workspace_account_sku_date",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    external_account_id: Mapped[int] = mapped_column(ForeignKey("external_accounts.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    account_ref: Mapped[str] = mapped_column(String(128), index=True)
    store_ref: Mapped[str] = mapped_column(String(128), default="default", index=True)
    external_sku: Mapped[str] = mapped_column(String(255), index=True)
    internal_sku_id: Mapped[int | None] = mapped_column(ForeignKey("product_skus.id"), nullable=True, index=True)
    sales_date: Mapped[date] = mapped_column(Date, index=True)
    gross_qty: Mapped[int] = mapped_column(Integer, default=0)
    cancelled_qty: Mapped[int] = mapped_column(Integer, default=0)
    refunded_qty: Mapped[int] = mapped_column(Integer, default=0)
    net_qty: Mapped[int] = mapped_column(Integer, default=0)
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    refund_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    net_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    order_count: Mapped[int] = mapped_column(Integer, default=0)
    data_completeness: Mapped[str] = mapped_column(String(16), default="complete", index=True)
    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class ReplenishmentSuggestion(Base):
    __tablename__ = "replenishment_suggestions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "warehouse_id", "sku_id", "source_hash", name="uq_replenishment_suggestion_source"),
        UniqueConstraint("workspace_id", "warehouse_id", "sku_id", "active_slot", name="uq_replenishment_suggestion_active_slot"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False, index=True)
    sku_id: Mapped[int] = mapped_column(ForeignKey("product_skus.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default="suggested", index=True)
    suggested_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decision_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    submitted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    formula_version: Mapped[str] = mapped_column(String(32), default="replenishment.v1")
    coverage_days: Mapped[int] = mapped_column(Integer, default=14)
    daily_avg_qty: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
    on_hand_qty: Mapped[int] = mapped_column(Integer, default=0)
    safety_stock_qty: Mapped[int] = mapped_column(Integer, default=0)
    reorder_point_qty: Mapped[int] = mapped_column(Integer, default=0)
    sales_window_days: Mapped[int] = mapped_column(Integer, default=14)
    sales_qty: Mapped[int] = mapped_column(Integer, default=0)
    effective_sale_days: Mapped[int] = mapped_column(Integer, default=0)
    data_completeness: Mapped[str] = mapped_column(String(16), default="insufficient", index=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_hash: Mapped[str] = mapped_column(String(72), index=True)
    source_snapshot_json: Mapped[str] = mapped_column(Text)
    as_of_date: Mapped[date] = mapped_column(Date, index=True)
    active_slot: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ReplenishmentSuggestionAction(Base):
    __tablename__ = "replenishment_suggestion_actions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "suggestion_id", "idempotency_key", name="uq_replenishment_action_idempotency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    suggestion_id: Mapped[int] = mapped_column(ForeignKey("replenishment_suggestions.id"), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(24), default="decision")
    idempotency_key: Mapped[str] = mapped_column(String(128), index=True)
    payload_hash: Mapped[str] = mapped_column(String(72))
    from_status: Mapped[str] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    decision_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(128))
    expected_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PurchaseRequest(Base):
    __tablename__ = "purchase_requests"
    __table_args__ = (
        UniqueConstraint("workspace_id", "request_no", name="uq_purchase_request_workspace_no"),
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_purchase_request_workspace_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False, index=True)
    request_no: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="submitted", index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    supplier_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expected_arrival_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), index=True)
    payload_hash: Mapped[str] = mapped_column(String(72))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PurchaseRequestAction(Base):
    __tablename__ = "purchase_request_actions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "purchase_request_id", "action_type", "idempotency_key", name="uq_purchase_request_action_idempotency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    purchase_request_id: Mapped[int] = mapped_column(ForeignKey("purchase_requests.id"), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(24))
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), index=True)
    payload_hash: Mapped[str] = mapped_column(String(72))
    expected_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor: Mapped[str] = mapped_column(String(128))
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    result: Mapped[str] = mapped_column(String(16), default="succeeded")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PurchaseRequestLine(Base):
    __tablename__ = "purchase_request_lines"
    __table_args__ = (
        UniqueConstraint("workspace_id", "purchase_request_id", "sku_id", name="uq_purchase_request_line_sku"),
        UniqueConstraint("workspace_id", "suggestion_id", name="uq_purchase_request_line_suggestion"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    purchase_request_id: Mapped[int] = mapped_column(ForeignKey("purchase_requests.id"), nullable=False, index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False, index=True)
    sku_id: Mapped[int] = mapped_column(ForeignKey("product_skus.id"), nullable=False, index=True)
    suggestion_id: Mapped[int] = mapped_column(ForeignKey("replenishment_suggestions.id"), nullable=False, index=True)
    requested_qty: Mapped[int] = mapped_column(Integer)
    source_suggestion_version: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)



def backfill_legacy_workspace(db, workspace_id: int) -> int:
    """将单工作空间兼容库中的未归属旧数据绑定到该空间。

    旧版本没有 workspace_id；只有在数据库恰好只有一个活动 Workspace，且
    目标就是这个 Workspace 时才允许执行，避免把无法判定归属的数据静默
    分配给错误商家。
    """
    active_workspaces = db.query(Workspace).filter(
        Workspace.status == "active"
    ).order_by(Workspace.id).all()
    if len(active_workspaces) != 1 or active_workspaces[0].id != workspace_id:
        raise ValueError("LEGACY_WORKSPACE_TARGET_NOT_UNIQUE")

    models = (
        Product,
        ProductPriceHistory,
        CrawlJob,
        Document,
        DocumentVersion,
        DocumentChunk,
        DocumentProductLink,
        ProductSku,
        InboundOrder,
        InboundLine,
        InventoryTransaction,
        InventoryBalance,
        ExternalInventorySnapshot,
        ExternalEventInbox,
    ExternalSyncRun,
        ReconciliationResult,
        InventoryPolicy,
        InventoryAlert,
        ExternalAccount,
        ExternalProductMapping,
        ExternalOrder,
        ExternalOrderLine,
        DailySkuSale,
        ReplenishmentSuggestion,
        ReplenishmentSuggestionAction,
        PurchaseRequest,
        PurchaseRequestLine,
    )
    changed = 0
    for model in models:
        changed += int(
            db.query(model)
            .filter(model.workspace_id.is_(None))
            .update({model.workspace_id: workspace_id}, synchronize_session=False)
        )
    if changed:
        db.commit()
    return changed


def init_db() -> None:
    """启动时确保数据库完成 Alembic 升级。

    测试和一次性脚本可能先用 ``Base.metadata.create_all`` 建立当前模型；
    这种没有 alembic_version 但已是完整新 schema 的库只需 stamp。真正的
    legacy 库交给 state-aware 初始化器处理，避免静默跳过 repair migration。
    """
    settings.resolve_path("data").mkdir(parents=True, exist_ok=True)
    settings.resolve_path(settings.artifacts_dir).mkdir(parents=True, exist_ok=True)

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "alembic_version" not in tables:
        if not tables.intersection({"products", "crawl_jobs"}):
            from alembic import command
            from alembic.config import Config

            cfg = Config(str(settings.project_root / "alembic.ini"))
            cfg.set_main_option("sqlalchemy.url", settings.database_url)
            command.upgrade(cfg, "head")
            return
        product_columns = {column["name"] for column in inspector.get_columns("products")}
        required_current = {"workspace_id", "source", "external_product_id"}
        required_tables = {
            "products", "product_price_history", "crawl_jobs", "documents",
            "document_versions", "document_chunks", "document_product_links",
            "product_skus", "warehouses", "inbound_orders", "inbound_lines",
            "inventory_balances", "inventory_transactions", "inventory_policies",
            "external_inventory_snapshots", "external_event_inbox",
            "reconciliation_results", "inventory_alerts", "external_sync_runs",
            "external_accounts", "external_product_mappings", "external_orders",
            "external_order_lines", "daily_sku_sales", "replenishment_suggestions",
            "replenishment_suggestion_actions", "purchase_requests",
            "purchase_request_lines", "purchase_request_actions", "workspaces",
            "user_accounts", "workspace_memberships", "warehouse_access",
            "auth_sessions",
        }
        if required_current.issubset(product_columns) and required_tables.issubset(tables):
            from alembic import command
            from alembic.config import Config

            cfg = Config(str(settings.project_root / "alembic.ini"))
            cfg.set_main_option("sqlalchemy.url", settings.database_url)
            command.stamp(cfg, "head")
            return
        from init_db import main as initialize_legacy

        initialize_legacy()
        return

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(settings.project_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
