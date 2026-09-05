"""统一外部订单事实层和每日销量聚合测试。"""

from __future__ import annotations

import importlib
import json
from datetime import datetime
from decimal import Decimal

import pytest

from app.connectors import ConnectorError, load_orders


def _order_content(
    *,
    order_no: str = "ORDER-1",
    status: str = "paid",
    ordered_qty: int = 5,
    cancelled_qty: int = 0,
    refunded_qty: int = 0,
    gross_amount: str = "50.00",
    refund_amount: str = "0.00",
    event_version: int | None = 1,
    updated_at: str = "2026-09-01T10:01:00Z",
    account_ref: str = "account-a",
    store_ref: str = "store-a",
) -> str:
    row = {
        "account_ref": account_ref,
        "store_ref": store_ref,
        "external_order_no": order_no,
        "status": status,
        "created_at": "2026-09-01T10:00:00Z",
        "updated_at": updated_at,
        "lines": [{
            "external_line_id": "LINE-1",
            "external_sku": "EXT-SKU-1",
            "ordered_qty": ordered_qty,
            "cancelled_qty": cancelled_qty,
            "refunded_qty": refunded_qty,
            "gross_amount": gross_amount,
            "refund_amount": refund_amount,
        }],
    }
    if event_version is not None:
        row["event_version"] = event_version
    return json.dumps({"records": [row]})


@pytest.fixture
def sales_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'sales.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "false")
    import app.config as cfg
    import app.db as db_mod
    import app.external_orders as sales_mod
    import app.repository as repo_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(repo_mod)
    importlib.reload(sales_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    yield db_mod, repo_mod, sales_mod
    cfg.get_settings.cache_clear()


def _workspace_product_sku(db_mod, repo_mod, *, tenant_key: str):
    from app.schemas import ProductRecord

    workspace = db_mod.Workspace(tenant_key=tenant_key, name=tenant_key)
    db_mod_session = db_mod.SessionLocal()
    db_mod_session.add(workspace)
    db_mod_session.flush()
    product = repo_mod.upsert_product(
        db_mod_session,
        ProductRecord(
            source="fixture",
            external_product_id=f"product-{tenant_key}",
            title="测试商品",
            url="https://fixture.local/product",
            current_price=Decimal("10.00"),
            observed_at=datetime(2026, 9, 1, 0, 0, 0),
        ),
        workspace_id=workspace.id,
    )
    db_mod_session.flush()
    sku = db_mod.ProductSku(
        workspace_id=workspace.id,
        product_id=product.id,
        sku_code=f"SKU-{tenant_key}",
    )
    db_mod_session.add(sku)
    db_mod_session.commit()
    return db_mod_session, workspace, sku


def test_order_normalizer_supports_refund_and_rejects_invalid_quantity():
    record = load_orders(
        _order_content(cancelled_qty=1, refunded_qty=1, refund_amount="10.00"),
        platform="jd",
    )[0]
    assert record.store_ref == "store-a"
    assert record.lines[0].ordered_qty == 5
    assert record.lines[0].cancelled_qty == 1
    assert record.lines[0].refunded_qty == 1
    assert record.lines[0].refund_amount == Decimal("10.00")

    with pytest.raises(ConnectorError, match="不能大于"):
        load_orders(_order_content(ordered_qty=1, cancelled_qty=1, refunded_qty=1), platform="jd")


def test_order_ingest_is_idempotent_and_refund_aware(sales_db):
    db_mod, _repo_mod, sales_mod = sales_db
    db, workspace, _sku = _workspace_product_sku(db_mod, _repo_mod, tenant_key="workspace-a")
    try:
        record = load_orders(
            _order_content(cancelled_qty=1, refunded_qty=1, refund_amount="10.00"),
            platform="jd",
        )[0]
        first = sales_mod.ingest_orders(db, [record], workspace_id=workspace.id)
        replay = sales_mod.ingest_orders(db, [record], workspace_id=workspace.id)
        assert first.inserted == 1
        assert replay.no_op == 1
        rows, total = sales_mod.list_daily_sales(db, workspace_id=workspace.id)
        assert total == 1
        assert rows[0].gross_qty == 5
        assert rows[0].cancelled_qty == 1
        assert rows[0].refunded_qty == 1
        assert rows[0].net_qty == 3
        assert rows[0].net_amount == Decimal("40.00")
    finally:
        db.close()


def test_same_order_number_isolated_between_workspaces(sales_db):
    db_mod, repo_mod, sales_mod = sales_db
    db_a, workspace_a, _sku_a = _workspace_product_sku(db_mod, repo_mod, tenant_key="workspace-a")
    db_a.close()
    db, workspace_b, _sku_b = _workspace_product_sku(db_mod, repo_mod, tenant_key="workspace-b")
    try:
        record = load_orders(_order_content(order_no="SAME-ORDER"), platform="jd")[0]
        first = sales_mod.ingest_orders(db, [record], workspace_id=workspace_b.id)
        second = sales_mod.ingest_orders(db, [record], workspace_id=workspace_b.id)
        assert first.inserted == 1
        assert second.no_op == 1
        rows_a, total_a = sales_mod.list_daily_sales(db, workspace_id=workspace_a.id)
        rows_b, total_b = sales_mod.list_daily_sales(db, workspace_id=workspace_b.id)
        assert rows_a == []
        assert total_a == 0
        assert total_b == 1
        assert rows_b[0].workspace_id == workspace_b.id
    finally:
        db.close()


def test_newer_order_version_updates_projection_and_stale_is_ignored(sales_db):
    db_mod, _repo_mod, sales_mod = sales_db
    db, workspace, _sku = _workspace_product_sku(db_mod, _repo_mod, tenant_key="workspace-version")
    try:
        first = load_orders(_order_content(event_version=2), platform="jd")[0]
        newer = load_orders(
            _order_content(event_version=3, ordered_qty=7, updated_at="2026-09-01T10:03:00Z"),
            platform="jd",
        )[0]
        older = load_orders(
            _order_content(event_version=1, ordered_qty=1, updated_at="2026-09-01T10:00:30Z"),
            platform="jd",
        )[0]
        assert sales_mod.ingest_orders(db, [first], workspace_id=workspace.id).inserted == 1
        assert sales_mod.ingest_orders(db, [older], workspace_id=workspace.id).stale == 1
        assert sales_mod.ingest_orders(db, [newer], workspace_id=workspace.id).updated == 1
        rows, _ = sales_mod.list_daily_sales(db, workspace_id=workspace.id)
        assert rows[0].gross_qty == 7
        assert rows[0].net_qty == 7
    finally:
        db.close()


def test_order_ingest_does_not_write_internal_inventory(sales_db):
    db_mod, repo_mod, sales_mod = sales_db
    db, workspace, sku = _workspace_product_sku(db_mod, repo_mod, tenant_key="workspace-inventory")
    try:
        warehouse = db_mod.Warehouse(workspace_id=workspace.id, code="W-1", name="测试仓")
        db.add(warehouse)
        db.flush()
        db.add(db_mod.InventoryBalance(workspace_id=workspace.id, warehouse_id=warehouse.id, sku_id=sku.id, on_hand_qty=11))
        db.add(db_mod.InventoryTransaction(workspace_id=workspace.id, warehouse_id=warehouse.id, sku_id=sku.id, quantity_delta=11, movement_type="seed", idempotency_key="seed-inventory"))
        db.commit()
        before_balance = db.scalar(__import__("sqlalchemy").select(db_mod.InventoryBalance.on_hand_qty))
        before_transactions = db.scalar(__import__("sqlalchemy").select(__import__("sqlalchemy").func.count(db_mod.InventoryTransaction.id)))
        record = load_orders(_order_content(), platform="jd")[0]
        sales_mod.ingest_orders(db, [record], workspace_id=workspace.id)
        after_balance = db.scalar(__import__("sqlalchemy").select(db_mod.InventoryBalance.on_hand_qty))
        after_transactions = db.scalar(__import__("sqlalchemy").select(__import__("sqlalchemy").func.count(db_mod.InventoryTransaction.id)))
        assert after_balance == before_balance == 11
        assert after_transactions == before_transactions == 1
    finally:
        db.close()
