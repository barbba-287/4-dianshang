"""DOCX 解析器：使用 python-docx 按段落产出 ChunkDraft。"""

import io

from docx import Document
from docx.opc.exceptions import PackageNotFoundError

from app.importers.base import ChunkDraft, DocumentParser, ParserError


class DocxParser(DocumentParser):
    source_type = "docx"

    def parse(self, stream: bytes, *, filename: str | None = None) -> list[ChunkDraft]:
        try:
            doc = Document(io.BytesIO(stream))
        except PackageNotFoundError as exc:
            raise ParserError("DOCX_READ_ERROR", f"无法解析 DOCX: {exc}") from exc

        chunks: list[ChunkDraft] = []
        current_section: str | None = None
        for paragraph_no, paragraph in enumerate(doc.paragraphs):
            text = paragraph.text.strip()
            if not text:
                continue
            style_name = paragraph.style.name if paragraph.style else ""
            if style_name and "Heading" in style_name:
                current_section = text
                chunks.append(
                    ChunkDraft(
                        text=text,
                        page_no=None,
                        paragraph_no=paragraph_no,
                        section_title=current_section,
                        extras={"kind": "heading", "style": style_name},
                    )
                )
                continue
            chunks.append(
                ChunkDraft(
                    text=text,
                    page_no=None,
                    paragraph_no=paragraph_no,
                    section_title=current_section,
                    extras={"kind": "paragraph"},
                )
            )

        # 简单表格抽取：每个 table 一段
        for table_idx, table in enumerate(doc.tables):
            for row_idx, row in enumerate(table.rows):
                cells = [cell.text.strip() for cell in row.cells]
                joined = " | ".join(cell for cell in cells if cell)
                if not joined:
                    continue
                chunks.append(
                    ChunkDraft(
                        text=joined,
                        page_no=None,
                        paragraph_no=None,
                        section_title=current_section,
                        extras={
                            "kind": "table",
                            "table_index": str(table_idx),
                            "row_index": str(row_idx),
                        },
                    )
                )

        if not chunks:
            raise ParserError("DOCX_EMPTY", "DOCX 未提取到任何文本内容")
        return chunks