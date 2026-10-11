"""内容应用服务契约测试（导出与审核）。"""
from __future__ import annotations

import hashlib
import importlib
import json
from datetime import datetime
from decimal import Decimal

import pytest
import sqlalchemy.exc

from app.content_application import ContentApplicationError, ContentApplicationService


def _body_hash(revision_id: int, fmt: str) -> str:
    """导出路径沿用旧哈希算法（默认 json.dumps 分隔符）。"""
    return hashlib.sha256(
        json.dumps({"revision_id": revision_id, "format": fmt}, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


# ============================================================
# 0. review_revision 覆盖（恢复原有 + 补键校验分支）
# ============================================================

def test_review_requires_idempotency_key_before_mutation(monkeypatch):
    """review_revision：版本不存在 → CONTENT_REVISION_NOT_FOUND（恢复原有用例）。"""
    import app.content_application as ca

    class _Db:
        def scalar(self, query):
            return None

    service = ca.ContentApplicationService(_Db())
    with pytest.raises(ca.ContentApplicationError, match="CONTENT_REVISION_NOT_FOUND"):
        service.review_revision(
            workspace_id=1, revision_id=1, action="submit", note=None,
            expected_version=1, idempotency_key="", actor="operator",
        )


def test_review_requires_idempotency_key():
    """review_revision：键缺失 → IDEMPOTENCY_KEY_REQUIRED（真正命中键校验分支）。"""
    import app.content_application as ca

    class _Revision:
        version = 1
        status = "draft"
        quality_status = "passed"

    class _Db:
        def scalar(self, query):
            return _Revision()

    service = ca.ContentApplicationService(_Db())
    with pytest.raises(ca.ContentApplicationError, match="IDEMPOTENCY_KEY_REQUIRED"):
        service.review_revision(
            workspace_id=1, revision_id=1, action="submit", note=None,
            expected_version=1, idempotency_key="", actor="operator",
        )


# ============================================================
# 1. 哈希兼容（保留现有用例）
# ============================================================

def test_export_uses_legacy_payload_hash_and_queues_job(monkeypatch):
    captured = {}

    class _Revision:
        id = 7
        simulated = True

    class _Export:
        id = 11

    class _Db:
        def __init__(self):
            self.scalar_calls = 0

        def scalar(self, query):
            self.scalar_calls += 1
            return _Revision() if self.scalar_calls == 1 else None

        def add(self, value):
            captured["export"] = value

        def flush(self):
            pass

        def commit(self):
            pass

        def refresh(self, value):
            pass

    def fake_enqueue(db, **kwargs):
        captured["job"] = kwargs
        return type("Job", (), {"id": 12})()

    monkeypatch.setattr("app.content_application.ProductContentRevision.status", "approved", raising=False)
    monkeypatch.setattr("app.content_application.enqueue_job", fake_enqueue)
    # Avoid SQLAlchemy expression evaluation in this minimal service contract fake.
    service = ContentApplicationService(_Db())
    service.db.scalar = lambda query: (_Revision() if not captured.get("looked_up_existing") else None)
    original_scalar = service.db.scalar

    def scalar(query):
        if not captured.get("looked_up_existing"):
            captured["looked_up_existing"] = True
            return _Revision()
        return None

    service.db.scalar = scalar
    result = service.export_revision(
        workspace_id=1,
        revision_id=7,
        request_revision_id=7,
        export_format="json",
        idempotency_key="export-key",
        actor="operator",
    )
    expected = hashlib.sha256(
        json.dumps({"revision_id": 7, "format": "json"}, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    assert captured["export"].payload_hash == expected
    assert captured["job"]["idempotency_key"] == "export:export-key"
    assert result["replayed"] is False


# ============================================================
# 2. T4 并发竞态（极简 fake，精确控制 scalar 序列）
# ============================================================

class _Revision:
    id = 7
    simulated = True


def _fake_db(sequence, *, flush_raises=False):
    """构造一个按 sequence 依次返回 scalar 结果的极简 session。

    flush_raises=True 时 flush() 抛 IntegrityError，模拟非 deferrable 唯一约束
    在 flush 阶段（而非 enqueue_job 的 commit）立即触发冲突的场景。
    """
    calls = []

    class _Db:
        def __init__(self):
            self.rolled_back = False

        def scalar(self, query):
            calls.append("scalar")
            idx = len(calls) - 1
            return sequence[idx] if idx < len(sequence) else None

        def add(self, value):
            pass

        def flush(self):
            if flush_raises:
                raise sqlalchemy.exc.IntegrityError("UNIQUE constraint failed", {}, None)

        def rollback(self):
            self.rolled_back = True

        def commit(self):
            pass

        def refresh(self, value):
            pass

    return _Db()


def _racing_enqueue(monkeypatch):
    def raise_integrity(db, **kwargs):
        raise sqlalchemy.exc.IntegrityError("UNIQUE constraint failed", {}, None)

    monkeypatch.setattr("app.content_application.enqueue_job", raise_integrity)


def test_export_concurrent_same_key_replays(monkeypatch):
    """T4：并发同键命中唯一约束且哈希一致 → 重放，不 500。"""
    competing = type("Export", (), {"id": 99, "payload_hash": _body_hash(7, "json")})()
    db = _fake_db([_Revision(), None, competing])
    _racing_enqueue(monkeypatch)
    result = ContentApplicationService(db).export_revision(
        workspace_id=1, revision_id=7, request_revision_id=7,
        export_format="json", idempotency_key="k", actor="op",
    )
    assert result["replayed"] is True
    assert result["export"].id == 99
    assert db.rolled_back is True


def test_export_concurrent_key_reuse_different_hash(monkeypatch):
    """T4：并发同键命中唯一约束且哈希不同 → IDEMPOTENCY_KEY_REUSE，不 500。"""
    competing = type("Export", (), {"id": 99, "payload_hash": "different"})()
    db = _fake_db([_Revision(), None, competing])
    _racing_enqueue(monkeypatch)
    with pytest.raises(ContentApplicationError) as exc:
        ContentApplicationService(db).export_revision(
            workspace_id=1, revision_id=7, request_revision_id=7,
            export_format="json", idempotency_key="k", actor="op",
        )
    assert exc.value.code == "IDEMPOTENCY_KEY_REUSE"


def test_export_concurrent_persist_failed(monkeypatch):
    """T4：并发冲突且重查无竞争行 → 稳定错误，不 500。"""
    db = _fake_db([_Revision(), None, None])
    _racing_enqueue(monkeypatch)
    with pytest.raises(ContentApplicationError) as exc:
        ContentApplicationService(db).export_revision(
            workspace_id=1, revision_id=7, request_revision_id=7,
            export_format="json", idempotency_key="k", actor="op",
        )
    assert exc.value.code == "CONTENT_EXPORT_PERSIST_FAILED"


def test_export_concurrent_flush_integrity_error_replays(monkeypatch):
    """T4：唯一约束在 flush 阶段立即触发 → 同样走重放，不 500。"""
    competing = type("Export", (), {"id": 99, "payload_hash": _body_hash(7, "json")})()
    db = _fake_db([_Revision(), None, competing], flush_raises=True)
    result = ContentApplicationService(db).export_revision(
        workspace_id=1, revision_id=7, request_revision_id=7,
        export_format="json", idempotency_key="k", actor="op",
    )
    assert result["replayed"] is True
    assert result["export"].id == 99
    assert db.rolled_back is True


# ============================================================
# 3. 真实 session 服务级测试（脱离 FastAPI）
# ============================================================

def _reload_content_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'content-app.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    import app.config as cfg
    import app.db as db_mod
    import app.content_application as ca

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(ca)
    db_mod.Base.metadata.create_all(db_mod.engine)
    return db_mod, ca


@pytest.fixture
def content_env(tmp_path, monkeypatch):
    db_mod, ca = _reload_content_env(monkeypatch, tmp_path)
    from app.repository import upsert_product
    from app.schemas import ProductRecord

    def _make_workspace(tenant_key):
        db = db_mod.SessionLocal()
        workspace = db_mod.Workspace(tenant_key=tenant_key, name=tenant_key)
        db.add(workspace)
        db.flush()
        product = upsert_product(
            db,
            ProductRecord(
                source="fixture",
                external_product_id=f"p-{tenant_key}",
                title="商品",
                url=f"https://fixture/{tenant_key}",
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
            content_json='{"title":"商品","selling_points":["快","好"]}',
            source_snapshot_json="{}",
            source_snapshot_hash="snapshot",
            provider="local",
            source_mode="local",
            simulated=True,
            quality_status="passed",
            version=1,
            idempotency_key=f"generation-{tenant_key}",
            payload_hash="generation-hash",
        )
        db.add(revision)
        db.commit()
        wid, rid = workspace.id, revision.id
        db.close()
        return wid, rid

    workspace_a, revision_a = _make_workspace("svc-a")
    workspace_b, revision_b = _make_workspace("svc-b")
    yield db_mod, ca, workspace_a, revision_a, workspace_b, revision_b
    db_mod.engine.dispose()


def _export_count(db, db_mod, workspace_id):
    return db.query(db_mod.ProductContentExport).filter(
        db_mod.ProductContentExport.workspace_id == workspace_id
    ).count()


def test_export_revision_creates_export_and_job(content_env):
    db_mod, ca, wid, rid, _, _ = content_env
    db = db_mod.SessionLocal()
    try:
        result = ca.ContentApplicationService(db).export_revision(
            workspace_id=wid, revision_id=rid, request_revision_id=rid,
            export_format="json", idempotency_key="k1", actor="op",
        )
        assert result["replayed"] is False
        assert result["export"].id is not None
        assert result["export"].status == "queued"
        assert result["export"].simulated is True
        assert result["job"].type == "content_export"
        assert result["job"].run_id
    finally:
        db.close()


def test_export_revision_requires_idempotency_key(content_env):
    db_mod, ca, wid, rid, _, _ = content_env
    db = db_mod.SessionLocal()
    try:
        with pytest.raises(ca.ContentApplicationError) as exc:
            ca.ContentApplicationService(db).export_revision(
                workspace_id=wid, revision_id=rid, request_revision_id=rid,
                export_format="json", idempotency_key="", actor="op",
            )
        assert exc.value.code == "IDEMPOTENCY_KEY_REQUIRED"
    finally:
        db.close()


def test_export_revision_rejects_mismatch(content_env):
    db_mod, ca, wid, rid, _, _ = content_env
    db = db_mod.SessionLocal()
    try:
        with pytest.raises(ca.ContentApplicationError) as exc:
            ca.ContentApplicationService(db).export_revision(
                workspace_id=wid, revision_id=rid, request_revision_id=rid + 1,
                export_format="json", idempotency_key="n2", actor="op",
            )
        assert exc.value.code == "REVISION_MISMATCH"
    finally:
        db.close()


def test_export_revision_rejects_unapproved(content_env):
    db_mod, ca, wid, rid, _, _ = content_env
    db = db_mod.SessionLocal()
    try:
        revision = db.get(db_mod.ProductContentRevision, rid)
        revision.status = "draft"
        db.commit()
        with pytest.raises(ca.ContentApplicationError) as exc:
            ca.ContentApplicationService(db).export_revision(
                workspace_id=wid, revision_id=rid, request_revision_id=rid,
                export_format="json", idempotency_key="n1", actor="op",
            )
        assert exc.value.code == "CONTENT_NOT_APPROVED"
        assert _export_count(db, db_mod, wid) == 0
    finally:
        db.close()


def test_export_revision_is_idempotent(content_env):
    db_mod, ca, wid, rid, _, _ = content_env
    db = db_mod.SessionLocal()
    try:
        svc = ca.ContentApplicationService(db)
        first = svc.export_revision(
            workspace_id=wid, revision_id=rid, request_revision_id=rid,
            export_format="json", idempotency_key="r1", actor="op",
        )
        second = svc.export_revision(
            workspace_id=wid, revision_id=rid, request_revision_id=rid,
            export_format="json", idempotency_key="r1", actor="op",
        )
        assert first["replayed"] is False
        assert second["replayed"] is True
        assert second["export"].id == first["export"].id
        assert _export_count(db, db_mod, wid) == 1
        job_count = db.query(db_mod.CrawlJob).filter(
            db_mod.CrawlJob.workspace_id == wid,
            db_mod.CrawlJob.type == "content_export",
        ).count()
        assert job_count == 1
    finally:
        db.close()


def test_export_revision_rejects_key_reuse(content_env):
    db_mod, ca, wid, rid, _, _ = content_env
    db = db_mod.SessionLocal()
    try:
        svc = ca.ContentApplicationService(db)
        svc.export_revision(
            workspace_id=wid, revision_id=rid, request_revision_id=rid,
            export_format="json", idempotency_key="r2", actor="op",
        )
        with pytest.raises(ca.ContentApplicationError) as exc:
            svc.export_revision(
                workspace_id=wid, revision_id=rid, request_revision_id=rid,
                export_format="markdown", idempotency_key="r2", actor="op",
            )
        assert exc.value.code == "IDEMPOTENCY_KEY_REUSE"
    finally:
        db.close()


def test_export_revision_new_key_creates_second_export(content_env):
    db_mod, ca, wid, rid, _, _ = content_env
    db = db_mod.SessionLocal()
    try:
        svc = ca.ContentApplicationService(db)
        svc.export_revision(
            workspace_id=wid, revision_id=rid, request_revision_id=rid,
            export_format="json", idempotency_key="r4-a", actor="op",
        )
        svc.export_revision(
            workspace_id=wid, revision_id=rid, request_revision_id=rid,
            export_format="json", idempotency_key="r4-b", actor="op",
        )
        assert _export_count(db, db_mod, wid) == 2
    finally:
        db.close()


def test_export_revision_cross_workspace_revision_returns_not_approved(content_env):
    db_mod, ca, wid_a, _rid_a, _wid_b, rid_b = content_env
    db = db_mod.SessionLocal()
    try:
        with pytest.raises(ca.ContentApplicationError) as exc:
            ca.ContentApplicationService(db).export_revision(
                workspace_id=wid_a, revision_id=rid_b, request_revision_id=rid_b,
                export_format="json", idempotency_key="cross", actor="op",
            )
        assert exc.value.code == "CONTENT_NOT_APPROVED"
        assert _export_count(db, db_mod, wid_a) == 0
    finally:
        db.close()


def test_export_enqueue_failure_leaves_no_export_row(content_env, monkeypatch):
    """F1：enqueue_job 抛非 IntegrityError 异常 → 异常原样冒泡，export 行不落库。"""
    db_mod, ca, wid, rid, _, _ = content_env

    def fail_enqueue(db, **kwargs):
        raise RuntimeError("job enqueue failed")

    monkeypatch.setattr(ca, "enqueue_job", fail_enqueue)
    db = db_mod.SessionLocal()
    try:
        with pytest.raises(RuntimeError):
            ca.ContentApplicationService(db).export_revision(
                workspace_id=wid, revision_id=rid, request_revision_id=rid,
                export_format="json", idempotency_key="f1", actor="op",
            )
        db.rollback()
        assert _export_count(db, db_mod, wid) == 0
    finally:
        db.close()
