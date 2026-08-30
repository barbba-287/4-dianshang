"""RAG 子系统共享实例。

模块级单例 Embedder / VectorStore / Retriever / Answerer，避免每个
请求重新构造（向量库从 JSON 加载较慢）。
"""

import threading

from app.config import get_settings
from app.rag import HashEmbedder, InMemoryVectorStore, RagAnswerer, RagRetriever


_lock = threading.RLock()  # RLock 允许同一线程递归获取，避免 get_answerer→get_retriever 死锁
_instances: dict[str, object] = {}


def get_vector_store() -> InMemoryVectorStore:
    with _lock:
        store = _instances.get("vector_store")
        if store is None:
            settings = get_settings()
            store = InMemoryVectorStore(settings.vector_store_path)
            _instances["vector_store"] = store
        return store


def get_embedder() -> HashEmbedder:
    with _lock:
        embedder = _instances.get("embedder")
        if embedder is None:
            settings = get_settings()
            embedder = HashEmbedder(dim=settings.embedder_dim)
            _instances["embedder"] = embedder
        return embedder


def get_retriever() -> RagRetriever:
    with _lock:
        retriever = _instances.get("retriever")
        if retriever is None:
            settings = get_settings()
            retriever = RagRetriever(
                embedder=get_embedder(),
                store=get_vector_store(),
                top_k=settings.rag_top_k,
                min_score=settings.rag_min_score,
            )
            _instances["retriever"] = retriever
        return retriever


def get_answerer() -> RagAnswerer:
    with _lock:
        answerer = _instances.get("answerer")
        if answerer is None:
            answerer = RagAnswerer(retriever=get_retriever())
            _instances["answerer"] = answerer
        return answerer


def reset_instances() -> None:
    """测试用：清空单例缓存。"""
    with _lock:
        _instances.clear()