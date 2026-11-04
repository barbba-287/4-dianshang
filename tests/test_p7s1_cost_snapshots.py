"""P7-S1 成本快照（成本入口）测试：F-1..F-9 + 白名单未污染。

夹具模式照 tests/test_p6s1_csv_import.py（tmp_path 隔离库 + 会话登录）。
本片只验「成本事实能进来且幂等」，利润计算与锚定属 P7-S2/S3，此处不涉及。
"""
from __future__ import annotations

import importlib
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

COST_CSV = "商品编码,成本单价,生效时间\nSKU-A,12.5,2026-11-01\nSKU-B,8,2026-11-02\n"
COST_CSV_WITH_BAD_ROW = "sku_code,unit_cost\nSKU-A,12.5\nSKU-A,abc\n"


class Ctx:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'p7s1.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    import app.config as cfg
    import app.db as db_mod
    import app.main as main_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)

    from app.employee_auth import hash_password

    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="p7s1", name="P7-S1 成本测试")
        db.add(workspace)
        db.flush()
        admin = db_mod.UserAccount(login="admin", password_hash=hash_password("admin-pw"), display_name="管理员")
        viewer = db_mod.UserAccount(login="viewer", password_hash=hash_password("viewer-pw"), display_name="只读")
        db.add_all([admin, viewer])
        db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=admin.id, role="admin"))
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=viewer.id, role="readonly"))
        warehouse = db_mod.Warehouse(
            workspace_id=workspace.id, code="W-P7S1", name="P7-S1 仓", warehouse_type="own",
        )
        db.add(warehouse)
        db.flush()
        db.add(db_mod.WarehouseAccess(user_id=admin.id, warehouse_id=warehouse.id, status="active"))
        product = db_mod.Product(
            workspace_id=workspace.id, source="fixture", external_product_id="p7s1-p",
            title="P7-S1 商品", url="https://fixture.local/p7s1-p", current_price=Decimal("30.00"),
        )
        db.add(product)
        db.flush()
        sku_a = db_mod.ProductSku(workspace_id=workspace.id, product_id=product.id, sku_code="SKU-A")
        sku_b = db_mod.ProductSku(workspace_id=workspace.id, product_id=product.id, sku_code="SKU-B")
        db.add_all([sku_a, sku_b])
        db.flush()
        inbound = db_mod.InboundOrder(
            workspace_id=workspace.id, warehouse_id=warehouse.id,
            reference_no="IN-P7S1-1", status="expected",
        )
        db.add(inbound)
        db.flush()
        db.add_all([
            db_mod.InboundLine(
                workspace_id=workspace.id, inbound_order_id=inbound.id,
                sku_id=sku_a.id, expected_qty=10, received_qty=0, damaged_qty=0,
            ),
            db_mod.InboundLine(
                workspace_id=workspace.id, inbound_order_id=inbound.id,
                sku_id=sku_b.id, expected_qty=5, received_qty=0, damaged_qty=0,
            ),
        ])
        db.commit()
        ids = {
            "workspace_id": workspace.id, "warehouse_id": warehouse.id,
            "sku_a": sku_a.id, "sku_b": sku_b.id, "inbound_id": inbound.id,
        }

    clients: dict[str, TestClient] = {}
    for login, password in (("admin", "admin-pw"), ("viewer", "viewer-pw")):
        client = TestClient(main_mod.app)
        response = client.post(
            "/login", data={"login": login, "password": password, "next": "/"}, follow_redirects=False,
        )
        assert response.status_code == 303
        client.headers.update({"X-CSRF-Token": client.cookies.get("dianshang_csrf")})
        clients[login] = client

    yield Ctx(db_mod=db_mod, main_mod=main_mod, clients=clients, **ids)
    cfg.get_settings.cache_clear()


def _snapshots(ctx, sku_id: int | None = None):
    with ctx.db_mod.SessionLocal() as db:
        query = db.query(ctx.db_mod.CostSnapshot)
        if sku_id is not None:
            query = query.filter(ctx.db_mod.CostSnapshot.sku_id == sku_id)
        return query.order_by(ctx.db_mod.CostSnapshot.id).all()


def _receive(ctx, *, unit_a="12.50", unit_b="8.00", key="rcv-1"):
    lines = [
        {"sku_id": ctx.sku_a, "received_qty": 10, "damaged_qty": 1, "unit_cost": unit_a},
        {"sku_id": ctx.sku_b, "received_qty": 5, "damaged_qty": 0, "unit_cost": unit_b},
    ]
    if unit_a is None:
        lines[0].pop("unit_cost")
    if unit_b is None:
        lines[1].pop("unit_cost")
    return ctx.clients["admin"].post(
        f"/api/inbounds/{ctx.inbound_id}/receive",
        json={"lines": lines},
        headers={"Idempotency-Key": key},
    )


def _confirm(ctx):
    return ctx.clients["admin"].post(
        f"/api/inbounds/{ctx.inbound_id}/confirm", json={},
    )


# ---------------------------------------------------------------------------
# F-1 / F-2 / F-3 确认入库落快照
# ---------------------------------------------------------------------------


def test_f1_confirm_inbound_writes_snapshot(ctx):
    """F-1：确认入库按 SKU 落快照，生效时间 = 确认时间，来源 = inbound_confirm。"""
    assert _receive(ctx).status_code == 200
    assert _snapshots(ctx) == []

    response = _confirm(ctx)
    assert response.status_code == 200

    rows = _snapshots(ctx)
    assert len(rows) == 2
    by_sku = {row.sku_id: row for row in rows}
    assert by_sku[ctx.sku_a].unit_cost == Decimal("12.50")
    assert by_sku[ctx.sku_b].unit_cost == Decimal("8.00")
    with ctx.db_mod.SessionLocal() as db:
        order = db.get(ctx.db_mod.InboundOrder, ctx.inbound_id)
        confirmed_at = order.confirmed_at
    for row in rows:
        assert row.source == "inbound_confirm"
        assert row.currency == "CNY"
        assert row.effective_time == confirmed_at
        assert row.source_ref.startswith(f"inbound:{ctx.inbound_id}:line:")
        assert row.workspace_id == ctx.workspace_id


def test_f2_confirm_idempotent_keeps_single_snapshot(ctx):
    """F-2：重复确认（幂等重放）不产生第二行快照。"""
    assert _receive(ctx).status_code == 200
    assert _confirm(ctx).status_code == 200
    first = _snapshots(ctx)
    assert len(first) == 2

    assert _confirm(ctx).status_code == 200
    assert len(_snapshots(ctx)) == 2


def test_f3_confirm_without_unit_cost_skips_snapshot(ctx):
    """F-3：存量入库单没有单价 → 确认照常成功，不写快照、不报错。"""
    assert _receive(ctx, unit_a=None, unit_b=None).status_code == 200
    assert _confirm(ctx).status_code == 200

    assert _snapshots(ctx) == []
    with ctx.db_mod.SessionLocal() as db:
        order = db.get(ctx.db_mod.InboundOrder, ctx.inbound_id)
        assert order.status == "confirmed"
        assert order.confirmed_at is not None


def test_f3b_receive_replay_keeps_unit_cost(ctx):
    """F-3 附带：同 idempotency-key 重放上报，单价与明细不被清空。

    receive 是单向状态机（expected → received），不存在"二次改价"路径；
    这里守住的是幂等重放不产生副作用（P3 既有语义不被本片破坏）。
    """
    assert _receive(ctx).status_code == 200
    assert _receive(ctx).status_code == 200
    with ctx.db_mod.SessionLocal() as db:
        lines = db.query(ctx.db_mod.InboundLine).filter(
            ctx.db_mod.InboundLine.inbound_order_id == ctx.inbound_id
        ).order_by(ctx.db_mod.InboundLine.sku_id).all()
        prices = {line.sku_id: line.unit_cost for line in lines}
    assert prices[ctx.sku_a] == Decimal("12.50")
    assert prices[ctx.sku_b] == Decimal("8.00")


# ---------------------------------------------------------------------------
# F-4 / F-5 / F-6 / F-7 手工录入与 CSV 导入
# ---------------------------------------------------------------------------


def test_f4_manual_create_and_validation(ctx):
    """F-4：单条录入合法落库；未知 SKU 404；负单价 422。"""
    ok = ctx.clients["admin"].post(
        "/api/finance/cost-snapshots",
        json={"sku_code": "SKU-A", "unit_cost": "18.80", "note": "手工补录"},
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["sku_code"] == "SKU-A"
    assert item["unit_cost"] == "18.80"
    assert item["source"] == "manual"
    assert item["note"] == "手工补录"

    missing = ctx.clients["admin"].post(
        "/api/finance/cost-snapshots", json={"sku_code": "NOPE", "unit_cost": "1.00"},
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "SKU_NOT_FOUND"

    negative = ctx.clients["admin"].post(
        "/api/finance/cost-snapshots", json={"sku_code": "SKU-A", "unit_cost": "-1"},
    )
    assert negative.status_code == 422


def test_f5_cost_csv_import_with_chinese_headers(ctx):
    """F-5：中文列头自动映射，两行全部入库（复用 P6-S2 映射能力）。"""
    response = ctx.clients["admin"].post(
        "/api/finance/cost-snapshots/import",
        files={"file": ("costs.csv", COST_CSV.encode("utf-8"), "text/csv")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["imported"] == 2
    assert body["rejected_total"] == 0
    assert body["total"] == 2
    rows = _snapshots(ctx)
    assert {row.unit_cost for row in rows} == {Decimal("12.50"), Decimal("8.00")}
    assert all(row.source == "manual" for row in rows)


def test_f6_cost_csv_bad_row_isolated_not_silently_dropped(ctx):
    """F-6：静默丢行护栏——非法单价进 rejected，计数不变量成立（用户硬要求 2）。"""
    response = ctx.clients["admin"].post(
        "/api/finance/cost-snapshots/import",
        files={"file": ("costs.csv", COST_CSV_WITH_BAD_ROW.encode("utf-8"), "text/csv")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert body["imported"] == 1
    assert body["rejected_total"] == 1
    # 收尾不变量：进来的每一行都有明确归宿（入库 / 拒绝），没有第三态
    assert body["imported"] + body["rejected_total"] == body["total"]
    rejected = body["rejected_rows"][0]
    assert rejected["row_number"] == 2
    assert "成本单价" in rejected["reason_message"]
    assert rejected["raw_data"]["sku_code"] == "SKU-A"
    assert len(_snapshots(ctx)) == 1


def test_f7_cost_csv_reimport_is_idempotent(ctx):
    """F-7：同文件重传不膨胀（source_ref 唯一约束 + 先查后插）。"""
    payload = COST_CSV.encode("utf-8")
    first = ctx.clients["admin"].post(
        "/api/finance/cost-snapshots/import", files={"file": ("costs.csv", payload, "text/csv")},
    )
    assert first.status_code == 200
    assert first.json()["imported"] == 2

    second = ctx.clients["admin"].post(
        "/api/finance/cost-snapshots/import", files={"file": ("costs.csv", payload, "text/csv")},
    )
    assert second.status_code == 200
    assert second.json()["imported"] == 0
    assert second.json()["skipped"] == 2
    assert len(_snapshots(ctx)) == 2


def test_f7b_preview_missing_required_returns_200(ctx):
    """F-7 附带：缺必填列时预览返回 200 + missing（与 P6 预览同语义），入库才 422。"""
    preview = ctx.clients["admin"].post(
        "/api/finance/cost-snapshots/preview",
        files={"file": ("costs.csv", "商品编码\nSKU-A\n".encode("utf-8"), "text/csv")},
    )
    assert preview.status_code == 200
    assert "unit_cost" in preview.json()["missing_required_fields"]

    commit = ctx.clients["admin"].post(
        "/api/finance/cost-snapshots/import",
        files={"file": ("costs.csv", "商品编码\nSKU-A\n".encode("utf-8"), "text/csv")},
    )
    assert commit.status_code == 422
    assert commit.json()["detail"]["code"] == "COST_MISSING_FIELDS"


# ---------------------------------------------------------------------------
# F-8 权限
# ---------------------------------------------------------------------------


def test_f8_permissions(ctx):
    """F-8：只读角色可查（catalog.read），不可写（catalog.write）。"""
    write = ctx.clients["viewer"].post(
        "/api/finance/cost-snapshots", json={"sku_code": "SKU-A", "unit_cost": "9.90"},
    )
    assert write.status_code == 403

    read = ctx.clients["viewer"].get("/api/finance/cost-snapshots?sku_code=SKU-A")
    assert read.status_code == 200
    assert read.json()["total"] == 0

    ctx.clients["admin"].post(
        "/api/finance/cost-snapshots", json={"sku_code": "SKU-A", "unit_cost": "9.90"},
    )
    read_again = ctx.clients["viewer"].get("/api/finance/cost-snapshots?sku_code=SKU-A")
    assert read_again.json()["total"] == 1


# ---------------------------------------------------------------------------
# F-9 页面契约 + 白名单未污染
# ---------------------------------------------------------------------------


def test_f9_frontend_contracts(ctx):
    """F-9：实收页有单价输入与预估总成本节点；数据源页有成本导入项。"""
    from pathlib import Path

    templates = Path(ctx.main_mod.__file__).parent / "templates"
    warehouse = (templates / "warehouse.html").read_text(encoding="utf-8")
    sources = (templates / "sources.html").read_text(encoding="utf-8")

    assert "data-unit-cost" in warehouse
    assert "receive-total-cost" in warehouse
    assert "recalcCost" in warehouse
    # 空单价必须提交 null，不能是 0（0 = 免费成本，会污染成本库）
    assert "?null:Number" in warehouse
    assert '<option value="costs">成本快照</option>' in sources
    assert "/api/finance/cost-snapshots/preview" in sources


def test_f10_external_import_whitelist_unchanged(ctx):
    """F-10：外部导入端点仍拒绝 costs（COST_KIND 未进 IMPORT_KINDS = 零回归）。"""
    from app.imports import COST_KIND, IMPORT_KINDS

    assert COST_KIND not in IMPORT_KINDS
    response = ctx.clients["admin"].post(
        "/api/external/imports/preview",
        files={"file": ("costs.csv", COST_CSV.encode("utf-8"), "text/csv")},
        data={"platform": "taobao", "kind": "costs"},
    )
    assert response.status_code == 422
