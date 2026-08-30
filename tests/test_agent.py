"""S5 Agent 子系统单测。

覆盖：
- AgentTool.validate_input：必填字段 / 类型 / 未知字段；
- ToolContext 与 AgentAction 序列化；
- AgentToolRegistry：注册 / 重复 / 查询 / 列表 / 限流 / 未知工具；
- 只读拒绝写入型工具；
- AgentOrchestrator：调用 + 审计 + 列表；
- 真实工具：search_products / get_product_detail / get_price_history。
"""

import importlib
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.agent.audit import AgentAuditLogger, AgentAuditRecord
from app.agent.registry import (
    AgentTool,
    AgentToolRegistry,
    ToolCallResult,
    ToolContext,
    ToolNotAllowed,
    ToolValidationError,
    default_registry,
)
from app.agent.orchestrator import AgentAction, AgentOrchestrator


def test_validate_input_required_fields():
    tool = AgentTool(
        name="x",
        description="x",
        input_schema={
            "type": "object",
            "properties": {"product_id": {"type": "integer"}},
            "required": ["product_id"],
        },
        handler=lambda p, c: p,
    )
    with pytest.raises(ToolValidationError) as excinfo:
        tool.validate_input({})
    assert excinfo.value.code == "MISSING_FIELD"
    with pytest.raises(ToolValidationError) as excinfo:
        tool.validate_input({"product_id": "abc"})
    assert excinfo.value.code == "TYPE_MISMATCH"
    out = tool.validate_input({"product_id": 1})
    assert out == {"product_id": 1}


def test_validate_input_unknown_field():
    tool = AgentTool(
        name="x",
        description="x",
        input_schema={"type": "object", "properties": {"a": {"type": "integer"}}},
        handler=lambda p, c: p,
    )
    with pytest.raises(ToolValidationError) as excinfo:
        tool.validate_input({"a": 1, "b": 2})
    assert excinfo.value.code == "UNKNOWN_FIELD"


def test_validate_input_payload_must_be_object():
    tool = AgentTool(
        name="x",
        description="x",
        input_schema={"type": "object", "properties": {}},
        handler=lambda p, c: p,
    )
    with pytest.raises(ToolValidationError) as excinfo:
        tool.validate_input("not a dict")  # type: ignore[arg-type]
    assert excinfo.value.code == "INVALID_INPUT"


def test_registry_register_and_lookup():
    reg = AgentToolRegistry()
    tool = AgentTool(
        name="hello",
        description="hi",
        input_schema={"type": "object", "properties": {}},
        handler=lambda p, c: "ok",
    )
    reg.register(tool)
    assert reg.get("hello") is tool
    with pytest.raises(ValueError):
        reg.register(tool)


def test_registry_rejects_non_readonly_in_call():
    reg = AgentToolRegistry()

    def write_handler(payload, context):
        return "should not be reached"

    tool = AgentTool(
        name="delete_everything",
        description="danger",
        input_schema={"type": "object", "properties": {}},
        handler=write_handler,
        is_readonly=False,
    )
    reg.register(tool)
    result = reg.call("delete_everything", {})
    assert result.ok is False
    assert result.error_code == "TOOL_NOT_READONLY"


def test_registry_unknown_tool_returns_error():
    reg = AgentToolRegistry()
    result = reg.call("missing", {})
    assert result.ok is False
    assert result.error_code == "TOOL_NOT_FOUND"


def test_registry_rate_limit():
    reg = AgentToolRegistry()
    tool = AgentTool(
        name="x",
        description="x",
        input_schema={"type": "object", "properties": {}},
        handler=lambda p, c: 1,
        max_calls_per_minute=2,
    )
    reg.register(tool)
    assert reg.call("x", {}).ok is True
    assert reg.call("x", {}).ok is True
    third = reg.call("x", {})
    assert third.ok is False
    assert third.error_code == "TOOL_NOT_ALLOWED"


def test_default_registry_has_readonly_tools():
    tools = default_registry.list_tools(include_readonly_only=True)
    names = {t.name for t in tools}
    assert {"search_products", "get_product_detail", "get_price_history"}.issubset(names)


def test_search_products_returns_results(tmp_path, monkeypatch):
    _bootstrap_db(tmp_path, monkeypatch)
    reg = AgentToolRegistry()
    from app.agent import impl

    impl.register_default_tools(reg)
    result = reg.call("search_products", {"keyword": "tea"})
    assert result.ok is True
    assert "total" in result.output
    assert "items" in result.output


def test_get_product_detail_unknown_returns_error(tmp_path, monkeypatch):
    _bootstrap_db(tmp_path, monkeypatch)
    reg = AgentToolRegistry()
    from app.agent import impl

    impl.register_default_tools(reg)
    result = reg.call("get_product_detail", {"product_id": 99999})
    assert result.ok is True
    assert result.output.get("error") == "PRODUCT_NOT_FOUND"


def test_get_price_history_empty(tmp_path, monkeypatch):
    _bootstrap_db(tmp_path, monkeypatch)
    reg = AgentToolRegistry()
    from app.agent import impl

    impl.register_default_tools(reg)
    result = reg.call("get_price_history", {"product_id": 1})
    assert result.ok is True
    assert "items" in result.output


def test_orchestrator_writes_audit(tmp_path, monkeypatch):
    _bootstrap_db(tmp_path, monkeypatch)
    audit_path = tmp_path / "audit.jsonl"
    audit = AgentAuditLogger(audit_path)
    orchestrator = AgentOrchestrator(audit=audit)
    action = AgentAction(tool="search_products", input={"keyword": "tea"})
    result = orchestrator.invoke(action)
    assert result.ok is True
    records = audit.list_records()
    assert len(records) >= 1
    last = records[-1]
    assert last["tool"] == "search_products"
    assert last["ok"] is True


def test_orchestrator_rejects_unknown_tool(tmp_path, monkeypatch):
    _bootstrap_db(tmp_path, monkeypatch)
    orchestrator = AgentOrchestrator()
    result = orchestrator.invoke(AgentAction(tool="not_a_tool", input={}))
    assert result.ok is False
    assert result.error_code == "TOOL_NOT_FOUND"


def test_orchestrator_invoke_many_returns_list(tmp_path, monkeypatch):
    _bootstrap_db(tmp_path, monkeypatch)
    orchestrator = AgentOrchestrator()
    actions = [
        AgentAction(tool="search_products", input={"keyword": "tea"}, call_id="a1"),
        AgentAction(tool="get_product_detail", input={"product_id": 1}, call_id="a2"),
    ]
    results = orchestrator.invoke_many(actions)
    assert len(results) == 2
    assert results[0].ok is True


# ---------- helpers ----------


def _bootstrap_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    import app.config as cfg
    import app.db as db_mod
    import app.repository as repo_mod
    import app.background as bg_mod
    import app.agent as agent_mod
    import app.agent.impl as agent_impl_mod
    import app.main as main_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(repo_mod)
    importlib.reload(bg_mod)
    importlib.reload(agent_impl_mod)
    importlib.reload(agent_mod)
    importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)

    # 直接 seed 一条商品 + 价格历史，便于 get_product_detail / get_price_history 测试
    from app.schemas import ProductRecord

    record = ProductRecord(
        source="fixture",
        external_product_id="tea-001",
        title="高山绿茶 250g",
        url="https://fixture.local/tea-001",
        category="茶饮",
        description="清香回甘",
        rating=Decimal("4.8"),
        current_price=Decimal("89.90"),
        observed_at=datetime(2026, 8, 30, 12, 0, 0),
    )
    with db_mod.SessionLocal() as db:
        repo_mod.upsert_product(db, record)
        db.commit()