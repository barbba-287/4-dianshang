"""电商商品与客服工作台采集标准化测试。

用途：验证商品字段清洗、价格/评分解析和非法数据拦截，保证采集器输出
可以安全进入数据库层。
"""

from decimal import Decimal

import pytest

from app.crawler import CrawlError, normalize_product


def test_normalize_product_cleans_fields_and_parses_currency():
    record = normalize_product(
        {
            "source": "fixture",
            "external_product_id": " tea-001 ",
            "title": " 高山绿茶 ",
            "url": "/products/tea-001",
            "category": " 茶饮 ",
            "description": " 清香回甘 ",
            "rating": "4.8分",
            "current_price": "￥1,289.90",
            "currency": "cny",
        },
        "https://fixture.local",
    )

    assert record.external_product_id == "tea-001"
    assert record.title == "高山绿茶"
    assert str(record.url) == "https://fixture.local/products/tea-001"
    assert record.current_price == Decimal("1289.90")
    assert record.rating == Decimal("4.8")
    assert record.currency == "CNY"


def test_normalize_product_rejects_invalid_price():
    with pytest.raises(CrawlError, match="无法解析价格"):
        normalize_product(
            {
                "source": "fixture",
                "external_product_id": "tea-001",
                "title": "高山绿茶",
                "url": "/products/tea-001",
                "current_price": "待询价",
            },
            "https://fixture.local",
        )


def test_normalize_product_rejects_missing_identity():
    with pytest.raises(CrawlError, match="external_product_id"):
        normalize_product(
            {
                "source": "fixture",
                "external_product_id": "",
                "title": "高山绿茶",
                "url": "/products/tea-001",
                "current_price": "89.90",
            },
            "https://fixture.local",
        )
