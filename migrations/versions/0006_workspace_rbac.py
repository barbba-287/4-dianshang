"""为员工登录和仓库授权创建正式数据库结构。"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0006_workspace_rbac"
down_revision: Union[str, Sequence[str], None] = "0005_external_sync"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_index_if_missing(name: str, table: str, columns: list[str]) -> None:
    if context.is_offline_mode():
        op.create_index(name, table, columns)
        return
    if name not in {item["name"] for item in inspect(op.get_bind()).get_indexes(table)}:
        op.create_index(name, table, columns)


def upgrade() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    existing = set(inspect(bind).get_table_names())
    if "workspaces" not in existing:
        op.create_table(
            "workspaces",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("tenant_key", sa.String(128), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("tenant_key", name="uq_workspace_tenant_key"),
        )
    _create_index_if_missing("ix_workspaces_tenant_key", "workspaces", ["tenant_key"])
    _create_index_if_missing("ix_workspaces_status", "workspaces", ["status"])

    if "user_accounts" not in existing:
        op.create_table(
            "user_accounts",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("login", sa.String(128), nullable=False),
            sa.Column("password_hash", sa.String(255), nullable=False),
            sa.Column("display_name", sa.String(255), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="active"),
            sa.Column("last_login_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("login", name="uq_user_account_login"),
        )
    _create_index_if_missing("ix_user_accounts_login", "user_accounts", ["login"])
    _create_index_if_missing("ix_user_accounts_status", "user_accounts", ["status"])

    if "workspace_memberships" not in existing:
        op.create_table(
            "workspace_memberships",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("workspace_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(32), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_membership_workspace"),
            sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"], name="fk_membership_user"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_membership"),
        )
    for name, columns in (("ix_workspace_memberships_workspace_id", ["workspace_id"]), ("ix_workspace_memberships_user_id", ["user_id"]), ("ix_workspace_memberships_role", ["role"]), ("ix_workspace_memberships_status", ["status"])):
        _create_index_if_missing(name, "workspace_memberships", columns)

    if "warehouse_access" not in existing:
        op.create_table(
            "warehouse_access",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("warehouse_id", sa.Integer(), nullable=False),
            sa.Column("role_override", sa.String(32), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"], name="fk_warehouse_access_user"),
            sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"], name="fk_warehouse_access_warehouse"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "warehouse_id", name="uq_warehouse_access"),
        )
    for name, columns in (("ix_warehouse_access_user_id", ["user_id"]), ("ix_warehouse_access_warehouse_id", ["warehouse_id"]), ("ix_warehouse_access_status", ["status"])):
        _create_index_if_missing(name, "warehouse_access", columns)

    if "auth_sessions" not in existing:
        op.create_table(
            "auth_sessions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("csrf_token_hash", sa.String(64), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("membership_id", sa.Integer(), nullable=False),
            sa.Column("workspace_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("ip_address", sa.String(64), nullable=True),
            sa.Column("user_agent", sa.String(512), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"], name="fk_auth_session_user"),
            sa.ForeignKeyConstraint(["membership_id"], ["workspace_memberships.id"], name="fk_auth_session_membership"),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_auth_session_workspace"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("token_hash", name="uq_auth_session_token_hash"),
        )
    for name, columns in (("ix_auth_sessions_token_hash", ["token_hash"]), ("ix_auth_sessions_user_id", ["user_id"]), ("ix_auth_sessions_membership_id", ["membership_id"]), ("ix_auth_sessions_workspace_id", ["workspace_id"]), ("ix_auth_sessions_expires_at", ["expires_at"])):
        _create_index_if_missing(name, "auth_sessions", columns)

    warehouse_columns = {column["name"] for column in inspect(op.get_bind()).get_columns("warehouses")}
    if "workspace_id" not in warehouse_columns:
        with op.batch_alter_table("warehouses") as batch_op:
            batch_op.add_column(sa.Column("workspace_id", sa.Integer(), nullable=True))
            batch_op.create_index("ix_warehouses_workspace_id", ["workspace_id"])
            batch_op.create_foreign_key("fk_warehouse_workspace", "workspaces", ["workspace_id"], ["id"])

    if "inventory_policies" not in existing:
        op.create_table(
            "inventory_policies",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("warehouse_id", sa.Integer(), nullable=False),
            sa.Column("sku_id", sa.Integer(), nullable=False),
            sa.Column("safety_stock_qty", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("reorder_point_qty", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"], name="fk_inventory_policy_warehouse"),
            sa.ForeignKeyConstraint(["sku_id"], ["product_skus.id"], name="fk_inventory_policy_sku"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("warehouse_id", "sku_id", name="uq_inventory_policy_warehouse_sku"),
        )
    _create_index_if_missing("ix_inventory_policies_warehouse_id", "inventory_policies", ["warehouse_id"])
    _create_index_if_missing("ix_inventory_policies_sku_id", "inventory_policies", ["sku_id"])


def downgrade() -> None:
    with op.batch_alter_table("warehouses") as batch_op:
        batch_op.drop_constraint("fk_warehouse_workspace", type_="foreignkey")
        batch_op.drop_index("ix_warehouses_workspace_id")
        batch_op.drop_column("workspace_id")
    for name, table in (("ix_inventory_policies_sku_id", "inventory_policies"), ("ix_inventory_policies_warehouse_id", "inventory_policies")):
        op.drop_index(name, table_name=table)
    op.drop_table("inventory_policies")
    for name, table in (("ix_auth_sessions_expires_at", "auth_sessions"), ("ix_auth_sessions_workspace_id", "auth_sessions"), ("ix_auth_sessions_membership_id", "auth_sessions"), ("ix_auth_sessions_user_id", "auth_sessions"), ("ix_auth_sessions_token_hash", "auth_sessions")):
        op.drop_index(name, table_name=table)
    op.drop_table("auth_sessions")
    for name, table in (("ix_warehouse_access_status", "warehouse_access"), ("ix_warehouse_access_warehouse_id", "warehouse_access"), ("ix_warehouse_access_user_id", "warehouse_access")):
        op.drop_index(name, table_name=table)
    op.drop_table("warehouse_access")
    for name, table in (("ix_workspace_memberships_status", "workspace_memberships"), ("ix_workspace_memberships_role", "workspace_memberships"), ("ix_workspace_memberships_user_id", "workspace_memberships"), ("ix_workspace_memberships_workspace_id", "workspace_memberships")):
        op.drop_index(name, table_name=table)
    op.drop_table("workspace_memberships")
    for name, table in (("ix_user_accounts_status", "user_accounts"), ("ix_user_accounts_login", "user_accounts")):
        op.drop_index(name, table_name=table)
    op.drop_table("user_accounts")
    for name, table in (("ix_workspaces_status", "workspaces"), ("ix_workspaces_tenant_key", "workspaces")):
        op.drop_index(name, table_name=table)
    op.drop_table("workspaces")
