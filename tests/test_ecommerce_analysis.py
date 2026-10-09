from app.skills_ecommerce import SkillContext, analyze_ad_roi, analyze_sales_fluctuation, check_inventory_alert, find_product_opportunities
from app.skills_ecommerce_fixture import MockEcomDataSource


def context():
    return SkillContext(workspace_id=1, actor="test", request_id="req-test", source_mode="mock")


def test_sales_fluctuation_has_completeness_and_evidence():
    result = analyze_sales_fluctuation(sku_id=1, days=7, context=context(), source=MockEcomDataSource())
    assert result["metric"] == "sales_fluctuation"
    assert result["data_completeness"] == "complete"
    assert "evidence" in result


def test_ad_roi_returns_calculated_metric():
    result = analyze_ad_roi(campaign_id="c-1", context=context(), source=MockEcomDataSource())
    assert result["roi"] == 2.6
    assert result["status"] == "known"


def test_inventory_alert_is_read_only_analysis():
    result = check_inventory_alert(warehouse_id=1, context=context(), source=MockEcomDataSource())
    assert result["alert_count"] == 1
    assert result["alerts"][0]["severity"] == "warning"


def test_product_opportunities_are_evidence_bound():
    result = find_product_opportunities(category="茶饮", context=context(), source=MockEcomDataSource())
    assert result["count"] == 1
    assert result["items"][0]["reason"] == "GROWTH_WITH_LOW_COVERAGE"
