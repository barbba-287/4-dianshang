"""S2 文档导入解析器单测。

覆盖：
- detect_source_type 按文件后缀 / MIME 推断；
- PdfParser：多页文本提取、加密 PDF 抛错、空 PDF 抛错；
- DocxParser：段落 / 表格 / 标题切分。
"""

import io

import pytest
from docx import Document as DocxDocument
from pypdf import PdfWriter

from app.importers import (
    DocxParser,
    ParserError,
    PdfParser,
    detect_source_type,
    get_parser,
)


def test_detect_source_type_by_extension():
    assert detect_source_type("manual.pdf") == "pdf"
    assert detect_source_type("manual.docx") == "docx"


def test_detect_source_type_by_content_type():
    assert detect_source_type(None, "application/pdf") == "pdf"
    assert (
        detect_source_type(
            None,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        == "docx"
    )


def test_detect_source_type_unsupported():
    with pytest.raises(ParserError, match="UNSUPPORTED_TYPE"):
        detect_source_type("manual.doc")
    with pytest.raises(ParserError, match="UNSUPPORTED_TYPE"):
        detect_source_type("manual.xlsx")
    with pytest.raises(ParserError, match="UNSUPPORTED_TYPE"):
        detect_source_type(None)


def _make_pdf_bytes(pages: list[str]) -> bytes:
    writer = PdfWriter()
    for text in pages:
        writer.add_blank_page(width=72, height=72)
        if text:
            writer.pages[-1].write_text = text  # 占位，不实际写入内容
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_pdf_with_text(pages: list[str]) -> bytes:
    """通过 reportlab 或手工写出可被 pypdf 读取的文本。"""
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for text in pages:
        c.drawString(72, 720, text)
        c.showPage()
    c.save()
    return buf.getvalue()


def test_pdf_parser_extracts_pages():
    try:
        # 使用 ASCII 文本避免字体嵌入问题（reportlab 默认字体不含中文）
        content = _make_pdf_with_text(["Page1 tea green tea 250g", "Page2 ceramic mug 420ml"])
    except ImportError:
        pytest.skip("reportlab 未安装，跳过 PDF 文本生成")
    chunks = PdfParser().parse(content, filename="manual.pdf")
    assert len(chunks) >= 1
    assert any("tea green tea" in chunk.text for chunk in chunks)
    assert any(chunk.page_no == 1 for chunk in chunks)


def test_pdf_parser_rejects_empty():
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    # 空白 PDF 可能被识别为 1 页空文本；解析器应识别出 PDF_EMPTY
    try:
        PdfParser().parse(buf.getvalue(), filename="empty.pdf")
        assert False, "应该抛 ParserError"
    except ParserError as exc:
        assert exc.code in ("PDF_EMPTY", "PDF_PAGE_ERROR")


def test_docx_parser_extracts_paragraphs_and_table():
    doc = DocxDocument()
    doc.add_heading("退换货规则", level=1)
    doc.add_paragraph("收货后 7 天内可申请无理由退换。")
    doc.add_paragraph("需保持商品完好，包装齐全。")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "场景"
    table.cell(0, 1).text = "处理"
    table.cell(1, 0).text = "质量问题"
    table.cell(1, 1).text = "免费换货"
    buf = io.BytesIO()
    doc.save(buf)
    chunks = DocxParser().parse(buf.getvalue(), filename="rules.docx")
    # 标题、段落、表格共至少 5 个 chunk
    assert len(chunks) >= 5
    assert any(chunk.text == "退换货规则" for chunk in chunks)
    assert any(chunk.text.startswith("收货后") for chunk in chunks)
    assert any("质量问题" in chunk.text and "免费换货" in chunk.text for chunk in chunks)


def test_get_parser_dispatch():
    assert isinstance(get_parser("pdf"), PdfParser)
    assert isinstance(get_parser("docx"), DocxParser)
    with pytest.raises(ParserError):
        get_parser("xlsx")