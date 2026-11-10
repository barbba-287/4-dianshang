# -*- coding: utf-8 -*-
"""P10-S2 利润趋势折线 + 导出跟随 SKU 筛选测试：T1 ~ T8。

覆盖两条主线：

- **M1（导出跟随 SKU 筛选）**：前端 `exportProfitLines` 必须真的把 `sku_code` 传给
  后端（后端能力本就完整，缺的是前端传参）。T8 用**函数体切片**断言，不是全文搜
  ——模板里 `loadProfitDetail` 早就含 `URLSearchParams`，全文搜恒为真、抓不到任何东西。
- **M2（利润趋势折线）**：端点结构、期边界、口径与环比同源、缺失期为 null、
  多币种不折算、权限、参数钳制。

夹具直接复用 `tests/test_p7s3_profit_engine.py` 的 `ctx`（含 sku_a/sku_b/sku_c、
`_order`/`_snapshot`/`_ingest`），不另造一套造数脚手架（沿用 P9-S1 / P10-S1
已确立的跨文件复用范式）。
"""
from __future__ import annotations

import importlib
import pathlib
import sys
from datetime import date, timedelta

# 复用 P7-S3 的夹具模块：tests/ 不是包，同目录模块可直接 import。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
p7s3 = importlib.import_module("test_p7s3_profit_engine")

ctx = p7s3.ctx          # pytest 夹具（沿用 P7-S3 的构造）
_order = p7s3._order
_snapshot = p7s3._snapshot
_ingest = p7s3._ingest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
COCKPIT_TEMPLATE = REPO_ROOT / "app" / "templates" / "cockpit.html"

AS_OF = "2026-09-10"
AS_OF_DATE = date(2026, 9, 10)


def _template() -> str:
    return COCKPIT_TEMPLATE.read_text(encoding="utf-8")


def _trend(ctx, *, days: int = 7, points: int = 6, as_of: str = AS_OF, login: str = "admin"):
    return ctx.clients[login].get(
        f"/api/finance/profit/trend?days={days}&points={points}&as_of={as_of}"
    )


def _order_at(ctx, *, day_offset: int, currency: str = "CNY", gross: str = "50.00",
              order_no: str | None = None):
    """造一笔落在「距 as_of 往前 day_offset 天」那天的订单（UTC 日界）。"""
    created = (AS_OF_DATE - timedelta(days=day_offset)).isoformat() + "T10:00:00Z"
    return _order(
        order_no=order_no or f"ORD-{day_offset}-{currency}",
        lines=[{
            "external_line_id": f"L-{day_offset}-{currency}",
            "external_sku": "EXT-SKU-A",
            "qty": 1,
            "gross": gross,
        }],
        created=created,
        updated=created,
        currency=currency,
    )


# ---------------------------------------------------------------------------
# T1 端点基本结构
# ---------------------------------------------------------------------------


def test_t1_trend_endpoint_returns_ordered_points(ctx):
    """T1：200；points 长度 == 请求的 points；按时间正序；每项含 from/to/label/profit。"""
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(ctx, _order_at(ctx, day_offset=0))

    response = _trend(ctx, points=6)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body.get("points"), list)
    assert len(body["points"]) == 6, f"points 长度应为 6，实际 {len(body['points'])}"
    for item in body["points"]:
        for key in ("from", "to", "label", "profit"):
            assert key in item, f"points 元素缺少字段：{key}"

    # 时间正序：最早在前、最新在后。
    to_dates = [date.fromisoformat(item["to"]) for item in body["points"]]
    assert to_dates == sorted(to_dates), f"points 未按时间正序：{to_dates}"
    assert body["as_of"] == AS_OF
    assert body["days"] == 7
    assert body["multi_currency"] is False
    assert body["currency"] == "CNY"


# ---------------------------------------------------------------------------
# T2 期边界正确
# ---------------------------------------------------------------------------


def test_t2_periods_are_contiguous_and_latest_ends_at_as_of(ctx):
    """T2：相邻两期首尾相接（to 与 from 差 1 天、不重叠）；最新一期 to == as_of。"""
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(ctx, _order_at(ctx, day_offset=0))

    body = _trend(ctx, days=7, points=4).json()
    points = body["points"]
    assert points[-1]["to"] == AS_OF, f"最新一期的 to 应等于 as_of，实际 {points[-1]['to']}"

    for index in range(1, len(points)):
        previous_to = date.fromisoformat(points[index - 1]["to"])
        current_from = date.fromisoformat(points[index]["from"])
        assert current_from == previous_to + timedelta(days=1), \
            f"第 {index} 期与上一期不首尾相接：{points[index - 1]['to']} → {points[index]['from']}"

    # 每期都恰好 days 天。
    for item in points:
        span = (date.fromisoformat(item["to"]) - date.fromisoformat(item["from"])).days + 1
        assert span == 7, f"窗口应为 7 天，实际 {span}"


# ---------------------------------------------------------------------------
# T3 口径与环比同源（直接对拍，不手算）
# ---------------------------------------------------------------------------


def test_t3_trend_value_equals_profit_totals_for_same_window(ctx):
    """T3：某期趋势值 == `profit_totals` 对同一窗口的返回值（直接对拍，不手算）。"""
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(ctx, _order_at(ctx, day_offset=0, gross="120.00"))

    body = _trend(ctx, days=7, points=3).json()
    latest = body["points"][-1]

    with ctx.db_mod.SessionLocal() as db:
        direct = ctx.finance.profit_totals(
            db,
            workspace_id=ctx.workspace_id,
            window_from=date.fromisoformat(latest["from"]),
            window_to=date.fromisoformat(latest["to"]),
        )
    assert direct, "同一窗口的 profit_totals 不应为空"
    assert latest["profit"] == f"{direct['CNY']:.2f}", \
        f"趋势值与 profit_totals 不一致：趋势 {latest['profit']} vs 直调 {direct['CNY']:.2f}"
    assert latest["has_data"] is True


# ---------------------------------------------------------------------------
# T4 缺失期为 null 不是 0
# ---------------------------------------------------------------------------


def test_t4_missing_periods_are_null_not_zero(ctx):
    """T4：只在最新期造数据 → 其余期 profit is None、has_data is False，不许出现 "0.00"。"""
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(ctx, _order_at(ctx, day_offset=0, gross="80.00"))

    body = _trend(ctx, days=7, points=5).json()
    points = body["points"]
    assert points[-1]["profit"] is not None, "最新一期应有利润"
    assert points[-1]["has_data"] is True

    for item in points[:-1]:
        assert item["profit"] is None, \
            f"无数据期应为 null，实际 {item['profit']!r}（填 0 会把「没有行」画成「亏光了」）"
        assert item["has_data"] is False
        assert item["profit"] != "0.00"
    assert all(item["profit"] != "0.00" for item in points)


# ---------------------------------------------------------------------------
# T5 多币种不折算
# ---------------------------------------------------------------------------


def test_t5_multi_currency_is_not_converted(ctx):
    """T5：两个币种各有数据 → multi_currency is True、points == []、reason 非空。"""
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(ctx, _order_at(ctx, day_offset=0, currency="CNY"))
    _ingest(ctx, _order_at(ctx, day_offset=0, currency="USD", order_no="ORD-USD"))

    body = _trend(ctx, days=7, points=3).json()
    assert body["multi_currency"] is True, \
        "两个币种并存时必须声明多币种（折算成一根线 = 画一个不存在的数）"
    assert body["points"] == [], f"多币种时不应返回点，实际 {body['points']}"
    assert body["reason"], "多币种必须给出原因说明"
    assert body["currency"] is None


# ---------------------------------------------------------------------------
# T6 权限与明细端点一致（★本片最重要的一条）
# ---------------------------------------------------------------------------


def test_t6_trend_requires_same_roles_as_profit_lines(ctx):
    """T6：只读角色调用趋势端点 → 403（与 /api/finance/profit/lines 同一判据）。"""
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(ctx, _order_at(ctx, day_offset=0))

    assert _trend(ctx, login="admin").status_code == 200
    denied = _trend(ctx, login="viewer")
    assert denied.status_code == 403, \
        f"readonly 角色不应能读利润趋势，实际 {denied.status_code}"
    # 明细端点同判据（防止两条端点的权限谓词漂移）。
    detail = ctx.clients["viewer"].get(
        f"/api/finance/profit/lines?days=7&as_of={AS_OF}"
    )
    assert detail.status_code == 403


# ---------------------------------------------------------------------------
# T7 参数钳制
# ---------------------------------------------------------------------------


def test_t7_invalid_trend_params_return_422(ctx):
    """T7：points=0/13、days=0/91 → 422，不得 500。"""
    for params in (
        "days=7&points=0&as_of=2026-09-10",
        "days=7&points=13&as_of=2026-09-10",
        "days=0&points=6&as_of=2026-09-10",
        "days=91&points=6&as_of=2026-09-10",
    ):
        response = ctx.clients["admin"].get(f"/api/finance/profit/trend?{params}")
        assert response.status_code == 422, \
            f"{params} 应返回 422，实际 {response.status_code}：{response.text[:200]}"


# ---------------------------------------------------------------------------
# T8 前端模板契约（含 M1 的导出跟随 SKU 筛选）
# ---------------------------------------------------------------------------


def test_t8_template_contract_for_trend_panel_and_sku_export(ctx):
    """T8：趋势面板锚点齐备；`exportProfitLines` **函数体**内含 sku_code 与 URLSearchParams。

    关键：按函数体切片断言，不做全文 `in`——`loadProfitDetail`（P10-S1）早就含
    `URLSearchParams`，全文搜恒为真、抓不到任何东西。切片后基线（未做 M1）应先是
    False，做完 M1 才变 True；若一开始就是绿，说明切片切错了。
    """
    template = _template()

    for anchor in (
        'id="profit-trend-chart"',
        'id="cockpit-profit-trend-panel"',
        'id="cockpit-profit-trend-meta"',
        "loadProfitTrend",
        "renderProfitTrend",
    ):
        assert anchor in template, f"cockpit.html 缺少趋势面板锚点：{anchor}"

    export_fn = template.split("async function exportProfitLines")[1].split("async function load()")[0]
    assert "sku_code" in export_fn, \
        "exportProfitLines 函数体内未传 sku_code（导出结果与屏幕筛选不一致）"
    assert "URLSearchParams" in export_fn, \
        "exportProfitLines 必须用 URLSearchParams 拼参（SKU 含 & / / / 中文时会被截断）"
