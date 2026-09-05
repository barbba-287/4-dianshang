"""Taobao adapter mapping and pagination tests."""
import json
from pathlib import Path

import pytest

from app.platform_adapters.base import AdapterError
from app.platform_adapters.taobao import TaobaoAdapter
from app.platform_sync import collect_pages, preview_adapter

FIXTURES = Path(__file__).parent / "fixtures" / "taobao"


def _fixture_transport(name):
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    def transport(**_kwargs):
        return payload
    return transport


def test_disabled_adapter_never_calls_transport():
    called = []
    adapter = TaobaoAdapter(enabled=False, transport=lambda **kwargs: called.append(kwargs))
    with pytest.raises(AdapterError, match="未启用"):
        adapter.fetch_orders(account_ref="a", store_ref="s")
    assert called == []


def test_taobao_order_fixture_maps_to_canonical_record():
    adapter = TaobaoAdapter(enabled=True, transport=_fixture_transport("orders_paid.json"), simulated=False)
    page = adapter.fetch_orders(account_ref="account-a", store_ref="store-a")
    assert page.has_more is False
    assert page.records[0].external_order_no == "TB-1001"
    assert page.records[0].order_status == "paid"
    assert page.records[0].lines[0].ordered_qty == 2
    assert page.records[0].simulated is False


def test_taobao_inventory_fixture_maps_to_snapshot():
    adapter = TaobaoAdapter(enabled=True, transport=_fixture_transport("inventory.json"), simulated=False)
    page = adapter.fetch_inventory(account_ref="account-a", store_ref="store-a")
    assert page.records[0].external_sku == "TB-SKU-1"
    assert page.records[0].available_qty == 7
    assert page.records[0].warehouse_ref == "TB-W-1"


def test_preview_collects_pages_and_rejects_repeated_cursor():
    calls = []
    def transport(**kwargs):
        calls.append(kwargs["cursor"])
        return {"records": [], "has_more": True, "next_cursor": "same"}
    adapter = TaobaoAdapter(enabled=True, transport=transport, simulated=False)
    with pytest.raises(AdapterError, match="游标"):
        preview_adapter(adapter, account_ref="a", store_ref="s", resource="orders", max_pages=3)
    assert calls == [None, "same"]


def test_adapter_is_read_only():
    adapter = TaobaoAdapter()
    with pytest.raises(AdapterError, match="只读"):
        adapter.write("cancel-order")
