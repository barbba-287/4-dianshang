"""S2 文档导入 API 与后台执行端到端测试。

覆盖：
- 上传 PDF 入队后状态变 ready；
- 上传相同 PDF 再次只新增版本；
- 上传 DOCX 解析并写 chunks；
- 不支持的文件类型返回 415；
- 上传超限文件返回 413；
- 文档版本列表返回 chunk_count。
"""

import importlib
import io

import pytest
from docx import Document as DocxDocument
from fastapi.testclient import TestClient

from app.background import reset_executor, wait_for_jobs


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
    doc = DocxDocument()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    uploads_dir = tmp_path / "uploads"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IMPORTS_DIR", str(uploads_dir))
    monkeypatch.setenv("WORKER_LEASE_SECONDS", "30")

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

    with TestClient(main_mod.app) as c:
        yield c

    reset_executor()
    cfg.get_settings.cache_clear()


def test_upload_pdf_returns_202_and_succeeds(client):
    # 使用 ASCII 文本避免 reportlab 默认字体不支持中文导致提取为空
    pdf = _make_pdf_bytes(["Page1 tea green tea 250g"])
    response = client.post(
        "/api/documents",
        files={"file": ("manual.pdf", pdf, "application/pdf")},
        data={"title": "Tea Spec"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["version_no"] == 1
    assert body["status"] == "parsing"

    wait_for_jobs(timeout=10)
    versions = client.get(f"/api/documents/{body['document_id']}/versions").json()
    assert versions[0]["status"] == "ready"
    assert versions[0]["chunk_count"] >= 1


def test_upload_same_pdf_is_idempotent(client):
    """相同 sha256 的 PDF 重复上传应复用同一 document/version，不重复入库。"""
    pdf = _make_pdf_bytes(["v1 tea green tea 250g"])
    first = client.post(
        "/api/documents",
        files={"file": ("manual.pdf", pdf, "application/pdf")},
        data={"title": "v1"},
    ).json()
    wait_for_jobs(timeout=10)
    second = client.post(
        "/api/documents",
        files={"file": ("manual.pdf", pdf, "application/pdf")},
        data={"title": "v1"},
    ).json()
    wait_for_jobs(timeout=10)
    assert second["document_id"] == first["document_id"]
    assert second["version_id"] == first["version_id"]
    assert second["version_no"] == first["version_no"]


def test_upload_different_pdf_creates_new_document(client):
    """不同 sha 的 PDF 创建新 document（新 version_no=1）。"""
    pdf1 = _make_pdf_bytes(["v1 tea green tea 250g"])
    pdf2 = _make_pdf_bytes(["v2 red tea 500g"])
    first = client.post(
        "/api/documents",
        files={"file": ("a.pdf", pdf1, "application/pdf")},
        data={"title": "a"},
    ).json()
    wait_for_jobs(timeout=10)
    second = client.post(
        "/api/documents",
        files={"file": ("b.pdf", pdf2, "application/pdf")},
        data={"title": "b"},
    ).json()
    wait_for_jobs(timeout=10)
    assert second["document_id"] != first["document_id"]
    assert second["version_no"] == 1


def test_upload_docx_extracts_chunks(client):
    docx = _make_docx_bytes(["Return policy: 7 days no-reason return allowed.", "Shipping: free over 99 yuan."])
    response = client.post(
        "/api/documents",
        files={
            "file": (
                "rules.docx",
                docx,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        data={"title": "After-sales"},
    )
    assert response.status_code == 202
    body = response.json()
    wait_for_jobs(timeout=10)
    versions = client.get(f"/api/documents/{body['document_id']}/versions").json()
    assert versions[0]["status"] == "ready"
    assert versions[0]["chunk_count"] == 2


def test_unsupported_type_returns_415(client):
    response = client.post(
        "/api/documents",
        files={"file": ("legacy.doc", b"not a real doc", "application/msword")},
        data={"title": "old"},
    )
    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "UNSUPPORTED_TYPE"


def test_oversize_file_returns_413(client, monkeypatch):
    # 通过 env 限制文件大小并 reload 模块以让新配置生效
    monkeypatch.setenv("UPLOAD_MAX_BYTES", "50")
    import importlib

    import app.config as cfg
    import app.storage as storage_mod
    import app.main as main_mod

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


def test_versions_for_unknown_document_returns_404(client):
    assert client.get("/api/documents/99999/versions").status_code == 404