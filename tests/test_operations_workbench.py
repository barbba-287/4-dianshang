"""运营工作台今日待办聚合测试。"""

from datetime import date, datetime, timedelta
from decimal import Decimal
import importlib

import pytest


@pytest.fixture
def workbench_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'workbench.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    import app.config as cfg
    import app.db as db_mod
    import app.repository as repo_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(repo_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    yield db_mod, repo_mod
    cfg.get_settings.cache_clear()


def _seed_facts(db_mod, repo_mod, *, workspace_id=None):
    from app.schemas import ProductRecord

    workspace = db_mod.Workspace(tenant_key="demo-todo-shop", name="[虚构] 待办演示商家")
    db = db_mod.SessionLocal()
    db.add(workspace)
    db.flush()
    warehouse = db_mod.Warehouse(workspace_id=workspace.id, code="TODO-WH", name="演示仓", warehouse_type="own")
    db.add(warehouse)
    db.flush()
    product = repo_mod.upsert_product(
        db,
        ProductRecord(
            source="fixture",
            external_product_id="todo-product",
            title="待办商品",
            url="https://fixture.local/todo-product",
            current_price=Decimal("10.00"),
            observed_at=datetime.utcnow(),
        ),
        workspace_id=workspace.id,
    )
    sku = db_mod.ProductSku(workspace_id=workspace.id, product_id=product.id, sku_code="TODO-SKU", variant_label="演示规格")
    db.add(sku)
    db.flush()
    now = datetime.utcnow()
    db.add(db_mod.InventoryAlert(
        workspace_id=workspace.id, kind="low_stock", severity="critical", status="open",
        dedupe_key="todo-alert", title="库存低于补货点", message="库存不足",
        warehouse_id=warehouse.id, sku_id=sku.id, created_at=now, last_seen_at=now,
    ))
    db.add(db_mod.ReplenishmentSuggestion(
        workspace_id=workspace.id, warehouse_id=warehouse.id, sku_id=sku.id,
        status="suggested", suggested_qty=12, decision_qty=None, formula_version="v1",
        coverage_days=14, daily_avg_qty=Decimal("2"), on_hand_qty=2,
        safety_stock_qty=5, reorder_point_qty=8, sales_window_days=14,
        sales_qty=28, effective_sale_days=14, data_completeness="complete",
        reason="LOW_STOCK", source_hash="todo-suggestion-hash", source_snapshot_json="{}",
        as_of_date=date.today(), active_slot="open", version=1, created_at=now, updated_at=now,
    ))
    db.add(db_mod.PurchaseRequest(
        workspace_id=workspace.id, warehouse_id=warehouse.id, request_no="TODO-PR-001",
        status="draft", note="待人工复核", created_by="test", version=1,
        idempotency_key="todo-pr-key", payload_hash="todo-pr-hash", created_at=now, updated_at=now,
    ))
    db.add(db_mod.InboundOrder(
        workspace_id=workspace.id, warehouse_id=warehouse.id, reference_no="TODO-IN-001",
        status="received", created_by="test", created_at=now - timedelta(minutes=1), updated_at=now,
    ))
    db.add(db_mod.ExternalSyncRun(
        workspace_id=workspace.id, run_id="todo-sync-001", platform="mock", account_ref="demo",
        store_ref="demo-store", sync_type="inventory", source_mode="json", simulated=True,
        status="failed", started_at=now - timedelta(minutes=2), finished_at=now,
        heartbeat_at=now, error_code="TIMEOUT", retryable_resources_json='["inventory"]',
    ))
    db.commit()
    result = (workspace, warehouse, sku)
    db.close()
    return result


def test_today_aggregates_existing_facts_and_preserves_demo_boundary(workbench_db):
    db_mod, repo_mod = workbench_db
    from app.operations_workbench import build_operations_today

    with db_mod.SessionLocal() as db:
        workspace, warehouse, _sku = _seed_facts(db_mod, repo_mod)
        result = build_operations_today(db, workspace_id=workspace.id, now=datetime(2026, 10, 6, 12, 0, 0))

    assert result["simulated"] is True
    assert result["source_mode"] == "synthetic"
    assert result["evidence_level"] == "E2"
    assert result["summary"]["total"] == 5
    assert {item["type"] for item in result["items"]} == {
        "inventory_alert", "replenishment_review", "purchase_draft", "sync_failure", "inbound_confirmation",
    }
    assert all(item["target_path"] for item in result["items"])


def test_today_is_workspace_and_warehouse_scoped(workbench_db):
    db_mod, repo_mod = workbench_db
    from app.operations_workbench import build_operations_today

    with db_mod.SessionLocal() as db:
        workspace, warehouse, _sku = _seed_facts(db_mod, repo_mod)
        other = db_mod.Workspace(tenant_key="demo-other-shop", name="另一个商家")
        db.add(other)
        db.flush()
        other_warehouse = db_mod.Warehouse(workspace_id=other.id, code="OTHER-WH", name="另一个仓", warehouse_type="own")
        db.add(other_warehouse)
        db.commit()
        result = build_operations_today(db, workspace_id=workspace.id, warehouse_ids=(warehouse.id,))
        empty = build_operations_today(db, workspace_id=other.id)

    assert result["summary"]["total"] == 5
    assert empty["summary"]["total"] == 0
    assert all(item["warehouse_id"] == warehouse.id or item["type"] == "sync_failure" for item in result["items"])


def test_today_rejects_invalid_limit(workbench_db):
    db_mod, _repo_mod = workbench_db
    from app.operations_workbench import build_operations_today

    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="demo-limit", name="限制测试")
        db.add(workspace)
        db.commit()
        with pytest.raises(ValueError, match="INVALID_TODO_LIMIT"):
            build_operations_today(db, workspace_id=workspace.id, limit=0)
