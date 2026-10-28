"""连接器标准化与离线导入回归测试。"""

from app.connectors import ConnectorError, connector_capabilities, load_events, load_orders, load_records, normalize_inventory_row


def test_all_candidate_platforms_are_read_only():
    """能力条目如实反映状态（P2-1 能力真实化后的收敛规格）。

    旧断言锁的是"六个平台一律 live_enabled=False / simulated=True"这份**写死假数据**，
    能力真实化后按 Q3 裁定翻转：现在锁的是——
    ① 只读恒真；② 未接适配器实现的平台显式带 `implemented=False`；
    ③ shopify 的 live 状态与运行期开关（.env 配置 + 进程内热开关）一致；
    ④ taobao 如实报骨架态（未接真实 transport）。
    """
    from app.config import get_settings
    from app.runtime_flags import live_enabled as runtime_live_enabled

    items = {item["platform"]: item for item in connector_capabilities()}
    assert {"taobao", "jd", "pdd", "douyin", "amazon"} <= set(items)
    assert all(item["read_only"] for item in items.values())
    for platform in ("jd", "pdd", "douyin", "amazon"):
        assert items[platform]["implemented"] is False
    settings = get_settings()
    live_allowed = bool(settings.shopify_live_enabled and runtime_live_enabled())
    assert items["shopify"]["live_enabled"] is live_allowed
    assert items["shopify"]["simulated"] is (not live_allowed)
    assert items["taobao"]["live_enabled"] is False
    assert items["taobao"]["simulated"] is True


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


def test_offline_json_order_and_event_inputs_are_simulated_even_if_payload_claims_live():
    order = {
        "account_ref": "demo", "external_order_no": "O-1", "order_status": "paid",
        "external_created_at": "2026-09-01T08:00:00Z", "lines": [{"external_sku": "S-1", "ordered_qty": 1}],
        "simulated": False,
    }
    event = {
        "account_ref": "demo", "external_event_id": "E-1", "event_type": "order.updated",
        "occurred_at": "2026-09-01T08:00:00Z", "payload": {}, "simulated": False,
    }
    orders = load_orders(__import__("json").dumps([order]), platform="jd", source_mode="json")
    events = load_events(__import__("json").dumps([event]), platform="jd", source_mode="json")
    inventory = load_records(
        '{"account_ref":"demo","as_of":"2026-09-01T08:00:00Z","records":[{"external_sku":"S-1","available_qty":9,"simulated":false}]}',
        platform="jd", source_mode="json",
    )
    assert orders[0].simulated is True
    assert events[0].simulated is True
    assert inventory[0].simulated is True


def test_explicit_shopify_live_normalization_is_not_simulated():
    row = {"account_ref": "store.myshopify.com", "external_sku": "S-1", "available_qty": 9, "as_of": "2026-09-01T08:00:00Z"}
    record = normalize_inventory_row(row, platform="shopify", source_mode="shopify_live", index=0)
    assert record.simulated is False
    assert record.source_mode == "shopify_live"
    with __import__("pytest").raises(ConnectorError, match="必须通过已授权的 Shopify Adapter"):
        load_records(__import__("json").dumps([row]), platform="shopify", source_mode="shopify_live")


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
