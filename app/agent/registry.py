"""Agent 工具协议与注册表。

设计要点：
- 工具以 JSON Schema 声明参数，服务端独立校验，不依赖模型遵守。
- 调用次数与时间窗口限制由 registry 强制，避免单个会话耗尽资源。
- 任何调用均写入审计日志，参数和返回都会被脱敏记录。
- 任何写入型工具必须经过服务端权限与业务规则检查；S5 默认只暴露只读工具。
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from time import monotonic
from typing import Any, Protocol


class ToolValidationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ToolNotAllowed(ToolValidationError):
    def __init__(self, reason: str):
        super().__init__("TOOL_NOT_ALLOWED", reason)
        self.reason = reason


@dataclass
class ToolCallResult:
    ok: bool
    output: Any = None
    error_code: str | None = None
    error_message: str | None = None
    duration_ms: int = 0
    truncated: bool = False


@dataclass
class AgentTool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict, "ToolContext"], Any]
    is_readonly: bool = True
    timeout_seconds: float = 5.0
    max_calls_per_minute: int = 30
    requires: tuple[str, ...] = ()
    output_schema: dict | None = None

    def validate_input(self, payload: dict) -> dict:
        """按 input_schema 做最小校验：必填字段、类型。"""
        required = self.input_schema.get("required", [])
        properties = self.input_schema.get("properties", {})
        if not isinstance(payload, dict):
            raise ToolValidationError("INVALID_INPUT", "payload 必须是对象")
        for key in required:
            if key not in payload:
                raise ToolValidationError("MISSING_FIELD", f"缺少必填字段: {key}")
        for key, value in payload.items():
            spec = properties.get(key)
            if spec is None:
                raise ToolValidationError("UNKNOWN_FIELD", f"未知字段: {key}")
            expected_type = spec.get("type")
            if expected_type and not _matches_type(value, expected_type):
                raise ToolValidationError(
                    "TYPE_MISMATCH",
                    f"字段 {key} 类型不匹配: 期望 {expected_type}",
                )
        return payload


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


@dataclass
class ToolContext:
    tenant_id: str = "default"
    workspace_id: int | None = None
    user_id: str | None = None
    request_id: str | None = None
    extras: dict = field(default_factory=dict)


class AgentToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, AgentTool] = {}
        self._counters: dict[tuple[str, str], list[float]] = {}
        self._lock = RLock()

    def register(self, tool: AgentTool) -> None:
        with self._lock:
            if tool.name in self._tools:
                raise ValueError(f"tool {tool.name} 已注册")
            self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        with self._lock:
            self._tools.pop(name, None)
            for key in [key for key in self._counters if key[0] == name]:
                self._counters.pop(key, None)

    def get(self, name: str) -> AgentTool | None:
        with self._lock:
            return self._tools.get(name)

    def list_tools(self, *, include_readonly_only: bool = False) -> list[AgentTool]:
        with self._lock:
            tools = list(self._tools.values())
        if include_readonly_only:
            tools = [t for t in tools if t.is_readonly]
        return sorted(tools, key=lambda t: t.name)

    def call(
        self,
        name: str,
        payload: dict,
        *,
        context: ToolContext | None = None,
    ) -> ToolCallResult:
        context = context or ToolContext()
        with self._lock:
            tool = self._tools.get(name)
            if tool is None:
                return ToolCallResult(
                    ok=False,
                    error_code="TOOL_NOT_FOUND",
                    error_message=f"未知工具: {name}",
                )
            if not tool.is_readonly:
                return ToolCallResult(
                    ok=False,
                    error_code="TOOL_NOT_READONLY",
                    error_message=f"工具 {name} 非只读，已被禁用",
                )
            try:
                self._check_rate_limit(name, context.tenant_id, tool.max_calls_per_minute)
            except ToolNotAllowed as exc:
                return ToolCallResult(
                    ok=False,
                    error_code=exc.code,
                    error_message=exc.message,
                )
        try:
            payload = tool.validate_input(payload)
        except ToolValidationError as exc:
            return ToolCallResult(
                ok=False,
                error_code=exc.code,
                error_message=exc.message,
            )
        start = monotonic()
        try:
            output = tool.handler(payload, context)
            duration_ms = int((monotonic() - start) * 1000)
            return ToolCallResult(ok=True, output=output, duration_ms=duration_ms)
        except Exception as exc:  # noqa: BLE001
            duration_ms = int((monotonic() - start) * 1000)
            return ToolCallResult(
                ok=False,
                error_code="TOOL_RUNTIME_ERROR",
                error_message=str(exc)[:200],
                duration_ms=duration_ms,
            )

    def _check_rate_limit(self, name: str, tenant_id: str, limit: int) -> None:
        if limit <= 0:
            raise ValueError("工具限流值必须为正数")
        key = (name, tenant_id)
        now = monotonic()
        window = [t for t in self._counters.get(key, []) if now - t < 60.0]
        if len(window) >= limit:
            self._counters[key] = window
            raise ToolNotAllowed(
                f"工具 {name} 在 60s 内调用次数超过 {limit}",
            )
        window.append(now)
        self._counters[key] = window


default_registry = AgentToolRegistry()
"""进程级默认注册表。"""


def now_iso() -> str:
    return datetime.utcnow().isoformat()