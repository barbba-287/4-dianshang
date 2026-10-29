"""数据质量门禁（观察态）：external_sync_runs 追加 quality_status / quality_json 两列。

P2-3（dev_task.p2s3-quality.md）：
- up：给 `external_sync_runs` 追加两个**可空**列（不重建表、不动既有列）：
  - `quality_status`：String(16)，取值 `ok | warn | failed | unknown`；
    NULL = 本次运行没跑质量（失败 / 取消 / 部分成功的 run 两列都保持 NULL）。
  - `quality_json`：Text，六维质量报告全文（JSON 文本）；对外只透出 quality_status。
- down：把两列逐列还原到 0020 形态，其余列/索引/唯一约束零变化，不重建表。

红线：本迁移只做"加可空列"，不改既有列语义、不回填数据、不引入默认值；
**不用 SQLite batch**（batch 会重建成表——本片红线禁止，见 R-33 先例），逐列 ADD/DROP COLUMN。
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "0021_external_sync_quality"
down_revision: Union[str, Sequence[str], None] = "0020_external_sync_attribution"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 追加到 external_sync_runs 的可空列（顺序即迁移后的物理列顺序，也与模型声明顺序一致）。
_QUALITY_COLUMNS: tuple[tuple[str, sa.Column], ...] = (
    ("quality_status", sa.Column("quality_status", sa.String(16), nullable=True)),
    ("quality_json", sa.Column("quality_json", sa.Text(), nullable=True)),
)


def upgrade() -> None:
    if context.is_offline_mode():
        # 离线 SQL 导出没有可反射的库：直接输出已知的加列语句。
        for _name, column in _QUALITY_COLUMNS:
            op.add_column("external_sync_runs", column)
        return

    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "external_sync_runs" not in tables:
        return
    columns = {item["name"] for item in inspect(bind).get_columns("external_sync_runs")}
    for name, column in _QUALITY_COLUMNS:
        if name not in columns:
            op.add_column("external_sync_runs", column)


def downgrade() -> None:
    # 红线：downgrade 只丢本迁移加的两列；不动其余列/索引/约束，不重建表。
    for name, _column in reversed(_QUALITY_COLUMNS):
        op.drop_column("external_sync_runs", name)
