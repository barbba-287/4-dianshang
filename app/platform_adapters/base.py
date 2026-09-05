"""Read-only platform adapter contracts."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from app.connectors import ExternalOrderRecord, InventorySnapshotRecord


class AdapterError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class AdapterPage:
    records: Sequence[ExternalOrderRecord | InventorySnapshotRecord]
    next_cursor: str | None = None
    has_more: bool = False
    request_id: str | None = None
    source_ref: str | None = None
    data_completeness: str = "complete"
    metadata: dict[str, Any] = field(default_factory=dict)


class ReadOnlyPlatformAdapter(Protocol):
    platform: str
    read_only: bool
    live_enabled: bool
    simulated: bool

    def fetch_orders(self, *, account_ref: str, store_ref: str, cursor: str | None = None, updated_from: str | None = None, updated_to: str | None = None) -> AdapterPage: ...
    def fetch_inventory(self, *, account_ref: str, store_ref: str, cursor: str | None = None) -> AdapterPage: ...
