"""P2-3 数据质量门禁：六维质量模型（纯计算 + 本地留存只读）。

红线（与 `dev_task.p2s3-quality.md` 对齐）：

1. **零 live 依赖**：本模块**不访问网络、不连数据库**——只读本地留存文件 + 纯计算；
   库内事实（已入库行、既有行哈希、台账宣告）由调用方以数据形式喂进来。
2. **六维写死**，不得增删：
   `structural`(hard) / `integrity`(hard) / `uniqueness`(soft) / `consistency`(soft) /
   `freshness`(soft) / `completeness`(soft)。
   hard 维度有 findings → `failed`；只有 soft 有 findings → `warn`；全零 → `ok`。
3. **truncated 与 unknown 严格分界**：
   - 截断 = 有证据但不完整 → 该页 structural 降级为 skip、completeness 记一条 → `warn`；
   - 缺失 = 根本没得校验 → `unknown`（既不冒充 `warn`，也不冒充 `failed`）。
4. **integrity 走 normalize 预检**：把同一行喂给 `app/connectors.py` 的 normalize 函数，
   统计"它会抛的错"（预检计数）；**不替代** normalize 在写入路径上的既有语义，
   也不改动 `app/connectors.py`（该文件是本片红线不可改动位置）。

六维口径（与任务文件 T1 逐条对应）：

| 维度 | 硬/软 | 判定 |
|---|---|---|
| structural | hard | GraphQL `errors` 节点存在 / JSON 不可解析 / 缺 `data.<resource>` 键 |
| integrity | hard | 行缺必填字段、类型错、金额或数量为负（= normalize 会抛的错） |
| uniqueness | soft | 同批内幂等键重复出现 |
| consistency | soft | 同幂等键的 payload_hash 不一致（R-31 conflict 的质量视角） |
| freshness | soft | `meta.captured_at` 或行 `as_of` 距校验时点超过过期阈值 |
| completeness | soft | 宣告资源与实际页集不符：留存件/页缺失、留存件被截断 |
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.connectors import (
    ConnectorError,
    normalize_event_row,
    normalize_inventory_row,
    normalize_order_row,
)

# ---------------------------------------------------------------------------
# 常量（写死，不得增删维度）
# ---------------------------------------------------------------------------

QUALITY_DIMENSIONS: tuple[str, ...] = (
    "structural",
    "integrity",
    "uniqueness",
    "consistency",
    "freshness",
    "completeness",
)
HARD_DIMENSIONS: tuple[str, ...] = ("structural", "integrity")
SOFT_DIMENSIONS: tuple[str, ...] = ("uniqueness", "consistency", "freshness", "completeness")

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"

# 每个维度最多留 3 条样例（任务文件 T1 写死）。
SAMPLE_LIMIT = 3

# 留存文件后缀（与 app/cli.py 的 retention 命令同款布局）。
_META_SUFFIX = ".meta.json"
_BODY_SUFFIX = ".json"
_TRUNCATED_SUFFIX = ".json.truncated"

# 台账资源名 → GraphQL `data` 下的键（适配器按 data_key 拉取）。
_DATA_KEY_BY_RESOURCE: dict[str, str] = {
    "orders": "orders",
    "inventory": "inventoryItems",
    "products": "products",
}

# 留存文件里没有运行期字段（account_ref 之类），预检时用占位符补齐，
# 只校验"文件里可判定"的那部分（缺 sku / 负数 / 类型错 / 金额非法）。
_REPLAY_ACCOUNT_PLACEHOLDER = "replay://retention"


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QualityRow:
    """被校验的一行外部事实。

    - `origin="stored"`：库内已入库行（键=真实幂等键）。
    - `origin="source"`：由留存页推导出的行（键=回放定位键，hash=内容指纹）。
    - `errors`：预检出的问题（normalize 会抛的错）；空元组=干净行。
    """

    resource: str
    key: str
    payload_hash: str | None = None
    as_of: datetime | None = None
    origin: str = "stored"
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class QualityPage:
    """一页上游响应（= 一次上游请求的留存件）。"""

    resource: str
    request_id: str
    captured_at: datetime | None = None
    truncated: bool = False
    structural_errors: tuple[str, ...] = ()
    rows: tuple[QualityRow, ...] = ()


@dataclass(frozen=True)
class QualityEvidence:
    """一次质量校验的全部输入事实。"""

    run_id: str
    pages: tuple[QualityPage, ...] = ()
    rows: tuple[QualityRow, ...] = ()
    declared_resources: tuple[str, ...] = ()
    # 幂等键 → 库中**既有行**的 payload_hash 集合（本 run 之外的行）。
    existing_hashes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # 本次运行是否"应当有留存"（live 路径为真；fixture/mock 路径为假）。
    retention_expected: bool = False
    # 显式判定"根本没得校验"的原因；非空 → status=unknown。
    unknown_reason: str | None = None
    now: datetime | None = None
    stale_seconds: int = 300


@dataclass(frozen=True)
class QualityReport:
    """质量报告（落库形态：run.quality_json）。"""

    run_id: str
    status: str
    dims: dict[str, dict]
    checked_at: str

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "dims": {
                key: {
                    "level": value["level"],
                    "findings": value["findings"],
                    "samples": list(value["samples"]),
                }
                for key, value in self.dims.items()
            },
            "checked_at": self.checked_at,
        }


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _parse_iso(value: Any) -> datetime | None:
    """宽松解析 ISO-8601 时间；解析不了就返回 None（不当成"合法"）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _age_seconds(now: datetime, then: datetime) -> float:
    return max(0.0, (_aware(now) - _aware(then)).total_seconds())


def _content_hash(payload: Any) -> str:
    """内容指纹（回放侧用；与适配器的 payload_hash 不同口径，只用于同批内比对）。"""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def default_stale_seconds() -> int:
    """过期阈值：复用既有配置 `shopify_sync_budget_seconds`（本片不新增配置项）。"""
    from app.config import get_settings

    return int(get_settings().shopify_sync_budget_seconds)


def retention_dir_for_run(run_id: str) -> Path:
    """某个 run 的留存目录（与 CLI retention 命令同款路径口径）。"""
    from app.config import get_settings

    settings = get_settings()
    return settings.resolve_path(settings.artifacts_dir) / "shopify_sync" / run_id


# ---------------------------------------------------------------------------
# normalize 预检（integrity 的唯一口径：不替代 normalize，只统计它会抛的错）
# ---------------------------------------------------------------------------


def inventory_precheck(raw: dict, *, platform: str, source_mode: str) -> list[str]:
    """用 `normalize_inventory_row` 预检一行库存，返回它会抛的错（空列表=干净）。"""
    try:
        normalize_inventory_row(raw, platform=platform, source_mode=source_mode, index=0)
    except ConnectorError as exc:
        return [str(exc)]
    return []


def order_precheck(raw: dict, *, platform: str, source_mode: str) -> list[str]:
    """用 `normalize_order_row` 预检一行订单（含行明细），返回它会抛的错。"""
    try:
        normalize_order_row(raw, platform=platform, source_mode=source_mode, index=0)
    except ConnectorError as exc:
        return [str(exc)]
    return []


def event_precheck(raw: dict, *, platform: str, source_mode: str) -> list[str]:
    """用 `normalize_event_row` 预检一行事件，返回它会抛的错。"""
    try:
        normalize_event_row(raw, platform=platform, source_mode=source_mode, index=0)
    except ConnectorError as exc:
        return [str(exc)]
    return []


# ---------------------------------------------------------------------------
# 留存件 → 留存页证据（只读本地文件）
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _order_source_row(node: Mapping[str, Any], *, captured_at: datetime | None) -> QualityRow:
    """把一条 GraphQL 订单节点转成可预检的行（只做取值，不做业务改写）。"""
    order_no = str(node.get("name") or node.get("id") or "")
    created = node.get("createdAt")
    updated = node.get("updatedAt") or created
    money = ((node.get("currentTotalPriceSet") or {}).get("shopMoney") or {})
    currency = str(money.get("currencyCode") or "USD")
    lines: list[dict[str, Any]] = []
    for index, line in enumerate((node.get("lineItems") or {}).get("nodes") or []):
        line = line or {}
        variant = line.get("variant") or {}
        line_money = ((line.get("originalTotalSet") or {}).get("shopMoney") or {})
        lines.append({
            "external_line_id": str(line.get("id") or f"line:{index}"),
            "external_sku": str(line.get("sku") or variant.get("id") or "line"),
            "ordered_qty": line.get("quantity", 0),
            "cancelled_qty": 0,
            "refunded_qty": 0,
            "gross_amount": str(line_money.get("amount", "0")),
            "refund_amount": "0",
            "currency": str(line_money.get("currencyCode") or currency),
        })
    raw = {
        "account_ref": _REPLAY_ACCOUNT_PLACEHOLDER,
        "external_order_no": order_no,
        "store_ref": "default",
        "external_created_at": created,
        "external_updated_at": updated,
        "paid_at": node.get("processedAt"),
        "order_status": str(node.get("displayFinancialStatus") or "unknown"),
        "gross_amount": str(money.get("amount", "0")),
        "refund_amount": "0",
        "currency": currency,
        "lines": lines,
    }
    errors = order_precheck(raw, platform="shopify", source_mode="shopify_live")
    as_of = _parse_iso(updated) or captured_at
    return QualityRow(
        resource="orders",
        key=f"replay:orders:{order_no}",
        payload_hash=_content_hash(raw),
        as_of=as_of,
        origin="source",
        errors=tuple(errors),
    )


def _inventory_source_row(item: Mapping[str, Any], level: Mapping[str, Any], *, captured_at: datetime | None) -> QualityRow:
    """把一条 GraphQL 库存档（item + level）转成可预检的行。"""
    quantities = level.get("quantities") or {}
    available = quantities.get("available")
    if available is None:
        available = 0
    item_id = str(item.get("id") or "")
    location_id = str(((level.get("location") or {}).get("id")) or "unknown")
    raw = {
        "account_ref": _REPLAY_ACCOUNT_PLACEHOLDER,
        "external_sku": str(item.get("sku") or item.get("id") or ""),
        "as_of": _aware(captured_at).isoformat() if captured_at else None,
        "received_at": _aware(captured_at).isoformat() if captured_at else None,
        "available_qty": available,
        "reserved_qty": 0,
        "inbound_qty": 0,
    }
    errors = inventory_precheck(raw, platform="shopify", source_mode="shopify_live")
    return QualityRow(
        resource="inventory",
        key=f"replay:inventory:{item_id}:{location_id}",
        payload_hash=_content_hash(raw),
        as_of=captured_at,
        origin="source",
        errors=tuple(errors),
    )


def source_rows_from_payload(resource: str, payload: Any, *, captured_at: datetime | None = None) -> list[QualityRow]:
    """从 GraphQL 响应里取出待预检的原始行（只定位与取值，不改写业务语义）。"""
    if not isinstance(payload, Mapping):
        return []
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return []
    rows: list[QualityRow] = []
    if resource == "orders":
        for edge in ((data.get("orders") or {}).get("edges") or []):
            node = (edge or {}).get("node")
            if isinstance(node, Mapping):
                rows.append(_order_source_row(node, captured_at=captured_at))
    elif resource == "inventory":
        for edge in ((data.get("inventoryItems") or {}).get("edges") or []):
            item = (edge or {}).get("node")
            if not isinstance(item, Mapping):
                continue
            levels = (item.get("inventoryLevels") or {}).get("edges") or []
            for level_edge in levels:
                level = (level_edge or {}).get("node")
                if isinstance(level, Mapping):
                    rows.append(_inventory_source_row(item, level, captured_at=captured_at))
    return rows


def load_retention_pages(directory: Path) -> tuple[QualityPage, ...]:
    """读本地留存目录 → 留存页证据（**只读文件**，不访问网络）。"""
    directory = Path(directory)
    if not directory.is_dir():
        return ()
    pages: list[QualityPage] = []
    for meta_path in sorted(directory.glob(f"*{_META_SUFFIX}")):
        request_id = meta_path.name[: -len(_META_SUFFIX)]
        meta = _load_json(meta_path)
        if not isinstance(meta, Mapping):
            meta = {}
        resource = str(meta.get("resource") or "unknown")
        captured_at = _parse_iso(meta.get("captured_at"))
        truncated = bool(meta.get("truncated")) or (directory / f"{request_id}{_TRUNCATED_SUFFIX}").is_file()
        if truncated:
            # 截断件不是完整响应：跳过该页的结构校验，只在 completeness 记一条。
            pages.append(QualityPage(
                resource=resource,
                request_id=request_id,
                captured_at=captured_at,
                truncated=True,
            ))
            continue
        body_path = directory / f"{request_id}{_BODY_SUFFIX}"
        if not body_path.is_file():
            pages.append(QualityPage(
                resource=resource,
                request_id=request_id,
                captured_at=captured_at,
                structural_errors=("留存正文缺失",),
            ))
            continue
        try:
            text = body_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        structural: list[str] = []
        payload: Any = None
        try:
            payload = json.loads(text)
        except ValueError as exc:
            structural.append(f"JSON 不可解析（{exc}）")
        if not structural:
            if not isinstance(payload, Mapping):
                structural.append("响应不是 JSON 对象")
            else:
                if payload.get("errors"):
                    structural.append("响应含 GraphQL errors 节点")
                data = payload.get("data")
                data_key = _DATA_KEY_BY_RESOURCE.get(resource)
                if not isinstance(data, Mapping):
                    structural.append("缺 data.<resource> 键")
                elif data_key and data_key not in data:
                    structural.append(f"缺 data.{data_key} 键")
        rows = tuple(source_rows_from_payload(resource, payload, captured_at=captured_at)) if not structural else ()
        pages.append(QualityPage(
            resource=resource,
            request_id=request_id,
            captured_at=captured_at,
            structural_errors=tuple(structural),
            rows=rows,
        ))
    return tuple(pages)


# ---------------------------------------------------------------------------
# 六维评估
# ---------------------------------------------------------------------------


def evaluate(evidence: QualityEvidence) -> QualityReport:
    """按六维口径算出一份质量报告（纯计算）。"""
    now = _aware(evidence.now) if evidence.now else datetime.now(timezone.utc)
    dims: dict[str, dict] = {
        name: {
            "level": "hard" if name in HARD_DIMENSIONS else "soft",
            "findings": 0,
            "samples": [],
        }
        for name in QUALITY_DIMENSIONS
    }

    def _add(dim: str, message: str) -> None:
        bucket = dims[dim]
        bucket["findings"] += 1
        if len(bucket["samples"]) < SAMPLE_LIMIT:
            bucket["samples"].append(message)

    # --- unknown：根本没得校验（不是 warn）——
    # 留存目录完全缺失 / meta 找不到 / 既无留存页又无入库行时，六维一律不评估（dims 全零），
    # 否则会出现"status=unknown 却 findings>0"的自相矛盾读数。
    # 状态与维度的映射关系只在"已评估"时成立：
    #   hard 维度 findings>0 → failed；仅 soft → warn；全零 → ok。
    if evidence.unknown_reason:
        return QualityReport(
            run_id=evidence.run_id,
            status=STATUS_UNKNOWN,
            dims=dims,
            checked_at=now.isoformat(),
        )

    # --- structural（hard）：GraphQL errors 节点 / JSON 不可解析 / 缺 data.<resource> 键
    for page in evidence.pages:
        if page.truncated:
            continue  # 截断页降级为 skip（缺口记在 completeness）
        for message in page.structural_errors:
            _add("structural", f"{page.resource}/{page.request_id}: {message}")

    # --- integrity（hard）：脏行数（每行只记一条，细节进样例）
    for row in evidence.rows:
        if row.errors:
            _add("integrity", f"{row.resource}: " + "；".join(row.errors))

    # --- uniqueness（soft）：同批内幂等键重复出现
    counter: dict[str, int] = {}
    for row in evidence.rows:
        counter[row.key] = counter.get(row.key, 0) + 1
    for key, count in sorted(counter.items()):
        if count > 1:
            _add("uniqueness", f"幂等键 {key} 在本批出现 {count} 次")

    # --- consistency（soft）：同幂等键的 payload_hash 不一致（R-31 conflict 的质量视角）
    hashes_by_key: dict[str, set[str]] = {}
    for row in evidence.rows:
        if row.payload_hash:
            hashes_by_key.setdefault(row.key, set()).add(row.payload_hash)
    for key, hashes in sorted(hashes_by_key.items()):
        if len(hashes) > 1:
            _add("consistency", f"幂等键 {key} 在本批出现 {len(hashes)} 个不同 payload_hash")
    conflicted: set[str] = set()
    for row in evidence.rows:
        existing = evidence.existing_hashes.get(row.key) or ()
        if not existing or not row.payload_hash or row.key in conflicted:
            continue
        if row.payload_hash not in existing:
            conflicted.add(row.key)
            _add("consistency", f"幂等键 {row.key} 与库中既有行 payload_hash 不一致")

    # --- freshness（soft）：采集时间 / 行 as_of 距校验时点超过过期阈值
    threshold = max(0, int(evidence.stale_seconds))
    for page in evidence.pages:
        if page.captured_at and _age_seconds(now, page.captured_at) > threshold:
            _add("freshness", f"留存页 {page.resource}/{page.request_id} 采集时间超过 {threshold} 秒")
    for row in evidence.rows:
        if row.as_of and _age_seconds(now, row.as_of) > threshold:
            _add("freshness", f"行 {row.resource}/{row.key} 的 as_of 超过 {threshold} 秒")

    # --- completeness（soft）：宣告资源与实际页集不符、truncated 页（该页跳过校验）
    for page in evidence.pages:
        if page.truncated:
            _add("completeness", f"留存响应 {page.resource}/{page.request_id} 被截断，该页跳过校验")
    # 注：**留存件整体缺失**不记 completeness finding——它由 unknown 规则承担
    # （"根本没得校验"≠"校验过但有缺口"，两者不得互相冒充）。
    if evidence.declared_resources and evidence.retention_expected:
        page_resources = {page.resource for page in evidence.pages}
        for name in evidence.declared_resources:
            if name not in page_resources:
                _add("completeness", f"资源 {name} 已在台账宣告但没有留存页")
        for name in sorted(page_resources - set(evidence.declared_resources)):
            _add("completeness", f"资源 {name} 有留存页但未在台账宣告")

    # --- 状态判定（unknown 已在函数开头短路返回）：hard 有发现 → failed；仅 soft → warn；全零 → ok
    if any(dims[name]["findings"] for name in HARD_DIMENSIONS):
        status = STATUS_FAILED
    elif any(dims[name]["findings"] for name in SOFT_DIMENSIONS):
        status = STATUS_WARN
    else:
        status = STATUS_OK

    return QualityReport(
        run_id=evidence.run_id,
        status=status,
        dims=dims,
        checked_at=now.isoformat(),
    )


# ---------------------------------------------------------------------------
# 回放入口（模块函数）：只读本地留存，零网络
# ---------------------------------------------------------------------------


def replay_quality(
    run_id: str,
    *,
    retention_dir: Path | None = None,
    now: datetime | None = None,
    stale_seconds: int | None = None,
) -> QualityReport:
    """回放某个 run 的留存证据，重算六维质量报告（**只读本地文件**）。

    - 留存目录缺失 / 没有可用留存件 → `status=unknown`（根本没得校验，不抛异常）。
    - 留存件被截断 → 该页跳过校验 + completeness 记一条 → `status=warn`。
    - 零网络：本函数不触碰任何出站 HTTP 客户端。
    """
    directory = Path(retention_dir) if retention_dir is not None else retention_dir_for_run(run_id)
    pages = load_retention_pages(directory)
    rows = tuple(row for page in pages for row in page.rows)
    unknown_reason = None
    if not pages:
        unknown_reason = "留存目录缺失或没有可用留存件"
    evidence = QualityEvidence(
        run_id=run_id,
        pages=pages,
        rows=rows,
        retention_expected=True,
        unknown_reason=unknown_reason,
        now=now,
        stale_seconds=default_stale_seconds() if stale_seconds is None else int(stale_seconds),
    )
    return evaluate(evidence)
