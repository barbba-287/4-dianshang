from __future__ import annotations

"""唯一身份模型：Principal、角色权限表、API/demo 认证与工作空间解析。

本模块是领域层，不依赖任何 HTTP 框架（不 import fastapi），也不抛业务错误
（不 import DomainError）。员工密码、会话与 HTTP 适配层留在 app/employee_auth.py。
"""

from dataclasses import dataclass
import hmac

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Workspace

ROLE_ADMIN = "admin"
ROLE_OPERATIONS = "operations"
ROLE_WAREHOUSE = "warehouse"
ROLE_CUSTOMER_SERVICE = "customer_service"
ROLE_READONLY = "readonly"

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_ADMIN: frozenset(),
    ROLE_OPERATIONS: frozenset({
        "catalog.read", "catalog.write", "inbound.create", "inbound.read", "inbound.confirm",
        "inventory.read", "knowledge.read", "knowledge.write", "inventory.write",
        "replenishment.read", "replenishment.write", "replenishment.confirm",
        "replenishment.ignore", "purchasing.submit", "purchase.read", "analytics.read",
        "content.read", "content.write", "content.review", "content.export",
    }),
    ROLE_WAREHOUSE: frozenset({"inbound.read", "inbound.receive", "inventory.read"}),
    ROLE_CUSTOMER_SERVICE: frozenset({"catalog.read", "inbound.read", "inventory.read", "knowledge.read", "knowledge.write", "analytics.read", "content.read"}),
    ROLE_READONLY: frozenset({"catalog.read", "inbound.read", "inventory.read", "knowledge.read", "analytics.read", "content.read"}),
}

ROLE_LABELS = {
    ROLE_ADMIN: "管理员",
    ROLE_OPERATIONS: "运营",
    ROLE_WAREHOUSE: "仓库协作方",
    ROLE_CUSTOMER_SERVICE: "客服",
    ROLE_READONLY: "只读成员",
}


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    key_id: str | None = None
    scopes: tuple[str, ...] = ()
    user_id: int | None = None
    workspace_id: int | None = None
    roles: tuple[str, ...] = ()
    warehouse_ids: tuple[int, ...] = ()
    auth_type: str = "api_key"
    session_id: int | None = None

    @property
    def is_employee(self) -> bool:
        return self.auth_type == "session"

    def has_role(self, *required: str) -> bool:
        return bool(set(required) & set(self.roles))

    def can(self, permission: str) -> bool:
        if self.auth_type == "demo":
            return False
        if self.auth_type == "api_key":
            return permission in self.scopes or "*" in self.scopes
        if self.has_role(ROLE_ADMIN):
            return True
        role_permissions = ROLE_PERMISSIONS
        return any(permission in role_permissions.get(role, frozenset()) for role in self.roles)


def demo_principal(settings: Settings, *, workspace_id: int | None = None) -> Principal:
    return Principal(
        subject="anonymous",
        tenant_id=settings.api_tenant_id or "default",
        workspace_id=workspace_id,
        auth_type="demo",
    )


def _resolve_compat_workspace(db: Session, settings: Settings) -> int | None:
    """Resolve demo scope without ever treating NULL as a wildcard."""
    workspaces = db.scalars(
        select(Workspace).where(Workspace.status == "active").order_by(Workspace.id)
    ).all()
    if len(workspaces) == 1:
        from app.db import backfill_legacy_workspace
        backfill_legacy_workspace(db, workspaces[0].id)
        return workspaces[0].id
    if len(workspaces) > 1:
        configured = (settings.auth_bootstrap_tenant_key or "").strip()
        matches = [item for item in workspaces if item.tenant_key == configured]
        return matches[0].id if len(matches) == 1 else None
    configured = (settings.auth_bootstrap_tenant_key or "default").strip()
    existing = db.scalar(select(Workspace).where(Workspace.tenant_key == configured))
    if existing is not None:
        # 停用空间不能被兼容模式偷偷复活，也不能因重名再次创建。
        return None
    workspace = Workspace(
        tenant_key=configured,
        name=settings.auth_bootstrap_workspace_name or "默认商家",
    )
    db.add(workspace)
    db.commit()
    db.refresh(workspace)
    from app.db import backfill_legacy_workspace
    backfill_legacy_workspace(db, workspace.id)
    return workspace.id


def _resolve_api_workspace(db: Session, settings: Settings) -> int | None:
    workspace = db.scalar(
        select(Workspace).where(
            Workspace.tenant_key == settings.api_tenant_id,
            Workspace.status == "active",
        )
    )
    return workspace.id if workspace is not None else None


def validate_auth_config(settings: Settings) -> None:
    if settings.api_auth_enabled:
        if len(settings.api_key) < 32:
            raise ValueError("API_AUTH_ENABLED=true 时必须配置至少 32 位 API_KEY")
        if not settings.api_tenant_id.strip():
            raise ValueError("API_AUTH_ENABLED=true 时必须配置 API_TENANT_ID")
    if settings.employee_auth_enabled and settings.session_ttl_seconds <= 0:
        raise ValueError("SESSION_TTL_SECONDS 必须为正数")


def authenticate_api_key(value: str | None, settings: Settings, db: Session | None = None) -> Principal | None:
    validate_auth_config(settings)
    if not settings.api_auth_enabled:
        workspace_id = None
        if db is not None:
            workspace_id = _resolve_compat_workspace(db, settings)
        return demo_principal(settings, workspace_id=workspace_id)
    if not value or not hmac.compare_digest(value, settings.api_key):
        return None
    workspace_id = None
    if db is not None:
        workspace_id = _resolve_api_workspace(db, settings)
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
