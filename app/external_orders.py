"""统一外部订单事实和每日销量聚合服务。

订单事实只更新外部域；不会调用任何内部库存写入路径。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import json

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.connectors import ExternalOrderRecord
from app.config import resolve_fee_rate
from app.order_aggregation import aggregate_order_records, log_merge_reports
from app.db import (
    CostSnapshot,
    DailySkuSale,
    ExternalAccount,
    ExternalOrder,
    ExternalOrderLine,
    ExternalProductMapping,
    ProductSku,
)

# P7-S2：已锚定状态。这两个状态的行在订单更新重建时**原样继承**，永不重新 as-of。
# 红线 R-3：否则「今天补录的成本」会改写一笔三天前更新过的订单的历史利润。
ANCHORED_COST_STATUSES = ("resolved", "backfilled")
COST_STATUS_RESOLVED = "resolved"
COST_STATUS_MISSING = "missing"
COST_STATUS_UNMAPPED = "unmapped"

# 批内重复订单号示例上限（错误文案只举前 N 个，避免几十行订单号糊满屏）。
_DUPLICATE_SAMPLE_LIMIT = 3


class BatchDuplicateOrderError(ValueError):
    """批内出现重复订单号（R-65，用户 2026-11-04 裁定方案 A）。

    背景：`ingest_orders` 更新订单时先 DELETE 订单行再重建，未 flush 前的同批重复
    订单号会命中两次 INSERT，撞 `uq_external_order_line_identity` 唯一约束 →
    IntegrityError → 调用方 `except Exception` 转成 500「报表导入失败」，前端只能
    看到一句无信息量的话。

    触发场景很现实：P6-S1 的契约是「CSV 一行一个订单，多商品放 JSON lines 列」，
    而平台导出的订单报表通常**一行一个 SKU**，同一订单号天然跨多行。

    方案 A（不再 500）：在入口处拒绝，返回 422 + 可操作文案，不猜测合并规则。
    方案 B（系统按订单号自动预聚合）登记为 R-66，属后续优化项，本片不实施——
    合并规则一旦猜错（金额按行求和还是取首行？行项目 ID 怎么造？）会安静地写错账。

    **只拦「内容不一致」的重复**：完全相同的重复行是幂等重放，既有逻辑按 `no_op` 处理，
    拦下来只会让正常的分页同步失败（见 `find_duplicate_order_numbers`）。
    """

    code = "DUPLICATE_ORDER_NO_IN_BATCH"

    def __init__(self, order_no: str, count: int, *, duplicate_count: int = 1) -> None:
        self.order_no = order_no
        self.count = count
        self.duplicate_count = duplicate_count
        self.user_message = (
            f"订单号 {order_no} 在本批次出现 {count} 次且内容不一致，无法确定以哪一行为准。"
            "当前版本要求「一行一个订单」：请把同一订单的多个商品合并为一行"
            "（多个商品放进 lines 列），或按订单号拆分文件后分批导入。"
        )
        super().__init__(self.user_message)


def find_duplicate_order_numbers(records: list[ExternalOrderRecord]) -> list[tuple[str, int]]:
    """返回批内「内容不一致的重复订单号」（按出现次数降序）。

    判重键与数据库唯一约束同构：`(platform, account_ref, store_ref, external_order_no)`。

    **只报内容冲突的重复**：同一订单号在本批次出现多次且 `payload_hash` 全相同，属
    幂等重放（Shopify 游标翻页跨页重复、用户把同一行粘贴两次），既有逻辑会把它算成
    `no_op`，不需要也不应该拒绝——把它当错误会让正常同步挂掉。真正危险的是**内容不同**
    的重复：调用方无法知道该保留哪一版金额/状态，且重建订单行时会撞唯一约束。
    """
    groups: dict[tuple[str, str, str, str], list[str]] = {}
    for record in records:
        key = (record.platform, record.account_ref, record.store_ref, record.external_order_no)
        groups.setdefault(key, []).append(record.payload_hash)
    duplicates = [
        (key[3], len(hashes))
        for key, hashes in groups.items()
        if len(hashes) > 1 and len(set(hashes)) > 1
    ]
    duplicates.sort(key=lambda item: (-item[1], item[0]))
    return duplicates


@dataclass
class SalesImportStats:
    total: int = 0
    inserted: int = 0
    updated: int = 0
    no_op: int = 0
    conflict: int = 0
    stale: int = 0
    affected_dates: list[str] | None = None
    sync_run_id: str | None = None
    # R-66：聚合口径的两个可观测计数（不进任何响应 schema，避免打挂键集断言）。
    # total 仍是**聚合前的记录数**（= 文件数据行数），与 P6 的 rows_total 语义一致。
    merged_orders: int = 0
    merged_rows: int = 0


def ensure_external_account(
    db: Session,
    *,
    workspace_id: int,
    platform: str,
    account_ref: str,
    store_ref: str = "default",
    source_mode: str = "mock",
    simulated: bool = True,
) -> ExternalAccount:
    if not workspace_id:
        raise ValueError("WORKSPACE_CONTEXT_REQUIRED")
    store_ref = (store_ref or "default").strip() or "default"
    account = db.scalar(select(ExternalAccount).where(
        ExternalAccount.workspace_id == workspace_id,
        ExternalAccount.platform == platform,
        ExternalAccount.account_ref == account_ref,
        ExternalAccount.store_ref == store_ref,
    ))
    if account is None:
        account = ExternalAccount(
            workspace_id=workspace_id, platform=platform, account_ref=account_ref,
            store_ref=store_ref, source_mode=source_mode, simulated=simulated,
        )
        db.add(account)
        db.flush()
    elif account.status != "active":
        raise ValueError("EXTERNAL_ACCOUNT_INACTIVE")
    return account


def set_product_mapping(
    db: Session,
    *,
    workspace_id: int,
    external_account_id: int,
    external_sku: str,
    internal_sku_id: int | None,
) -> ExternalProductMapping:
    account = db.scalar(select(ExternalAccount).where(
        ExternalAccount.id == external_account_id,
        ExternalAccount.workspace_id == workspace_id,
    ))
    if account is None:
        raise ValueError("EXTERNAL_ACCOUNT_NOT_FOUND")
    sku = None
    if internal_sku_id is not None:
        sku = db.scalar(select(ProductSku).where(
            ProductSku.id == internal_sku_id,
            ProductSku.workspace_id == workspace_id,
            ProductSku.is_active.is_(True),
        ))
        if sku is None:
            raise ValueError("SKU_NOT_FOUND")
    mapping = db.scalar(select(ExternalProductMapping).where(
        ExternalProductMapping.workspace_id == workspace_id,
        ExternalProductMapping.external_account_id == external_account_id,
        ExternalProductMapping.external_sku == external_sku,
    ))
    if mapping is None:
        mapping = ExternalProductMapping(
            workspace_id=workspace_id, external_account_id=external_account_id,
            external_sku=external_sku, internal_sku_id=internal_sku_id,
            mapping_status="mapped" if sku else "unmapped", source="manual",
        )
        db.add(mapping)
    else:
        mapping.internal_sku_id = internal_sku_id
        mapping.mapping_status = "mapped" if sku else "unmapped"
    db.commit()
    db.refresh(mapping)
    rebuild_daily_sales(db, workspace_id=workspace_id, external_account_id=external_account_id, external_sku=external_sku)
    return mapping


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(__import__("datetime").timezone.utc).replace(tzinfo=None)
    return value


def _is_sale(order: ExternalOrder) -> bool:
    return order.order_status in {"paid", "fulfilled", "completed"}


def _line_net(line: ExternalOrderLine) -> tuple[int, Decimal]:
    qty = max(line.ordered_qty - line.cancelled_qty - line.refunded_qty, 0)
    amount = max(Decimal(line.gross_amount or 0) - Decimal(line.refund_amount or 0), Decimal("0"))
    return qty, amount


def _rebuild_key(
    db: Session,
    *,
    workspace_id: int,
    external_account_id: int,
    external_sku: str,
    sales_date: date | None = None,
) -> None:
    account = db.get(ExternalAccount, external_account_id)
    if account is None or account.workspace_id != workspace_id:
        raise ValueError("EXTERNAL_ACCOUNT_NOT_FOUND")
    mapping = db.scalar(select(ExternalProductMapping).where(
        ExternalProductMapping.workspace_id == workspace_id,
        ExternalProductMapping.external_account_id == external_account_id,
        ExternalProductMapping.external_sku == external_sku,
    ))
    internal_sku_id = mapping.internal_sku_id if mapping and mapping.mapping_status == "mapped" else None
    order_query = select(ExternalOrder).where(
        ExternalOrder.workspace_id == workspace_id,
        ExternalOrder.external_account_id == external_account_id,
        ExternalOrder.store_ref == account.store_ref,
    )
    orders = db.scalars(order_query).all()
    groups: dict[date, dict[str, object]] = defaultdict(lambda: {
        "gross_qty": 0, "cancelled_qty": 0, "refunded_qty": 0, "net_qty": 0,
        "gross_amount": Decimal("0"), "refund_amount": Decimal("0"), "net_amount": Decimal("0"),
        "orders": set(), "complete": True,
    })
    for order in orders:
        day = order.external_created_at.date()
        if sales_date is not None and day != sales_date:
            continue
        for line in db.scalars(select(ExternalOrderLine).where(
            ExternalOrderLine.workspace_id == workspace_id,
            ExternalOrderLine.external_order_id == order.id,
            ExternalOrderLine.external_sku == external_sku,
        )).all():
            item = groups[day]
            item["gross_qty"] = int(item["gross_qty"]) + (line.ordered_qty if _is_sale(order) else 0)
            item["cancelled_qty"] = int(item["cancelled_qty"]) + (line.cancelled_qty if _is_sale(order) else line.ordered_qty)
            item["refunded_qty"] = int(item["refunded_qty"]) + (line.refunded_qty if _is_sale(order) else 0)
            net_qty, net_amount = _line_net(line) if _is_sale(order) else (0, Decimal("0"))
            item["net_qty"] = int(item["net_qty"]) + net_qty
            item["gross_amount"] = Decimal(item["gross_amount"]) + (Decimal(line.gross_amount or 0) if _is_sale(order) else Decimal("0"))
            item["refund_amount"] = Decimal(item["refund_amount"]) + (Decimal(line.refund_amount or 0) if _is_sale(order) else Decimal("0"))
            item["net_amount"] = Decimal(item["net_amount"]) + net_amount
            item["orders"].add(order.id)
            if order.data_completeness != "complete" or line.data_completeness != "complete":
                item["complete"] = False
    existing_filters = [
        DailySkuSale.workspace_id == workspace_id,
        DailySkuSale.external_account_id == external_account_id,
        DailySkuSale.external_sku == external_sku,
    ]
    if sales_date is not None:
        existing_filters.append(DailySkuSale.sales_date == sales_date)
    db.execute(delete(DailySkuSale).where(*existing_filters))
    for day, item in groups.items():
        db.add(DailySkuSale(
            workspace_id=workspace_id, external_account_id=external_account_id,
            platform=account.platform, account_ref=account.account_ref, store_ref=account.store_ref,
            external_sku=external_sku, internal_sku_id=internal_sku_id, sales_date=day,
            gross_qty=int(item["gross_qty"]), cancelled_qty=int(item["cancelled_qty"]),
            refunded_qty=int(item["refunded_qty"]), net_qty=int(item["net_qty"]),
            gross_amount=Decimal(item["gross_amount"]), refund_amount=Decimal(item["refund_amount"]),
            net_amount=Decimal(item["net_amount"]), order_count=len(item["orders"]),
            data_completeness="complete" if item["complete"] else "partial",
        ))


def rebuild_daily_sales(
    db: Session,
    *,
    workspace_id: int,
    external_account_id: int,
    external_sku: str,
    sales_date: date | None = None,
) -> None:
    _rebuild_key(db, workspace_id=workspace_id, external_account_id=external_account_id, external_sku=external_sku, sales_date=sales_date)
    db.commit()


def _anchor_pending_lines(
    db: Session,
    pending: list[tuple[ExternalOrderLine, int, datetime, str]],
    *,
    workspace_id: int,
) -> None:
    """对本次批次里「需要新建锚定」的订单行做一次批量 as-of 抓取。

    查询预算（红线 R-2）：本函数只发 **1 条** SELECT，绝不在循环里查库。
    已锚定行的继承由调用方完成，根本不进本函数——它们不该碰成本表。
    """
    if not pending:
        return
    sku_ids = {item[1] for item in pending}
    upper_bound = max(item[2] for item in pending)
    snapshots = db.scalars(
        select(CostSnapshot)
        .where(
            CostSnapshot.workspace_id == workspace_id,
            CostSnapshot.sku_id.in_(sku_ids),
            CostSnapshot.effective_time <= upper_bound,
        )
        .order_by(
            CostSnapshot.sku_id.asc(),
            CostSnapshot.effective_time.asc(),
            CostSnapshot.id.asc(),
        )
    ).all()
    by_sku: dict[int, list[CostSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        by_sku[snapshot.sku_id].append(snapshot)

    anchored_at = datetime.utcnow()
    for line, sku_id, sale_time, platform in pending:
        # 费率与成本分开锚定：成本未命中时费率仍可确定（费率是配置事实）。
        # 先落 fee_rate，将来回填就不必再读"当时的配置"——那时的配置早已可能变了。
        line.fee_rate = resolve_fee_rate(platform)
        line.anchored_at = anchored_at
        hit: CostSnapshot | None = None
        for snapshot in reversed(by_sku.get(sku_id, ())):
            if snapshot.effective_time <= sale_time:
                hit = snapshot
                break
        if hit is None:
            line.cost_snapshot_id = None
            line.unit_cost = None
            line.cost_status = COST_STATUS_MISSING
        else:
            line.cost_snapshot_id = hit.id
            line.unit_cost = hit.unit_cost
            line.cost_status = COST_STATUS_RESOLVED


def ingest_orders(
    db: Session,
    records: list[ExternalOrderRecord],
    *,
    workspace_id: int,
    sync_run_id: str | None = None,
) -> SalesImportStats:
    if not workspace_id:
        raise ValueError("WORKSPACE_CONTEXT_REQUIRED")
    # R-66：展平报表预聚合。必须发生在 R-65 判重**之前**——否则「一行一个 SKU」的合法
    # CSV（同一订单号跨多行）会在判重处就被 422 拦死，永远走不到合并。
    # 聚合是纯内存变换：只对 source_mode == "csv" 生效，无 DB 访问，失败即原样返回。
    original_total = len(records)
    records, merge_reports = aggregate_order_records(records)
    if merge_reports:
        log_merge_reports(merge_reports)
    # R-65：判重必须在任何写入之前。整批要么全部进入，要么一条都不动——
    # 半批入库 + 500 会让「订单事实」与批次历史对不上，人工排查成本远高于直接拒绝。
    # 输入是**聚合后**的记录：可合并的已经合并，剩下的才是真冲突。
    duplicates = find_duplicate_order_numbers(records)
    if duplicates:
        order_no, count = duplicates[0]
        raise BatchDuplicateOrderError(order_no, count, duplicate_count=len(duplicates))
    stats = SalesImportStats(
        total=original_total,
        sync_run_id=sync_run_id,
        affected_dates=[],
        merged_orders=len(merge_reports),
        merged_rows=sum(report.row_count for report in merge_reports),
    )
    touched: set[tuple[int, str]] = set()
    # P7-S2：待锚定行先攒着，循环外一次性预取快照（红线 R-2：新增查询 ≤1）。
    pending: list[tuple[ExternalOrderLine, int, datetime, str]] = []
    try:
        for record in records:
            account = ensure_external_account(
                db, workspace_id=workspace_id, platform=record.platform,
                account_ref=record.account_ref, store_ref=record.store_ref,
                source_mode=record.source_mode, simulated=record.simulated,
            )
            existing = db.scalar(select(ExternalOrder).where(
                ExternalOrder.workspace_id == workspace_id,
                ExternalOrder.external_account_id == account.id,
                ExternalOrder.external_order_no == record.external_order_no,
            ))
            # (external_sku, cost_status, cost_snapshot_id, unit_cost, fee_rate, anchored_at)
            inherited: dict[str, tuple[str, str, int | None, Decimal | None, Decimal | None, datetime | None]] = {}
            if existing is not None:
                newer = (record.event_version is not None and (existing.event_version is None or record.event_version > existing.event_version)) or (
                    record.event_version is None and _utc_naive(record.external_updated_at) > _utc_naive(existing.external_updated_at)
                )
                if existing.payload_hash == record.payload_hash:
                    stats.no_op += 1
                    continue
                if not newer:
                    stats.stale += 1
                    continue
                old_lines = db.scalars(
                    select(ExternalOrderLine).where(ExternalOrderLine.external_order_id == existing.id)
                ).all()
                # 先把锚定五件套抄成普通元组再删行——挂着的 ORM 对象随 DELETE 失效，
                # 直接引用会在重建时读到脏状态（这正是「成本神秘丢失」的经典成因）。
                inherited = {
                    line.external_line_id: (
                        line.external_sku,
                        line.cost_status or COST_STATUS_MISSING,
                        line.cost_snapshot_id,
                        line.unit_cost,
                        line.fee_rate,
                        line.anchored_at,
                    )
                    for line in old_lines
                }
                touched.update((account.id, line.external_sku) for line in old_lines)
                db.execute(delete(ExternalOrderLine).where(ExternalOrderLine.external_order_id == existing.id))
                order = existing
                stats.updated += 1
                touched.update((account.id, line.external_sku) for line in record.lines)
            else:
                order = ExternalOrder(
                    workspace_id=workspace_id, external_account_id=account.id,
                    platform=record.platform, account_ref=record.account_ref, store_ref=record.store_ref,
                    external_order_no=record.external_order_no, order_status=record.order_status,
                    external_created_at=record.external_created_at, paid_at=record.paid_at,
                    external_updated_at=record.external_updated_at, event_version=record.event_version,
                    gross_amount=record.gross_amount, refund_amount=record.refund_amount, currency=record.currency,
                    payload_json=json.dumps(record.as_dict(), ensure_ascii=False, sort_keys=True),
                    payload_hash=record.payload_hash, idempotency_key=record.idempotency_key,
                    source_mode=record.source_mode, simulated=record.simulated, sync_run_id=sync_run_id,
                    data_completeness=record.data_completeness, status_reason=record.status_reason,
                )
                db.add(order)
                db.flush()
                stats.inserted += 1
            order.order_status = record.order_status
            order.external_created_at = record.external_created_at
            order.paid_at = record.paid_at
            order.external_updated_at = record.external_updated_at
            order.event_version = record.event_version
            order.gross_amount = record.gross_amount
            order.refund_amount = record.refund_amount
            order.payload_json = json.dumps(record.as_dict(), ensure_ascii=False, sort_keys=True)
            order.payload_hash = record.payload_hash
            order.idempotency_key = record.idempotency_key
            order.sync_run_id = sync_run_id
            order.data_completeness = record.data_completeness
            order.status_reason = record.status_reason
            sale_time = _utc_naive(record.external_created_at)
            for line in record.lines:
                mapping = db.scalar(select(ExternalProductMapping).where(
                    ExternalProductMapping.workspace_id == workspace_id,
                    ExternalProductMapping.external_account_id == account.id,
                    ExternalProductMapping.external_sku == line.external_sku,
                ))
                internal_id = mapping.internal_sku_id if mapping and mapping.mapping_status == "mapped" else None
                row = ExternalOrderLine(
                    workspace_id=workspace_id, external_order_id=order.id,
                    external_line_id=line.external_line_id, external_sku=line.external_sku,
                    internal_sku_id=internal_id, ordered_qty=line.ordered_qty,
                    cancelled_qty=line.cancelled_qty, refunded_qty=line.refunded_qty,
                    gross_amount=line.gross_amount, refund_amount=line.refund_amount,
                    currency=line.currency, mapping_status="mapped" if internal_id else "unmapped",
                    data_completeness=record.data_completeness, payload_hash=line.payload_hash,
                )
                db.add(row)
                previous = inherited.get(line.external_line_id)
                if internal_id is None:
                    # 没有内部 SKU → 成本无从谈起，也不能算"缺失成本"（归因分离，Q6 裁定）。
                    row.cost_status = COST_STATUS_UNMAPPED
                    row.fee_rate = resolve_fee_rate(record.platform)
                    row.anchored_at = datetime.utcnow()
                elif (
                    previous
                    and previous[1] in ANCHORED_COST_STATUSES
                    and previous[0] == line.external_sku
                ):
                    # ★ 红线 R-3：已锚定 → 原样继承五件套，绝不重新 as-of。
                    # 若在这里重查成本表，「今天补录的成本」就会改写一笔三天前更新过的订单的
                    # 历史利润——这是本切片唯一不可救的架构级错误，宁可保留旧结论。
                    # 附带比对 external_sku：外部行标识若换了商品，旧锚定锚的是别的 SKU 的成本，
                    # 沿用等于用错成本，那种情况落到下面的重新抓取分支。
                    row.cost_snapshot_id = previous[2]
                    row.unit_cost = previous[3]
                    row.fee_rate = previous[4]
                    row.cost_status = previous[1]
                    row.anchored_at = previous[5]
                else:
                    # 新行 / 上次未命中（missing）/ 从 unmapped 升级为 mapped → 走 as-of。
                    pending.append((row, internal_id, sale_time, record.platform))
                touched.add((account.id, line.external_sku))
            stats.affected_dates.append(record.external_created_at.date().isoformat())
        # 成本锚定必须在 flush 之前：此时行对象仍处于 pending 状态、字段可直接赋值，
        # 且整批只发 1 条快照查询（红线 R-2）。
        _anchor_pending_lines(db, pending, workspace_id=workspace_id)
        db.flush()
        for account_id, external_sku in touched:
            _rebuild_key(db, workspace_id=workspace_id, external_account_id=account_id, external_sku=external_sku)
        db.commit()
    except Exception:
        db.rollback()
        raise
    stats.affected_dates = sorted(set(stats.affected_dates or []))
    return stats


def list_daily_sales(
    db: Session,
    *,
    workspace_id: int,
    platform: str | None = None,
    account_ref: str | None = None,
    store_ref: str | None = None,
    external_sku: str | None = None,
    internal_sku_id: int | None = None,
    start: date | None = None,
    end: date | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[DailySkuSale], int]:
    filters = [DailySkuSale.workspace_id == workspace_id]
    for condition in (
        DailySkuSale.platform == platform if platform else None,
        DailySkuSale.account_ref == account_ref if account_ref else None,
        DailySkuSale.store_ref == store_ref if store_ref else None,
        DailySkuSale.external_sku == external_sku if external_sku else None,
        DailySkuSale.internal_sku_id == internal_sku_id if internal_sku_id else None,
        DailySkuSale.sales_date >= start if start else None,
        DailySkuSale.sales_date <= end if end else None,
    ):
        if condition is not None:
            filters.append(condition)
    from sqlalchemy import func
    total = db.scalar(select(func.count(DailySkuSale.id)).where(*filters)) or 0
    rows = db.scalars(select(DailySkuSale).where(*filters).order_by(DailySkuSale.sales_date.desc(), DailySkuSale.id.desc()).offset(offset).limit(limit)).all()
    return rows, int(total)
