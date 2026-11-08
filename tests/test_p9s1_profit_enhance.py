"""P9-S1 利润中心补完测试：F-39 ~ F-51。

覆盖三条需求：

- **R1 利润环比**（F-39 ~ F-43）：上一等长窗口的利润环比，含负基数、多币种、
  零基数三条「不给百分比」的分支，以及驾驶舱查询预算守卫。
- **R2 明细全量导出**（F-44 ~ F-48、F-51）：突破页面上限、权限谓词、CSV BOM、
  空表导出、截断、以及「不可计算行金额列留空」。
- **R3 SKU 切片**（F-49）：按内部编码过滤明细与导出。

夹具直接复用 tests/test_p7s3_profit_engine.py 的 ctx（含 sku_a/sku_b/sku_c、
_snapshot/_ingest/_order 与 cockpit 客户端），不另造一套。
"""
from __future__ import annotations

import importlib
import io
import pathlib
import sys
from datetime import date

import pytest
from fastapi.testclient import TestClient

# 复用 P7-S3 的夹具模块：tests/ 不是包，同目录模块可直接 import。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
p7s3 = importlib.import_module("test_p7s3_profit_engine")

ctx = p7s3.ctx          # pytest 夹具（沿用 P7-S3 的构造）
_order = p7s3._order
_snapshot = p7s3._snapshot
_ingest = p7s3._ingest

AS_OF = "2026-09-10"
DAYS = 7
# 当期窗口 = [2026-09-04, 2026-09-10]，上期窗口 = [2026-08-28, 2026-09-03]
PREV_TIME = "2026-08-29T10:00:00Z"
CURR_TIME = "2026-09-08T10:00:00Z"


def _line(sku: str, *, qty: int = 1, gross: str = "100.00", line_id: str = "L-1") -> dict:
    return {"external_line_id": line_id, "external_sku": sku, "qty": qty, "gross": gross}


def _profit_kpi(ctx) -> dict:
    response = ctx.clients["admin"].get(f"/api/dashboard/cockpit?days={DAYS}&as_of={AS_OF}")
    assert response.status_code == 200, response.text
    return response.json()["kpis"]["estimated_profit"]


def _export(ctx, *, query: str = "", client=None):
    client = client or ctx.clients["admin"]
    return client.get(f"/api/finance/profit/lines/export?days={DAYS}&as_of={AS_OF}{query}")


def _xlsx_rows(payload: bytes) -> list[list]:
    import openpyxl

    workbook = openpyxl.load_workbook(io.BytesIO(payload))
    sheet = workbook.active
    return [[cell.value for cell in row] for row in sheet.iter_rows()]


def _add_user(ctx, login: str, password: str, role: str) -> TestClient:
    """在当前 workspace 里加一个成员并登录（复用 ctx 的账号表与登录流程）。"""
    from app.employee_auth import hash_password

    with ctx.db_mod.SessionLocal() as db:
        account = ctx.db_mod.UserAccount(
            login=login, password_hash=hash_password(password), display_name=login
        )
        db.add(account)
        db.flush()
        db.add(
            ctx.db_mod.WorkspaceMembership(
                workspace_id=ctx.workspace_id, user_id=account.id, role=role
            )
        )
        db.commit()

    client = TestClient(ctx.main_mod.app)
    response = client.post(
        "/login", data={"login": login, "password": password, "next": "/"}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    client.headers.update({"X-CSRF-Token": client.cookies.get("dianshang_csrf")})
    return client


# ---------------------------------------------------------------------------
# R1 利润环比
# ---------------------------------------------------------------------------


def test_f39_profit_change_ratio_uses_previous_equal_window(ctx):
    """上期 40.00、当期 50.00 → 环比 0.25。

    成本 55.00 的 SKU-B 落在上期窗口、成本 45.00 的 SKU-A 落在当期窗口；
    费率 5% → 上期 100-55-5=40.00，当期 100-45-5=50.00。
    """
    _snapshot(ctx, ctx.sku_b, "55.00")
    _snapshot(ctx, ctx.sku_a, "45.00")
    _ingest(
        ctx,
        _order(
            order_no="ORD-PREV",
            lines=[_line("EXT-SKU-B")],
            created=PREV_TIME, updated=PREV_TIME,
        ),
        _order(
            order_no="ORD-CURR",
            lines=[_line("EXT-SKU-A")],
            created=CURR_TIME, updated=CURR_TIME,
        ),
    )

    kpi = _profit_kpi(ctx)
    assert abs(kpi["change_ratio"] - 0.25) < 1e-9, kpi
    assert kpi["change_basis"] == "上一等长窗口"
    assert kpi["value"] == "50.00"


def test_f40_negative_previous_profit_has_no_ratio(ctx):
    """上期 −40.00、当期 +50.00（扭亏为盈）→ 不给百分比。

    若按 (cur − prev) / prev 算会得到 −2.25（读作「下降 225%」），
    若按 abs(prev) 算会得到 +2.25（读作「增长 225%」，语义又错一次）。
    正确做法是不给百分比，用 change_basis 说明原因。
    """
    _snapshot(ctx, ctx.sku_b, "135.00")
    _snapshot(ctx, ctx.sku_a, "45.00")
    _ingest(
        ctx,
        _order(order_no="ORD-PREV", lines=[_line("EXT-SKU-B")], created=PREV_TIME, updated=PREV_TIME),
        _order(order_no="ORD-CURR", lines=[_line("EXT-SKU-A")], created=CURR_TIME, updated=CURR_TIME),
    )

    kpi = _profit_kpi(ctx)
    assert kpi["value"] == "50.00"
    assert kpi["change_ratio"] is None
    assert "为负" in kpi["change_basis"], kpi
    assert kpi["change_ratio"] != -2.25


def test_f40b_loss_narrowing_has_no_ratio(ctx):
    """上期 −40.00、当期 −20.00（亏损收窄一半）→ 同样不给百分比。

    按 abs(prev) 会算成 +50%（读作「增长了 50%」，实际是亏损减半），语义错。
    """
    _snapshot(ctx, ctx.sku_b, "135.00")
    _snapshot(ctx, ctx.sku_c, "115.00")
    _ingest(
        ctx,
        _order(order_no="ORD-PREV", lines=[_line("EXT-SKU-B")], created=PREV_TIME, updated=PREV_TIME),
        _order(order_no="ORD-CURR", lines=[_line("EXT-SKU-C")], created=CURR_TIME, updated=CURR_TIME),
    )

    kpi = _profit_kpi(ctx)
    assert kpi["change_ratio"] is None
    assert "为负" in kpi["change_basis"], kpi


def test_f41_no_previous_orders_has_no_ratio(ctx):
    """上期无任何订单（利润 0）、当期有 → 无基数，不给百分比。"""
    _snapshot(ctx, ctx.sku_a, "45.00")
    _ingest(ctx, _order(order_no="ORD-CURR", lines=[_line("EXT-SKU-A")], created=CURR_TIME, updated=CURR_TIME))

    kpi = _profit_kpi(ctx)
    assert kpi["value"] == "50.00"
    assert kpi["change_ratio"] is None
    assert "无基数" in kpi["change_basis"], kpi


def test_f42_multi_currency_has_no_ratio(ctx):
    """多币种 → KPI 主值 null、环比也给不出单一数字。"""
    _snapshot(ctx, ctx.sku_a, "45.00")
    _ingest(
        ctx,
        _order(order_no="ORD-CNY", lines=[_line("EXT-SKU-A")], created=CURR_TIME, updated=CURR_TIME),
        _order(
            order_no="ORD-USD",
            lines=[_line("EXT-SKU-A", line_id="L-2")],
            created="2026-09-08T11:00:00Z", updated="2026-09-08T11:00:00Z",
            currency="USD",
        ),
    )

    response = ctx.clients["admin"].get(f"/api/dashboard/cockpit?days={DAYS}&as_of={AS_OF}")
    body = response.json()
    kpi = body["kpis"]["estimated_profit"]
    assert kpi["multi_currency"] is True
    assert kpi["value"] is None
    assert kpi["change_ratio"] is None
    assert "多币种" in kpi["change_basis"], kpi
    assert {item["currency"] for item in body["profit"]["by_currency"]} == {"CNY", "USD"}


def test_f43_cockpit_query_budget_is_nine(ctx):
    """驾驶舱 SELECT ≤ 9 条；profit_totals 自身必须只有 1 条。

    环比绝不能靠「再调一次 summarize_profit」实现（那是 2 条，会把驾驶舱顶到 10 条）。
    """
    from sqlalchemy import event

    import app.cockpit as cockpit_mod
    import app.finance as finance_mod

    _snapshot(ctx, ctx.sku_a, "45.00")
    _ingest(ctx, _order(order_no="ORD-CURR", lines=[_line("EXT-SKU-A")], created=CURR_TIME, updated=CURR_TIME))

    counted: list[str] = []
    with ctx.db_mod.SessionLocal() as db:
        engine = db.get_bind()

        def _before(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                counted.append(statement)

        event.listen(engine, "before_cursor_execute", _before)
        try:
            cockpit_mod.build_cockpit(
                db, workspace_id=ctx.workspace_id, days=DAYS, as_of=date(2026, 9, 10)
            )
        finally:
            event.remove(engine, "before_cursor_execute", _before)
    assert len(counted) <= 9, f"驾驶舱查询应 ≤ 9 条，实际 {len(counted)}"

    totals_counted: list[str] = []
    with ctx.db_mod.SessionLocal() as db:
        engine = db.get_bind()

        def _before(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                totals_counted.append(statement)

        event.listen(engine, "before_cursor_execute", _before)
        try:
            finance_mod.profit_totals(
                db, workspace_id=ctx.workspace_id,
                window_from=date(2026, 8, 28), window_to=date(2026, 9, 3),
            )
        finally:
            event.remove(engine, "before_cursor_execute", _before)
    assert len(totals_counted) == 1, f"profit_totals 应固定 1 条查询，实际 {len(totals_counted)}"


# ---------------------------------------------------------------------------
# R2 明细全量导出
# ---------------------------------------------------------------------------


def test_f44_export_exceeds_page_limit(ctx):
    """250 行可计算明细：导出全量 250 行，页面分页仍是 200 行。"""
    _snapshot(ctx, ctx.sku_a, "45.00")
    records = [
        _order(
            order_no=f"ORD-{index:04d}",
            version=index + 1,
            lines=[_line("EXT-SKU-A", line_id=f"L-{index}")],
            created="2026-09-08T10:00:00Z", updated="2026-09-08T10:05:00Z",
        )
        for index in range(250)
    ]
    _ingest(ctx, *records)

    response = _export(ctx)
    assert response.status_code == 200, response.text
    assert response.headers["X-Export-Row-Count"] == "250"
    assert "X-Export-Truncated" not in response.headers

    detail = ctx.clients["admin"].get(
        f"/api/finance/profit/lines?days={DAYS}&as_of={AS_OF}&limit=200"
    )
    body = detail.json()
    assert body["total"] == 250
    assert len(body["items"]) == 200


def test_f45_export_rejects_readonly_and_customer_service(ctx):
    """权限谓词必须与明细端点逐字一致：只写 permission 会漏成权限后门。"""
    readonly = _add_user(ctx, "export-viewer", "viewer-pw", "readonly")
    service = _add_user(ctx, "export-service", "service-pw", "customer_service")
    operations = _add_user(ctx, "export-ops", "ops-pw", "operations")

    assert _export(ctx, client=readonly).status_code == 403
    assert _export(ctx, client=service).status_code == 403
    assert _export(ctx, client=operations).status_code == 200


def test_f46_csv_export_has_utf8_bom(ctx):
    _snapshot(ctx, ctx.sku_a, "45.00")
    _ingest(ctx, _order(order_no="ORD-CURR", lines=[_line("EXT-SKU-A")], created=CURR_TIME, updated=CURR_TIME))

    response = _export(ctx, query="&format=csv")
    assert response.status_code == 200, response.text
    assert "text/csv" in response.headers["content-type"]
    assert response.content.startswith(b"\xef\xbb\xbf")


def test_f47_empty_window_export_still_has_headers(ctx):
    """空窗口导出是合法需求（「我确认这窗口没数据」）：200 + 只有表头。"""
    response = _export(ctx)
    assert response.status_code == 200, response.text
    assert response.headers["X-Export-Row-Count"] == "0"

    rows = _xlsx_rows(response.content)
    assert len(rows) == 1, rows
    assert len(rows[0]) == 10, rows[0]
    assert rows[0][0] == "订单号"
    assert rows[0][-1] == "成本状态"


def test_f48_export_truncates_at_max_rows(ctx, monkeypatch):
    """超过导出行数上限即截断，响应头给出真实行数。"""
    import app.finance as finance_mod

    _snapshot(ctx, ctx.sku_a, "45.00")
    records = [
        _order(
            order_no=f"ORD-{index:04d}",
            version=index + 1,
            lines=[_line("EXT-SKU-A", line_id=f"L-{index}")],
            created="2026-09-08T10:00:00Z", updated="2026-09-08T10:05:00Z",
        )
        for index in range(8)
    ]
    _ingest(ctx, *records)

    monkeypatch.setattr(finance_mod, "PROFIT_EXPORT_MAX_ROWS", 5)
    response = _export(ctx)
    assert response.status_code == 200, response.text
    assert response.headers["X-Export-Truncated"] == "1"
    assert response.headers["X-Export-Row-Count"] == "5"


def test_f51_export_leaves_amounts_blank_for_uncomputable_lines(ctx):
    """不可计算行的成本/平台费/利润必须留空，绝不能填 0。

    xlsx 里空串会落成空单元格（openpyxl 读回是 None），所以「必须是空串」这条用
    CSV 通道严格断言（csv.reader 保留空串），xlsx 只断言「不是 0」。
    """
    import csv as csv_mod

    # 没有成本快照 → cost_status = missing
    _ingest(ctx, _order(order_no="ORD-NOCOST", lines=[_line("EXT-SKU-A")], created=CURR_TIME, updated=CURR_TIME))

    response = _export(ctx)
    assert response.status_code == 200, response.text
    rows = _xlsx_rows(response.content)
    assert len(rows) == 2, rows
    data = rows[1]
    # 列序：订单号0 下单时间1 SKU2 币种3 净数量4 实付净额5 成本6 平台费7 利润8 成本状态9
    assert data[6] in ("", None) and data[7] in ("", None) and data[8] in ("", None)
    assert data[6] != "0" and data[6] != "0.00"
    assert data[7] != "0" and data[7] != "0.00"
    assert data[8] != "0" and data[8] != "0.00"
    assert data[9] == "缺成本"

    csv_response = _export(ctx, query="&format=csv")
    assert csv_response.status_code == 200, csv_response.text
    csv_rows = list(csv_mod.reader(io.StringIO(csv_response.content.decode("utf-8-sig"))))
    assert len(csv_rows) == 2, csv_rows
    csv_data = csv_rows[1]
    assert csv_data[6] == "" and csv_data[7] == "" and csv_data[8] == "", csv_data
    assert csv_data[6] != "0" and csv_data[6] != "0.00"
    assert csv_data[9] == "缺成本", csv_data


# ---------------------------------------------------------------------------
# R3 SKU 切片
# ---------------------------------------------------------------------------


def test_f49_sku_code_filter_applies_to_lines_and_export(ctx):
    _snapshot(ctx, ctx.sku_a, "45.00")
    _snapshot(ctx, ctx.sku_b, "55.00")
    _ingest(
        ctx,
        _order(order_no="ORD-A", lines=[_line("EXT-SKU-A")], created=CURR_TIME, updated=CURR_TIME),
        _order(
            order_no="ORD-B",
            lines=[_line("EXT-SKU-B", line_id="L-2")],
            created="2026-09-08T11:00:00Z", updated="2026-09-08T11:00:00Z",
        ),
    )

    detail = ctx.clients["admin"].get(
        f"/api/finance/profit/lines?days={DAYS}&as_of={AS_OF}&sku_code=SKU-A"
    )
    body = detail.json()
    assert body["total"] == 1
    assert [item["sku_code"] for item in body["items"]] == ["SKU-A"]

    # 找不到的 SKU 返回空结果，而不是 422
    missing = ctx.clients["admin"].get(
        f"/api/finance/profit/lines?days={DAYS}&as_of={AS_OF}&sku_code=SKU-NOPE"
    )
    assert missing.status_code == 200
    assert missing.json()["total"] == 0

    response = _export(ctx, query="&sku_code=SKU-A")
    assert response.status_code == 200, response.text
    rows = _xlsx_rows(response.content)
    assert len(rows) == 2, rows
    assert rows[1][2] == "SKU-A"


# ---------------------------------------------------------------------------
# F-50 环比窗口边界
# ---------------------------------------------------------------------------


def test_f50_previous_window_is_adjacent_and_non_overlapping(ctx):
    """当期 [as_of-6, as_of]、上期 [as_of-13, as_of-7]：不重叠、无缝隙。

    边界订单分别落在 as_of-7（属上期）与 as_of-6（属当期），
    再造一单落在窗口之外，验证它不计入任何一个窗口。
    """
    _snapshot(ctx, ctx.sku_a, "45.00")
    _ingest(
        ctx,
        # 上期最后一刻：100 - 45 - 5 = 50.00
        _order(
            order_no="ORD-EDGE-PREV",
            lines=[_line("EXT-SKU-A")],
            created="2026-09-03T23:00:00Z", updated="2026-09-03T23:00:00Z",
        ),
        # 当期第一刻：200 - 45 - 10 = 145.00
        _order(
            order_no="ORD-EDGE-CURR",
            lines=[_line("EXT-SKU-A", line_id="L-2", gross="200.00")],
            created="2026-09-04T00:30:00Z", updated="2026-09-04T00:30:00Z",
        ),
        # 当期最后一刻：300 - 45 - 15 = 240.00
        _order(
            order_no="ORD-EDGE-LAST",
            lines=[_line("EXT-SKU-A", line_id="L-3", gross="300.00")],
            created="2026-09-10T23:00:00Z", updated="2026-09-10T23:00:00Z",
        ),
        # 窗口之外（更早）：任何窗口都不该收它
        _order(
            order_no="ORD-OUTSIDE",
            lines=[_line("EXT-SKU-A", line_id="L-4", gross="400.00")],
            created="2026-08-27T10:00:00Z", updated="2026-08-27T10:00:00Z",
        ),
    )

    response = ctx.clients["admin"].get(f"/api/dashboard/cockpit?days={DAYS}&as_of={AS_OF}")
    body = response.json()
    assert body["window"]["from"] == "2026-09-04"
    assert body["window"]["to"] == "2026-09-10"
    assert body["window"]["prev_from"] == "2026-08-28"
    assert body["window"]["prev_to"] == "2026-09-03"

    kpi = body["kpis"]["estimated_profit"]
    assert kpi["value"] == "385.00", kpi          # 145 + 240，窗口外的 400 不在内
    assert abs(kpi["change_ratio"] - 6.7) < 1e-9, kpi   # (385 - 50) / 50


# ---------------------------------------------------------------------------
# F-52 导出表名（P9-S2 · R-72）
# ---------------------------------------------------------------------------


def test_f52_profit_export_sheet_title(ctx):
    """利润导出的 xlsx 工作表名必须是「利润明细」。

    P9-S1 的偏差：利润导出走 build_download 的 else 分支，Sheet 名落成「批次结果」。
    P9-S2 给 build_download 加可选 sheet_title，利润导出显式传「利润明细」。
    """
    import openpyxl

    _snapshot(ctx, ctx.sku_a, "45.00")
    _ingest(ctx, _order(order_no="ORD-CURR", lines=[_line("EXT-SKU-A")], created=CURR_TIME, updated=CURR_TIME))

    response = _export(ctx)  # 默认 format=xlsx
    assert response.status_code == 200, response.text
    workbook = openpyxl.load_workbook(io.BytesIO(response.content))
    assert workbook.sheetnames == ["利润明细"], workbook.sheetnames
