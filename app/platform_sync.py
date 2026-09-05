"""Paginated orchestration for read-only platform adapters."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.platform_adapters.base import AdapterError, AdapterPage, ReadOnlyPlatformAdapter
from app.connectors import load_orders, load_records
from app.external_orders import ingest_orders
from app.external_sync import ImportStats, begin_sync_run, complete_sync_run, fail_sync_run, ingest_inventory


def run_fixture_sync(
    db,
    *,
    workspace_id: int,
    platform: str,
    account_ref: str,
    store_ref: str = "default",
    orders_content: str | bytes | None = None,
    inventory_content: str | bytes | None = None,
    source_mode: str = "mock",
):
    if not workspace_id:
        raise ValueError("WORKSPACE_CONTEXT_REQUIRED")
    if not orders_content and not inventory_content:
        raise ValueError("FIXTURE_CONTENT_REQUIRED")
    if source_mode not in {"json", "csv", "mock"}:
        raise ValueError("UNSUPPORTED_SOURCE_MODE")
    if orders_content and inventory_content:
        sync_type = "fixture_bundle"
    elif orders_content:
        sync_type = "orders"
    else:
        sync_type = "inventory"
    run = begin_sync_run(db, workspace_id=workspace_id, platform=platform, sync_type=sync_type, account_ref=account_ref, store_ref=store_ref, source_mode=source_mode, simulated=True)
    try:
        order_stats = None
        inventory_stats = None
        if orders_content:
            records = load_orders(orders_content, platform=platform, source_mode=source_mode)
            if any(row.account_ref != account_ref or row.store_ref != store_ref for row in records):
                raise ValueError("MIXED_EXTERNAL_ACCOUNT")
            order_stats = ingest_orders(db, records, workspace_id=workspace_id, sync_run_id=run.run_id)
        if inventory_content:
            records = load_records(inventory_content, platform=platform, source_mode=source_mode)
            if any(row.account_ref != account_ref or (row.store_ref or "default") != store_ref for row in records):
                raise ValueError("MIXED_EXTERNAL_ACCOUNT")
            inventory_stats = ingest_inventory(db, records, workspace_id=workspace_id, sync_run_id=run.run_id)
        total = (order_stats.total if order_stats else 0) + (inventory_stats.total if inventory_stats else 0)
        inserted = (order_stats.inserted if order_stats else 0) + (inventory_stats.inserted if inventory_stats else 0)
        no_op = (order_stats.no_op if order_stats else 0) + (inventory_stats.no_op if inventory_stats else 0)
        conflict = (order_stats.conflict if order_stats else 0) + (inventory_stats.conflict if inventory_stats else 0)
        complete_sync_run(db, run, ImportStats(total=total, inserted=inserted, no_op=no_op, conflict=conflict))
        return {"run": run, "orders": order_stats, "inventory": inventory_stats}
    except Exception as exc:
        fail_sync_run(db, run, exc)
        raise


def collect_pages(fetch: Callable[..., AdapterPage], *, max_pages: int = 100, **kwargs: Any) -> list:
    cursor = None
    seen: set[str] = set()
    records = []
    for _ in range(max_pages):
        page = fetch(cursor=cursor, **kwargs)
        records.extend(page.records)
        if not page.has_more:
            return records
        if not page.next_cursor or page.next_cursor in seen:
            raise AdapterError("TAOBAO_CURSOR_LOOP", "平台分页游标未前进")
        seen.add(page.next_cursor)
        cursor = page.next_cursor
    raise AdapterError("TAOBAO_PARTIAL_SYNC", "平台分页超过最大页数", retryable=False)


def preview_adapter(adapter: ReadOnlyPlatformAdapter, *, account_ref: str, store_ref: str, resource: str, max_pages: int = 100, **kwargs: Any) -> dict:
    if resource == "orders":
        records = collect_pages(adapter.fetch_orders, account_ref=account_ref, store_ref=store_ref, max_pages=max_pages, **kwargs)
    elif resource == "inventory":
        records = collect_pages(adapter.fetch_inventory, account_ref=account_ref, store_ref=store_ref, max_pages=max_pages, **kwargs)
    else:
        raise ValueError("UNSUPPORTED_ADAPTER_RESOURCE")
    return {
        "platform": adapter.platform,
        "read_only": adapter.read_only,
        "live_enabled": adapter.live_enabled,
        "simulated": adapter.simulated,
        "resource": resource,
        "total": len(records),
        "records": [record.as_dict() for record in records],
    }
