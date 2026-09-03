"""连接器标准化与离线导入回归测试。"""

from app.connectors import ConnectorError, connector_capabilities, load_records


def test_all_candidate_platforms_are_read_only():
    platforms = {item["platform"] for item in connector_capabilities()}
    assert {"taobao", "jd", "pdd", "douyin", "amazon"} <= platforms
    assert all(item["read_only"] and not item["live_enabled"] and item["simulated"] for item in connector_capabilities())


def test_json_envelope_normalizes_defaults_and_amazon_fields():
    records = load_records(
        '{"account_ref":"demo","warehouse_ref":"OWN-01","as_of":"2026-09-01T08:00:00Z","records":[{"external_sku":"S-1","available_qty":9,"asin":"B-DEMO"}]}',
        platform="amazon",
        source_mode="json",
    )
    assert records[0].account_ref == "demo"
    assert records[0].warehouse_ref == "OWN-01"
    assert records[0].asin == "B-DEMO"
    assert records[0].available_qty == 9
    assert records[0].simulated is True


def test_csv_normalizes_envelope_columns():
    content = "platform,account_ref,warehouse_ref,as_of,external_sku,available_qty,reserved_qty,inbound_qty\njd,shop,OWN-01,2026-09-01T08:00:00Z,S-1,4,1,2\n"
    records = load_records(content, platform="jd", source_mode="csv")
    assert len(records) == 1
    assert records[0].available_qty == 4
    assert records[0].payload_hash.startswith("sha256:")


def test_normalization_is_deterministic_and_rejects_invalid_quantity():
    content = '{"account_ref":"demo","as_of":"2026-09-01T08:00:00Z","records":[{"external_sku":"S-1","available_qty":1}]}'
    first = load_records(content, platform="taobao", source_mode="json")[0]
    second = load_records(content, platform="taobao", source_mode="json")[0]
    assert first.payload_hash == second.payload_hash
    try:
        load_records('{"account_ref":"demo","as_of":"2026-09-01T08:00:00Z","records":[{"external_sku":"S-1","available_qty":-1}]}', platform="taobao", source_mode="json")
    except ConnectorError as exc:
        assert "available_qty" in str(exc)
    else:
        raise AssertionError("negative quantity should be rejected")
