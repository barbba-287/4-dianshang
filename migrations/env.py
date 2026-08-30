"""Alembic 环境配置。

- 从 `app.config.get_settings().database_url` 读取连接串，支持
  `DATABASE_URL` 环境变量覆盖 .env 文件中的配置（便于本地用 SQLite
  / 演示用 MySQL）。
- `target_metadata` 使用 `app.db.Base.metadata`，并显式 import 所有
  model，让 `--autogenerate` 能反向对比生成迁移脚本。
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# 让 `from app...` 在 alembic 进程里可用
from app.config import get_settings
from app.db import Base

# 显式 import 所有 model，确保 Base.metadata 包含完整表
from app.db import (  # noqa: F401  触发模型注册
    CrawlJob,
    Document,
    DocumentChunk,
    DocumentProductLink,
    DocumentVersion,
    Product,
    ProductPriceHistory,
)


# Alembic Config 对象，提供 .ini 中的值
config = context.config

# 读取 Python logging 配置
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 让 autogenerate 能比较 Base.metadata
target_metadata = Base.metadata

# 从 Settings 注入 sqlalchemy.url（覆盖 .ini 中的空值）
config.set_main_option("sqlalchemy.url", get_settings().database_url)


def run_migrations_offline() -> None:
    """offline 模式：只生成 SQL，不连接数据库。

    用于 `alembic upgrade head --sql` 导出迁移脚本（CI / 审计）。
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """online 模式：创建 Engine 并连接到目标数据库。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()