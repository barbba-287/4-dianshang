"""Config-driven visual workflows for e-commerce product categories."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from app.workflow_schema import legacy_plan, validate_workflow, workflow_hash
from typing import Literal


WorkflowKey = Literal["package_preserve", "scene_extend", "model_atmosphere"]


@dataclass(frozen=True)
class VisualWorkflow:
    key: WorkflowKey
    label: str
    categories: tuple[str, ...]
    cutout: bool
    preserve_text: bool
    background_prompt: str
    post_process: tuple[str, ...]
    quality_checks: tuple[str, ...]
    requires_human_subject_check: bool = True
    version: str = "v1"

    def graph(self) -> dict:
        foreground_type = "foreground_cutout" if self.cutout else "source_geometry_check"
        graph = {
            "schema_version": "visual-workflow.v1",
            "workflow_key": self.key,
            "version": self.version,
            "label": self.label,
            "categories": list(self.categories),
            "metadata": {"description": self.label, "input_contract": "product+source_image", "output_contract": "candidate_assets"},
            "nodes": [
                {"node_id": "source_snapshot", "type": "source_snapshot", "label": "商品事实快照", "config": {}},
                {"node_id": "foreground", "type": foreground_type, "label": "主体保真", "config": {"cutout": self.cutout, "preserve_text": self.preserve_text}},
                {"node_id": "background", "type": "provider_generate", "label": "AI 背景", "config": {"prompt": self.background_prompt}},
                {"node_id": "post_process", "type": "deterministic_post_process", "label": "后处理排版", "config": {"operations": list(self.post_process)}},
                {"node_id": "quality", "type": "quality_gate", "label": "品类质量门禁", "config": {"checks": list(self.quality_checks)}},
                {"node_id": "review", "type": "human_review", "label": "人工审核", "config": {"required": self.requires_human_subject_check}},
                {"node_id": "export", "type": "export", "label": "导出", "config": {"formats": ["json", "markdown"]}},
            ],
            "edges": [{"edge_id": f"e{index}", "source": source, "target": target} for index, (source, target) in enumerate((("source_snapshot", "foreground"), ("foreground", "background"), ("background", "post_process"), ("post_process", "quality"), ("quality", "review"), ("review", "export")), 1)],
            "guardrails": {"workspace_required": True, "source_snapshot_required": True, "human_review_required": True},
        }
        return validate_workflow(graph)

    def plan(self) -> dict:
        return {**legacy_plan(self.graph()), "cutout": self.cutout, "preserve_text": self.preserve_text, "background_prompt": self.background_prompt, "post_process": list(self.post_process), "quality_checks": list(self.quality_checks), "requires_human_subject_check": self.requires_human_subject_check}


WORKFLOWS: dict[str, VisualWorkflow] = {
    "package_preserve": VisualWorkflow(
        key="package_preserve",
        label="包装保真型",
        categories=("食品", "茶饮", "美妆", "保健品"),
        cutout=True,
        preserve_text=True,
        background_prompt="干净、符合品牌调性的生活场景背景；主体区域保持清晰，避免生成文字和新包装",
        post_process=("preserve_subject", "render_copy", "render_price", "render_selling_points"),
        quality_checks=("text_intact", "logo_visible", "subject_geometry", "no_fabricated_claims"),
    ),
    "scene_extend": VisualWorkflow(
        key="scene_extend",
        label="场景延展型",
        categories=("家居", "3C数码", "家电", "办公用品"),
        cutout=False,
        preserve_text=False,
        background_prompt="真实使用场景，自然光线，比例和透视可信，不改变产品结构",
        post_process=("render_copy", "render_price"),
        quality_checks=("proportion_correct", "ports_and_controls", "no_fabricated_specs"),
    ),
    "model_atmosphere": VisualWorkflow(
        key="model_atmosphere",
        label="模特/氛围型",
        categories=("服装", "鞋包", "配饰"),
        cutout=True,
        preserve_text=True,
        background_prompt="符合目标人群和品牌调性的模特或生活方式场景，保留商品版型、材质和颜色",
        post_process=("preserve_subject", "render_copy", "render_selling_points"),
        quality_checks=("fit_and_material", "color_consistency", "logo_visible", "no_fabricated_claims"),
    ),
}


def get_workflow(key: str) -> VisualWorkflow:
    try:
        return WORKFLOWS[key]
    except KeyError as exc:
        raise ValueError(f"UNKNOWN_VISUAL_WORKFLOW:{key}") from exc


def suggest_workflow(category: str | None) -> VisualWorkflow:
    normalized = (category or "").strip().lower()
    for workflow in WORKFLOWS.values():
        if any(item.lower() in normalized or normalized in item.lower() for item in workflow.categories):
            return workflow
    return WORKFLOWS["package_preserve"]


def resolve_workflow(*, workflow_key: str | None = None, category: str | None = None) -> VisualWorkflow:
    """Resolve an explicit workflow or provide a category suggestion."""
    if workflow_key:
        return get_workflow(workflow_key)
    return suggest_workflow(category)


def customize_workflow(workflow: VisualWorkflow, *, background_prompt: str | None = None, post_process: tuple[str, ...] | None = None, quality_checks: tuple[str, ...] | None = None, version: str | None = None) -> VisualWorkflow:
    """Create a reusable derived version without mutating the base template."""
    if background_prompt is not None and not background_prompt.strip():
        raise ValueError("BACKGROUND_PROMPT_REQUIRED")
    return replace(
        workflow,
        background_prompt=background_prompt.strip() if background_prompt is not None else workflow.background_prompt,
        post_process=post_process if post_process is not None else workflow.post_process,
        quality_checks=quality_checks if quality_checks is not None else workflow.quality_checks,
        version=version or f"{workflow.version}.custom",
    )


def snapshot_workflow(workflow: VisualWorkflow) -> dict:
    """Persist a task-safe immutable graph snapshot."""
    graph = workflow.graph()
    graph["workflow_hash"] = workflow_hash(graph)
    return graph


def build_workflow_prompt(workflow: VisualWorkflow, *, product_title: str, brief: str = "") -> str:
    preserve = "必须保留商品主体、包装文字、Logo、颜色和关键结构；" if workflow.preserve_text else "必须保留商品主体结构、比例和关键功能部件；"
    return f"{preserve}{workflow.background_prompt}。商品：{product_title}。运营要求：{brief}"[:1800]
