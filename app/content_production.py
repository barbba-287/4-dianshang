"""AIGC 商品页图片与上架文案的 provider-neutral 生产服务。"""
from __future__ import annotations

import hashlib
import io
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import CrawlJob, Product, ProductContentExport, ProductContentRevision, ProductMediaAsset
from app.storage import StorageError
from app.upload_security import safe_storage_path
from app.quality_gate import run_quality_gate


class ContentProviderError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


@dataclass(frozen=True)
class GenerationSpec:
    scene: str
    style: str
    operation: str
    width: int
    height: int
    candidate_count: int
    prompt: str
    negative_prompt: str


@dataclass(frozen=True)
class GenerationResult:
    images: list[bytes]
    content: dict
    provider: str
    model: str | None
    simulated: bool
    source_mode: str
    request_id: str | None = None


class ImageProvider(Protocol):
    name: str
    simulated: bool

    def generate_or_edit(self, *, source_image: bytes | None, spec: GenerationSpec, snapshot: dict, cancel_check) -> GenerationResult:
        ...


def _font(size: int):
    for candidate in (os.environ.get("AIGC_FONT_PATH", ""), "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf", "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"):
        if candidate and Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


class LocalCompositorProvider:
    """无外部模型时的可复现图片 fallback，不冒充文生图。"""

    name = "local_compositor"
    simulated = True

    def generate_or_edit(self, *, source_image: bytes | None, spec: GenerationSpec, snapshot: dict, cancel_check) -> GenerationResult:
        source = None
        if source_image:
            try:
                source = Image.open(io.BytesIO(source_image)).convert("RGBA")
            except (OSError, ValueError) as exc:
                raise ContentProviderError("SOURCE_IMAGE_INVALID", "商品基础图无法读取") from exc
        title = str(snapshot.get("title") or "商品")
        subtitle = str(snapshot.get("category") or "商品页素材")
        images: list[bytes] = []
        for index in range(spec.candidate_count):
            if cancel_check():
                raise ContentProviderError("CANCELLED", "生成任务已取消")
            canvas = Image.new("RGB", (spec.width, spec.height), ((242 + index * 3) % 256, 247, 252))
            draw = ImageDraw.Draw(canvas)
            if source is not None:
                source_copy = source.copy()
                source_copy.thumbnail((spec.width // 2, spec.height // 2))
                left = (spec.width - source_copy.width) // 2
                top = max(80, (spec.height - source_copy.height) // 2 - 30)
                canvas.paste(source_copy, (left, top), source_copy)
            else:
                box = (spec.width // 4, spec.height // 4, spec.width * 3 // 4, spec.height * 3 // 4)
                draw.rounded_rectangle(box, radius=24, fill=(210, 226, 241), outline=(80, 120, 160), width=3)
                draw.text((box[0] + 20, (box[1] + box[3]) // 2), "基础图待上传", font=_font(28), fill=(50, 75, 100))
            draw.rectangle((0, 0, spec.width, 82), fill=(20, 47, 76))
            draw.text((32, 23), f"候选 {index + 1} · {spec.scene}", font=_font(27), fill="white")
            draw.rectangle((0, spec.height - 150, spec.width, spec.height), fill="white")
            draw.text((32, spec.height - 125), title[:30], font=_font(30), fill=(20, 32, 50))
            draw.text((32, spec.height - 78), subtitle[:32], font=_font(22), fill=(80, 100, 120))
            output = io.BytesIO()
            canvas.save(output, format="PNG", optimize=True)
            images.append(output.getvalue())
        return GenerationResult(images=images, content=build_listing_content(snapshot=snapshot, spec=spec, provider=self.name), provider=self.name, model="local-compositor-v1", simulated=True, source_mode="local")

class WanImageEditProvider:
    """阿里云百炼万相 2.7 多模态图片编辑适配器。"""

    name = "wan"
    simulated = False

    def generate_or_edit(self, *, source_image: bytes | None, spec: GenerationSpec, snapshot: dict, cancel_check) -> GenerationResult:
        settings = get_settings()
        if not settings.aigc_api_key:
            raise ContentProviderError("AIGC_PROVIDER_NOT_CONFIGURED", "万相 Provider 未配置 API Key")
        if source_image is None:
            raise ContentProviderError("SOURCE_IMAGE_REQUIRED", "万相图像编辑需要商品基础图")
        if cancel_check():
            raise ContentProviderError("CANCELLED", "生成任务已取消")
        try:
            import base64
            import urllib.request
            from dashscope.aigc.image_generation import ImageGeneration
            from dashscope.api_entities.dashscope_response import Message

            image_data = f"data:image/png;base64,{base64.b64encode(source_image).decode('ascii')}"
            message = Message(role="user", content=[{"text": spec.prompt}, {"image": image_data}])
            response = ImageGeneration.call(
                model=settings.aigc_image_model,
                api_key=settings.aigc_api_key,
                workspace=settings.aigc_workspace or None,
                messages=[message],
                n=spec.candidate_count,
                size="2K",
                watermark=False,
            )
            if getattr(response, "status_code", None) != 200:
                raise ContentProviderError(
                    getattr(response, "code", None) or "AIGC_PROVIDER_ERROR",
                    "万相返回错误",
                )
            images: list[bytes] = []
            for choice in getattr(getattr(response, "output", None), "choices", None) or []:
                for item in getattr(getattr(choice, "message", None), "content", None) or []:
                    if isinstance(item, dict) and item.get("type") == "image" and item.get("image"):
                        if cancel_check():
                            raise ContentProviderError("CANCELLED", "生成任务已取消")
                        with urllib.request.urlopen(item["image"], timeout=settings.aigc_image_timeout_seconds) as stream:
                            images.append(stream.read())
            if not images:
                raise ContentProviderError("AIGC_EMPTY_RESULT", "万相未返回图片")
            return GenerationResult(
                images=images,
                content=build_listing_content(snapshot=snapshot, spec=spec, provider=self.name),
                provider=self.name,
                model=settings.aigc_image_model,
                simulated=False,
                source_mode="live",
                request_id=getattr(response, "request_id", None),
            )
        except ContentProviderError:
            raise
        except Exception as exc:
            detail = str(exc)[:300] or "万相调用失败"
            raise ContentProviderError("AIGC_PROVIDER_ERROR", detail) from exc


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_snapshot(product: Product, sku=None) -> dict:
    snapshot = {"product_id": product.id, "title": product.title, "description": product.description or "", "category": product.category or "", "currency": product.currency, "current_price": str(product.current_price), "source": product.source, "external_product_id": product.external_product_id}
    if sku is not None:
        snapshot.update({"sku_id": sku.id, "sku_code": sku.sku_code, "variant_label": sku.variant_label or "", "unit": sku.unit})
    return snapshot


def snapshot_hash(snapshot: dict) -> str:
    return hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()


def build_spec(*, scene: str, style: str, operation: str, width: int, height: int, candidate_count: int, snapshot: dict, brief: str = "") -> GenerationSpec:
    title, category = snapshot.get("title") or "商品", snapshot.get("category") or "电商商品"
    prompt = f"电商商品页{scene}，保留商品主体、包装、颜色和比例，商品名称为{title}，类目为{category}；{brief}；{style}风格，主体清晰，适合商品上架"
    return GenerationSpec(scene=scene, style=style, operation=operation, width=width, height=height, candidate_count=candidate_count, prompt=prompt[:1800], negative_prompt="商品变形，包装改字，Logo错误，虚构配件，虚构规格，虚构功效，低清晰度，水印，乱码")


def build_listing_content(*, snapshot: dict, spec: GenerationSpec, provider: str) -> dict:
    title = str(snapshot.get("title") or "商品")
    description = str(snapshot.get("description") or "")
    category = str(snapshot.get("category") or "商品")
    points = [p.strip() for p in description.replace("。", "，").split("，") if p.strip()][:3] or [f"{category}商品信息可追溯", "支持人工审核后导出", "图片与文案按商品事实生成"]
    return {"title": title[:60], "short_title": title[:30], "selling_points": points, "detail_sections": [{"heading": "商品说明", "body": description or "待运营补充商品说明"}], "specifications": {key: snapshot[key] for key in ("sku_code", "variant_label", "unit", "category") if snapshot.get(key)}, "seo_keywords": [category, title[:20]], "image_alt": f"{title}商品页主图", "claims": [{"claim": point, "evidence_fields": ["description"] if snapshot.get("description") else [], "verification": "source_snapshot"} for point in points], "generation": {"scene": spec.scene, "style": spec.style, "operation": spec.operation, "prompt": spec.prompt, "negative_prompt": spec.negative_prompt, "provider": provider}}


def quality_check(content: dict, snapshot: dict) -> tuple[str, list[dict]]:
    issues: list[dict] = []
    if not content.get("title"):
        issues.append({"code": "TITLE_REQUIRED", "message": "标题不能为空"})
    if not content.get("selling_points") or not 1 <= len(content["selling_points"]) <= 5:
        issues.append({"code": "SELLING_POINTS_INVALID", "message": "卖点数量必须为 1 至 5 条"})
    serialized = canonical_json(content)
    for forbidden in ("保证", "绝对", "第一", "包治"):
        if forbidden in serialized:
            issues.append({"code": "FORBIDDEN_CLAIM", "message": f"包含禁止承诺词：{forbidden}"})
    return ("blocked" if issues else "passed"), issues


def _artifact_root() -> Path:
    root = get_settings().resolve_path(get_settings().artifacts_dir) / "content"
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_asset_bytes(*, workspace_id: int, run_id: str, name: str, data: bytes) -> str:
    if not data:
        raise StorageError("EMPTY_ASSET", "图片内容为空")
    root = _artifact_root().resolve()
    target = safe_storage_path(root, f"{workspace_id}/{run_id}/{name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_bytes(data)
    os.replace(temp, target)
    return target.relative_to(root).as_posix()


def _get_image_provider(provider: str) -> ImageProvider:
    if provider == "wan":
        return WanImageEditProvider()
    if provider in {"local", "local_compositor", "mock"}:
        return LocalCompositorProvider()
    raise ContentProviderError("UNKNOWN_PROVIDER", "当前未启用该图片 Provider")


def _load_source_image(db: Session, *, workspace_id: int, product_id: int, asset_id: int | None = None, expected_hash: str | None = None) -> bytes | None:
    query = select(ProductMediaAsset).where(ProductMediaAsset.workspace_id == workspace_id, ProductMediaAsset.product_id == product_id, ProductMediaAsset.role == "source")
    if asset_id is not None:
        query = query.where(ProductMediaAsset.id == asset_id)
    else:
        query = query.order_by(ProductMediaAsset.id.desc())
    asset = db.scalar(query)
    if asset is None:
        return None
    try:
        content = safe_storage_path(_artifact_root().resolve(), asset.storage_uri).read_bytes()
    except OSError as exc:
        raise ContentProviderError("SOURCE_ASSET_MISSING", "商品基础图不存在") from exc
    actual_hash = hashlib.sha256(content).hexdigest()
    if expected_hash and actual_hash != expected_hash:
        raise ContentProviderError("SOURCE_ASSET_CHANGED", "任务绑定的基础图已发生变化")
    return content


def execute_generation_job(db: Session, job: CrawlJob, cancel_check) -> None:
    payload = json.loads(job.payload_json or job.cursor or "{}")
    revision = db.scalar(select(ProductContentRevision).where(ProductContentRevision.id == int(payload["revision_id"]), ProductContentRevision.workspace_id == job.workspace_id))
    if revision is None:
        raise ContentProviderError("CONTENT_REVISION_NOT_FOUND", "内容版本不存在")
    product = db.scalar(select(Product).where(Product.id == revision.product_id, Product.workspace_id == job.workspace_id))
    if product is None:
        raise ContentProviderError("PRODUCT_NOT_FOUND", "商品不存在")
    snapshot = json.loads(revision.source_snapshot_json)
    spec_data = json.loads(revision.prompt_snapshot or "{}")
    spec = GenerationSpec(**{key: spec_data[key] for key in GenerationSpec.__dataclass_fields__})
    source_descriptor = (snapshot.get("source_assets") or [None])[0]
    source_image = _load_source_image(db, workspace_id=job.workspace_id, product_id=product.id, asset_id=source_descriptor.get("asset_id") if source_descriptor else None, expected_hash=source_descriptor.get("sha256") if source_descriptor else None)
    result = _get_image_provider(payload.get("provider", "local")).generate_or_edit(source_image=source_image, spec=spec, snapshot=snapshot, cancel_check=cancel_check)
    quality_status, issues = quality_check(result.content, snapshot)
    gate = run_quality_gate(content=result.content, snapshot=snapshot, images=result.images)
    if gate.status == "blocked":
        quality_status = "blocked"
    elif gate.status == "warn" and quality_status == "passed":
        quality_status = "warn"
    issues = [{"code": item.code, "severity": item.severity, "message": item.message, "path": item.path, "evidence": list(item.evidence)} for item in gate.issues] + issues
    asset_ids = []
    for index, image in enumerate(result.images, start=1):
        path = save_asset_bytes(workspace_id=job.workspace_id, run_id=job.run_id or str(job.id), name=f"candidate-{index:02d}.png", data=image)
        with Image.open(io.BytesIO(image)) as checked:
            width, height = checked.size
        asset = ProductMediaAsset(workspace_id=job.workspace_id, product_id=product.id, revision_id=revision.id, role="candidate", storage_uri=path, content_sha256=hashlib.sha256(image).hexdigest(), mime_type="image/png", size_bytes=len(image), width=width, height=height, provider=result.provider, provider_model=result.model, provider_request_id=result.request_id, source_mode=result.source_mode, simulated=result.simulated, prompt_snapshot=spec.prompt, params_json=canonical_json({"width": spec.width, "height": spec.height, "candidate_count": spec.candidate_count, "scene": spec.scene}), selected=index == 1)
        db.add(asset)
        db.flush()
        asset_ids.append(asset.id)
    result.content.update({"asset_ids": asset_ids, "source_snapshot_hash": revision.source_snapshot_hash, "simulated": result.simulated, "provider": result.provider})
    revision.content_json = canonical_json(result.content)
    revision.content_hash = hashlib.sha256(revision.content_json.encode("utf-8")).hexdigest()
    revision.provider, revision.provider_model, revision.provider_request_id = result.provider, result.model, result.request_id
    revision.source_mode, revision.simulated, revision.quality_status = result.source_mode, result.simulated, quality_status
    revision.quality_issues_json = canonical_json(issues)
    revision.status = "quality_failed" if quality_status == "blocked" else "draft"
    revision.updated_at = datetime.utcnow()
    db.commit()
    job.cursor = canonical_json({"revision_id": revision.id, "asset_ids": asset_ids, "quality_status": quality_status})


def _listing_markdown(content: dict) -> str:
    lines = [f"# {content.get('title', '')}", "", f"## 短标题\n{content.get('short_title', '')}", "", "## 核心卖点"]
    lines.extend(f"- {item}" for item in content.get("selling_points", []))
    lines.append("\n## 详情")
    for section in content.get("detail_sections", []):
        lines.extend([f"### {section.get('heading', '')}", section.get("body", ""), ""])
    lines.append(f"## 图片 alt\n{content.get('image_alt', '')}")
    return "\n".join(lines) + "\n"


def execute_export_job(db: Session, job: CrawlJob, cancel_check) -> None:
    payload = json.loads(job.payload_json or job.cursor or "{}")
    export = db.scalar(select(ProductContentExport).where(ProductContentExport.id == int(payload["export_id"]), ProductContentExport.workspace_id == job.workspace_id))
    if export is None:
        raise ContentProviderError("CONTENT_EXPORT_NOT_FOUND", "导出任务不存在")
    revision = db.scalar(select(ProductContentRevision).where(ProductContentRevision.id == export.revision_id, ProductContentRevision.workspace_id == job.workspace_id, ProductContentRevision.status == "approved"))
    if revision is None:
        raise ContentProviderError("CONTENT_NOT_APPROVED", "只有审核通过的版本可以导出")
    content = json.loads(revision.content_json or "{}")
    assets = db.scalars(select(ProductMediaAsset).where(ProductMediaAsset.revision_id == revision.id, ProductMediaAsset.workspace_id == job.workspace_id).order_by(ProductMediaAsset.id)).all()
    root = _artifact_root().resolve()
    manifest = {"revision_id": revision.id, "revision_no": revision.revision_no, "content_hash": revision.content_hash, "source_snapshot_hash": revision.source_snapshot_hash, "provider": revision.provider, "simulated": revision.simulated, "assets": [{"asset_id": row.id, "storage_uri": row.storage_uri, "sha256": row.content_sha256, "selected": row.selected} for row in assets]}
    target = safe_storage_path(root, f"exports/{job.workspace_id}/{export.id}")
    target.mkdir(parents=True, exist_ok=True)
    (target / "listing.json").write_text(canonical_json(content) + "\n", encoding="utf-8")
    (target / "listing.md").write_text(_listing_markdown(content), encoding="utf-8")
    (target / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    for row in assets:
        (target / f"candidate-{row.id:02d}.png").write_bytes(safe_storage_path(root, row.storage_uri).read_bytes())
    artifact = (canonical_json({"listing": content, "manifest": manifest}) + "\n").encode("utf-8")
    export.artifact_uri = save_asset_bytes(workspace_id=job.workspace_id, run_id=f"exports/{export.id}", name="package.json", data=artifact)
    export.artifact_sha256, export.artifact_size = hashlib.sha256(artifact).hexdigest(), len(artifact)
    export.manifest_json, export.status, export.finished_at, export.simulated = canonical_json(manifest), "succeeded", datetime.utcnow(), revision.simulated
    db.commit()
