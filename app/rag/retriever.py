"""RAG 检索与无答案兜底。

按 query → embedder → top_k + score_threshold 检索，返回命中的
VectorRecord 与分数。业务侧可附加 payload 过滤（如按产品 id、按
document_version_id 限定）。
"""

from collections.abc import Sequence
from dataclasses import dataclass

from app.rag.embedder import Embedder
from app.rag.vector_store import VectorRecord, VectorStore


@dataclass
class Hit:
    chunk_id: int
    document_id: int
    document_version_id: int
    snippet: str
    score: float
    locator: dict


class RagRetriever:
    def __init__(
        self,
        *,
        embedder: Embedder,
        store: VectorStore,
        top_k: int = 5,
        min_score: float = 0.2,
    ):
        self.embedder = embedder
        self.store = store
        self.top_k = top_k
        self.min_score = min_score

    def retrieve(
        self,
        query: str,
        *,
        top_k: int | None = None,
        min_score: float | None = None,
        filter_payload: dict | None = None,
    ) -> list[Hit]:
        vector = self.embedder.embed(query)
        k = top_k if top_k is not None else self.top_k
        threshold = min_score if min_score is not None else self.min_score
        raw = self.store.search(
            vector,
            top_k=k,
            score_threshold=threshold,
            filter_payload=filter_payload,
        )
        return [self._to_hit(record, score) for record, score in raw]

    @staticmethod
    def _to_hit(record: VectorRecord, score: float) -> Hit:
        payload = record.payload
        return Hit(
            chunk_id=int(payload.get("chunk_id", 0)),
            document_id=int(payload.get("document_id", 0)),
            document_version_id=int(payload.get("document_version_id", 0)),
            snippet=str(payload.get("snippet", "")),
            score=float(score),
            locator=dict(payload.get("locator", {})),
        )


def configure_default(embedder: Embedder, store: VectorStore) -> RagRetriever:
    """基于 Settings 构造默认 Retriever。"""
    from app.config import get_settings

    settings = get_settings()
    return RagRetriever(
        embedder=embedder,
        store=store,
        top_k=settings.rag_top_k,
        min_score=settings.rag_min_score,
    )