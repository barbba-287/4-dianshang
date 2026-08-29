"""电商商品与客服工作台的数据库初始化命令。

用途：创建第一周闭环所需的商品、价格历史和采集任务表，支持 SQLite
快速开发以及通过 DATABASE_URL 配置的 MySQL 环境。
"""

from app.config import get_settings
from app.db import init_db


if __name__ == "__main__":
    settings = get_settings()
    init_db()
    print(f"数据库已初始化: {settings.database_url}")
