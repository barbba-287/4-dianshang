"""电商工作台的数据库初始化命令。

智能识别库状态，自动选择 Alembic 初始化或升级：

- 空库（无业务表）→ upgrade head 创建全部表；
- legacy 库（业务表已存在但 alembic_version 不存在）→ stamp 0001
  标记已知 baseline，再 upgrade head 执行后续修复迁移；
- 已迁移库（alembic_version 已存在）→ upgrade head 幂等。

使用方式
--------

```bash
python init_db.py            # 自动判断并升级到最新版本
DATABASE_URL=sqlite:///.../db python init_db.py
alembic current              # 查看当前版本
alembic history --verbose    # 查看迁移历史
```

兼容说明
--------

- 新 SQLite：upgrade head 创建全部 7 张表；
- 已有 MySQL（week1 schema）：标记已知 baseline 后执行 repair migration；
- 已有 MySQL（含 S1~S5 全部表 + alembic_version）：upgrade head 幂等。
- 已迁移库后续改 model：`alembic revision --autogenerate` + `upgrade head`。
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from app.config import get_settings
from app.main import safe_database_url
from app.db import engine


_BUSINESS_TABLES = {"products", "crawl_jobs"}
_REQUIRED_TABLES = {
    "products",
    "product_price_history",
    "crawl_jobs",
    "documents",
    "document_versions",
    "document_chunks",
    "document_product_links",
    "product_skus",
    "warehouses",
    "inbound_orders",
    "inbound_lines",
    "inventory_balances",
    "inventory_transactions",
    "external_inventory_snapshots",
    "external_event_inbox",
    "reconciliation_results",
    "inventory_policies",
    "workspaces",
    "user_accounts",
    "workspace_memberships",
    "warehouse_access",
    "auth_sessions",
}
_REQUIRED_CRAWL_COLUMNS = {
    "type",
    "max_retries",
    "attempt",
    "worker_id",
    "lease_until",
    "next_run_at",
    "run_id",
    "cancel_requested",
}


def _alembic_config() -> Config:
    settings = get_settings()
    cfg = Config(str(Path(__file__).resolve().parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    return cfg


def _detect_state() -> tuple[str, list[str]]:
    """返回 (state, table_names)。state ∈ {empty, legacy, migrated}.

    处理三种边界：
    - 空库：无业务表 → empty
    - legacy 库：业务表已存在但 alembic_version 不存在 → legacy
    - 半迁移库：alembic_version 存在但 version_num 为空（之前 upgrade
      中断留下的“孤儿”表）→ legacy（先标记 0001，再执行后续迁移）
    - 已迁移库：alembic_version 有具体版本号 → migrated
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "alembic_version" not in tables:
        if tables & _BUSINESS_TABLES:
            return "legacy", sorted(tables)
        return "empty", sorted(tables)
    # alembic_version 存在，查 version_num
    with engine.connect() as conn:
        version = conn.execute(
            text("SELECT version_num FROM alembic_version LIMIT 1")
        ).scalar()
    if not version:
        # 孤儿表：之前 upgrade 中断，未记录任何版本
        return "legacy", sorted(tables)
    return "migrated", sorted(tables)


def main() -> None:
    cfg = _alembic_config()
    settings = get_settings()
    print(f"数据库: {safe_database_url(settings.database_url)}")
    state, tables = _detect_state()
    print(f"检测: {state} | 已有表 {len(tables)} 张")

    if state == "empty":
        print("执行 alembic upgrade head（创建全部表）...")
        command.upgrade(cfg, "head")
    elif state == "legacy":
        # 旧库已经有 week1 表，不能执行 0001 的全量建表 migration。
        # 先把它标记在 baseline，再运行后续 repair migration，补齐 S1
        # 字段和 S2-S5 表；不能直接 stamp head，否则后续迁移会被跳过。
        print("legacy 库：标记 0001 baseline 后执行后续迁移...")
        command.stamp(cfg, "0001_baseline")
        command.upgrade(cfg, "head")
    else:
        print("已迁移库：执行 alembic upgrade head（幂等）...")
        command.upgrade(cfg, "head")

    # 显示当前版本
    with engine.connect() as conn:
        rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    print(f"当前 alembic 版本: {rev}")


if __name__ == "__main__":
    main()