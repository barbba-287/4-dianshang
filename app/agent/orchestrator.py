"""Agent 编排器：单轮 function-call 风格的工具调用。

S5 阶段不直接调用 LLM，而是提供一个简化版的“决定 + 执行”接口：
- `decide(actions)`：根据上层（业务侧 / LLM）已经决定好的工具调用
  列表，依次执行；
- 编排器只负责：参数校验 → 限流 → 超时 → 审计 → 返回；
- 写入型工具被显式拒绝，避免被滥用。
"""

from collections.abc import Sequence
from dataclasses import dataclass

from app.agent.audit import AgentAuditLogger, AgentAuditRecord, summarise
from app.agent.registry import (
    AgentTool,
    AgentToolRegistry,
    ToolCallResult,
    ToolContext,
    default_registry,
    now_iso,
)


@dataclass
class AgentAction:
    tool: str
    input: dict
    call_id: str | None = None


class AgentOrchestrator:
    def __init__(
        self,
        *,
        registry: AgentToolRegistry | None = None,
        audit: AgentAuditLogger | None = None,
    ):
        self.registry = registry or default_registry
        self.audit = audit or AgentAuditLogger()

    def list_tools(self) -> list[AgentTool]:
        return self.registry.list_tools(include_readonly_only=True)

    def invoke(self, action: AgentAction, *, context: ToolContext | None = None) -> ToolCallResult:
        context = context or ToolContext()
        tool = self.registry.get(action.tool)
        if tool is None:
            result = ToolCallResult(
                ok=False,
                error_code="TOOL_NOT_FOUND",
                error_message=f"未知工具: {action.tool}",
            )
            self._audit(action, context, result)
            return result
        if not tool.is_readonly:
            result = ToolCallResult(
                ok=False,
                error_code="TOOL_NOT_READONLY",
                error_message=f"工具 {action.tool} 已被禁用（非只读）",
            )
            self._audit(action, context, result)
            return result

        result = self.registry.call(action.tool, action.input, context=context)
        self._audit(action, context, result)
        return result

    def invoke_many(
        self,
        actions: Sequence[AgentAction],
        *,
        context: ToolContext | None = None,
    ) -> list[ToolCallResult]:
        context = context or ToolContext()
        results: list[ToolCallResult] = []
        for action in actions:
            results.append(self.invoke(action, context=context))
        return results

    def _audit(
        self,
        action: AgentAction,
        context: ToolContext,
        result: ToolCallResult,
    ) -> None:
        record = AgentAuditRecord(
            timestamp=now_iso(),
            tool=action.tool,
            ok=result.ok,
            duration_ms=result.duration_ms,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            request_id=context.request_id,
            input_summary=summarise(action.input),
            output_summary=summarise(result.output) if result.ok else "",
            error_code=result.error_code,
            extras={"call_id": action.call_id} if action.call_id else {},
        )
        self.audit.record(record)