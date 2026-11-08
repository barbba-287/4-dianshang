"""P8-S1 展平报表自动预聚合测试（R-66）：F-27..F-35。

编号口径（dev_task.p8-order-merge.md §5）：

- F-27 ★用户指定：3 个 SKU 的同一订单号 CSV → 自动聚合成 1 单 3 明细并成功入库
- F-28 幂等：同一 CSV 重传全部 no_op，订单/明细数不变（payload_hash 重算必须确定性）
- F-29 M3 金额三态：SAME / SINGLE_NONZERO / SUM
- F-30 M2 字段冲突 → 放弃合并 → R-65 的 422，零写入
- F-31 非 CSV 渠道不受影响（json 仍 422、shopify_live 原样通过）
- F-32 缺 external_line_id 时组内统一重编号，不撞 uq_external_order_line_identity
- F-33 合并后逐 SKU 成本各自 as-of，不得共享成本
- F-34 聚合是纯内存：零 SQL；入库的快照查询仍为 1 条
- F-35 SUM 态下 明细逐行之和 == 主表 gross_amount
"""
from __future__ import annotations

from dataclasses import replace
import importlib
import json
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sa_event
from sqlalchemy import select

SALE_TIME = "2026-09-10T10:00:00Z"
UPDATED = "2026-09-10T10:01:00Z"

HEADER = (
    "external_order_no,account_ref,store_ref,status,created_at,updated_at,"
    "external_sku,ordered_qty,gross_amount"
)


def _csv(*rows: tuple[str, str, int, str], status: str = "paid", updated: str = UPDATED) -> bytes:
    """rows: (订单号, 外部SKU, 数量, 金额)。刻意不带 external_line_id 列——真实报表也没有。"""
    body = "".join(
        f"{order_no},account-a,store-a,{status},{SALE_TIME},{updated},{sku},{qty},{gross}\n"
        for order_no, sku, qty, gross in rows
    )
    return (HEADER + "\n" + body).encode()


class Ctx:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'p8s1.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("PROFIT_PLATFORM_FEE_RATE", "0.05")
    monkeypatch.setenv("PROFIT_PLATFORM_FEE_OVERRIDES", "")

    import app.config as cfg
    import app.db as db_mod
    import app.external_orders as ext_mod
    import app.main as main_mod
    import app.order_aggregation as merge_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(ext_mod)
    importlib.reload(merge_mod)
    importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)

    from app.connectors import load_orders
    from app.employee_auth import hash_password

    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="p8s1", name="P8-S1 预聚合测试")
        db.add(workspace)
        db.flush()
        account = db_mod.UserAccount(
            login="operator", password_hash=hash_password("operator-pw"), display_name="运营",
        )
        db.add(account)
        db.flush()
        db.add(db_mod.WorkspaceMembership(
            workspace_id=workspace.id, user_id=account.id, role="operations",
        ))
        product = db_mod.Product(
            workspace_id=workspace.id, source="fixture", external_product_id="p8s1-p",
            title="P8-S1 商品", url="https://fixture.local/p8s1-p",
            current_price=Decimal("40.00"),
        )
        db.add(product)
        db.flush()
        skus = {}
        for code in ("A", "B", "C"):
            sku = db_mod.ProductSku(
                workspace_id=workspace.id, product_id=product.id, sku_code=f"SKU-{code}",
            )
            db.add(sku)
            db.flush()
            skus[code] = sku.id
        external = ext_mod.ensure_external_account(
            db, workspace_id=workspace.id, platform="taobao",
            account_ref="account-a", store_ref="store-a",
        )
        for code in ("A", "B", "C"):
            db.add(db_mod.ExternalProductMapping(
                workspace_id=workspace.id, external_account_id=external.id,
                external_sku=f"EXT-SKU-{code}", internal_sku_id=skus[code],
                mapping_status="mapped",
            ))
        # F-33：三个 SKU 三个不同成本价（同一天生效，as-of 只看 sku 不看顺序）
        for code, cost in (("A", "10.00"), ("B", "20.00"), ("C", "30.00")):
            db.add(db_mod.CostSnapshot(
                workspace_id=workspace.id, sku_id=skus[code],
                unit_cost=Decimal(cost), effective_time=datetime(2026, 8, 1),
                source="manual", source_ref=f"p8s1-{code}", currency="CNY",
            ))
        db.commit()
        ids = {"workspace_id": workspace.id, "account_id": external.id, **skus}

    client = TestClient(main_mod.app)
    login = client.post(
        "/login",
        data={"login": "operator", "password": "operator-pw", "next": "/"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    client.headers.update({"X-CSRF-Token": client.cookies.get("dianshang_csrf")})

    yield Ctx(
        db_mod=db_mod, ext_mod=ext_mod, merge_mod=merge_mod, main_mod=main_mod,
        load_orders=load_orders, client=client, **ids,
    )
    cfg.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _upload(ctx, content: bytes, *, path="/api/external/imports", platform="taobao", kind="orders", **extra):
    data = {"platform": platform, "kind": kind}
    data.update(extra)
    return ctx.client.post(path, files={"file": ("orders.csv", content, "text/csv")}, data=data)


def _orders(ctx):
    with ctx.db_mod.SessionLocal() as db:
        return db.scalars(select(ctx.db_mod.ExternalOrder)).all()


def _lines(ctx):
    with ctx.db_mod.SessionLocal() as db:
        return db.scalars(
            select(ctx.db_mod.ExternalOrderLine).order_by(ctx.db_mod.ExternalOrderLine.id)
        ).all()


def _records(ctx, content: bytes):
    return ctx.load_orders(content.decode(), platform="taobao", source_mode="csv")


THREE_SKU_CSV = _csv(
    ("ORD-3SKU", "EXT-SKU-A", 2, "40.00"),
    ("ORD-3SKU", "EXT-SKU-B", 1, "30.00"),
    ("ORD-3SKU", "EXT-SKU-C", 1, "20.00"),
)


# ---------------------------------------------------------------------------
# F-27 ★用户指定：3 个 SKU 的同一订单号 CSV 自动聚合成 1 单
# ---------------------------------------------------------------------------


def test_f27_three_sku_same_order_number_merges_into_one_order(ctx):
    """用户指定场景：真实的「一行一个 SKU」报表必须能直接导入，不再 422。"""
    response = _upload(ctx, THREE_SKU_CSV)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["inserted"] == 1, "3 行应聚合成 1 个订单"
    assert body["total"] == 3, "total 仍是文件数据行数（P6 语义）"

    orders = _orders(ctx)
    lines = _lines(ctx)
    assert len(orders) == 1
    assert len(lines) == 3
    assert {line.external_sku for line in lines} == {"EXT-SKU-A", "EXT-SKU-B", "EXT-SKU-C"}
    assert orders[0].external_order_no == "ORD-3SKU"
    assert orders[0].gross_amount == Decimal("90.00"), "SUM 态：40+30+20"


def test_f27b_preview_matches_commit_cardinality(ctx):
    """预览与入库必须同口径：预览看到 1 条，入库也是 1 单（否则商家会以为文件丢了）。"""
    preview = _upload(ctx, THREE_SKU_CSV, path="/api/external/imports/preview")
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["total"] == 3
    assert len(body["normalized_rows"]) == 1
    assert len(body["normalized_rows"][0]["lines"]) == 3


# ---------------------------------------------------------------------------
# F-28 幂等
# ---------------------------------------------------------------------------


def test_f28_same_file_is_idempotent_after_merge(ctx):
    """合并后 payload_hash 必须重算且确定性——否则重传会变成"反复更新"或"静默不更新"。"""
    first = _upload(ctx, THREE_SKU_CSV)
    assert first.status_code == 200, first.text
    assert first.json()["inserted"] == 1

    second = _upload(ctx, THREE_SKU_CSV)
    assert second.status_code == 200, second.text
    assert second.json()["no_op"] == 1
    assert second.json()["inserted"] == 0
    assert len(_orders(ctx)) == 1
    assert len(_lines(ctx)) == 3

    # 第三次仍应 no_op（hash 不能随调用次数漂移）
    third = _upload(ctx, THREE_SKU_CSV)
    assert third.status_code == 200, third.text
    assert third.json()["no_op"] == 1


def test_f28b_content_change_is_detected_not_swallowed(ctx):
    """内容真变了必须走 updated：合并若沿用首行 hash，第二行的变化会被静默吞掉。"""
    assert _upload(ctx, THREE_SKU_CSV).status_code == 200
    changed = _csv(
        ("ORD-3SKU", "EXT-SKU-A", 2, "40.00"),
        ("ORD-3SKU", "EXT-SKU-B", 5, "30.00"),  # 数量 1 → 5
        ("ORD-3SKU", "EXT-SKU-C", 1, "20.00"),
        updated="2026-09-11T09:00:00Z",  # 平台侧更新时间必须更晚，否则按 stale 跳过
    )
    response = _upload(ctx, changed)
    assert response.status_code == 200, response.text
    assert response.json()["updated"] == 1
    lines = {line.external_sku: line for line in _lines(ctx)}
    assert len(lines) == 3
    assert lines["EXT-SKU-B"].ordered_qty == 5


# ---------------------------------------------------------------------------
# F-29 M3 金额三态
# ---------------------------------------------------------------------------


def _merge(ctx, content: bytes):
    records, reports = ctx.merge_mod.aggregate_order_records(_records(ctx, content))
    assert len(records) == 1 and len(reports) == 1
    return records[0], reports[0]


def test_f29a_amount_rule_same(ctx):
    """多行取值唯一（"订单总额复制到每行"）→ 取该值，然后按数量分摊（R-67 方案②）。"""
    csv_bytes = _csv(
        ("ORD-SAME", "EXT-SKU-A", 1, "100.00"),
        ("ORD-SAME", "EXT-SKU-B", 1, "100.00"),
        ("ORD-SAME", "EXT-SKU-C", 1, "100.00"),
    )
    record, report = _merge(ctx, csv_bytes)
    assert report.gross_rule == ctx.merge_mod.MERGE_RULE_ALLOCATED, "SAME 态且每行都等于单头 → 触发分摊"
    assert record.gross_amount == Decimal("100.00"), "单头金额不变，始终是商家给的订单总额"
    assert [line.gross_amount for line in record.lines] == [
        Decimal("33.33"), Decimal("33.33"), Decimal("33.34"),
    ], "三等分：前两行 33.33，末行补齐 33.34"
    assert report.lines_gross_sum == Decimal("100.00")
    assert report.delta == Decimal("0.00"), "分摊后逐行之和必须与单头一致，M6 校验才通过"


def test_f29b_amount_rule_single_nonzero(ctx):
    """订单级金额挂在首行、其余行为 0 → 取那个非零值（不求和，否则 0 会稀释）。"""
    csv_bytes = _csv(
        ("ORD-ONE", "EXT-SKU-A", 1, "90.00"),
        ("ORD-ONE", "EXT-SKU-B", 1, "0"),
        ("ORD-ONE", "EXT-SKU-C", 1, "0"),
    )
    record, report = _merge(ctx, csv_bytes)
    assert report.gross_rule == ctx.merge_mod.MERGE_RULE_SINGLE_NONZERO
    assert record.gross_amount == Decimal("90.00")
    assert report.delta == Decimal("0.00"), "主表 90 与明细之和 90.00 一致"


def test_f29c_amount_rule_sum(ctx):
    """每行填的是本行金额 → 求和（取首行会丢掉后两行的钱）。"""
    record, report = _merge(ctx, THREE_SKU_CSV)
    assert report.gross_rule == ctx.merge_mod.MERGE_RULE_SUM
    assert record.gross_amount == Decimal("90.00")
    assert report.delta == Decimal("0.00")


def test_f29d_refund_amount_uses_same_rules_independently(ctx):
    """refund_amount 独立判定：不能因 gross 是 SUM 态就把 refund 也强行求和。"""
    csv_bytes = (
        HEADER.replace("gross_amount", "gross_amount,refund_amount")
        + "\n"
        f"ORD-REF,account-a,store-a,paid,{SALE_TIME},{UPDATED},EXT-SKU-A,1,50.00,0\n"
        f"ORD-REF,account-a,store-a,paid,{SALE_TIME},{UPDATED},EXT-SKU-B,1,30.00,20.00\n"
    ).encode()
    record, report = _merge(ctx, csv_bytes)
    assert report.gross_rule == ctx.merge_mod.MERGE_RULE_SUM
    assert record.gross_amount == Decimal("80.00")
    assert report.refund_rule == ctx.merge_mod.MERGE_RULE_SINGLE_NONZERO
    assert record.refund_amount == Decimal("20.00")


# ---------------------------------------------------------------------------
# F-30 M2 字段冲突：放弃合并，交给 R-65 的 422
# ---------------------------------------------------------------------------


def test_f30_status_conflict_falls_back_to_r65_rejection(ctx):
    """状态不一致说明文件自相矛盾——不替商家编造事实，原样交回让 R-65 拦。"""
    first = _csv(("ORD-CONFLICT", "EXT-SKU-A", 1, "50.00"), status="paid")
    second = _csv(("ORD-CONFLICT", "EXT-SKU-B", 1, "50.00"), status="cancelled")
    content = first + second.split(b"\n", 1)[1]  # 拼成三行（含一个表头）
    response = _upload(ctx, content)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "DUPLICATE_ORDER_NO_IN_BATCH"
    assert _orders(ctx) == [], "拒绝必须发生在任何写入之前"


def test_f30b_merge_failure_never_raises(ctx):
    """M7：聚合是增强，任何判定不通过都只能降级，绝不许抛异常。"""
    records = _records(ctx, _csv(
        ("ORD-X", "EXT-SKU-A", 1, "50.00"),
        ("ORD-X", "EXT-SKU-B", 1, "50.00"),
    ))
    conflicted = [records[0], replace(records[1], order_status="cancelled")]
    merged, reports = ctx.merge_mod.aggregate_order_records(conflicted)
    assert merged == conflicted
    assert reports == []


# ---------------------------------------------------------------------------
# F-31 非 CSV 渠道零影响
# ---------------------------------------------------------------------------


def test_f31a_json_mode_still_rejects_duplicates(ctx):
    """JSON 模式的记录本就该是一行一个订单，同名冲突仍是 R-65 的 422。"""
    payload = {
        "platform": "taobao",
        "source_mode": "json",
        "content": json.dumps({"records": [
            {
                "account_ref": "account-a", "store_ref": "store-a",
                "external_order_no": "ORD-JSON", "status": "paid",
                "created_at": SALE_TIME, "updated_at": UPDATED, "event_version": 1,
                "lines": [{"external_line_id": "L-1", "external_sku": "EXT-SKU-A",
                           "ordered_qty": 1, "cancelled_qty": 0, "refunded_qty": 0,
                           "gross_amount": "50.00", "refund_amount": "0.00"}],
            },
            {
                "account_ref": "account-a", "store_ref": "store-a",
                "external_order_no": "ORD-JSON", "status": "paid",
                "created_at": SALE_TIME, "updated_at": UPDATED, "event_version": 1,
                "lines": [{"external_line_id": "L-2", "external_sku": "EXT-SKU-B",
                           "ordered_qty": 1, "cancelled_qty": 0, "refunded_qty": 0,
                           "gross_amount": "20.00", "refund_amount": "0.00"}],
            },
        ]}, ensure_ascii=False),
    }
    response = ctx.client.post("/api/external/orders/ingest", json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "DUPLICATE_ORDER_NO_IN_BATCH"


def test_f31b_shopify_live_records_are_never_merged(ctx):
    """Shopify live 不受影响是**结构性**保证：source_mode 不是 csv 就不进合并分支。"""
    records = _records(ctx, THREE_SKU_CSV)
    live = [replace(record, source_mode="shopify_live") for record in records]
    merged, reports = ctx.merge_mod.aggregate_order_records(live)
    assert reports == []
    assert len(merged) == 3, "三条同名 live 记录必须原样保留"


def test_f31c_mock_mode_records_are_never_merged(ctx):
    records = _records(ctx, THREE_SKU_CSV)
    mock = [replace(record, source_mode="mock") for record in records]
    merged, reports = ctx.merge_mod.aggregate_order_records(mock)
    assert reports == []
    assert len(merged) == 3


# ---------------------------------------------------------------------------
# F-32 external_line_id 冲突重编号
# ---------------------------------------------------------------------------


def test_f32_missing_external_line_id_gets_unique_ids(ctx):
    """CSV 没有 external_line_id 列 → 每行都是 line:0 → 合并必须重编号，否则撞唯一约束。

    P9-S3（dev_task.p9s3-order-upsert.md §2 D2 / §6 T-R68-5）：**入库后的行 id 形态变了**——
    合成 id 已从 `line:N` 改为稳定指纹 `csv:{sku_token}|{seq}`。
    本用例的意图（组内 id 互不相同、不撞唯一约束）**一字未改**，只是断言跟着新规则改写。
    注：聚合层（`aggregate_order_records`）的 id 仍是 `line:0..`——见 F-32b，那条未动。
    """
    response = _upload(ctx, THREE_SKU_CSV)
    assert response.status_code == 200, response.text
    lines = _lines(ctx)
    assert len(lines) == 3
    assert len({line.external_line_id for line in lines}) == 3, "组内 id 必须互不相同"
    assert sorted(line.external_line_id for line in lines) == [
        "csv:EXT-SKU-A|0", "csv:EXT-SKU-B|0", "csv:EXT-SKU-C|0",
    ]


def test_f32b_unique_ids_are_preserved(ctx):
    """原本互不冲突的 id 不该被无谓改写（保持与上游一致）。"""
    records = _records(ctx, THREE_SKU_CSV)
    assert all(record.lines[0].external_line_id == "line:0" for record in records)
    merged, _ = ctx.merge_mod.aggregate_order_records(records)
    assert [line.external_line_id for line in merged[0].lines] == ["line:0", "line:1", "line:2"]


# ---------------------------------------------------------------------------
# F-33 成本锚定逐 SKU 独立
# ---------------------------------------------------------------------------


def test_f33_each_sku_keeps_its_own_cost(ctx):
    """合并不得让多个明细行共享同一个 unit_cost——那是 P7-S2 as-of 锚定的命根子。"""
    assert _upload(ctx, THREE_SKU_CSV).status_code == 200
    lines = {line.external_sku: line for line in _lines(ctx)}
    assert {sku: line.unit_cost for sku, line in lines.items()} == {
        "EXT-SKU-A": Decimal("10.00"),
        "EXT-SKU-B": Decimal("20.00"),
        "EXT-SKU-C": Decimal("30.00"),
    }
    assert all(line.cost_status == "resolved" for line in lines.values())
    assert len({line.cost_snapshot_id for line in lines.values()}) == 3


# ---------------------------------------------------------------------------
# F-34 查询预算
# ---------------------------------------------------------------------------


def test_f34_aggregation_is_pure_memory(ctx):
    """聚合是纯内存：不得产生任何 SQL（红线 R-2 的延伸）。"""
    records = _records(ctx, THREE_SKU_CSV)
    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa_event.listen(ctx.db_mod.engine, "before_cursor_execute", _capture)
    try:
        ctx.merge_mod.aggregate_order_records(records)
    finally:
        sa_event.remove(ctx.db_mod.engine, "before_cursor_execute", _capture)
    assert statements == [], f"聚合产生了 SQL：{statements}"


def test_f34b_snapshot_query_still_one(ctx):
    """合并后入库时，成本快照批量查询仍需 ≤1 条（3 个 SKU 也只 1 条）。"""
    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa_event.listen(ctx.db_mod.engine, "before_cursor_execute", _capture)
    try:
        assert _upload(ctx, THREE_SKU_CSV).status_code == 200
    finally:
        sa_event.remove(ctx.db_mod.engine, "before_cursor_execute", _capture)
    snapshot_queries = [sql for sql in statements if "cost_snapshots" in sql.lower()]
    assert len(snapshot_queries) == 1, f"期望 1 条快照查询，实际 {len(snapshot_queries)}"


# ---------------------------------------------------------------------------
# F-35 金额对账
# ---------------------------------------------------------------------------


def test_f35_sum_rule_lines_equal_main_gross(ctx):
    """SUM 态下主表金额必须精确等于明细逐行之和（两位小数，不许浮点漂移）。"""
    assert _upload(ctx, THREE_SKU_CSV).status_code == 200
    order = _orders(ctx)[0]
    lines = _lines(ctx)
    assert sum((line.gross_amount for line in lines), Decimal("0")) == order.gross_amount
    assert order.gross_amount == Decimal("90.00")


def test_f35b_daily_sales_rebuilt_from_merged_order(ctx):
    """日销聚合必须基于合并后的明细，三个 SKU 各一行，数量与金额对得上。"""
    assert _upload(ctx, THREE_SKU_CSV).status_code == 200
    with ctx.db_mod.SessionLocal() as db:
        rows = {
            row.external_sku: row
            for row in db.scalars(select(ctx.db_mod.DailySkuSale)).all()
        }
    assert set(rows) == {"EXT-SKU-A", "EXT-SKU-B", "EXT-SKU-C"}
    assert rows["EXT-SKU-A"].net_qty == 2
    assert rows["EXT-SKU-A"].net_amount == Decimal("40.00")
    assert all(row.order_count == 1 for row in rows.values())


# ---------------------------------------------------------------------------
# F-36 ★P7 x P8 边界：合并订单的利润必须 == 三个明细行利润之和（用户指定对拍）
# ---------------------------------------------------------------------------


def _cockpit_profit(ctx, *, days: int = 7, as_of: str = "2026-09-10") -> dict:
    response = ctx.client.get(f"/api/dashboard/cockpit?days={days}&as_of={as_of}")
    assert response.status_code == 200, response.text
    return response.json()["profit"]


def _profit_lines(ctx, *, days: int = 7, as_of: str = "2026-09-10") -> dict:
    response = ctx.client.get(f"/api/finance/profit/lines?days={days}&as_of={as_of}&limit=50")
    assert response.status_code == 200, response.text
    return response.json()


def test_f36_merged_order_profit_equals_sum_of_line_profits(ctx):
    """★用户指定对拍：3 行同名 CSV 合并后，利润引擎按**明细行**算，不得按订单头重复计收入。

    夹具成本 A=10 / B=20 / C=30，费率 5%：
        A: 40 - 10x2 - 40x5%  =  18.00
        B: 30 - 20x1 - 30x5%  =   8.50
        C: 20 - 30x1 - 20x5%  = -11.00   （成本高于售价 → 亏损行，必须进 losers）
        合计：收入 90.00  成本 70.00  平台费 4.50  利润 15.50

    关键断言是 revenue == 90.00 而不是 270.00：如果引擎读的是订单头 `gross_amount`，
    合并后的单头 90 会被 3 个明细行各取一次 → 收入三倍、利润虚高（R-67 的形态）。
    """
    assert _upload(ctx, THREE_SKU_CSV).status_code == 200
    order = _orders(ctx)[0]
    assert order.gross_amount == Decimal("90.00"), "SUM 态：40+30+20"
    assert len(_lines(ctx)) == 3

    profit = _cockpit_profit(ctx)
    assert profit["computed_lines"] == 3, "三行都必须成功锚定成本"
    assert profit["quality"] == "complete"
    cell = profit["by_currency"][0]
    assert cell["revenue"] == "90.00", "▲ 收入必须等于明细之和，绝不能是订单头的 3 倍（270.00）"
    assert cell["revenue"] != "270.00", "R-67 形态：订单头金额被逐行重复计入"
    assert cell["cost"] == "70.00"
    assert cell["platform_fee"] == "4.50"
    assert cell["profit"] == "15.50"
    assert profit["total_profit"] == "15.50"

    detail = _profit_lines(ctx)
    assert detail["total"] == 3
    line_profit_sum = sum((Decimal(item["profit"]) for item in detail["items"]), Decimal("0"))
    assert line_profit_sum == Decimal("15.50"), "逐行利润之和必须等于 KPI 总额"
    assert detail["page_profit"] == "15.50"
    per_line = {item["external_sku"]: item["profit"] for item in detail["items"]}
    assert per_line == {"EXT-SKU-A": "18.00", "EXT-SKU-B": "8.50", "EXT-SKU-C": "-11.00"}


def test_f36b_merged_order_lines_anchor_independent_costs(ctx):
    """合并不得让三个 SKU 共享同一份成本——每行各自的 unit_cost 必须来自自己的 SKU 快照。"""
    assert _upload(ctx, THREE_SKU_CSV).status_code == 200
    anchored = {line.external_sku: line for line in _lines(ctx)}
    assert anchored["EXT-SKU-A"].unit_cost == Decimal("10.00")
    assert anchored["EXT-SKU-B"].unit_cost == Decimal("20.00")
    assert anchored["EXT-SKU-C"].unit_cost == Decimal("30.00")
    assert all(line.cost_status == "resolved" for line in anchored.values())
    assert anchored["EXT-SKU-A"].fee_rate == Decimal("0.0500")


def test_f36c_merged_order_ranking_splits_winner_and_loser(ctx):
    """排行必须按明细粒度拆分：赚钱的 A/B 进 winners，亏钱的 C 进 losers。"""
    assert _upload(ctx, THREE_SKU_CSV).status_code == 200
    ranking = _cockpit_profit(ctx)["ranking"]
    winners = {item["sku_code"]: item["profit"] for item in ranking["winners"]}
    losers = {item["sku_code"]: item["profit"] for item in ranking["losers"]}
    assert winners == {"SKU-A": "18.00", "SKU-B": "8.50"}
    assert losers == {"SKU-C": "-11.00"}


# ---------------------------------------------------------------------------
# F-37 ★R-67 方案②：单头金额复制到每行 → 按数量分摊（用户 2026-11-06 裁定）
# ---------------------------------------------------------------------------


def _same_amount_csv(order_no: str, rows: tuple[tuple[str, int], ...], amount: str) -> bytes:
    """每一行都填同一个金额（商家把订单总额复制到每行的脏形态）。"""
    return _csv(*[(order_no, sku, qty, amount) for sku, qty in rows])


def test_f37_allocates_order_total_across_lines_by_quantity(ctx):
    """★用户指定：每行都是 90 的报表 → 分摊后各行金额合理（45 / 22.50 / 22.50）且能正常入库。"""
    csv_bytes = _same_amount_csv(
        "ORD-ALLOC", (("EXT-SKU-A", 2), ("EXT-SKU-B", 1), ("EXT-SKU-C", 1)), "90.00",
    )
    record, report = _merge(ctx, csv_bytes)
    assert report.gross_rule == ctx.merge_mod.MERGE_RULE_ALLOCATED
    assert record.gross_amount == Decimal("90.00")
    # 摊法：90 × 2/4 = 45.00；90 × 1/4 = 22.50；末行补齐 90 − 67.50 = 22.50
    assert [line.gross_amount for line in record.lines] == [
        Decimal("45.00"), Decimal("22.50"), Decimal("22.50"),
    ]
    assert sum(line.gross_amount for line in record.lines) == Decimal("90.00")
    assert report.delta == Decimal("0.00")

    # 端到端：入库 → 利润引擎按分摊后的行金额算，总额就是订单总额 90
    assert _upload(ctx, csv_bytes).status_code == 200
    order = _orders(ctx)[0]
    assert order.gross_amount == Decimal("90.00")
    profit = _cockpit_profit(ctx)
    cell = profit["by_currency"][0]
    assert cell["revenue"] == "90.00", "分摊后收入必须等于订单总额，不是 3×90=270"
    assert cell["cost"] == "70.00"
    assert cell["platform_fee"] == "4.51", "2.25 + 1.13 + 1.13（逐行四舍五入后求和）"
    assert cell["profit"] == "15.49"
    assert profit["total_profit"] == "15.49"
    assert profit["quality"] == "complete"

    detail = _profit_lines(ctx)
    per_line = {item["external_sku"]: item["profit"] for item in detail["items"]}
    assert per_line == {"EXT-SKU-A": "22.75", "EXT-SKU-B": "1.37", "EXT-SKU-C": "-8.63"}
    assert sum(Decimal(value) for value in per_line.values()) == Decimal("15.49")


def test_f37b_last_line_absorbs_rounding_remainder(ctx):
    """四舍五入残差必须由末行补齐：100 三等分 → 33.33 / 33.33 / 33.34，合计分毫不差。"""
    csv_bytes = _same_amount_csv(
        "ORD-ROUND", (("EXT-SKU-A", 1), ("EXT-SKU-B", 1), ("EXT-SKU-C", 1)), "100.00",
    )
    record, report = _merge(ctx, csv_bytes)
    amounts = [line.gross_amount for line in record.lines]
    assert amounts == [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")]
    assert sum(amounts) == Decimal("100.00"), "末行补齐后必须精确等于单头金额"
    assert report.delta == Decimal("0.00")


def test_f37c_correct_per_line_amounts_are_never_allocated(ctx):
    """本来每行就填对的（SUM 态）一律不动 —— 规则 5「其他地方维持现状」。"""
    record, report = _merge(ctx, THREE_SKU_CSV)
    assert report.gross_rule == ctx.merge_mod.MERGE_RULE_SUM
    assert [line.gross_amount for line in record.lines] == [
        Decimal("40.00"), Decimal("30.00"), Decimal("20.00"),
    ]
    assert report.delta == Decimal("0.00")


def test_f37d_single_nonzero_form_is_never_allocated(ctx):
    """「总额挂首行、其余填 0」形态本来就是对的，绝不能分摊（否则 0 行被凭空塞钱）。"""
    csv_bytes = _csv(
        ("ORD-ONE", "EXT-SKU-A", 1, "90.00"),
        ("ORD-ONE", "EXT-SKU-B", 1, "0"),
        ("ORD-ONE", "EXT-SKU-C", 1, "0"),
    )
    record, report = _merge(ctx, csv_bytes)
    assert report.gross_rule == ctx.merge_mod.MERGE_RULE_SINGLE_NONZERO
    assert [line.gross_amount for line in record.lines] == [
        Decimal("90.00"), Decimal("0"), Decimal("0"),
    ]


def test_f37e_zero_quantity_skips_allocation_and_keeps_warning(ctx):
    """全部行数量为 0 时分母为 0 → 不分摊、绝不抛错，保持原值并留 delta 信号。"""
    csv_bytes = _same_amount_csv(
        "ORD-ZQ", (("EXT-SKU-A", 0), ("EXT-SKU-B", 0)), "90.00",
    )
    record, report = _merge(ctx, csv_bytes)
    assert report.gross_rule == ctx.merge_mod.MERGE_RULE_SAME, "无法分摊时保持 M3 原判"
    assert [line.gross_amount for line in record.lines] == [Decimal("90.00"), Decimal("90.00")]
    assert report.delta == Decimal("-90.00"), "留信号给 M6，不静默"


def test_f37f_allocation_is_deterministic_across_reupload(ctx):
    """分摊必须确定性：同一文件重传得到同样的行金额，否则幂等失效、锚定反复重建。"""
    csv_bytes = _same_amount_csv(
        "ORD-DET", (("EXT-SKU-A", 2), ("EXT-SKU-B", 1), ("EXT-SKU-C", 1)), "90.00",
    )
    assert _upload(ctx, csv_bytes).status_code == 200
    first = sorted(str(line.gross_amount) for line in _lines(ctx))
    again = _upload(ctx, csv_bytes)
    assert again.status_code == 200
    assert again.json()["no_op"] == 1, "重传必须命中 no_op，分摊结果不得漂移"
    second = sorted(str(line.gross_amount) for line in _lines(ctx))
    assert first == second == ["22.50", "22.50", "45.00"]


# ---------------------------------------------------------------------------
# F-38 ★R-71：退款金额也被复制到每行 → 同样按数量分摊（用户 2026-11-06 裁定）
# ---------------------------------------------------------------------------

REFUND_HEADER = HEADER.replace("gross_amount", "gross_amount,refund_amount")


def _csv_refund(*rows: tuple[str, str, int, str, str]) -> bytes:
    """rows: (订单号, 外部SKU, 数量, 商品金额, 退款金额)。"""
    body = "".join(
        f"{order_no},account-a,store-a,paid,{SALE_TIME},{UPDATED},{sku},{qty},{gross},{refund}\n"
        for order_no, sku, qty, gross, refund in rows
    )
    return (REFUND_HEADER + "\n" + body).encode()


# 订单总额 200、退款总额 90，但商家把两个总额都复制到了每一行（双脏形态）
DIRTY_REFUND_CSV = _csv_refund(
    ("ORD-R71", "EXT-SKU-A", 1, "200.00", "90.00"),
    ("ORD-R71", "EXT-SKU-B", 1, "200.00", "90.00"),
    ("ORD-R71", "EXT-SKU-C", 1, "200.00", "90.00"),
)


def test_f38_refund_copied_to_every_row_is_allocated(ctx):
    """★用户指定：退款也是每行 90 的脏报表 → 分摊后利润完全正确（不退化成负数）。

    分摊结果：
        商品 200 → 66.67 / 66.67 / 66.66（和 200）
        退款  90 → 30.00 / 30.00 / 30.00（和 90）
        实付净额 → 36.67 / 36.67 / 36.66（和 110 = 200 − 90）

    若不摊退款：三行各扣 90，逐行 66.67 − 90 < 0 被下限截成 0，收入归 0、利润 −60 ——
    正是「退款被扣 N 次」的财务灾难。夹具成本 A=10 / B=20 / C=30，费率 5%：
        合计 收入 110.00  成本 60.00  平台费 5.49  利润 44.51
    """
    record, report = _merge(ctx, DIRTY_REFUND_CSV)
    assert [line.gross_amount for line in record.lines] == [
        Decimal("66.67"), Decimal("66.67"), Decimal("66.66"),
    ]
    assert [line.refund_amount for line in record.lines] == [
        Decimal("30.00"), Decimal("30.00"), Decimal("30.00"),
    ]
    assert sum(line.refund_amount for line in record.lines) == Decimal("90.00")

    assert _upload(ctx, DIRTY_REFUND_CSV).status_code == 200
    profit = _cockpit_profit(ctx)
    cell = profit["by_currency"][0]
    assert cell["revenue"] == "110.00", "实付净额必须是 200 − 90，不是 200 − 270"
    assert cell["revenue"] != "0.00", "未摊退款时收入会被下限截成 0 —— 这正是 R-71 的后果"
    assert cell["cost"] == "60.00"
    assert cell["platform_fee"] == "5.49"
    assert cell["profit"] == "44.51"
    assert profit["total_profit"] == "44.51"

    detail = _profit_lines(ctx)
    per_line = {item["external_sku"]: item["profit"] for item in detail["items"]}
    assert per_line == {"EXT-SKU-A": "24.84", "EXT-SKU-B": "14.84", "EXT-SKU-C": "4.83"}
    assert sum(Decimal(value) for value in per_line.values()) == Decimal("44.51")


def test_f38b_both_rules_reported_allocated_and_deltas_zero(ctx):
    """gross 与 refund 各自独立判定，但双脏形态下两条都应记为 same_allocated 且 delta 归零。"""
    record, report = _merge(ctx, DIRTY_REFUND_CSV)
    assert report.gross_rule == ctx.merge_mod.MERGE_RULE_ALLOCATED
    assert report.refund_rule == ctx.merge_mod.MERGE_RULE_ALLOCATED
    assert record.gross_amount == Decimal("200.00"), "单头金额不变"
    assert record.refund_amount == Decimal("90.00")
    assert report.delta == Decimal("0.00")
    assert report.refund_delta == Decimal("0.00"), "分摊后退款也必须对齐，M6 才通过"
    assert report.lines_refund_sum == Decimal("90.00")


def test_f38c_correct_per_line_refund_is_never_allocated(ctx):
    """退款本来就逐行填对的（SUM 态）一律不动 —— 复用判据不等于滥用判据。"""
    csv_bytes = _csv_refund(
        ("ORD-RSUM", "EXT-SKU-A", 1, "100.00", "20.00"),
        ("ORD-RSUM", "EXT-SKU-B", 1, "60.00", "5.00"),
        ("ORD-RSUM", "EXT-SKU-C", 1, "40.00", "5.00"),
    )
    record, report = _merge(ctx, csv_bytes)
    assert report.gross_rule == ctx.merge_mod.MERGE_RULE_SUM
    assert report.refund_rule == ctx.merge_mod.MERGE_RULE_SUM
    assert [line.refund_amount for line in record.lines] == [
        Decimal("20.00"), Decimal("5.00"), Decimal("5.00"),
    ]
    assert report.refund_delta == Decimal("0.00")


def test_f38d_refund_allocation_does_not_touch_gross(ctx):
    """独立性：只有退款脏（商品逐行正确）时只摊退款，商品金额一个字节都不许改。"""
    csv_bytes = _csv_refund(
        ("ORD-RONLY", "EXT-SKU-A", 1, "100.00", "30.00"),
        ("ORD-RONLY", "EXT-SKU-B", 1, "60.00", "30.00"),
        ("ORD-RONLY", "EXT-SKU-C", 1, "40.00", "30.00"),
    )
    record, report = _merge(ctx, csv_bytes)
    assert report.gross_rule == ctx.merge_mod.MERGE_RULE_SUM, "商品是 SUM 态，不该被摊"
    assert [line.gross_amount for line in record.lines] == [
        Decimal("100.00"), Decimal("60.00"), Decimal("40.00"),
    ]
    assert report.refund_rule == ctx.merge_mod.MERGE_RULE_ALLOCATED
    assert [line.refund_amount for line in record.lines] == [
        Decimal("10.00"), Decimal("10.00"), Decimal("10.00"),
    ]
    assert record.refund_amount == Decimal("30.00")


def test_f38e_refund_allocation_follows_quantity(ctx):
    """退款分摊同样按数量加权：qty 2/1/1 摊 90 → 45.00 / 22.50 / 22.50。"""
    csv_bytes = _csv_refund(
        ("ORD-RQTY", "EXT-SKU-A", 2, "200.00", "90.00"),
        ("ORD-RQTY", "EXT-SKU-B", 1, "200.00", "90.00"),
        ("ORD-RQTY", "EXT-SKU-C", 1, "200.00", "90.00"),
    )
    record, report = _merge(ctx, csv_bytes)
    assert [line.refund_amount for line in record.lines] == [
        Decimal("45.00"), Decimal("22.50"), Decimal("22.50"),
    ]
    assert sum(line.refund_amount for line in record.lines) == Decimal("90.00")
    assert report.refund_delta == Decimal("0.00")
