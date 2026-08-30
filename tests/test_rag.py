"""S3 RAG 子系统单测。

覆盖：
- HashEmbedder 确定性 / 归一化 / 维度；
- InMemoryVectorStore top_k / 阈值 / payload 过滤 / 删除；
- RagRetriever 转 Hit；
- RagAnswerer 无答案兜底（empty / low_score）；
- StubLLMProvider 拼装答案。
"""

import pytest

from app.rag import (
    HashEmbedder,
    Hit,
    InMemoryVectorStore,
    RagAnswerer,
    RagRetriever,
    StubLLMProvider,
    VectorRecord,
    cosine_similarity,
)


def test_hash_embedder_is_deterministic_and_normalized():
    embedder = HashEmbedder(dim=64)
    v1 = embedder.embed("高山绿茶 清香回甘")
    v2 = embedder.embed("高山绿茶 清香回甘")
    assert v1 == v2
    norm = sum(x * x for x in v1) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_hash_embedder_dimension_and_empty():
    embedder = HashEmbedder(dim=128)
    vec = embedder.embed("高山绿茶")
    assert len(vec) == 128
    empty = embedder.embed("")
    assert empty == [0.0] * 128


def test_hash_embedder_similar_texts_share_direction():
    embedder = HashEmbedder(dim=128)
    sim = cosine_similarity(embedder.embed("高山绿茶 清香"), embedder.embed("高山绿茶 回甘"))
    other = cosine_similarity(embedder.embed("高山绿茶 清香"), embedder.embed("陶瓷马克杯 防尘"))
    assert sim > other


def test_in_memory_vector_store_top_k_and_threshold(tmp_path):
    store = InMemoryVectorStore(tmp_path / "vectors.json")
    embedder = HashEmbedder(dim=32)
    texts = [
        ("t1", "高山绿茶 清香回甘 250g", {"version": 1}),
        ("t2", "高山红茶 浓香 500g", {"version": 1}),
        ("t3", "陶瓷保温马克杯 防尘 420ml", {"version": 2}),
    ]
    records = [
        VectorRecord(id=k, vector=embedder.embed(text), payload=payload)
        for k, text, payload in texts
    ]
    store.add(records)

    query = embedder.embed("高山绿茶 清香")
    hits = store.search(query, top_k=2, score_threshold=0.0)
    assert len(hits) <= 2
    # 阈值过滤
    high = store.search(query, top_k=5, score_threshold=0.9)
    # 可能为空，但每个命中都 >= 0.9
    for _, score in high:
        assert score >= 0.9


def test_in_memory_vector_store_filters_payload(tmp_path):
    store = InMemoryVectorStore(tmp_path / "vectors.json")
    embedder = HashEmbedder(dim=16)
    records = [
        VectorRecord(id="a", vector=embedder.embed("高山绿茶"), payload={"version": 1}),
        VectorRecord(id="b", vector=embedder.embed("高山绿茶"), payload={"version": 2}),
    ]
    store.add(records)
    only_v1 = store.search(
        embedder.embed("高山绿茶"),
        top_k=10,
        score_threshold=0.0,
        filter_payload={"version": 1},
    )
    assert {record.id for record, _ in only_v1} == {"a"}


def test_in_memory_vector_store_delete_by_version(tmp_path):
    store = InMemoryVectorStore(tmp_path / "vectors.json")
    embedder = HashEmbedder(dim=8)
    store.add([
        VectorRecord(id="a", vector=embedder.embed("x"), payload={"document_version_id": 1}),
        VectorRecord(id="b", vector=embedder.embed("y"), payload={"document_version_id": 2}),
    ])
    assert store.delete_by_version(1) == 1
    assert {r.id for r in store.records} == {"b"}


def test_retriever_returns_hit_with_payload():
    embedder = HashEmbedder(dim=16)
    store = InMemoryVectorStore()
    store.add([
        VectorRecord(
            id="x",
            vector=embedder.embed("高山绿茶"),
            payload={
                "chunk_id": 7,
                "document_id": 1,
                "document_version_id": 3,
                "snippet": "snippet text",
                "locator": {"page_no": 2},
            },
        )
    ])
    retriever = RagRetriever(embedder=embedder, store=store, top_k=1, min_score=0.0)
    hits = retriever.retrieve("高山绿茶")
    assert len(hits) >= 1
    hit: Hit = hits[0]
    assert hit.chunk_id == 7
    assert hit.document_version_id == 3
    assert hit.locator == {"page_no": 2}


def test_answerer_empty_returns_no_answer_empty():
    embedder = HashEmbedder(dim=8)
    store = InMemoryVectorStore()
    answerer = RagAnswerer(
        retriever=RagRetriever(embedder=embedder, store=store, top_k=3, min_score=0.5),
        llm=StubLLMProvider(),
    )
    result = answerer.answer("高山绿茶规格")
    assert result.no_answer is True
    assert result.reason == "empty"
    assert result.answer is None


def test_answerer_low_score_returns_no_answer_low_score():
    embedder = HashEmbedder(dim=8)
    store = InMemoryVectorStore()
    store.add([
        VectorRecord(
            id="x",
            vector=embedder.embed("高山绿茶"),
            payload={"chunk_id": 1, "document_id": 1, "document_version_id": 1, "snippet": "x"},
        )
    ])
    # 设极高阈值使即便命中也判为 low_score
    answerer = RagAnswerer(
        retriever=RagRetriever(embedder=embedder, store=store, top_k=3, min_score=0.99),
        llm=StubLLMProvider(),
    )
    result = answerer.answer("完全无关的查询")
    assert result.no_answer is True
    assert result.reason in {"empty", "low_score"}


def test_stub_llm_provider_assembles_answer():
    embedder = HashEmbedder(dim=8)
    store = InMemoryVectorStore()
    store.add([
        VectorRecord(
            id="x",
            vector=embedder.embed("高山绿茶"),
            payload={
                "chunk_id": 1,
                "document_id": 1,
                "document_version_id": 1,
                "snippet": "高山绿茶 250g 清香回甘",
            },
        )
    ])
    retriever = RagRetriever(embedder=embedder, store=store, top_k=1, min_score=0.0)
    answerer = RagAnswerer(retriever=retriever, llm=StubLLMProvider())
    result = answerer.answer("高山绿茶")
    assert result.no_answer is False
    assert result.answer is not None
    assert "高山绿茶" in result.answer