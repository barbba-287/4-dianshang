"""P3-S3 知识文档入库测试：谱系（D1）、发布闸（D4）、发布/撤回（D3）、权限（D6）。

用例编号与任务文件 `dev_task.p3s3-knowledge-doc.md` §6 一一对应：K-1..K-12。

夹具模式沿用 `tests/test_api_documents.py`（tmp_path + monkeypatch env + reload +
`wait_for_jobs`），额外把 `VECTOR_STORE_PATH` 指向 tmp（隔离默认 `data/vectors.json`）
并把 `RAG_MIN_SCORE` 压到 0.0（HashEmbedder 场景下稳定命中）。
"""

import importlib
import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.background import reset_executor, wait_for_jobs
from app.rag_runtime import reset_instances


STAFF_ROLES = ("admin", "operations", "customer_service", "readonly", "warehouse")


def _make_pdf_bytes(pages: list[str]) -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for text in pages:
        c.drawString(72, 720, text)
        c.showPage()
    c.save()
    return buf.getvalue()


def _make_docx_bytes(paragraphs: list[str]) -> bytes:
    from docx import Document as DocxDocument

    doc = DocxDocument()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _bootstrap(tmp_path, monkeypatch, *, employee_auth: bool):
    """统一的环境隔离 + 模块 reload，返回 (db_mod, main_mod)。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IMPORTS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("WORKER_LEASE_SECONDS", "30")
    monkeypatch.setenv("VECTOR_STORE_PATH", str(tmp_path / "vectors.json"))
    # 降低阈值让 hash embedder 测试更稳定；生产用真实 embedding 时应 >= 0.5
    monkeypatch.setenv("RAG_MIN_SCORE", "0.0")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true" if employee_auth else "false")
    monkeypatch.setenv("API_AUTH_ENABLED", "false")

    import app.config as cfg
    import app.db as db_mod
    import app.repository as repo_mod
    import app.background as bg_mod
    import app.main as main_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(repo_mod)
    importlib.reload(bg_mod)
    importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    reset_executor()
    reset_instances()
    return db_mod, main_mod


def _teardown() -> None:
    reset_executor()
    reset_instances()
    import app.config as cfg
    import app.rag_runtime as rt_mod

    cfg.get_settings.cache_clear()
    # rag_runtime 从不被 _bootstrap reload，其 `from app.config import get_settings`
    # 是首循环的死绑定：不清它，lru_cache 里旧循环的 settings（含旧 VECTOR_STORE_PATH）
    # 会存活到后续测试文件，后继循环的向量库实例 _load() 旧持久化文件 → 跨文件向量泄漏
    # （实测：K1+K9 后 rag 的 no_evidence 命中 K9 的 v2 向量，确定性复现）。
    rt_mod.get_settings.cache_clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """服务通道（demo/api_key）：上传 / 解析 / 检索主链路。"""
    _db_mod, main_mod = _bootstrap(tmp_path, monkeypatch, employee_auth=False)
    with TestClient(main_mod.app) as test_client:
        yield test_client
    _teardown()


@pytest.fixture
def staff(tmp_path, monkeypatch):
    """员工会话通道：各角色账号（admin/operations/customer_service/readonly/warehouse）。"""
    db_mod, main_mod = _bootstrap(tmp_path, monkeypatch, employee_auth=True)
    from app.employee_auth import hash_password

    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="p3s3-staff", name="P3S3 测试商家")
        db.add(workspace)
        db.flush()
        for role in STAFF_ROLES:
            user = db_mod.UserAccount(
                login=f"p3s3-{role}",
                password_hash=hash_password("password-123"),
                display_name=f"P3S3 {role}",
            )
            db.add(user)
            db.flush()
            db.add(
                db_mod.WorkspaceMembership(
                    workspace_id=workspace.id, user_id=user.id, role=role
                )
            )
        db.commit()

    with TestClient(main_mod.app) as test_client:
        yield test_client
    _teardown()


def _login(client: TestClient, login: str, password: str = "password-123") -> None:
    response = client.post(
        "/login",
        data={"login": login, "password": password, "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    csrf = client.cookies.get("dianshang_csrf")
    assert csrf
    client.headers.update({"X-CSRF-Token": csrf})


def _upload_pdf(client: TestClient, filename: str, text: str) -> dict:
    pdf = _make_pdf_bytes([text])
    response = client.post(
        "/api/documents",
        files={"file": (filename, pdf, "application/pdf")},
        data={"title": filename},
    )
    assert response.status_code == 202, response.text
    return response.json()


def _workspace_id(db_mod) -> int:
    with db_mod.SessionLocal() as db:
        return db.scalar(select(db_mod.Workspace.id).order_by(db_mod.Workspace.id))


def _make_raw_version(db_mod, *, workspace_id: int, status: str) -> tuple[int, int]:
    """构造一个**没有入队 job** 的版本（用于发布/撤回前置用例）。"""
    with db_mod.SessionLocal() as db:
        document = db_mod.Document(
            workspace_id=workspace_id,
            source_type="pdf",
            title="pending",
            filename="pending.pdf",
            sha256="b" * 63 + "1",
        )
        db.add(document)
        db.flush()
        version = db_mod.DocumentVersion(
            workspace_id=workspace_id,
            document_id=document.id,
            version_no=1,
            sha256="b" * 63 + "1",
            size_bytes=42,
            storage_uri="pdf/pending.pdf",
            status=status,
        )
        db.add(version)
        db.commit()
        return document.id, version.id


# ---------- K-1..K-12 ----------


def test_k1_upload_regression_still_works(client):
    """K-1 正常上传回归：202 → parsing → wait → ready + chunk_count≥1。"""
    body = _upload_pdf(client, "manual.pdf", "Page1 tea green tea 250g")
    assert body["version_no"] == 1
    assert body["status"] == "parsing"

    wait_for_jobs(timeout=10)
    versions = client.get(f"/api/documents/{body['document_id']}/versions").json()
    assert versions[0]["status"] == "ready"
    assert versions[0]["chunk_count"] >= 1


def test_k2_oversize_upload_returns_413(client, monkeypatch):
    """K-2 超限拦截：413 FILE_TOO_LARGE（回归既有语义）。"""
    # 照抄 test_api_documents.py::test_oversize_file_returns_413 的成熟写法：
    # setenv + cache_clear + reload(storage/main)。storage 绑定的 get_settings 是
    # from-import 快照，不 reload 的话 conftest 的每测试 cache_clear 够不到它，
    # 50 会残留在该对象缓存里污染后续所有走 storage 的用例（K4 起全挂的根因）。
    import importlib

    import app.config as cfg
    import app.storage as storage_mod
    import app.main as main_mod

    monkeypatch.setenv("UPLOAD_MAX_BYTES", "50")
    cfg.get_settings.cache_clear()
    importlib.reload(storage_mod)
    importlib.reload(main_mod)
    assert cfg.get_settings().upload_max_bytes == 50

    pdf = _make_pdf_bytes(["placeholder text"])
    response = client.post(
        "/api/documents",
        files={"file": ("manual.pdf", pdf, "application/pdf")},
        data={"title": "big"},
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "FILE_TOO_LARGE"


def test_k3_fake_extension_and_fake_signature_rejected(client):
    """K-3 假后缀 / 假内容拦截。"""
    exe = client.post(
        "/api/documents",
        files={"file": ("bad.exe", b"MZ fake", "application/octet-stream")},
        data={"title": "bad"},
    )
    assert exe.status_code == 415
    assert exe.json()["detail"]["code"] == "UNSUPPORTED_TYPE"

    fake = client.post(
        "/api/documents",
        files={"file": ("x.pdf", b"not a real pdf", "application/pdf")},
        data={"title": "fake"},
    )
    assert fake.status_code == 415
    assert fake.json()["detail"]["code"] == "SIGNATURE_MISMATCH"


def test_k4_same_filename_new_content_creates_version_chain(client):
    """K-4 版本演进：同文件名新内容 → 同 document、version_no=2、v1 保留。"""
    first = _upload_pdf(client, "manual.pdf", "v1 tea green tea 250g")
    wait_for_jobs(timeout=10)
    second = _upload_pdf(client, "manual.pdf", "v2 oolong tea 500g")
    wait_for_jobs(timeout=10)

    assert second["document_id"] == first["document_id"]
    assert second["version_no"] == 2

    versions = client.get(f"/api/documents/{first['document_id']}/versions").json()
    assert [item["version_no"] for item in versions] == [2, 1]
    assert versions[1]["status"] == "ready"  # 旧版本保留且状态不变

    listing = client.get("/api/documents").json()
    assert listing["total"] == 1  # Document 行数不增


def test_k5_publish_then_retrievable(client):
    """K-5 发布后检索生效：publish → wait → rag_query 命中该 version。"""
    body = _upload_pdf(client, "manual.pdf", "premium green tea leaves spring harvest")
    wait_for_jobs(timeout=10)

    published = client.post(
        f"/api/documents/{body['document_id']}/versions/{body['version_id']}/publish"
    )
    assert published.status_code == 200, published.text
    assert published.json()["indexing_queued"] is True
    wait_for_jobs(timeout=10)

    answer = client.post("/api/rag/query", json={"query": "green tea"}).json()
    assert answer["no_answer"] is False
    version_ids = {item["document_version_id"] for item in answer["citations"]}
    assert body["version_id"] in version_ids


def test_k6_unpublish_removes_from_retrieval(client):
    """K-6 下线后不参与检索：unpublish → rag_query 不再命中该 version。"""
    body = _upload_pdf(client, "manual.pdf", "premium green tea leaves spring harvest")
    wait_for_jobs(timeout=10)
    client.post(
        f"/api/documents/{body['document_id']}/versions/{body['version_id']}/publish"
    )
    wait_for_jobs(timeout=10)
    assert client.post("/api/rag/query", json={"query": "green tea"}).json()["citations"]

    withdrawn = client.post(
        f"/api/documents/{body['document_id']}/versions/{body['version_id']}/unpublish"
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["removed_vectors"] >= 1

    answer = client.post("/api/rag/query", json={"query": "green tea"}).json()
    version_ids = {item["document_version_id"] for item in answer["citations"]}
    assert body["version_id"] not in version_ids


def test_k7_unpublished_version_is_not_retrievable(client):
    """K-7 未发布不可检索（D4 闸实锤）：ready 未发布 → rag_query 零命中。"""
    body = _upload_pdf(client, "manual.pdf", "premium green tea leaves spring harvest")
    wait_for_jobs(timeout=10)

    versions = client.get(f"/api/documents/{body['document_id']}/versions").json()
    assert versions[0]["status"] == "ready"
    assert versions[0]["published_at"] is None

    answer = client.post("/api/rag/query", json={"query": "green tea"}).json()
    version_ids = {item["document_version_id"] for item in answer["citations"]}
    assert body["version_id"] not in version_ids
    assert answer["no_answer"] is True


def test_k8_publish_requires_ready_state(client, tmp_path):
    """K-8 发布前置：parsing 状态 publish → 409 INVALID_DOCUMENT_STATE。"""
    import app.db as db_mod

    client.get("/api/documents")  # 触发 demo 工作空间解析
    workspace_id = _workspace_id(db_mod)
    document_id, version_id = _make_raw_version(
        db_mod, workspace_id=workspace_id, status="parsing"
    )

    response = client.post(
        f"/api/documents/{document_id}/versions/{version_id}/publish"
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "INVALID_DOCUMENT_STATE"


def test_k9_new_publish_supersedes_old_version(client):
    """K-9 新发布顶替旧发布：v1 自动降级、旧向量同步删除、检索只命中 v2。"""
    first = _upload_pdf(client, "manual.pdf", "green tea premium spring harvest")
    wait_for_jobs(timeout=10)
    client.post(
        f"/api/documents/{first['document_id']}/versions/{first['version_id']}/publish"
    )
    wait_for_jobs(timeout=10)

    second = _upload_pdf(client, "manual.pdf", "oolong roasted tea 500g")
    wait_for_jobs(timeout=10)
    assert second["document_id"] == first["document_id"]
    assert second["version_no"] == 2

    published = client.post(
        f"/api/documents/{second['document_id']}/versions/{second['version_id']}/publish"
    )
    assert published.status_code == 200, published.text
    assert published.json()["superseded_version_ids"] == [first["version_id"]]
    wait_for_jobs(timeout=10)

    detail = client.get(f"/api/documents/{first['document_id']}").json()
    by_no = {item["version_no"]: item for item in detail["versions"]}
    assert by_no[1]["published_at"] is None
    assert by_no[1]["superseded"] is True
    assert by_no[2]["published_at"] is not None
    assert by_no[2]["superseded"] is False

    answer = client.post("/api/rag/query", json={"query": "tea"}).json()
    version_ids = {item["document_version_id"] for item in answer["citations"]}
    assert version_ids == {second["version_id"]}
    snippets = " ".join(item["snippet"] for item in answer["citations"]).lower()
    assert "green tea" not in snippets  # 旧版本向量已被同步删除


def test_k10_unpublish_requires_published_state(client):
    """K-10 撤回前置：未发布的版本 unpublish → 409 VERSION_NOT_PUBLISHED。"""
    import app.db as db_mod

    client.get("/api/documents")
    workspace_id = _workspace_id(db_mod)
    document_id, version_id = _make_raw_version(
        db_mod, workspace_id=workspace_id, status="ready"
    )

    response = client.post(
        f"/api/documents/{document_id}/versions/{version_id}/unpublish"
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "VERSION_NOT_PUBLISHED"


def test_k11_permissions_are_enforced_for_roles(staff):
    """K-11 权限：readonly 上传/发布 403；warehouse 发布 403；客服可传不可发布。"""
    # 运营：可上传可发布（造出待发布版本）
    _login(staff, "p3s3-operations")
    body = _upload_pdf(staff, "manual.pdf", "green tea spec for operations")
    wait_for_jobs(timeout=10)
    assert body["version_id"]

    # 客服：可上传（knowledge.write），不可发布（无发布角色）
    _login(staff, "p3s3-customer_service")
    cs_upload = staff.post(
        "/api/documents",
        files={"file": ("cs.pdf", _make_pdf_bytes(["cs note"]), "application/pdf")},
        data={"title": "cs"},
    )
    assert cs_upload.status_code == 202, cs_upload.text
    cs_publish = staff.post(
        f"/api/documents/{body['document_id']}/versions/{body['version_id']}/publish"
    )
    assert cs_publish.status_code == 403
    assert cs_publish.json()["detail"]["code"] == "FORBIDDEN"

    # 只读成员：无 knowledge.write → 上传 403、发布 403
    _login(staff, "p3s3-readonly")
    ro_upload = staff.post(
        "/api/documents",
        files={"file": ("ro.pdf", _make_pdf_bytes(["ro note"]), "application/pdf")},
        data={"title": "ro"},
    )
    assert ro_upload.status_code == 403
    ro_publish = staff.post(
        f"/api/documents/{body['document_id']}/versions/{body['version_id']}/publish"
    )
    assert ro_publish.status_code == 403

    # 仓库协作方：无 knowledge.write → 发布 403
    _login(staff, "p3s3-warehouse")
    wh_publish = staff.post(
        f"/api/documents/{body['document_id']}/versions/{body['version_id']}/publish"
    )
    assert wh_publish.status_code == 403


def test_k12_explicit_version_endpoint(client):
    """K-12 显式版本端点：异 filename 409；同 filename 新内容 → v2；同 sha 幂等。"""
    first = _upload_pdf(client, "manual.pdf", "v1 tea green tea 250g")
    wait_for_jobs(timeout=10)

    # 异 filename → 409 FILENAME_LINEAGE_MISMATCH
    mismatch = client.post(
        f"/api/documents/{first['document_id']}/versions",
        files={"file": ("other.pdf", _make_pdf_bytes(["other"]), "application/pdf")},
        data={"title": "other"},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "FILENAME_LINEAGE_MISMATCH"

    # 同 filename 新内容 → 同 document、version_no=2
    # 同一份字节只造一次：reportlab PDF 内嵌生成时间戳，两次现造可能 sha 不同。
    v2_pdf = _make_pdf_bytes(["v2 oolong tea 500g"])
    new_version = client.post(
        f"/api/documents/{first['document_id']}/versions",
        files={
            "file": (
                "manual.pdf",
                v2_pdf,
                "application/pdf",
            )
        },
        data={"title": "v2"},
    )
    assert new_version.status_code == 202, new_version.text
    body = new_version.json()
    assert body["document_id"] == first["document_id"]
    assert body["version_no"] == 2

    # 同 sha → 幂等返回既有版本
    same = client.post(
        f"/api/documents/{first['document_id']}/versions",
        files={
            "file": (
                "manual.pdf",
                v2_pdf,
                "application/pdf",
            )
        },
        data={"title": "v2"},
    )
    assert same.status_code == 202, same.text
    assert same.json()["version_id"] == body["version_id"]
    assert same.json()["version_no"] == 2
