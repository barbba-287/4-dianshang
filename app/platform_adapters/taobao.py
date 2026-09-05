"""Taobao read-only response mapping and injectable transport.

The adapter is disabled by default and never performs a network request unless
an explicitly enabled transport is injected by a future authorized runtime.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from app.connectors import ExternalOrderRecord, InventorySnapshotRecord, normalize_inventory_row, normalize_order_row
from app.platform_adapters.base import AdapterError, AdapterPage


def fixture_transport(content: str, *, resource: str) -> Callable[..., dict[str, Any]]:
    """Build an in-memory transport from an inline, already supplied fixture."""
    import json
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise AdapterError("TAOBAO_BAD_RESPONSE", "淘宝 fixture JSON 无效") from exc
    if not isinstance(payload, dict):
        raise AdapterError("TAOBAO_BAD_RESPONSE", "淘宝 fixture 必须是对象")
    expected = payload.get("resource")
    if expected is not None and expected != resource:
        raise AdapterError("TAOBAO_BAD_RESPONSE", "淘宝 fixture resource 不匹配")
    def transport(**_kwargs: Any) -> dict[str, Any]:
        return payload
    return transport


class TaobaoAdapter:
    platform = "taobao"
    read_only = True

    def __init__(self, *, enabled: bool = False, transport: Callable[..., dict[str, Any]] | None = None, simulated: bool = True, max_page_size: int = 100):
        self.live_enabled = bool(enabled and transport is not None and not simulated)
        self.simulated = simulated
        self._transport = transport
        self.max_page_size = max_page_size

    def _request(self, *, resource: str, account_ref: str, store_ref: str, cursor: str | None, **params: Any) -> dict[str, Any]:
        if self._transport is None or not self.live_enabled:
            raise AdapterError("TAOBAO_ADAPTER_DISABLED", "淘宝只读适配器未启用")
        try:
            response = self._transport(resource=resource, account_ref=account_ref, store_ref=store_ref, cursor=cursor, params=params)
        except TimeoutError as exc:
            raise AdapterError("TAOBAO_TIMEOUT", "淘宝请求超时", retryable=True) from exc
        except ConnectionError as exc:
            raise AdapterError("TAOBAO_TRANSPORT_ERROR", "淘宝请求失败", retryable=True) from exc
        if not isinstance(response, dict):
            raise AdapterError("TAOBAO_BAD_RESPONSE", "淘宝响应不是对象")
        if response.get("error_code"):
            code = str(response["error_code"])
            retryable = code in {"429", "500", "502", "503", "504"}
            raise AdapterError("TAOBAO_RATE_LIMITED" if code == "429" else "TAOBAO_BAD_RESPONSE", str(response.get("error_message") or code), retryable=retryable)
        return response

    @staticmethod
    def _rows(response: dict[str, Any], *, resource: str) -> list[dict[str, Any]]:
        rows = response.get("records", response.get("items", []))
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise AdapterError("TAOBAO_BAD_RESPONSE", f"淘宝 {resource} records 无效")
        return rows

    def fetch_orders(self, *, account_ref: str, store_ref: str, cursor: str | None = None, updated_from: str | None = None, updated_to: str | None = None) -> AdapterPage:
        response = self._request(resource="orders", account_ref=account_ref, store_ref=store_ref, cursor=cursor, updated_from=updated_from, updated_to=updated_to)
        rows = self._rows(response, resource="orders")
        normalized = [normalize_order_row({**row, "account_ref": account_ref, "store_ref": store_ref, "simulated": self.simulated}, platform=self.platform, source_mode="mock" if self.simulated else "json", index=index) for index, row in enumerate(rows)]
        next_cursor = response.get("next_cursor")
        return AdapterPage(records=normalized, next_cursor=str(next_cursor) if next_cursor else None, has_more=bool(response.get("has_more")), request_id=str(response.get("request_id")) if response.get("request_id") else None, source_ref=response.get("source_ref"), data_completeness=str(response.get("data_completeness") or "complete"), metadata={"resource": "orders"})

    def fetch_inventory(self, *, account_ref: str, store_ref: str, cursor: str | None = None) -> AdapterPage:
        response = self._request(resource="inventory", account_ref=account_ref, store_ref=store_ref, cursor=cursor)
        rows = self._rows(response, resource="inventory")
        rows = [{**row, "account_ref": account_ref, "store_ref": store_ref, "simulated": self.simulated} for row in rows]
        normalized = [normalize_inventory_row(row, platform=self.platform, source_mode="mock" if self.simulated else "json", index=index) for index, row in enumerate(rows)]
        next_cursor = response.get("next_cursor")
        return AdapterPage(records=normalized, next_cursor=str(next_cursor) if next_cursor else None, has_more=bool(response.get("has_more")), request_id=str(response.get("request_id")) if response.get("request_id") else None, source_ref=response.get("source_ref"), data_completeness=str(response.get("data_completeness") or "complete"), metadata={"resource": "inventory"})

    def write(self, *_args: Any, **_kwargs: Any) -> None:
        raise AdapterError("TAOBAO_READ_ONLY_ONLY", "淘宝适配器仅支持只读")
