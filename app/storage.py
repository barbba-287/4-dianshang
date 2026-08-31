"""上传文件存储。

S2 阶段保留原文件到 `uploads/<source_type>/<sha256>.<ext>`；文件
大小受配置 `UPLOAD_MAX_BYTES` 限制，调用方应在写入前先做 size 校验。
"""

import os
import secrets
from pathlib import Path

from app.config import get_settings
from app.upload_security import safe_storage_path


class StorageError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def storage_root() -> Path:
    settings = get_settings()
    return settings.resolve_path(settings.imports_dir)


def ensure_storage() -> Path:
    root = storage_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_upload(*, source_type: str, sha256_hex: str, ext: str, content: bytes) -> str:
    """保存字节流到 `<root>/<source_type>/<sha256>.<ext>`，返回相对路径。"""
    if not content:
        raise StorageError("EMPTY_FILE", "文件内容为空")
    if source_type not in {"pdf", "docx"} or ext.lower().lstrip(".") != source_type:
        raise StorageError("INVALID_STORAGE_TYPE", "非法存储文件类型")
    if len(sha256_hex) != 64 or any(c not in "0123456789abcdefABCDEF" for c in sha256_hex):
        raise StorageError("INVALID_STORAGE_NAME", "非法存储文件名")
    root = ensure_storage().resolve()
    target_dir = safe_storage_path(root, source_type)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = safe_storage_path(root, f"{source_type}/{sha256_hex.lower()}.{source_type}")
    temp_path = target_dir / f".{target_path.name}.{secrets.token_hex(8)}.tmp"
    try:
        with temp_path.open("wb") as fp:
            fp.write(content)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(temp_path, target_path)
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise StorageError("STORAGE_WRITE_ERROR", "文件存储失败") from exc
    return target_path.relative_to(root).as_posix()


def storage_size_limit() -> int:
    settings = get_settings()
    return settings.upload_max_bytes


def enforce_size_limit(content_length: int | None) -> int:
    limit = storage_size_limit()
    if content_length is not None and content_length > limit:
        raise StorageError(
            "FILE_TOO_LARGE",
            f"文件过大: {content_length} bytes > {limit}",
        )
    return limit


def file_uri_for(storage_path: str) -> str:
    """把相对存储路径转换为绝对 URI，便于 RAG 检索引用。"""
    root = storage_root().resolve()
    try:
        return safe_storage_path(root, storage_path).as_uri()
    except ValueError as exc:
        raise StorageError("UNSAFE_STORAGE_PATH", "非法存储路径") from exc
