"""Provider-neutral model decisions for the local operator assistant."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol, Sequence

from app.agent.registry import AgentTool


@dataclass(frozen=True)
class ModelDecision:
    kind: str
    tool: str | None = None
    input: dict | None = None
    answer: str | None = None
    error_code: str | None = None


class ModelAdapter(Protocol):
    def decide(self, message: str, tools: Sequence[AgentTool], observations: Sequence[dict], *, input_hints: dict | None = None) -> ModelDecision: ...


class MockAdapter:
    _INTENTS = ((("补货", "补多少", "replenishment", "restock"), "get_replenishment_evidence"), (("健康", "库存健康", "sku", "SKU", "可售天数"), "list_sku_health"), (("库存", "存量", "inventory", "stock"), "get_inventory"))

    def decide(self, message: str, tools: Sequence[AgentTool], observations: Sequence[dict], *, input_hints: dict | None = None) -> ModelDecision:
        if observations:
            last = observations[-1]
            if not last.get("ok"):
                return ModelDecision(kind="final", answer="工具执行失败，未生成未经验证的结论。", error_code=last.get("error_code") or "TOOL_RUNTIME_ERROR")
            return ModelDecision(kind="final", answer="查询完成，结果已返回。")
        selected = None
        lowered = message.lower()
        for words, tool_name in self._INTENTS:
            if any(word.lower() in message or word.lower() in lowered for word in words):
                selected = tool_name
                break
        if selected is None or selected not in {tool.name for tool in tools}:
            return ModelDecision(kind="final", answer="我暂时只能协助查询库存、SKU 健康度和补货依据。", error_code="MOCK_INTENT_NOT_UNDERSTOOD")
        hints = dict(input_hints or {})
        for key in ("workspace_id", "workspace", "tenant_id", "tenant"):
            hints.pop(key, None)
        if selected == "get_inventory":
            hints.setdefault("page", 1); hints.setdefault("page_size", 20)
        elif selected == "list_sku_health":
            hints.setdefault("coverage_days", 14)
        elif selected == "get_replenishment_evidence":
            for field in ("warehouse_id", "sku_id"):
                if field not in hints:
                    match = re.search(rf"{field}\s*[:=：]\s*(\d+)", message, flags=re.IGNORECASE)
                    if match: hints[field] = int(match.group(1))
            hints.setdefault("coverage_days", 14)
        return ModelDecision(kind="tool_call", tool=selected, input=hints)
