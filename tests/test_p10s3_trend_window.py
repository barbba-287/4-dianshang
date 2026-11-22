# -*- coding: utf-8 -*-
"""P10-S3 利润趋势窗口选择器测试：S1 ~ S7（后端）+ F1 ~ F5（前端契约）。

覆盖两条主线：

- **后端放宽**：``PROFIT_TREND_MAX_POINTS`` 由 12 提到 31（按天粒度成为可能），
  新增**跨参数组合护栏** ``points × days ≤ 90``（``INVALID_TREND_SPAN``）。
  组合护栏是**活代码**——FastAPI 的 ``Query(ge/le)`` 拦不住跨参数约束，
  所以 S4 必须检查 ``body["detail"]`` 是 dict 且 ``["code"] == "INVALID_TREND_SPAN"``
  （只断言 422 会重蹈 O-6「INVALID_TREND_PARAMS 是死代码」的覆辙）。
- **前端解绑**：趋势的 ``days``/``points`` 不再绑死驾驶舱 ``#days``，
  改由新控件 ``#profit-trend-window`` 的预设组合决定；note 文案同步修正
  （解绑之后「与 KPI 卡同一口径」里的**窗口**不再相同，只剩**利润口径**相同）。

夹具直接复用 ``tests/test_p7s3_profit_engine.py`` 的 ``ctx``（含 sku_a/sku_b/sku_c、
``_order``/``_snapshot``/``_ingest``），不另造一套造数脚手架（沿用 P9-S1 / P10-S1
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

# D1 的五个预设组合（任务书 §2），顺序与模板里的 option 一致。
EXPECTED_WINDOW_VALUES = {"7,6", "1,7", "1,30", "7,12", "30,3"}


def _template() -> str:
    return COCKPIT_TEMPLATE.read_text(encoding="utf-8")


def _fn_body(html: str, name: str) -> str:
    """取顶层 async/sync 函数体（含 async 前缀），按大括号配对。

    锚点必须写 ``"function " + name + "("``（**带左括号**）——不带括号会先命中
    ``loadProfitDetail`` 之类的同名前缀函数，切到错误的函数体。
    """
    start = html.index("function " + name + "(")   # ★ 必须带左括号
    if html[start - 6:start] == "async ":
        start -= 6
    brace = html.index("{", start)
    depth = 0
    for i in range(brace, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return html[start:i + 1]
    raise AssertionError(f"{name} 函数体未闭合")


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
# S1 默认组合零回归（对拍证明没重写聚合）
# ---------------------------------------------------------------------------


def test_s1_default_window_is_zero_regression(ctx):
    """S1：days=7&points=6 → 200、6 期、days==7；最新期与直调 profit_totals 逐字相等。

    「对拍」是关键：如果实施方顺手重写了聚合逻辑，这个断言会红。
    """
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(ctx, _order_at(ctx, day_offset=0, gross="120.00"))

    response = _trend(ctx, days=7, points=6)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["points"]) == 6, f"默认组合应返回 6 期，实际 {len(body['points'])}"
    assert body["days"] == 7

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


# ---------------------------------------------------------------------------
# S2 按天粒度窗口正确
# ---------------------------------------------------------------------------


def test_s2_daily_granularity_windows_are_correct(ctx):
    """S2：days=1&points=30 → 30 期；每期 from == to；相邻期首尾相接；最新期 to == as_of。"""
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(ctx, _order_at(ctx, day_offset=0))

    response = _trend(ctx, days=1, points=30)
    assert response.status_code == 200, response.text
    body = response.json()
    points = body["points"]
    assert len(points) == 30, f"按天粒度应返回 30 期，实际 {len(points)}"
    assert body["days"] == 1

    for index, item in enumerate(points):
        assert item["from"] == item["to"], \
            f"第 {index} 期应恰好 1 天（from == to），实际 {item['from']} → {item['to']}"

    # 正序：最早在前、最新在后。
    to_dates = [date.fromisoformat(item["to"]) for item in points]
    assert to_dates == sorted(to_dates), f"points 未按时间正序：{to_dates}"
    assert points[-1]["to"] == AS_OF, f"最新一期的 to 应等于 as_of，实际 {points[-1]['to']}"

    # 相邻期首尾相接（按天粒度下就是「差 1 天」）。
    for index in range(1, len(points)):
        previous_to = date.fromisoformat(points[index - 1]["to"])
        current_from = date.fromisoformat(points[index]["from"])
        assert current_from == previous_to + timedelta(days=1), \
            f"第 {index} 期与上一期不首尾相接：{points[index - 1]['to']} → {points[index]['from']}"


# ---------------------------------------------------------------------------
# S3 新边界
# ---------------------------------------------------------------------------


def test_s3_points_upper_bound_moved_to_31(ctx):
    """S3：days=1&points=31 → 200 且 31 期；points=32 → 422；points=0 仍 422。"""
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(ctx, _order_at(ctx, day_offset=0))

    ok = _trend(ctx, days=1, points=31)
    assert ok.status_code == 200, ok.text
    assert len(ok.json()["points"]) == 31

    for points in (32, 0):
        bad = _trend(ctx, days=7, points=points)
        assert bad.status_code == 422, \
            f"points={points} 应返回 422，实际 {bad.status_code}：{bad.text[:200]}"


# ---------------------------------------------------------------------------
# S4 ★ 组合护栏是活代码（不重蹈 O-6）
# ---------------------------------------------------------------------------


def test_s4_span_guard_is_reachable_code_with_error_code(ctx):
    """S4：days=90&points=2（180 天）→ 422，且 detail 是 dict、code == INVALID_TREND_SPAN。

    ★ 硬判据：``body["detail"]`` **必须是 dict**。FastAPI 的 ``RequestValidationError``
    输出 ``{"detail": [...]}``（**list**），DomainError 输出 ``{"detail": {"code","message"}}``
    （**dict**）。所以若 detail 是 list ⇒ 请求在参数层就被拦了 ⇒ 组合护栏根本没执行，
    本用例必须红。这正是台账 O-6 的教训（「只断言 422」不算验证）。
    """
    response = _trend(ctx, days=90, points=2)
    assert response.status_code == 422, \
        f"days=90&points=2（180 天）应返回 422，实际 {response.status_code}：{response.text[:200]}"

    body = response.json()
    detail = body.get("detail")
    assert isinstance(detail, dict), \
        f"detail 应为 dict（DomainError 形态），实际 {type(detail).__name__}：{detail!r}"
    assert detail.get("code") == "INVALID_TREND_SPAN", \
        f"错误码应为 INVALID_TREND_SPAN，实际 {detail.get('code')!r}"
    assert "90" in detail.get("message", ""), \
        f"错误文案应说明 90 天上限，实际 {detail.get('message')!r}"


# ---------------------------------------------------------------------------
# S5 边界内侧
# ---------------------------------------------------------------------------


def test_s5_span_guard_boundary_inside_is_allowed(ctx):
    """S5：days=90&points=1（恰好 90 天）→ 200、1 期（护栏不能过紧）。"""
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(ctx, _order_at(ctx, day_offset=0))

    response = _trend(ctx, days=90, points=1)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["points"]) == 1, f"应返回 1 期，实际 {len(body['points'])}"
    assert body["days"] == 90


# ---------------------------------------------------------------------------
# S6 查询预算（放宽期数没有引入 N+1 膨胀）
# ---------------------------------------------------------------------------


def test_s6_points_drives_exactly_one_totals_query_each(ctx, monkeypatch):
    """S6：days=1&points=31 → ``profit_totals`` 被调用恰好 31 次，每次窗口恰好 1 天。

    spy **必须调用原函数**（不许替换成假数据），否则 S1/S2 的对拍会失真。
    """
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(ctx, _order_at(ctx, day_offset=0))

    original = ctx.finance.profit_totals
    calls: list[tuple] = []

    def _spy(db, **kwargs):
        calls.append((kwargs.get("window_from"), kwargs.get("window_to")))
        return original(db, **kwargs)

    monkeypatch.setattr(ctx.finance, "profit_totals", _spy)

    response = _trend(ctx, days=1, points=31)
    assert response.status_code == 200, response.text

    assert len(calls) == 31, \
        f"每期应恰好 1 条聚合查询，31 期应调 31 次，实际 {len(calls)} 次"
    for window_from, window_to in calls:
        assert window_from == window_to, \
            f"按天粒度窗口应 from == to，实际 {window_from} → {window_to}"

    # 31 个窗口首尾相接、不重不漏。
    spans = sorted((window_from, window_to) for window_from, window_to in calls)
    for index in range(1, len(spans)):
        assert spans[index][0] == spans[index - 1][1] + timedelta(days=1), \
            f"窗口不首尾相接：{spans[index - 1]} → {spans[index]}"
    assert len(set(spans)) == 31, "31 个窗口必须互不相同（不重）"


# ---------------------------------------------------------------------------
# S7 权限不回退
# ---------------------------------------------------------------------------


def test_s7_permission_predicate_unchanged(ctx):
    """S7：readonly / customer_service → 403；admin / operations → 200。

    ★ readonly 与 customer_service 的权限集里**都有** ``analytics.read``，
    全靠 ``roles`` 拦住——漏 roles 就是「页面挡住、接口漏出」的利润后门。
    本切片必须确认权限谓词没有被顺手放宽。
    """
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(ctx, _order_at(ctx, day_offset=0))

    # ctx 夹具自带 admin（admin 角色）与 viewer（readonly 角色）。
    assert _trend(ctx, login="admin").status_code == 200
    denied = _trend(ctx, login="viewer")
    assert denied.status_code == 403, \
        f"readonly 角色不应能读利润趋势，实际 {denied.status_code}"

    # customer_service / operations 两个角色 ctx 夹具没有，现场补。
    extra = _ensure_role_client(ctx, "customer_service", "p10s3-cs")
    assert _trend(ctx, login=extra).status_code == 403, \
        "customer_service 角色不应能读利润趋势（403）"

    ops = _ensure_role_client(ctx, "operations", "p10s3-ops")
    assert _trend(ctx, login=ops).status_code == 200, \
        "operations 角色应能读利润趋势（200）"


def _ensure_role_client(ctx, role: str, login: str) -> str:
    """在 ctx 的 workspace 里补一个指定角色的账号，并返回其在 ctx.clients 里的键。

    ctx 夹具只建了 admin / viewer 两个账号；本用例需要 customer_service 与
    operations 两个角色来验证权限谓词（``roles=(ROLE_ADMIN, ROLE_OPERATIONS)``）。
    """
    from app.employee_auth import hash_password

    password = "p10s3-role-pw"
    with ctx.db_mod.SessionLocal() as db:
        account = ctx.db_mod.UserAccount(
            login=login, password_hash=hash_password(password), display_name=login
        )
        db.add(account)
        db.flush()
        db.add(ctx.db_mod.WorkspaceMembership(
            workspace_id=ctx.workspace_id, user_id=account.id, role=role
        ))
        db.commit()

    from fastapi.testclient import TestClient

    client = TestClient(ctx.main_mod.app)
    response = client.post(
        "/login", data={"login": login, "password": password, "next": "/"}, follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    client.headers.update({"X-CSRF-Token": client.cookies.get("dianshang_csrf")})
    ctx.clients[login] = client
    return login


# ---------------------------------------------------------------------------
# 前端契约：F1 ~ F5
# ---------------------------------------------------------------------------


def test_f1_window_control_exists_with_exact_option_set():
    """F1：``#profit-trend-window`` 存在；option 的 value **集合精确等于**五个预设。

    用「切片 + 正则抽取 option value」，不用全文 ``in``——``value="7,6"`` 这种
    短字面全文搜可能命中别处。
    """
    import re

    tpl = _template()
    marker = 'id="profit-trend-window"'
    assert marker in tpl, "cockpit.html 缺少趋势窗口控件 #profit-trend-window"

    # 取出该 select 元素（从 select 开标签到对应 </select>）。
    select_at = tpl.index('<select id="profit-trend-window"')
    end_at = tpl.index("</select>", select_at)
    block = tpl[select_at:end_at]

    values = re.findall(r'<option value="([^"]*)"', block)
    assert set(values) == EXPECTED_WINDOW_VALUES, \
        f"option value 集合应为 {sorted(EXPECTED_WINDOW_VALUES)}，实际 {sorted(values)}"
    assert len(values) == 5, f"应有 5 个预设，实际 {len(values)}"

    # 默认项带 selected（进页面零变化）。
    assert '<option value="7,6" selected>' in block, \
        "默认项必须是 7,6 且带 selected（一进页面零变化）"


def test_f2_load_profit_trend_decoupled_from_cockpit_days():
    """F2：``loadProfitTrend`` 函数体内不再出现 ``$('#days')`` 与写死的 ``points=6``。"""
    tpl = _template()
    body = _fn_body(tpl, "loadProfitTrend")
    assert "$('#days')" not in body, \
        "loadProfitTrend 仍绑死驾驶舱 #days（趋势窗口必须独立可选）"
    assert "points=6" not in body, \
        "loadProfitTrend 仍写死 points=6（points 必须来自新控件）"
    assert "profit-trend-window" in body, "loadProfitTrend 未读取新控件 #profit-trend-window"
    assert "points=${encodeURIComponent" in body, "points 必须走 encodeURIComponent 拼参"


def test_f3_window_onchange_only_reloads_trend():
    """F3：``#profit-trend-window`` 的 onchange **只调** ``loadProfitTrend()``。

    ★ 精确切片：从 ``$('#profit-trend-window').onchange`` 起截到第一个 ``;``。
    绝不切整行——那一行同时有 ``$('#days').onchange=load``，会把 ``=load;`` 带进来造成假红；
    也绝不全文搜（``$("#days").onchange=load`` 恒在）。
    """
    tpl = _template()
    start = tpl.index("$('#profit-trend-window').onchange")
    stmt = tpl[start:tpl.index(";", start) + 1]
    assert "loadProfitTrend()" in stmt, "onchange 必须调用 loadProfitTrend()"
    assert "=load;" not in stmt, "onchange 不得绑定 load（会刷新整个驾驶舱）"
    assert "=load " not in stmt, "onchange 不得绑定 load（会刷新整个驾驶舱）"


def test_f4_trend_note_wording_fixed():
    """F4：note 文案里的「同一口径」已改为「同一利润口径 + 统计窗口独立」。

    旧句带结尾分号断言，是为了与新文案的「同一**利润**口径」区分开。
    """
    tpl = _template()
    body = _fn_body(tpl, "renderProfitTrend")
    assert "与上方 KPI 卡「利润环比」同一口径；" not in body, \
        "note 仍写「同一口径」——解绑之后窗口已不同，旧文案会误导（以为每期就是 KPI 卡的环比窗口）"
    assert "统计窗口独立" in body, "note 未说明统计窗口独立"
    assert "同一利润口径" in body, "note 未说明利润口径仍相同"


def test_f5_no_new_colors_and_no_new_chart_instances():
    """F5：``<style>`` 段 hex=0 / ``rgba(=0``；``<script>`` 段色值合计 8；
    ``echarts.init(`` = 3 且 ``'wb')`` = 3；``areaStyle`` 仍为 1。

    本片**不新增任何图表实例与色值**（新控件只用既有 ``.pager-select`` / ``.field``）。
    """
    import re

    tpl = _template()
    style = "".join(re.findall(r"<style>(.*?)</style>", tpl, re.S))
    script = "".join(re.findall(r"<script>(.*?)</script>", tpl, re.S))

    hex_re = re.compile(r"#[0-9a-fA-F]{3,8}\b")
    assert not hex_re.findall(style), f"<style> 残留 hex 色值：{hex_re.findall(style)[:5]}"
    assert "rgba(" not in style, "<style> 残留 rgba() 色值"

    script_colors = len(hex_re.findall(script)) + script.count("rgba(")
    assert script_colors == 8, f"<script> 内色值合计应为 8，实际 {script_colors}"
    assert script.count("echarts.init(") == 3, \
        f"echarts.init 数量应为 3，实际 {script.count('echarts.init(')}"
    assert script.count("echarts.init(") == script.count("'wb')"), \
        "存在未带 'wb' 主题的 echarts.init 调用"
    assert script.count("areaStyle") == 1, \
        f"areaStyle 应仍为 1，实际 {script.count('areaStyle')}"
