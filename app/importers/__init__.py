"""电商工作台的文档导入子包。

提供 PDF / DOCX 解析器的统一抽象；S2 阶段只支持 PDF 与 DOCX，
旧版 .doc / .xls 不在范围内。
"""

from app.importers.base import (
    ChunkDraft,
    DocumentParser,
    ParserError,
    detect_source_type,
    get_parser,
)
from app.importers.docx import DocxParser
from app.importers.pdf import PdfParser

__all__ = [
    "ChunkDraft",
    "DocumentParser",
    "DocxParser",
    "ParserError",
    "PdfParser",
    "detect_source_type",
    "get_parser",
]