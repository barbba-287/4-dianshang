"""Embedder 抽象与本地假实现。

本地假 Embedder 基于 `hashlib.sha384`，把 token 序列映射到固定维度
的归一化向量。结果确定性、可重放，仅用于演示和评测；生产请替换为
BGE-M3 / DashScope Embedding 等真实模型。
"""

import hashlib
import math
import re
from collections.abc import Iterable
from typing import Protocol


class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]:
        ...


_TOKEN_RE = re.compile(r"[\w一-鿿]+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(text or "")]


class HashEmbedder:
    """基于 SHA-384 的确定性 Embedder；不依赖外部模型。"""

    def __init__(self, dim: int = 256):
        if dim <= 0 or dim > 384:
            raise ValueError(f"dim 必须在 1..384 之间，当前 {dim}")
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        tokens = _tokens(text)
        if not tokens:
            return [0.0] * self.dim
        vec = [0.0] * self.dim
        for token in tokens:
            digest = hashlib.sha384(token.encode("utf-8")).digest()
            # 取前 dim 字节
            for i in range(self.dim):
                # 用 8-bit 字节构造符号与幅度
                byte = digest[i % len(digest)]
                sign = 1.0 if (byte & 1) else -1.0
                magnitude = (byte >> 1) / 127.5  # 归一化到 0..1
                vec[i] += sign * magnitude
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0:
            return vec
        return [v / norm for v in vec]

    def embed_many(self, texts: Iterable[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]