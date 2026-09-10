"""补货建议和采购申请 service 回归测试。"""
from __future__ import annotations

import importlib
from datetime import date, timedelta
from decimal import Decimal
import json

import pytest
from sqlalchemy import func, select


def _reload_sales_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'replenishment.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    import app.config as cfg
    import app.db as db_mod
    import app.replenishment as replenishment
    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(replenishment)
    db_mod.Base.metadata.create_all(db_mod.engine)
    return db_mod, replenishment


def _fixture(db_mod, *, tenant_key="workspace-a"):
    from app.schemas import ProductRecord
    from app.repository import upsert_product
    db = db_mod.SessionLocal()
    workspace = db_mod.Workspace(tenant_key=tenant_key, name=tenant_key)
    db.add(workspace)
    db.flush()
    product = upsert_product(db, ProductRecord(source="fixture", external_product_id=f"p-{tenant_key}", title="商品", url="https://fixture.local/p", current_price=Decimal("10.00"), observed_at=__import__("datetime").datetime.utcnow()), workspace_id=workspace.id)
    db.flush()
    sku = db_mod.ProductSku(workspace_id=workspace.id, product_id=product.id, sku_code=f"SKU-{tenant_key}")
    warehouse = db_mod.Warehouse(workspace_id=workspace.id, code=f"W-{tenant_key}", name="仓库")
    db.add_all([sku, warehouse])
    db.flush()
    policy = db_mod.InventoryPolicy(workspace_id=workspace.id, warehouse_id=warehouse.id, sku_id=sku.id, safety_stock_qty=5, reorder_point_qty=8)
    db.add(policy)
    db.add(db_mod.InventoryBalance(workspace_id=workspace.id, warehouse_id=warehouse.id, sku_id=sku.id, on_hand_qty=4))
    for offset in range(14):
        db.add(db_mod.DailySkuSale(
            workspace_id=workspace.id, external_account_id=1, platform="jd", account_ref="a", store_ref="s", external_sku=sku.sku_code,
            internal_sku_id=sku.id, sales_date=date.today() - timedelta(days=13 - offset),
            gross_qty=3, cancelled_qty=0, refunded_qty=0, net_qty=3,
            gross_amount=Decimal("30.00"), refund_amount=Decimal("0"), net_amount=Decimal("30.00"), order_count=1,
            data_completeness="complete",
        ))
    # DailySkuSale requires account FK; create the account before committing and repair rows.
    account = db_mod.ExternalAccount(workspace_id=workspace.id, platform="jd", account_ref="a", store_ref="s")
    db.add(account)
    db.flush()
    db.query(db_mod.DailySkuSale).update({db_mod.DailySkuSale.external_account_id: account.id})
    db.commit()
    return db, workspace, sku, warehouse


@pytest.fixture
def env(monkeypatch, tmp_path):
    return _reload_sales_env(monkeypatch, tmp_path)


def test_generate_decide_and_submit_purchase_request_without_inventory_side_effect(env):
    db_mod, replenishment = env
    db, workspace, sku, warehouse = _fixture(db_mod)
    try:
        suggestion = replenishment.generate_suggestion(db, workspace_id=workspace.id, warehouse_id=warehouse.id, sku_id=sku.id, actor="ops")
        assert suggestion.suggested_qty == 43
        assert suggestion.status == "suggested"
        before_balance = db.scalar(select(db_mod.InventoryBalance.on_hand_qty).where(db_mod.InventoryBalance.workspace_id == workspace.id))
        before_transactions = db.scalar(select(func.count(db_mod.InventoryTransaction.id)).where(db_mod.InventoryTransaction.workspace_id == workspace.id)) or 0
        confirmed = replenishment.decide_suggestion(db, workspace_id=workspace.id, suggestion_id=suggestion.id, action="confirm", actor="ops", idempotency_key="decision-1", expected_version=1)
        assert confirmed.status == "confirmed"
        request = replenishment.submit_purchase_request(db, workspace_id=workspace.id, suggestion_ids=[suggestion.id], actor="ops", idempotency_key="purchase-1")
        assert request.status == "submitted"
        replay = replenishment.submit_purchase_request(db, workspace_id=workspace.id, suggestion_ids=[suggestion.id], actor="ops", idempotency_key="purchase-1")
        assert replay.id == request.id
        after_balance = db.scalar(select(db_mod.InventoryBalance.on_hand_qty).where(db_mod.InventoryBalance.workspace_id == workspace.id))
        after_transactions = db.scalar(select(func.count(db_mod.InventoryTransaction.id)).where(db_mod.InventoryTransaction.workspace_id == workspace.id)) or 0
        assert before_balance == after_balance == 4
        assert before_transactions == after_transactions == 0
    finally:
        db.close()


def test_purchase_draft_lifecycle_and_inventory_invariant(env):
    db_mod, replenishment = env
    db, workspace, sku, warehouse = _fixture(db_mod, tenant_key="draft")
    try:
        suggestion = replenishment.generate_suggestion(db, workspace_id=workspace.id, warehouse_id=warehouse.id, sku_id=sku.id, actor="ops")
        confirmed = replenishment.decide_suggestion(db, workspace_id=workspace.id, suggestion_id=suggestion.id, action="confirm", actor="ops", idempotency_key="draft-decision", expected_version=1)
        before_balance = db.scalar(select(db_mod.InventoryBalance.on_hand_qty).where(db_mod.InventoryBalance.workspace_id == workspace.id))
        before_transactions = db.scalar(select(func.count(db_mod.InventoryTransaction.id)).where(db_mod.InventoryTransaction.workspace_id == workspace.id)) or 0
        draft = replenishment.create_purchase_request_draft(db, workspace_id=workspace.id, suggestion_ids=[suggestion.id], actor="ops", idempotency_key="draft-create", note="检查后提交", supplier_ref="supplier-a")
        assert draft.status == "draft"
        assert draft.submitted_by is None
        assert draft.version == 1
        replay = replenishment.create_purchase_request_draft(db, workspace_id=workspace.id, suggestion_ids=[suggestion.id], actor="ops", idempotency_key="draft-create", note="检查后提交", supplier_ref="supplier-a")
        assert replay.id == draft.id
        edited = replenishment.edit_purchase_request_draft(db, workspace_id=workspace.id, request_id=draft.id, actor="ops", idempotency_key="draft-edit", expected_version=1, note="已检查", supplier_ref="supplier-b")
        assert edited.version == 2
        submitted = replenishment.submit_purchase_request_draft(db, workspace_id=workspace.id, request_id=draft.id, actor="ops", idempotency_key="draft-submit", expected_version=2)
        assert submitted.status == "submitted"
        assert submitted.submitted_by == "ops"
        after_balance = db.scalar(select(db_mod.InventoryBalance.on_hand_qty).where(db_mod.InventoryBalance.workspace_id == workspace.id))
        after_transactions = db.scalar(select(func.count(db_mod.InventoryTransaction.id)).where(db_mod.InventoryTransaction.workspace_id == workspace.id)) or 0
        assert before_balance == after_balance == 4
        assert before_transactions == after_transactions == 0
        db.refresh(confirmed)
        assert confirmed.status == "submitted"
    finally:
        db.close()


def test_purchase_draft_rejects_stale_version_without_partial_commit(env):
    db_mod, replenishment = env
    db, workspace, sku, warehouse = _fixture(db_mod, tenant_key="stale-draft")
    try:
        suggestion = replenishment.generate_suggestion(db, workspace_id=workspace.id, warehouse_id=warehouse.id, sku_id=sku.id, actor="ops")
        replenishment.decide_suggestion(db, workspace_id=workspace.id, suggestion_id=suggestion.id, action="confirm", actor="ops", idempotency_key="stale-decision", expected_version=1)
        draft = replenishment.create_purchase_request_draft(db, workspace_id=workspace.id, suggestion_ids=[suggestion.id], actor="ops", idempotency_key="stale-create")
        suggestion.version += 1
        db.commit()
        with pytest.raises(ValueError, match="SUGGESTION_STALE"):
            replenishment.submit_purchase_request_draft(db, workspace_id=workspace.id, request_id=draft.id, actor="ops", idempotency_key="stale-submit", expected_version=1)
        db.rollback()
        db.expire_all()
        assert db.get(db_mod.PurchaseRequest, draft.id).status == "draft"
    finally:
        db.close()


def test_decision_idempotency_and_version_conflict(env):
    db_mod, replenishment = env
    db, workspace, sku, warehouse = _fixture(db_mod, tenant_key="version")
    try:
        suggestion = replenishment.generate_suggestion(db, workspace_id=workspace.id, warehouse_id=warehouse.id, sku_id=sku.id, actor="ops")
        confirmed = replenishment.decide_suggestion(db, workspace_id=workspace.id, suggestion_id=suggestion.id, action="confirm", actor="ops", idempotency_key="same", expected_version=1)
        replay = replenishment.decide_suggestion(db, workspace_id=workspace.id, suggestion_id=suggestion.id, action="confirm", actor="ops", idempotency_key="same", expected_version=1)
        assert replay.id == confirmed.id
        with pytest.raises(ValueError, match="IDEMPOTENCY_KEY_REUSE"):
            replenishment.decide_suggestion(db, workspace_id=workspace.id, suggestion_id=suggestion.id, action="modify", actor="ops", idempotency_key="same", decision_qty=1, reason="改数量", expected_version=1)
        with pytest.raises(ValueError, match="SUGGESTION_VERSION_CONFLICT"):
            replenishment.decide_suggestion(db, workspace_id=workspace.id, suggestion_id=suggestion.id, action="modify", actor="ops", idempotency_key="different", decision_qty=1, reason="改数量", expected_version=1)
    finally:
        db.close()

def test_ignore_requires_reason_and_workspace_isolation(env):
    db_mod, replenishment = env
    db, workspace, sku, warehouse = _fixture(db_mod, tenant_key="ignore")
    db2, workspace2, sku2, warehouse2 = _fixture(db_mod, tenant_key="other")
    try:
        suggestion = replenishment.generate_suggestion(db, workspace_id=workspace.id, warehouse_id=warehouse.id, sku_id=sku.id, actor="ops")
        with pytest.raises(ValueError, match="IGNORE_REASON_REQUIRED"):
            replenishment.decide_suggestion(db, workspace_id=workspace.id, suggestion_id=suggestion.id, action="ignore", actor="ops", idempotency_key="ignore-1", expected_version=1)
        ignored = replenishment.decide_suggestion(db, workspace_id=workspace.id, suggestion_id=suggestion.id, action="ignore", actor="ops", idempotency_key="ignore-1", reason="暂不采购", expected_version=1)
        assert ignored.status == "ignored"
        with pytest.raises(ValueError, match="SUGGESTION_NOT_FOUND"):
            replenishment.decide_suggestion(db2, workspace_id=workspace2.id, suggestion_id=suggestion.id, action="confirm", actor="ops", idempotency_key="cross", expected_version=1)
    finally:
        db.close(); db2.close()
