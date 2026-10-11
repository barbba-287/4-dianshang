"""商品内容生产与审核应用服务。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json

import sqlalchemy.exc
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.content_production import build_snapshot, build_spec, canonical_json, snapshot_hash
from app.db import (
    CrawlJob,
    Product,
    ProductContentAction,
    ProductContentRevision,
    ProductMediaAsset,
    ProductSku,
)
from app.repository import enqueue_job
from app.visual_workflows import build_workflow_prompt, resolve_workflow, snapshot_workflow


@dataclass(frozen=True)
class ContentApplicationError(Exception):
    """内容应用边界的稳定错误；HTTP 状态码由接口层决定。"""

    code: str
    message: str

    def __str__(self) -> str:
        return self.code


class ContentApplicationService:
    """编排商品内容任务和审核状态，不拥有认证或 HTTP 语义。"""

    def __init__(self, db: Session):
        self.db = db

    def create_generation(
        self,
        *,
        workspace_id: int,
        actor: str,
        idempotency_key: str,
        product_id: int,
        sku_id: int | None,
        scene: str,
        style: str,
        operation: str,
        provider: str,
        brief: str,
        width: int,
        height: int,
        candidate_count: int,
        workflow_key: str | None,
    ) -> dict:
        product = self.db.scalar(
            select(Product).where(Product.id == product_id, Product.workspace_id == workspace_id)
        )
        if product is None:
            raise ContentApplicationError("PRODUCT_NOT_FOUND", "商品不存在")
        sku = None
        if sku_id is not None:
            sku = self.db.scalar(
                select(ProductSku).where(
                    ProductSku.id == sku_id,
                    ProductSku.product_id == product.id,
                    ProductSku.workspace_id == workspace_id,
                )
            )
            if sku is None:
                raise ContentApplicationError("SKU_NOT_FOUND", "SKU 不属于当前商品")

        source_asset = self.db.scalar(
            select(ProductMediaAsset)
            .where(
                ProductMediaAsset.workspace_id == workspace_id,
                ProductMediaAsset.product_id == product.id,
                ProductMediaAsset.role == "source",
                ProductMediaAsset.selected.is_(True),
            )
            .order_by(ProductMediaAsset.id.desc())
        )
        source_descriptor = None
        if source_asset is not None:
            source_descriptor = {
                "asset_id": source_asset.id,
                "sha256": source_asset.content_sha256,
                "mime_type": source_asset.mime_type,
                "width": source_asset.width,
                "height": source_asset.height,
            }
        snapshot = build_snapshot(product, sku)
        if source_descriptor:
            snapshot["source_assets"] = [source_descriptor]
        try:
            workflow = resolve_workflow(workflow_key=workflow_key, category=product.category)
            spec = build_spec(
                scene=scene,
                style=style,
                operation=operation,
                width=width,
                height=height,
                candidate_count=candidate_count,
                snapshot=snapshot,
                brief=build_workflow_prompt(workflow, product_title=product.title, brief=brief),
            )
        except ValueError as exc:
            code = str(exc) or "CONTENT_INPUT_INVALID"
            raise ContentApplicationError(code, "商品内容参数无效") from exc

        payload = {
            "product_id": product_id,
            "sku_id": sku_id,
            "scene": scene,
            "style": style,
            "operation": operation,
            "provider": provider,
            "brief": brief,
            "width": width,
            "height": height,
            "candidate_count": candidate_count,
            "workflow_key": workflow_key,
        }
        body_hash = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        existing = self.db.scalar(
            select(ProductContentRevision).where(
                ProductContentRevision.workspace_id == workspace_id,
                ProductContentRevision.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if existing.payload_hash != body_hash:
                raise ContentApplicationError("IDEMPOTENCY_KEY_REUSE", "幂等键对应请求内容已改变")
            job = self.db.get(CrawlJob, existing.generation_job_id) if existing.generation_job_id else None
            return {"revision": existing, "job": job, "replayed": True}

        max_revision = self.db.scalar(
            select(func.max(ProductContentRevision.revision_no)).where(
                ProductContentRevision.workspace_id == workspace_id,
                ProductContentRevision.product_id == product.id,
            )
        ) or 0
        revision = ProductContentRevision(
            workspace_id=workspace_id,
            product_id=product.id,
            sku_id=sku_id,
            revision_no=max_revision + 1,
            status="generating",
            channel="generic",
            source_snapshot_json=canonical_json(snapshot),
            source_snapshot_hash=snapshot_hash(snapshot),
            provider=provider,
            source_mode="live" if provider == "wan" else "local",
            simulated=provider != "wan",
            prompt_snapshot=canonical_json({**spec.__dict__, "workflow": snapshot_workflow(workflow)}),
            created_by=actor,
            version=1,
            idempotency_key=idempotency_key,
            payload_hash=body_hash,
        )
        self.db.add(revision)
        self.db.flush()
        job = enqueue_job(
            self.db,
            type_="content_generate",
            source=provider,
            keyword=scene,
            payload={"revision_id": revision.id, "provider": provider},
            workspace_id=workspace_id,
            idempotency_key=idempotency_key,
            payload_hash=body_hash,
        )
        revision.generation_job_id = job.id
        self.db.commit()
        self.db.refresh(revision)
        return {"revision": revision, "job": job, "replayed": False}

    def export_revision(
        self,
        *,
        workspace_id: int,
        revision_id: int,
        request_revision_id: int,
        export_format: str,
        idempotency_key: str,
        actor: str,
    ) -> dict:
        """Queue export for an approved revision while preserving legacy hash semantics."""
        if request_revision_id != revision_id:
            raise ContentApplicationError("REVISION_MISMATCH", "版本 ID 不匹配")
        if not idempotency_key:
            raise ContentApplicationError("IDEMPOTENCY_KEY_REQUIRED", "必须提供 Idempotency-Key")
        revision = self.db.scalar(
            select(ProductContentRevision).where(
                ProductContentRevision.id == revision_id,
                ProductContentRevision.workspace_id == workspace_id,
                ProductContentRevision.status == "approved",
            )
        )
        if revision is None:
            raise ContentApplicationError("CONTENT_NOT_APPROVED", "只有审核通过的版本可以导出")
        payload = {"revision_id": request_revision_id, "format": export_format}
        # Keep the historical default json.dumps separators for persisted-key compatibility.
        body_hash = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        from app.db import ProductContentExport

        existing = self.db.scalar(
            select(ProductContentExport).where(
                ProductContentExport.workspace_id == workspace_id,
                ProductContentExport.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if existing.payload_hash != body_hash:
                raise ContentApplicationError("IDEMPOTENCY_KEY_REUSE", "幂等键对应内容已改变")
            return {"export": existing, "job": None, "replayed": True}
        export = ProductContentExport(
            workspace_id=workspace_id,
            revision_id=revision.id,
            format=export_format,
            status="queued",
            simulated=revision.simulated,
            idempotency_key=idempotency_key,
            payload_hash=body_hash,
            created_by=actor,
        )
        self.db.add(export)
        try:
            self.db.flush()
            job = enqueue_job(
                self.db,
                type_="content_export",
                source="content",
                keyword=export_format,
                payload={"export_id": export.id},
                workspace_id=workspace_id,
                idempotency_key=f"export:{idempotency_key}",
                payload_hash=body_hash,
            )
        except sqlalchemy.exc.IntegrityError as exc:
            # 并发同键命中 (workspace_id, idempotency_key) 唯一约束：该约束非
            # deferrable，flush 或 enqueue_job 的 commit 阶段都可能触发冲突。
            # 回滚本次未落库的 export，重查竞争者，按 payload_hash 区分重放/键复用。
            self.db.rollback()
            competing = self.db.scalar(
                select(ProductContentExport).where(
                    ProductContentExport.workspace_id == workspace_id,
                    ProductContentExport.idempotency_key == idempotency_key,
                )
            )
            if competing is None:
                raise ContentApplicationError("CONTENT_EXPORT_PERSIST_FAILED", "导出任务无法持久化") from exc
            if competing.payload_hash != body_hash:
                raise ContentApplicationError("IDEMPOTENCY_KEY_REUSE", "幂等键对应请求内容已改变") from exc
            return {"export": competing, "job": None, "replayed": True}
        self.db.commit()
        self.db.refresh(export)
        return {"export": export, "job": job, "replayed": False}

    def review_revision(
        self,
        *,
        workspace_id: int,
        revision_id: int,
        action: str,
        note: str | None,
        expected_version: int,
        idempotency_key: str,
        actor: str,
        request_id: str | None = None,
    ) -> dict:
        revision = self.db.scalar(
            select(ProductContentRevision).where(
                ProductContentRevision.id == revision_id,
                ProductContentRevision.workspace_id == workspace_id,
            )
        )
        if revision is None:
            raise ContentApplicationError("CONTENT_REVISION_NOT_FOUND", "内容版本不存在")
        if revision.version != expected_version:
            raise ContentApplicationError("CONTENT_VERSION_CONFLICT", "内容版本已变化，请刷新后重试")
        if action == "approve" and revision.quality_status == "blocked":
            raise ContentApplicationError("CONTENT_QUALITY_BLOCKED", "质量检查未通过，不能审核通过")
        transitions = {
            "submit": {"draft": "in_review"},
            "approve": {"in_review": "approved"},
            "request_changes": {"in_review": "changes_requested"},
            "reject": {"in_review": "rejected", "draft": "rejected"},
        }
        target = transitions.get(action, {}).get(revision.status)
        if target is None:
            raise ContentApplicationError("CONTENT_INVALID_STATE", "当前状态不允许此审核动作")
        if not idempotency_key:
            raise ContentApplicationError("IDEMPOTENCY_KEY_REQUIRED", "必须提供 Idempotency-Key")
        action_hash = hashlib.sha256(
            canonical_json({"action": action, "note": note, "expected_version": expected_version}).encode()
        ).hexdigest()
        previous_action = self.db.scalar(
            select(ProductContentAction).where(
                ProductContentAction.workspace_id == workspace_id,
                ProductContentAction.revision_id == revision.id,
                ProductContentAction.action_type == action,
                ProductContentAction.idempotency_key == idempotency_key,
            )
        )
        if previous_action is not None:
            if previous_action.payload_hash != action_hash:
                raise ContentApplicationError("IDEMPOTENCY_KEY_REUSE", "审核幂等键对应内容已改变")
            return {"revision": revision, "replayed": True}

        self.db.add(
            ProductContentAction(
                workspace_id=workspace_id,
                revision_id=revision.id,
                action_type=action,
                from_status=revision.status,
                to_status=target,
                note=note,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                payload_hash=action_hash,
                actor=actor,
                request_id=request_id,
            )
        )
        revision.status = target
        revision.version += 1
        revision.reviewed_by = actor
        revision.reviewed_at = datetime.utcnow()
        if target == "approved":
            revision.approved_at = datetime.utcnow()
        if target in {"rejected", "changes_requested"}:
            revision.rejection_reason = note
        self.db.commit()
        self.db.refresh(revision)
        return {"revision": revision, "replayed": False}
