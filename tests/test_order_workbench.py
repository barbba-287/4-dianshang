"""订单只读工作台查询测试。"""

from datetime import datetime, timedelta
from decimal import Decimal
import importlib

import pytest


@pytest.fixture
def order_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'orders.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    import app.config as cfg
    import app.db as db_mod
    cfg.get_settings.cache_clear(); importlib.reload(cfg); importlib.reload(db_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    yield db_mod
    cfg.get_settings.cache_clear()


def _insert_order(db, db_mod, *, workspace_id, order_no, status, created_at, completeness="complete", simulated=True):
    account = db.query(db_mod.ExternalAccount).filter_by(workspace_id=workspace_id, platform="mock", account_ref="demo", store_ref="store").first()
    if account is None:
        account = db_mod.ExternalAccount(workspace_id=workspace_id, platform="mock", account_ref="demo", store_ref="store", source_mode="mock", simulated=True)
        db.add(account); db.flush()
    order = db_mod.ExternalOrder(
        workspace_id=workspace_id, external_account_id=account.id, platform="mock", account_ref="demo", store_ref="store",
        external_order_no=order_no, order_status=status, external_created_at=created_at, external_updated_at=created_at,
        gross_amount=Decimal("29.90"), refund_amount=Decimal("0"), currency="CNY", payload_json="{}", payload_hash=f"hash-{order_no}",
        idempotency_key=f"key-{order_no}", source_mode="mock", simulated=simulated, data_completeness=completeness,
    )
    db.add(order); db.flush()
    db.add(db_mod.ExternalOrderLine(workspace_id=workspace_id, external_order_id=order.id, external_line_id="line-1", external_sku="SKU-1", ordered_qty=2, cancelled_qty=0, refunded_qty=0, gross_amount=Decimal("29.90"), refund_amount=Decimal("0"), currency="CNY", mapping_status="unmapped", data_completeness=completeness, payload_hash=f"line-{order_no}"))
    return order


def test_order_list_filters_summary_and_lines_are_workspace_scoped(order_db):
    db_mod = order_db
    from app.order_workbench import get_order, list_orders
    now = datetime(2026, 10, 6, 9, 0)
    with db_mod.SessionLocal() as db:
        first = db_mod.Workspace(tenant_key="orders-a", name="订单 A")
        other = db_mod.Workspace(tenant_key="orders-b", name="订单 B")
        db.add_all([first, other]); db.flush()
        paid = _insert_order(db, db_mod, workspace_id=first.id, order_no="A-PAID", status="paid", created_at=now, simulated=True)
        pending = _insert_order(db, db_mod, workspace_id=first.id, order_no="A-PENDING", status="pending", created_at=now - timedelta(days=1), completeness="partial")
        private = _insert_order(db, db_mod, workspace_id=other.id, order_no="B-PAID", status="paid", created_at=now)
        db.commit()
        result = list_orders(db, workspace_id=first.id, fulfillment_view="to_fulfill")
        attention = list_orders(db, workspace_id=first.id, fulfillment_view="needs_attention")
        detail = get_order(db, workspace_id=first.id, order_id=paid.id)
        hidden = get_order(db, workspace_id=first.id, order_id=private.id)

    assert result["total"] == 1
    assert result["items"][0]["external_order_no"] == "A-PAID"
    assert result["items"][0]["net_item_qty"] == 2
    assert result["items"][0]["simulated"] is True
    assert attention["items"][0]["external_order_no"] == "A-PENDING"
    assert attention["items"][0]["data_completeness"] == "partial"
    assert detail["lines"][0].external_sku == "SKU-1"
    assert hidden is None


def test_order_list_date_range_and_invalid_filters(order_db):
    db_mod = order_db
    from app.order_workbench import list_orders
    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="orders-date", name="订单日期")
        db.add(workspace); db.flush()
        _insert_order(db, db_mod, workspace_id=workspace.id, order_no="IN-RANGE", status="paid", created_at=datetime(2026, 10, 5, 10))
        _insert_order(db, db_mod, workspace_id=workspace.id, order_no="OUT-RANGE", status="paid", created_at=datetime(2026, 10, 1, 10))
        db.commit()
        result = list_orders(db, workspace_id=workspace.id, date_from=__import__("datetime").date(2026, 10, 5), date_to=__import__("datetime").date(2026, 10, 6))
        with pytest.raises(ValueError, match="INVALID_DATE_RANGE"):
            list_orders(db, workspace_id=workspace.id, date_from=__import__("datetime").date(2026, 10, 6), date_to=__import__("datetime").date(2026, 10, 5))
        with pytest.raises(ValueError, match="INVALID_FULFILLMENT_VIEW"):
            list_orders(db, workspace_id=workspace.id, fulfillment_view="ship_now")
    assert [item["external_order_no"] for item in result["items"]] == ["IN-RANGE"]
