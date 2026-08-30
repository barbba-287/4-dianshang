"""RAG 答案组装与可选 LLM adapter 占位。

无 LLM 模式：直接用 top-1 snippet 作为 answer；无证据或分数过低时
返回 `{no_answer:true, reason:'low_score'|'empty'}`。后续可注入真实
LLM 实现 `LLMProvider.answer()`。
"""

from dataclasses import dataclass
from typing import Protocol

from app.rag.retriever import Hit, RagRetriever


@dataclass
class RagAnswer:
    answer: str | None
    hits: list[Hit]
    no_answer: bool
    reason: str | None = None


class LLMProvider(Protocol):
    def answer(self, *, question: str, hits: list[Hit]) -> str:
        ...


class StubLLMProvider:
    """占位 LLM：把 hits 拼成一段简短答案；生产请替换为真实模型。"""

    def answer(self, *, question: str, hits: list[Hit]) -> str:
        if not hits:
            return "当前资料无法确认。"
        bullet = "\n".join(
            f"- {hit.snippet[:120]}" for hit in hits[:3]
        )
        return f"基于知识库检索到以下信息：\n{bullet}"


class RagAnswerer:
    def __init__(
        self,
        *,
        retriever: RagRetriever,
        llm: LLMProvider | None = None,
    ):
        self.retriever = retriever
        self.llm = llm or StubLLMProvider()

    def answer(
        self,
        question: str,
        *,
        top_k: int | None = None,
        min_score: float | None = None,
        filter_payload: dict | None = None,
    ) -> RagAnswer:
        hits = self.retriever.retrieve(
            question,
            top_k=top_k,
            min_score=min_score,
            filter_payload=filter_payload,
        )
        if not hits:
            return RagAnswer(
                answer=None,
                hits=[],
                no_answer=True,
                reason="empty",
            )
        # 分数最高的命中若仍低于阈值，也视为低置信度
        if hits[0].score < (min_score if min_score is not None else self.retriever.min_score):
            return RagAnswer(
                answer=None,
                hits=hits,
                no_answer=True,
                reason="low_score",
            )
        answer_text = self.llm.answer(question=question, hits=hits)
        return RagAnswer(answer=answer_text, hits=hits, no_answer=False)