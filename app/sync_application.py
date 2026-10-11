"""外部事实同步应用服务。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SyncApplicationError(Exception):
    """同步应用边界的稳定错误；HTTP 状态码由接口层决定。"""

    code: str
    message: str

    def __str__(self) -> str:
        return self.code


class SyncApplicationService:
    """编排同步，但不拥有认证、权限或 HTTP 语义。"""

    def __init__(self, db):
        self.db = db

    def run_fixture_bundle(
        self,
        *,
        workspace_id: int,
        platform: str,
        account_ref: str,
        store_ref: str = "default",
        orders_content: str | bytes | None = None,
        inventory_content: str | bytes | None = None,
        source_mode: str = "mock",
    ) -> dict:
        if not workspace_id:
            raise SyncApplicationError("WORKSPACE_CONTEXT_REQUIRED", "缺少 Workspace 上下文")
        try:
            from app.platform_sync import run_fixture_sync

            return run_fixture_sync(
                self.db,
                workspace_id=workspace_id,
                platform=platform,
                account_ref=account_ref,
                store_ref=store_ref,
                orders_content=orders_content,
                inventory_content=inventory_content,
                source_mode=source_mode,
            )
        except ValueError as exc:
            code = str(exc) or "EXTERNAL_INPUT_INVALID"
            raise SyncApplicationError(code, "离线同步输入无效") from exc
        except SyncApplicationError:
            raise
        except Exception as exc:
            code = str(getattr(exc, "code", "") or type(exc).__name__)
            raise SyncApplicationError(code, "离线同步失败") from exc

    def retry_fixture_bundle(
        self,
        *,
        workspace_id: int,
        run_id: str,
        resources: list[str] | None = None,
        orders_content: str | bytes | None = None,
        inventory_content: str | bytes | None = None,
        idempotency_key: str,
    ) -> dict:
        """Retry failed fixture resources while preserving the parent run scope."""
        import hashlib
        import json
        import sqlalchemy.exc
        from sqlalchemy import select

        from app.db import ExternalSyncRun
        from app.external_sync import resource_status, retryable_resources
        from app.platform_sync import run_fixture_sync

        parent = self.db.scalar(
            select(ExternalSyncRun).where(
                ExternalSyncRun.run_id == run_id,
                ExternalSyncRun.workspace_id == workspace_id,
            )
        )
        if parent is None:
            raise SyncApplicationError("SYNC_RUN_NOT_FOUND", "同步运行不存在")
        if parent.status not in {"failed", "partial"} or parent.sync_type not in {"fixture_bundle", "orders", "inventory"}:
            raise SyncApplicationError("SYNC_RUN_NOT_RETRYABLE", "该同步运行不支持补偿")

        available = set(retryable_resources(parent))
        selected = set(resources or available)
        if not selected or not selected.issubset({"orders", "inventory"}) or not selected.issubset(available):
            raise SyncApplicationError("SYNC_RESOURCE_NOT_RETRYABLE", "选定资源不可补偿")
        if ("orders" in selected and not orders_content) or ("inventory" in selected and not inventory_content):
            raise SyncApplicationError("FIXTURE_CONTENT_REQUIRED", "补偿资源必须提供 fixture")

        content_hash = hashlib.sha256(
            json.dumps(
                {
                    "run_id": run_id,
                    "resources": sorted(selected),
                    "orders_content": orders_content if "orders" in selected else None,
                    "inventory_content": inventory_content if "inventory" in selected else None,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        existing = self.db.scalar(
            select(ExternalSyncRun).where(
                ExternalSyncRun.workspace_id == workspace_id,
                ExternalSyncRun.retry_of_run_id == run_id,
                ExternalSyncRun.retry_idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if existing.retry_payload_hash != content_hash:
                raise SyncApplicationError("IDEMPOTENCY_KEY_REUSE", "补偿幂等键对应内容已改变")
            return {"run": existing, "replayed": True}

        carried = resource_status(parent)
        try:
            result = run_fixture_sync(
                self.db,
                workspace_id=workspace_id,
                platform=parent.platform,
                account_ref=parent.account_ref or "",
                store_ref=parent.store_ref or "default",
                orders_content=orders_content if "orders" in selected else None,
                inventory_content=inventory_content if "inventory" in selected else None,
                source_mode=parent.source_mode,
                attempt=(parent.attempt or 1) + 1,
                retry_of_run_id=parent.run_id,
                retry_idempotency_key=idempotency_key,
                retry_payload_hash=content_hash,
                carried_forward={name: item for name, item in carried.items() if name not in selected},
                sync_type_override=parent.sync_type,
            )
        except sqlalchemy.exc.IntegrityError as exc:
            self.db.rollback()
            competing = self.db.scalar(
                select(ExternalSyncRun).where(
                    ExternalSyncRun.workspace_id == workspace_id,
                    ExternalSyncRun.retry_of_run_id == run_id,
                    ExternalSyncRun.retry_idempotency_key == idempotency_key,
                )
            )
            if competing is None:
                raise SyncApplicationError("SYNC_RETRY_PERSIST_FAILED", "同步补偿无法持久化") from exc
            if competing.retry_payload_hash != content_hash:
                raise SyncApplicationError("IDEMPOTENCY_KEY_REUSE", "补偿幂等键对应内容已改变") from exc
            return {"run": competing, "replayed": True}
        except ValueError as exc:
            raise SyncApplicationError(str(exc) or "EXTERNAL_INPUT_INVALID", "同步补偿失败") from exc

        child = result["run"]
        self.db.commit()
        self.db.refresh(child)
        return {"run": child, "replayed": False}

    def ingest_inventory(
        self,
        *,
        workspace_id: int,
        platform: str,
        source_mode: str,
        content: str | bytes,
    ) -> dict:
        if not workspace_id:
            raise SyncApplicationError("WORKSPACE_CONTEXT_REQUIRED", "缺少 Workspace 上下文")
        run = None
        try:
            from app.connectors import load_records
            from app.external_sync import (
                begin_sync_run,
                complete_sync_run,
                ingest_inventory,
                validate_inventory_batch_scope,
            )

            records = load_records(content, platform=platform, source_mode=source_mode)
            account_ref, store_ref, _warehouse_ref = validate_inventory_batch_scope(records)
            run = begin_sync_run(
                self.db,
                workspace_id=workspace_id,
                platform=platform,
                sync_type="inventory",
                account_ref=account_ref,
                store_ref=store_ref,
                source_mode=source_mode,
            )
            stats = ingest_inventory(
                self.db,
                records,
                workspace_id=workspace_id,
                sync_run_id=run.run_id,
            )
            complete_sync_run(self.db, run, stats)
            return {
                "platform": platform,
                "source_mode": source_mode,
                "simulated": True,
                "live_enabled": False,
                "stats": stats,
                "sync_run_id": stats.sync_run_id or run.run_id,
                "sync_status": stats.sync_status or run.status,
            }
        except ValueError as exc:
            if run is not None:
                from app.external_sync import fail_sync_run

                fail_sync_run(self.db, run, exc)
            code = str(exc) or "EXTERNAL_INPUT_INVALID"
            raise SyncApplicationError(code, "外部库存同步输入无效") from exc
        except SyncApplicationError:
            raise
        except Exception as exc:
            if run is not None:
                from app.external_sync import fail_sync_run

                fail_sync_run(self.db, run, exc)
            raise SyncApplicationError("EXTERNAL_SYNC_FAILED", "外部库存同步失败") from exc


def _stats_payload(stats):
    if stats is None:
        return None
    return {
        "total": stats.total,
        "inserted": stats.inserted,
        "updated": getattr(stats, "updated", 0),
        "no_op": stats.no_op,
        "conflict": stats.conflict,
        "stale": getattr(stats, "stale", 0),
    }


def fixture_response_data(result: dict) -> dict:
    """将同步领域结果映射为既有 FixtureSyncResponse 字段。"""
    run = result["run"]
    inventory = result.get("inventory")
    inventory_data = _stats_payload(inventory)
    if inventory_data is not None:
        inventory_data["snapshot_ids"] = inventory.snapshot_ids or []
    return {
        "sync_run_id": run.run_id,
        "sync_status": run.status,
        "orders": _stats_payload(result.get("orders")),
        "inventory": inventory_data,
    }


def inventory_response_data(result: dict) -> dict:
    """将 direct inventory 领域结果映射为既有 API 字段。"""
    stats = result["stats"]
    return {
        "platform": result["platform"],
        "source_mode": result["source_mode"],
        "simulated": result["simulated"],
        "live_enabled": result["live_enabled"],
        "inserted": stats.inserted,
        "no_op": stats.no_op,
        "conflict": stats.conflict,
        "total": stats.total,
        "snapshot_ids": stats.snapshot_ids or [],
        "sync_run_id": result["sync_run_id"],
        "sync_status": result["sync_status"],
    }
