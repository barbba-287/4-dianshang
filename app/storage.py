"""上传文件存储。

S2 阶段保留原文件到 `uploads/<source_type>/<sha256>.<ext>`；文件
大小受配置 `UPLOAD_MAX_BYTES` 限制，调用方应在写入前先做 size 校验。
"""

import os
from pathlib import Path

from app.config import get_settings


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
    root = ensure_storage()
    target_dir = root / source_type
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{sha256_hex}.{ext.lstrip('.')}"
    target_path = target_dir / filename
    target_path.write_bytes(content)
    return str(target_path.relative_to(root))


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
    root = storage_root()
    return (root / storage_path).resolve().as_uri()