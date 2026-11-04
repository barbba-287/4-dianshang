"""P7-S1 成本快照：新增 cost_snapshots 表 + 入库行单价 + 订单行成本锚定五件套。

P7（dev_task.p7-profit-center.md v1.1）第一刀，只做**结构**：

- 新表 `cost_snapshots`：SKU 在某个生效时刻的入库成本单价，**append-only**
  （应用层永不 UPDATE / DELETE，成本修正=追加新快照）。
  唯一约束 `(workspace_id, sku_id, effective_time, source, source_ref)`：
  - 四分量缺一不可：同一 SKU 同一时刻允许「入库」与「手工」两种来源并存；
  - `source_ref` 让重复确认入库不产生第二行（`inbound:{order}:line:{line}`）。
- `inbound_lines.unit_cost`：实收申报的入库成本单价（可空——存量单与"暂不填价"场景）。
- `external_order_lines` 五列：`cost_snapshot_id / unit_cost / fee_rate / cost_status / anchored_at`。
  本迁移只建列；锚定逻辑在 P7-S2 的 `ingest_orders` 内实现。

红线：
- **不用 SQLite batch_alter_table**（batch 会重建整表，见 R-33 先例）；加列一律
  `op.add_column`，SQLite 原生 ALTER TABLE ADD COLUMN 足够（列均为 NULL 或有常量默认）。
- 全部操作幂等：先反射判断表/列存在，再执行（create_all 建过的测试库不会撞）。
- 不改任何既有列的类型、约束与索引语义。
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0025_cost_snapshots"
down_revision: Union[str, Sequence[str], None] = "0024_external_import_rejections"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "cost_snapshots"
_UNIQUE_CONSTRAINT_NAME = "uq_cost_snapshot_identity"
_INDEX_WORKSPACE = "ix_cost_snapshots_workspace_id"
_INDEX_SKU = "ix_cost_snapshots_sku_id"
_INDEX_SOURCE = "ix_cost_snapshots_source"
_INDEX_LOOKUP = "ix_cost_snapshot_lookup"

# 追加列：表名 → [(列名, Column)]
_ADDED_COLUMNS: dict[str, list[tuple[str, sa.Column]]] = {
    "inbound_lines": [
        ("unit_cost", sa.Column("unit_cost", sa.Numeric(12, 2), nullable=True)),
    ],
    "external_order_lines": [
        ("cost_snapshot_id", sa.Column("cost_snapshot_id", sa.Integer(), nullable=True)),
        ("unit_cost", sa.Column("unit_cost", sa.Numeric(12, 2), nullable=True)),
        ("fee_rate", sa.Column("fee_rate", sa.Numeric(6, 4), nullable=True)),
        (
            "cost_status",
            sa.Column("cost_status", sa.String(16), nullable=False, server_default="missing"),
        ),
        ("anchored_at", sa.Column("anchored_at", sa.DateTime(), nullable=True)),
    ],
}

# 追加索引：表名 → [(索引名, [列], unique)]
_ADDED_INDEXES: dict[str, list[tuple[str, list[str], bool]]] = {
    "external_order_lines": [
        ("ix_external_order_lines_cost_snapshot_id", ["cost_snapshot_id"], False),
        ("ix_external_order_lines_cost_status", ["cost_status"], False),
    ],
}


def _columns() -> list[sa.Column]:
    """列定义工厂（每次调用返回全新实例，避免复用同一 Column 对象）。"""
    return [
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("sku_id", sa.Integer(), sa.ForeignKey("product_skus.id"), nullable=False),
        sa.Column("unit_cost", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="CNY"),
        sa.Column("effective_time", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(24), nullable=False, server_default="manual"),
        sa.Column("source_ref", sa.String(128), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("created_by", sa.String(128), nullable=True),
    ]


def _create_table() -> None:
    op.create_table(
        _TABLE,
        *_columns(),
        sa.UniqueConstraint(
            "workspace_id", "sku_id", "effective_time", "source", "source_ref",
            name=_UNIQUE_CONSTRAINT_NAME,
        ),
    )
    op.create_index(_INDEX_WORKSPACE, _TABLE, ["workspace_id"], unique=False)
    op.create_index(_INDEX_SKU, _TABLE, ["sku_id"], unique=False)
    op.create_index(_INDEX_SOURCE, _TABLE, ["source"], unique=False)
    op.create_index(_INDEX_LOOKUP, _TABLE, ["workspace_id", "sku_id", "effective_time"], unique=False)


def upgrade() -> None:
    if context.is_offline_mode():
        _create_table()
        for table, columns in _ADDED_COLUMNS.items():
            for _, column in columns:
                op.add_column(table, column)
        for table, indexes in _ADDED_INDEXES.items():
            for name, cols, unique in indexes:
                op.create_index(name, table, cols, unique=unique)
        return

    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if _TABLE not in existing_tables:
        _create_table()

    # 加列前必须判断表是否存在：0031 之前的 stamped 库可能缺表
    # （test_alembic_upgrade_repairs_stamped_0017_ticket_table 正是这种残缺库，
    #  get_columns 对不存在的表会抛 NoSuchTableError，迁移整条链会中断）。
    for table, columns in _ADDED_COLUMNS.items():
        if table not in existing_tables:
            continue
        existing = {col["name"] for col in inspector.get_columns(table)}
        for name, column in columns:
            if name in existing:
                continue
            op.add_column(table, column)

    for table, indexes in _ADDED_INDEXES.items():
        if table not in existing_tables:
            continue
        existing_indexes = {index["name"] for index in inspector.get_indexes(table)}
        for name, cols, unique in indexes:
            if name in existing_indexes:
                continue
            op.create_index(name, table, cols, unique=unique)


def downgrade() -> None:
    if context.is_offline_mode():
        for table, indexes in _ADDED_INDEXES.items():
            for name, _cols, _unique in indexes:
                op.drop_index(name, table_name=table)
        for table, columns in _ADDED_COLUMNS.items():
            for name, _column in columns:
                op.drop_column(table, name)
        op.drop_table(_TABLE)
        return

    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    for table, indexes in _ADDED_INDEXES.items():
        if table not in existing_tables:
            continue
        existing_indexes = {index["name"] for index in inspector.get_indexes(table)}
        for name, _cols, _unique in indexes:
            if name not in existing_indexes:
                continue
            op.drop_index(name, table_name=table)

    for table, columns in _ADDED_COLUMNS.items():
        if table not in existing_tables:
            continue
        existing = {col["name"] for col in inspector.get_columns(table)}
        for name, _column in columns:
            if name not in existing:
                continue
            op.drop_column(table, name)

    if _TABLE in existing_tables:
        op.drop_table(_TABLE)
