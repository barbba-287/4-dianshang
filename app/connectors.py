"""离线外部平台连接器：只解析和标准化，不访问网络。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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
class ExternalOrderLineRecord:
    external_line_id: str
    external_sku: str
    ordered_qty: int
    cancelled_qty: int
    refunded_qty: int
    gross_amount: Decimal
    refund_amount: Decimal
    currency: str
    payload_hash: str


@dataclass(frozen=True)
class ExternalOrderRecord:
    schema_version: str
    platform: str
    account_ref: str
    store_ref: str
    external_order_no: str
    order_status: str
    external_created_at: datetime
    paid_at: datetime | None
    external_updated_at: datetime
    event_version: int | None
    gross_amount: Decimal
    refund_amount: Decimal
    currency: str
    lines: tuple[ExternalOrderLineRecord, ...]
    payload_hash: str
    idempotency_key: str
    raw_ref: str | None
    source_mode: str
    simulated: bool = True
    data_completeness: str = "complete"
    status_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "platform": self.platform,
            "account_ref": self.account_ref,
            "store_ref": self.store_ref,
            "external_order_no": self.external_order_no,
            "order_status": self.order_status,
            "external_created_at": self.external_created_at.isoformat(),
            "paid_at": self.paid_at.isoformat() if self.paid_at else None,
            "external_updated_at": self.external_updated_at.isoformat(),
            "event_version": self.event_version,
            "gross_amount": str(self.gross_amount),
            "refund_amount": str(self.refund_amount),
            "currency": self.currency,
            "lines": [
                {"external_line_id": line.external_line_id, "external_sku": line.external_sku,
                 "ordered_qty": line.ordered_qty, "cancelled_qty": line.cancelled_qty,
                 "refunded_qty": line.refunded_qty, "gross_amount": str(line.gross_amount),
                 "refund_amount": str(line.refund_amount), "currency": line.currency,
                 "payload_hash": line.payload_hash}
                for line in self.lines
            ],
            "payload_hash": self.payload_hash,
            "idempotency_key": self.idempotency_key,
            "raw_ref": self.raw_ref,
            "source_mode": self.source_mode,
            "simulated": self.simulated,
            "data_completeness": self.data_completeness,
            "status_reason": self.status_reason,
        }


ORDER_STATUSES = {"pending", "paid", "fulfilled", "completed", "cancelled", "closed", "unknown"}


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
    if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
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


def _money(row: dict[str, Any], field: str, default: str = "0") -> Decimal:
    value = row.get(field, default)
    if isinstance(value, bool):
        raise ConnectorError(f"{field} 必须是金额")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ConnectorError(f"{field} 必须是金额") from exc
    if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
        raise ConnectorError(f"{field} 必须是非负、最多两位小数的金额")
    return amount.quantize(Decimal("0.01"))


def _order_status(value: Any) -> tuple[str, str | None]:
    if not isinstance(value, str) or not value.strip():
        raise ConnectorError("order_status 不能为空")
    raw = value.strip().lower()
    aliases = {
        "待付款": "pending", "待支付": "pending", "已付款": "paid", "已支付": "paid",
        "已发货": "fulfilled", "已完成": "completed", "交易成功": "completed",
        "已取消": "cancelled", "取消": "cancelled", "已关闭": "closed",
    }
    canonical = aliases.get(raw, raw if raw in ORDER_STATUSES else "unknown")
    return canonical, None if canonical != "unknown" else f"未知订单状态: {value}"


def normalize_order_row(row: dict[str, Any], *, platform: str, source_mode: str, index: int) -> ExternalOrderRecord:
    if platform not in PLATFORMS:
        raise ConnectorError(f"不支持的平台: {platform}")
    if source_mode not in MODES:
        raise ConnectorError(f"不支持的数据模式: {source_mode}")
    account_ref = _required_text(row, "account_ref")
    order_no = _required_text(row, "external_order_no")
    store_ref = str(row.get("store_ref") or "default").strip() or "default"
    created = _parse_time(row.get("external_created_at") or row.get("created_at"), field="external_created_at")
    updated = _parse_time(row.get("external_updated_at") or row.get("updated_at") or row.get("external_created_at") or row.get("created_at"), field="external_updated_at")
    paid_value = row.get("paid_at")
    paid_at = _parse_time(paid_value, field="paid_at") if paid_value else None
    status, reason = _order_status(row.get("order_status") or row.get("status"))
    lines_value = row.get("lines")
    if not isinstance(lines_value, list) or not lines_value or not all(isinstance(item, dict) for item in lines_value):
        raise ConnectorError("lines 必须是非空对象数组")
    lines: list[ExternalOrderLineRecord] = []
    for line_index, line in enumerate(lines_value):
        external_sku = _required_text(line, "external_sku")
        ordered = _nonnegative_int(line, "ordered_qty")
        cancelled = _nonnegative_int(line, "cancelled_qty")
        refunded = _nonnegative_int(line, "refunded_qty")
        if cancelled + refunded > ordered:
            raise ConnectorError("cancelled_qty + refunded_qty 不能大于 ordered_qty")
        line_id = str(line.get("external_line_id") or line.get("line_id") or f"line:{line_index}").strip()
        line_payload = {**line, "external_line_id": line_id}
        lines.append(ExternalOrderLineRecord(
            external_line_id=line_id, external_sku=external_sku, ordered_qty=ordered,
            cancelled_qty=cancelled, refunded_qty=refunded,
            gross_amount=_money(line, "gross_amount"), refund_amount=_money(line, "refund_amount"),
            currency=str(line.get("currency") or row.get("currency") or "CNY"),
            payload_hash=_payload_hash(line_payload),
        ))
    event_version = row.get("event_version")
    if event_version is not None:
        try:
            event_version = int(event_version)
        except (TypeError, ValueError) as exc:
            raise ConnectorError("event_version 必须是整数") from exc
        if event_version < 0:
            raise ConnectorError("event_version 必须是非负整数")
    canonical = {**row, "account_ref": account_ref, "store_ref": store_ref, "external_order_no": order_no,
                 "external_created_at": created.isoformat(), "external_updated_at": updated.isoformat(),
                 "lines": [{**line.__dict__, "gross_amount": str(line.gross_amount), "refund_amount": str(line.refund_amount)} for line in lines]}
    return ExternalOrderRecord(
        schema_version=str(row.get("schema_version") or "1"), platform=platform,
        account_ref=account_ref, store_ref=store_ref, external_order_no=order_no,
        order_status=status, external_created_at=created, paid_at=paid_at,
        external_updated_at=updated, event_version=event_version,
        gross_amount=_money(row, "gross_amount"), refund_amount=_money(row, "refund_amount"),
        currency=str(row.get("currency") or "CNY"), lines=tuple(lines),
        payload_hash=_payload_hash(canonical),
        idempotency_key=str(row.get("idempotency_key") or f"{platform}:{account_ref}:{store_ref}:{order_no}:{event_version if event_version is not None else updated.isoformat()}"),
        raw_ref=str(row["raw_ref"]).strip() if row.get("raw_ref") else f"mock://{platform}/orders/{index}",
        source_mode=source_mode, simulated=bool(row.get("simulated", source_mode == "mock")), data_completeness="partial" if reason else "complete", status_reason=reason,
    )


def load_orders(content: str | bytes, *, platform: str, source_mode: str = "json") -> list[ExternalOrderRecord]:
    rows = _decode_input(content, source_mode=source_mode)
    # JSON records contain nested lines. CSV accepts one order per row with a JSON `lines` column.
    if source_mode == "csv":
        for row in rows:
            if isinstance(row.get("lines"), str):
                try:
                    row["lines"] = json.loads(row["lines"])
                except json.JSONDecodeError as exc:
                    raise ConnectorError("CSV lines 必须是 JSON 数组") from exc
            elif row.get("external_sku"):
                row["lines"] = [{key: row[key] for key in ("external_line_id", "external_sku", "ordered_qty", "cancelled_qty", "refunded_qty", "gross_amount", "refund_amount", "currency") if row.get(key) is not None}]
    return [normalize_order_row(row, platform=platform, source_mode=source_mode, index=index) for index, row in enumerate(rows)]

def normalize_event_row(row: dict[str, Any], *, platform: str, source_mode: str, index: int) -> CanonicalEvent:
    if platform not in PLATFORMS:
        raise ConnectorError(f"不支持的平台: {platform}")
    if source_mode not in MODES:
        raise ConnectorError(f"不支持的数据模式: {source_mode}")
    event_id = _required_text(row, "external_event_id")
    account_ref = _required_text(row, "account_ref")
    occurred_at = _parse_time(row.get("occurred_at"), field="occurred_at")
    received_at = _parse_time(row.get("received_at") or row.get("occurred_at"), field="received_at")
    payload = row.get("payload")
    if not isinstance(payload, dict):
        raise ConnectorError("payload 必须是对象")
    key = str(row.get("idempotency_key") or f"{platform}:{account_ref}:{event_id}")
    event_version = row.get("event_version")
    if event_version is not None:
        try:
            event_version = int(event_version)
        except (TypeError, ValueError) as exc:
            raise ConnectorError("event_version 必须是整数") from exc
    return CanonicalEvent(
        schema_version=str(row.get("schema_version") or "1"),
        platform=platform,
        account_ref=account_ref,
        external_event_id=event_id,
        event_type=_required_text(row, "event_type"),
        external_object_no=str(row["external_object_no"]).strip() if row.get("external_object_no") else None,
        event_version=event_version,
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

    def orders(self) -> list[ExternalOrderRecord]:
        return [normalize_order_row(row, platform=self.platform, source_mode=self.source_mode, index=index) for index, row in enumerate(self._rows)]


def load_records(content: str | bytes, *, platform: str, source_mode: str = "json") -> list[InventorySnapshotRecord]:
    """读取并标准化库存记录；不访问网络、不产生数据库副作用。"""
    return MockConnector(platform=platform, content=content, source_mode=source_mode).inventory_snapshots()


def load_events(content: str | bytes, *, platform: str, source_mode: str = "json") -> list[CanonicalEvent]:
    return MockConnector(platform=platform, content=content, source_mode=source_mode, kind="events").events()


def load_orders(content: str | bytes, *, platform: str, source_mode: str = "json") -> list[ExternalOrderRecord]:
    return MockConnector(platform=platform, content=content, source_mode=source_mode, kind="orders").orders()


def connector_capabilities() -> list[dict[str, Any]]:
    return [{"platform": platform, "source_modes": list(MODES), "read_only": True, "live_enabled": False, "simulated": True} for platform in PLATFORMS]


def records_to_json(records: Iterable[InventorySnapshotRecord]) -> str:
    return json.dumps([record.as_dict() for record in records], ensure_ascii=False, sort_keys=True, indent=2) + "\n"
