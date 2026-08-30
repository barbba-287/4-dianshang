"""电商工作台的可控 Agent 子包。

提供只读工具注册表、参数校验、审计日志、调用次数上限与超时控制。
S5 阶段只暴露 3 个只读工具；任何写入或订单相关操作均未实现。
"""

from app.agent.audit import AgentAuditLogger, AgentAuditRecord
from app.agent.registry import (
    AgentTool,
    AgentToolRegistry,
    ToolCallResult,
    ToolNotAllowed,
    ToolValidationError,
    default_registry,
)
from app.agent import impl  # noqa: F401  触发默认工具注册

__all__ = [
    "AgentAuditLogger",
    "AgentAuditRecord",
    "AgentTool",
    "AgentToolRegistry",
    "ToolCallResult",
    "ToolNotAllowed",
    "ToolValidationError",
    "default_registry",
]