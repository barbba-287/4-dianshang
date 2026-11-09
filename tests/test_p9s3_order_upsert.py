# -*- coding: utf-8 -*-
"""P9-S3 阶段 A 测试：行指纹 + R-68 修复 + R-73 + line_mode 预埋。

对应 `dev_task.p9s3-order-upsert.md` §6（阶段 A：11 条集成 + 4 条纯函数）。

编号口径：

- **4 条纯函数单测**（A-2）：`test_a2_*`
- **11 条集成用例**：
  - `test_t_r68_1` .. `test_t_r68_6`（行序倒置 / 存量兼容 / 同 SKU 多行 / 平台真实 id）
  - `test_t_probe_1`（组 1 对拍 + 组 3 不变量）、`test_t_probe_2`（组 2 对拍）
  - `test_t_r73_main_py_css_variable_coverage`（R-73 + 反证）
  - `test_t_lm_1_incremental_rejected`、`test_t_lm_2_invalid_value_and_non_order_kind`

红线（§7）在本文件里的落点：

- R-3：已锚定行必须原样继承（T-R68-1 断言 `anchored_at` 逐字段相等）；
- R-2：整批仍只发 1 条成本快照查询（由既有 `test_f34b_snapshot_query_still_one` 守）；
- 红线 3：`source_mode != "csv"` 的记录零行为变化（T-PROBE-1 的 mock 场景）；
- 红线 4：R-65 的 422 不变（T-PROBE-1 的组 3 场景）；
- 红线 6：接口变更仅限 `line_mode`（T-LM-1 / T-LM-2）。
"""
from __future__ import annotations

import importlib
import importlib.util
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

ROOT = Path(__file__).resolve().parents[1]

# 探针基建（下划线开头，pytest 不收集；这里显式按路径加载）
_probe_spec = importlib.util.spec_from_file_location(
    "p9s3_probe", ROOT / "tests" / "_p9s3_probe.py"
)
probe = importlib.util.module_from_spec(_probe_spec)
_probe_spec.loader.exec_module(probe)

SALE_TIME = "2026-09-10T10:00:00Z"
UPDATED = "2026-09-10T10:01:00Z"
UPDATED2 = "2026-09-10T10:02:00Z"

HEADER = (
    "external_order_no,account_ref,store_ref,status,created_at,updated_at,"
    "external_sku,ordered_qty,gross_amount"
)


def _csv(*rows, status: str = "paid", updated: str = UPDATED, header: str = HEADER) -> bytes:
    """rows: (订单号, 外部SKU, 数量, 金额)。刻意不带 external_line_id 列——真实报表也没有。"""
    body = "".join(
        f"{order_no},account-a,store-a,{status},{SALE_TIME},{updated},{sku},{qty},{gross}\n"
        for order_no, sku, qty, gross in rows
    )
    return (header + "\n" + body).encode()


class Ctx:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    """HTTP 链路的夹具（沿用 tests/test_p8s1_order_merge.py 的范式）。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'p9s3a.db'}")
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
        workspace = db_mod.Workspace(tenant_key="p9s3a", name="P9-S3 阶段 A 测试")
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
            workspace_id=workspace.id, source="fixture", external_product_id="p9s3a-p",
            title="P9-S3 商品", url="https://fixture.local/p9s3a-p",
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
        for code, cost in (("A", "10.00"), ("B", "20.00"), ("C", "30.00")):
            db.add(db_mod.CostSnapshot(
                workspace_id=workspace.id, sku_id=skus[code],
                unit_cost=Decimal(cost), effective_time=datetime(2026, 8, 1),
                source="manual", source_ref=f"p9s3a-{code}", currency="CNY",
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

def _upload(ctx, content: bytes, *, path="/api/external/imports", platform="taobao",
            kind="orders", **extra):
    data = {"platform": platform, "kind": kind}
    data.update(extra)
    return ctx.client.post(path, files={"file": ("orders.csv", content, "text/csv")}, data=data)


def _lines(ctx):
    with ctx.db_mod.SessionLocal() as db:
        return db.scalars(
            select(ctx.db_mod.ExternalOrderLine).order_by(ctx.db_mod.ExternalOrderLine.id)
        ).all()


def _five_pieces(ctx):
    """{外部SKU: (cost_status, cost_snapshot_id, unit_cost, fee_rate, anchored_at)}"""
    out = {}
    for line in _lines(ctx):
        out[line.external_sku] = (
            line.cost_status,
            line.cost_snapshot_id,
            line.unit_cost,
            line.fee_rate,
            line.anchored_at,
        )
    return out


def _profit(ctx):
    """利润中心摘要（直接调领域层，避免端点窗口口径干扰）。"""
    from app.finance import summarize_profit

    with ctx.db_mod.SessionLocal() as db:
        return summarize_profit(
            db,
            workspace_id=ctx.workspace_id,
            window_from=date(2026, 9, 1),
            window_to=date(2026, 9, 30),
        )


ABC_CSV = _csv(
    ("ORD-ABC", "EXT-SKU-A", 2, "40.00"),
    ("ORD-ABC", "EXT-SKU-B", 1, "30.00"),
    ("ORD-ABC", "EXT-SKU-C", 1, "20.00"),
)
CBA_CSV = _csv(
    ("ORD-ABC", "EXT-SKU-C", 1, "20.00"),
    ("ORD-ABC", "EXT-SKU-A", 2, "40.00"),
    ("ORD-ABC", "EXT-SKU-B", 1, "30.00"),
    updated=UPDATED2,
)


# ===========================================================================
# A-2 纯函数单测（4 条）
# ===========================================================================

def test_a2_sku_token_length_100_and_101():
    """长度 100 的 SKU 原样保留；长度 101 走 sha1 分支。"""
    from app.order_line_identity import sku_token

    sku_100 = "S" * 100
    sku_101 = "S" * 101
    assert sku_token(sku_100) == sku_100
    token_101 = sku_token(sku_101)
    assert token_101 != sku_101
    assert len(token_101) == 16
    assert token_101 == sku_token(sku_101), "hash 分支必须确定性"


def test_a2_sku_token_with_pipe_goes_hash():
    """SKU 含 `|` 时必须走 hash 分支（原样保留会破坏 csv:{token}|{seq} 的解析）。"""
    from app.order_line_identity import line_fingerprint, sku_token

    sku = "A|B"
    token = sku_token(sku)
    assert token != sku
    assert "|" not in token
    fingerprint = line_fingerprint(sku, 0)
    assert fingerprint.startswith("csv:")
    assert fingerprint.count("|") == 1
    assert fingerprint.split("|")[1] == "0"


def test_a2_normalize_csv_line_ids_same_sku_multiple_rows():
    """同订单 [A, A, B] → csv:A|0 / csv:A|1 / csv:B|0。"""
    from dataclasses import replace

    from app.connectors import ExternalOrderLineRecord, ExternalOrderRecord
    from app.order_line_identity import normalize_csv_line_ids

    def _line(sku, index):
        return ExternalOrderLineRecord(
            external_line_id="line:%d" % index,
            external_sku=sku,
            ordered_qty=1,
            cancelled_qty=0,
            refunded_qty=0,
            gross_amount=Decimal("10.00"),
            refund_amount=Decimal("0"),
            currency="CNY",
            payload_hash="hash-%d" % index,
        )

    record = ExternalOrderRecord(
        schema_version="1", platform="taobao", account_ref="account-a", store_ref="store-a",
        external_order_no="ORD-1", order_status="paid",
        external_created_at=datetime(2026, 9, 10, 10, 0),
        paid_at=None, external_updated_at=datetime(2026, 9, 10, 10, 1),
        event_version=None, gross_amount=Decimal("30.00"), refund_amount=Decimal("0"),
        currency="CNY", lines=(_line("A", 0), _line("A", 1), _line("B", 2)),
        payload_hash="record-hash", idempotency_key="idem-1", raw_ref="csv://x",
        source_mode="csv", simulated=True, data_completeness="complete", status_reason=None,
    )
    normalized = normalize_csv_line_ids([record])
    ids = [line.external_line_id for line in normalized[0].lines]
    assert ids == ["csv:A|0", "csv:A|1", "csv:B|0"]
    # 入参不可被就地修改（frozen dataclass）
    assert [line.external_line_id for line in record.lines] == ["line:0", "line:1", "line:2"]
    # 非 csv 记录零变化（红线 3）
    json_record = replace(record, source_mode="json")
    assert normalize_csv_line_ids([json_record])[0] is json_record


def test_a2_line_fingerprint_shorter_than_128():
    """指纹 id 必须短于 128（db.py:835 的 String(128)）。"""
    from app.order_line_identity import line_fingerprint

    assert len(line_fingerprint("S" * 100, 0)) < 128
    assert len(line_fingerprint("S" * 5000, 12345)) < 128
    assert len(line_fingerprint("A|B", 0)) < 128


# ===========================================================================
# T-R68-1 .. T-R68-6（6 条集成）
# ===========================================================================

def test_t_r68_1_inverted_rows_keep_anchoring_five_pieces(ctx):
    """T-R68-1：行序倒置重传后，锚定五件套逐字段等于第一次。"""
    first = ctx.client.post(
        "/api/external/imports",
        files={"file": ("orders.csv", ABC_CSV, "text/csv")},
        data={"platform": "taobao", "kind": "orders"},
    )
    assert first.status_code == 200, first.text
    before = _five_pieces(ctx)
    assert set(before) == {"EXT-SKU-A", "EXT-SKU-B", "EXT-SKU-C"}
    assert before["EXT-SKU-A"][1] is not None
    assert Decimal(str(before["EXT-SKU-A"][2])) == Decimal("10.00")

    second = ctx.client.post(
        "/api/external/imports",
        files={"file": ("orders.csv", CBA_CSV, "text/csv")},
        data={"platform": "taobao", "kind": "orders"},
    )
    assert second.status_code == 200, second.text
    after = _five_pieces(ctx)

    # ★ R-68 的核心：anchored_at 不得被刷新，五件套逐字段不变。
    for sku, pieces in before.items():
        assert after[sku] == pieces, "SKU %s 的五件套被改写：%r -> %r" % (sku, pieces, after[sku])


def test_t_r68_2_profit_unchanged_after_inverted_upload(ctx):
    """T-R68-2：行序倒置后利润中心一分钱不变。"""
    assert ctx.client.post(
        "/api/external/imports",
        files={"file": ("orders.csv", ABC_CSV, "text/csv")},
        data={"platform": "taobao", "kind": "orders"},
    ).status_code == 200
    before = _profit(ctx)
    assert before["by_currency"], "利润中心应当算出至少一个币种"
    assert Decimal(str(before["by_currency"][0]["profit"])) > 0, "利润不应为 0（否则断言平凡）"

    assert ctx.client.post(
        "/api/external/imports",
        files={"file": ("orders.csv", CBA_CSV, "text/csv")},
        data={"platform": "taobao", "kind": "orders"},
    ).status_code == 200
    after = _profit(ctx)

    assert after["by_currency"][0]["profit"] == before["by_currency"][0]["profit"]
    assert after["by_currency"][0]["cost"] == before["by_currency"][0]["cost"]
    assert after["total_profit"] == before["total_profit"]


def test_t_r68_3_line_ids_stable_after_inverted_upload(ctx):
    """T-R68-3：倒置重传后的 external_line_id 集合 == 第一次（D2 的新规则）。"""
    ctx.client.post(
        "/api/external/imports",
        files={"file": ("orders.csv", ABC_CSV, "text/csv")},
        data={"platform": "taobao", "kind": "orders"},
    )
    first_ids = sorted(line.external_line_id for line in _lines(ctx))
    assert first_ids == ["csv:EXT-SKU-A|0", "csv:EXT-SKU-B|0", "csv:EXT-SKU-C|0"]

    ctx.client.post(
        "/api/external/imports",
        files={"file": ("orders.csv", CBA_CSV, "text/csv")},
        data={"platform": "taobao", "kind": "orders"},
    )
    second_ids = sorted(line.external_line_id for line in _lines(ctx))
    assert second_ids == first_ids


def test_t_r68_4_legacy_ids_still_inherit_anchoring(ctx):
    """T-R68-4：存量数据（库里 id 还是 line:N）重传后锚定仍被继承（双索引兜底）。"""
    ctx.client.post(
        "/api/external/imports",
        files={"file": ("orders.csv", ABC_CSV, "text/csv")},
        data={"platform": "taobao", "kind": "orders"},
    )
    before = _five_pieces(ctx)

    # 手工把 id 改回旧规则（模拟存量数据）
    with ctx.db_mod.SessionLocal() as db:
        for index, sku in enumerate(("EXT-SKU-A", "EXT-SKU-B", "EXT-SKU-C")):
            db.execute(
                update(ctx.db_mod.ExternalOrderLine)
                .where(ctx.db_mod.ExternalOrderLine.external_sku == sku)
                .values(external_line_id="line:%d" % index)
            )
        db.commit()
    assert sorted(line.external_line_id for line in _lines(ctx)) == ["line:0", "line:1", "line:2"]

    assert ctx.client.post(
        "/api/external/imports",
        files={"file": ("orders.csv", CBA_CSV, "text/csv")},
        data={"platform": "taobao", "kind": "orders"},
    ).status_code == 200

    after = _five_pieces(ctx)
    for sku, pieces in before.items():
        assert after[sku] == pieces, "SKU %s 的五件套在存量兼容路径上丢了：%r -> %r" % (
            sku, pieces, after[sku])
    # 旧 id 行被重建后应换成新规则 id
    assert sorted(line.external_line_id for line in _lines(ctx)) == [
        "csv:EXT-SKU-A|0", "csv:EXT-SKU-B|0", "csv:EXT-SKU-C|0",
    ]


def test_t_r68_5_same_sku_multiple_rows_no_unique_conflict(ctx):
    """T-R68-5：[A, A, B] 入库成功，无 IntegrityError，id = csv:A|0 / csv:A|1 / csv:B|0。"""
    payload = _csv(
        ("ORD-AAB", "EXT-SKU-A", 1, "10.00"),
        ("ORD-AAB", "EXT-SKU-A", 2, "20.00"),
        ("ORD-AAB", "EXT-SKU-B", 1, "30.00"),
    )
    response = ctx.client.post(
        "/api/external/imports",
        files={"file": ("orders.csv", payload, "text/csv")},
        data={"platform": "taobao", "kind": "orders"},
    )
    assert response.status_code == 200, response.text
    lines = _lines(ctx)
    assert len(lines) == 3
    assert sorted(line.external_line_id for line in lines) == [
        "csv:EXT-SKU-A|0", "csv:EXT-SKU-A|1", "csv:EXT-SKU-B|0",
    ]


def test_t_r68_6_platform_real_line_id_untouched(ctx):
    """T-R68-6：平台真实 id（"99001"）原样保留，不被改写成指纹 id。"""
    from app.connectors import normalize_order_row

    record = normalize_order_row(
        {
            "account_ref": "account-a",
            "store_ref": "store-a",
            "external_order_no": "ORD-REAL-ID",
            "external_created_at": SALE_TIME,
            "external_updated_at": UPDATED,
            "order_status": "paid",
            "gross_amount": "10.00",
            "lines": [{
                "external_line_id": "99001",
                "external_sku": "EXT-SKU-A",
                "ordered_qty": 1,
                "gross_amount": "10.00",
            }],
        },
        platform="taobao",
        source_mode="csv",
        index=0,
    )
    assert record.lines[0].external_line_id == "99001"

    with ctx.db_mod.SessionLocal() as db:
        ctx.ext_mod.ingest_orders(db, [record], workspace_id=ctx.workspace_id)
    lines = _lines(ctx)
    assert [line.external_line_id for line in lines] == ["99001"]
    assert lines[0].external_sku == "EXT-SKU-A"


# ===========================================================================
# T-PROBE-1 / T-PROBE-2（对拍）
# ===========================================================================

PROBE_SKUS = ("A", "B", "C")
PROBE_COSTS = {
    "A": ("10.00", datetime(2026, 8, 1)),
    "B": ("20.00", datetime(2026, 8, 1)),
    "C": ("30.00", datetime(2026, 8, 1)),
}


def _probe_pair(tmp_path, name):
    """建一对**完全相同**的独立库（legacy / new）。"""
    legacy = probe.create_probe_db(
        tmp_path / ("%s_legacy.db" % name), tenant_key="p9s3-%s-legacy" % name,
        skus=PROBE_SKUS, costs=PROBE_COSTS)
    new = probe.create_probe_db(
        tmp_path / ("%s_new.db" % name), tenant_key="p9s3-%s-new" % name,
        skus=PROBE_SKUS, costs=PROBE_COSTS)
    return legacy, new


def _probe_records(ctx, content: bytes):
    return ctx.load_orders(content.decode(), platform="taobao", source_mode="csv")


def test_t_probe_1_group1_identical_and_group3_unchanged(ctx, tmp_path):
    """T-PROBE-1：组 1（逐字段一致）+ 组 3（R-65 422 / payload_hash 语义）必须完全不变。"""
    scenarios = {
        # 组 1：单订单单行
        "single": _csv(("ORD-1", "EXT-SKU-A", 1, "10.00")),
        # 组 1：多 SKU 聚合
        "multi": ABC_CSV,
        # 组 1：金额三态 / 退款分摊（多单多形态）
        "amounts": _csv(
            ("ORD-SAME", "EXT-SKU-A", 2, "40.00"),
            ("ORD-SINGLE", "EXT-SKU-B", 1, "20.00"),
            ("ORD-SUM", "EXT-SKU-C", 1, "30.00"),
        ),
    }
    for name, content in scenarios.items():
        legacy_pack, new_pack = _probe_pair(tmp_path, name)
        legacy_sf, new_sf = legacy_pack[1], new_pack[1]
        records = _probe_records(ctx, content)

        legacy_stats, legacy_db = probe.run_ingest(
            legacy_sf, records, workspace_id=legacy_pack[2]["workspace_id"], legacy=True)
        legacy_snap = probe.snapshot(legacy_db)
        legacy_db.close()

        new_stats, new_db = probe.run_ingest(
            new_sf, records, workspace_id=new_pack[2]["workspace_id"], legacy=False)
        new_snap = probe.snapshot(new_db)
        new_db.close()

        diffs = probe.compare(legacy_snap, new_snap)
        print("【组 1】场景 %-8s → 快照差异 %d 条；导入统计一致=%s；payload_hash 逐条一致=%s" % (
            name, len(diffs),
            probe.stats_snapshot(legacy_stats) == probe.stats_snapshot(new_stats),
            {row["order_no"]: row["payload_hash"] for row in legacy_snap["orders"]}
            == {row["order_no"]: row["payload_hash"] for row in new_snap["orders"]},
        ))
        assert not diffs, "组 1 场景 %s 的 legacy/new 快照不一致：%s" % (name, diffs)
        assert probe.stats_snapshot(legacy_stats) == probe.stats_snapshot(new_stats), (
            "组 1 场景 %s 的导入统计不一致" % name)

        # 组 3：payload_hash 语义不变（改 id 规则不许让存量重传从 no_op 变 updated）
        legacy_hashes = {row["order_no"]: row["payload_hash"] for row in legacy_snap["orders"]}
        new_hashes = {row["order_no"]: row["payload_hash"] for row in new_snap["orders"]}
        assert legacy_hashes == new_hashes

    # 组 1：同文件重传必须 no_op（两侧都是）
    legacy_pack, new_pack = _probe_pair(tmp_path, "noop")
    records = _probe_records(ctx, ABC_CSV)
    for legacy, pack in ((True, legacy_pack), (False, new_pack)):
        stats, db = probe.run_ingest(
            pack[1], records, workspace_id=pack[2]["workspace_id"], legacy=legacy)
        db.close()
        assert stats.no_op == 0
        stats2, db = probe.run_ingest(
            pack[1], records, workspace_id=pack[2]["workspace_id"], legacy=legacy)
        snap = probe.snapshot(db)
        db.close()
        assert stats2.no_op == 1, "同文件重传必须是 no_op（legacy=%s）" % legacy
        assert len(snap["lines"]) == 3
    print("【组 1】场景 noop     → 两侧同文件重传均为 no_op=1，行数均为 3 [OK]")

    # 组 1：非 csv 路径（mock / shopify 风格）零行为变化（红线 3）
    legacy_pack, new_pack = _probe_pair(tmp_path, "mock")
    json_payload = json.dumps([{
        "account_ref": "account-a",
        "store_ref": "store-a",
        "external_order_no": "ORD-MOCK",
        "external_created_at": SALE_TIME,
        "external_updated_at": UPDATED,
        "order_status": "paid",
        "gross_amount": "10.00",
        "lines": [{
            "external_line_id": "L-1",
            "external_sku": "EXT-SKU-A",
            "ordered_qty": 1,
            "gross_amount": "10.00",
        }],
    }])
    mock_records = ctx.load_orders(json_payload, platform="taobao", source_mode="mock")
    legacy_snap = None
    new_snap = None
    for legacy, pack, target in ((True, legacy_pack, "legacy"), (False, new_pack, "new")):
        stats, db = probe.run_ingest(
            pack[1], mock_records, workspace_id=pack[2]["workspace_id"], legacy=legacy)
        snap = probe.snapshot(db)
        db.close()
        if legacy:
            legacy_snap = snap
        else:
            new_snap = snap
    diffs = probe.compare(legacy_snap, new_snap)
    assert not diffs, "非 csv 路径的 legacy/new 快照不一致：%s" % diffs
    assert [row["raw_line_id"] for row in new_snap["lines"]] == ["L-1"], "平台真实 id 不得被改写"
    print("【组 1】场景 mock     → 快照差异 0 条；平台真实 id 两侧均为 L-1（未被改写）[OK]")

    # 组 3：R-65 的 422 不变（整批拒绝、错误码与文案不动）
    conflict_csv = (
        HEADER + "\n"
        + f"ORD-CONFLICT,account-a,store-a,paid,{SALE_TIME},{UPDATED},EXT-SKU-A,1,10.00\n"
        + f"ORD-CONFLICT,account-a,store-a,cancelled,{SALE_TIME},{UPDATED},EXT-SKU-B,1,20.00\n"
    ).encode()
    legacy_pack, new_pack = _probe_pair(tmp_path, "r65")
    conflict_records = _probe_records(ctx, conflict_csv)
    codes = []
    for legacy, pack in ((True, legacy_pack), (False, new_pack)):
        with pytest.raises(Exception) as excinfo:
            probe.run_ingest(
                pack[1], conflict_records, workspace_id=pack[2]["workspace_id"], legacy=legacy)
        codes.append((type(excinfo.value).__name__, str(excinfo.value)))
    assert codes[0] == codes[1], "R-65 的错误类型/文案两侧必须一致：%r" % (codes,)
    assert codes[0][0] == "BatchDuplicateOrderError"
    print("【组 3】R-65 422：两侧异常类型/文案一致 → %s" % (codes[0],))

    # 组 3：HTTP 响应体键集不变
    response = ctx.client.post(
        "/api/external/imports",
        files={"file": ("orders.csv", ABC_CSV, "text/csv")},
        data={"platform": "taobao", "kind": "orders"},
    )
    assert response.status_code == 200, response.text
    assert set(response.json().keys()) == {
        "platform", "kind", "total", "inserted", "updated", "no_op", "conflict",
        "sync_run_id", "sync_status", "detected_encoding", "rejected_count",
    }
    print("【组 3】HTTP 响应体键集未变（%d 个键）[OK]" % len(response.json()))


def test_t_probe_2_group2_inverted_must_differ(ctx, tmp_path):
    """T-PROBE-2：行序倒置下 legacy 与 new **必须不同**，且 new 的五件套未被刷新。"""
    legacy_pack, new_pack = _probe_pair(tmp_path, "inverted")
    legacy_sf, new_sf = legacy_pack[1], new_pack[1]

    first_records = _probe_records(ctx, ABC_CSV)
    inverted_records = _probe_records(ctx, CBA_CSV)

    snaps = {}
    prev = {}
    for legacy, pack in ((True, legacy_pack), (False, new_pack)):
        stats, db = probe.run_ingest(
            pack[1], first_records, workspace_id=pack[2]["workspace_id"], legacy=legacy)
        snap1 = probe.snapshot(db)
        db.close()

        # 追加一条更贵的新成本快照：legacy 重新 as-of 会取到它，new 必须保留旧锚定。
        with pack[1]() as db:
            db.add(ctx.db_mod.CostSnapshot(
                workspace_id=pack[2]["workspace_id"],
                sku_id=pack[2]["sku_ids"]["A"],
                unit_cost=Decimal("12.00"),
                effective_time=datetime(2026, 9, 1),
                source="manual", source_ref="p9s3-inverted-A-new", currency="CNY",
            ))
            db.commit()

        stats2, db = probe.run_ingest(
            pack[1], inverted_records, workspace_id=pack[2]["workspace_id"], legacy=legacy)
        snap2 = probe.snapshot(db)
        db.close()
        snaps[legacy] = (snap1, snap2, stats2)

    legacy_snap1, legacy_snap2, legacy_stats = snaps[True]
    new_snap1, new_snap2, new_stats = snaps[False]

    diffs = probe.compare(legacy_snap2, new_snap2,
                          left_prev=legacy_snap1["lines"], right_prev=new_snap1["lines"])
    print("【组 2】行序倒置重传 → legacy/new 差异 %d 条（一致 = 修复未生效）" % len(diffs))
    assert diffs, "组 2 必须出现差异（一致 = 修复未生效）"

    # new 侧：五件套原样保留（anchored_at 未被刷新）
    new_rows = {row["line_key"]: row for row in new_snap2["lines"]}
    old_rows = {row["line_key"]: row for row in new_snap1["lines"]}
    assert set(new_rows) == set(old_rows)
    for key, row in new_rows.items():
        assert row["anchored_at"] == old_rows[key]["anchored_at"], (
            "new 侧的 anchored_at 被刷新了：%r -> %r" % (old_rows[key]["anchored_at"], row["anchored_at"]))
        assert row["cost_snapshot_id"] == old_rows[key]["cost_snapshot_id"]
        assert row["unit_cost"] == old_rows[key]["unit_cost"]
    print("【组 2】new 侧五件套：anchored_at / cost_snapshot_id / unit_cost 全部原样保留 [OK]")

    # legacy 侧：锚定确实丢了（重新 as-of 取到了新快照）——证明这确实是「修好的 bug」
    legacy_rows = {row["line_key"]: row for row in legacy_snap2["lines"]}
    legacy_old = {row["line_key"]: row for row in legacy_snap1["lines"]}
    changed = [
        key for key, row in legacy_rows.items()
        if row["unit_cost"] != legacy_old[key]["unit_cost"]
    ]
    print("【组 2】legacy 侧因行序倒置而重新 as-of 的行：%s" % changed)
    assert changed, "legacy 侧本应因行序倒置而重新 as-of（成本变化），实际没变"


# ===========================================================================
# T-R73（R-73 修复 + 反证）
# ===========================================================================

def _load_tokens_module():
    spec = importlib.util.spec_from_file_location(
        "p5s1_tokens", ROOT / "tests" / "test_p5s1_tokens.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_t_r73_main_py_css_variable_coverage():
    """T-R73：T-2 的扫描源已覆盖 app/main.py；并做「人为改错必变红」的反证。"""
    tokens = _load_tokens_module()
    assert tokens.APP_MAIN.exists(), "T-2 的 sources 必须包含 app/main.py"
    defined = set(tokens._defined_vars(tokens._read(tokens.TOKENS)))
    defined |= set(tokens._defined_vars(tokens._read(tokens.SHELL)))

    main_text = tokens._read(tokens.APP_MAIN)
    assert tokens._dangling_in(main_text, defined) == [], "main.py 存在悬空 CSS 变量引用"

    # 反证（任务书 §3 明确要求，不许只口头说）：
    # 人为把 var(--color-border) 改成 var(--color-bordr) 时，T-2 的判定必须报红。
    mutated = main_text.replace("var(--color-border)", "var(--color-bordr)")
    assert mutated != main_text, "main.py 里必须存在 var(--color-border)，否则反证不成立"
    dangling = tokens._dangling_in(mutated, defined)
    assert "--color-bordr" in dangling, "人为改错后 T-2 的判定没有报红：%s" % dangling
    assert "--color-border" not in dangling


# ===========================================================================
# T-LM-1 / T-LM-2（line_mode 护栏 → 阶段 B：真实现上线）
# ===========================================================================

def test_t_lm_1_incremental_effective_in_stage_b(ctx):
    """T-LM-1（**阶段 B 契约变迁**）：kind=orders 带 line_mode=incremental → 200 且**生效**。

    阶段 A 的断言是「422 LINE_MODE_NOT_ENABLED」（防"假开关"的临时护栏）。
    阶段 B 按任务书 B-5 落地了 incremental 的真实现，护栏已删除，
    断言必须改成「**生效**」——否则这条用例还在保护一个已经不存在的契约。

    生效的判据不是"只看 200"：增量模式下**本批未出现的旧行必须原样保留**。
    先全量导入 ORD-ABC 的三行，再增量导入只含 A 的文件 → 库里应仍是 3 行。
    """
    first = _upload(ctx, ABC_CSV)
    assert first.status_code == 200, first.text
    assert len(_lines(ctx)) == 3, _lines(ctx)

    only_a = (
        f"{HEADER}\n"
        f"ORD-ABC,account-a,store-a,paid,{SALE_TIME},{UPDATED2},EXT-SKU-A,2,40.00\n"
    ).encode()
    second = _upload(ctx, only_a, line_mode="incremental")
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["sync_status"] == "succeeded", second.text
    rows = _lines(ctx)
    assert len(rows) == 3, rows
    assert sorted(row.external_sku for row in rows) == ["EXT-SKU-A", "EXT-SKU-B", "EXT-SKU-C"]


def test_t_lm_2_invalid_value_and_non_order_kind(ctx):
    """T-LM-2：line_mode=xx → 422 LINE_MODE_INVALID；非订单 kind 忽略该参数。"""
    # 1) 非法值
    response = _upload(ctx, ABC_CSV, line_mode="xx")
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "LINE_MODE_INVALID", response.text
    assert _lines(ctx) == []

    # 2) 非订单 kind（inventory）：忽略 line_mode，正常入库不报错
    inventory_csv = (
        "external_sku,as_of,available_qty\n"
        f"SKU-A,{SALE_TIME},5\n"
    ).encode()
    response = _upload(ctx, inventory_csv, kind="inventory", line_mode="incremental",
                       account_ref="account-a", store_ref="store-a")
    assert response.status_code == 200, response.text

    # 3) 非订单 kind（costs，成本快照端点）：多余 form 字段被忽略，正常入库不报错
    cost_csv = (
        "sku_code,unit_cost,effective_time\n"
        f"SKU-A,9.90,{SALE_TIME}\n"
    ).encode()
    response = ctx.client.post(
        "/api/finance/cost-snapshots/import",
        files={"file": ("costs.csv", cost_csv, "text/csv")},
        data={"line_mode": "incremental"},
    )
    assert response.status_code == 200, response.text
