"""P7-S3 利润引擎测试：F-17..F-25 + R-65。

编号口径（dev_task.p7-profit-center.md §15.5）：

- F-17 手算对拍（★验收闸）：净数量/实付净额/成本/平台费/利润 逐项等于手算值
- F-18 费率锚定（★验收闸）：改全局费率后历史订单利润不变，新订单用新费率
- F-19 黄条口径：missing 按**订单**去重，unmapped 不计入 X
- F-20 排行：winners 仅正利润降序、losers 仅负利润升序、空态空数组
- F-21 多币种：KPI 主值 null + multi_currency，分组返回不折算
- F-22 权限：analytics.read + admin/operations 可读；只读角色 403
- F-23 空态安全：零成本快照时 cockpit 不 500，KPI quality=insufficient
- F-24 只读纪律 + 查询预算：finance.py 无写操作；summarize_profit ≤ 2 条查询
- F-25 下钻明细：status 过滤、currency 过滤、逐行之和 == KPI 总额、非法参数 422
- R-65 批内重复订单号：入口 422 + 明确错误码，且一行都不写
"""
from __future__ import annotations

import importlib
import json
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

SALE_TIME = "2026-09-10T10:00:00Z"
AS_OF = "2026-09-10"


class Ctx:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'p7s3.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("PROFIT_PLATFORM_FEE_RATE", "0.05")
    monkeypatch.setenv("PROFIT_PLATFORM_FEE_OVERRIDES", "")

    import app.config as cfg
    import app.db as db_mod
    import app.external_orders as ext_mod
    import app.main as main_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(ext_mod)
    importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)

    from app.employee_auth import hash_password

    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="p7s3", name="P7-S3 利润测试")
        db.add(workspace)
        db.flush()
        users = {}
        for login, password, role in (
            ("admin", "admin-pw", "admin"),
            ("viewer", "viewer-pw", "readonly"),
        ):
            account = db_mod.UserAccount(
                login=login, password_hash=hash_password(password), display_name=login
            )
            db.add(account)
            db.flush()
            db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=account.id, role=role))
            users[login] = (password, role)
        product = db_mod.Product(
            workspace_id=workspace.id, source="fixture", external_product_id="p7s3-p",
            title="P7-S3 商品", url="https://fixture.local/p7s3-p", current_price=Decimal("40.00"),
        )
        db.add(product)
        db.flush()
        sku_a = db_mod.ProductSku(workspace_id=workspace.id, product_id=product.id, sku_code="SKU-A")
        sku_b = db_mod.ProductSku(workspace_id=workspace.id, product_id=product.id, sku_code="SKU-B")
        sku_c = db_mod.ProductSku(workspace_id=workspace.id, product_id=product.id, sku_code="SKU-C")
        db.add_all([sku_a, sku_b, sku_c])
        db.flush()
        account = ext_mod.ensure_external_account(
            db, workspace_id=workspace.id, platform="jd", account_ref="account-a", store_ref="store-a",
        )
        db.add_all([
            db_mod.ExternalProductMapping(
                workspace_id=workspace.id, external_account_id=account.id,
                external_sku="EXT-SKU-A", internal_sku_id=sku_a.id, mapping_status="mapped",
            ),
            db_mod.ExternalProductMapping(
                workspace_id=workspace.id, external_account_id=account.id,
                external_sku="EXT-SKU-B", internal_sku_id=sku_b.id, mapping_status="mapped",
            ),
            db_mod.ExternalProductMapping(
                workspace_id=workspace.id, external_account_id=account.id,
                external_sku="EXT-SKU-C", internal_sku_id=sku_c.id, mapping_status="mapped",
            ),
            # EXT-SKU-UNMAPPED 刻意不给映射 → unmapped 分支
        ])
        db.commit()
        ids = {
            "workspace_id": workspace.id, "account_id": account.id,
            "sku_a": sku_a.id, "sku_b": sku_b.id, "sku_c": sku_c.id,
        }

    clients: dict[str, TestClient] = {}
    for login, (password, _role) in users.items():
        client = TestClient(main_mod.app)
        response = client.post(
            "/login", data={"login": login, "password": password, "next": "/"}, follow_redirects=False,
        )
        assert response.status_code == 303
        client.headers.update({"X-CSRF-Token": client.cookies.get("dianshang_csrf")})
        clients[login] = client

    import app.finance as finance_mod

    importlib.reload(finance_mod)

    yield Ctx(
        db_mod=db_mod, main_mod=main_mod, ext_mod=ext_mod, cfg=cfg, finance=finance_mod,
        clients=clients, **ids,
    )
    cfg.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _order(
    *,
    order_no: str = "ORD-1",
    lines: list[dict] | None = None,
    created: str = SALE_TIME,
    updated: str = "2026-09-10T10:01:00Z",
    version: int | None = 1,
    status: str = "paid",
    currency: str = "CNY",
):
    if lines is None:
        lines = [{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 2, "gross": "100.00"}]
    row = {
        "account_ref": "account-a", "store_ref": "store-a",
        "external_order_no": order_no, "status": status, "currency": currency,
        "created_at": created, "updated_at": updated,
        "lines": [
            {
                "external_line_id": item["external_line_id"],
                "external_sku": item["external_sku"],
                "ordered_qty": item.get("qty", 1),
                "cancelled_qty": item.get("cancelled", 0),
                "refunded_qty": item.get("refunded", 0),
                "gross_amount": item.get("gross", "50.00"),
                "refund_amount": item.get("refund", "0.00"),
            }
            for item in lines
        ],
    }
    if version is not None:
        row["event_version"] = version
    from app.connectors import load_orders

    return load_orders(json.dumps({"records": [row]}), platform="jd")[0]


def _snapshot(ctx, sku_id: int, cost: str, when: str = "2026-08-01T00:00:00", ref: str = "s") -> None:
    with ctx.db_mod.SessionLocal() as db:
        db.add(
            ctx.db_mod.CostSnapshot(
                workspace_id=ctx.workspace_id, sku_id=sku_id,
                unit_cost=Decimal(cost), effective_time=datetime.fromisoformat(when),
                source="manual", source_ref=ref, currency="CNY",
            )
        )
        db.commit()


def _ingest(ctx, *records):
    with ctx.db_mod.SessionLocal() as db:
        return ctx.ext_mod.ingest_orders(db, list(records), workspace_id=ctx.workspace_id)


def _cockpit(ctx, *, days: int = 7, as_of: str = AS_OF, login: str = "admin"):
    return ctx.clients[login].get(f"/api/dashboard/cockpit?days={days}&as_of={as_of}")


def _profit_lines(ctx, **params):
    login = params.pop("login", "admin")
    query = f"days={params.pop('days', 7)}&as_of={params.pop('as_of', AS_OF)}"
    for key, value in params.items():
        query += f"&{key}={value}"
    return ctx.clients[login].get(f"/api/finance/profit/lines?{query}")


def _profit_of(ctx, *, as_of: str = AS_OF) -> dict:
    response = _cockpit(ctx, as_of=as_of)
    assert response.status_code == 200, response.text
    return response.json()["profit"]


# ---------------------------------------------------------------------------
# F-17 手算对拍（★验收闸）
# ---------------------------------------------------------------------------


def test_f17_profit_formula_matches_hand_calculation(ctx):
    """两笔订单逐项对拍，含退款与取消。

    成本 10.00 / 费率 5%：
      订单1：qty 2，实付 100（无退款）→ 净数量 2、成本 20.00、平台费 5.00、利润 75.00
      订单2：qty 3，取消 1、退款 1，金额 90 退 30 → 净数量 1、实付 60.00、
             成本 10.00、平台费 3.00、利润 47.00
      合计：实付 160.00、成本 30.00、平台费 8.00、利润 122.00
    """
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(
        ctx,
        _order(order_no="ORD-1", lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 2, "gross": "100.00"}]),
        _order(
            order_no="ORD-2",
            updated="2026-09-10T11:00:00Z",
            lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 3, "cancelled": 1, "refunded": 1, "gross": "90.00", "refund": "30.00"}],
        ),
    )

    profit = _profit_of(ctx)
    cell = profit["by_currency"][0]
    assert profit["quality"] == "complete"
    assert profit["total_profit"] == "122.00"
    assert cell["currency"] == "CNY"
    assert cell["revenue"] == "160.00"       # 100 + 60
    assert cell["cost"] == "30.00"           # 10×2 + 10×1
    assert cell["platform_fee"] == "8.00"    # 5% × 160
    assert cell["profit"] == "122.00"
    assert cell["net_qty"] == 3
    assert cell["computed_lines"] == 2

    # 明细逐行对拍：两行利润之和必须等于 KPI 总额（舍入口径一致）
    lines = _profit_lines(ctx, status="computed").json()
    assert lines["total"] == 2
    assert lines["page_profit"] == "122.00"
    by_order = {item["external_order_no"]: item for item in lines["items"]}
    assert by_order["ORD-1"]["net_qty"] == 2
    assert by_order["ORD-1"]["profit"] == "75.00"
    assert by_order["ORD-2"]["net_qty"] == 1
    assert by_order["ORD-2"]["net_revenue"] == "60.00"
    assert by_order["ORD-2"]["cost"] == "10.00"
    assert by_order["ORD-2"]["platform_fee"] == "3.00"
    assert by_order["ORD-2"]["profit"] == "47.00"


def test_f17b_refund_beyond_revenue_clamps_to_zero(ctx):
    """退款额 > 商品额（脏数据）时实付净额夹到 0，不产生负收入。"""
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(ctx, _order(lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 1, "gross": "50.00", "refund": "80.00"}]))

    profit = _profit_of(ctx)
    cell = profit["by_currency"][0]
    assert cell["revenue"] == "0.00"
    assert cell["cost"] == "10.00"
    assert cell["platform_fee"] == "0.00"
    assert cell["profit"] == "-10.00"


# ---------------------------------------------------------------------------
# F-18 费率锚定（★验收闸）
# ---------------------------------------------------------------------------


def test_f18_fee_rate_change_does_not_touch_history(ctx, monkeypatch):
    """改全局费率 5% → 8%：已锚定订单利润分毫不变，新订单按 8% 扣。"""
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(ctx, _order(order_no="ORD-OLD", lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 2, "gross": "100.00"}]))

    before = _profit_of(ctx)
    assert before["total_profit"] == "75.00"   # 100 − 20 − 5

    monkeypatch.setenv("PROFIT_PLATFORM_FEE_RATE", "0.08")
    ctx.cfg.get_settings.cache_clear()

    after_rate_change = _profit_of(ctx)
    assert after_rate_change["total_profit"] == "75.00", "改费率改写了历史利润"
    assert after_rate_change["by_currency"][0]["platform_fee"] == "5.00"

    _ingest(
        ctx,
        _order(order_no="ORD-NEW", created="2026-09-10T12:00:00Z", updated="2026-09-10T12:01:00Z",
               lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 2, "gross": "100.00"}]),
    )
    both = _profit_of(ctx)
    # 旧单 75.00 + 新单 100 − 20 − 8 = 72.00 → 147.00
    assert both["by_currency"][0]["platform_fee"] == "13.00"   # 5 + 8
    assert both["total_profit"] == "147.00"


# ---------------------------------------------------------------------------
# F-19 黄条口径
# ---------------------------------------------------------------------------


def test_f19_missing_cost_counted_by_order_and_unmapped_excluded(ctx):
    """一笔订单两个 SKU 都缺成本只算 1 笔；未映射行不进 X。"""
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(
        ctx,
        # 有成本：正常计入
        _order(order_no="ORD-OK", lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 1, "gross": "50.00"}]),
        # 缺成本：B、C 两个 SKU 都没有快照 → 同一订单，只算 1 笔
        _order(
            order_no="ORD-MISSING",
            updated="2026-09-10T13:00:00Z",
            lines=[
                {"external_line_id": "L-1", "external_sku": "EXT-SKU-B", "qty": 1, "gross": "30.00"},
                {"external_line_id": "L-2", "external_sku": "EXT-SKU-C", "qty": 1, "gross": "20.00"},
            ],
        ),
        # 未映射：既不算利润也不进黄条
        _order(
            order_no="ORD-UNMAPPED",
            updated="2026-09-10T14:00:00Z",
            lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-UNMAPPED", "qty": 1, "gross": "10.00"}],
        ),
    )

    profit = _profit_of(ctx)
    assert profit["missing_cost_order_count"] == 1
    assert profit["missing_cost_line_count"] == 2
    assert profit["unmapped_line_count"] == 1
    assert profit["warning"]["show"] is True
    assert profit["warning"]["message"] == "有 1 笔订单因缺失入库成本未能计算利润"
    assert profit["quality"] == "partial"
    assert profit["reason"] == "MISSING_COST_SNAPSHOTS"
    # 缺成本行的收入不进利润汇总（未知不能当 0）
    assert profit["by_currency"][0]["revenue"] == "50.00"
    assert profit["total_profit"] == "37.50"   # 50 − 10 − 2.50


def test_f19b_no_missing_cost_keeps_warning_hidden(ctx):
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(ctx, _order(lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 1, "gross": "50.00"}]))
    profit = _profit_of(ctx)
    assert profit["warning"]["show"] is False
    assert profit["warning"]["message"] is None
    assert profit["quality"] == "complete"


# ---------------------------------------------------------------------------
# F-20 排行
# ---------------------------------------------------------------------------


def test_f20_ranking_only_positive_winners_and_negative_losers(ctx):
    """A 赚、B 亏、C 打平：C 两边都不进（利润 0 既不是赚也不是亏）。"""
    _snapshot(ctx, ctx.sku_a, "10.00")
    _snapshot(ctx, ctx.sku_b, "80.00")
    _snapshot(ctx, ctx.sku_c, "50.00")
    _ingest(
        ctx,
        _order(order_no="ORD-A", lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 2, "gross": "100.00"}]),
        _order(order_no="ORD-B", updated="2026-09-10T11:00:00Z",
               lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-B", "qty": 1, "gross": "50.00"}]),
        _order(order_no="ORD-C", updated="2026-09-10T12:00:00Z",
               lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-C", "qty": 1, "gross": "52.63"}]),
    )

    ranking = _profit_of(ctx)["ranking"]
    winners = {item["sku_code"]: item for item in ranking["winners"]}
    losers = {item["sku_code"]: item for item in ranking["losers"]}
    assert set(winners) == {"SKU-A"}
    assert winners["SKU-A"]["profit"] == "75.00"
    assert set(losers) == {"SKU-B"}
    assert losers["SKU-B"]["profit"] == "-32.50"    # 50 − 80 − 2.50
    assert ranking["limit"] == 10


def test_f20b_ranking_empty_state_returns_empty_arrays(ctx):
    _ingest(ctx, _order(lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 1, "gross": "50.00"}]))
    ranking = _profit_of(ctx)["ranking"]
    assert ranking["winners"] == []
    assert ranking["losers"] == []


# ---------------------------------------------------------------------------
# F-21 多币种
# ---------------------------------------------------------------------------


def test_f21_multi_currency_never_converts_or_sums(ctx):
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(
        ctx,
        _order(order_no="ORD-CNY", lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 1, "gross": "100.00"}], currency="CNY"),
        _order(order_no="ORD-USD", updated="2026-09-10T15:00:00Z", currency="USD",
               lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 1, "gross": "100.00"}]),
    )
    body = _cockpit(ctx).json()
    kpi = body["kpis"]["estimated_profit"]
    profit = body["profit"]
    assert kpi["value"] is None, "多币种不得给出跨币种合计"
    assert kpi["multi_currency"] is True
    assert {item["currency"] for item in profit["by_currency"]} == {"CNY", "USD"}
    assert profit["total_profit"] is None
    currencies = {item["currency"] for item in profit["ranking"]["winners"]}
    assert currencies == {"CNY", "USD"}
    # 每个币种各算各的：都是 100 − 10 − 5 = 85
    assert all(item["profit"] == "85.00" for item in profit["by_currency"])


# ---------------------------------------------------------------------------
# F-22 权限
# ---------------------------------------------------------------------------


def test_f22_profit_requires_analytics_read_and_operator_role(ctx):
    assert _cockpit(ctx, login="admin").status_code == 200
    assert _profit_lines(ctx, login="admin").status_code == 200
    # readonly 有 analytics.read，但驾驶舱/利润沿用既有「仅 admin + operations」的角色闸，
    # 与 /api/dashboard/cockpit 保持一致（不单独开一个更宽的口子）。
    assert _cockpit(ctx, login="viewer").status_code == 403
    assert _profit_lines(ctx, login="viewer").status_code == 403


# ---------------------------------------------------------------------------
# F-23 空态安全
# ---------------------------------------------------------------------------


def test_f23_no_cost_snapshot_still_renders_cockpit(ctx):
    _ingest(ctx, _order(lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 1, "gross": "50.00"}]))
    response = _cockpit(ctx)
    assert response.status_code == 200
    kpi = response.json()["kpis"]["estimated_profit"]
    assert kpi["quality"] == "insufficient"
    assert kpi["reason"] == "NO_COST_SNAPSHOTS"
    assert kpi["value"] is None
    profit = response.json()["profit"]
    assert profit["missing_cost_order_count"] == 1
    assert profit["warning"]["show"] is True


def test_f23b_empty_workspace_profit_is_insufficient(ctx):
    profit = _profit_of(ctx)
    assert profit["quality"] == "insufficient"
    assert profit["reason"] == "NO_ORDERS"
    assert profit["total_profit"] is None
    assert profit["by_currency"] == []


# ---------------------------------------------------------------------------
# F-24 只读纪律 + 查询预算
# ---------------------------------------------------------------------------


def test_f24_finance_module_is_read_only(ctx):
    import os

    source = os.path.join(os.path.dirname(os.path.abspath(ctx.main_mod.__file__)), "finance.py")
    text = open(source, encoding="utf-8").read()
    for forbidden in ("db.add(", "db.commit(", "db.delete(", "db.flush(", "db.merge(", "db.bulk_", "session.add("):
        assert forbidden not in text, f"finance.py 出现写操作：{forbidden}"
    # SQL 层写动词（避免 dict.update() 这类误判，只查 SQLAlchemy 构造器）
    for constructor in ("sqlalchemy import insert", "sqlalchemy import update", "sqlalchemy import delete",
                        "sa.insert(", "sa.update(", "sa.delete("):
        assert constructor not in text, f"finance.py 引入写构造器：{constructor}"
    # 也不该出现「落库缓存」之类的表写入
    assert "CostSnapshot(" not in text.replace("CostSnapshot.sku_id", "")


def test_f24b_summarize_profit_uses_two_queries(ctx):
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(
        ctx,
        _order(order_no="ORD-1", lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 1, "gross": "50.00"}]),
        _order(order_no="ORD-2", updated="2026-09-10T11:00:00Z", lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 2, "gross": "80.00"}]),
    )
    from datetime import date

    import app.finance as finance_mod

    counted: list[str] = []
    with ctx.db_mod.SessionLocal() as db:
        engine = db.get_bind()

        def _before(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                counted.append(statement)

        event.listen(engine, "before_cursor_execute", _before)
        try:
            finance_mod.summarize_profit(
                db, workspace_id=ctx.workspace_id,
                window_from=date(2026, 9, 4), window_to=date(2026, 9, 10),
            )
        finally:
            event.remove(engine, "before_cursor_execute", _before)
    assert len(counted) == 2, f"利润汇总应固定 2 条查询，实际 {len(counted)}"


# ---------------------------------------------------------------------------
# F-25 下钻明细
# ---------------------------------------------------------------------------


def test_f25_detail_filters_and_sums_match_kpi(ctx):
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(
        ctx,
        _order(order_no="ORD-OK", lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 2, "gross": "100.00"}]),
        _order(order_no="ORD-MISSING", updated="2026-09-10T11:00:00Z",
               lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-B", "qty": 1, "gross": "30.00"}]),
    )

    everything = _profit_lines(ctx, status="all").json()
    assert everything["total"] == 2
    assert everything["page_computed"] == 1
    assert everything["page_profit"] == "75.00"

    computed = _profit_lines(ctx, status="computed").json()
    assert computed["total"] == 1
    assert computed["items"][0]["external_order_no"] == "ORD-OK"

    missing = _profit_lines(ctx, status="missing").json()
    assert missing["total"] == 1
    assert missing["items"][0]["profit"] is None
    assert missing["items"][0]["cost_status"] == "missing"

    # 与 KPI 对拍
    kpi_total = _profit_of(ctx)["total_profit"]
    assert computed["page_profit"] == kpi_total


def test_f25b_detail_rejects_invalid_params(ctx):
    assert _profit_lines(ctx, status="bogus").status_code == 422
    assert _profit_lines(ctx, limit=999).status_code == 422
    assert _profit_lines(ctx, currency="CNY").status_code == 200


def test_f25c_orders_sku_filter_powers_ranking_drilldown(ctx):
    """排行点击跳到 /orders?sku=… 的接口支撑：内部编码与外部编码都能命中。"""
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(ctx, _order(lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 1, "gross": "50.00"}]))
    internal = ctx.clients["admin"].get("/api/orders?sku_code=SKU-A")
    external = ctx.clients["admin"].get("/api/orders?sku_code=EXT-SKU-A")
    none = ctx.clients["admin"].get("/api/orders?sku_code=SKU-NOPE")
    assert internal.status_code == 200 and internal.json()["total"] == 1
    assert external.status_code == 200 and external.json()["total"] == 1
    assert none.status_code == 200 and none.json()["total"] == 0


# ---------------------------------------------------------------------------
# R-65 批内重复订单号
# ---------------------------------------------------------------------------


def test_r65_duplicate_order_numbers_rejected_at_entry(ctx):
    """同一批次里内容冲突的重复订单号必须在写入前被拒绝，且一行都不落库。"""
    first = _order(order_no="ORD-DUP", lines=[{"external_line_id": "L-1", "external_sku": "EXT-SKU-A", "qty": 1, "gross": "50.00"}])
    second = _order(order_no="ORD-DUP", updated="2026-09-10T12:00:00Z",
                    lines=[{"external_line_id": "L-2", "external_sku": "EXT-SKU-B", "qty": 1, "gross": "20.00"}])
    with ctx.db_mod.SessionLocal() as db:
        with pytest.raises(ctx.ext_mod.BatchDuplicateOrderError) as excinfo:
            ctx.ext_mod.ingest_orders(db, [first, second], workspace_id=ctx.workspace_id)
    assert excinfo.value.code == "DUPLICATE_ORDER_NO_IN_BATCH"
    assert excinfo.value.order_no == "ORD-DUP"
    assert "ORD-DUP" in str(excinfo.value)
    with ctx.db_mod.SessionLocal() as db:
        orders = db.scalars(select(ctx.db_mod.ExternalOrder).where(
            ctx.db_mod.ExternalOrder.workspace_id == ctx.workspace_id,
        )).all()
    assert orders == [], "重复批次不应留下任何订单行"


def test_r65c_identical_duplicates_stay_idempotent(ctx):
    """内容完全相同的重复行属幂等重放（分页跨页重复 / 用户粘贴两次），不得拒绝。

    这正是「只拦内容冲突」的原因：Shopify 游标翻页返回同一订单两次是正常现象，
    若一律拒绝，正常同步会整批失败。
    """
    record = _order(order_no="ORD-SAME")
    stats = _ingest(ctx, record, record)
    assert stats.inserted == 1
    assert stats.no_op == 1
    with ctx.db_mod.SessionLocal() as db:
        orders = db.scalars(select(ctx.db_mod.ExternalOrder).where(
            ctx.db_mod.ExternalOrder.workspace_id == ctx.workspace_id,
        )).all()
        lines = db.scalars(select(ctx.db_mod.ExternalOrderLine)).all()
    assert len(orders) == 1
    assert len(lines) == 1


def test_r65_duplicate_returns_422_with_actionable_message(ctx):
    """HTTP 层：从 500「报表导入失败」变成 422 + 能照着做的文案。"""
    payload = {
        "platform": "jd",
        "source_mode": "json",
        "content": json.dumps({
            "records": [
                {
                    "account_ref": "account-a", "store_ref": "store-a",
                    "external_order_no": "ORD-DUP", "status": "paid",
                    "created_at": SALE_TIME, "updated_at": SALE_TIME,
                    "event_version": 1,
                    "lines": [{"external_line_id": "L-1", "external_sku": "EXT-SKU-A",
                               "ordered_qty": 1, "cancelled_qty": 0, "refunded_qty": 0,
                               "gross_amount": "50.00", "refund_amount": "0.00"}],
                },
                {
                    "account_ref": "account-a", "store_ref": "store-a",
                    "external_order_no": "ORD-DUP", "status": "paid",
                    "created_at": SALE_TIME, "updated_at": SALE_TIME,
                    "event_version": 1,
                    "lines": [{"external_line_id": "L-2", "external_sku": "EXT-SKU-B",
                               "ordered_qty": 1, "cancelled_qty": 0, "refunded_qty": 0,
                               "gross_amount": "20.00", "refund_amount": "0.00"}],
                },
            ],
        }, ensure_ascii=False),
    }
    response = ctx.clients["admin"].post("/api/external/orders/ingest", json=payload)
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "DUPLICATE_ORDER_NO_IN_BATCH"
    assert "ORD-DUP" in detail["message"]
    assert "一行一个订单" in detail["message"]



# ---------------------------------------------------------------------------
# F-26 前端契约（照 P5-S2 的做法把「页面必须有的锚点」写进测试，防静默丢失）
# ---------------------------------------------------------------------------


def test_f26_cockpit_and_orders_template_contracts(ctx):
    import os

    base = os.path.dirname(os.path.abspath(ctx.main_mod.__file__))
    cockpit = open(os.path.join(base, "templates", "cockpit.html"), encoding="utf-8").read()
    orders = open(os.path.join(base, "templates", "orders.html"), encoding="utf-8").read()

    # 面板与黄条必须存在（锚点登记在 _PAGE_ANCHORS 里，删了导航自检会失配）
    assert 'id="cockpit-profit-panel"' in cockpit
    assert 'id="profit-warning"' in cockpit
    assert 'id="profit-ranking"' in cockpit
    assert 'id="profit-detail"' in cockpit
    # 利润卡片可下钻 + 口径 tooltip 来自后端 note（不在模板里硬编码第二份文案）
    assert "data-profit-drill" in cockpit
    assert "card.note" in cockpit
    assert "toggleProfitDetail" in cockpit
    # 排行行必须是可点击链接到订单页
    assert '/orders?sku=' in cockpit
    # 订单页承接 ?sku= 并对接口传 sku_code
    assert "presetSku" in orders and "['sku','sku_code']" in orders
