"""上传内容和文件路径的安全边界。"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path, PurePosixPath

from app.importers.base import ParserError

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_DOCX_REQUIRED = {"[Content_Types].xml", "word/document.xml"}


def normalize_mime(content_type: str | None) -> str | None:
    if not content_type:
        return None
    return content_type.split(";", 1)[0].strip().lower() or None


def validate_content(
    content: bytes,
    *,
    source_type: str,
    filename: str | None = None,
    content_type: str | None = None,
    max_zip_members: int = 2000,
    max_zip_uncompressed_bytes: int = 100 * 1024 * 1024,
) -> str:
    """验证内容签名并返回受控的 canonical 扩展名。"""
    mime = normalize_mime(content_type)
    suffix = Path(filename or "").suffix.lower()
    if source_type not in {"pdf", "docx"}:
        raise ParserError("UNSUPPORTED_TYPE", "不支持的文档类型")
    expected_mime = PDF_MIME if source_type == "pdf" else DOCX_MIME
    if suffix and suffix not in {f".{source_type}"}:
        raise ParserError("TYPE_MISMATCH", "文件扩展名与文档类型不一致")
    if mime and mime not in {expected_mime, "application/octet-stream"}:
        raise ParserError("TYPE_MISMATCH", "文件 MIME 与文档类型不一致")
    if source_type == "pdf":
        if not content.startswith(b"%PDF-"):
            raise ParserError("SIGNATURE_MISMATCH", "文件内容不是有效 PDF")
        return "pdf"
    _validate_docx_zip(content, max_zip_members, max_zip_uncompressed_bytes)
    return "docx"


def _validate_docx_zip(content: bytes, max_members: int, max_total: int) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) > max_members:
                raise ParserError("PARSE_LIMIT", "DOCX 文件成员数量超限")
            total = 0
            names: set[str] = set()
            for info in members:
                name = info.filename
                path = PurePosixPath(name)
                if "\\" in name or path.is_absolute() or ".." in path.parts:
                    raise ParserError("UNSAFE_ARCHIVE", "DOCX 包含不安全路径")
                if info.flag_bits & 0x1:
                    raise ParserError("UNSAFE_ARCHIVE", "DOCX 加密成员不受支持")
                total += info.file_size
                if total > max_total:
                    raise ParserError("PARSE_LIMIT", "DOCX 解压后大小超限")
                names.add(name)
            missing = _DOCX_REQUIRED - names
            if missing:
                raise ParserError("SIGNATURE_MISMATCH", "DOCX 缺少必要文件")
            bad = archive.testzip()
            if bad is not None:
                raise ParserError("UNSAFE_ARCHIVE", "DOCX 校验失败")
    except ParserError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise ParserError("DOCX_READ_ERROR", "无法读取 DOCX 文件") from exc


def safe_storage_path(root: Path, relative_path: str) -> Path:
    """解析并验证 root 下的相对存储路径，拒绝穿越和 symlink 逃逸。"""
    if not relative_path or "\x00" in relative_path:
        raise ValueError("非法存储路径")
    path = Path(relative_path)
    if path.is_absolute() or Path(path).drive or "\\" in relative_path:
        raise ValueError("存储路径必须是安全相对路径")
    if ".." in path.parts:
        raise ValueError("存储路径不允许目录穿越")
    root = root.resolve()
    target = (root / path).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("存储路径超出根目录") from exc
    return target
