"""电商工作台的数据库初始化命令。

智能识别库状态，自动选择 alembic stamp head 或 alembic upgrade head：

- 空库（无业务表）→ upgrade head 创建全部表；
- legacy 库（业务表已存在但 alembic_version 不存在）→ stamp head
  标记 baseline，不执行 DDL；
- 已迁移库（alembic_version 已存在）→ upgrade head 幂等。

使用方式
--------

```bash
python init_db.py            # 自动判断走 stamp / upgrade
DATABASE_URL=sqlite:///.../db python init_db.py
alembic current              # 查看当前版本
alembic history --verbose    # 查看迁移历史
```

兼容说明
--------

- 新 SQLite：upgrade head 创建全部 7 张表；
- 已有 MySQL（week1 schema）：stamp head 标记 baseline；
- 已有 MySQL（含 S1~S5 全部表 + alembic_version）：upgrade head 幂等。
- 已迁移库后续改 model：`alembic revision --autogenerate` + `upgrade head`。
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from app.config import get_settings
from app.db import engine


_BUSINESS_TABLES = {"products", "crawl_jobs"}


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
      中断留下的“孤儿”表）→ legacy（用 stamp head 标记为已迁移）
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
    print(f"数据库: {settings.database_url}")
    state, tables = _detect_state()
    print(f"检测: {state} | 已有表 {len(tables)} 张")

    if state == "empty":
        print("执行 alembic upgrade head（创建全部表）...")
        command.upgrade(cfg, "head")
    elif state == "legacy":
        print("legacy 库：执行 alembic stamp head（标记 baseline，不执行 DDL）...")
        command.stamp(cfg, "head")
    else:
        print("已迁移库：执行 alembic upgrade head（幂等）...")
        command.upgrade(cfg, "head")

    # 显示当前版本
    with engine.connect() as conn:
        rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    print(f"当前 alembic 版本: {rev}")


if __name__ == "__main__":
    main()