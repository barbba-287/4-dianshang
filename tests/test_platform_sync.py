"""Offline platform sync orchestration tests."""
from __future__ import annotations

import importlib
import json
from decimal import Decimal

import pytest
from sqlalchemy import func, select


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'sync.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    import app.config as cfg
    import app.db as db_mod
    import app.platform_sync as sync_mod
    import app.repository as repo_mod
    cfg.get_settings.cache_clear(); importlib.reload(cfg); importlib.reload(db_mod); importlib.reload(sync_mod); importlib.reload(repo_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    db = db_mod.SessionLocal()
    workspace = db_mod.Workspace(tenant_key="sync", name="同步")
    db.add(workspace); db.flush()
    return cfg, db_mod, sync_mod, repo_mod, db, workspace


def _orders(account="a", store="s", order_no="O-1"):
    return json.dumps({"records":[{"account_ref":account,"store_ref":store,"external_order_no":order_no,"status":"paid","created_at":"2026-09-01T10:00:00Z","updated_at":"2026-09-01T10:01:00Z","lines":[{"external_sku":"S-1","ordered_qty":2,"gross_amount":"20.00"}]}]})


def _inventory(account="a", store="s"):
    return json.dumps({"records":[{"account_ref":account,"store_ref":store,"external_sku":"S-1","available_qty":7,"as_of":"2026-09-01T10:00:00Z","received_at":"2026-09-01T10:01:00Z"}]})


def test_fixture_bundle_run_is_successful_and_idempotent(monkeypatch, tmp_path):
    _cfg, db_mod, sync_mod, _repo, db, workspace = _env(monkeypatch, tmp_path)
    try:
        first = sync_mod.run_fixture_sync(db, workspace_id=workspace.id, platform="jd", account_ref="a", store_ref="s", orders_content=_orders(), inventory_content=_inventory(), source_mode="json")
        assert first["run"].status == "succeeded"
        assert first["orders"].inserted == 1
        assert first["inventory"].inserted == 1
        second = sync_mod.run_fixture_sync(db, workspace_id=workspace.id, platform="jd", account_ref="a", store_ref="s", orders_content=_orders(), inventory_content=_inventory(), source_mode="json")
        assert second["orders"].no_op == 1
        assert second["inventory"].no_op == 1
        assert (db.scalar(select(func.count(db_mod.InventoryBalance.id)).where(db_mod.InventoryBalance.workspace_id == workspace.id)) or 0) == 0
    finally:
        db.close()


def test_fixture_bundle_rejects_mixed_store_and_marks_run_failed(monkeypatch, tmp_path):
    _cfg, _db_mod, sync_mod, _repo, db, workspace = _env(monkeypatch, tmp_path)
    try:
        mixed = json.dumps({"records":[json.loads(_orders()["records"][0]) if False else {"account_ref":"a","store_ref":"other","external_order_no":"O-2","status":"paid","created_at":"2026-09-01T10:00:00Z","updated_at":"2026-09-01T10:01:00Z","lines":[{"external_sku":"S-1","ordered_qty":1}]}]})
        with pytest.raises(ValueError, match="MIXED_EXTERNAL_ACCOUNT"):
            sync_mod.run_fixture_sync(db, workspace_id=workspace.id, platform="jd", account_ref="a", store_ref="s", orders_content=mixed, source_mode="json")
        run = db.query(_db_mod.ExternalSyncRun).order_by(_db_mod.ExternalSyncRun.id.desc()).first()
        assert run.status == "failed"
    finally:
        db.close()


def test_fixture_sync_marks_partial_and_keeps_successful_resource(monkeypatch, tmp_path):
    _cfg, db_mod, sync_mod, _repo, db, workspace = _env(monkeypatch, tmp_path)
    try:
        class RetryableFixtureError(RuntimeError):
            code = "TIMEOUT"
            retryable = True

        original = sync_mod.load_records
        monkeypatch.setattr(sync_mod, "load_records", lambda *args, **kwargs: (_ for _ in ()).throw(RetryableFixtureError("temporary")))
        result = sync_mod.run_fixture_sync(
            db,
            workspace_id=workspace.id,
            platform="jd",
            account_ref="a",
            store_ref="s",
            orders_content=_orders(),
            inventory_content=_inventory(),
            source_mode="json",
        )
        run = result["run"]
        assert run.status == "partial"
        statuses = json.loads(run.resource_status_json)
        assert statuses["orders"]["status"] == "succeeded"
        assert statuses["inventory"]["status"] == "failed"
        assert statuses["inventory"]["error_code"] == "TIMEOUT"
        assert json.loads(run.retryable_resources_json) == ["inventory"]
        assert db.query(db_mod.ExternalOrder).count() == 1
        assert db.query(db_mod.ExternalInventorySnapshot).count() == 0
        assert db.query(db_mod.InventoryBalance).count() == 0

        monkeypatch.setattr(sync_mod, "load_records", original)
    finally:
        db.close()


def test_fixture_sync_all_resources_failed_keeps_failed_run(monkeypatch, tmp_path):
    _cfg, _db_mod, sync_mod, _repo, db, workspace = _env(monkeypatch, tmp_path)
    try:
        class RetryableFixtureError(RuntimeError):
            code = "TIMEOUT"
            retryable = True

        monkeypatch.setattr(sync_mod, "load_orders", lambda *args, **kwargs: (_ for _ in ()).throw(RetryableFixtureError("temporary")))
        monkeypatch.setattr(sync_mod, "load_records", lambda *args, **kwargs: (_ for _ in ()).throw(RetryableFixtureError("temporary")))
        with pytest.raises(RetryableFixtureError):
            sync_mod.run_fixture_sync(
                db,
                workspace_id=workspace.id,
                platform="jd",
                account_ref="a",
                store_ref="s",
                orders_content=_orders(),
                inventory_content=_inventory(),
                source_mode="json",
            )
        run = db.query(_db_mod.ExternalSyncRun).order_by(_db_mod.ExternalSyncRun.id.desc()).first()
        assert run.status == "failed"
        assert run.finished_at is not None
        assert json.loads(run.retryable_resources_json) == ["inventory", "orders"]
    finally:
        db.close()


def test_fixture_sync_requires_resource(monkeypatch, tmp_path):
    _cfg, _db_mod, sync_mod, _repo, db, workspace = _env(monkeypatch, tmp_path)
    try:
        with pytest.raises(ValueError, match="FIXTURE_CONTENT_REQUIRED"):
            sync_mod.run_fixture_sync(db, workspace_id=workspace.id, platform="jd", account_ref="a", store_ref="s")
    finally:
        db.close()
