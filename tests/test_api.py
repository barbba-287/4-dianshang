from datetime import datetime
from decimal import Decimal
"""电商商品与客服工作台 API 的行为测试。

用途：验证健康检查、商品采集接口、分页筛选、幂等写入、价格历史和
采集失败记录，确保第一周后端闭环可回归。
"""

import importlib

import pytest
from fastapi.testclient import TestClient

from app.schemas import ProductRecord


def sample_records() -> list[ProductRecord]:
    observed_at = datetime(2026, 8, 29, 12, 0, 0)
    return [
        ProductRecord(
            source="fixture",
            external_product_id="tea-001",
            title="高山云雾绿茶 250g",
            url="https://fixture.local/products/tea-001",
            category="茶饮",
            description="春季采摘，清香回甘，适合日常冲泡。",
            rating=Decimal("4.8"),
            current_price=Decimal("89.90"),
            observed_at=observed_at,
        ),
        ProductRecord(
            source="fixture",
            external_product_id="mug-001",
            title="陶瓷保温马克杯 420ml",
            url="https://fixture.local/products/mug-001",
            category="家居",
            description="带盖防尘，适合办公室和居家使用。",
            rating=Decimal("4.6"),
            current_price=Decimal("39.00"),
            observed_at=observed_at,
        ),
        ProductRecord(
            source="fixture",
            external_product_id="lamp-001",
            title="护眼 LED 台灯",
            url="https://fixture.local/products/lamp-001",
            category="家居",
            description="三档色温调节，支持定时关闭。",
            rating=Decimal("4.7"),
            current_price=Decimal("129.00"),
            observed_at=observed_at,
        ),
    ]


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    import app.config as config_module
    import app.db as db_module
    import app.main as main_module
    import app.repository as repository_module

    config_module.get_settings.cache_clear()
    importlib.reload(config_module)
    importlib.reload(db_module)
    importlib.reload(repository_module)
    importlib.reload(main_module)
    db_module.Base.metadata.create_all(db_module.engine)
    monkeypatch.setattr(main_module, "collect_fixture", lambda _: sample_records())
    with TestClient(main_module.app) as test_client:
        yield test_client
    config_module.get_settings.cache_clear()


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_crawl_is_idempotent_and_exposes_products(client):
    first = client.post("/api/crawl/fixture")
    second = client.post("/api/crawl/fixture")
    assert first.status_code == 201
    assert second.status_code == 201

    response = client.get("/api/products?page_size=10")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3


def test_product_filter_and_not_found(client):
    client.post("/api/crawl/fixture")
    response = client.get("/api/products?category=家居")
    assert response.status_code == 200
    assert response.json()["total"] == 2

    missing = client.get("/api/products/9999")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "PRODUCT_NOT_FOUND"


def test_price_history_starts_once_for_duplicate_crawl(client):
    client.post("/api/crawl/fixture")
    client.post("/api/crawl/fixture")
    products = client.get("/api/products").json()["items"]
    tea = next(item for item in products if item["external_product_id"] == "tea-001")
    history = client.get(f"/api/products/{tea['id']}/price-history")
    assert history.status_code == 200
    assert len(history.json()) == 1


def test_crawl_failure_is_recorded(client, monkeypatch):
    import app.crawler as crawler_module
    import app.main as main_module

    def fail(_):
        raise crawler_module.CrawlError("FIXTURE_BROKEN", "fixture 无效")

    monkeypatch.setattr(main_module, "collect_fixture", fail)
    response = client.post("/api/crawl/fixture")
    assert response.status_code == 422
    jobs = client.get("/api/crawl/jobs").json()
    assert jobs[0]["status"] == "failed"
    assert jobs[0]["error_code"] == "FIXTURE_BROKEN"


def test_price_change_adds_history_entry(client, monkeypatch):
    client.post("/api/crawl/fixture")
    changed = sample_records()
    changed[0] = changed[0].model_copy(update={"current_price": Decimal("79.90")})

    import app.main as main_module
    monkeypatch.setattr(main_module, "collect_fixture", lambda _: changed)
    client.post("/api/crawl/fixture")
    tea = next(
        item for item in client.get("/api/products").json()["items"]
        if item["external_product_id"] == "tea-001"
    )
    history = client.get(f"/api/products/{tea['id']}/price-history")
    assert [item["price"] for item in history.json()] == ["89.90", "79.90"]
