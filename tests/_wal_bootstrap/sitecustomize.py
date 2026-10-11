"""测试用 SQLite WAL 注入（供子进程使用）。

本机（Windows）SQLite 默认的 `journal_mode=delete` 会为每次 DDL 建表
创建并删除一个 `-journal` 回滚日志文件，约 40ms/表；43 张表需约 10.5s。
改为 WAL 后同一操作约 0.04s。

`tests/conftest.py` 已通过挂在 `Engine` 基类上的事件监听覆盖了主进程，
但 `subprocess.run([sys.executable, ...])` 启动的子进程（如
`tests/test_alembic.py`、部分 CLI 测试）不会执行 conftest。Python 启动时
会自动导入 `sys.path` 上第一个 `sitecustomize`，因此 conftest 会把本目录
前置到 `PYTHONPATH`，让子进程也拿到同样的 PRAGMA 设置。

WorkBuddy 沙箱自身的 `sitecustomize` shim 在设计上会主动 `__import__`
本模块后再恢复自身，因此两者可叠加、互不破坏。
"""

import sqlite3


def _install_sqlite_wal() -> None:
    try:
        from sqlalchemy import event
        from sqlalchemy.engine import Engine
    except ImportError:
        # 非 SQLAlchemy 的子进程（如纯 CLI 脚本）无需处理
        return

    if getattr(Engine, "_dianshang_sqlite_wal_hooked", False):
        return

    @event.listens_for(Engine, "connect")
    def _enable_wal(dbapi_connection, _connection_record):
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

    Engine._dianshang_sqlite_wal_hooked = True


_install_sqlite_wal()
