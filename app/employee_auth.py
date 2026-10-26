from __future__ import annotations

"""员工登录、session 和角色权限辅助。

身份模型本体已收敛到 `app/identity.py`（唯一 Principal / 角色权限表 / API 与
demo 认证），本模块保留 HTTP 适配层：密码哈希、session 三件套、CSRF 校验、
仓库授权依赖，并按原路径 re-export 身份相关名字，保证既有导入方
（account_admin / cli / demo_seed / main / tests）不受影响。
"""

from datetime import datetime, timedelta
from hashlib import sha256
import hmac
import secrets

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import AuthSession, UserAccount, Warehouse, WarehouseAccess, WorkspaceMembership, Workspace
from app.errors import DomainError
from app.identity import (
    ROLE_ADMIN,
    ROLE_CUSTOMER_SERVICE,
    ROLE_LABELS,
    ROLE_OPERATIONS,
    ROLE_PERMISSIONS,
    ROLE_READONLY,
    ROLE_WAREHOUSE,
    Principal,
    _resolve_api_workspace,
    _resolve_compat_workspace,
    authenticate_api_key,
    demo_principal,
    validate_auth_config,
)

__all__ = [
    "ROLE_ADMIN",
    "ROLE_CUSTOMER_SERVICE",
    "ROLE_LABELS",
    "ROLE_OPERATIONS",
    "ROLE_PERMISSIONS",
    "ROLE_READONLY",
    "ROLE_WAREHOUSE",
    "Principal",
    "authenticate_api_key",
    "create_session",
    "demo_principal",
    "hash_password",
    "load_session",
    "principal_from_session",
    "require_principal",
    "require_warehouse_access",
    "validate_auth_config",
    "validate_csrf",
    "verify_password",
]


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


def require_principal(request: Request, *, permission: str | None = None, roles: tuple[str, ...] = ()) -> Principal:
    from app.config import get_settings

    principal = getattr(request.state, "principal", None)
    employee_auth_enabled = get_settings().employee_auth_enabled
    if principal is None or (
        employee_auth_enabled and principal.auth_type in ("demo", "api_key")
    ):
        raise DomainError(code="UNAUTHORIZED", message="请先登录", http_status=401)
    if principal.workspace_id is None and principal.auth_type != "demo":
        raise DomainError(code="WORKSPACE_CONTEXT_REQUIRED", message="工作空间上下文缺失", http_status=401)
    if principal.auth_type == "demo":
        if get_settings().demo_mode_enabled and not employee_auth_enabled:
            if roles:
                raise DomainError(code="FORBIDDEN", message="演示模式不可访问员工管理功能", http_status=403)
            return principal
        raise DomainError(code="UNAUTHORIZED", message="请先登录", http_status=401)
    if principal.auth_type == "api_key" and not employee_auth_enabled:
        if permission and not principal.can(permission):
            raise DomainError(code="FORBIDDEN", message="API key scope 不足", http_status=403)
        if roles:
            raise DomainError(code="FORBIDDEN", message="API key 不能执行员工角色管理", http_status=403)
        return principal
    if permission and not principal.can(permission):
        raise DomainError(code="FORBIDDEN", message="没有执行此操作的权限", http_status=403)
    if roles and not principal.has_role(*roles):
        raise DomainError(code="FORBIDDEN", message="当前角色无权执行此操作", http_status=403)
    return principal


def validate_csrf(request: Request, db: Session) -> None:
    """校验浏览器 session 的双提交 CSRF token。"""
    principal = getattr(request.state, "principal", None)
    if principal is None or not getattr(principal, "is_employee", False):
        return
    token = request.headers.get("X-CSRF-Token")
    cookie_token = request.cookies.get("dianshang_csrf")
    if not token or not cookie_token or not hmac.compare_digest(token, cookie_token):
        raise DomainError(code="CSRF_INVALID", message="CSRF 校验失败", http_status=403)
    session = db.get(AuthSession, principal.session_id) if principal.session_id else None
    if session is None or not hmac.compare_digest(_digest(token), session.csrf_token_hash):
        raise DomainError(code="CSRF_INVALID", message="CSRF 校验失败", http_status=403)


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
        raise DomainError(
            code="WAREHOUSE_NOT_FOUND",
            message="仓库不存在",
            http_status=404,
        )
    if db is not None:
        warehouse = db.get(Warehouse, warehouse_id)
        if (
            warehouse is None
            or not warehouse.is_active
            or warehouse.workspace_id != principal.workspace_id
        ):
            raise DomainError(
                code="WAREHOUSE_NOT_FOUND",
                message="仓库不存在",
                http_status=404,
            )
    if principal.has_role(ROLE_ADMIN, ROLE_OPERATIONS):
        return
    if warehouse_id in principal.warehouse_ids:
        return
    raise DomainError(
        code="WAREHOUSE_NOT_FOUND",
        message="仓库不存在",
        http_status=404,
    )
