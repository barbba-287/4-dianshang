"""P4 经营驾驶舱契约测试（对应 dev_task.p4-dashboard.md §6 的 P-1..P-11）。

覆盖点：
- P-1  GMV / 订单数基础聚合 + ``SALE_ORDER_STATUSES`` 与 ``_is_sale`` 口径对拍
- P-2  窗口边界与环比（prev=0 → null，不返回 Infinity）
- P-3  稀疏日期不补 0
- P-4  多币种分组返回、绝不折算、``fx_todo`` 原文
- P-5  缺货 / 积压判定（口径 A：NO_SALES + SLOW_MOVING）
- P-6  采购 / 入库待办状态边界
- P-7  知识库健康只认已发布版本
- P-8  权限（403/200）与跨租户隔离
- P-9  页面渲染契约
- P-10 空工作区不炸
- P-11 图表空态防御契约（源码位置断言）

本文件只新增，不改动任何既有测试文件。
"""

import importlib
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
COCKPIT_TEMPLATE = REPO_ROOT / "app" / "templates" / "cockpit.html"


AS_OF = date(2026, 10, 31)
PASSWORD = "cockpit-password"


# ---------------------------------------------------------------------------
# 夹具：两套 workspace + 多角色账号
# ---------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cockpit.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")

    import app.config as cfg
    import app.db as db_mod
    import app.repository as repo_mod
    import app.background as bg_mod
    import app.business_dates as dates_mod
    import app.cockpit as cockpit_mod
    import app.main as main_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(repo_mod)
    importlib.reload(bg_mod)
    importlib.reload(dates_mod)
    importlib.reload(cockpit_mod)
    importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)

    from app.employee_auth import hash_password

    with db_mod.SessionLocal() as db:
        ws1 = db_mod.Workspace(tenant_key="cockpit-ws1", name="驾驶舱商家一号")
        ws2 = db_mod.Workspace(tenant_key="cockpit-ws2", name="驾驶舱商家二号")
        db.add_all([ws1, ws2])
        db.flush()
        accounts = {
            "admin": (ws1.id, "admin"),
            "operations": (ws1.id, "operations"),
            "customer_service": (ws1.id, "customer_service"),
            "readonly": (ws1.id, "readonly"),
            "empty_admin": (ws2.id, "admin"),
        }
        for login_name, (workspace_id, role) in accounts.items():
            user = db_mod.UserAccount(
                login=login_name,
                password_hash=hash_password(PASSWORD),
                display_name=f"驾驶舱测试-{login_name}",
            )
            db.add(user)
            db.flush()
            db.add(db_mod.WorkspaceMembership(workspace_id=workspace_id, user_id=user.id, role=role))
        db.commit()

    with TestClient(main_mod.app) as test_client:
        yield test_client

    bg_mod.reset_executor()
    cfg.get_settings.cache_clear()


def _login(client: TestClient, login_name: str) -> None:
    response = client.post(
        "/login",
        data={"login": login_name, "password": PASSWORD, "next": "/cockpit"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303), response.text


def _db():
    import app.db as db_mod

    return db_mod.SessionLocal()


# ---------------------------------------------------------------------------
# 数据构造助手
# ---------------------------------------------------------------------------
def _account(db, workspace_id: int, suffix: str = "a"):
    import app.db as db_mod

    account = db_mod.ExternalAccount(
        workspace_id=workspace_id,
        platform="mock",
        account_ref=f"cockpit-account-{suffix}",
        store_ref=f"cockpit-store-{suffix}",
    )
    db.add(account)
    db.flush()
    return account


def _order(
    db,
    *,
    workspace_id: int,
    account_id: int,
    order_no: str,
    status: str,
    gross: str,
    refund: str = "0",
    currency: str = "CNY",
    created_at: datetime | None = None,
    completeness: str = "complete",
):
    import app.db as db_mod

    created_at = created_at or datetime(2026, 10, 30, 12, 0, 0)
    order = db_mod.ExternalOrder(
        workspace_id=workspace_id,
        external_account_id=account_id,
        platform="mock",
        account_ref="cockpit-account-a",
        store_ref="cockpit-store-a",
        external_order_no=order_no,
        order_status=status,
        external_created_at=created_at,
        external_updated_at=created_at,
        gross_amount=Decimal(gross),
        refund_amount=Decimal(refund),
        currency=currency,
        payload_json="{}",
        payload_hash=f"hash-{order_no}",
        idempotency_key=f"key-{order_no}",
        source_mode="mock",
        simulated=True,
        data_completeness=completeness,
    )
    db.add(order)
    db.flush()
    return order


def _sku(db, *, workspace_id: int, sku_code: str):
    import app.db as db_mod

    product = db_mod.Product(
        workspace_id=workspace_id,
        source="fixture",
        external_product_id=f"prod-{sku_code}",
        title=f"商品 {sku_code}",
        url=f"https://fixture.local/{sku_code}",
        current_price=Decimal("10.00"),
    )
    db.add(product)
    db.flush()
    sku = db_mod.ProductSku(workspace_id=workspace_id, product_id=product.id, sku_code=sku_code)
    db.add(sku)
    db.flush()
    return sku


def _warehouse(db, *, workspace_id: int, code: str):
    import app.db as db_mod

    warehouse = db_mod.Warehouse(workspace_id=workspace_id, code=code, name=f"仓库 {code}")
    db.add(warehouse)
    db.flush()
    return warehouse


def _policy(db, *, workspace_id: int, warehouse_id: int, sku_id: int, safety: int, reorder: int):
    import app.db as db_mod

    policy = db_mod.InventoryPolicy(
        workspace_id=workspace_id,
        warehouse_id=warehouse_id,
        sku_id=sku_id,
        safety_stock_qty=safety,
        reorder_point_qty=reorder,
    )
    db.add(policy)
    db.flush()
    return policy


def _balance(db, *, workspace_id: int, warehouse_id: int, sku_id: int, on_hand: int):
    import app.db as db_mod

    balance = db_mod.InventoryBalance(
        workspace_id=workspace_id,
        warehouse_id=warehouse_id,
        sku_id=sku_id,
        on_hand_qty=on_hand,
    )
    db.add(balance)
    db.flush()
    return balance


def _daily_sale(
    db,
    *,
    workspace_id: int,
    account_id: int,
    sku_id: int,
    external_sku: str,
    sales_date: date,
    net_qty: int,
    completeness: str = "complete",
):
    import app.db as db_mod

    sale = db_mod.DailySkuSale(
        workspace_id=workspace_id,
        external_account_id=account_id,
        platform="mock",
        account_ref="cockpit-account-a",
        store_ref="cockpit-store-a",
        external_sku=external_sku,
        internal_sku_id=sku_id,
        sales_date=sales_date,
        gross_qty=net_qty,
        cancelled_qty=0,
        refunded_qty=0,
        net_qty=net_qty,
        gross_amount=Decimal("10.00") * net_qty,
        refund_amount=Decimal("0"),
        net_amount=Decimal("10.00") * net_qty,
        order_count=1,
        data_completeness=completeness,
    )
    db.add(sale)
    db.flush()
    return sale


def _purchase(db, *, workspace_id: int, warehouse_id: int, request_no: str, status: str):
    import app.db as db_mod

    request = db_mod.PurchaseRequest(
        workspace_id=workspace_id,
        warehouse_id=warehouse_id,
        request_no=request_no,
        status=status,
        submitted_by="tester",
        idempotency_key=f"pr-key-{request_no}",
        payload_hash=f"pr-hash-{request_no}",
    )
    db.add(request)
    db.flush()
    return request


def _inbound(db, *, workspace_id: int, warehouse_id: int, reference_no: str, status: str):
    import app.db as db_mod

    order = db_mod.InboundOrder(
        workspace_id=workspace_id,
        warehouse_id=warehouse_id,
        reference_no=reference_no,
        status=status,
    )
    db.add(order)
    db.flush()
    return order


def _cockpit(client: TestClient, **params):
    response = client.get("/api/dashboard/cockpit", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# P-1 GMV 与订单数基础聚合（含状态谓词对拍）
# ---------------------------------------------------------------------------
def test_p1_gmv_and_order_count_aggregate(client):
    with _db() as db:
        account = _account(db, 1)
        _order(db, workspace_id=1, account_id=account.id, order_no="P1-1", status="paid", gross="100.00")
        _order(
            db,
            workspace_id=1,
            account_id=account.id,
            order_no="P1-2",
            status="paid",
            gross="50.00",
            refund="20.00",
        )
        _order(db, workspace_id=1, account_id=account.id, order_no="P1-3", status="cancelled", gross="999.00")
        db.commit()

    _login(client, "admin")
    body = _cockpit(client, days=7, as_of=AS_OF.isoformat())

    # cancelled 不计；2 单 paid 净额之和 = 100 + (50 − 20) = 130
    assert Decimal(body["kpis"]["gmv"]["value"]) == Decimal("130.00")
    assert body["kpis"]["orders"]["value"] == 2
    assert body["kpis"]["gmv"]["unit"] == "CNY"
    assert body["by_currency"] == [{"currency": "CNY", "gmv": "130.00", "order_count": 2}]

    # 状态谓词对拍：cockpit.SALE_ORDER_STATUSES 必须与 external_orders._is_sale 完全一致
    from app.external_orders import _is_sale
    from app.cockpit import SALE_ORDER_STATUSES

    probe = type("Probe", (), {"order_status": None})()
    statuses = ["paid", "fulfilled", "completed", "cancelled", "pending", "refunded", "created", "shipped"]
    sale_statuses = set()
    for status in statuses:
        probe.order_status = status
        if _is_sale(probe):
            sale_statuses.add(status)
    assert sale_statuses == set(SALE_ORDER_STATUSES)
    assert sale_statuses == {"paid", "fulfilled", "completed"}


# ---------------------------------------------------------------------------
# P-2 窗口与环比
# ---------------------------------------------------------------------------
def test_p2_window_bounds_and_change_ratio(client):
    with _db() as db:
        account = _account(db, 1)
        _order(db, workspace_id=1, account_id=account.id, order_no="P2-A", status="paid", gross="100.00",
               created_at=datetime(2026, 10, 30, 9, 0, 0))
        _order(db, workspace_id=1, account_id=account.id, order_no="P2-B", status="paid", gross="50.00",
               created_at=datetime(2026, 10, 20, 9, 0, 0))
        _order(db, workspace_id=1, account_id=account.id, order_no="P2-C", status="paid", gross="20.00",
               created_at=datetime(2026, 10, 26, 9, 0, 0))
        db.commit()

    _login(client, "admin")
    body = _cockpit(client, days=7, as_of=AS_OF.isoformat())

    assert body["days"] == 7
    assert body["from"] == "2026-10-25"
    assert body["to"] == "2026-10-31"
    assert body["prev_from"] == "2026-10-18"
    assert body["prev_to"] == "2026-10-24"
    assert body["window"] == {
        "days": 7,
        "from": "2026-10-25",
        "to": "2026-10-31",
        "prev_from": "2026-10-18",
        "prev_to": "2026-10-24",
    }
    assert body["meta"]["from"] == "2026-10-25"
    assert body["meta"]["to"] == "2026-10-31"
    assert body["meta"]["prev_from"] == "2026-10-18"
    assert body["meta"]["prev_to"] == "2026-10-24"
    assert body["meta"]["as_of"] == "2026-10-31"
    assert body["meta"]["timezone"] == "UTC"

    # 当前窗口 = A(100) + C(20) = 120；上一窗口 = B(50)
    assert Decimal(body["kpis"]["gmv"]["value"]) == Decimal("120.00")
    assert body["kpis"]["gmv"]["change_ratio"] == pytest.approx((120 - 50) / 50)
    assert body["kpis"]["orders"]["value"] == 2
    assert body["kpis"]["orders"]["change_ratio"] == pytest.approx((2 - 1) / 1)

    # prev 窗口为 0 时环比必须是 null（而不是 Infinity）
    zero_base = _cockpit(client, days=7, as_of="2026-10-25")
    assert Decimal(zero_base["kpis"]["gmv"]["value"]) == Decimal("50.00")
    assert zero_base["kpis"]["gmv"]["change_ratio"] is None
    assert zero_base["kpis"]["orders"]["change_ratio"] is None


# ---------------------------------------------------------------------------
# P-3 稀疏日期不补 0
# ---------------------------------------------------------------------------
def test_p3_sparse_days_are_null_not_zero(client):
    with _db() as db:
        account = _account(db, 1)
        _order(db, workspace_id=1, account_id=account.id, order_no="P3-A", status="paid", gross="40.00",
               created_at=datetime(2026, 10, 28, 9, 0, 0))
        _order(db, workspace_id=1, account_id=account.id, order_no="P3-B", status="paid", gross="60.00",
               created_at=datetime(2026, 10, 31, 9, 0, 0))
        db.commit()

    _login(client, "admin")
    body = _cockpit(client, days=4, as_of=AS_OF.isoformat())
    trend = body["sales_trend"]

    assert trend["dates"] == ["2026-10-28", "2026-10-29", "2026-10-30", "2026-10-31"]
    assert trend["missing_days"] == 2
    assert trend["gmv"][1] is None
    assert trend["gmv"][2] is None
    assert trend["gmv"][0] == "40.00"
    assert trend["gmv"][3] == "60.00"
    assert trend["order_counts"] == [1, 0, 0, 1]
    assert trend["days_with_data"] == [True, False, False, True]
    assert trend["gmv_by_currency"]["CNY"][1] is None


# ---------------------------------------------------------------------------
# P-4 多币种不混加
# ---------------------------------------------------------------------------
def test_p4_multi_currency_is_never_summed(client):
    with _db() as db:
        account = _account(db, 1)
        _order(db, workspace_id=1, account_id=account.id, order_no="P4-CNY-1", status="paid", gross="100.00")
        _order(db, workspace_id=1, account_id=account.id, order_no="P4-CNY-2", status="paid", gross="50.00")
        _order(db, workspace_id=1, account_id=account.id, order_no="P4-USD-1", status="paid", gross="30.00",
               currency="USD")
        db.commit()

    _login(client, "admin")
    body = _cockpit(client, days=7, as_of=AS_OF.isoformat())

    grouped = {row["currency"]: row for row in body["by_currency"]}
    assert set(grouped) == {"CNY", "USD"}
    assert grouped["CNY"]["gmv"] == "150.00"
    assert grouped["CNY"]["order_count"] == 2
    assert grouped["USD"]["gmv"] == "30.00"
    assert grouped["USD"]["order_count"] == 1

    # 主值不得出现跨币种合计
    assert body["kpis"]["gmv"]["value"] is None
    assert body["kpis"]["gmv"]["multi_currency"] is True
    assert body["kpis"]["gmv"]["unit"] is None

    # 顶层 fx_todo 逐字相等
    assert body["fx_todo"] == "汇率折算依赖财务模块后续提供"


# ---------------------------------------------------------------------------
# P-5 缺货 / 积压判定（口径 A）
# ---------------------------------------------------------------------------
def test_p5_shortage_and_overstock_classification(client):
    with _db() as db:
        account = _account(db, 1)
        warehouse = _warehouse(db, workspace_id=1, code="W-P5")

        sku_out = _sku(db, workspace_id=1, sku_code="P5-OUT")
        sku_low = _sku(db, workspace_id=1, sku_code="P5-LOW")
        sku_nopolicy = _sku(db, workspace_id=1, sku_code="P5-NOPOLICY")
        sku_nosales = _sku(db, workspace_id=1, sku_code="P5-NOSALES")
        sku_slow = _sku(db, workspace_id=1, sku_code="P5-SLOW")

        _policy(db, workspace_id=1, warehouse_id=warehouse.id, sku_id=sku_out.id, safety=10, reorder=20)
        _balance(db, workspace_id=1, warehouse_id=warehouse.id, sku_id=sku_out.id, on_hand=5)
        _policy(db, workspace_id=1, warehouse_id=warehouse.id, sku_id=sku_low.id, safety=10, reorder=20)
        _balance(db, workspace_id=1, warehouse_id=warehouse.id, sku_id=sku_low.id, on_hand=15)
        # sku_nopolicy：只有库存没有策略 → unknown_policy，不计缺货
        _balance(db, workspace_id=1, warehouse_id=warehouse.id, sku_id=sku_nopolicy.id, on_hand=30)
        # sku_nosales：有策略、不缺货、近 30 天无销量 → NO_SALES
        _policy(db, workspace_id=1, warehouse_id=warehouse.id, sku_id=sku_nosales.id, safety=0, reorder=0)
        _balance(db, workspace_id=1, warehouse_id=warehouse.id, sku_id=sku_nosales.id, on_hand=100)
        # sku_slow：有策略、不缺货、日均 1 件、库存 1000 → 可售 1000 天 ≥ 90 → SLOW_MOVING
        _policy(db, workspace_id=1, warehouse_id=warehouse.id, sku_id=sku_slow.id, safety=0, reorder=0)
        _balance(db, workspace_id=1, warehouse_id=warehouse.id, sku_id=sku_slow.id, on_hand=1000)
        _daily_sale(
            db,
            workspace_id=1,
            account_id=account.id,
            sku_id=sku_slow.id,
            external_sku="P5-SLOW",
            sales_date=AS_OF - timedelta(days=1),
            net_qty=1,
        )
        db.commit()

    _login(client, "admin")
    body = _cockpit(client, days=7, as_of=AS_OF.isoformat())

    distribution = body["inventory_distribution"]
    assert distribution["out_of_stock"] == 1
    assert distribution["below_reorder"] == 1
    assert distribution["unknown_policy"] == 1
    assert distribution["overstock"] == 2

    shortage = {item["sku_code"]: item for item in body["todo"]["shortage"]}
    assert set(shortage) == {"P5-OUT", "P5-LOW"}
    assert shortage["P5-OUT"]["status"] == "out_of_stock"
    assert shortage["P5-LOW"]["status"] == "below_reorder"
    assert "P5-NOPOLICY" not in shortage

    overstock = {item["sku_code"]: item for item in body["todo"]["overstock"]}
    assert set(overstock) == {"P5-NOSALES", "P5-SLOW"}
    assert overstock["P5-NOSALES"]["reason"] == "NO_SALES"
    assert overstock["P5-SLOW"]["reason"] == "SLOW_MOVING"
    # reason 单值（不是列表/多值）
    for item in body["todo"]["overstock"]:
        assert isinstance(item["reason"], str)
    assert overstock["P5-SLOW"]["days_of_inventory"] == pytest.approx(1000.0)

    # 前端文案必须是用户钦定的两句（逐字）
    template = COCKPIT_TEMPLATE.read_text(encoding="utf-8")
    assert "因近 30 天无销量滞销" in template
    assert "预计可售超 90 天" in template


# ---------------------------------------------------------------------------
# P-6 待办计数状态边界
# ---------------------------------------------------------------------------
def test_p6_pending_todo_status_boundaries(client):
    with _db() as db:
        warehouse = _warehouse(db, workspace_id=1, code="W-P6")
        _purchase(db, workspace_id=1, warehouse_id=warehouse.id, request_no="P6-SUB", status="submitted")
        for status in ("approved", "rejected", "cancelled", "draft"):
            _purchase(db, workspace_id=1, warehouse_id=warehouse.id, request_no=f"P6-{status.upper()}", status=status)
        _inbound(db, workspace_id=1, warehouse_id=warehouse.id, reference_no="P6-EXPECTED", status="expected")
        _inbound(db, workspace_id=1, warehouse_id=warehouse.id, reference_no="P6-RECEIVED", status="received")
        _inbound(db, workspace_id=1, warehouse_id=warehouse.id, reference_no="P6-CONFIRMED", status="confirmed")
        db.commit()

    _login(client, "admin")
    body = _cockpit(client, days=7, as_of=AS_OF.isoformat())

    assert body["kpis"]["pending_purchase_approvals"]["value"] == 1
    assert body["todo"]["pending_purchase_count"] == 1
    assert [item["ref"] for item in body["todo"]["purchase_approvals"]] == ["P6-SUB"]
    assert body["todo"]["purchase_approvals"][0]["status"] == "submitted"

    assert body["todo"]["pending_inbound_count"] == 2
    inbound_refs = sorted(item["ref"] for item in body["todo"]["pending_inbounds"])
    assert inbound_refs == ["P6-EXPECTED", "P6-RECEIVED"]
    assert "P6-CONFIRMED" not in inbound_refs


# ---------------------------------------------------------------------------
# P-7 知识库健康只认已发布
# ---------------------------------------------------------------------------
def test_p7_knowledge_counts_only_published_versions(client):
    import app.db as db_mod

    with _db() as db:
        document = db_mod.Document(
            workspace_id=1,
            source_type="pdf",
            title="驾驶舱测试文档",
            filename="cockpit.pdf",
            sha256="cockpit-doc-sha",
        )
        db.add(document)
        db.flush()

        draft = db_mod.DocumentVersion(
            workspace_id=1,
            document_id=document.id,
            version_no=1,
            sha256="cockpit-v1",
            size_bytes=100,
            storage_uri="artifacts/v1.pdf",
            status="ready",
            published_at=None,
        )
        published = db_mod.DocumentVersion(
            workspace_id=1,
            document_id=document.id,
            version_no=2,
            sha256="cockpit-v2",
            size_bytes=200,
            storage_uri="artifacts/v2.pdf",
            status="ready",
            published_at=datetime(2026, 10, 20, 8, 0, 0),
        )
        failed = db_mod.DocumentVersion(
            workspace_id=1,
            document_id=document.id,
            version_no=3,
            sha256="cockpit-v3",
            size_bytes=300,
            storage_uri="artifacts/v3.pdf",
            status="failed",
            error_code="PARSE_FAILED",
            published_at=None,
        )
        db.add_all([draft, published, failed])
        db.flush()

        for version, tag in ((draft, "draft"), (published, "published")):
            for chunk_no in range(3):
                db.add(
                    db_mod.DocumentChunk(
                        workspace_id=1,
                        document_version_id=version.id,
                        chunk_no=chunk_no,
                        text=f"chunk {chunk_no} of {tag}",
                        content_hash=f"chunk-hash-{tag}-{chunk_no}",
                    )
                )
        db.commit()

    _login(client, "admin")
    body = _cockpit(client, days=7, as_of=AS_OF.isoformat())

    assert body["knowledge"]["available_chunks"] == 3
    assert body["knowledge"]["published_versions"] == 1
    assert body["knowledge"]["failed_versions"] == 1
    assert body["knowledge"]["documents"] == 1
    assert body["kpis"]["knowledge_chunks"]["value"] == 3


# ---------------------------------------------------------------------------
# P-8 权限与跨租户隔离
# ---------------------------------------------------------------------------
def test_p8_permissions_and_tenant_isolation(client):
    with _db() as db:
        ws1_account = _account(db, 1)
        _order(db, workspace_id=1, account_id=ws1_account.id, order_no="P8-WS1", status="paid", gross="10.00")

        ws2_account = _account(db, 2, suffix="b")
        _order(db, workspace_id=2, account_id=ws2_account.id, order_no="P8-WS2", status="paid", gross="999.00")
        ws2_warehouse = _warehouse(db, workspace_id=2, code="W-WS2")
        ws2_sku = _sku(db, workspace_id=2, sku_code="P8-WS2-SKU")
        _policy(db, workspace_id=2, warehouse_id=ws2_warehouse.id, sku_id=ws2_sku.id, safety=10, reorder=20)
        _balance(db, workspace_id=2, warehouse_id=ws2_warehouse.id, sku_id=ws2_sku.id, on_hand=1)
        db.commit()

    # 角色无权限 → 403
    for role_login in ("customer_service", "readonly"):
        _login(client, role_login)
        forbidden = client.get("/api/dashboard/cockpit", params={"days": 7, "as_of": AS_OF.isoformat()})
        assert forbidden.status_code == 403, f"{role_login}: {forbidden.text}"
        page = client.get("/cockpit")
        assert page.status_code == 403, f"{role_login}: {page.status_code}"

    # 有权限 → 200
    for role_login in ("operations", "admin"):
        _login(client, role_login)
        allowed = client.get("/api/dashboard/cockpit", params={"days": 7, "as_of": AS_OF.isoformat()})
        assert allowed.status_code == 200, f"{role_login}: {allowed.text}"
        assert allowed.json()["meta"]["workspace_id"] == 1

    # 跨租户隔离：workspace 2 的订单 / 库存不得出现在 workspace 1 的响应里
    _login(client, "admin")
    body = _cockpit(client, days=7, as_of=AS_OF.isoformat())
    assert Decimal(body["kpis"]["gmv"]["value"]) == Decimal("10.00")
    assert body["todo"]["shortage"] == []
    assert body["inventory_distribution"]["out_of_stock"] == 0
    assert "P8-WS2-SKU" not in str(body)

    # workspace 2 登录后只看到自己的数据
    _login(client, "empty_admin")
    other = _cockpit(client, days=7, as_of=AS_OF.isoformat())
    assert other["meta"]["workspace_id"] == 2
    assert Decimal(other["kpis"]["gmv"]["value"]) == Decimal("999.00")
    assert [item["sku_code"] for item in other["todo"]["shortage"]] == ["P8-WS2-SKU"]
    assert "P8-WS1" not in str(other)


# ---------------------------------------------------------------------------
# P-9 页面渲染契约
# ---------------------------------------------------------------------------
def test_p9_cockpit_page_contract(client):
    _login(client, "admin")
    response = client.get("/cockpit")
    assert response.status_code == 200, response.text
    html = response.text

    for marker in (
        'id="kpi-grid"',
        'id="sales-chart"',
        'id="inventory-pie"',
        'id="todo-shortage"',
        'id="todo-overstock"',
        'id="todo-purchase"',
        'id="todo-inbound"',
    ):
        assert marker in html, marker
    assert 'href="/cockpit"' in html
    assert 'id="logout-link"' not in html
    assert 'id="logout"' in html
    assert "/api/dashboard/cockpit" in html
    # 既有页面不受影响
    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert 'id="sales-trend-panel"' in dashboard.text


# ---------------------------------------------------------------------------
# P-10 空工作区不炸
# ---------------------------------------------------------------------------
def test_p10_empty_workspace_is_safe(client):
    _login(client, "empty_admin")
    body = _cockpit(client, days=7)

    assert body["kpis"]["gmv"]["value"] is None
    assert body["kpis"]["gmv"]["quality"] == "insufficient"
    assert body["kpis"]["gmv"]["reason"] == "NO_ORDER_FACTS"
    assert body["kpis"]["orders"]["value"] == 0
    assert body["kpis"]["orders"]["quality"] == "insufficient"
    assert body["kpis"]["out_of_stock_skus"]["value"] == 0
    assert body["kpis"]["out_of_stock_skus"]["quality"] == "insufficient"
    assert body["kpis"]["overstock_skus"]["value"] == 0
    assert body["kpis"]["overstock_skus"]["quality"] == "insufficient"
    assert body["kpis"]["pending_purchase_approvals"]["value"] == 0
    assert body["kpis"]["pending_purchase_approvals"]["quality"] == "insufficient"
    assert body["kpis"]["knowledge_chunks"]["value"] == 0
    assert body["kpis"]["knowledge_chunks"]["quality"] == "insufficient"

    assert body["sales_trend"]["missing_days"] == body["days"]
    assert body["by_currency"] == []
    assert body["fx_todo"] == "汇率折算依赖财务模块后续提供"
    assert len(body["limitations"]) >= 4
    assert all(item.strip() for item in body["limitations"])

    page = client.get("/cockpit")
    assert page.status_code == 200
    assert 'id="sales-chart"' in page.text
    assert 'id="inventory-pie"' in page.text


# ---------------------------------------------------------------------------
# P-11 图表空态防御契约（源码位置断言）
# ---------------------------------------------------------------------------
def test_p11_chart_empty_state_contract():
    template = COCKPIT_TEMPLATE.read_text(encoding="utf-8")

    # ① 占位函数与样式存在
    assert "function chartEmpty" in template
    assert ".chart-empty" in template
    assert "chart-empty" in template

    # ② 空态判定必须在 echarts.init 之前（按函数体切片做位置断言）
    def function_body(source: str, name: str, next_name: str) -> str:
        start = source.index(f"function {name}(")
        end = source.index(f"function {next_name}(")
        return source[start:end]

    sales_body = function_body(template, "renderSalesTrend", "renderInventoryPie")
    pie_body = function_body(template, "renderInventoryPie", "statusBadge")

    assert "echarts.init" in sales_body and "echarts.init" in pie_body
    assert sales_body.index("chartEmpty(") < sales_body.index("echarts.init")
    assert pie_body.index("chartEmpty(") < pie_body.index("echarts.init")

    # ④ 不得出现「先 init 再判空」：判定分支的 return 必须早于 init
    assert sales_body.index("if(!hasData)") < sales_body.index("echarts.init")
    assert "return;" in sales_body[: sales_body.index("echarts.init")]
    assert pie_body.index("if(!total)") < pie_body.index("echarts.init")
    assert "return;" in pie_body[: pie_body.index("echarts.init")]

    # 空态文案必须中文且含原因词
    assert "无订单事实" in template
    assert "无库存策略" in template
    for body in (sales_body, pie_body):
        placeholder = body[body.index("chartEmpty(") : body.index("chartEmpty(") + 200]
        assert "无" in placeholder or "暂无" in placeholder

    # ③ 两个图表容器始终存在（空态是容器内占位，不是容器消失）
    assert 'id="sales-chart"' in template
    assert 'id="inventory-pie"' in template

    # 趋势图缺失日不得被当成 0 画：柱状数据对缺失日取 null
    assert "daysWithData[index]===false?null:count" in template
