"""P7-S3 利润核算引擎（只读、按需查询）。

**口径写死在本模块顶部**（dev_task.p7-profit-center.md §15.2）。改公式 = 改历史利润，
必须同时改 `FORMULA` 版本号与 `tests/test_p7s3_profit_engine.py` 的手算对拍用例：

    净数量   = 订购数 − 取消数 − 退款数        （下限 0，行级有效数量）
    实付净额 = 商品金额 − 退款金额              （下限 0）
    成本     = 锚定成本单价 × 净数量
    平台费   = 实付净额 × 锚定费率              （按实付扣，退款部分不抽佣）
    利润     = 实付净额 − 成本 − 平台费

三条纪律：

- **只读**：本模块只有 SELECT，不出现 ``db.add`` / ``db.commit`` / ``db.delete``。
  利润永远按需算，不落库、不缓存——落库就要面对「哪天回填了成本、哪天改了费率」的
  重算问题，而这些已经在 P7-S2 用「锚定」解决，重算只会引入新的不一致。
- **不同步路径**：订单 ingest 只做锚定（P7-S2），从不算利润。单笔订单的利润是
  读路径的事，同步路径加一次全表聚合会把它变成写放大。
- **多币种不折算**：按币种分组返回；跨币种合计无经济意义（`fx_todo` 属财务模块）。

查询预算：``summarize_profit`` 固定 **2 条**（1 条分组聚合 + 1 条缺失订单去重计数），
``list_profit_lines`` 2 条（明细 + 总数），``profit_totals`` **1 条**（P9-S1 利润环比用），
``export_profit_lines`` **1 条**（P9-S1 明细导出，多取 1 行判截断）。驾驶舱整体上限
因此从 8 条抬到 9 条。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.db import ExternalOrder, ExternalOrderLine, ProductSku

# 与 app/cockpit.py / app/external_orders.py 的 `_is_sale` 同口径。
# 真源放在本模块：利润、GMV、订单数必须用同一组状态谓词，否则「GMV 有数、利润无数」
# 这类对不上的账会被当成 bug 反复排查。cockpit 从这里 import，测试对拍仍然成立。
SALE_ORDER_STATUSES = frozenset({"paid", "fulfilled", "completed"})

COST_STATUS_RESOLVED = "resolved"
COST_STATUS_BACKFILLED = "backfilled"
COST_STATUS_MISSING = "missing"
COST_STATUS_UNMAPPED = "unmapped"
# 只有这两种状态代表「成本已固化」，才允许参与利润计算。
COMPUTABLE_STATUSES = (COST_STATUS_RESOLVED, COST_STATUS_BACKFILLED)

RANKING_LIMIT = 10
DETAIL_LIMIT_MAX = 200
DETAIL_LIMIT_DEFAULT = 50

# P9-S1：利润明细导出的行数上限。导出是全量语义，超过上限即截断并回响应头，
# 避免一个大窗口把内存和用户的 Excel 一起撑爆。
PROFIT_EXPORT_MAX_ROWS = 20000

# 明细/排行的状态过滤白名单（对外参数，非法值一律 422）。
DETAIL_STATUS_FILTERS = ("all", "computed", COST_STATUS_MISSING, COST_STATUS_UNMAPPED)

FORMULA = {
    "version": "p7s3-v1",
    "net_qty": "净数量 = 订购数 − 取消数 − 退款数（下限 0）",
    "net_revenue": "实付净额 = 商品金额 − 退款金额（下限 0）",
    "cost": "成本 = 锚定成本单价 × 净数量",
    "platform_fee": "平台费 = 实付净额 × 锚定费率（按实付扣，退款部分不抽佣）",
    "profit": "利润 = 实付净额 − 成本 − 平台费",
    "rounding": "平台费按行四舍五入到分后汇总；成本与实付净额本身即为两位小数，无二次舍入",
    "excluded": "成本状态为 missing / unmapped 的订单行不参与利润汇总，单独计数后进黄色提示",
}

PROFIT_LIMITATIONS = (
    "利润为**预估**口径：平台费按锚定费率估算，不含佣金阶梯、支付手续费、运费、仓储与退货处理成本。",
    "退款行的成本按净数量同步冲减（视为退货回仓）；若实际未回仓，真实成本会高于本口径。",
    "成本状态为 missing（无入库成本）或 unmapped（外部 SKU 未映射）的订单行不计入利润，"
    "仅在 missing_cost_order_count 中计数。",
    "多币种按币种分组返回、绝不折算；跨币种不显示合计。",
    "利润按需实时计算、不落库：成本或费率改动只影响之后的锚定，已锚定行永远按当时的成本/费率重算。",
)


def _dec(value) -> Decimal:
    """把 SQL 聚合结果（Decimal / float / int / str / None）统一成 Decimal。"""
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _money(value) -> str:
    """两位小数金额字符串（金额不落 float 参与汇总）。"""
    return f"{_dec(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def _cent(value) -> Decimal:
    return _dec(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _ratio(value) -> str:
    """费率按四位小数返回（与 external_order_lines.fee_rate 精度一致）。"""
    return f"{_dec(value).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP):.4f}"


def net_qty(ordered_qty: int, cancelled_qty: int, refunded_qty: int) -> int:
    """净数量：行级有效数量，下限 0（超额退款不产生负数量）。"""
    return max(0, int(ordered_qty or 0) - int(cancelled_qty or 0) - int(refunded_qty or 0))


def _qty_expression():
    """SQL 侧净数量表达式，与 ``net_qty`` 逐字同口径。

    用 ``case`` 而非 ``greatest/max``：SQLite 的两参 ``max`` 是标量函数、PostgreSQL 没有
    两参 ``max``，``case`` 是三种方言都稳的唯一写法。
    """
    raw = (
        ExternalOrderLine.ordered_qty
        - ExternalOrderLine.cancelled_qty
        - ExternalOrderLine.refunded_qty
    )
    return case((raw > 0, raw), else_=0)


def _revenue_expression():
    raw = ExternalOrderLine.gross_amount - ExternalOrderLine.refund_amount
    return case((raw > 0, raw), else_=0)


def _window_filters(workspace_id: int, window_from: date, window_to: date) -> list:
    """销售窗口谓词，与 cockpit 的 GMV 同一边界（UTC 日界，右开区间）。"""
    start_dt = datetime(window_from.year, window_from.month, window_from.day)
    end_dt = datetime(window_to.year, window_to.month, window_to.day) + timedelta(days=1)
    return [
        ExternalOrder.workspace_id == workspace_id,
        ExternalOrder.order_status.in_(tuple(SALE_ORDER_STATUSES)),
        ExternalOrder.external_created_at >= start_dt,
        ExternalOrder.external_created_at < end_dt,
    ]


def _empty_summary() -> dict:
    return {
        "formula": dict(FORMULA),
        "by_currency": [],
        "total_profit": None,
        "multi_currency": False,
        "quality": "insufficient",
        "reason": "NO_ORDERS",
        "computed_lines": 0,
        "missing_cost_order_count": 0,
        "missing_cost_line_count": 0,
        "unmapped_line_count": 0,
        "warning": {
            "show": False,
            "level": "warning",
            "message": None,
            "missing_cost_order_count": 0,
        },
        "ranking": {"winners": [], "losers": [], "currencies": [], "limit": RANKING_LIMIT},
        "limitations": list(PROFIT_LIMITATIONS),
    }


def summarize_profit(
    db: Session,
    *,
    workspace_id: int,
    window_from: date,
    window_to: date,
) -> dict:
    """按需汇总一个销售窗口内的预估利润（2 条查询，零 N+1）。

    返回结构见 dev_task.p7-profit-center.md §15.3。绝不抛业务异常：驾驶舱不能因为
    「一单成本都没有」而 500，缺数据用 ``quality`` / ``warning`` 表达。
    """
    if not workspace_id:
        raise ValueError("WORKSPACE_CONTEXT_REQUIRED")
    filters = _window_filters(workspace_id, window_from, window_to)

    qty_expr = _qty_expression()
    revenue_expr = _revenue_expression()
    # 参与汇总的前提：成本已锚定 + 单价/费率都有值。三者缺一，利润就是「不知道」，
    # 不能当 0 算——把 unknown 当 0 是利润类报表最典型的失真来源。
    computable = (
        ExternalOrderLine.cost_status.in_(COMPUTABLE_STATUSES)
        & ExternalOrderLine.unit_cost.is_not(None)
        & ExternalOrderLine.fee_rate.is_not(None)
    )
    cost_expr = case((computable, ExternalOrderLine.unit_cost * qty_expr), else_=0)
    # 平台费**逐行**四舍五入到分再汇总，这样「排行/明细各行之和」与「KPI 总额」永远相等；
    # 若改成「汇总后再舍入」，明细加起来会与总额差几分钱，财务当场就会问为什么。
    fee_expr = case((computable, func.round(revenue_expr * ExternalOrderLine.fee_rate, 2)), else_=0)

    rows = db.execute(
        select(
            func.coalesce(ExternalOrderLine.currency, "CNY").label("currency"),
            ExternalOrderLine.internal_sku_id.label("sku_id"),
            ProductSku.sku_code.label("sku_code"),
            ExternalOrderLine.cost_status.label("cost_status"),
            func.sum(qty_expr).label("net_qty"),
            func.sum(revenue_expr).label("revenue"),
            func.sum(cost_expr).label("cost"),
            func.sum(fee_expr).label("fee"),
            func.count(ExternalOrderLine.id).label("line_count"),
        )
        .select_from(ExternalOrderLine)
        .join(ExternalOrder, ExternalOrder.id == ExternalOrderLine.external_order_id)
        .outerjoin(
            ProductSku,
            (ProductSku.id == ExternalOrderLine.internal_sku_id)
            & (ProductSku.workspace_id == ExternalOrderLine.workspace_id),
        )
        .where(*filters)
        .group_by(
            func.coalesce(ExternalOrderLine.currency, "CNY"),
            ExternalOrderLine.internal_sku_id,
            ProductSku.sku_code,
            ExternalOrderLine.cost_status,
        )
    ).all()

    # 查询 2：缺成本的订单数**按订单去重**（一笔订单 3 个 SKU 都缺成本算 1 笔）。
    # 分组聚合里做不了跨组去重，所以单独一条；这是驾驶舱预算 6→8 的其中一条。
    missing_order_count = int(
        db.scalar(
            select(func.count(func.distinct(ExternalOrderLine.external_order_id)))
            .select_from(ExternalOrderLine)
            .join(ExternalOrder, ExternalOrder.id == ExternalOrderLine.external_order_id)
            .where(*filters, ExternalOrderLine.cost_status == COST_STATUS_MISSING)
        )
        or 0
    )

    if not rows:
        return _empty_summary()

    by_currency: dict[str, dict] = {}
    sku_totals: dict[tuple[str, int | None, str | None], dict] = {}
    computed_lines = 0
    missing_line_count = 0
    unmapped_line_count = 0

    for row in rows:
        currency = row.currency or "CNY"
        status = row.cost_status or COST_STATUS_MISSING
        line_count = int(row.line_count or 0)
        cell = by_currency.setdefault(
            currency,
            {"currency": currency, "revenue": Decimal("0"), "cost": Decimal("0"),
             "platform_fee": Decimal("0"), "profit": Decimal("0"),
             "computed_lines": 0, "missing_lines": 0, "unmapped_lines": 0, "net_qty": 0,
             "skus": set()},
        )
        if status == COST_STATUS_MISSING:
            missing_line_count += line_count
            cell["missing_lines"] += line_count
            continue
        if status == COST_STATUS_UNMAPPED:
            unmapped_line_count += line_count
            cell["unmapped_lines"] += line_count
            continue
        if status not in COMPUTABLE_STATUSES:
            # 未知状态按「不可算」处理，绝不默认成 0 利润。
            missing_line_count += line_count
            cell["missing_lines"] += line_count
            continue

        revenue = _cent(row.revenue)
        cost = _cent(row.cost)
        fee = _cent(row.fee)
        cell["revenue"] += revenue
        cell["cost"] += cost
        cell["platform_fee"] += fee
        cell["profit"] += revenue - cost - fee
        cell["computed_lines"] += line_count
        cell["net_qty"] += int(row.net_qty or 0)
        cell["skus"].add(row.sku_id)
        computed_lines += line_count

        key = (currency, row.sku_id, row.sku_code)
        bucket = sku_totals.setdefault(
            key,
            {"sku_id": row.sku_id, "sku_code": row.sku_code, "currency": currency,
             "net_qty": 0, "revenue": Decimal("0"), "cost": Decimal("0"),
             "platform_fee": Decimal("0"), "profit": Decimal("0"), "line_count": 0},
        )
        bucket["net_qty"] += int(row.net_qty or 0)
        bucket["revenue"] += revenue
        bucket["cost"] += cost
        bucket["platform_fee"] += fee
        bucket["profit"] += revenue - cost - fee
        bucket["line_count"] += line_count

    currencies = sorted(by_currency)
    multi_currency = len(currencies) > 1
    payload_by_currency = []
    for currency in currencies:
        cell = by_currency[currency]
        payload_by_currency.append(
            {
                "currency": currency,
                "revenue": _money(cell["revenue"]),
                "cost": _money(cell["cost"]),
                "platform_fee": _money(cell["platform_fee"]),
                "profit": _money(cell["profit"]),
                "profit_margin": (
                    f"{(_cent(cell['profit']) / _cent(cell['revenue'])).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP):.4f}"
                    if _cent(cell["revenue"]) > 0
                    else None
                ),
                "net_qty": cell["net_qty"],
                "computed_lines": cell["computed_lines"],
                "missing_lines": cell["missing_lines"],
                "unmapped_lines": cell["unmapped_lines"],
                "sku_count": len(cell["skus"]),
            }
        )

    # 一行都算不出来时主值必须是 null 而不是 "0.00"：
    # 「不知道」和「不赚钱」在利润表上是两件事，显示 0 会让读者以为这窗口真的白干。
    total_profit = (
        _money(by_currency[currencies[0]]["profit"])
        if (currencies and not multi_currency and computed_lines > 0)
        else None
    )

    if computed_lines == 0:
        quality, reason = "insufficient", "NO_COST_SNAPSHOTS"
    elif missing_line_count > 0 or unmapped_line_count > 0:
        quality, reason = "partial", "MISSING_COST_SNAPSHOTS"
    else:
        quality, reason = "complete", None

    warning_message = (
        f"有 {missing_order_count} 笔订单因缺失入库成本未能计算利润" if missing_order_count else None
    )

    return {
        "formula": dict(FORMULA),
        "by_currency": payload_by_currency,
        "total_profit": total_profit,
        "multi_currency": multi_currency,
        "quality": quality,
        "reason": reason,
        "computed_lines": computed_lines,
        "missing_cost_order_count": missing_order_count,
        "missing_cost_line_count": missing_line_count,
        "unmapped_line_count": unmapped_line_count,
        "warning": {
            "show": missing_order_count > 0,
            "level": "warning",
            "message": warning_message,
            "missing_cost_order_count": missing_order_count,
        },
        "ranking": _build_ranking(sku_totals),
        "limitations": list(PROFIT_LIMITATIONS),
    }


def _build_ranking(sku_totals: dict) -> dict:
    """单品利润排行：只在**可计算**行里排，按币种分组（跨币种不可比）。

    winners = 利润 > 0 降序 Top10；losers = 利润 < 0 升序 Top10（亏得最多在前）。
    利润恰为 0 的行两边都不进——它不是「赚」也不是「亏」，进任何一边都在误导读者。
    """
    buckets: dict[str, list[dict]] = {}
    for bucket in sku_totals.values():
        buckets.setdefault(bucket["currency"], []).append(bucket)

    winners: list[dict] = []
    losers: list[dict] = []
    for currency in sorted(buckets):
        items = buckets[currency]
        ordered = sorted(items, key=lambda item: (item["profit"], item["sku_code"] or ""), reverse=True)
        gains = [_ranking_item(item) for item in ordered if item["profit"] > 0][:RANKING_LIMIT]
        losses = [
            _ranking_item(item)
            for item in sorted(items, key=lambda item: (item["profit"], item["sku_code"] or ""))
            if item["profit"] < 0
        ][:RANKING_LIMIT]
        # 每个币种各自 Top10，不做跨币种混排（混排等于把不同货币的利润放一起比大小）。
        winners.extend(gains)
        losers.extend(losses)
    for item in winners + losers:
        item.pop("_profit", None)
    return {
        "winners": winners,
        "losers": losers,
        "currencies": sorted(buckets),
        "limit": RANKING_LIMIT,
    }


def _ranking_item(item: dict) -> dict:
    revenue = _cent(item["revenue"])
    profit = _cent(item["profit"])
    return {
        "sku_id": item["sku_id"],
        "sku_code": item["sku_code"],
        "currency": item["currency"],
        "net_qty": item["net_qty"],
        "revenue": _money(revenue),
        "cost": _money(item["cost"]),
        "platform_fee": _money(item["platform_fee"]),
        "profit": _money(profit),
        "profit_margin": (
            f"{(profit / revenue).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP):.4f}"
            if revenue > 0
            else None
        ),
        "line_count": item["line_count"],
        "_profit": profit,
    }


def line_amounts(
    line: ExternalOrderLine,
    *,
    unit_cost: Decimal | None = None,
    fee_rate: Decimal | None = None,
) -> dict:
    """单行利润拆解（明细下钻与测试手算共用同一份实现）。

    返回 ``profit=None`` 表示不可计算（缺成本 / 未映射），此时 ``cost_status``
    说明原因——前端据此显示「缺失成本」而不是把 0 当成利润。
    """
    cost_status = line.cost_status or COST_STATUS_MISSING
    unit_cost = line.unit_cost if unit_cost is None else unit_cost
    fee_rate = line.fee_rate if fee_rate is None else fee_rate
    quantity = net_qty(line.ordered_qty, line.cancelled_qty, line.refunded_qty)
    revenue = _cent(max(Decimal("0"), _dec(line.gross_amount) - _dec(line.refund_amount)))
    payload = {
        "external_line_id": line.external_line_id,
        "external_sku": line.external_sku,
        "internal_sku_id": line.internal_sku_id,
        "currency": line.currency or "CNY",
        "ordered_qty": int(line.ordered_qty or 0),
        "cancelled_qty": int(line.cancelled_qty or 0),
        "refunded_qty": int(line.refunded_qty or 0),
        "net_qty": quantity,
        "net_revenue": _money(revenue),
        "unit_cost": None if unit_cost is None else _money(unit_cost),
        "fee_rate": None if fee_rate is None else _ratio(fee_rate),
        "cost": "0.00",
        "platform_fee": "0.00",
        "profit": None,
        "cost_status": cost_status,
    }
    if cost_status not in COMPUTABLE_STATUSES or unit_cost is None or fee_rate is None:
        return payload
    cost = _cent(_dec(unit_cost) * quantity)
    fee = _cent(revenue * _dec(fee_rate))
    payload["cost"] = _money(cost)
    payload["platform_fee"] = _money(fee)
    payload["profit"] = _money(revenue - cost - fee)
    return payload


def list_profit_lines(
    db: Session,
    *,
    workspace_id: int,
    window_from: date,
    window_to: date,
    currency: str | None = None,
    status: str = "all",
    sku_code: str | None = None,
    limit: int = DETAIL_LIMIT_DEFAULT,
    offset: int = 0,
) -> dict:
    """利润明细下钻（订单行粒度，2 条查询）。

    ``status``：all / computed / missing / unmapped。缺省 all —— 下钻的目的是让人看懂
    「为什么总额是这个数」，把不可计算的行藏起来只会让总额显得更没依据。

    ``sku_code``：按**内部编码**过滤（P9-S1）。命中不到时返回空结果而非 422 ——
    SKU 被删、拼写错误都该表现为「没有数据」，不该把用户挡在门外。
    """
    if not workspace_id:
        raise ValueError("WORKSPACE_CONTEXT_REQUIRED")
    if status not in DETAIL_STATUS_FILTERS:
        raise ValueError("INVALID_PROFIT_STATUS")
    if limit < 1 or limit > DETAIL_LIMIT_MAX:
        raise ValueError("INVALID_PAGINATION")
    if offset < 0:
        raise ValueError("INVALID_PAGINATION")
    filters = _window_filters(workspace_id, window_from, window_to)
    if currency:
        filters.append(func.coalesce(ExternalOrderLine.currency, "CNY") == currency)
    if status == "computed":
        filters.append(ExternalOrderLine.cost_status.in_(COMPUTABLE_STATUSES))
    elif status in (COST_STATUS_MISSING, COST_STATUS_UNMAPPED):
        filters.append(ExternalOrderLine.cost_status == status)
    if sku_code:
        # 内部编码过滤（与 /orders?sku= 的预填一致）。outerjoin + where 等价于
        # innerjoin 语义：SKU 不存在时命中不到任何行，返回空结果而不是 422。
        filters.append(ProductSku.sku_code == sku_code)

    base = (
        select(ExternalOrderLine, ExternalOrder)
        .join(ExternalOrder, ExternalOrder.id == ExternalOrderLine.external_order_id)
        .outerjoin(
            ProductSku,
            (ProductSku.id == ExternalOrderLine.internal_sku_id)
            & (ProductSku.workspace_id == ExternalOrderLine.workspace_id),
        )
        .where(*filters)
    )
    total = int(
        db.scalar(
            select(func.count(ExternalOrderLine.id))
            .select_from(ExternalOrderLine)
            .join(ExternalOrder, ExternalOrder.id == ExternalOrderLine.external_order_id)
            .outerjoin(
                ProductSku,
                (ProductSku.id == ExternalOrderLine.internal_sku_id)
                & (ProductSku.workspace_id == ExternalOrderLine.workspace_id),
            )
            .where(*filters)
        )
        or 0
    )
    rows = db.execute(
        base.order_by(ExternalOrder.external_created_at.desc(), ExternalOrderLine.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    sku_codes: dict[int, str] = {}
    sku_ids = {line.internal_sku_id for line, _order in rows if line.internal_sku_id is not None}
    if sku_ids:
        for sku_id, sku_code in db.execute(
            select(ProductSku.id, ProductSku.sku_code).where(
                ProductSku.workspace_id == workspace_id, ProductSku.id.in_(sku_ids)
            )
        ).all():
            sku_codes[int(sku_id)] = sku_code

    items = []
    for line, order in rows:
        item = line_amounts(line)
        item.update(
            {
                "order_id": order.id,
                "external_order_no": order.external_order_no,
                "platform": order.platform,
                "order_status": order.order_status,
                "ordered_at": order.external_created_at.isoformat() if order.external_created_at else None,
                "sku_code": sku_codes.get(int(line.internal_sku_id)) if line.internal_sku_id else None,
            }
        )
        items.append(item)

    # 汇总只统计「当前过滤条件下的可计算行」，与 KPI 的 by_currency 同口径。
    computed = [item for item in items if item["profit"] is not None]
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "status": status,
        "currency": currency,
        "formula": dict(FORMULA),
        "page_profit": _money(sum((Decimal(item["profit"]) for item in computed), Decimal("0"))),
        "page_computed": len(computed),
        "limitations": list(PROFIT_LIMITATIONS),
    }


# ---------------------------------------------------------------------------
# P9-S1：利润环比取数 + 利润明细全量导出
# ---------------------------------------------------------------------------

# 导出列定义（顺序写死，前端与测试都对齐这张表）。
PROFIT_EXPORT_HEADERS = (
    "订单号", "下单时间", "SKU", "币种", "净数量",
    "实付净额", "成本", "平台费", "利润", "成本状态",
)

# 成本状态 → 导出用的中文标签。缺费率是兜底分支：状态已锚定但单价/费率缺失，
# 此时利润不可计算，得让看表的人知道「缺的是费率」而不是「缺成本」。
COST_STATUS_LABELS = {
    COST_STATUS_RESOLVED: "已锚定",
    COST_STATUS_BACKFILLED: "已锚定",
    COST_STATUS_MISSING: "缺成本",
    COST_STATUS_UNMAPPED: "未映射 SKU",
}
COST_STATUS_LABEL_FALLBACK = "缺费率"


def _computable_expression():
    """可计算谓词：成本已锚定 + 单价与费率都有值（三者缺一，利润就是「不知道」）。"""
    return (
        ExternalOrderLine.cost_status.in_(COMPUTABLE_STATUSES)
        & ExternalOrderLine.unit_cost.is_not(None)
        & ExternalOrderLine.fee_rate.is_not(None)
    )


def profit_totals(
    db: Session,
    *,
    workspace_id: int,
    window_from: date,
    window_to: date,
) -> dict[str, Decimal]:
    """按币种返回窗口内的预估利润合计（**1 条查询**，只含可计算行）。

    存在的理由：环比只需要「每币种的利润合计」，不需要 by_currency 的完整结构、
    不需要排行、不需要 missing 计数。为了这一个数再调一次 ``summarize_profit``
    会让驾驶舱查询从 8 条涨到 10 条（它固定 2 条），而这里 1 条就够。

    口径与 ``summarize_profit`` **逐字同源**：成本 = 单价 × 净数量、
    平台费 = round(实付净额 × 费率, 2) 逐行舍入后汇总、利润 = 实付净额 − 成本 − 平台费。
    窗口内无可计算行时返回空 dict（不是 ``{"CNY": 0}``）——「没有行」与「利润是 0」
    在报表上是两件事。
    """
    if not workspace_id:
        raise ValueError("WORKSPACE_CONTEXT_REQUIRED")

    filters = _window_filters(workspace_id, window_from, window_to)
    qty_expr = _qty_expression()
    revenue_expr = _revenue_expression()
    computable = _computable_expression()
    cost_expr = case((computable, ExternalOrderLine.unit_cost * qty_expr), else_=0)
    fee_expr = case((computable, func.round(revenue_expr * ExternalOrderLine.fee_rate, 2)), else_=0)
    profit_expr = revenue_expr - cost_expr - fee_expr

    rows = db.execute(
        select(
            func.coalesce(ExternalOrderLine.currency, "CNY").label("currency"),
            func.sum(profit_expr).label("profit"),
        )
        .select_from(ExternalOrderLine)
        .join(ExternalOrder, ExternalOrder.id == ExternalOrderLine.external_order_id)
        .where(*filters, computable)
        .group_by(func.coalesce(ExternalOrderLine.currency, "CNY"))
    ).all()

    if not rows:
        return {}
    return {(row.currency or "CNY"): _cent(row.profit) for row in rows}


PROFIT_TREND_MAX_POINTS = 12


def profit_trend(
    db: Session,
    *,
    workspace_id: int,
    as_of: date,
    days: int,
    points: int,
) -> dict:
    """最近 ``points`` 个等长窗口的利润合计序列（每窗口 1 条查询）。

    窗口语义与驾驶舱本期/上期**逐字一致**（见 app/cockpit.py 的推导）：
    第 i 期（i=0 最新）= [as_of - i*days - (days-1), as_of - i*days]，UTC 日界右开。
    相邻窗口首尾相接、不重叠。

    口径与 ``profit_totals`` **逐字同源**（不重写成本/平台费/利润的聚合），
    因此趋势与利润环比永远对得上——两者用的是同一个函数。

    返回结构::

        {
          "as_of": "2026-11-09",
          "days": 7,
          "points": [
             {"from": "2026-10-29", "to": "2026-11-04", "label": "11-04",
              "profit": "123.45", "has_data": true},
             ...
          ],
          "currency": "CNY" | None,
          "multi_currency": false,
          "reason": None | "多币种无法折算单一趋势",
        }

    ``points`` 数组**按时间正序**（最早在前、最新在后），前端 x 轴直接用。

    三条关键语义（与 ``profit_totals`` 的 docstring 一致）：

    1. **缺失期是 ``None``，不是 ``"0.00"``**：``profit_totals`` 返回空 dict 时该期
       没有可计算行，「没有行」与「利润是 0」是两件事，折线必须断开而不是扎到 0。
    2. **多币种不折算**：出现 ≥2 个币种时返回 ``multi_currency=True`` 且 ``points=[]``，
       宁可不给曲线，也不画一个不存在的数。
    3. **金额不落 float**：Decimal → 两位小数字符串。
    """
    if not workspace_id:
        raise ValueError("WORKSPACE_CONTEXT_REQUIRED")
    if points < 1 or points > PROFIT_TREND_MAX_POINTS or days < 1 or days > 90:
        raise ValueError("INVALID_TREND_PARAMS")

    # 逐期调用 profit_totals（每窗口 1 条查询），不做单条拼接 SQL：
    # 分桶表达式会与 _window_filters 的右开日界产生第二套口径，两套口径迟早漂移。
    series: list[dict] = []
    currencies: set[str] = set()
    for index in range(points):
        window_to = as_of - timedelta(days=index * days)
        window_from = window_to - timedelta(days=days - 1)
        totals = profit_totals(
            db,
            workspace_id=workspace_id,
            window_from=window_from,
            window_to=window_to,
        )
        currencies.update(totals.keys())
        series.append({
            "from": window_from.isoformat(),
            "to": window_to.isoformat(),
            "label": f"{window_to.month:02d}-{window_to.day:02d}",
            "_totals": totals,
        })

    if len(currencies) >= 2:
        # 多币种：无法折算成一根线，直接不画。
        return {
            "as_of": as_of.isoformat(),
            "days": days,
            "points": [],
            "currency": None,
            "multi_currency": True,
            "reason": "多币种无法折算单一趋势",
        }

    currency = next(iter(currencies), None)
    points_out: list[dict] = []
    for item in reversed(series):   # 正序输出：最早在前，最新在后
        totals = item.pop("_totals")
        value = totals.get(currency) if currency else None
        points_out.append({
            **item,
            "profit": f"{value:.2f}" if value is not None else None,
            "has_data": value is not None,
        })

    return {
        "as_of": as_of.isoformat(),
        "days": days,
        "points": points_out,
        "currency": currency,
        "multi_currency": False,
        "reason": None,
    }


def _export_cost_status_label(line: ExternalOrderLine, *, computed: bool) -> str:
    """成本状态中文标签。"""
    if computed:
        return COST_STATUS_LABELS.get(line.cost_status or COST_STATUS_MISSING, COST_STATUS_LABEL_FALLBACK)
    status = line.cost_status or COST_STATUS_MISSING
    if status in (COST_STATUS_MISSING, COST_STATUS_UNMAPPED):
        return COST_STATUS_LABELS[status]
    return COST_STATUS_LABEL_FALLBACK


def export_profit_lines(
    db: Session,
    *,
    workspace_id: int,
    window_from: date,
    window_to: date,
    currency: str | None = None,
    status: str = "all",
    sku_code: str | None = None,
    max_rows: int | None = None,
) -> tuple[list[str], list[list[str]], bool]:
    """利润明细全量导出（**1 条查询**，最多 ``max_rows`` 行）。

    返回 ``(headers, rows, truncated)``。

    两条硬规则：

    1. **不可计算行的金额列留空**，绝不填 0 —— 导出文件会被拿去 SUM，填 0 等于把
       「不知道」污染成「不赚钱」，这正是 F-51 锁死的行为。
    2. **导出是全量语义**，不带分页参数。导出带分页会产生「导出第 2 页」这种
       没人用的功能，却让人以为可以分片取全量。

    多取 1 行用于判断是否截断（``limit(max_rows + 1)``），不做第二条 count 查询。
    ``max_rows`` 缺省读模块常量（哨兵 None 而不是把常量写进默认值，
    这样上限值可被测试与运维在运行时改写）。
    """
    if not workspace_id:
        raise ValueError("WORKSPACE_CONTEXT_REQUIRED")
    if status not in DETAIL_STATUS_FILTERS:
        raise ValueError("INVALID_PROFIT_STATUS")
    if max_rows is None:
        max_rows = PROFIT_EXPORT_MAX_ROWS
    if max_rows < 1:
        raise ValueError("INVALID_PAGINATION")

    filters = _window_filters(workspace_id, window_from, window_to)
    if currency:
        filters.append(func.coalesce(ExternalOrderLine.currency, "CNY") == currency)
    if status == "computed":
        filters.append(ExternalOrderLine.cost_status.in_(COMPUTABLE_STATUSES))
    elif status in (COST_STATUS_MISSING, COST_STATUS_UNMAPPED):
        filters.append(ExternalOrderLine.cost_status == status)
    if sku_code:
        filters.append(ProductSku.sku_code == sku_code)

    rows = db.execute(
        select(ExternalOrderLine, ExternalOrder, ProductSku.sku_code)
        .select_from(ExternalOrderLine)
        .join(ExternalOrder, ExternalOrder.id == ExternalOrderLine.external_order_id)
        .outerjoin(
            ProductSku,
            (ProductSku.id == ExternalOrderLine.internal_sku_id)
            & (ProductSku.workspace_id == ExternalOrderLine.workspace_id),
        )
        .where(*filters)
        .order_by(ExternalOrder.external_created_at.desc(), ExternalOrderLine.id.desc())
        .limit(max_rows + 1)
    ).all()

    truncated = len(rows) > max_rows
    rows = rows[:max_rows]

    table: list[list[str]] = []
    for line, order, sku_code_value in rows:
        # 行金额复用 line_amounts —— 与明细下钻、测试手算共用同一份实现，
        # 两套算法迟早会漂移，漂移的那天没人知道哪个才是对的。
        item = line_amounts(line)
        computed = item["profit"] is not None
        created = order.external_created_at or order.paid_at
        table.append(
            [
                order.external_order_no or "",
                created.isoformat() if created else "",
                sku_code_value or item["external_sku"] or "—",
                item["currency"],
                str(item["net_qty"]),
                item["net_revenue"],
                item["cost"] if computed else "",
                item["platform_fee"] if computed else "",
                item["profit"] if computed else "",
                _export_cost_status_label(line, computed=computed),
            ]
        )

    return list(PROFIT_EXPORT_HEADERS), table, truncated
