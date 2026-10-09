"""Shopify Admin GraphQL read-only adapter and transports."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from app.platform_adapters.base import AdapterError

SHOPIFY_API_VERSION = "2026-07"


class ShopifyTransport(Protocol):
    def query(self, *, query: str, variables: dict, request_id: str) -> dict[str, Any]: ...


def canonical_store_domain(value: str) -> str:
    value = value.strip().lower()
    if "://" in value:
        value = urlparse(value).netloc
    value = value.split("/", 1)[0].split(":", 1)[0]
    if not value or not value.endswith(".myshopify.com") or any(c.isspace() for c in value):
        raise ValueError("SHOPIFY_STORE_DOMAIN_INVALID")
    return value


@dataclass
class ShopifyLiveTransport:
    store_domain: str
    access_token: str
    api_version: str = SHOPIFY_API_VERSION
    timeout_seconds: float = 20.0
    max_retries: int = 2

    def __post_init__(self):
        self.store_domain = canonical_store_domain(self.store_domain)
        if not self.access_token:
            raise ValueError("SHOPIFY_ACCESS_TOKEN_REQUIRED")
        self.endpoint = f"https://{self.store_domain}/admin/api/{self.api_version}/graphql.json"

    def query(self, *, query: str, variables: dict, request_id: str) -> dict[str, Any]:
        if "mutation" in query.lower():
            raise AdapterError("SHOPIFY_READ_ONLY_ONLY", "Shopify adapter 仅支持只读")
        body = {"query": query, "variables": variables}
        for attempt in range(self.max_retries + 1):
            try:
                response = httpx.post(self.endpoint, json=body, headers={"X-Shopify-Access-Token": self.access_token, "User-Agent": "dianshang-shopify-readonly/0.1", "X-Request-ID": request_id}, timeout=self.timeout_seconds)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise AdapterError("SHOPIFY_TIMEOUT", "Shopify 请求超时", retryable=True) from exc
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.max_retries:
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else min(2 ** attempt, 8)
                    time.sleep(delay)
                    continue
                raise AdapterError("SHOPIFY_RATE_LIMITED" if response.status_code == 429 else "SHOPIFY_UPSTREAM_ERROR", "Shopify 上游暂时不可用", retryable=True)
            if response.status_code in {401, 403}:
                raise AdapterError("SHOPIFY_AUTH_FAILED", "Shopify token 或权限无效")
            if response.status_code >= 400:
                raise AdapterError("SHOPIFY_HTTP_ERROR", "Shopify 请求被拒绝")
            try:
                data = response.json()
            except ValueError as exc:
                raise AdapterError("SHOPIFY_BAD_RESPONSE", "Shopify 响应不是 JSON") from exc
            if data.get("errors"):
                raise AdapterError("SHOPIFY_GRAPHQL_ERROR", "Shopify GraphQL 返回业务错误")
            return {**data, "request_id": request_id}
        raise AdapterError("SHOPIFY_UPSTREAM_ERROR", "Shopify 请求失败", retryable=True)


@dataclass
class ShopifyFixtureTransport:
    responses: dict[str, dict[str, Any]]

    def query(self, *, query: str, variables: dict, request_id: str) -> dict[str, Any]:
        if "mutation" in query.lower():
            raise AdapterError("SHOPIFY_READ_ONLY_ONLY", "Shopify adapter 仅支持只读")
        key = "inventory" if "inventoryItems(" in query else "orders" if "orders(" in query else "products"
        if key not in self.responses:
            raise AdapterError("SHOPIFY_FIXTURE_MISSING", f"缺少 fixture: {key}")
        return {**self.responses[key], "request_id": request_id}


PRODUCTS_QUERY = """query Products($first: Int!, $after: String) { products(first: $first, after: $after) { edges { cursor node { id title handle descriptionHtml updatedAt variants(first: 100) { nodes { id sku barcode title price } } } } pageInfo { hasNextPage endCursor } } }"""
ORDERS_QUERY = """query Orders($first: Int!, $after: String, $query: String) { orders(first: $first, after: $after, query: $query, sortKey: UPDATED_AT) { edges { cursor node { id name createdAt updatedAt processedAt displayFinancialStatus displayFulfillmentStatus currentTotalPriceSet { shopMoney { amount currencyCode } } lineItems(first: 100) { nodes { id sku quantity originalTotalSet { shopMoney { amount currencyCode } } variant { id title } } } } } pageInfo { hasNextPage endCursor } } }"""
INVENTORY_QUERY = """query Inventory($first: Int!, $after: String) { inventoryItems(first: $first, after: $after) { edges { node { id sku inventoryLevels(first: 100) { edges { node { id quantities(names: [\"available\"]) { name quantity } location { id } } } } } } pageInfo { hasNextPage endCursor } } }"""


class ShopifyReadOnlyAdapter:
    platform = "shopify"
    read_only = True
    live_enabled = False
    simulated = True

    def __init__(self, transport: ShopifyTransport, *, live_enabled=False, simulated=True, page_size=50, max_pages=100):
        self._transport = transport
        self.live_enabled = bool(live_enabled and not simulated)
        self.simulated = simulated
        self.page_size = min(page_size, 100)
        self.max_pages = max_pages

    def _pages(self, query: str, data_key: str, variables: dict | None = None) -> list[dict]:
        cursor = None
        seen: set[str] = set()
        records: list[dict] = []
        for _ in range(self.max_pages):
            request_id = f"shopify-{uuid.uuid4().hex[:12]}"
            response = self._transport.query(query=query, variables={"first": self.page_size, "after": cursor, **(variables or {})}, request_id=request_id)
            connection = ((response.get("data") or {}).get(data_key) or {})
            records.extend(edge.get("node") for edge in connection.get("edges", []) if isinstance(edge, dict) and isinstance(edge.get("node"), dict))
            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return records
            next_cursor = page_info.get("endCursor")
            if not next_cursor or next_cursor in seen:
                raise AdapterError("SHOPIFY_CURSOR_LOOP", "Shopify 分页游标未前进")
            seen.add(next_cursor)
            cursor = next_cursor
        raise AdapterError("SHOPIFY_PAGE_LIMIT", "Shopify 分页超过上限")

    def fetch_products(self):
        return self._pages(PRODUCTS_QUERY, "products")

    def fetch_orders(self, *, updated_query: str | None = None):
        return self._pages(ORDERS_QUERY, "orders", {"query": updated_query})

    def inventory_records(self) -> list[dict]:
        return self._pages(INVENTORY_QUERY, "inventoryItems")

    def fetch_inventory(self):
        records = []
        observed_at = datetime.now(timezone.utc)
        received_at = observed_at
        for item in self.inventory_records():
            for edge in (item.get("inventoryLevels") or {}).get("edges", []):
                level = edge.get("node") or {}
                quantities = {
                    quantity.get("name"): quantity.get("quantity")
                    for quantity in level.get("quantities", [])
                    if isinstance(quantity, dict)
                }
                location = (level.get("location") or {}).get("id")
                external_item = item.get("id") or "unknown"
                external_sku = item.get("sku")
                # A missing Shopify SKU is not a valid internal SKU. Keep the
                # stable InventoryItem GID as the external identity instead.
                external_sku = external_sku.strip() if isinstance(external_sku, str) and external_sku.strip() else external_item
                available = quantities.get("available")
                if available is None:
                    # A level without an available quantity is incomplete, but
                    # it is still retained for audit rather than being hidden.
                    available = 0
                raw_level = {"item": item, "level": level}
                payload_hash = "sha256:" + hashlib.sha256(
                    json.dumps(raw_level, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                observed_key = observed_at.isoformat()
                records.append({
                    "schema_version": "shopify.v1",
                    "platform": "shopify",
                    "account_ref": getattr(self._transport, "store_domain", "shopify"),
                    "store_ref": getattr(self._transport, "store_domain", "shopify"),
                    "marketplace": "shopify",
                    "warehouse_ref": location,
                    "external_sku": external_sku,
                    "available_qty": int(available),
                    "reserved_qty": 0,
                    "inbound_qty": 0,
                    "as_of": observed_key,
                    "received_at": received_at.isoformat(),
                    "payload_hash": payload_hash,
                    # The same upstream state must replay as a no-op; a changed
                    # quantity gets a new payload hash and is retained as a new
                    # immutable observation.
                    "idempotency_key": f"shopify:{external_item}:{location or 'unknown'}:{payload_hash}",
                    "raw_ref": f"shopify://inventory/{external_item}/{location or 'unknown'}",
                    "source_mode": "shopify_live" if self.live_enabled else "mock",
                    "simulated": self.simulated,
                })
        return records

    def write(self, *_args, **_kwargs):
        raise AdapterError("SHOPIFY_READ_ONLY_ONLY", "Shopify adapter 仅支持只读")
