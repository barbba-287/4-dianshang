"""S3 RAG API 端到端测试。

覆盖：
- 上传 PDF → 索引 → query 命中并返回 answer + citations；
- 无证据 query 返回 {no_answer:true, reason}；
- /api/rag/query 参数校验；
- /api/settings 返回运行时配置。
"""

import importlib
import io

import pytest
from docx import Document as DocxDocument
from fastapi.testclient import TestClient

from app.background import reset_executor, wait_for_jobs
from app.rag_runtime import reset_instances


def _make_pdf_bytes(text: str) -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
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
    vector_path = tmp_path / "vectors.json"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IMPORTS_DIR", str(uploads_dir))
    monkeypatch.setenv("WORKER_LEASE_SECONDS", "30")
    monkeypatch.setenv("VECTOR_STORE_PATH", str(vector_path))
    # 降低阈值让 hash embedder 测试更稳定；生产用真实 embedding 时应 >= 0.5
    monkeypatch.setenv("RAG_MIN_SCORE", "0.0")

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

    with TestClient(main_mod.app) as c:
        yield c

    reset_executor()
    reset_instances()
    cfg.get_settings.cache_clear()


def test_rag_query_returns_answer_and_citations(client):
    # 使用英文文本避免 reportlab 默认字体不支持中文导致 PDF 提取为空
    pdf = _make_pdf_bytes("premium green tea leaves spring harvest 250g")
    upload = client.post(
        "/api/documents",
        files={"file": ("manual.pdf", pdf, "application/pdf")},
        data={"title": "Tea Spec"},
    ).json()
    wait_for_jobs(timeout=10)

    response = client.post(
        "/api/rag/query",
        json={"query": "green tea"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["no_answer"] is False
    assert body["answer"] is not None
    assert len(body["citations"]) >= 1
    assert body["citations"][0]["snippet"]
    assert body["retrieval_diagnostics"]["hits_count"] >= 1


def test_rag_query_no_evidence_returns_no_answer(client):
    response = client.post(
        "/api/rag/query",
        json={"query": "completely unrelated outer space coffee"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["no_answer"] is True
    assert body["answer"] is None
    assert body["reason"] == "empty"


def test_rag_query_docx_pipeline(client):
    docx = _make_docx_bytes(["Return policy: 7 days no-reason return allowed.", "Shipping: free over 99 yuan."])
    upload = client.post(
        "/api/documents",
        files={
            "file": (
                "rules.docx",
                docx,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        data={"title": "After-sales"},
    ).json()
    wait_for_jobs(timeout=10)

    response = client.post(
        "/api/rag/query",
        json={"query": "return policy"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["no_answer"] is False
    assert "return" in body["answer"].lower() or "Return" in body["answer"]


def test_rag_query_rejects_empty_query(client):
    response = client.post("/api/rag/query", json={"query": ""})
    assert response.status_code == 422


def test_settings_endpoint_reports_runtime(client):
    response = client.get("/api/settings")
    assert response.status_code == 200
    body = response.json()
    assert body["app_name"]
    assert body["worker_concurrency"] >= 1
    assert body["embedder_dim"] > 0
    assert "vector_record_count" in body