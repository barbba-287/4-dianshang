"""PDF 解析器：使用 pypdf 按页产出 ChunkDraft。"""

import io

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.importers.base import ChunkDraft, DocumentParser, ParserError


class PdfParser(DocumentParser):
    source_type = "pdf"

    def parse(self, stream: bytes, *, filename: str | None = None) -> list[ChunkDraft]:
        try:
            pdf = PdfReader(io.BytesIO(stream), strict=False)
        except PdfReadError as exc:
            raise ParserError("PDF_READ_ERROR", f"无法解析 PDF: {exc}") from exc

        if pdf.is_encrypted:
            raise ParserError("PDF_ENCRYPTED", "PDF 已加密，请提供明文版本")

        chunks: list[ChunkDraft] = []
        for index, page in enumerate(pdf.pages):
            try:
                text = page.extract_text() or ""
            except Exception as exc:  # noqa: BLE001 - 解析失败逐页隔离
                raise ParserError("PDF_PAGE_ERROR", f"第 {index + 1} 页解析失败: {exc}") from exc
            text = text.strip()
            if not text:
                continue
            chunks.append(
                ChunkDraft(
                    text=text,
                    page_no=index + 1,
                    paragraph_no=None,
                    section_title=None,
                    extras={"source": "pdf", "page_count": str(len(pdf.pages))},
                )
            )
        if not chunks:
            raise ParserError("PDF_EMPTY", "PDF 未提取到任何文本内容")
        return chunks