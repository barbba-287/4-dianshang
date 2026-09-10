"""Bounded single-core operator assistant."""
from __future__ import annotations
from dataclasses import dataclass, field
from time import monotonic
from app.agent.adapters import ModelAdapter
from app.agent.orchestrator import AgentAction, AgentOrchestrator
from app.agent.registry import AgentToolRegistry, ToolCallResult, ToolContext

@dataclass
class AssistantTrace:
    tool: str
    input: dict
    result: ToolCallResult

@dataclass
class AssistantResult:
    ok: bool
    answer: str
    traces: list[AssistantTrace] = field(default_factory=list)
    error_code: str | None = None
    turns: int = 0

class AgentCore:
    def __init__(self, *, adapter: ModelAdapter, registry: AgentToolRegistry, orchestrator: AgentOrchestrator | None = None, max_turns: int = 3, max_tool_calls: int = 3, timeout_seconds: float = 10.0):
        if max_turns < 1 or max_tool_calls < 1 or timeout_seconds <= 0: raise ValueError("INVALID_AGENT_LIMITS")
        self.adapter, self.registry = adapter, registry
        self.orchestrator = orchestrator or AgentOrchestrator(registry=registry)
        self.max_turns, self.max_tool_calls, self.timeout_seconds = max_turns, max_tool_calls, timeout_seconds

    def run(self, message: str, *, context: ToolContext, input_hints: dict | None = None) -> AssistantResult:
        if not isinstance(message, str) or not message.strip(): return AssistantResult(False, "请输入要查询的问题。", error_code="MESSAGE_REQUIRED")
        traces, observations, started = [], [], monotonic()
        readonly_tools = self.registry.list_tools(include_readonly_only=True)
        for turn in range(1, self.max_turns + 1):
            if monotonic() - started > self.timeout_seconds: return AssistantResult(False, "助手执行超时，未生成未经验证的结论。", traces, "AGENT_TIMEOUT", turn - 1)
            try:
                decision = self.adapter.decide(message, readonly_tools, observations, input_hints=input_hints)
            except Exception:
                return AssistantResult(False, "助手决策失败，未生成未经验证的结论。", traces, "MODEL_ADAPTER_ERROR", turn)
            if decision.kind == "final": return AssistantResult(decision.error_code is None, decision.answer or "", traces, decision.error_code, turn)
            if decision.kind != "tool_call" or not decision.tool: return AssistantResult(False, "助手返回了无法执行的决定。", traces, "INVALID_MODEL_DECISION", turn)
            if len(traces) >= self.max_tool_calls: return AssistantResult(False, "已达到本次助手的工具调用上限。", traces, "TOOL_CALL_LIMIT", turn)
            action = AgentAction(tool=decision.tool, input=decision.input or {}, call_id=f"turn-{turn}")
            result = self.orchestrator.invoke(action, context=context)
            traces.append(AssistantTrace(action.tool, action.input, result))
            observations.append({"tool": action.tool, "ok": result.ok, "output": result.output, "error_code": result.error_code})
        return AssistantResult(False, "已达到助手执行轮次上限，未生成结论。", traces, "MAX_TURNS_EXCEEDED", self.max_turns)
