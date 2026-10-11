"""内容导出 API 回归测试。"""
from __future__ import annotations

import importlib
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'content-export.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    import app.config as cfg
    import app.db as db_mod
    import app.background as bg_mod
    import app.main as main_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(bg_mod)
    importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    from app.employee_auth import hash_password
    from app.repository import upsert_product
    from app.schemas import ProductRecord

    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="content-export", name="内容导出")
        db.add(workspace)
        db.flush()
        operator = db_mod.UserAccount(login="operator", password_hash=hash_password("operator-password"), display_name="运营")
        db.add(operator)
        db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=operator.id, role="operations"))
        readonly = db_mod.UserAccount(login="readonly", password_hash=hash_password("readonly-password"), display_name="只读")
        db.add(readonly)
        db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=readonly.id, role="readonly"))

        workspace_b = db_mod.Workspace(tenant_key="content-export-b", name="内容导出B")
        db.add(workspace_b)
        db.flush()

        product = upsert_product(
            db,
            ProductRecord(
                source="fixture",
                external_product_id="export-product",
                title="导出商品",
                url="https://fixture/export",
                current_price=Decimal("10.00"),
                observed_at=datetime.utcnow(),
            ),
            workspace_id=workspace.id,
        )
        db.flush()
        revision = db_mod.ProductContentRevision(
            workspace_id=workspace.id,
            product_id=product.id,
            revision_no=1,
            status="approved",
            channel="generic",
            content_json='{"title":"导出商品","selling_points":["快","好"]}',
            source_snapshot_json="{}",
            source_snapshot_hash="snapshot",
            provider="local",
            source_mode="local",
            simulated=True,
            quality_status="passed",
            version=1,
            idempotency_key="generation-key",
            payload_hash="generation-hash",
        )
        db.add(revision)
        db.flush()

        product_b = upsert_product(
            db,
            ProductRecord(
                source="fixture",
                external_product_id="export-product-b",
                title="B商品",
                url="https://fixture/export-b",
                current_price=Decimal("10.00"),
                observed_at=datetime.utcnow(),
            ),
            workspace_id=workspace_b.id,
        )
        db.flush()
        revision_b = db_mod.ProductContentRevision(
            workspace_id=workspace_b.id,
            product_id=product_b.id,
            revision_no=1,
            status="approved",
            channel="generic",
            content_json='{"title":"B商品"}',
            source_snapshot_json="{}",
            source_snapshot_hash="snapshot-b",
            provider="local",
            source_mode="local",
            simulated=True,
            quality_status="passed",
            version=1,
            idempotency_key="generation-key-b",
            payload_hash="generation-hash-b",
        )
        db.add(revision_b)
        db.commit()
        workspace_id, revision_id = workspace.id, revision.id
        workspace_b_id, revision_b_id = workspace_b.id, revision_b.id

    test_client = TestClient(main_mod.app)
    login = test_client.post(
        "/login",
        data={"login": "operator", "password": "operator-password", "next": "/content"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    csrf = test_client.cookies.get("dianshang_csrf")
    assert csrf
    test_client.headers.update({"X-CSRF-Token": csrf})
    try:
        yield test_client, db_mod, bg_mod, workspace_id, revision_id, workspace_b_id, revision_b_id
    finally:
        bg_mod.reset_executor()
        cfg.get_settings.cache_clear()


def _export(client, revision_id, key, fmt="json"):
    c = client[0]
    return c.post(
        f"/api/content/revisions/{revision_id}/export",
        json={"revision_id": revision_id, "format": fmt},
        headers={"Idempotency-Key": key},
    )


def test_export_requires_approved_revision_and_is_idempotent(client):
    c, db_mod, _bg, _wa, revision_id, _wb, _rb = client
    payload = {"revision_id": revision_id, "format": "json"}
    first = c.post(
        f"/api/content/revisions/{revision_id}/export",
        json=payload,
        headers={"Idempotency-Key": "export-api-1"},
    )
    assert first.status_code == 202, first.text
    body = first.json()
    assert body["status"] == "queued"
    replay = c.post(
        f"/api/content/revisions/{revision_id}/export",
        json=payload,
        headers={"Idempotency-Key": "export-api-1"},
    )
    assert replay.status_code == 202
    assert replay.json()["id"] == body["id"]
    with db_mod.SessionLocal() as db:
        assert db.query(db_mod.ProductContentExport).count() == 1
        assert db.query(db_mod.CrawlJob).filter(db_mod.CrawlJob.type == "content_export").count() == 1


def test_export_rejects_mismatch_and_unapproved_without_side_effect(client):
    c, db_mod, _bg, workspace_id, revision_id, _wb, _rb = client
    mismatch = c.post(
        f"/api/content/revisions/{revision_id}/export",
        json={"revision_id": revision_id + 1, "format": "json"},
        headers={"Idempotency-Key": "export-mismatch"},
    )
    assert mismatch.status_code == 422
    with db_mod.SessionLocal() as db:
        revision = db.get(db_mod.ProductContentRevision, revision_id)
        revision.status = "draft"
        db.commit()
    blocked = c.post(
        f"/api/content/revisions/{revision_id}/export",
        json={"revision_id": revision_id, "format": "json"},
        headers={"Idempotency-Key": "export-draft"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "CONTENT_NOT_APPROVED"
    with db_mod.SessionLocal() as db:
        assert db.query(db_mod.ProductContentExport).filter(
            db_mod.ProductContentExport.workspace_id == workspace_id
        ).count() == 0


def test_export_contract_fields(client):
    _c, _db, _bg, _wa, revision_id, _wb, _rb = client
    resp = _export(client, revision_id, "export-contract")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    expected_fields = {
        "id", "revision_id", "format", "status", "artifact_uri",
        "artifact_sha256", "artifact_size", "simulated", "created_at", "finished_at",
    }
    assert set(body.keys()) == expected_fields
    assert body["revision_id"] == revision_id
    assert body["format"] == "json"
    assert body["status"] == "queued"
    assert body["simulated"] is True


def test_export_executes_and_succeeds(client):
    _c, db_mod, bg_mod, _wa, revision_id, _wb, _rb = client
    resp = _export(client, revision_id, "export-exec")
    assert resp.status_code == 202
    export_id = resp.json()["id"]
    bg_mod.wait_for_jobs()
    with db_mod.SessionLocal() as db:
        export = db.get(db_mod.ProductContentExport, export_id)
        assert export.status == "succeeded"
        assert export.artifact_sha256
        assert export.artifact_size
        assert export.manifest_json


def test_export_get_same_workspace(client):
    c, _db, _bg, _wa, revision_id, _wb, _rb = client
    export_id = _export(client, revision_id, "export-get").json()["id"]
    same = c.get(f"/api/content/exports/{export_id}")
    assert same.status_code == 200
    assert same.json()["id"] == export_id


def test_export_get_cross_workspace_returns_404(client):
    c, db_mod, _bg, _wa, _ra, workspace_b_id, revision_b_id = client
    with db_mod.SessionLocal() as db:
        export_b = db_mod.ProductContentExport(
            workspace_id=workspace_b_id,
            revision_id=revision_b_id,
            format="json",
            status="queued",
            simulated=True,
            idempotency_key="export-b-key",
            payload_hash="hash-b",
            created_by="other",
        )
        db.add(export_b)
        db.commit()
        export_b_id = export_b.id
    cross = c.get(f"/api/content/exports/{export_b_id}")
    assert cross.status_code == 404


def test_export_requires_idempotency_key(client):
    c, _db, _bg, _wa, revision_id, _wb, _rb = client
    resp = c.post(
        f"/api/content/revisions/{revision_id}/export",
        json={"revision_id": revision_id, "format": "json"},
    )
    assert resp.status_code == 422


def test_export_rejects_overlong_idempotency_key(client):
    _c, _db, _bg, _wa, revision_id, _wb, _rb = client
    resp = _export(client, revision_id, "k" * 129)
    assert resp.status_code == 422


def test_export_rejects_missing_revision(client):
    resp = _export(client, 999999, "export-missing")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "CONTENT_NOT_APPROVED"


def test_export_rejects_invalid_format(client):
    _c, _db, _bg, _wa, revision_id, _wb, _rb = client
    resp = _export(client, revision_id, "export-fmt", fmt="xml")
    assert resp.status_code == 422


def test_export_cross_workspace_revision_returns_conflict(client):
    _c, db_mod, _bg, workspace_id, _ra, _wb, revision_b_id = client
    resp = _export(client, revision_b_id, "export-cross")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "CONTENT_NOT_APPROVED"
    with db_mod.SessionLocal() as db:
        assert db.query(db_mod.ProductContentExport).filter(
            db_mod.ProductContentExport.workspace_id == workspace_id
        ).count() == 0


def test_export_forbidden_for_readonly_role(client):
    c, _db, _bg, _wa, revision_id, _wb, _rb = client
    login = c.post(
        "/login",
        data={"login": "readonly", "password": "readonly-password", "next": "/content"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    c.headers.update({"X-CSRF-Token": c.cookies.get("dianshang_csrf")})
    resp = _export(client, revision_id, "export-readonly")
    assert resp.status_code == 403


def test_export_key_reuse_different_format(client):
    _c, db_mod, _bg, _wa, revision_id, _wb, _rb = client
    first = _export(client, revision_id, "export-reuse", fmt="json")
    assert first.status_code == 202
    second = _export(client, revision_id, "export-reuse", fmt="markdown")
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSE"
    with db_mod.SessionLocal() as db:
        assert db.query(db_mod.ProductContentExport).count() == 1


def test_export_new_key_creates_second_export(client):
    _c, db_mod, _bg, _wa, revision_id, _wb, _rb = client
    assert _export(client, revision_id, "export-new-1").status_code == 202
    assert _export(client, revision_id, "export-new-2").status_code == 202
    with db_mod.SessionLocal() as db:
        assert db.query(db_mod.ProductContentExport).count() == 2


def test_export_background_rechecks_approval(client, monkeypatch):
    _c, db_mod, _bg, _wa, revision_id, _wb, _rb = client
    import app.background as bg

    monkeypatch.setattr(bg, "_submit_future", lambda job_id: None)
    resp = _export(client, revision_id, "export-recheck")
    assert resp.status_code == 202
    export_id = resp.json()["id"]
    with db_mod.SessionLocal() as db:
        revision = db.get(db_mod.ProductContentRevision, revision_id)
        revision.status = "draft"
        db.commit()
    from app.content_production import ContentProviderError, execute_export_job

    with db_mod.SessionLocal() as db:
        job = db.query(db_mod.CrawlJob).filter(db_mod.CrawlJob.type == "content_export").first()
        with pytest.raises(ContentProviderError) as exc:
            execute_export_job(db, job, cancel_check=lambda: False)
        assert exc.value.code == "CONTENT_NOT_APPROVED"
        export = db.get(db_mod.ProductContentExport, export_id)
        assert export.status == "queued"


def test_export_artifact_hash_reproducible(client):
    _c, db_mod, bg_mod, _wa, revision_id, _wb, _rb = client
    a = _export(client, revision_id, "export-repro-a").json()["id"]
    b = _export(client, revision_id, "export-repro-b").json()["id"]
    bg_mod.wait_for_jobs()
    with db_mod.SessionLocal() as db:
        export_a = db.get(db_mod.ProductContentExport, a)
        export_b = db.get(db_mod.ProductContentExport, b)
        assert export_a.status == "succeeded"
        assert export_b.status == "succeeded"
        assert export_a.artifact_sha256 == export_b.artifact_sha256


def test_export_unauthenticated_returns_401(client):
    _c, db_mod, bg_mod, _wa, revision_id, _wb, _rb = client
    from fastapi.testclient import TestClient

    import app.main as main_mod

    anon = TestClient(main_mod.app)
    resp = anon.post(
        f"/api/content/revisions/{revision_id}/export",
        json={"revision_id": revision_id, "format": "json"},
        headers={"Idempotency-Key": "export-anon"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "UNAUTHORIZED"
