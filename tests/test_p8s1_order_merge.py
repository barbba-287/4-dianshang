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
    """多行取值唯一（含"订单总额复制到每行"）→ 取该值，delta != 0 必须留 warning。"""
    csv_bytes = _csv(
        ("ORD-SAME", "EXT-SKU-A", 1, "100.00"),
        ("ORD-SAME", "EXT-SKU-B", 1, "100.00"),
        ("ORD-SAME", "EXT-SKU-C", 1, "100.00"),
    )
    record, report = _merge(ctx, csv_bytes)
    assert report.gross_rule == ctx.merge_mod.MERGE_RULE_SAME
    assert record.gross_amount == Decimal("100.00")
    assert report.lines_gross_sum == Decimal("300.00")
    assert report.delta == Decimal("-200.00"), "R-67 的可观测信号：明细之和 != 主表"


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
    """CSV 没有 external_line_id 列 → 每行都是 line:0 → 合并必须重编号，否则撞唯一约束。"""
    response = _upload(ctx, THREE_SKU_CSV)
    assert response.status_code == 200, response.text
    lines = _lines(ctx)
    assert len(lines) == 3
    assert len({line.external_line_id for line in lines}) == 3, "组内 id 必须互不相同"
    assert all(line.external_line_id.startswith("line:") for line in lines)


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
