"""Quality gate for content assets and listing claims."""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from PIL import Image

from app.workflow_schema import validate_workflow


@dataclass(frozen=True)
class QualityIssue:
    code: str
    severity: str
    message: str
    path: str | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class QualityGateResult:
    status: str
    issues: tuple[QualityIssue, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "issues": [{"code": x.code, "severity": x.severity, "message": x.message, "path": x.path, "evidence": list(x.evidence)} for x in self.issues]}


def run_quality_gate(*, content: dict, snapshot: dict, images: list[bytes], workflow: dict | None = None) -> QualityGateResult:
    issues: list[QualityIssue] = []
    if workflow is not None:
        try:
            validate_workflow(workflow)
        except ValueError as exc:
            issues.append(QualityIssue("WORKFLOW_INVALID", "error", str(exc), "workflow"))
    title = content.get("title")
    if not title:
        issues.append(QualityIssue("TITLE_REQUIRED", "error", "标题不能为空", "content.title"))
    points = content.get("selling_points") or []
    if not 1 <= len(points) <= 5:
        issues.append(QualityIssue("SELLING_POINTS_INVALID", "error", "卖点数量必须为 1 至 5 条", "content.selling_points"))
    serialized = str(content)
    for forbidden in ("保证", "绝对", "第一", "包治"):
        if forbidden in serialized:
            issues.append(QualityIssue("FORBIDDEN_CLAIM", "error", f"包含禁止承诺词：{forbidden}", "content"))
    for index, image in enumerate(images):
        try:
            with Image.open(io.BytesIO(image)) as checked:
                checked.verify()
                if checked.width < 240 or checked.height < 240:
                    issues.append(QualityIssue("IMAGE_DIMENSIONS_TOO_SMALL", "error", "候选图尺寸过小", f"assets[{index}]"))
        except Exception:
            issues.append(QualityIssue("IMAGE_INVALID", "error", "候选图无法解码", f"assets[{index}]"))
    if snapshot.get("current_price") and snapshot["current_price"] not in serialized:
        issues.append(QualityIssue("PRICE_EVIDENCE_MISSING", "warning", "内容未引用商品价格事实", "content", ("current_price",)))
    if not images:
        issues.append(QualityIssue("ASSET_REQUIRED", "error", "没有候选图片", "assets"))
    if any(item.severity == "error" for item in issues):
        status = "blocked"
    elif issues:
        status = "warn"
    else:
        status = "passed"
    return QualityGateResult(status, tuple(issues))
