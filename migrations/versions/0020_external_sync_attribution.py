"""归因四件套 + 授权引用实体化（actor / request_id / credential_ref_id / upstream ids）。

P2-2（dev_task.p2s2-source-batch.md）：
- up：
  1. 新建表 `external_credential_refs`：授权引用实体，
     **只存 env 键名 + 8 位 sha256 指纹**，明文凭据任何形态都不入库。
  2. `external_sync_runs` 追加四个**可空**列（不重建表、不动既有列）：
     - `actor`：操作者（HTTP 走 principal.subject，系统路径走 `system:<来源>` 常量）
     - `request_id`：请求 id（HTTP 复用中间件的 `X-Request-ID`，非 HTTP 走 `req-bg-<uuid12>`）
     - `credential_ref_id`：本次运行用的授权引用（指向 `external_credential_refs.id`）
     - `upstream_request_ids_json`：本次运行各页上游请求 id（只保留最近 50 条）
- down：把两张表还原到 0019 形态（四列全丢 + 新表整表删），
  其余列/索引/唯一约束零变化，不重建表。

红线：本迁移只做"加可空列 + 建新表"，不改既有列语义、不回填数据、不引入默认值。
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0020_external_sync_attribution"
down_revision: Union[str, Sequence[str], None] = "0019_external_sync_run_cursor"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 追加到 external_sync_runs 的可空列（顺序即迁移后的物理列顺序，也与模型声明顺序一致）。
_PLAIN_RUN_COLUMNS: tuple[tuple[str, sa.Column], ...] = (
    ("actor", sa.Column("actor", sa.String(128), nullable=True)),
    ("request_id", sa.Column("request_id", sa.String(64), nullable=True)),
)

_TRAILING_RUN_COLUMN: tuple[str, sa.Column] = (
    "upstream_request_ids_json",
    sa.Column("upstream_request_ids_json", sa.Text(), nullable=True),
)

# credential_ref_id 带外键，但 SQLite 的 ALTER TABLE 不能单独 ADD CONSTRAINT
# （alembic 会抛 "No support for ALTER of constraints in SQLite dialect"，
# 走 batch 会重建成表——本片红线禁止 batch）。
# 这里用原生 ALTER TABLE ... ADD COLUMN ... REFERENCES（SQLite 允许：可空列可加外键），
# 不重建表、不引入 batch，语义与模型里的 ForeignKey 一致。
_FK_COLUMN_NAME = "credential_ref_id"
_FK_COLUMN_SQL = (
    "ALTER TABLE external_sync_runs "
    "ADD COLUMN credential_ref_id INTEGER REFERENCES external_credential_refs (id)"
)

# 授权引用表的索引（与 app/db.py 模型的 index=True 对齐）。
_REF_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_external_credential_refs_workspace_id", "workspace_id"),
    ("ix_external_credential_refs_platform", "platform"),
)


def upgrade() -> None:
    if context.is_offline_mode():
        # 离线 SQL 导出没有可反射的库：直接输出已知的建表/加列语句。
        op.create_table("external_credential_refs", *_credential_refs_table_args())
        for _name, column in _PLAIN_RUN_COLUMNS:
            op.add_column("external_sync_runs", column)
        op.execute(_FK_COLUMN_SQL)
        op.add_column("external_sync_runs", _TRAILING_RUN_COLUMN[1])
        return

    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())

    # 先建新表：credential_ref_id 列要引用它。
    if "external_credential_refs" not in tables:
        op.create_table("external_credential_refs", *_credential_refs_table_args())
    indexes = {item["name"] for item in inspect(bind).get_indexes("external_credential_refs")}
    for index_name, column_name in _REF_INDEXES:
        if index_name not in indexes:
            op.create_index(index_name, "external_credential_refs", [column_name])

    if "external_sync_runs" in tables:
        columns = {item["name"] for item in inspect(bind).get_columns("external_sync_runs")}
        for name, column in _PLAIN_RUN_COLUMNS:
            if name not in columns:
                op.add_column("external_sync_runs", column)
        if _FK_COLUMN_NAME not in columns:
            op.execute(_FK_COLUMN_SQL)
        trailing_name, trailing_column = _TRAILING_RUN_COLUMN
        if trailing_name not in columns:
            op.add_column("external_sync_runs", trailing_column)


def downgrade() -> None:
    # 红线：downgrade 只丢本迁移加的四列与新表；不动其余列/索引/约束，不重建表。
    # 先丢列（含指向新表的外键列），再删新表，避免留下悬空外键。
    op.drop_column("external_sync_runs", _TRAILING_RUN_COLUMN[0])
    op.drop_column("external_sync_runs", _FK_COLUMN_NAME)
    op.drop_column("external_sync_runs", "request_id")
    op.drop_column("external_sync_runs", "actor")
    op.drop_table("external_credential_refs")


def _credential_refs_table_args() -> tuple:
    """`external_credential_refs` 的建表参数（与 app/db.py 的模型逐列一致）。"""
    return (
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=True),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("ref_name", sa.String(64), nullable=False),
        sa.Column("env_key", sa.String(128), nullable=False),
        # 8 位 sha256 指纹；明文凭据永远不入库。
        sa.Column("secret_fingerprint", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_external_credential_ref_workspace"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_name", name="uq_external_credential_ref_ref_name"),
    )
