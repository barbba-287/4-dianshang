"""P7-S1 成本快照服务（append-only 成本事实层）。

设计纪律（dev_task.p7-profit-center.md §2.2 / §13）：

- **append-only**：本模块只有 INSERT，**不出现 update / delete**。成本修正 = 追加新快照。
  历史利润可审计的前提就是「昨天的成本事实不能被今天的操作改写」。
- **一数据行一记录**：`load_cost_rows` 对本行任何不合规都抛 `ConnectorError`，
  **绝不 `continue` 静默跳过**。这是 P6-S3「静默丢行」护栏成立的前提：`imports._isolate`
  用「记录数 == 行数」判定区间是否正常，一旦某行被无声吞掉，护栏会把它判成坏行并二分，
  合法数据反而被误拒。
- **零 N+1**：`ingest_cost_records` 一次 `IN (...)` 解析全部 SKU 编码，再逐个插入。
- **幂等**：唯一约束 `(workspace_id, sku_id, effective_time, source, source_ref)` 兜底，
  先查后插 + 约束双保险，同文件重传不膨胀。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
import csv
import io

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.connectors import ConnectorError
from app.db import CostSnapshot, ExternalProductMapping, ProductSku

COST_KIND = "costs"
SOURCE_MANUAL = "manual"
SOURCE_INBOUND = "inbound_confirm"
MAX_UNIT_COST = Decimal("1000000000")
_QUANT = Decimal("0.01")


@dataclass
class CostRecord:
    """一行解析结果。`sku_code` 是原始编码，`sku_id` 由 ingest 阶段批量解析后回填。"""

    sku_code: str
    unit_cost: Decimal
    effective_time: datetime | None
    currency: str
    note: str | None
    sku_id: int | None = None


@dataclass
class CostIngestStats:
    total: int = 0
    imported: int = 0
    skipped: int = 0
    # 行级拒绝（SKU 解析不到的记录在此；格式类坏行在 imports 层就已归入 rejected）
    rejected: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "imported": self.imported,
            "skipped": self.skipped,
            "rejected_total": len(self.rejected),
            "rejected": self.rejected,
        }


def _parse_decimal(raw: str) -> Decimal:
    text = (raw or "").strip().replace(",", "").replace("￥", "").replace("¥", "")
    if not text:
        raise ConnectorError("成本单价不能为空")
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ConnectorError(f"成本单价非法：{raw}") from exc
    if value < 0:
        raise ConnectorError(f"成本单价不能为负：{raw}")
    if value > MAX_UNIT_COST:
        raise ConnectorError(f"成本单价超出上限：{raw}")
    # 多于两位小数四舍五入到分（不报错：财务表里常见 3 位小数，拒绝会让整表重做）
    return value.quantize(_QUANT)


def _parse_time(raw: str) -> datetime:
    text = (raw or "").strip()
    if not text:
        raise ConnectorError("生效时间不能为空")
    candidate = text.replace("/", "-").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ConnectorError(f"生效时间非法：{raw}") from exc


def _clean(value) -> str:
    return "" if value is None else str(value).strip()


def load_cost_rows(text: str) -> list[CostRecord]:
    """把映射后的成本 CSV 文本解析成记录列表（一行一记录，违规抛 ConnectorError）。

    与 `connectors.load_records/load_orders` 同契约：被 `imports._isolate` 以「计数 + 异常」
    双重判定调用，因此**严禁**任何形式的静默跳过。
    """
    reader = csv.DictReader(io.StringIO(text))
    headers = [name for name in (reader.fieldnames or []) if name is not None]
    if not headers:
        raise ConnectorError("成本文件缺少列头")
    records: list[CostRecord] = []
    for row in reader:
        cleaned = {key: value for key, value in row.items() if key is not None}
        if not any((value or "").strip() for value in cleaned.values() if isinstance(value, str)):
            # 纯空行不计入数据行（与 _csv_rows 的语义一致：空行不是"一条记录"）
            continue
        sku_code = _clean(cleaned.get("sku_code"))
        if not sku_code:
            raise ConnectorError("商品编码不能为空")
        unit_cost = _parse_decimal(_clean(cleaned.get("unit_cost")))
        raw_time = _clean(cleaned.get("effective_time"))
        effective_time = _parse_time(raw_time) if raw_time else None
        currency = _clean(cleaned.get("currency")) or "CNY"
        if len(currency) > 8:
            raise ConnectorError(f"币种非法：{currency}")
        note = _clean(cleaned.get("note")) or None
        records.append(
            CostRecord(
                sku_code=sku_code,
                unit_cost=unit_cost,
                effective_time=effective_time,
                currency=currency,
                note=note,
            )
        )
    if not records:
        raise ConnectorError("成本文件没有数据行")
    return records


def resolve_sku_ids(db: Session, *, workspace_id: int, sku_codes: list[str]) -> dict[str, int]:
    """批量把编码解析成内部 SKU（一次两条查询，零 N+1）。

    优先 `product_skus.sku_code`；未命中再回落 `external_product_mappings.external_sku`
    —— 财务手上的表往往来自平台导出，用外部编码更顺手，不该强迫人工换算。
    """
    unique = sorted({code for code in sku_codes if code})
    resolved: dict[str, int] = {}
    if not unique:
        return resolved
    rows = db.execute(
        select(ProductSku.sku_code, ProductSku.id).where(
            ProductSku.workspace_id == workspace_id,
            ProductSku.sku_code.in_(unique),
        )
    ).all()
    for code, sku_id in rows:
        resolved[code] = int(sku_id)
    remaining = [code for code in unique if code not in resolved]
    if remaining:
        mappings = db.execute(
            select(ExternalProductMapping.external_sku, ExternalProductMapping.internal_sku_id).where(
                ExternalProductMapping.workspace_id == workspace_id,
                ExternalProductMapping.external_sku.in_(remaining),
                ExternalProductMapping.mapping_status == "mapped",
                ExternalProductMapping.internal_sku_id.is_not(None),
            )
        ).all()
        for code, sku_id in mappings:
            if code not in resolved and sku_id is not None:
                resolved[str(code)] = int(sku_id)
    return resolved


def _snapshot_exists(db: Session, *, workspace_id: int, sku_id: int, effective_time: datetime,
                     source: str, source_ref: str) -> bool:
    return db.scalar(
        select(CostSnapshot.id).where(
            CostSnapshot.workspace_id == workspace_id,
            CostSnapshot.sku_id == sku_id,
            CostSnapshot.effective_time == effective_time,
            CostSnapshot.source == source,
            CostSnapshot.source_ref == source_ref,
        )
    ) is not None


def create_cost_snapshot(
    db: Session,
    *,
    workspace_id: int,
    sku_id: int,
    unit_cost: Decimal,
    effective_time: datetime,
    source: str,
    source_ref: str,
    currency: str = "CNY",
    note: str | None = None,
    created_by: str | None = None,
    commit: bool = True,
) -> tuple[CostSnapshot | None, bool]:
    """写入一条快照（幂等）。返回 (快照, 是否新建)；已存在时返回 (None, False)。

    只 INSERT / 只 SELECT —— 本模块严禁 update / delete（R-1）。
    """
    if _snapshot_exists(
        db, workspace_id=workspace_id, sku_id=sku_id,
        effective_time=effective_time, source=source, source_ref=source_ref,
    ):
        return None, False
    snapshot = CostSnapshot(
        workspace_id=workspace_id,
        sku_id=sku_id,
        unit_cost=unit_cost,
        currency=currency or "CNY",
        effective_time=effective_time,
        source=source,
        source_ref=source_ref,
        note=note,
        created_at=datetime.utcnow(),
        created_by=created_by,
    )
    db.add(snapshot)
    if commit:
        db.commit()
        db.refresh(snapshot)
    else:
        db.flush()
    return snapshot, True


def ingest_cost_records(
    db: Session,
    records: list[CostRecord],
    *,
    workspace_id: int,
    created_by: str | None = None,
    batch_id: str | None = None,
    default_effective_time: datetime | None = None,
) -> CostIngestStats:
    """批量入库。SKU 解析不到的记录进 rejected（行级容错，不影响其它行）。

    零 N+1：SKU 解析 2 条 + 既有 source_ref 预取 1 条，之后全部在内存里判定，
    最后一次 commit。计数不变量：`imported + skipped + rejected_total == total`。
    """
    stats = CostIngestStats(total=len(records))
    if not records:
        return stats
    resolved = resolve_sku_ids(
        db, workspace_id=workspace_id, sku_codes=[record.sku_code for record in records]
    )
    fallback_time = default_effective_time or datetime.utcnow()
    batch = batch_id or "adhoc"

    # 1) 内存排布：先定 sku 与 source_ref，坏行进 rejected（不中断整批）
    planned: list[tuple[CostRecord, int, str, datetime]] = []
    for record in records:
        sku_id = resolved.get(record.sku_code)
        if sku_id is None:
            stats.rejected.append(
                {"sku_code": record.sku_code, "reason_code": "SKU_NOT_FOUND",
                 "reason_message": f"找不到 SKU：{record.sku_code}"}
            )
            continue
        record.sku_id = sku_id
        effective_time = record.effective_time or fallback_time
        source_ref = f"manual:{batch}:{record.sku_code}:{effective_time.isoformat()}"
        planned.append((record, sku_id, source_ref, effective_time))

    # 2) 一次预取已存在的 source_ref（重传幂等的判定依据，避免逐行 SELECT）
    existing_refs: set[str] = set()
    if planned:
        existing_refs = set(
            db.scalars(
                select(CostSnapshot.source_ref).where(
                    CostSnapshot.workspace_id == workspace_id,
                    CostSnapshot.source == SOURCE_MANUAL,
                    CostSnapshot.source_ref.in_([ref for _, _, ref, _ in planned]),
                )
            ).all()
        )

    try:
        for record, sku_id, source_ref, effective_time in planned:
            if source_ref in existing_refs:
                stats.skipped += 1
                continue
            db.add(
                CostSnapshot(
                    workspace_id=workspace_id,
                    sku_id=sku_id,
                    unit_cost=record.unit_cost,
                    currency=record.currency or "CNY",
                    effective_time=effective_time,
                    source=SOURCE_MANUAL,
                    source_ref=source_ref,
                    note=record.note,
                    created_at=datetime.utcnow(),
                    created_by=created_by,
                )
            )
            existing_refs.add(source_ref)
            stats.imported += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return stats


def list_cost_snapshots(
    db: Session,
    *,
    workspace_id: int,
    sku_id: int | None = None,
    sku_code: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """只读查询：按生效时间倒序分页。返回 (行, 总数)。"""
    filters = [CostSnapshot.workspace_id == workspace_id]
    if sku_id is not None:
        filters.append(CostSnapshot.sku_id == sku_id)
    if sku_code:
        filters.append(
            CostSnapshot.sku_id.in_(
                select(ProductSku.id).where(
                    ProductSku.workspace_id == workspace_id,
                    ProductSku.sku_code == sku_code,
                )
            )
        )
    if start is not None:
        filters.append(CostSnapshot.effective_time >= start)
    if end is not None:
        filters.append(CostSnapshot.effective_time <= end)
    total = int(db.scalar(select(func.count(CostSnapshot.id)).where(*filters)) or 0)
    rows = db.execute(
        select(CostSnapshot, ProductSku.sku_code)
        .join(ProductSku, ProductSku.id == CostSnapshot.sku_id)
        .where(*filters)
        .order_by(CostSnapshot.effective_time.desc(), CostSnapshot.id.desc())
        .limit(max(1, min(limit, 200)))
        .offset(max(0, offset))
    ).all()
    items = [
        {
            "id": snapshot.id,
            "sku_id": snapshot.sku_id,
            "sku_code": sku_code_value,
            "unit_cost": f"{Decimal(snapshot.unit_cost):.2f}",
            "currency": snapshot.currency,
            "effective_time": snapshot.effective_time.isoformat(),
            "source": snapshot.source,
            "source_ref": snapshot.source_ref,
            "note": snapshot.note,
            "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
            "created_by": snapshot.created_by,
        }
        for snapshot, sku_code_value in rows
    ]
    return items, total
