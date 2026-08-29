"""电商商品与客服工作台的本地样例采集命令。

用途：通过 Playwright 读取项目内 HTML fixture，将标准化商品记录导入
当前配置的数据库，用于第一周数据闭环演示和验收。
"""

from pathlib import Path

from app.crawler import collect_fixture
from app.db import SessionLocal, init_db
from app.repository import import_records


if __name__ == "__main__":
    project_root = Path(__file__).resolve().parent
    fixture = project_root / "fixtures" / "products.html"
    init_db()
    records = collect_fixture(fixture)
    with SessionLocal() as db:
        job = import_records(db, records, keyword="fixture")
    print(f"采集完成: {len(records)} 件商品，任务 #{job.id}，状态={job.status}")
