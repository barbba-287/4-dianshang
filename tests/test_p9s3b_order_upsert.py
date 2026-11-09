# -*- coding: utf-8 -*-
"""P9-S3 阶段 B 测试：SAVEPOINT 跨批 MERGE 与 UPSERT 落地。

对应 `dev_task.p9s3-order-upsert.md` §4B（B-1 ~ B-5）与 §6（阶段 B：≥9 条）。

编号口径：

- **T-R69-1 ~ T-R69-6**（跨批合并 / 行序倒置对拍 / 单头重算 / stale 边界）
- **T-SP-1 ~ T-SP-2**（SAVEPOINT 单笔隔离与副作用清理）
- **T-UI-1 ~ T-UI-2**（前端开关的静态断言，不引入 Playwright）

红线（§7）在本文件里的落点：

- R-3：已锚定行原样继承五件套（T-R69-1 / T-R69-6 逐字段断言）；
- R-74：单头金额 == 库里全部行之和（T-R69-4）；
- 红线 1：id 生成规则不动（跨批补行不得产生新 id，T-R69-1 / T-R69-6 断言 id 集合）；
- 红线 2：`payload_hash` 不重算（T-R69-1 / T-R69-2 用 no_op 幂等反证）；
- 红线 11：`line_mode="full"` 分支行为零变化（T-R69-2）。
"""
from __future__ import annotations

import importlib
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

ROOT = Path(__file__).resolve().parents[1]

SALE_TIME = "2026-09-10T10:00:00Z"
UPDATED = "2026-09-10T10:01:00Z"
UPDATED2 = "2026-09-10T10:02:00Z"

HEADER = (
    "external_order_no,account_ref,store_ref,status,created_at,updated_at,"
    "external_sku,ordered_qty,gross_amount"
)


def _csv(*rows, account: str = "account-a", store: str = "store-a", status: str = "paid",
         updated: str = UPDATED, header: str = HEADER) -> bytes:
    """rows: (订单号, 外部SKU, 数量, 金额)。刻意不带 external_line_id 列——真实报表也没有。"""
    body = "".join(
        f"{order_no},{account},{store},{status},{SALE_TIME},{updated},{sku},{qty},{gross}\n"
        for order_no, sku, qty, gross in rows
    )
    return (header + "\n" + body).encode()


class Ctx:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    """HTTP 链路的夹具（沿用 tests/test_p9s3_order_upsert.py 的范式，租户键换 p9s3b）。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'p9s3b.db'}")
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
        workspace = db_mod.Workspace(tenant_key="p9s3b", name="P9-S3 阶段 B 测试")
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
            workspace_id=workspace.id, source="fixture", external_product_id="p9s3b-p",
            title="P9-S3 阶段 B 商品", url="https://fixture.local/p9s3b-p",
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
        # 两个外部账号：account-a 跑"分批导入"，account-b 跑"一次性导入"，
        # 两者互不干扰，用来做跨批合并 vs 一次性导入的**逐值对拍**。
        externals = {}
        for ref, store in (("account-a", "store-a"), ("account-b", "store-b")):
            external = ext_mod.ensure_external_account(
                db, workspace_id=workspace.id, platform="taobao",
                account_ref=ref, store_ref=store,
            )
            for code in ("A", "B", "C"):
                db.add(db_mod.ExternalProductMapping(
                    workspace_id=workspace.id, external_account_id=external.id,
                    external_sku=f"EXT-SKU-{code}", internal_sku_id=skus[code],
                    mapping_status="mapped",
                ))
            externals[ref] = external.id
        for code, cost in (("A", "10.00"), ("B", "20.00"), ("C", "30.00")):
            db.add(db_mod.CostSnapshot(
                workspace_id=workspace.id, sku_id=skus[code],
                unit_cost=Decimal(cost), effective_time=datetime(2026, 8, 1),
                source="manual", source_ref=f"p9s3b-{code}", currency="CNY",
            ))
        db.commit()
        ids = {"workspace_id": workspace.id, **skus, **externals}

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


def _records(ctx, content: bytes):
    """直接构造记录（绕开 HTTP，用于观察 stats 与"记录是否被改写"）。"""
    return ctx.load_orders(content.decode(), platform="taobao", source_mode="csv")


def _ingest(ctx, content: bytes, *, line_mode: str = "full"):
    """直调领域层（拿得到 stats：rejected_orders / skipped_lines / stale ...）。"""
    records = _records(ctx, content)
    with ctx.db_mod.SessionLocal() as db:
        return ctx.ext_mod.ingest_orders(
            db, records, workspace_id=ctx.workspace_id, line_mode=line_mode,
        )


def _orders(ctx, account_ref: str = "account-a"):
    with ctx.db_mod.SessionLocal() as db:
        return db.scalars(
            select(ctx.db_mod.ExternalOrder)
            .where(ctx.db_mod.ExternalOrder.workspace_id == ctx.workspace_id)
            .order_by(ctx.db_mod.ExternalOrder.id)
        ).all()


def _order(ctx, order_no: str, account_ref: str = "account-a"):
    with ctx.db_mod.SessionLocal() as db:
        return db.scalar(
            select(ctx.db_mod.ExternalOrder).where(
                ctx.db_mod.ExternalOrder.workspace_id == ctx.workspace_id,
                ctx.db_mod.ExternalOrder.account_ref == account_ref,
                ctx.db_mod.ExternalOrder.external_order_no == order_no,
            )
        )


def _order_lines(ctx, order_id: int):
    with ctx.db_mod.SessionLocal() as db:
        return db.scalars(
            select(ctx.db_mod.ExternalOrderLine)
            .where(ctx.db_mod.ExternalOrderLine.external_order_id == order_id)
            .order_by(ctx.db_mod.ExternalOrderLine.id)
        ).all()


def _lines(ctx, account_ref: str = "account-a"):
    order_ids = [o.id for o in _orders(ctx, account_ref)]
    if not order_ids:
        return []
    with ctx.db_mod.SessionLocal() as db:
        return db.scalars(
            select(ctx.db_mod.ExternalOrderLine)
            .where(ctx.db_mod.ExternalOrderLine.external_order_id.in_(order_ids))
            .order_by(ctx.db_mod.ExternalOrderLine.id)
        ).all()


def _line_facts(lines) -> dict:
    """行事实（**不含 anchored_at**：两批导入的时间戳天然不同）。"""
    return {
        line.external_line_id: (
            line.external_sku,
            line.ordered_qty,
            str(line.gross_amount),
            str(line.refund_amount),
            line.cost_status,
            line.cost_snapshot_id,
            str(line.unit_cost),
            str(line.fee_rate),
            line.mapping_status,
        )
        for line in lines
    }


def _anchor_pieces(lines) -> dict:
    """锚定五件套（用于 R-3 的逐字段比对）。"""
    return {
        line.external_line_id: (
            line.cost_status,
            line.cost_snapshot_id,
            str(line.unit_cost),
            str(line.fee_rate),
            line.anchored_at,
        )
        for line in lines
    }


def _daily_sales(ctx, account_ref: str = "account-a") -> dict:
    with ctx.db_mod.SessionLocal() as db:
        rows = db.scalars(
            select(ctx.db_mod.DailySkuSale)
            .where(ctx.db_mod.DailySkuSale.account_ref == account_ref)
            .order_by(ctx.db_mod.DailySkuSale.external_sku)
        ).all()
        return {
            row.external_sku: (
                row.gross_qty, row.cancelled_qty, row.refunded_qty, row.net_qty,
                str(row.gross_amount), str(row.refund_amount), str(row.net_amount),
                row.order_count, row.data_completeness, str(row.sales_date),
            )
            for row in rows
        }


def _fail_trigger(db, sku: str = "EXT-SKU-B") -> None:
    """装一个必然失败的 SQLite 触发器：在 **flush 阶段**制造单笔冲突。

    这是 SAVEPOINT 能力的真实考验点：冲突发生在 flush（写库）而不是 Python 层，
    根事务必须活着，批次必须能继续。
    """
    db.execute(text(
        "CREATE TRIGGER p9s3b_fail BEFORE INSERT ON external_order_lines "
        f"WHEN NEW.external_sku = '{sku}' "
        "BEGIN SELECT RAISE(ABORT, 'p9s3b forced failure'); END"
    ))
    db.commit()


# ===========================================================================
# T-R69-1 ~ T-R69-6：跨批合并 / 行序倒置对拍 / 单头重算 / stale 边界
# ===========================================================================

def test_t_r69_1_incremental_adds_missing_row_and_keeps_anchor(ctx):
    """T-R69-1：跨批补行——第一批只有 A，第二批（incremental）只有 B → 订单变 2 行。

    同时锁三条红线：

    - R-3：A 的锚定五件套**逐字段不变**；
    - 红线 1：A 的 `external_line_id` 不变（没有产生第二个 id）；
    - 红线 2：整批重传仍然是 no_op（`payload_hash` 没被重算）。
    """
    first = _upload(ctx, _csv(("ORD-1", "EXT-SKU-A", 2, "40.00")))
    assert first.status_code == 200, first.text
    order = _order(ctx, "ORD-1")
    lines_before = _order_lines(ctx, order.id)
    assert len(lines_before) == 1
    anchor_before = _anchor_pieces(lines_before)
    ids_before = {line.external_line_id for line in lines_before}
    assert ids_before == {"csv:EXT-SKU-A|0"}
    assert anchor_before["csv:EXT-SKU-A|0"][1] is not None, "首行必须已锚定"

    second = _upload(
        ctx,
        _csv(("ORD-1", "EXT-SKU-B", 1, "30.00"), updated=UPDATED2),
        line_mode="incremental",
    )
    assert second.status_code == 200, second.text

    lines_after = _order_lines(ctx, _order(ctx, "ORD-1").id)
    assert len(lines_after) == 2, "跨批补行必须新增一行"
    ids_after = {line.external_line_id for line in lines_after}
    assert ids_after == {"csv:EXT-SKU-A|0", "csv:EXT-SKU-B|0"}, "A 的 id 不得变、不得重复"
    # R-3：A 的五件套一个字段都不许动
    anchor_after = _anchor_pieces(lines_after)
    assert anchor_after["csv:EXT-SKU-A|0"] == anchor_before["csv:EXT-SKU-A|0"]
    # B 是新行 → 必须走 as-of 锚定
    assert anchor_after["csv:EXT-SKU-B|0"][1] is not None
    assert anchor_after["csv:EXT-SKU-B|0"][0] == "resolved"

    # 红线 2：payload_hash 未被重算 → 整批重传仍然是 no_op（库里仍 2 行）
    third = _upload(
        ctx,
        _csv(("ORD-1", "EXT-SKU-B", 1, "30.00"), updated=UPDATED2),
        line_mode="incremental",
    )
    assert third.status_code == 200, third.text
    assert third.json()["no_op"] == 1, "同文件重传必须判 no_op（payload_hash 不得重算）"
    assert len(_order_lines(ctx, _order(ctx, "ORD-1").id)) == 2


def test_t_r69_2_full_mode_keeps_delete_and_rebuild(ctx):
    """T-R69-2：不传 line_mode（缺省 full）→ 仍然是删除-重建，本批没出现的 A 被删掉。

    这是红线 11 的行为守门：阶段 B 的改动**不得**改变 `full` 分支的语义。
    """
    first = _upload(ctx, _csv(("ORD-1", "EXT-SKU-A", 2, "40.00")))
    assert first.status_code == 200, first.text
    assert len(_lines(ctx)) == 1

    second = _upload(ctx, _csv(("ORD-1", "EXT-SKU-B", 1, "30.00"), updated=UPDATED2))
    assert second.status_code == 200, second.text
    lines = _lines(ctx)
    assert len(lines) == 1, "full 模式必须删除重建（本批没有的 A 应消失）"
    assert lines[0].external_sku == "EXT-SKU-B"


def test_t_r69_3_split_import_matches_one_shot_import(ctx):
    """T-R69-3（对拍①·跨批合并）：分两批 incremental 导入 == 一次性全量导入。

    对拍口径：行集合（id + 业务字段 + 锚定）、订单单头、日销表 —— **逐值相等**。
    account-a 走分批，account-b 走一次性，两边互不干扰。
    """
    first = _upload(ctx, _csv(("ORD-1", "EXT-SKU-A", 2, "40.00"), account="account-a"))
    assert first.status_code == 200, first.text
    second = _upload(
        ctx,
        _csv(("ORD-1", "EXT-SKU-B", 1, "30.00"), account="account-a", updated=UPDATED2),
        line_mode="incremental",
    )
    assert second.status_code == 200, second.text
    one_shot = _upload(
        ctx,
        _csv(
            ("ORD-1", "EXT-SKU-A", 2, "40.00"),
            ("ORD-1", "EXT-SKU-B", 1, "30.00"),
            account="account-b", store="store-b", updated=UPDATED2,
        ),
    )
    assert one_shot.status_code == 200, one_shot.text

    order_a = _order(ctx, "ORD-1", "account-a")
    order_b = _order(ctx, "ORD-1", "account-b")
    lines_a = _order_lines(ctx, order_a.id)
    lines_b = _order_lines(ctx, order_b.id)

    # ① 行集合逐值相等
    assert _line_facts(lines_a) == _line_facts(lines_b)
    assert set(_line_facts(lines_a)) == {"csv:EXT-SKU-A|0", "csv:EXT-SKU-B|0"}
    # ② 订单单头相等（金额口径也必须一致）
    assert order_a.gross_amount == order_b.gross_amount == Decimal("70.00")
    assert order_a.refund_amount == order_b.refund_amount == Decimal("0")
    assert order_a.order_status == order_b.order_status == "paid"
    assert order_a.external_updated_at == order_b.external_updated_at
    # ③ 日销表逐值相等
    sales_a = _daily_sales(ctx, "account-a")
    sales_b = _daily_sales(ctx, "account-b")
    assert sales_a == sales_b
    assert set(sales_a) == {"EXT-SKU-A", "EXT-SKU-B"}
    assert sales_a["EXT-SKU-A"][0] == 2 and sales_a["EXT-SKU-A"][4] == "40.00"
    assert sales_a["EXT-SKU-B"][0] == 1 and sales_a["EXT-SKU-B"][4] == "30.00"


def test_t_r69_4_header_equals_lines_sum_and_record_not_mutated(ctx):
    """T-R69-4（R-74）：单头金额 == 库里该订单**全部行**之和；传入的 record 单头不被改写。

    R-74 的原症状：incremental 只更新了本批的行，单头却还是"本批之和"，
    库里 40 + 30 = 70，单头停在 30 —— 报表对不上。
    """
    first = _upload(ctx, _csv(("ORD-1", "EXT-SKU-A", 2, "40.00")))
    assert first.status_code == 200, first.text
    order = _order(ctx, "ORD-1")
    assert order.gross_amount == Decimal("40.00")

    content = _csv(("ORD-1", "EXT-SKU-B", 1, "30.00"), updated=UPDATED2)
    records = _records(ctx, content)
    before = [(r.external_order_no, r.gross_amount, r.refund_amount, r.order_status) for r in records]
    with ctx.db_mod.SessionLocal() as db:
        ctx.ext_mod.ingest_orders(
            db, records, workspace_id=ctx.workspace_id, line_mode="incremental",
        )
    # 传入的 record（含单头）**不得被改写**
    after = [(r.external_order_no, r.gross_amount, r.refund_amount, r.order_status) for r in records]
    assert after == before
    assert records[0].gross_amount == Decimal("30.00"), "record 单头仍是它自己的 30.00"

    lines = _order_lines(ctx, _order(ctx, "ORD-1").id)
    line_sum = sum((line.gross_amount for line in lines), Decimal("0"))
    refund_sum = sum((line.refund_amount for line in lines), Decimal("0"))
    order = _order(ctx, "ORD-1")
    assert order.gross_amount == line_sum == Decimal("70.00"), "单头必须等于库里全部行之和"
    assert order.refund_amount == refund_sum


def test_t_r69_5_stale_batch_does_not_block_new_row(ctx):
    """T-R69-5：旧版本批次**不得**拦掉跨批补行，但旧版本的载荷也不得覆盖新版本。

    - 库里没有的行（EXT-SKU-B）→ 无条件入库（这是 D5 问题 2 的核心修复）；
    - 库里已有的行（EXT-SKU-A）→ 旧版本批次被跳过，载荷保持不变（两个方向都要守）。
    """
    first = _upload(ctx, _csv(("ORD-1", "EXT-SKU-A", 2, "40.00"), updated=UPDATED2))
    assert first.status_code == 200, first.text

    # 第二批时间戳**更旧**（UPDATED < UPDATED2），且带了一个新 SKU。
    stats = _ingest(
        ctx,
        _csv(
            ("ORD-1", "EXT-SKU-A", 9, "99.00"),
            ("ORD-1", "EXT-SKU-B", 1, "30.00"),
            updated=UPDATED,
        ),
        line_mode="incremental",
    )
    assert stats.stale == 0, "只要有一行生效就不算 stale"
    assert stats.skipped_lines == 1, "旧版本的 A 必须被跳过"
    assert stats.inserted_lines == 1, "新行 B 必须入库"

    lines = _order_lines(ctx, _order(ctx, "ORD-1").id)
    facts = _line_facts(lines)
    assert set(facts) == {"csv:EXT-SKU-A|0", "csv:EXT-SKU-B|0"}
    assert facts["csv:EXT-SKU-A|0"][1] == 2, "A 的数量不得被旧版本覆盖"
    assert facts["csv:EXT-SKU-A|0"][2] == "40.00", "A 的金额不得被旧版本覆盖"
    assert facts["csv:EXT-SKU-B|0"][2] == "30.00"


def test_t_r69_6_inverted_row_order_upsert_keeps_identity(ctx):
    """T-R69-6（对拍②·行序倒置）：第二批行序与第一批相反 → 命中原行，不得新增行。

    指纹 = (外部SKU, 该 SKU 在订单内的出现序号)，与行在文件里的位置无关；
    倒置重传必须命中同一批行（id 集合不变），并且已锚定的五件套一个字段都不动。
    """
    first = _upload(
        ctx,
        _csv(
            ("ORD-1", "EXT-SKU-A", 2, "40.00"),
            ("ORD-1", "EXT-SKU-B", 1, "30.00"),
            updated=UPDATED,
        ),
    )
    assert first.status_code == 200, first.text
    order_id = _order(ctx, "ORD-1").id
    lines_before = _order_lines(ctx, order_id)
    ids_before = {line.external_line_id for line in lines_before}
    assert ids_before == {"csv:EXT-SKU-A|0", "csv:EXT-SKU-B|0"}
    anchor_before = _anchor_pieces(lines_before)

    # 行序倒置 + 数量金额变化
    second = _upload(
        ctx,
        _csv(
            ("ORD-1", "EXT-SKU-B", 1, "35.00"),
            ("ORD-1", "EXT-SKU-A", 3, "45.00"),
            updated=UPDATED2,
        ),
        line_mode="incremental",
    )
    assert second.status_code == 200, second.text

    lines_after = _order_lines(ctx, order_id)
    assert len(lines_after) == 2, "倒置重传不得新增行"
    facts = _line_facts(lines_after)
    assert set(facts) == ids_before, "id 集合必须与第一批完全一致"
    # 命中原行 → 业务字段被就地更新
    assert facts["csv:EXT-SKU-A|0"][1] == 3
    assert facts["csv:EXT-SKU-A|0"][2] == "45.00"
    assert facts["csv:EXT-SKU-B|0"][2] == "35.00"
    # R-3：五件套逐字段不变
    assert _anchor_pieces(lines_after) == anchor_before


# ===========================================================================
# T-SP-1 ~ T-SP-2：SAVEPOINT 单笔隔离与副作用清理
# ===========================================================================

def test_t_sp_1_single_record_failure_does_not_poison_batch(ctx):
    """T-SP-1：一笔失败（flush 阶段 DB 冲突）→ 该笔被拒，其余笔照常入库，批次记 partial。

    关键断言：`sync_status == "partial"`（不是 succeeded），失败的 ORD-2 **没有任何残留**。
    """
    with ctx.db_mod.SessionLocal() as db:
        _fail_trigger(db, "EXT-SKU-B")
    resp = _upload(
        ctx,
        _csv(
            ("ORD-1", "EXT-SKU-A", 1, "10.00"),
            ("ORD-2", "EXT-SKU-B", 1, "20.00"),
            ("ORD-3", "EXT-SKU-C", 1, "30.00"),
        ),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert body["inserted"] == 2, "失败笔不得计入 inserted"
    assert body["sync_status"] == "partial", "有失败笔必须是 partial"
    # 失败笔零残留
    assert sorted(o.external_order_no for o in _orders(ctx)) == ["ORD-1", "ORD-3"]
    assert sorted(line.external_sku for line in _lines(ctx)) == ["EXT-SKU-A", "EXT-SKU-C"]


def test_t_sp_2_failed_record_side_effects_are_cleaned(ctx, monkeypatch):
    """T-SP-2：失败笔的共享状态（pending / touched / 计数）必须被清干净。

    §1.6 坑 1：SAVEPOINT 回滚会把该笔新建的 ORM 对象 expunge 成 transient，
    给它做锚定**不会报错**，只会静默丢失（成本"神秘丢失"）。
    所以 `pending` 里绝不能留下失败笔的行——这里直接盯住 `_anchor_pending_lines` 的入参。
    """
    with ctx.db_mod.SessionLocal() as db:
        _fail_trigger(db, "EXT-SKU-B")

    seen: dict = {}
    original = ctx.ext_mod._anchor_pending_lines

    def _spy(db, pending, *, workspace_id):
        seen["skus"] = [item[0].external_sku for item in pending]
        return original(db, pending, workspace_id=workspace_id)

    monkeypatch.setattr(ctx.ext_mod, "_anchor_pending_lines", _spy)

    stats = _ingest(
        ctx,
        _csv(
            ("ORD-1", "EXT-SKU-A", 1, "10.00"),
            ("ORD-2", "EXT-SKU-B", 1, "20.00"),
            ("ORD-3", "EXT-SKU-C", 1, "30.00"),
        ),
    )
    assert stats.rejected_orders == 1
    assert stats.rejected_reasons and stats.rejected_reasons[0]["external_order_no"] == "ORD-2"
    # pending 里不得有失败笔的行（否则会给已回滚的对象做锚定 → 成本静默丢失）
    assert "EXT-SKU-B" not in seen["skus"]
    assert "EXT-SKU-A" in seen["skus"] and "EXT-SKU-C" in seen["skus"]
    # DB 零残留
    assert sorted(o.external_order_no for o in _orders(ctx)) == ["ORD-1", "ORD-3"]
    assert sorted(line.external_sku for line in _lines(ctx)) == ["EXT-SKU-A", "EXT-SKU-C"]
    # 失败笔的 SKU 不得出现在日销表里（touched 被回退 → 不触发重建）
    sales = _daily_sales(ctx, "account-a")
    assert "EXT-SKU-B" not in sales
    assert sales["EXT-SKU-A"][4] == "10.00"
    assert sales["EXT-SKU-C"][4] == "30.00"


# ===========================================================================
# T-UI-1 ~ T-UI-2：前端开关（静态断言，不引入浏览器）
# ===========================================================================

def _template_html() -> str:
    return (ROOT / "app" / "templates" / "sources.html").read_text(encoding="utf-8")


def test_t_ui_1_incremental_switch_exists_and_only_sent_on_commit(ctx):
    """T-UI-1：增量开关存在，且**只在提交时**才把 line_mode 传后端（预览不传）。"""
    html = _template_html()
    assert 'id="import-incremental"' in html
    assert 'id="import-incremental-hint"' in html
    # 复用既有 .import-skip 样式（零新增 CSS）
    assert re.search(
        r'<label class="import-skip"><input type="checkbox" id="import-incremental">', html
    )
    # 只在提交（path 不是 /preview）且勾选时才传 line_mode
    assert re.search(
        r"if\(\s*!path\.endsWith\('/preview'\)\s*&&\s*\$\('#import-incremental'\)\.checked\s*\)"
        r"\s*form\.append\('line_mode','incremental'\)",
        html,
    )
    # 既有「跳过异常行」的行为与文案一个字都没动
    assert "if($('#import-allow-skipped').checked)form.append('allow_skipped','true');" in html
    assert 'id="import-skip-hint"' in html


def test_t_ui_2_non_order_kind_disables_incremental(ctx):
    """T-UI-2：非订单 kind 必须禁用增量开关（并强制取消勾选、隐藏提示）。"""
    html = _template_html()
    assert "function syncIncrementalState()" in html
    assert "const orders=$('#import-kind').value==='orders';" in html
    assert "box.disabled=!orders;" in html
    assert "if(!orders)box.checked=false;" in html
    assert "hint.hidden=!(orders&&box.checked);" in html
    # kind 变化与开关变化都要重新同步
    assert "$('#import-kind').onchange=()=>{syncIncrementalState();};" in html
    assert "$('#import-incremental').onchange=()=>{syncIncrementalState();};" in html
    assert re.search(r"^syncIncrementalState\(\);", html, re.MULTILINE)
