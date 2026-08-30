"""电商工作台的 RAG 客服问答子包。

提供本地假 Embedder、内存向量库（JSON 持久化）、top_k 检索与无答
案兜底。生产可替换 Embedder / VectorStore / LLMProvider 实现。
"""

from app.rag.answerer import LLMProvider, RagAnswerer, StubLLMProvider
from app.rag.embedder import Embedder, HashEmbedder
from app.rag.retriever import Hit, RagRetriever
from app.rag.vector_store import (
    InMemoryVectorStore,
    VectorRecord,
    VectorStore,
    cosine_similarity,
)

__all__ = [
    "Embedder",
    "HashEmbedder",
    "Hit",
    "InMemoryVectorStore",
    "LLMProvider",
    "RagAnswerer",
    "RagRetriever",
    "StubLLMProvider",
    "VectorRecord",
    "VectorStore",
    "cosine_similarity",
]