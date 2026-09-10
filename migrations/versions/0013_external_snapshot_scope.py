"""补齐外部库存快照的店铺/外部仓隔离身份。"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0013_external_snapshot_scope"
down_revision: Union[str, Sequence[str], None] = "0012_external_sync_compensation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STORE_SENTINEL = "__default_store__"
_WAREHOUSE_SENTINEL = "__default_warehouse__"


def _constraint_names(bind) -> set[str]:
    return {
        item["name"]
        for item in inspect(bind).get_unique_constraints("external_inventory_snapshots")
        if item.get("name")
    }


def _duplicate_count(bind, columns: str) -> int:
    row = bind.execute(sa.text(
        "SELECT COUNT(*) FROM ("
        f"SELECT 1 FROM external_inventory_snapshots GROUP BY {columns} HAVING COUNT(*) > 1"
        ") AS duplicate_groups"
    )).scalar()
    return int(row or 0)


def upgrade() -> None:
    if context.is_offline_mode():
        op.add_column("external_inventory_snapshots", sa.Column("store_ref_key", sa.String(128), nullable=False, server_default=_STORE_SENTINEL))
        op.add_column("external_inventory_snapshots", sa.Column("warehouse_ref_key", sa.String(128), nullable=False, server_default=_WAREHOUSE_SENTINEL))
        # SQLite's offline dialect cannot ALTER a table constraint. Keep the
        # baseline constraint in the generated script and add the scoped keys
        # as unique indexes; online SQLite uses the batch rebuild below.
        op.create_index(
            "uq_external_snapshot_workspace_identity_v2",
            "external_inventory_snapshots",
            ["workspace_id", "platform", "account_ref", "store_ref_key", "warehouse_ref_key", "external_sku", "as_of", "payload_hash"],
            unique=True,
        )
        op.create_index(
            "uq_external_snapshot_workspace_key_v2",
            "external_inventory_snapshots",
            ["workspace_id", "platform", "account_ref", "store_ref_key", "warehouse_ref_key", "idempotency_key"],
            unique=True,
        )
        op.create_index("ix_external_inventory_snapshots_store_ref_key", "external_inventory_snapshots", ["store_ref_key"])
        op.create_index("ix_external_inventory_snapshots_warehouse_ref_key", "external_inventory_snapshots", ["warehouse_ref_key"])
        return
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "external_inventory_snapshots" not in tables:
        raise RuntimeError("0013 要求 external_inventory_snapshots 表存在，无法静默跳过")

    columns = {item["name"] for item in inspect(bind).get_columns("external_inventory_snapshots")}
    with op.batch_alter_table("external_inventory_snapshots") as batch:
        if "store_ref_key" not in columns:
            batch.add_column(sa.Column("store_ref_key", sa.String(128), nullable=True))
        if "warehouse_ref_key" not in columns:
            batch.add_column(sa.Column("warehouse_ref_key", sa.String(128), nullable=True))

    bind.execute(sa.text(
        "UPDATE external_inventory_snapshots "
        "SET store_ref_key = COALESCE(store_ref, :store_sentinel), "
        "warehouse_ref_key = COALESCE(warehouse_ref, :warehouse_sentinel) "
        "WHERE store_ref_key IS NULL OR warehouse_ref_key IS NULL"
    ), {"store_sentinel": _STORE_SENTINEL, "warehouse_sentinel": _WAREHOUSE_SENTINEL})

    missing = bind.execute(sa.text(
        "SELECT COUNT(*) FROM external_inventory_snapshots "
        "WHERE store_ref_key IS NULL OR warehouse_ref_key IS NULL"
    )).scalar()
    if missing:
        raise RuntimeError("0013 无法为外部库存快照生成非空来源身份")

    null_workspace = bind.execute(sa.text(
        "SELECT COUNT(*) FROM external_inventory_snapshots WHERE workspace_id IS NULL"
    )).scalar()
    if null_workspace:
        raise RuntimeError("0013 拒绝迁移 workspace_id=NULL 的外部库存快照")

    identity_columns = (
        "workspace_id, platform, account_ref, store_ref_key, warehouse_ref_key, "
        "external_sku, as_of, payload_hash"
    )
    key_columns = (
        "workspace_id, platform, account_ref, store_ref_key, warehouse_ref_key, idempotency_key"
    )
    if _duplicate_count(bind, identity_columns):
        raise RuntimeError("0013 外部库存内容身份存在重复，需人工处理后重试")
    if _duplicate_count(bind, key_columns):
        raise RuntimeError("0013 外部库存幂等身份存在重复，需人工处理后重试")

    columns = {item["name"] for item in inspect(bind).get_columns("external_inventory_snapshots")}
    with op.batch_alter_table("external_inventory_snapshots") as batch:
        if "store_ref_key" in columns:
            batch.alter_column("store_ref_key", existing_type=sa.String(128), nullable=False, server_default=_STORE_SENTINEL)
        if "warehouse_ref_key" in columns:
            batch.alter_column("warehouse_ref_key", existing_type=sa.String(128), nullable=False, server_default=_WAREHOUSE_SENTINEL)

    constraints = _constraint_names(bind)
    with op.batch_alter_table("external_inventory_snapshots") as batch:
        if "uq_external_snapshot_workspace_identity" in constraints:
            batch.drop_constraint("uq_external_snapshot_workspace_identity", type_="unique")
        if "uq_external_snapshot_workspace_identity" not in constraints:
            batch.create_unique_constraint(
                "uq_external_snapshot_workspace_identity",
                ["workspace_id", "platform", "account_ref", "store_ref_key", "warehouse_ref_key", "external_sku", "as_of", "payload_hash"],
            )
        if "uq_external_snapshot_workspace_key" not in constraints:
            batch.create_unique_constraint(
                "uq_external_snapshot_workspace_key",
                ["workspace_id", "platform", "account_ref", "store_ref_key", "warehouse_ref_key", "idempotency_key"],
            )

    indexes = {item["name"] for item in inspect(bind).get_indexes("external_inventory_snapshots")}
    for name, column in (
        ("ix_external_inventory_snapshots_store_ref_key", "store_ref_key"),
        ("ix_external_inventory_snapshots_warehouse_ref_key", "warehouse_ref_key"),
    ):
        if name not in indexes:
            op.create_index(name, "external_inventory_snapshots", [column])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "external_inventory_snapshots" not in tables:
        return
    indexes = {item["name"] for item in inspect(bind).get_indexes("external_inventory_snapshots")}
    for name in ("ix_external_inventory_snapshots_store_ref_key", "ix_external_inventory_snapshots_warehouse_ref_key"):
        if name in indexes:
            op.drop_index(name, table_name="external_inventory_snapshots")
    constraints = _constraint_names(bind)
    with op.batch_alter_table("external_inventory_snapshots") as batch:
        if "uq_external_snapshot_workspace_key" in constraints:
            batch.drop_constraint("uq_external_snapshot_workspace_key", type_="unique")
        if "uq_external_snapshot_workspace_identity" in constraints:
            batch.drop_constraint("uq_external_snapshot_workspace_identity", type_="unique")
        batch.create_unique_constraint(
            "uq_external_snapshot_workspace_identity",
            ["workspace_id", "platform", "account_ref", "external_sku", "as_of", "payload_hash"],
        )
        columns = {item["name"] for item in inspect(bind).get_columns("external_inventory_snapshots")}
        if "store_ref_key" in columns:
            batch.drop_column("store_ref_key")
        if "warehouse_ref_key" in columns:
            batch.drop_column("warehouse_ref_key")
