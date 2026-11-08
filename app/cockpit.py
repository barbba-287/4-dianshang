"""P4 经营驾驶舱只读聚合服务。

职责：把外部订单事实、内部库存台账、采购待办与知识库发布状态聚合成
老板/运营视角的驾驶舱数据。

纪律（见 dev_task.p4-dashboard.md）：

- **只读**：本模块只执行 SELECT，不出现 ``db.add`` / ``db.commit`` /
  ``db.delete``，不读写任何配置开关，不 import fastapi。
- **零 N+1**：每个指标用一条 SQL 聚合完成；趋势与环比基准用 ``GROUP BY 日``
  一次出「当期 + 上期」全窗口；缺货/积压明细用一条 JOIN + UNION 批量捞取后
  内存组装。``build_cockpit`` 自有查询固定 9 条（P7-S3 新增 2 条利润聚合，
  P9-S1 再加 1 条上期利润合计），加 ``resolve_sales_as_of`` 的 0~2 条 as_of 解析，
  全程查询条数上限 11 条。
- **口径诚实**：多币种分组返回、绝不折算（``fx_todo`` 保留原文）；0 值与
  「没有事实」通过 ``quality`` / ``missing_days`` 区分。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import Integer, case, func, literal, select, union_all
from sqlalchemy.orm import Session

from app.business_dates import resolve_sales_as_of
from app.db import (
    DailySkuSale,
    Document,
    DocumentChunk,
    DocumentVersion,
    ExternalOrder,
    InboundOrder,
    InventoryBalance,
    InventoryPolicy,
    ProductSku,
    PurchaseRequest,
)
from app.finance import SALE_ORDER_STATUSES, profit_totals, summarize_profit

# 销售状态谓词的真源已上移到 app/finance.py（P7-S3）：利润与 GMV 必须用同一组谓词，
# 否则会出现「GMV 有数、利润无数」这种对不上的账。这里只做 re-export，
# tests/test_p4_cockpit.py 对 ``cockpit.SALE_ORDER_STATUSES`` 与 ``_is_sale`` 的对拍断言不变。
__all__ = ["SALE_ORDER_STATUSES", "build_cockpit"]

# 积压（Q-P4-1 口径 A）阈值。
OVERSTOCK_DAYS_THRESHOLD = 90
OVERSTOCK_SALES_WINDOW_DAYS = 30

# 多币种 TODO 原文（Q-P4-3），逐字返回，不得改写。
FX_TODO = "汇率折算依赖财务模块后续提供"

# 采购单状态机（app/purchase_approval.py）：仅 submitted 计入待审批。
PURCHASE_PENDING_STATUS = "submitted"
# 入库单状态（db.py 默认 expected，repository.py 推进 received/confirmed；无取消态）。
INBOUND_PENDING_STATUSES = ("expected", "received")

# 明细列表最多返回条数（列表是入口不是全量报表）。
TODO_DETAIL_LIMIT = 50

LIMITATIONS = (
    "时间基准为 UTC 日界：external_created_at 按 UTC 自然日切分，meta.timezone 固定为 UTC。",
    "GMV 为外部平台订单聚合（Σ(gross_amount − refund_amount)，仅 paid/fulfilled/completed），只读、不等于内部履约收入。",
    "旧接口 /api/dashboard/summary 是库存口径聚合、不含 GMV；GMV 与订单数由本接口 /api/dashboard/cockpit 提供。",
    "缺货判定必须有 inventory_policies 策略；无策略的 SKU 计入 unknown_policy，不默认按 0 阈值判成缺货。",
    "知识库切片数为 DB 口径（已发布版本下的 chunk），发布到向量索引之间存在最终一致窗口。",
    "多币种按币种分组返回，绝不折算、不显示跨币种合计（fx_todo 为财务模块预留锚点）。",
    "积压与待办明细最多返回 50 条，卡片计数为全量计数。",
    "积压速度只认已映射到内部 SKU 的销量（daily_sku_sales.internal_sku_id），未映射 SKU 的销量不参与日均。",
    "预估利润按需实时计算、不落库；口径见 profit.formula（净数量 / 实付净额 / 成本 / 平台费）。",
    "成本状态 missing（无入库成本）或 unmapped（外部 SKU 未映射）的订单行不参与利润汇总，只计数。",
    "多币种时利润 KPI 主值为 null，按 profit.by_currency 分组展示，绝不折算。",
)


def _money(cents) -> str:
    """把「分」整数金额统一成两位小数字符串（金额不落 float 汇总）。"""
    if cents is None:
        return "0.00"
    try:
        amount = Decimal(int(cents)) / Decimal("100")
    except (InvalidOperation, ValueError, TypeError):
        amount = Decimal("0")
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{amount:.2f}"


def _change(current: int, previous: int) -> float | None:
    """环比：(cur − prev) / prev。prev 为 0 时返回 None（不返回 Infinity）。

    不做四舍五入，保留精确比值供调用方核对。
    """
    if previous == 0:
        return None
    return (current - previous) / previous


def _money_change(current: Decimal, previous: Decimal) -> float | None:
    """金额类环比：(cur − prev) / prev。**基数 ≤ 0 时返回 None**。

    为什么不能拿 ``abs(prev)`` 当分母：负基数下没有任何比值能同时表达方向与幅度。

    - 上期 −100、当期 +50（扭亏为盈）：用 prev 得 −150%（读作「下降 150%」），
      用 abs(prev) 得 +150%（方向对了，但「亏损 100 涨到盈利 50」说成「增长 150%」
      仍然会让人误读成利润规模变大）。
    - 上期 −100、当期 −50（亏损收窄）：用 prev 得 −50%（读作「下降 50%」），
      用 abs(prev) 得 +50%（读作「增长了 50%」——实际是亏损减半）。

    所以基数 ≤ 0 一律不给百分比，由调用方用 ``change_basis`` 说明原因。
    """
    if current is None or previous is None:
        return None
    if previous <= 0:
        return None
    return float((current - previous) / previous)


def _iso(value) -> str:
    """把 SQL 返回的日期/日期字符串统一成 ISO 字符串。"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _net_cents_expression():
    """订单净额（分，整数）：max(round((gross − refund) × 100), 0)。

    用整数分做 SQL 聚合，避免浮点累加误差；最终金额以字符串两位小数返回。
    """
    difference = ExternalOrder.gross_amount - ExternalOrder.refund_amount
    return case(
        (difference < 0, 0),
        else_=func.cast(func.round(difference * 100), Integer),
    )


def build_cockpit(
    db: Session,
    *,
    workspace_id: int,
    days: int = 7,
    as_of: date | None = None,
) -> dict:
    """聚合一屏驾驶舱数据（只读）。

    返回结构见 dev_task.p4-dashboard.md §4.1 与 §6。
    """
    if not workspace_id:
        raise ValueError("WORKSPACE_CONTEXT_REQUIRED")
    if days < 1 or days > 90:
        raise ValueError("INVALID_DAYS")

    # as_of 复用既有口径（app/business_dates.py，F-P4-5）。
    as_of = resolve_sales_as_of(db, workspace_id=workspace_id, explicit_as_of=as_of)
    window_from = as_of - timedelta(days=days - 1)
    window_to = as_of
    prev_to = as_of - timedelta(days=days)
    prev_from = prev_to - timedelta(days=days - 1)

    end_dt = datetime(window_to.year, window_to.month, window_to.day) + timedelta(days=1)
    prev_start_dt = datetime(prev_from.year, prev_from.month, prev_from.day)

    # ------------------------------------------------------------------
    # 查询 1：订单聚合（按 日 × 币种）——一次 GROUP BY 出「当期 + 上期」全窗口
    #   环比基准（上等长窗口）与当期窗口在时间上首尾相接，因此用一条查询覆盖
    #   [prev_from, window_to]，再按日期在内存里切分当期/上期，
    #   既满足「趋势一次出全窗口数组」，也不为环比多发一次往返（守卫 16）。
    # ------------------------------------------------------------------
    day_expr = func.date(ExternalOrder.external_created_at)
    net_cents = _net_cents_expression()
    all_order_rows = db.execute(
        select(
            day_expr.label("day"),
            ExternalOrder.currency.label("currency"),
            func.count(func.distinct(ExternalOrder.id)).label("order_count"),
            func.sum(net_cents).label("gmv_cents"),
            func.sum(case((ExternalOrder.data_completeness != "complete", 1), else_=0)).label("partial_rows"),
        ).where(
            ExternalOrder.workspace_id == workspace_id,
            ExternalOrder.order_status.in_(tuple(SALE_ORDER_STATUSES)),
            ExternalOrder.external_created_at >= prev_start_dt,
            ExternalOrder.external_created_at < end_dt,
        ).group_by(day_expr, ExternalOrder.currency)
    ).all()

    # 当期窗口 = as_of 往前 days 天；上期窗口 = 再往前 days 天（相邻、等长）。
    order_rows = [row for row in all_order_rows if _iso(row.day) >= window_from.isoformat()]
    prev_day_rows = [row for row in all_order_rows if _iso(row.day) < window_from.isoformat()]
    prev_by_currency: dict[str, dict] = {}
    for row in prev_day_rows:
        cell = prev_by_currency.setdefault(row.currency, {"gmv_cents": 0, "order_count": 0})
        cell["gmv_cents"] += int(row.gmv_cents or 0)
        cell["order_count"] += int(row.order_count or 0)

    # ------------------------------------------------------------------
    # 查询 2：库存台账 + 策略（一条 UNION，一次捞取后内存组装）
    #   A 段：有策略的 (仓库, SKU) 对，on_hand 来自余额表（缺失按 0）
    #   B 段：有余额但该 (仓库, SKU) 无策略 —— 计入 unknown_policy
    # ------------------------------------------------------------------
    policy_rows = (
        select(
            InventoryPolicy.sku_id.label("sku_id"),
            ProductSku.sku_code.label("sku_code"),
            InventoryPolicy.warehouse_id.label("warehouse_id"),
            func.coalesce(InventoryBalance.on_hand_qty, 0).label("on_hand"),
            InventoryPolicy.safety_stock_qty.label("safety"),
            InventoryPolicy.reorder_point_qty.label("reorder"),
            literal(1).label("has_pair_policy"),
        )
        .select_from(InventoryPolicy)
        .outerjoin(
            ProductSku,
            (ProductSku.id == InventoryPolicy.sku_id)
            & (ProductSku.workspace_id == InventoryPolicy.workspace_id),
        )
        .outerjoin(
            InventoryBalance,
            (InventoryBalance.workspace_id == InventoryPolicy.workspace_id)
            & (InventoryBalance.warehouse_id == InventoryPolicy.warehouse_id)
            & (InventoryBalance.sku_id == InventoryPolicy.sku_id),
        )
        .where(InventoryPolicy.workspace_id == workspace_id)
    )
    orphan_rows = (
        select(
            InventoryBalance.sku_id.label("sku_id"),
            ProductSku.sku_code.label("sku_code"),
            InventoryBalance.warehouse_id.label("warehouse_id"),
            InventoryBalance.on_hand_qty.label("on_hand"),
            literal(None).cast(Integer).label("safety"),
            literal(None).cast(Integer).label("reorder"),
            literal(0).label("has_pair_policy"),
        )
        .select_from(InventoryBalance)
        .outerjoin(
            ProductSku,
            (ProductSku.id == InventoryBalance.sku_id)
            & (ProductSku.workspace_id == InventoryBalance.workspace_id),
        )
        .outerjoin(
            InventoryPolicy,
            (InventoryPolicy.workspace_id == InventoryBalance.workspace_id)
            & (InventoryPolicy.warehouse_id == InventoryBalance.warehouse_id)
            & (InventoryPolicy.sku_id == InventoryBalance.sku_id),
        )
        .where(
            InventoryBalance.workspace_id == workspace_id,
            InventoryPolicy.id.is_(None),
        )
    )
    inventory_union = union_all(policy_rows, orphan_rows).subquery()
    inventory_rows = db.execute(
        select(
            inventory_union.c.sku_id,
            inventory_union.c.sku_code,
            inventory_union.c.warehouse_id,
            inventory_union.c.on_hand,
            inventory_union.c.safety,
            inventory_union.c.reorder,
            inventory_union.c.has_pair_policy,
        )
    ).all()

    # ------------------------------------------------------------------
    # 查询 3：近 30 天销量速度（按 SKU × 日）——积压判定
    # ------------------------------------------------------------------
    sales_start = as_of - timedelta(days=OVERSTOCK_SALES_WINDOW_DAYS - 1)
    sales_rows = db.execute(
        select(
            DailySkuSale.internal_sku_id.label("sku_id"),
            DailySkuSale.sales_date.label("sales_date"),
            func.sum(DailySkuSale.net_qty).label("net_qty"),
            func.min(case((DailySkuSale.data_completeness == "complete", 1), else_=0)).label("all_complete"),
        ).where(
            DailySkuSale.workspace_id == workspace_id,
            DailySkuSale.internal_sku_id.is_not(None),
            DailySkuSale.sales_date >= sales_start,
            DailySkuSale.sales_date <= as_of,
        ).group_by(DailySkuSale.internal_sku_id, DailySkuSale.sales_date)
    ).all()

    # ------------------------------------------------------------------
    # 查询 4 / 5：待办明细（采购待审批 / 未完成入库，各取最近 50 条）
    #   两条普通 SELECT，不用 UNION 复合查询（复合查询的方言渲染与列映射
    #   在 SQLite 上不稳），往返条数仍在预算内（见交付说明的查询条数清单）。
    # ------------------------------------------------------------------
    purchase_rows = db.execute(
        select(
            PurchaseRequest.id.label("id"),
            PurchaseRequest.request_no.label("ref"),
            PurchaseRequest.status.label("status"),
            PurchaseRequest.warehouse_id.label("warehouse_id"),
            PurchaseRequest.created_at.label("created_at"),
        )
        .where(
            PurchaseRequest.workspace_id == workspace_id,
            PurchaseRequest.status == PURCHASE_PENDING_STATUS,
        )
        .order_by(PurchaseRequest.created_at.desc(), PurchaseRequest.id.desc())
        .limit(TODO_DETAIL_LIMIT)
    ).all()
    inbound_rows = db.execute(
        select(
            InboundOrder.id.label("id"),
            InboundOrder.reference_no.label("ref"),
            InboundOrder.status.label("status"),
            InboundOrder.warehouse_id.label("warehouse_id"),
            InboundOrder.created_at.label("created_at"),
        )
        .where(
            InboundOrder.workspace_id == workspace_id,
            InboundOrder.status.in_(INBOUND_PENDING_STATUSES),
        )
        .order_by(InboundOrder.created_at.desc(), InboundOrder.id.desc())
        .limit(TODO_DETAIL_LIMIT)
    ).all()

    # ------------------------------------------------------------------
    # 查询 6：知识库健康 + 待办底数（标量子查询，一次往返）
    # ------------------------------------------------------------------
    counts = db.execute(
        select(
            select(func.count())
            .select_from(Document)
            .where(Document.workspace_id == workspace_id)
            .scalar_subquery()
            .label("documents"),
            select(func.count())
            .select_from(DocumentVersion)
            .where(
                DocumentVersion.workspace_id == workspace_id,
                DocumentVersion.published_at.is_not(None),
            )
            .scalar_subquery()
            .label("published_versions"),
            select(func.count())
            .select_from(DocumentVersion)
            .where(
                DocumentVersion.workspace_id == workspace_id,
                DocumentVersion.status == "parsing",
            )
            .scalar_subquery()
            .label("parsing_versions"),
            select(func.count())
            .select_from(DocumentVersion)
            .where(
                DocumentVersion.workspace_id == workspace_id,
                DocumentVersion.status == "failed",
            )
            .scalar_subquery()
            .label("failed_versions"),
            select(func.count())
            .select_from(DocumentChunk)
            .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
            .where(
                DocumentVersion.workspace_id == workspace_id,
                DocumentVersion.published_at.is_not(None),
            )
            .scalar_subquery()
            .label("available_chunks"),
            select(func.count())
            .select_from(PurchaseRequest)
            .where(
                PurchaseRequest.workspace_id == workspace_id,
                PurchaseRequest.status == PURCHASE_PENDING_STATUS,
            )
            .scalar_subquery()
            .label("pending_purchase"),
            select(func.count())
            .select_from(InboundOrder)
            .where(
                InboundOrder.workspace_id == workspace_id,
                InboundOrder.status.in_(INBOUND_PENDING_STATUSES),
            )
            .scalar_subquery()
            .label("pending_inbound"),
            select(func.count())
            .select_from(PurchaseRequest)
            .where(PurchaseRequest.workspace_id == workspace_id)
            .scalar_subquery()
            .label("purchase_requests"),
            select(func.count())
            .select_from(InboundOrder)
            .where(InboundOrder.workspace_id == workspace_id)
            .scalar_subquery()
            .label("inbound_orders"),
        )
    ).one()

    # ------------------------------------------------------------------
    # 内存组装（不再发查询）
    # ------------------------------------------------------------------
    dates = [(window_from + timedelta(days=index)).isoformat() for index in range(days)]
    day_currency: dict[str, dict[str, dict]] = {}
    for row in order_rows:
        bucket = day_currency.setdefault(_iso(row.day), {})
        bucket[row.currency] = {
            "gmv_cents": int(row.gmv_cents or 0),
            "order_count": int(row.order_count or 0),
            "partial_rows": int(row.partial_rows or 0),
        }

    currencies = sorted({row.currency for row in order_rows}) or sorted(prev_by_currency)
    single_currency = len(currencies) == 1
    multi_currency = len(currencies) > 1

    gmv_by_currency: dict[str, list[str | None]] = {currency: [] for currency in currencies}
    order_counts: list[int] = []
    gmv_series: list[str | None] = []
    days_with_data: list[bool] = []
    missing_days = 0
    partial_rows_total = 0
    order_total = 0
    for day in dates:
        bucket = day_currency.get(day)
        if not bucket:
            missing_days += 1
            gmv_series.append(None)
            order_counts.append(0)
            days_with_data.append(False)
            for currency in currencies:
                gmv_by_currency[currency].append(None)
            continue
        day_orders = 0
        day_cents: dict[str, int] = {}
        for currency in currencies:
            cell = bucket.get(currency)
            if cell is None:
                gmv_by_currency[currency].append(None)
                continue
            day_orders += cell["order_count"]
            day_cents[currency] = cell["gmv_cents"]
            partial_rows_total += cell["partial_rows"]
            gmv_by_currency[currency].append(_money(cell["gmv_cents"]))
        order_total += day_orders
        order_counts.append(day_orders)
        days_with_data.append(True)
        # 主 gmv 序列只在「整个窗口单币种」时有意义（多币种窗口混在一根线里
        # 就是跨币种混加）；多币种时该序列整体留 null，前端按 gmv_by_currency
        # 分币种画线，绝不折算（Q-P4-3）。
        gmv_series.append(_money(next(iter(day_cents.values()))) if single_currency and day_cents else None)

    by_currency = []
    for currency in currencies:
        total_cents = 0
        total_orders = 0
        for day in dates:
            cell = day_currency.get(day, {}).get(currency)
            if cell is None:
                continue
            total_cents += cell["gmv_cents"]
            total_orders += cell["order_count"]
        by_currency.append(
            {
                "currency": currency,
                "gmv": _money(total_cents),
                "order_count": total_orders,
            }
        )

    prev_orders_total = sum(cell["order_count"] for cell in prev_by_currency.values())

    gmv_cents_total = sum(
        int(cell["gmv_cents"])
        for day in dates
        for cell in day_currency.get(day, {}).values()
    )
    gmv_value: str | None = None
    gmv_change: float | None = None
    if single_currency:
        gmv_value = _money(gmv_cents_total)
        gmv_change = _change(
            gmv_cents_total,
            int(prev_by_currency.get(currencies[0], {}).get("gmv_cents", 0)),
        )
    orders_change = _change(order_total, prev_orders_total)

    # ------------------------------------------------------------------
    # 查询 7 / 8 / 9：预估利润（P7-S3 + P9-S1）
    #   窗口与 GMV 完全一致（同一组销售状态谓词、同一个右开日期区间），
    #   否则「GMV 100 万、利润算的是另一批订单」这种口径错位极难被发现。
    #   利润只算可计算行（成本已锚定），missing/unmapped 单独计数进黄色提示。
    #   查询 9 是上期利润合计（1 条，profit_totals），专供环比用——不为一个
    #   比值再调一次 summarize_profit（那会让驾驶舱从 8 条涨到 10 条）。
    # ------------------------------------------------------------------
    profit = summarize_profit(db, workspace_id=workspace_id, window_from=window_from, window_to=window_to)
    profit_currencies = [item["currency"] for item in profit["by_currency"]]
    prev_profit_totals = profit_totals(
        db, workspace_id=workspace_id, window_from=prev_from, window_to=prev_to
    )

    # 利润环比：多币种不折算（无单一可比基数）、当期不可计算与上期基数 ≤ 0 一律不给百分比。
    profit_change_ratio: float | None = None
    profit_change_basis: str | None = None
    if profit["multi_currency"]:
        profit_change_basis = "多币种无法折算单一环比"
    elif profit["total_profit"] is None:
        profit_change_basis = f"当期利润不可计算：{profit['reason']}" if profit["reason"] else "当期利润不可计算"
    else:
        profit_currency = profit_currencies[0] if profit_currencies else "CNY"
        previous_profit = prev_profit_totals.get(profit_currency, Decimal("0"))
        if previous_profit > 0:
            profit_change_ratio = _money_change(Decimal(profit["total_profit"]), previous_profit)
            profit_change_basis = "上一等长窗口"
        elif previous_profit == 0:
            profit_change_basis = "上一窗口利润为 0，无基数"
        else:
            profit_change_basis = "上一窗口利润为负，环比无经济意义"


    # ---------------- 库存分类 + 积压明细 ----------------
    # 缺货/补货判定按 (仓库, SKU) 逐对进行（inventory_policies 的唯一键就是
    # (workspace, warehouse, sku)），再上卷成 SKU 级计数；
    # 积压判定用该 SKU 在全部仓库的库存合计（可售天数需要总量）。
    sku_pairs: dict[int, dict] = {}
    for row in inventory_rows:
        entry = sku_pairs.setdefault(
            row.sku_id,
            {"sku_id": row.sku_id, "sku_code": row.sku_code, "pairs": []},
        )
        entry["pairs"].append(
            {
                "warehouse_id": row.warehouse_id,
                "on_hand": int(row.on_hand or 0),
                "has_policy": bool(int(row.has_pair_policy or 0)),
                "safety": row.safety,
                "reorder": row.reorder,
            }
        )

    # 销量速度：按 SKU 汇总（仅 complete 日参与日均，与 build_sku_health 一致）
    velocity: dict[int, dict] = {}
    for row in sales_rows:
        item = velocity.setdefault(
            row.sku_id,
            {"net_total": 0, "observed_days": 0, "complete_days": 0, "complete_net": 0},
        )
        net = int(row.net_qty or 0)
        item["net_total"] += net
        item["observed_days"] += 1
        if int(row.all_complete or 0):
            item["complete_days"] += 1
            item["complete_net"] += net

    distribution = {
        "out_of_stock": 0,
        "below_reorder": 0,
        "healthy": 0,
        "overstock": 0,
        "unknown_policy": 0,
    }
    shortage_items = []
    overstock_items = []
    overstock_data_insufficient = 0
    for entry in sku_pairs.values():
        pairs = entry["pairs"]
        judged = [pair for pair in pairs if pair["has_policy"]]
        total_on_hand = sum(pair["on_hand"] for pair in pairs)
        if not judged:
            # F-P4-6：无策略不判缺货，单列 unknown_policy（不占饼图）。
            distribution["unknown_policy"] += 1
            continue

        category = "healthy"
        worst_pair = None
        for pair in judged:
            safety = int(pair["safety"] or 0)
            reorder = int(pair["reorder"] or 0)
            if pair["on_hand"] <= safety:
                candidate = "out_of_stock"
            elif pair["on_hand"] <= reorder:
                candidate = "below_reorder"
            else:
                candidate = "healthy"
            if candidate == "out_of_stock":
                category = "out_of_stock"
                worst_pair = pair
                break
            if candidate == "below_reorder":
                category = "below_reorder"
                worst_pair = worst_pair or pair

        reason = None
        avg_daily = None
        days_of_inventory = None
        item = velocity.get(entry["sku_id"])
        if not sales_rows:
            # 全窗口没有任何销量事实 —— 不能断言「无销量」，留给数据质量判定。
            reason = None
        elif item is None:
            # 近 30 天该 SKU 无任何销量事实 → 净销量为 0（口径 A：NO_SALES）
            if total_on_hand > 0:
                reason = "NO_SALES"
        elif item["net_total"] == 0:
            if total_on_hand > 0:
                reason = "NO_SALES"
        elif item["complete_days"] > 0:
            avg_daily = item["complete_net"] / item["complete_days"]
            if avg_daily > 0:
                days_of_inventory = round(total_on_hand / avg_daily, 2)
                if days_of_inventory >= OVERSTOCK_DAYS_THRESHOLD:
                    reason = "SLOW_MOVING"
        else:
            overstock_data_insufficient += 1

        if category in ("out_of_stock", "below_reorder"):
            distribution[category] += 1
            shortage_items.append(
                {
                    "sku_id": entry["sku_id"],
                    "sku_code": entry["sku_code"],
                    "warehouse_id": worst_pair["warehouse_id"] if worst_pair else None,
                    "on_hand_qty": worst_pair["on_hand"] if worst_pair else total_on_hand,
                    "safety_stock_qty": int(worst_pair["safety"] or 0) if worst_pair else 0,
                    "reorder_point_qty": int(worst_pair["reorder"] or 0) if worst_pair else 0,
                    "status": category,
                }
            )
            continue

        # 积压只在「不缺货」的 SKU 上判定（缺货与积压同时命中时按缺货处理，
        # 饼图四类互斥，避免重复处理同一 SKU）。
        if reason is not None:
            distribution["overstock"] += 1
            overstock_items.append(
                {
                    "sku_id": entry["sku_id"],
                    "sku_code": entry["sku_code"],
                    "on_hand_qty": total_on_hand,
                    "avg_daily_qty": round(avg_daily, 4) if avg_daily is not None else None,
                    "days_of_inventory": days_of_inventory,
                    "reason": reason,
                }
            )
        else:
            distribution["healthy"] += 1

    shortage_items.sort(key=lambda item: (item["on_hand_qty"], item["sku_code"] or ""))
    overstock_items.sort(key=lambda item: (-(item["days_of_inventory"] or 0), item["sku_code"] or ""))
    # 明细是入口不是全量报表：最多返回 50 条，卡片计数仍是全量计数。
    shortage_items = shortage_items[:TODO_DETAIL_LIMIT]
    overstock_items = overstock_items[:TODO_DETAIL_LIMIT]

    # ---------------- 待办明细 ----------------
    def _todo_item(row) -> dict:
        return {
            "id": int(row.id),
            "ref": row.ref,
            "status": row.status,
            "warehouse_id": int(row.warehouse_id) if row.warehouse_id is not None else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    purchase_items = [_todo_item(row) for row in purchase_rows]
    inbound_items = [_todo_item(row) for row in inbound_rows]

    # ---------------- 知识库健康 ----------------
    knowledge = {
        "documents": int(counts.documents or 0),
        "published_versions": int(counts.published_versions or 0),
        "available_chunks": int(counts.available_chunks or 0),
        "parsing_versions": int(counts.parsing_versions or 0),
        "failed_versions": int(counts.failed_versions or 0),
    }

    # ---------------- 质量判定 ----------------
    has_order_rows = bool(order_rows)
    window_quality = "complete"
    window_reason = None
    if not has_order_rows:
        window_quality = "insufficient"
        window_reason = "NO_ORDER_FACTS"
    elif partial_rows_total > 0:
        window_quality = "partial"
        window_reason = "PARTIAL_DATA_COMPLETENESS"

    inventory_facts = bool(inventory_rows)
    inventory_quality = "complete"
    inventory_reason = None
    if not inventory_facts:
        inventory_quality = "insufficient"
        inventory_reason = "NO_INVENTORY_FACTS"
    elif distribution["unknown_policy"] > 0:
        inventory_quality = "partial"
        inventory_reason = "SKU_WITHOUT_POLICY"

    sales_facts = bool(sales_rows)
    overstock_quality = "complete"
    overstock_reason = None
    if not sales_facts:
        overstock_quality = "insufficient"
        overstock_reason = "NO_SALES_FACTS"
    elif not inventory_facts:
        overstock_quality = "insufficient"
        overstock_reason = "NO_INVENTORY_FACTS"
    elif overstock_data_insufficient > 0:
        overstock_quality = "partial"
        overstock_reason = "INCOMPLETE_SALES_COVERAGE"

    purchase_quality = "complete" if int(counts.purchase_requests or 0) > 0 else "insufficient"
    purchase_reason = None if purchase_quality == "complete" else "NO_PURCHASE_REQUESTS"
    inbound_quality = "complete" if int(counts.inbound_orders or 0) > 0 else "insufficient"
    inbound_reason = None if inbound_quality == "complete" else "NO_INBOUND_ORDERS"
    knowledge_quality = "complete" if knowledge["documents"] > 0 else "insufficient"
    knowledge_reason = None if knowledge_quality == "complete" else "NO_DOCUMENTS"

    kpis = {
        "gmv": {
            "key": "gmv",
            "label": "GMV",
            "value": gmv_value,
            "unit": currencies[0] if single_currency else None,
            "change_ratio": gmv_change,
            "change_basis": "上一等长窗口" if single_currency else None,
            "quality": window_quality,
            "reason": window_reason,
            "source": "external_orders 聚合（只读）",
            "note": "外部平台订单聚合、只读、不等于内部履约收入",
            "multi_currency": multi_currency,
        },
        "orders": {
            "key": "orders",
            "label": "订单数",
            "value": order_total,
            "unit": "单",
            "change_ratio": orders_change,
            "change_basis": "上一等长窗口",
            "quality": window_quality,
            "reason": window_reason,
            "source": "external_orders 去重计数（与 GMV 同源同谓词）",
            "note": "按订单行去重计数，不使用 daily_sku_sales.order_count 求和",
            "multi_currency": False,
        },
        "out_of_stock_skus": {
            "key": "out_of_stock_skus",
            "label": "缺货 SKU",
            "value": distribution["out_of_stock"],
            "unit": "个",
            "change_ratio": None,
            "change_basis": None,
            "quality": inventory_quality,
            "reason": inventory_reason,
            "source": "inventory_balances + inventory_policies 聚合",
            "note": "on_hand ≤ safety_stock 判定缺货；无策略 SKU 不计入",
            "multi_currency": False,
        },
        "overstock_skus": {
            "key": "overstock_skus",
            "label": "积压 SKU",
            "value": distribution["overstock"],
            "unit": "个",
            "change_ratio": None,
            "change_basis": None,
            "quality": overstock_quality,
            "reason": overstock_reason,
            "source": "inventory_balances + daily_sku_sales（近 30 天）聚合",
            "note": f"近 30 天无销量，或预计可售 ≥ {OVERSTOCK_DAYS_THRESHOLD} 天",
            "multi_currency": False,
        },
        "pending_purchase_approvals": {
            "key": "pending_purchase_approvals",
            "label": "采购待审批",
            "value": int(counts.pending_purchase or 0),
            "unit": "单",
            "change_ratio": None,
            "change_basis": None,
            "quality": purchase_quality,
            "reason": purchase_reason,
            "source": "purchase_requests 聚合",
            "note": "仅统计 status = submitted 的采购单",
            "multi_currency": False,
        },
        "knowledge_chunks": {
            "key": "knowledge_chunks",
            "label": "知识库可用切片",
            "value": knowledge["available_chunks"],
            "unit": "片",
            "change_ratio": None,
            "change_basis": None,
            "quality": knowledge_quality,
            "reason": knowledge_reason,
            "source": "document_versions + document_chunks 聚合（DB 口径）",
            "note": "已发布版本下的切片数；发布到向量索引存在最终一致窗口",
            "multi_currency": False,
        },
        "estimated_profit": {
            "key": "estimated_profit",
            "label": "预估总利润",
            # 多币种时主值必须留 null：把不同币种的利润加在一起得出的数字没有经济意义，
            # 而「显示一个错的总额」比「显示 —」危险得多。
            "value": profit["total_profit"],
            "unit": profit_currencies[0] if len(profit_currencies) == 1 else None,
            "change_ratio": profit_change_ratio,
            "change_basis": profit_change_basis,
            "quality": profit["quality"],
            "reason": profit["reason"],
            "source": "external_order_lines 锚定成本 × 锚定费率（只读、按需计算，不落库）",
            "note": "利润 = 实付净额 − 成本 − 平台费；平台费按实付扣除，退款部分不抽佣",
            "multi_currency": profit["multi_currency"],
        },
    }

    return {
        "meta": {
            "workspace_id": workspace_id,
            "as_of": as_of.isoformat(),
            "timezone": "UTC",
            "generated_at": datetime.utcnow().isoformat(),
            "days": days,
            "from": window_from.isoformat(),
            "to": window_to.isoformat(),
            "prev_from": prev_from.isoformat(),
            "prev_to": prev_to.isoformat(),
            "data_completeness": window_quality,
            "limitations": list(LIMITATIONS),
        },
        "days": days,
        "from": window_from.isoformat(),
        "to": window_to.isoformat(),
        "prev_from": prev_from.isoformat(),
        "prev_to": prev_to.isoformat(),
        "window": {
            "days": days,
            "from": window_from.isoformat(),
            "to": window_to.isoformat(),
            "prev_from": prev_from.isoformat(),
            "prev_to": prev_to.isoformat(),
        },
        "kpis": kpis,
        "profit": profit,
        "by_currency": by_currency,
        "fx_todo": FX_TODO,
        "sales_trend": {
            "dates": dates,
            "gmv": gmv_series,
            "order_counts": order_counts,
            "gmv_by_currency": gmv_by_currency,
            "days_with_data": days_with_data,
            "missing_days": missing_days,
            "currencies": currencies,
        },
        "inventory_distribution": distribution,
        "knowledge": knowledge,
        "todo": {
            "shortage": shortage_items,
            "overstock": overstock_items,
            "purchase_approvals": purchase_items,
            "pending_inbounds": inbound_items,
            "pending_purchase_count": int(counts.pending_purchase or 0),
            "pending_inbound_count": int(counts.pending_inbound or 0),
        },
        "limitations": list(LIMITATIONS),
    }
