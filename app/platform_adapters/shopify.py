"""Shopify Admin GraphQL read-only adapter and transports."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

import httpx

from app.platform_adapters.base import AdapterError

SHOPIFY_API_VERSION = "2026-07"

logger = logging.getLogger(__name__)

# GraphQL 数据键 → 留存用的资源名（与台账 resource 名一致：orders/inventory）。
_RESOURCE_BY_DATA_KEY = {
    "orders": "orders",
    "inventoryItems": "inventory",
    "products": "products",
}


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
    # live 响应原样留存的落盘目录与单文件字节上限；两者都为 None 时行为与留存前完全一致。
    retention_dir: str | None = None
    retention_max_bytes: int | None = None

    def __post_init__(self):
        self.store_domain = canonical_store_domain(self.store_domain)
        if not self.access_token:
            raise ValueError("SHOPIFY_ACCESS_TOKEN_REQUIRED")
        self.endpoint = f"https://{self.store_domain}/admin/api/{self.api_version}/graphql.json"
        # 留存累计状态（对外可读）：正文文件数、最差状态、落盘目录。
        self.retention_files = 0
        self.retention_status = "ok"
        self._retention_by_resource: dict[str, dict] = {}

    def query(self, *, query: str, variables: dict, request_id: str) -> dict[str, Any]:
        if "mutation" in query.lower():
            raise AdapterError("SHOPIFY_READ_ONLY_ONLY", "Shopify adapter 仅支持只读")
        body = {"query": query, "variables": variables}
        started = time.monotonic()
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
            # 留存原始响应体（在把响应交出去之前落盘），失败只告警不打断同步。
            self._retain_response(
                request_id=request_id,
                response=response,
                query=query,
                variables=variables,
                attempt=attempt + 1,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            return {**data, "request_id": request_id}
        raise AdapterError("SHOPIFY_UPSTREAM_ERROR", "Shopify 请求失败", retryable=True)

    # ------------------------------------------------------------------
    # live 响应原样留存
    #
    # 布局：<retention_dir>/<request_id>.json            正文（response.text 的 UTF-8 字节原样）
    #       <retention_dir>/<request_id>.json.truncated   超限时只写前 N 字节（不用 .json 后缀，避免被误解析）
    #       <retention_dir>/<request_id>.meta.json       元数据（含 truncated/stored_bytes/original_size）
    #
    # 语义：留存是诊断能力，写盘失败降级为 warning + 台账 failed，同步照常成功。
    # ------------------------------------------------------------------
    def _retain_response(self, *, request_id: str, response: Any, query: str, variables: dict, attempt: int, elapsed_ms: int) -> None:
        if not self.retention_dir:
            return
        # resource 由适配器在每次 fetch 前设置；fixture transport 没有该属性时按未知处理。
        resource = getattr(self, "resource", None)
        raw = response.text.encode("utf-8")
        original_size = len(raw)
        limit = self.retention_max_bytes
        truncated = limit is not None and original_size > limit
        stored = raw[:limit] if truncated else raw
        body_name = f"{request_id}.json.truncated" if truncated else f"{request_id}.json"
        try:
            os.makedirs(self.retention_dir, exist_ok=True)
        except OSError as exc:
            logger.warning("Shopify live 响应留存建目录失败 request_id=%s resource=%s error=%s", request_id, resource, exc)
            self._record_retention(resource, files=0, status="failed")
            return
        try:
            with open(os.path.join(self.retention_dir, body_name), "wb") as handle:
                handle.write(stored)
        except OSError as exc:
            logger.warning("Shopify live 响应留存写正文失败 request_id=%s resource=%s error=%s", request_id, resource, exc)
            self._record_retention(resource, files=0, status="failed")
            return
        self._record_retention(resource, files=1, status="truncated" if truncated else "ok")
        meta = {
            "request_id": request_id,
            "attempt": attempt,
            "http_status": response.status_code,
            "elapsed_ms": elapsed_ms,
            "query": query,
            "variables": variables,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "truncated": truncated,
            "stored_bytes": len(stored),
            "original_size": original_size,
            "resource": resource,
        }
        try:
            with open(os.path.join(self.retention_dir, f"{request_id}.meta.json"), "w", encoding="utf-8") as handle:
                json.dump(meta, handle, ensure_ascii=False, sort_keys=True, indent=2)
        except OSError as exc:
            logger.warning("Shopify live 响应留存写元数据失败 request_id=%s resource=%s error=%s", request_id, resource, exc)
            self._record_retention(resource, files=0, status="failed")

    def _record_retention(self, resource: str | None, *, files: int, status: str) -> None:
        """更新累计与按资源的留存状态（failed 优先于 truncated，truncated 优先于 ok）。"""
        key = resource or "unknown"
        entry = self._retention_by_resource.setdefault(key, {"files": 0, "status": "ok"})
        entry["files"] += files
        if status == "failed":
            entry["status"] = "failed"
        elif status == "truncated" and entry["status"] == "ok":
            entry["status"] = "truncated"
        self.retention_files += files
        if status == "failed":
            self.retention_status = "failed"
        elif status == "truncated" and self.retention_status == "ok":
            self.retention_status = "truncated"

    def retention_snapshot(self, resource: str) -> dict:
        """取某个资源的留存快照（供台账留痕用）；无留存时 files=0。"""
        entry = self._retention_by_resource.get(resource) or {"files": 0, "status": "ok"}
        return {"status": entry["status"], "files": entry["files"], "dir": self.retention_dir}


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

    def __init__(
        self,
        transport: ShopifyTransport,
        *,
        live_enabled=False,
        simulated=True,
        page_size=50,
        max_pages=100,
        deadline: float | None = None,
        cancel_check: Callable[[], bool] | None = None,
        start_cursor: str | None = None,
        on_page_done: Callable[[str | None, int, int], None] | None = None,
    ):
        self._transport = transport
        self.live_enabled = bool(live_enabled and not simulated)
        self.simulated = simulated
        self.page_size = min(page_size, 100)
        self.max_pages = max_pages
        # 运行期护栏（可选）：run 级 wall-clock 预算与取消检查点。
        # 两者都缺省时 _pages() 的行为与引入护栏前完全一致。
        self._deadline = deadline
        self._cancel_check = cancel_check
        # R6：断点续跑的起点游标与"每页成功后"回调。两者都缺省时 _pages() 的
        # 行为与引入它们之前完全一致（沿用 P2-0a deadline/cancel_check 的兼容手法）。
        # on_page_done(cursor, pages, fetched)：cursor 是本页的 endCursor，
        # **取完时（hasNextPage=false）为 None**；pages/fetched 是本次分页的累计值。
        self.start_cursor = start_cursor
        self.on_page_done = on_page_done
        # 本次（最后一次）分页实际消耗的页数，供 run 留痕；见 pages_used。
        self.pages_used = 0

    def _check_guardrails(self) -> None:
        """分页检查点：取消优先，其次 wall-clock 预算。"""
        if self._cancel_check is not None and self._cancel_check():
            raise AdapterError("SHOPIFY_SYNC_CANCELLED", "同步已被请求取消")
        if self._deadline is not None and time.monotonic() > self._deadline:
            raise AdapterError("SHOPIFY_SYNC_BUDGET_EXCEEDED", "同步超出 wall-clock 预算")

    def _notify_page_done(self, cursor: str | None, pages: int, fetched: int) -> None:
        """每页成功后回调（R6 的游标落库钩子）。

        回调异常**不吞**：游标写不进去就是同步事实没落库，必须让本次同步失败，
        而不是假装成功。回调缺省时不做任何事（既有行为零变化）。
        """
        callback = self.on_page_done
        if callback is None:
            return
        callback(cursor, pages, fetched)

    def _pages(self, query: str, data_key: str, variables: dict | None = None) -> list[dict]:
        # start_cursor 是续跑起点（None = 从第一页开始，既有行为）。
        cursor = self.start_cursor
        seen: set[str] = set()
        if cursor:
            # 起点游标也计入已见集合：续跑时若上游把游标指回起点，能立刻判成环。
            seen.add(cursor)
        records: list[dict] = []
        pages_used = 0
        fetched = 0
        # 留存用的资源名（如 orders/inventory）；fixture transport 没有该属性也不报错。
        self._set_transport_resource(_RESOURCE_BY_DATA_KEY.get(data_key, data_key))
        try:
            for _ in range(self.max_pages):
                # 每页一次检查点：取消与 wall-clock 预算都在这里生效。
                self._check_guardrails()
                request_id = f"shopify-{uuid.uuid4().hex[:12]}"
                response = self._transport.query(query=query, variables={"first": self.page_size, "after": cursor, **(variables or {})}, request_id=request_id)
                pages_used += 1
                connection = ((response.get("data") or {}).get(data_key) or {})
                page_records = [
                    edge.get("node")
                    for edge in connection.get("edges", [])
                    if isinstance(edge, dict) and isinstance(edge.get("node"), dict)
                ]
                records.extend(page_records)
                fetched += len(page_records)
                page_info = connection.get("pageInfo") or {}
                if not page_info.get("hasNextPage"):
                    # 取完：游标为 null（complete=true），末页信息就此落库。
                    self._notify_page_done(None, pages_used, fetched)
                    return records
                next_cursor = page_info.get("endCursor")
                if not next_cursor or next_cursor in seen:
                    raise AdapterError("SHOPIFY_CURSOR_LOOP", "Shopify 分页游标未前进")
                seen.add(next_cursor)
                cursor = next_cursor
                self._notify_page_done(next_cursor, pages_used, fetched)
            raise AdapterError("SHOPIFY_PAGE_LIMIT", "Shopify 分页超过上限")
        finally:
            # 成功、失败、取消都留痕，避免预算/取消路径下页数丢失。
            self.pages_used = pages_used

    def _set_transport_resource(self, resource: str) -> None:
        """把当前资源名写到 transport 上，供 live 响应留存的 meta 记录。

        transport 不支持动态属性时静默跳过（fixture transport 与自定义实现都安全）。
        """
        try:
            setattr(self._transport, "resource", resource)
        except Exception:  # noqa: BLE001 - 留存元信息失败不得影响同步
            pass

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
