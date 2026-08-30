"""内容哈希、版本号自增等工具函数。"""

import hashlib


def content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def estimate_token_count(text: str) -> int:
    """轻量 token 估算：按字符数 / 2；用于 chunk 元数据与评测。"""
    return max(1, len(text) // 2)


def next_version_no(current_max: int | None) -> int:
    return (current_max or 0) + 1