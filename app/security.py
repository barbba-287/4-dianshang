"""HTTP API key authentication and request principals."""

from dataclasses import dataclass
import hmac

from app.config import Settings


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    key_id: str | None = None
    scopes: tuple[str, ...] = ()
    workspace_id: int | None = None
    auth_type: str = "api_key"


def _resolve_workspace(db, settings: Settings, *, tenant_key: str | None = None) -> int | None:
    if db is None:
        return None
    from sqlalchemy import select
    from app.db import Workspace

    workspaces = db.scalars(
        select(Workspace).where(Workspace.status == "active").order_by(Workspace.id)
    ).all()
    if len(workspaces) == 1:
        from app.db import backfill_legacy_workspace
        backfill_legacy_workspace(db, workspaces[0].id)
        return workspaces[0].id
    if len(workspaces) > 1:
        configured = (tenant_key or settings.api_tenant_id or "").strip()
        matches = [item for item in workspaces if item.tenant_key == configured]
        return matches[0].id if len(matches) == 1 else None
    configured = (tenant_key or settings.auth_bootstrap_tenant_key or "default").strip()
    workspace = db.scalar(select(Workspace).where(Workspace.tenant_key == configured))
    if workspace is None:
        workspace = Workspace(
            tenant_key=configured,
            name=settings.auth_bootstrap_workspace_name or configured,
        )
        db.add(workspace)
        db.commit()
        db.refresh(workspace)
    from app.db import backfill_legacy_workspace
    backfill_legacy_workspace(db, workspace.id)
    return workspace.id


def demo_principal(settings: Settings, *, workspace_id: int | None = None) -> Principal:
    return Principal(
        subject="anonymous",
        tenant_id=settings.api_tenant_id or "default",
        workspace_id=workspace_id,
        auth_type="demo",
    )


def validate_auth_config(settings: Settings) -> None:
    if not settings.api_auth_enabled:
        return
    if len(settings.api_key) < 32:
        raise ValueError("API_AUTH_ENABLED=true 时必须配置至少 32 位 API_KEY")
    if not settings.api_tenant_id.strip():
        raise ValueError("API_AUTH_ENABLED=true 时必须配置 API_TENANT_ID")


def authenticate_api_key(value: str | None, settings: Settings, db=None) -> Principal | None:
    validate_auth_config(settings)
    if not settings.api_auth_enabled:
        workspace_id = _resolve_workspace(db, settings) if db is not None else None
        return demo_principal(settings, workspace_id=workspace_id)
    if not value or not hmac.compare_digest(value, settings.api_key):
        return None
    workspace_id = _resolve_workspace(db, settings) if db is not None else None
    if workspace_id is None:
        return None
    return Principal(
        subject=settings.api_user_id or "api-user",
        tenant_id=settings.api_tenant_id,
        key_id="static-api-key",
        scopes=("*",),
        workspace_id=workspace_id,
        auth_type="api_key",
    )
