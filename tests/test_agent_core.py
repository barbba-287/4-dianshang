"""Tests for the provider-neutral Mock operator assistant."""
from __future__ import annotations

import importlib

from app.agent.adapters import MockAdapter
from app.agent.core import AgentCore
from app.agent.orchestrator import AgentOrchestrator
from app.agent.registry import AgentTool, AgentToolRegistry, ToolContext


def test_mock_adapter_selects_inventory_and_rejects_unknown():
    tool = AgentTool(name="get_inventory", description="", input_schema={"type": "object", "properties": {}}, handler=lambda p, c: {})
    adapter = MockAdapter()
    decision = adapter.decide("帮我查库存", [tool], [])
    assert decision.kind == "tool_call"
    assert decision.tool == "get_inventory"
    unknown = adapter.decide("帮我写一封邮件", [tool], [])
    assert unknown.error_code == "MOCK_INTENT_NOT_UNDERSTOOD"


def test_core_executes_readonly_tool_and_aggregates_trace():
    registry = AgentToolRegistry()
    registry.register(AgentTool(
        name="get_inventory",
        description="库存",
        input_schema={"type": "object", "properties": {"page": {"type": "integer"}, "page_size": {"type": "integer"}}},
        handler=lambda payload, context: {"total": 1},
        requires=("inventory.read",),
    ))
    core = AgentCore(adapter=MockAdapter(), registry=registry, orchestrator=AgentOrchestrator(registry=registry))
    result = core.run("查询库存", context=ToolContext(workspace_id=1, extras={"permissions": ("inventory.read",)}))
    assert result.ok is True
    assert result.traces[0].tool == "get_inventory"
    assert result.traces[0].result.output["total"] == 1


def test_core_does_not_accept_workspace_override_and_enforces_permission():
    registry = AgentToolRegistry()
    registry.register(AgentTool(
        name="get_inventory",
        description="库存",
        input_schema={"type": "object", "properties": {"page": {"type": "integer"}, "page_size": {"type": "integer"}}},
        handler=lambda payload, context: {"workspace_id": context.workspace_id},
        requires=("inventory.read",),
    ))
    core = AgentCore(adapter=MockAdapter(), registry=registry, orchestrator=AgentOrchestrator(registry=registry))
    denied = core.run("查询库存", context=ToolContext(workspace_id=7, extras={"permissions": ()}), input_hints={"workspace_id": 999})
    assert denied.ok is False
    assert denied.error_code == "PERMISSION_DENIED"
    allowed = core.run("查询库存", context=ToolContext(workspace_id=7, extras={"permissions": ("inventory.read",)}), input_hints={"workspace_id": 999})
    assert allowed.ok is True
    assert allowed.traces[0].result.output["workspace_id"] == 7


def test_core_unknown_intent_is_bounded():
    registry = AgentToolRegistry()
    core = AgentCore(adapter=MockAdapter(), registry=registry, max_turns=1)
    result = core.run("查询天气", context=ToolContext(workspace_id=1))
    assert result.ok is False
    assert result.error_code == "MOCK_INTENT_NOT_UNDERSTOOD"


def test_core_enforces_tool_call_budget():
    class RepeatingAdapter:
        def decide(self, message, tools, observations, *, input_hints=None):
            from app.agent.adapters import ModelDecision
            return ModelDecision(kind="tool_call", tool="read", input={})

    registry = AgentToolRegistry()
    registry.register(AgentTool(
        name="read", description="read", input_schema={"type": "object", "properties": {}},
        handler=lambda payload, context: {"ok": True},
    ))
    result = AgentCore(
        adapter=RepeatingAdapter(), registry=registry, max_turns=3, max_tool_calls=1,
    ).run("查询库存", context=ToolContext(workspace_id=1))
    assert result.ok is False
    assert result.error_code == "TOOL_CALL_LIMIT"
    assert len(result.traces) == 1


def test_core_converts_adapter_failure_to_structured_error():
    class BrokenAdapter:
        def decide(self, *args, **kwargs):
            raise RuntimeError("provider unavailable")

    result = AgentCore(adapter=BrokenAdapter(), registry=AgentToolRegistry()).run(
        "查询库存", context=ToolContext(workspace_id=1)
    )
    assert result.ok is False
    assert result.error_code == "MODEL_ADAPTER_ERROR"
    assert "provider unavailable" not in result.answer


def test_registry_wildcard_permission_allows_readonly_skill():
    registry = AgentToolRegistry()
    registry.register(AgentTool(
        name="read", description="read", input_schema={"type": "object", "properties": {}},
        handler=lambda payload, context: {"workspace_id": context.workspace_id},
        requires=("inventory.read",),
    ))
    result = registry.call(
        "read", {}, context=ToolContext(workspace_id=7, extras={"permissions": ("*",)})
    )
    assert result.ok is True
    assert result.output["workspace_id"] == 7
