"""S7-2 安全边界回归测试。"""

from app.agent.audit import summarise
from app.agent.registry import AgentTool, AgentToolRegistry, ToolContext
from app.upload_security import ParserError, safe_storage_path, validate_content


def test_upload_security_checks_pdf_signature():
    assert validate_content(b"%PDF-1.7 data", source_type="pdf", filename="a.pdf", content_type="application/pdf") == "pdf"
    try:
        validate_content(b"not pdf", source_type="pdf", filename="a.pdf", content_type="application/pdf")
    except ParserError as exc:
        assert exc.code == "SIGNATURE_MISMATCH"
    else:
        raise AssertionError("invalid PDF signature should fail")


def test_upload_security_checks_docx_structure():
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    assert validate_content(buf.getvalue(), source_type="docx", filename="a.docx") == "docx"


def test_safe_storage_path_rejects_traversal(tmp_path):
    try:
        safe_storage_path(tmp_path, "../outside.txt")
    except ValueError:
        pass
    else:
        raise AssertionError("path traversal should fail")


def test_agent_quota_rejection_does_not_grow_window():
    registry = AgentToolRegistry()
    registry.register(
        AgentTool(
            name="limited",
            description="test",
            input_schema={"type": "object", "properties": {}},
            handler=lambda payload, context: {"ok": True},
            max_calls_per_minute=1,
        )
    )
    assert registry.call("limited", {}, context=ToolContext(tenant_id="a")).ok
    assert not registry.call("limited", {}, context=ToolContext(tenant_id="a")).ok
    assert len(registry._counters[("limited", "a")]) == 1
    assert registry.call("limited", {}, context=ToolContext(tenant_id="b")).ok


def test_audit_summary_redacts_sensitive_fields():
    summary = summarise({"password": "secret", "nested": {"token": "abc", "value": "ok"}})
    assert "secret" not in summary
    assert "abc" not in summary
    assert "[REDACTED]" in summary


def test_audit_summary_redacts_aliases_and_truncates():
    summary = summarise({
        "access_token": "access-secret",
        "apiKey": "api-secret",
        "nested": {"AUTHORIZATION": "bearer-secret"},
        "description": "x" * 500,
    })
    assert "access-secret" not in summary
    assert "api-secret" not in summary
    assert "bearer-secret" not in summary
    assert len(summary) <= 200
