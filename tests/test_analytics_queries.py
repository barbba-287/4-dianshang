"""AnalyticsQueryService 应用编排契约测试。"""
from __future__ import annotations

from datetime import date

import pytest

from app.analytics_queries import AnalyticsQueryError, AnalyticsQueryService


class _FakeSession:
    pass


def test_sales_passes_authenticated_workspace_to_builder(monkeypatch):
    captured = {}

    def fake_builder(db, **kwargs):
        captured.update(db=db, **kwargs)
        return {"as_of": "2026-10-08", "windows": {}}

    monkeypatch.setattr("app.analytics.build_sales_summary", fake_builder)
    db = _FakeSession()
    result = AnalyticsQueryService(db).sales(workspace_id=23, as_of=date(2026, 10, 8))

    assert result["windows"] == {}
    assert captured == {"db": db, "workspace_id": 23, "as_of": date(2026, 10, 8)}


def test_inventory_health_owns_page_offset_and_page_metadata(monkeypatch):
    captured = {}

    def fake_builder(db, **kwargs):
        captured.update(db=db, **kwargs)
        return {"items": [{"sku_id": 1}], "page": 1, "page_size": 3, "summary": {}}

    monkeypatch.setattr("app.analytics.build_inventory_health", fake_builder)
    result = AnalyticsQueryService(_FakeSession()).inventory_health(
        workspace_id=8, warehouse_id=4, page=3, page_size=3
    )

    assert captured["workspace_id"] == 8
    assert captured["warehouse_id"] == 4
    assert captured["limit"] == 3
    assert captured["offset"] == 6
    assert result["page"] == 3
    assert result["page_size"] == 3


def test_product_quadrant_builds_meta_and_preserves_reported_page(monkeypatch):
    captured = {}
    item = {"data_completeness": "complete", "sku_id": 1}

    def fake_builder(db, **kwargs):
        captured.update(db=db, **kwargs)
        return {
            "as_of": "2026-10-08",
            "business_timezone": "UTC",
            "growth_window": 7,
            "baseline_window": 14,
            "days_window": 14,
            "growth_high": 0.1,
            "days_low": 14.0,
            "summary": {"total_scored": 1},
            "items": [item],
            "limitations": ["fixture"],
            "unsupported_metrics": [],
        }

    monkeypatch.setattr("app.analytics.build_product_quadrant", fake_builder)
    result = AnalyticsQueryService(_FakeSession()).product_quadrant(
        workspace_id=11, warehouse_id=9, page=2, page_size=1
    )

    assert captured["workspace_id"] == 11
    assert captured["warehouse_id"] == 9
    assert result["items"] == [item]
    assert result["total"] == 1
    assert result["page"] == 2
    assert result["page_size"] == 1
    assert result["meta"]["workspace_id"] == 11
    assert result["meta"]["data_completeness"] == "complete"
    assert result["meta"]["metrics"][0]["metric"] == "product_quadrant_items"


def test_product_quadrant_converts_builder_value_error(monkeypatch):
    def fake_builder(*args, **kwargs):
        raise ValueError("INVALID_GROWTH_WINDOW")

    monkeypatch.setattr("app.analytics.build_product_quadrant", fake_builder)

    with pytest.raises(AnalyticsQueryError, match="INVALID_GROWTH_WINDOW"):
        AnalyticsQueryService(_FakeSession()).product_quadrant(workspace_id=1)


def test_dashboard_summary_passes_workspace_without_reinterpreting_scope(monkeypatch):
    captured = {}

    def fake_builder(db, **kwargs):
        captured.update(db=db, **kwargs)
        return {"kpis": {}}

    monkeypatch.setattr("app.dashboard.build_dashboard_summary", fake_builder)
    db = _FakeSession()
    assert AnalyticsQueryService(db).dashboard_summary(
        workspace_id=31, days=14, warehouse_id=6, sku_id=7, recent_limit=5
    ) == {"kpis": {}}
    assert captured == {
        "db": db,
        "workspace_id": 31,
        "days": 14,
        "as_of": None,
        "warehouse_id": 6,
        "sku_id": 7,
        "recent_limit": 5,
    }
