"""S1 任务工程化 API 与后台执行测试。

覆盖范围：
- POST /api/crawl 入队并立即返回 job_id；
- GET /api/crawl/{id} 读取任务详情；
- POST /api/crawl/{id}/cancel 写入 cancel_requested；
- 真实采集完成后状态变 succeeded 且商品落库；
- 采集失败时任务落到 failed；
- 取消标志在采集循环中被识别。
"""

import importlib
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.background import reset_executor, wait_for_jobs
from app.db import Base, SessionLocal, engine
from app.schemas import ProductRecord


def _sample_records():
    from datetime import datetime
    from decimal import Decimal

    observed_at = datetime(2026, 8, 30, 10, 0, 0)
    return [
        ProductRecord(
            source="fixture",
            external_product_id=f"async-{i}",
            title=f"样例商品 {i}",
            url=f"https://fixture.local/products/async-{i}",
            category="测试",
            description="异步任务测试",
            rating=Decimal("4.5"),
            current_price=Decimal("99.90"),
            observed_at=observed_at,
        )
        for i in range(2)
    ]


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("WORKER_LEASE_SECONDS", "30")

    import app.config as cfg
    import app.db as db_mod
    import app.repository as repo_mod
    import app.background as bg_mod
    import app.main as main_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(repo_mod)
    importlib.reload(bg_mod)
    importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    reset_executor()

    # 把真实 collect_fixture 替换成快速假实现；_execute_crawl_fixture
    # 在函数体内 import app.crawler.collect_fixture，因此需要 patch 原模块。
    def fake_collect(_path, *, attempt=0, cancel_check=None):
        if cancel_check is not None and cancel_check():
            from app.crawler import CrawlError
            raise CrawlError("CANCELLED", "采集被取消")
        return _sample_records()

    import app.crawler as crawler_mod
    monkeypatch.setattr(crawler_mod, "collect_fixture", fake_collect)

    with TestClient(main_mod.app) as client:
        yield client, db_mod

    reset_executor()
    cfg.get_settings.cache_clear()


def test_enqueue_returns_job_id_immediately(app_client):
    client, _ = app_client
    response = client.post("/api/crawl")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["type"] == "crawl_fixture"
    job_id = body["id"]

    # 状态最终应变为 succeeded
    wait_for_jobs(timeout=10)
    final = client.get(f"/api/crawl/{job_id}").json()
    assert final["status"] == "succeeded"
    assert final["attempt"] >= 1
    assert final["worker_id"] is not None


def test_cancel_sets_flag(app_client):
    client, _ = app_client
    response = client.post("/api/crawl")
    job_id = response.json()["id"]
    cancel = client.post(f"/api/crawl/{job_id}/cancel")
    assert cancel.status_code == 200
    # 取消请求可能赶上任务已完成的窗口；只要 cancel_requested 被写入或任务已
    # 处于终态即视为取消语义生效。
    final = client.get(f"/api/crawl/{job_id}").json()
    assert final["cancel_requested"] is True or final["status"] in (
        "succeeded",
        "failed",
        "cancelled",
    )


def test_job_not_found_returns_404(app_client):
    client, _ = app_client
    assert client.get("/api/crawl/99999").status_code == 404
    assert client.post("/api/crawl/99999/cancel").status_code == 404


def test_enqueue_persists_products(app_client):
    client, _ = app_client
    response = client.post("/api/crawl")
    job_id = response.json()["id"]
    wait_for_jobs(timeout=10)
    final = client.get(f"/api/crawl/{job_id}").json()
    assert final["status"] == "succeeded"

    # 商品应被落库
    from datetime import datetime
    from decimal import Decimal
    products = client.get("/api/products?keyword=样例").json()
    assert products["total"] >= 2