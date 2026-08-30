"""文档解析器抽象与工厂。

定义 DocumentParser 协议：解析输入字节流，按段（段落 / 页 / 章节）
产出 ChunkDraft。每个 ChunkDraft 至少包含文本与定位信息（页码 /
段落号 / sheet 与 cell），供后续 RAG 检索引用。
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ChunkDraft:
    text: str
    page_no: int | None = None
    paragraph_no: int | None = None
    section_title: str | None = None
    extras: dict[str, str] = field(default_factory=dict)


class ParserError(Exception):
    """解析失败时抛出；区分用户错误（坏文件、加密）与其他异常。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class DocumentParser:
    source_type: str  # "pdf" / "docx"

    def parse(self, stream: bytes, *, filename: str | None = None) -> Iterator[ChunkDraft]:
        raise NotImplementedError


def detect_source_type(filename: str | None, content_type: str | None = None) -> str:
    """根据文件名与 MIME 推断 source_type；不在白名单则抛 ParserError。"""
    if filename:
        suffix = Path(filename).suffix.lower().lstrip(".")
        if suffix in {"pdf", "docx"}:
            return suffix
    if content_type:
        if content_type == "application/pdf":
            return "pdf"
        if (
            content_type
            == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ):
            return "docx"
    raise ParserError(
        "UNSUPPORTED_TYPE",
        f"UNSUPPORTED_TYPE: 不支持的文件 filename={filename!r}, content_type={content_type!r}",
    )


def get_parser(source_type: str) -> DocumentParser:
    from app.importers.docx import DocxParser
    from app.importers.pdf import PdfParser

    if source_type == "pdf":
        return PdfParser()
    if source_type == "docx":
        return DocxParser()
    raise ParserError("UNSUPPORTED_TYPE", f"未知 source_type: {source_type}")