"""为业务数据补齐 workspace 归属、旧数据回填和空间内业务键。"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0007_workspace_iso"
down_revision: Union[str, Sequence[str], None] = "0006_workspace_rbac"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = (
    "products", "product_price_history", "product_skus", "crawl_jobs",
    "documents", "document_versions", "document_chunks", "document_product_links",
    "inbound_orders", "inbound_lines", "inventory_transactions", "inventory_balances",
    "inventory_policies", "external_inventory_snapshots", "external_event_inbox",
    "reconciliation_results", "inventory_alerts",
)

# 这些业务键在同一个商家内唯一，不应阻止两个商家使用相同编码。
_UNIQUE_REPLACEMENTS = {
    "products": (
        ("uq_product_source_external_id", "uq_product_workspace_source_external_id", ("workspace_id", "source", "external_product_id")),
    ),
    "product_skus": (
        ("uq_product_sku_code", "uq_product_workspace_sku_code", ("workspace_id", "sku_code")),
    ),
    "warehouses": (
        ("uq_warehouse_code", "uq_warehouse_workspace_code", ("workspace_id", "code")),
    ),
    "inbound_orders": (
        ("uq_inbound_reference_no", "uq_inbound_workspace_reference_no", ("workspace_id", "reference_no")),
    ),
    "inventory_transactions": (
        ("uq_inventory_transaction_key", "uq_inventory_transaction_workspace_key", ("workspace_id", "idempotency_key")),
    ),
    "external_inventory_snapshots": (
        ("uq_external_snapshot_identity", "uq_external_snapshot_workspace_identity", ("workspace_id", "platform", "account_ref", "external_sku", "as_of", "payload_hash")),
    ),
    "external_event_inbox": (
        ("uq_external_event_key", "uq_external_event_workspace_key", ("workspace_id", "platform", "account_ref", "idempotency_key")),
    ),
    "reconciliation_results": (
        ("uq_reconciliation_result_identity", "uq_reconciliation_workspace_identity", ("workspace_id", "snapshot_id", "external_sku", "warehouse_id")),
    ),
    "inventory_policies": (
        ("uq_inventory_policy_warehouse_sku", "uq_inventory_policy_workspace_warehouse_sku", ("workspace_id", "warehouse_id", "sku_id")),
    ),
    "inventory_balances": (
        ("uq_inventory_balance_warehouse_sku", "uq_inventory_balance_workspace_warehouse_sku", ("workspace_id", "warehouse_id", "sku_id")),
    ),
}
def _offline_upgrade() -> None:
    """Emit additive workspace columns for a fresh-schema SQL export.

    Legacy backfill and constraint inspection are intentionally online-only.
    """
    tables = (
        "products", "product_price_history", "product_skus", "crawl_jobs",
        "documents", "document_versions", "document_chunks",
        "document_product_links", "warehouses", "inbound_orders",
        "inbound_lines", "inventory_transactions", "inventory_balances",
        "inventory_policies", "external_inventory_snapshots",
        "external_event_inbox", "reconciliation_results",
    )
    for table in tables:
        op.add_column(table, sa.Column("workspace_id", sa.Integer(), nullable=True))
        op.create_index(f"ix_{table}_workspace_id", table, ["workspace_id"])
    op.add_column("crawl_jobs", sa.Column("payload_json", sa.Text(), nullable=True))


def _add_workspace_column(table: str) -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {item["name"] for item in inspector.get_columns(table)}
    foreign_keys = {
        tuple(item.get("constrained_columns", ()))
        for item in inspector.get_foreign_keys(table)
    }
    with op.batch_alter_table(table) as batch:
        if "workspace_id" not in columns:
            batch.add_column(sa.Column("workspace_id", sa.Integer(), nullable=True))
        if ("workspace_id",) not in foreign_keys:
            batch.create_foreign_key(
                f"fk_{table}_workspace", "workspaces", ["workspace_id"], ["id"]
            )
    names = {item["name"] for item in inspect(bind).get_indexes(table)}
    index_name = f"ix_{table}_workspace_id"
    if index_name not in names:
        op.create_index(index_name, table, ["workspace_id"])


def _ensure_legacy_workspace(existing: set[str]) -> None:
    """回填唯一可判定的 legacy 数据；多空间歧义时拒绝升级。"""
    bind = op.get_bind()
    if not existing.intersection(_TABLES):
        return
    rows = bind.execute(
        sa.text("SELECT id, tenant_key FROM workspaces WHERE status = 'active' ORDER BY id")
    ).fetchall()
    if len(rows) == 1:
        workspace_id = rows[0][0]
    elif len(rows) > 1:
        unbound = any(
            bind.execute(sa.text(
                f"SELECT 1 FROM {table} WHERE workspace_id IS NULL LIMIT 1"
            )).first() is not None
            for table in _TABLES if table in existing
        )
        if unbound:
            raise RuntimeError("0007 无法为多工作空间中的 NULL 数据判定归属")
        return
    else:
        workspace_id = bind.execute(
            sa.text(
                "INSERT INTO workspaces (tenant_key, name, status, created_at, updated_at) "
                "VALUES ('default', '默认商家', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        ).lastrowid
    for table in _TABLES:
        if table not in existing:
            continue
        bind.execute(
            sa.text(f"UPDATE {table} SET workspace_id = :workspace_id WHERE workspace_id IS NULL"),
            {"workspace_id": workspace_id},
        )


def _replace_unique_constraints(existing: set[str]) -> None:
    bind = op.get_bind()
    for table, replacements in _UNIQUE_REPLACEMENTS.items():
        if table not in existing:
            continue
        constraints = {item["name"] for item in inspect(bind).get_unique_constraints(table)}
        for old_name, new_name, columns in replacements:
            with op.batch_alter_table(table) as batch:
                if old_name in constraints:
                    batch.drop_constraint(old_name, type_="unique")
                if new_name not in constraints:
                    batch.create_unique_constraint(new_name, list(columns))
            constraints.add(new_name)


def _add_document_identity_constraint(existing: set[str]) -> None:
    if "documents" not in existing:
        return
    bind = op.get_bind()
    constraints = {item["name"] for item in inspect(bind).get_unique_constraints("documents")}
    if "uq_document_workspace_sha256" not in constraints:
        with op.batch_alter_table("documents") as batch:
            batch.create_unique_constraint(
                "uq_document_workspace_sha256", ["workspace_id", "sha256"]
            )


def upgrade() -> None:
    if context.is_offline_mode():
        _offline_upgrade()
        return
    bind = op.get_bind()
    existing = set(inspect(bind).get_table_names())
    for table in _TABLES:
        if table in existing:
            _add_workspace_column(table)
    if "crawl_jobs" in existing:
        columns = {item["name"] for item in inspect(bind).get_columns("crawl_jobs")}
        if "payload_json" not in columns:
            with op.batch_alter_table("crawl_jobs") as batch:
                batch.add_column(sa.Column("payload_json", sa.Text(), nullable=True))
    # 先完成旧数据归属，再替换唯一约束，避免旧全局键或 NULL 导致升级后不可见。
    _ensure_legacy_workspace(existing)
    _replace_unique_constraints(existing)
    _add_document_identity_constraint(existing)


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(inspect(bind).get_table_names())
    for table, replacements in _UNIQUE_REPLACEMENTS.items():
        if table not in existing:
            continue
        constraints = {item["name"] for item in inspect(bind).get_unique_constraints(table)}
        for old_name, new_name, columns in replacements:
            with op.batch_alter_table(table) as batch:
                if new_name in constraints:
                    batch.drop_constraint(new_name, type_="unique")
                if old_name not in constraints:
                    batch.create_unique_constraint(old_name, list(columns[1:]))
            constraints.add(old_name)
    if "documents" in existing:
        constraints = {item["name"] for item in inspect(bind).get_unique_constraints("documents")}
        if "uq_document_workspace_sha256" in constraints:
            with op.batch_alter_table("documents") as batch:
                batch.drop_constraint("uq_document_workspace_sha256", type_="unique")
    if "crawl_jobs" in existing:
        columns = {item["name"] for item in inspect(bind).get_columns("crawl_jobs")}
        if "payload_json" in columns:
            with op.batch_alter_table("crawl_jobs") as batch:
                batch.drop_column("payload_json")
    for table in reversed(_TABLES):
        if table not in existing:
            continue
        columns = {item["name"] for item in inspect(bind).get_columns(table)}
        if "workspace_id" not in columns:
            continue
        index_name = f"ix_{table}_workspace_id"
        if index_name in {item["name"] for item in inspect(bind).get_indexes(table)}:
            op.drop_index(index_name, table_name=table)
        with op.batch_alter_table(table) as batch:
            batch.drop_column("workspace_id")
