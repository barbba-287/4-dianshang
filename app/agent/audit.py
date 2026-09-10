"""Agent 审计日志：记录每次工具调用的工具名、参数摘要、结果码、耗时。"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any


@dataclass
class AgentAuditRecord:
    timestamp: str
    tool: str
    ok: bool
    duration_ms: int
    tenant_id: str = "default"
    workspace_id: int | None = None
    user_id: str | None = None
    request_id: str | None = None
    input_summary: str = ""
    output_summary: str = ""
    error_code: str | None = None
    extras: dict = field(default_factory=dict)


class AgentAuditLogger:
    """结构化审计日志，输出到 logger；可选持久化到 JSONL 文件。"""

    def __init__(self, file_path: str | Path | None = None):
        self._logger = logging.getLogger("agent.audit")
        self._lock = Lock()
        self._path = Path(file_path) if file_path else None
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, record: AgentAuditRecord) -> None:
        line = json.dumps(asdict(record), ensure_ascii=False)
        self._logger.info(line)
        if self._path is not None:
            with self._lock:
                with self._path.open("a", encoding="utf-8") as fp:
                    fp.write(line + "\n")

    def list_records(self, limit: int = 100) -> list[dict]:
        if limit <= 0 or self._path is None or not self._path.exists():
            return []
        with self._lock:
            with self._path.open("r", encoding="utf-8") as fp:
                lines = fp.readlines()[-limit:]
        records: list[dict] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                self._logger.warning("跳过损坏的 Agent 审计记录")
        return records


def now_iso() -> str:
    return datetime.utcnow().isoformat()


def summarise(payload: Any, *, max_len: int = 200) -> str:
    """序列化摘要并遮蔽常见敏感字段。"""
    sensitive = {
        "password", "passwd", "token", "secret", "authorization", "cookie",
        "apikey", "accesstoken", "key",
    }

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            scrubbed = {}
            for key, item in value.items():
                normalised = "".join(char.lower() for char in str(key) if char.isalnum())
                scrubbed[key] = "[REDACTED]" if normalised in sensitive else scrub(item)
            return scrubbed
        if isinstance(value, (list, tuple)):
            return [scrub(item) for item in value]
        return value

    try:
        text = json.dumps(scrub(payload), ensure_ascii=False)
    except TypeError:
        text = repr(payload)
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text
