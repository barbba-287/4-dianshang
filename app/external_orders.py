"""统一外部订单事实和每日销量聚合服务。

订单事实只更新外部域；不会调用任何内部库存写入路径。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import json

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.connectors import ExternalOrderRecord
from app.config import resolve_fee_rate
from app.order_aggregation import aggregate_order_records, log_merge_reports
from app.order_line_identity import legacy_seq_map, normalize_csv_line_ids
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
    # P9-S3 阶段 B（B-3）：SAVEPOINT 单笔隔离后，一批里可能既有成功的笔也有失败的笔。
    # 失败笔计入 rejected_orders，明细进 rejected_reasons。
    # **两者都不进响应 schema**（`ExternalImportCommitResponse` 的 11 个键不得变），
    # 只进 delivery 的日志与测试断言；批次 run 的状态用 partial 表达（见 external_sync）。
    rejected_orders: int = 0
    rejected_reasons: list[dict] | None = None
    # P9-S3 阶段 B（D5 问题 4）：行级计数必须与订单级计数分开，
    # 否则 incremental 下「入库行数 < CSV 行数」会被误读成丢数据。
    # 同样**不进响应 schema**。full 模式：删行重建 → 全部计入 inserted_lines。
    inserted_lines: int = 0
    updated_lines: int = 0
    skipped_lines: int = 0


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


def _ingest_orders_legacy(
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


@dataclass
class _IngestState:
    """`ingest_orders` 单笔处理过程中的**可变共享状态**。

    B-3 要求：SAVEPOINT 回滚后，`pending` 与 `touched` 必须**干净地**回退到该笔
    之前的状态。给已经回滚掉的 ORM 对象做锚定**不会报错**——对象已被 SQLAlchemy
    expunge 成 transient，赋值只会静默丢失，成本就变成"神秘丢失"（§1.6 坑 1）。
    """

    db: Session
    workspace_id: int
    sync_run_id: str | None
    stats: SalesImportStats
    touched: set[tuple[int, str]]
    pending: list[tuple[ExternalOrderLine, int, datetime, str]]


def _stats_snapshot(stats: SalesImportStats) -> dict:
    """快照订单级/行级计数，供 SAVEPOINT 回滚后回退（§1.6 坑 1）。"""
    return {
        "total": stats.total,
        "inserted": stats.inserted,
        "updated": stats.updated,
        "no_op": stats.no_op,
        "conflict": stats.conflict,
        "stale": stats.stale,
        "inserted_lines": stats.inserted_lines,
        "updated_lines": stats.updated_lines,
        "skipped_lines": stats.skipped_lines,
        "affected_dates": list(stats.affected_dates or []),
    }


def _stats_restore(stats: SalesImportStats, snapshot: dict) -> None:
    """把计数回退到快照（回滚的笔不得留下任何计数痕迹）。"""
    stats.total = snapshot["total"]
    stats.inserted = snapshot["inserted"]
    stats.updated = snapshot["updated"]
    stats.no_op = snapshot["no_op"]
    stats.conflict = snapshot["conflict"]
    stats.stale = snapshot["stale"]
    stats.inserted_lines = snapshot["inserted_lines"]
    stats.updated_lines = snapshot["updated_lines"]
    stats.skipped_lines = snapshot["skipped_lines"]
    stats.affected_dates = list(snapshot["affected_dates"])


def _ingest_order_full(
    state: _IngestState,
    record: ExternalOrderRecord,
    norm_record: ExternalOrderRecord,
) -> None:
    """`line_mode="full"` 的单笔处理：**删除-重建**（P9-S3 之前的老口径）。

    红线 11：`full` 分支的 `stale` 判定、DELETE、单头赋值**语义一个字都没改**。
    本函数只是把原 `ingest_orders` 的循环体**原样搬出来**，好让 SAVEPOINT 能包住它；
    唯一新增的是行级计数（D5 问题 4 要求如实区分 insert/update/skip）与
    `skipped_lines`（stale 整笔跳过的行数）。
    """
    db = state.db
    workspace_id = state.workspace_id
    stats = state.stats
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
    # P9-S3 D2：双索引兼容——指纹索引（新规则）+ 旧 id 索引（存量兜底）。
    inherited_by_fp: dict[tuple[str, int], tuple[str, str, int | None, Decimal | None, Decimal | None, datetime | None]] = {}
    if existing is not None:
        newer = (record.event_version is not None and (existing.event_version is None or record.event_version > existing.event_version)) or (
            record.event_version is None and _utc_naive(record.external_updated_at) > _utc_naive(existing.external_updated_at)
        )
        if existing.payload_hash == record.payload_hash:
            stats.no_op += 1
            return
        if not newer:
            stats.stale += 1
            stats.skipped_lines += len(record.lines)
            return
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
        # P9-S3 D2：指纹索引。旧行的 seq **从 id 解析**，不依赖 select 的返回
        # 顺序（该 select 没有 ORDER BY，按返回位置推断会让行为随数据库漂移）。
        _legacy_seq = legacy_seq_map(old_lines)
        inherited_by_fp = {
            _legacy_seq[line.external_line_id]: (
                line.external_sku,
                line.cost_status or COST_STATUS_MISSING,
                line.cost_snapshot_id,
                line.unit_cost,
                line.fee_rate,
                line.anchored_at,
            )
            for line in old_lines
            if line.external_line_id in _legacy_seq
        }
        state.touched.update((account.id, line.external_sku) for line in old_lines)
        db.execute(delete(ExternalOrderLine).where(ExternalOrderLine.external_order_id == existing.id))
        order = existing
        stats.updated += 1
        state.touched.update((account.id, line.external_sku) for line in record.lines)
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
            source_mode=record.source_mode, simulated=record.simulated, sync_run_id=state.sync_run_id,
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
    order.sync_run_id = state.sync_run_id
    order.data_completeness = record.data_completeness
    order.status_reason = record.status_reason
    sale_time = _utc_naive(record.external_created_at)
    # P9-S3 D2：seq 在「重建循环」里对本订单已见过的同 SKU 行数累加（Counter）。
    sku_counter: Counter[str] = Counter()
    for line, norm_line in zip(record.lines, norm_record.lines):
        mapping = db.scalar(select(ExternalProductMapping).where(
            ExternalProductMapping.workspace_id == workspace_id,
            ExternalProductMapping.external_account_id == account.id,
            ExternalProductMapping.external_sku == line.external_sku,
        ))
        internal_id = mapping.internal_sku_id if mapping and mapping.mapping_status == "mapped" else None
        row = ExternalOrderLine(
            workspace_id=workspace_id, external_order_id=order.id,
            external_line_id=norm_line.external_line_id, external_sku=line.external_sku,
            internal_sku_id=internal_id, ordered_qty=line.ordered_qty,
            cancelled_qty=line.cancelled_qty, refunded_qty=line.refunded_qty,
            gross_amount=line.gross_amount, refund_amount=line.refund_amount,
            currency=line.currency, mapping_status="mapped" if internal_id else "unmapped",
            data_completeness=record.data_completeness, payload_hash=line.payload_hash,
        )
        db.add(row)
        # P9-S3 D2：匹配顺序 = 指纹 → 旧 id → as-of。
        previous = None
        if record.source_mode == "csv":
            _seq = sku_counter[line.external_sku]
            sku_counter[line.external_sku] += 1
            previous = inherited_by_fp.get((line.external_sku, _seq))
        if previous is None:
            previous = inherited.get(norm_line.external_line_id)
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
            state.pending.append((row, internal_id, sale_time, record.platform))
        stats.inserted_lines += 1
        state.touched.add((account.id, line.external_sku))
    stats.affected_dates.append(record.external_created_at.date().isoformat())


def _ingest_order_incremental(
    state: _IngestState,
    record: ExternalOrderRecord,
    norm_record: ExternalOrderRecord,
) -> None:
    """`line_mode="incremental"` 的单笔处理：**逐行 upsert，不删行**。

    与 `full` 的三处本质差异（其余全部沿用 D2 双索引与 R-3）：

    1. **不 DELETE**：本批未出现的旧行原样保留（跨批补行的前提）；
    2. **`newer`/`stale` 下沉到行粒度**：库里没有的行无条件插入，已有的行仍按
       `newer` 判定——整笔 stale 的概念在 incremental 下不存在（D5 问题 2）；
    3. **单头金额按库里全部行重算**（关 R-74），且必须在 flush 之后。
    """
    db = state.db
    workspace_id = state.workspace_id
    stats = state.stats
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
    newer = True
    # D2 双索引（指纹 → 旧 id）。这里存的是 **ORM 行对象**：incremental 要就地更新。
    inherited_by_fp: dict[tuple[str, int], ExternalOrderLine] = {}
    inherited_by_id: dict[str, ExternalOrderLine] = {}
    if existing is not None:
        newer = (record.event_version is not None and (existing.event_version is None or record.event_version > existing.event_version)) or (
            record.event_version is None and _utc_naive(record.external_updated_at) > _utc_naive(existing.external_updated_at)
        )
        if existing.payload_hash == record.payload_hash:
            stats.no_op += 1
            return
        old_lines = db.scalars(
            select(ExternalOrderLine).where(ExternalOrderLine.external_order_id == existing.id)
        ).all()
        # 指纹索引的 seq 仍然**从 id 解析**（legacy_seq_map 是唯一口径）。
        # 同一个指纹命中多行时按 external_line_id 排序取第一行——**不依赖 select 的
        # 返回顺序**，否则「哪一行被更新」会随 SQLite 返回顺序漂移。
        _legacy_seq = legacy_seq_map(old_lines)
        for line in sorted(old_lines, key=lambda item: str(item.external_line_id)):
            _fp = _legacy_seq.get(line.external_line_id)
            if _fp is not None and _fp not in inherited_by_fp:
                inherited_by_fp[_fp] = line
        inherited_by_id = {line.external_line_id: line for line in old_lines}
        order = existing
        # 旧行虽然不删，但订单单头可能变（状态/时间），日销口径跟着变 → 一样要重建。
        state.touched.update((account.id, line.external_sku) for line in old_lines)
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
            source_mode=record.source_mode, simulated=record.simulated, sync_run_id=state.sync_run_id,
            data_completeness=record.data_completeness, status_reason=record.status_reason,
        )
        db.add(order)
        db.flush()
        stats.inserted += 1
    # 单头：**只有本批更新（newer）时才覆盖版本字段**，否则旧批次会把新批次的
    # 订单状态/时间戳改回去。gross/refund 走下面的"按库里全部行重算"。
    order.sync_run_id = state.sync_run_id
    if newer:
        order.order_status = record.order_status
        order.external_created_at = record.external_created_at
        order.paid_at = record.paid_at
        order.external_updated_at = record.external_updated_at
        order.event_version = record.event_version
        order.payload_json = json.dumps(record.as_dict(), ensure_ascii=False, sort_keys=True)
        order.payload_hash = record.payload_hash
        order.idempotency_key = record.idempotency_key
        order.data_completeness = record.data_completeness
        order.status_reason = record.status_reason
    sale_time = _utc_naive(record.external_created_at)
    sku_counter: Counter[str] = Counter()
    effective = 0
    for line, norm_line in zip(record.lines, norm_record.lines):
        mapping = db.scalar(select(ExternalProductMapping).where(
            ExternalProductMapping.workspace_id == workspace_id,
            ExternalProductMapping.external_account_id == account.id,
            ExternalProductMapping.external_sku == line.external_sku,
        ))
        internal_id = mapping.internal_sku_id if mapping and mapping.mapping_status == "mapped" else None
        # D2 双索引：指纹 → 旧 id。
        target = None
        if record.source_mode == "csv":
            _seq = sku_counter[line.external_sku]
            sku_counter[line.external_sku] += 1
            target = inherited_by_fp.get((line.external_sku, _seq))
        if target is None:
            target = inherited_by_id.get(norm_line.external_line_id)
        if target is not None and not newer:
            # 已有行 + 本批不是更新版本 → 该行跳过。旧版本的载荷**不被覆盖**
            # （并发/旧版本保护不变），这是 D5 问题 2 的"两个方向"里的第二个方向。
            stats.skipped_lines += 1
            state.touched.add((account.id, line.external_sku))
            continue
        if target is None:
            # 库里**没有**这一行 → 无条件插入。新增行不存在"版本更旧"的问题，
            # 这正是跨批补行原先被整笔 stale 拦掉的根因（D5 问题 2）。
            row = ExternalOrderLine(
                workspace_id=workspace_id, external_order_id=order.id,
                external_line_id=norm_line.external_line_id, external_sku=line.external_sku,
                internal_sku_id=internal_id, ordered_qty=line.ordered_qty,
                cancelled_qty=line.cancelled_qty, refunded_qty=line.refunded_qty,
                gross_amount=line.gross_amount, refund_amount=line.refund_amount,
                currency=line.currency, mapping_status="mapped" if internal_id else "unmapped",
                data_completeness=record.data_completeness, payload_hash=line.payload_hash,
            )
            db.add(row)
            stats.inserted_lines += 1
            effective += 1
        else:
            # 已有行 + 更新版本 → 就地更新业务字段。
            # **不改 external_line_id**（id 生成规则不动，红线 1）、**不动锚定五件套**。
            row = target
            row.internal_sku_id = internal_id
            row.ordered_qty = line.ordered_qty
            row.cancelled_qty = line.cancelled_qty
            row.refunded_qty = line.refunded_qty
            row.gross_amount = line.gross_amount
            row.refund_amount = line.refund_amount
            row.currency = line.currency
            row.mapping_status = "mapped" if internal_id else "unmapped"
            row.data_completeness = record.data_completeness
            row.payload_hash = line.payload_hash
            stats.updated_lines += 1
            effective += 1
        # 锚定：R-3 —— 已锚定的行原样继承，绝不重新 as-of。
        if internal_id is None:
            row.cost_status = COST_STATUS_UNMAPPED
            row.fee_rate = resolve_fee_rate(record.platform)
            row.anchored_at = datetime.utcnow()
        elif (
            target is not None
            and (target.cost_status or COST_STATUS_MISSING) in ANCHORED_COST_STATUSES
            and target.external_sku == line.external_sku
        ):
            # 原样继承：已锚定行一个字段都不碰（红线 R-3）。
            pass
        else:
            state.pending.append((row, internal_id, sale_time, record.platform))
        state.touched.add((account.id, line.external_sku))
    # R-74：单头金额/退款按**库里该订单全部行**重算（关 R-74）。
    # 必须在 flush 之后——autoflush=False，读不到刚插入的行（D5 问题 1）。
    db.flush()
    _rows = db.scalars(select(ExternalOrderLine).where(
        ExternalOrderLine.external_order_id == order.id
    )).all()
    gross_total = Decimal("0")
    refund_total = Decimal("0")
    for row in _rows:
        gross_total += row.gross_amount or Decimal("0")
        refund_total += row.refund_amount or Decimal("0")
    order.gross_amount = gross_total
    order.refund_amount = refund_total
    if effective == 0:
        # 本批 0 行生效 → 才算 stale（整笔 stale 的概念在 incremental 下取消）。
        if record.lines:
            stats.stale += 1
    elif existing is not None:
        stats.updated += 1
    stats.affected_dates.append(order.external_created_at.date().isoformat())


def ingest_orders(
    db: Session,
    records: list[ExternalOrderRecord],
    *,
    workspace_id: int,
    sync_run_id: str | None = None,
    line_mode: str = "full",
) -> SalesImportStats:
    if not workspace_id:
        raise ValueError("WORKSPACE_CONTEXT_REQUIRED")
    if line_mode not in ("full", "incremental"):
        raise ValueError("LINE_MODE_INVALID")
    # R-66：展平报表预聚合。必须发生在 R-65 判重**之前**——否则「一行一个 SKU」的合法
    # CSV（同一订单号跨多行）会在判重处就被 422 拦死，永远走不到合并。
    # 聚合是纯内存变换：只对 source_mode == "csv" 生效，无 DB 访问，失败即原样返回。
    original_total = len(records)
    records, merge_reports = aggregate_order_records(records)
    if merge_reports:
        log_merge_reports(merge_reports)
    # P9-S3 D2：行 id 稳定化。规范化位置必须在**聚合之后、R-65 判重之前**，
    # 且对所有 source_mode == "csv" 的记录统一处理（单行订单不走 merge，
    # 否则同一订单「3 行聚合」与「1 行」会得到两套 id 规则）。
    # 只改写 external_order_lines.external_line_id 这一个字段；
    # payload_json / payload_hash 仍取规范化之前的原始载荷（组 1 对拍要求逐字节一致）。
    normalized_records = normalize_csv_line_ids(records)
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
        rejected_reasons=[],
    )
    touched: set[tuple[int, str]] = set()
    # P7-S2：待锚定行先攒着，循环外一次性预取快照（红线 R-2：新增查询 ≤1）。
    pending: list[tuple[ExternalOrderLine, int, datetime, str]] = []
    state = _IngestState(
        db=db, workspace_id=workspace_id, sync_run_id=sync_run_id,
        stats=stats, touched=touched, pending=pending,
    )
    try:
        for record, norm_record in zip(records, normalized_records):
            # P9-S3 阶段 B（B-3）：每笔订单包一层 SAVEPOINT。
            # 实测结论见 .testtmp/delivery.p9s3b.md：
            #   - `begin_nested()` 在**创建 savepoint 之前**会隐式 flush 挂起的对象
            #     （探针 P2），所以本笔之前各笔的落库不会受本笔回滚影响（探针 P6/P7）；
            #   - savepoint 回滚会把本笔新建的 ORM 对象 **expunge 成 transient**
            #     （探针 P3），所以 `pending` / `touched` 必须手工回退（§1.6 坑 1）；
            #   - savepoint **能吸收 flush 阶段的 DB 约束冲突**（探针 P4），
            #     所以本笔的 flush 必须放在 savepoint **内**，否则会打穿根事务、整批陪葬。
            _pending_mark = len(pending)
            _touched_mark = set(touched)
            _stats_mark = _stats_snapshot(stats)
            try:
                with db.begin_nested():
                    if line_mode == "incremental":
                        _ingest_order_incremental(state, record, norm_record)
                    else:
                        _ingest_order_full(state, record, norm_record)
                    db.flush()
            except Exception as exc:
                # §1.6 坑 1：回滚后必须把该笔从共享状态里剔除干净。
                # `touched` 用"回退到本笔之前"而不是 `remove`——同一批里两笔订单
                # 可能触及同一个 SKU，直接 remove 会把前一笔的重建信号一起抹掉。
                #
                # R-75 专项（2026-11-09 复核提出）：**同批同 SKU** 时这条路径尤其关键。
                # 本笔失败回滚后，该 SKU 的日销重建信号必须**存活**（本笔之前的前序笔
                # 已经为它付过成本），否则该 SKU 的 `daily_sku_sales` 不会被重建，
                # 轻则日销停在旧值、重则被 `_rebuild_key` 的"先删后建"清空（**蒸发**）。
                # `intersection_update` 恰好满足：只删本笔**新增**的条目，
                # 前序笔已经登记的同一个 SKU 条目原样保留。
                # 守卫用例：`tests/test_p9s3b_order_upsert.py::test_t_sp_3_same_sku_rollback_keeps_daily_sales`
                # （变异实测：把本行改成 `touched.clear()` → 该用例报红）。
                del pending[_pending_mark:]
                touched.intersection_update(_touched_mark)
                _stats_restore(stats, _stats_mark)
                stats.rejected_orders += 1
                stats.rejected_reasons = (stats.rejected_reasons or []) + [{
                    "external_order_no": record.external_order_no,
                    "error_code": str(getattr(exc, "code", type(exc).__name__))[:64],
                    "error_message": str(getattr(exc, "message", exc))[:500],
                }]
                continue
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
