"""离线外部平台连接器：只解析和标准化，不访问网络。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
from typing import Any, Iterable, Protocol

PLATFORMS = ("taobao", "jd", "pdd", "douyin", "amazon")
MODES = ("json", "csv", "mock")


@dataclass(frozen=True)
class InventorySnapshotRecord:
    schema_version: str
    platform: str
    account_ref: str
    store_ref: str | None
    marketplace: str | None
    warehouse_ref: str | None
    external_sku: str
    internal_sku_code: str | None
    asin: str | None
    available_qty: int
    reserved_qty: int
    inbound_qty: int
    as_of: datetime
    received_at: datetime
    payload_hash: str
    idempotency_key: str
    raw_ref: str | None
    source_mode: str
    simulated: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "platform": self.platform,
            "account_ref": self.account_ref,
            "store_ref": self.store_ref,
            "marketplace": self.marketplace,
            "warehouse_ref": self.warehouse_ref,
            "external_sku": self.external_sku,
            "internal_sku_code": self.internal_sku_code,
            "asin": self.asin,
            "available_qty": self.available_qty,
            "reserved_qty": self.reserved_qty,
            "inbound_qty": self.inbound_qty,
            "as_of": self.as_of.isoformat(),
            "received_at": self.received_at.isoformat(),
            "payload_hash": self.payload_hash,
            "idempotency_key": self.idempotency_key,
            "raw_ref": self.raw_ref,
            "source_mode": self.source_mode,
            "simulated": self.simulated,
        }


@dataclass(frozen=True)
class CanonicalEvent:
    schema_version: str
    platform: str
    account_ref: str
    external_event_id: str
    event_type: str
    external_object_no: str | None
    event_version: int | None
    occurred_at: datetime
    received_at: datetime
    payload_hash: str
    idempotency_key: str
    payload: dict[str, Any]
    raw_ref: str | None
    source_mode: str
    simulated: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "platform": self.platform,
            "account_ref": self.account_ref,
            "external_event_id": self.external_event_id,
            "event_type": self.event_type,
            "external_object_no": self.external_object_no,
            "event_version": self.event_version,
            "occurred_at": self.occurred_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "payload_hash": self.payload_hash,
            "idempotency_key": self.idempotency_key,
            "payload": self.payload,
            "raw_ref": self.raw_ref,
            "source_mode": self.source_mode,
            "simulated": self.simulated,
        }


class ConnectorError(ValueError):
    pass


class PlatformConnector(Protocol):
    platform: str
    source_mode: str
    read_only: bool
    live_enabled: bool
    simulated: bool

    def inventory_snapshots(self) -> list[InventorySnapshotRecord]: ...

    def events(self) -> list[CanonicalEvent]: ...


def _parse_time(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ConnectorError(f"{field} 必须是 ISO-8601 时间")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ConnectorError(f"{field} 时间格式无效") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _required_text(row: dict[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ConnectorError(f"{field} 不能为空")
    return value.strip()


def _nonnegative_int(row: dict[str, Any], field: str) -> int:
    value = row.get(field, 0)
    if isinstance(value, bool):
        raise ConnectorError(f"{field} 必须是非负整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConnectorError(f"{field} 必须是非负整数") from exc
    if parsed < 0:
        raise ConnectorError(f"{field} 必须是非负整数")
    if isinstance(value, str) and str(parsed) != value.strip():
        raise ConnectorError(f"{field} 必须是非负整数")
    return parsed


def _apply_envelope_defaults(rows: list[dict[str, Any]], envelope: dict[str, Any]) -> list[dict[str, Any]]:
    defaults = {key: envelope.get(key) for key in ("schema_version", "platform", "account_ref", "store_ref", "warehouse_ref", "as_of", "received_at", "marketplace")}
    return [{**defaults, **row} for row in rows]


def _payload_hash(row: dict[str, Any]) -> str:
    encoded = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def normalize_inventory_row(row: dict[str, Any], *, platform: str, source_mode: str, index: int) -> InventorySnapshotRecord:
    if platform not in PLATFORMS:
        raise ConnectorError(f"不支持的平台: {platform}")
    if source_mode not in MODES:
        raise ConnectorError(f"不支持的数据模式: {source_mode}")
    external_sku = _required_text(row, "external_sku")
    account_ref = _required_text(row, "account_ref")
    as_of = _parse_time(row.get("as_of") or row.get("captured_at"), field="as_of")
    received_at = _parse_time(row.get("received_at") or row.get("as_of") or row.get("captured_at"), field="received_at")
    key = str(row.get("idempotency_key") or f"{platform}:{account_ref}:{external_sku}:{as_of.isoformat()}")
    warehouse_ref = row.get("warehouse_ref") or row.get("external_warehouse_ref")
    inbound_qty = row.get("inbound_qty", row.get("in_transit_qty", 0))
    return InventorySnapshotRecord(
        schema_version=str(row.get("schema_version") or "1"),
        platform=platform,
        account_ref=account_ref,
        store_ref=str(row["store_ref"]).strip() if row.get("store_ref") is not None else None,
        marketplace=str(row["marketplace"]).strip() if row.get("marketplace") is not None else None,
        warehouse_ref=str(warehouse_ref).strip() if warehouse_ref is not None else None,
        external_sku=external_sku,
        internal_sku_code=str(row["internal_sku_code"]).strip() if row.get("internal_sku_code") else None,
        asin=str(row["asin"]).strip() if row.get("asin") else None,
        available_qty=_nonnegative_int(row, "available_qty"),
        reserved_qty=_nonnegative_int(row, "reserved_qty"),
        inbound_qty=_nonnegative_int({"inbound_qty": inbound_qty}, "inbound_qty"),
        as_of=as_of,
        received_at=received_at,
        payload_hash=_payload_hash(row),
        idempotency_key=key,
        raw_ref=str(row["raw_ref"]).strip() if row.get("raw_ref") else f"mock://{platform}/inventory/{index}",
        source_mode=source_mode,
        simulated=True,
    )


def normalize_event_row(row: dict[str, Any], *, platform: str, source_mode: str, index: int) -> CanonicalEvent:
    event_id = _required_text(row, "external_event_id")
    account_ref = _required_text(row, "account_ref")
    occurred_at = _parse_time(row.get("occurred_at"), field="occurred_at")
    received_at = _parse_time(row.get("received_at") or row.get("occurred_at"), field="received_at")
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    key = str(row.get("idempotency_key") or f"{platform}:{account_ref}:{event_id}")
    return CanonicalEvent(
        schema_version=str(row.get("schema_version") or "1"),
        platform=platform,
        account_ref=account_ref,
        external_event_id=event_id,
        event_type=_required_text(row, "event_type"),
        external_object_no=str(row["external_object_no"]).strip() if row.get("external_object_no") else None,
        event_version=int(row["event_version"]) if row.get("event_version") is not None else None,
        occurred_at=occurred_at,
        received_at=received_at,
        payload_hash=_payload_hash(row),
        idempotency_key=key,
        payload=payload,
        raw_ref=str(row["raw_ref"]).strip() if row.get("raw_ref") else f"mock://{platform}/events/{index}",
        source_mode=source_mode,
        simulated=True,
    )


def _decode_input(content: str | bytes, *, source_mode: str) -> list[dict[str, Any]]:
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    if source_mode in ("json", "mock"):
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConnectorError("JSON 数据格式无效") from exc
        if isinstance(value, dict):
            envelope = value
            rows = value.get("records", value.get("items"))
            if rows is None and "external_sku" in value:
                rows = [value]
            if not isinstance(rows, list) or not all(isinstance(item, dict) for item in rows):
                raise ConnectorError("JSON 必须是对象数组或包含 records/items 数组")
            return _apply_envelope_defaults(rows, envelope)
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise ConnectorError("JSON 必须是对象数组或包含 records/items 数组")
        return value
    if source_mode == "csv":
        rows = list(csv.DictReader(io.StringIO(text)))
        if rows:
            defaults = {key: rows[0].get(key) for key in ("platform", "account_ref", "store_ref", "warehouse_ref", "as_of")}
            for row in rows:
                for key, value in defaults.items():
                    if not row.get(key) and value:
                        row[key] = value
        return rows
    raise ConnectorError(f"不支持的数据模式: {source_mode}")


class MockConnector:
    read_only = True
    live_enabled = False
    simulated = True

    def __init__(self, *, platform: str, content: str | bytes, source_mode: str = "mock", kind: str = "inventory"):
        if platform not in PLATFORMS:
            raise ConnectorError(f"不支持的平台: {platform}")
        self.platform = platform
        self.source_mode = source_mode
        self.kind = kind
        rows = _decode_input(content, source_mode=source_mode)
        self._rows = rows

    def inventory_snapshots(self) -> list[InventorySnapshotRecord]:
        return [normalize_inventory_row(row, platform=self.platform, source_mode=self.source_mode, index=index) for index, row in enumerate(self._rows)]

    def events(self) -> list[CanonicalEvent]:
        return [normalize_event_row(row, platform=self.platform, source_mode=self.source_mode, index=index) for index, row in enumerate(self._rows)]


def load_records(content: str | bytes, *, platform: str, source_mode: str = "json") -> list[InventorySnapshotRecord]:
    """读取并标准化库存记录；不访问网络、不产生数据库副作用。"""
    return MockConnector(platform=platform, content=content, source_mode=source_mode).inventory_snapshots()


def load_events(content: str | bytes, *, platform: str, source_mode: str = "json") -> list[CanonicalEvent]:
    return MockConnector(platform=platform, content=content, source_mode=source_mode, kind="events").events()


def connector_capabilities() -> list[dict[str, Any]]:
    return [{"platform": platform, "source_modes": list(MODES), "read_only": True, "live_enabled": False, "simulated": True} for platform in PLATFORMS]


def records_to_json(records: Iterable[InventorySnapshotRecord]) -> str:
    return json.dumps([record.as_dict() for record in records], ensure_ascii=False, sort_keys=True, indent=2) + "\n"
