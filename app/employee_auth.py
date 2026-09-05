from __future__ import annotations

"""员工登录、session 和角色权限辅助。"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import hmac
import secrets

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import AuthSession, UserAccount, Warehouse, WarehouseAccess, WorkspaceMembership, Workspace

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
        "replenishment.ignore", "purchasing.submit", "purchase.read",
    }),
    ROLE_WAREHOUSE: frozenset({"inbound.read", "inbound.receive", "inventory.read"}),
    ROLE_CUSTOMER_SERVICE: frozenset({"catalog.read", "inbound.read", "inventory.read", "knowledge.read"}),
    ROLE_READONLY: frozenset({"catalog.read", "inbound.read", "inventory.read", "knowledge.read"}),
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
    workspace = Workspace(
        tenant_key=settings.auth_bootstrap_tenant_key or "default",
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


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    """使用标准库 scrypt 保存密码哈希，避免明文和新增运行时依赖。"""
    import base64
    import hashlib

    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$16384$8$1$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    import base64
    import hashlib

    try:
        scheme, n, r, p, salt_text, digest_text = encoded.split("$", 5)
        if scheme != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode())
        expected = base64.urlsafe_b64decode(digest_text.encode())
        actual = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def create_session(db: Session, *, user: UserAccount, membership: WorkspaceMembership, settings: Settings, ip_address: str | None = None, user_agent: str | None = None) -> tuple[str, str, AuthSession]:
    raw_token = secrets.token_urlsafe(48)
    raw_csrf = secrets.token_urlsafe(32)
    session = AuthSession(
        token_hash=_digest(raw_token),
        csrf_token_hash=_digest(raw_csrf),
        user_id=user.id,
        membership_id=membership.id,
        workspace_id=membership.workspace_id,
        expires_at=datetime.utcnow() + timedelta(seconds=settings.session_ttl_seconds),
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return raw_token, raw_csrf, session


def load_session(db: Session, raw_token: str | None) -> tuple[AuthSession, UserAccount, WorkspaceMembership] | None:
    if not raw_token:
        return None
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == _digest(raw_token)))
    if session is None or session.revoked_at is not None or session.expires_at <= datetime.utcnow():
        return None
    user = db.get(UserAccount, session.user_id)
    membership = db.get(WorkspaceMembership, session.membership_id)
    workspace = db.get(__import__("app.db", fromlist=["Workspace"]).Workspace, session.workspace_id)
    if user is None or membership is None or workspace is None or user.status != "active" or membership.status != "active" or membership.user_id != session.user_id or membership.workspace_id != session.workspace_id or workspace.status != "active":
        return None
    session.last_seen_at = datetime.utcnow()
    db.commit()
    return session, user, membership


def principal_from_session(db: Session, raw_token: str | None) -> Principal | None:
    loaded = load_session(db, raw_token)
    if loaded is None:
        return None
    session, user, membership = loaded
    access = db.scalars(select(WarehouseAccess).join(__import__("app.db", fromlist=["Warehouse"]).Warehouse, __import__("app.db", fromlist=["Warehouse"]).Warehouse.id == WarehouseAccess.warehouse_id).where(WarehouseAccess.user_id == user.id, WarehouseAccess.status == "active", __import__("app.db", fromlist=["Warehouse"]).Warehouse.workspace_id == membership.workspace_id, __import__("app.db", fromlist=["Warehouse"]).Warehouse.is_active.is_(True))).all()
    return Principal(
        subject=user.login,
        tenant_id=str(membership.workspace_id),
        user_id=user.id,
        workspace_id=membership.workspace_id,
        roles=(membership.role,),
        warehouse_ids=tuple(item.warehouse_id for item in access),
        auth_type="session",
        session_id=session.id,
    )


def _as_employee_principal(principal) -> Principal:
    if isinstance(principal, Principal):
        return principal
    return Principal(
        subject=principal.subject,
        tenant_id=principal.tenant_id,
        key_id=principal.key_id,
        scopes=principal.scopes,
        workspace_id=getattr(principal, "workspace_id", None),
        auth_type="api_key" if principal.key_id else "demo",
    )


def require_principal(request: Request, *, permission: str | None = None, roles: tuple[str, ...] = ()) -> Principal:
    from app.config import get_settings

    principal = _as_employee_principal(getattr(request.state, "principal", None)) if getattr(request.state, "principal", None) is not None else None
    employee_auth_enabled = get_settings().employee_auth_enabled
    if principal is None or (
        employee_auth_enabled and principal.auth_type in ("demo", "api_key")
    ):
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "请先登录"})
    if principal.workspace_id is None and principal.auth_type != "demo":
        raise HTTPException(status_code=401, detail={"code": "WORKSPACE_CONTEXT_REQUIRED", "message": "工作空间上下文缺失"})
    if principal.auth_type == "demo":
        if get_settings().demo_mode_enabled and not employee_auth_enabled:
            if roles:
                raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "演示模式不可访问员工管理功能"})
            return principal
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "请先登录"})
    if principal.auth_type == "api_key" and not employee_auth_enabled:
        if permission and not principal.can(permission):
            raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "API key scope 不足"})
        if roles:
            raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "API key 不能执行员工角色管理"})
        return principal
    if permission and not principal.can(permission):
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "没有执行此操作的权限"})
    if roles and not principal.has_role(*roles):
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "当前角色无权执行此操作"})
    return principal


def validate_csrf(request: Request, db: Session) -> None:
    """校验浏览器 session 的双提交 CSRF token。"""
    principal = getattr(request.state, "principal", None)
    if principal is None or not getattr(principal, "is_employee", False):
        return
    token = request.headers.get("X-CSRF-Token")
    cookie_token = request.cookies.get("dianshang_csrf")
    if not token or not cookie_token or not hmac.compare_digest(token, cookie_token):
        raise HTTPException(status_code=403, detail={"code": "CSRF_INVALID", "message": "CSRF 校验失败"})
    session = db.get(AuthSession, principal.session_id) if principal.session_id else None
    if session is None or not hmac.compare_digest(_digest(token), session.csrf_token_hash):
        raise HTTPException(status_code=403, detail={"code": "CSRF_INVALID", "message": "CSRF 校验失败"})


def require_warehouse_access(
    principal: Principal,
    warehouse_id: int,
    db: Session,
) -> None:
    """校验仓库属于当前工作空间且在当前账户授权范围内。

    管理员和运营可以访问本工作空间的全部有效仓库；其他员工必须拥有
    明确的 active 授权。空授权集合始终拒绝，避免把“没有范围”误当成
    “全部范围”。API key / demo 仅在员工认证关闭时由旧演示模式使用。
    """
    if principal.auth_type in ("demo", "api_key"):
        return
    if principal.workspace_id is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "WAREHOUSE_NOT_FOUND", "message": "仓库不存在"},
        )
    if db is not None:
        warehouse = db.get(Warehouse, warehouse_id)
        if (
            warehouse is None
            or not warehouse.is_active
            or warehouse.workspace_id != principal.workspace_id
        ):
            raise HTTPException(
                status_code=404,
                detail={"code": "WAREHOUSE_NOT_FOUND", "message": "仓库不存在"},
            )
    if principal.has_role(ROLE_ADMIN, ROLE_OPERATIONS):
        return
    if warehouse_id in principal.warehouse_ids:
        return
    raise HTTPException(
        status_code=404,
        detail={"code": "WAREHOUSE_NOT_FOUND", "message": "仓库不存在"},
    )
