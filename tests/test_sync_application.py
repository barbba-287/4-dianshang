"""同步应用服务 contract 测试。"""
from __future__ import annotations

import pytest

from app.sync_application import (
    SyncApplicationError,
    SyncApplicationService,
    fixture_response_data,
    inventory_response_data,
)


class _Stats:
    total = 2
    inserted = 1
    updated = 1
    no_op = 0
    conflict = 0
    stale = 0
    snapshot_ids = [8]


class _Run:
    run_id = "run-1"
    status = "partial"


def test_inventory_service_passes_workspace_and_maps_stats(monkeypatch):
    captured = {}

    class _Run:
        run_id = "inventory-run"

    class _InventoryStats:
        total = 2
        inserted = 1
        no_op = 1
        conflict = 0
        snapshot_ids = [3]
        sync_run_id = "inventory-run"
        sync_status = "succeeded"

    def fake_load(content, **kwargs):
        captured["load"] = {"content": content, **kwargs}
        return [type("Record", (), {"account_ref": "a", "store_ref": "s", "platform": "jd"})()]

    def fake_validate(records):
        captured["records"] = records
        return "a", "s", "warehouse"

    def fake_begin(db, **kwargs):
        captured["begin"] = {"db": db, **kwargs}
        return _Run()

    def fake_ingest(db, records, **kwargs):
        captured["ingest"] = {"db": db, "records": records, **kwargs}
        return _InventoryStats()

    def fake_complete(db, run, stats):
        captured["complete"] = (db, run, stats)
        return run

    monkeypatch.setattr("app.connectors.load_records", fake_load)
    monkeypatch.setattr("app.external_sync.validate_inventory_batch_scope", fake_validate)
    monkeypatch.setattr("app.external_sync.begin_sync_run", fake_begin)
    monkeypatch.setattr("app.external_sync.ingest_inventory", fake_ingest)
    monkeypatch.setattr("app.external_sync.complete_sync_run", fake_complete)

    result = SyncApplicationService("db").ingest_inventory(
        workspace_id=9, platform="jd", source_mode="json", content="fixture"
    )

    assert captured["load"] == {"content": "fixture", "platform": "jd", "source_mode": "json"}
    assert captured["begin"]["workspace_id"] == 9
    assert captured["ingest"]["workspace_id"] == 9
    assert result["sync_run_id"] == "inventory-run"
    assert result["stats"].snapshot_ids == [3]


def test_inventory_response_data_preserves_api_shape():
    class _Stats:
        total = 2
        inserted = 1
        no_op = 1
        conflict = 0
        snapshot_ids = [3]

    result = inventory_response_data({
        "platform": "jd", "source_mode": "json", "simulated": True,
        "live_enabled": False, "stats": _Stats(),
        "sync_run_id": "run", "sync_status": "succeeded",
    })

    assert result == {
        "platform": "jd", "source_mode": "json", "simulated": True,
        "live_enabled": False, "inserted": 1, "no_op": 1, "conflict": 0,
        "total": 2, "snapshot_ids": [3], "sync_run_id": "run",
        "sync_status": "succeeded",
    }


def test_fixture_service_passes_authenticated_workspace_to_domain(monkeypatch):
    captured = {}

    def fake_run(db, **kwargs):
        captured.update(db=db, **kwargs)
        return {"run": _Run(), "orders": _Stats(), "inventory": None}

    monkeypatch.setattr("app.platform_sync.run_fixture_sync", fake_run)
    result = SyncApplicationService("db").run_fixture_bundle(
        workspace_id=7,
        platform="jd",
        account_ref="account",
        store_ref="store",
        orders_content="orders",
        source_mode="json",
    )

    assert captured == {
        "db": "db",
        "workspace_id": 7,
        "platform": "jd",
        "account_ref": "account",
        "store_ref": "store",
        "orders_content": "orders",
        "inventory_content": None,
        "source_mode": "json",
    }
    assert result["run"] is not None


def test_fixture_service_maps_stable_error_code(monkeypatch):
    def fake_run(*args, **kwargs):
        raise ValueError("FIXTURE_CONTENT_REQUIRED")

    monkeypatch.setattr("app.platform_sync.run_fixture_sync", fake_run)
    with pytest.raises(SyncApplicationError, match="FIXTURE_CONTENT_REQUIRED") as caught:
        SyncApplicationService("db").run_fixture_bundle(
            workspace_id=7,
            platform="jd",
            account_ref="account",
        )
    assert caught.value.message == "离线同步输入无效"


def test_fixture_response_data_preserves_existing_shape():
    data = fixture_response_data({"run": _Run(), "orders": _Stats(), "inventory": _Stats()})

    assert data == {
        "sync_run_id": "run-1",
        "sync_status": "partial",
        "orders": {"total": 2, "inserted": 1, "updated": 1, "no_op": 0, "conflict": 0, "stale": 0},
        "inventory": {"total": 2, "inserted": 1, "updated": 1, "no_op": 0, "conflict": 0, "stale": 0, "snapshot_ids": [8]},
    }


def test_fixture_service_requires_workspace_before_domain_call(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("domain must not run")

    monkeypatch.setattr("app.platform_sync.run_fixture_sync", fail_if_called)
    with pytest.raises(SyncApplicationError, match="WORKSPACE_CONTEXT_REQUIRED"):
        SyncApplicationService("db").run_fixture_bundle(
            workspace_id=0,
            platform="jd",
            account_ref="account",
        )
