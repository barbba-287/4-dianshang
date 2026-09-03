"""账户和仓库授权管理服务。"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import AuthSession, UserAccount, Warehouse, WarehouseAccess, Workspace, WorkspaceMembership
from app.employee_auth import hash_password

ROLES = {"operations", "warehouse", "customer_service", "readonly"}


def _membership(db: Session, *, workspace_id: int, user_id: int) -> WorkspaceMembership | None:
    return db.scalar(
        select(WorkspaceMembership)
        .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
        .where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
            Workspace.status == "active",
        )
    )


def _workspace_warehouse_ids(db: Session, *, workspace_id: int, user_id: int) -> list[int]:
    return list(
        db.scalars(
            select(WarehouseAccess.warehouse_id)
            .join(Warehouse, Warehouse.id == WarehouseAccess.warehouse_id)
            .where(
                WarehouseAccess.user_id == user_id,
                WarehouseAccess.status == "active",
                Warehouse.workspace_id == workspace_id,
                Warehouse.is_active.is_(True),
            )
            .order_by(WarehouseAccess.warehouse_id)
        ).all()
    )


def _user_response(db: Session, *, workspace_id: int, membership: WorkspaceMembership) -> dict:
    user = db.get(UserAccount, membership.user_id)
    if user is None:
        raise ValueError("USER_NOT_FOUND")
    return {
        "id": user.id,
        "login": user.login,
        "display_name": user.display_name,
        "status": user.status if membership.status == "active" else "inactive",
        "role": membership.role,
        "warehouse_ids": _workspace_warehouse_ids(db, workspace_id=workspace_id, user_id=user.id),
    }


def list_workspace_users(db: Session, *, workspace_id: int) -> list[dict]:
    memberships = db.scalars(
        select(WorkspaceMembership)
        .where(WorkspaceMembership.workspace_id == workspace_id)
        .order_by(WorkspaceMembership.id)
    ).all()
    result = []
    for membership in memberships:
        user = db.get(UserAccount, membership.user_id)
        if user is not None:
            result.append(_user_response(db, workspace_id=workspace_id, membership=membership))
    return result


def create_workspace_user(
    db: Session,
    *,
    workspace_id: int,
    login: str,
    display_name: str,
    password: str,
    role: str,
    warehouse_ids: list[int],
) -> dict:
    login = login.strip()
    display_name = display_name.strip()
    warehouse_ids = sorted(set(warehouse_ids))
    if not login or len(login) > 128 or not display_name or len(password) < 8:
        raise ValueError("INVALID_USER_INPUT")
    if role not in ROLES:
        raise ValueError("INVALID_ROLE")
    if role == "warehouse" and not warehouse_ids:
        raise ValueError("WAREHOUSE_SCOPE_REQUIRED")
    workspace = db.get(Workspace, workspace_id)
    if workspace is None or workspace.status != "active":
        raise ValueError("WORKSPACE_NOT_FOUND")
    if db.scalar(select(UserAccount).where(UserAccount.login == login)) is not None:
        raise ValueError("USER_LOGIN_EXISTS")
    warehouses = (
        db.scalars(
            select(Warehouse).where(
                Warehouse.id.in_(warehouse_ids),
                Warehouse.workspace_id == workspace_id,
                Warehouse.is_active.is_(True),
            )
        ).all()
        if warehouse_ids
        else []
    )
    if len(warehouses) != len(warehouse_ids):
        raise ValueError("WAREHOUSE_SCOPE_INVALID")
    user = UserAccount(login=login, display_name=display_name, password_hash=hash_password(password))
    db.add(user)
    db.flush()
    membership = WorkspaceMembership(workspace_id=workspace_id, user_id=user.id, role=role)
    db.add(membership)
    for warehouse_id in warehouse_ids:
        db.add(WarehouseAccess(user_id=user.id, warehouse_id=warehouse_id, status="active"))
    db.commit()
    return _user_response(db, workspace_id=workspace_id, membership=membership)


def set_warehouse_access(
    db: Session, *, workspace_id: int, user_id: int, warehouse_id: int, active: bool
) -> None:
    membership = _membership(db, workspace_id=workspace_id, user_id=user_id)
    warehouse = db.scalar(
        select(Warehouse).where(
            Warehouse.id == warehouse_id,
            Warehouse.workspace_id == workspace_id,
            Warehouse.is_active.is_(True),
        )
    )
    if membership is None or membership.status != "active" or warehouse is None:
        raise ValueError("WAREHOUSE_SCOPE_INVALID")
    access = db.scalar(
        select(WarehouseAccess).where(
            WarehouseAccess.user_id == user_id,
            WarehouseAccess.warehouse_id == warehouse_id,
        )
    )
    if access is None:
        if not active:
            return
        db.add(WarehouseAccess(user_id=user_id, warehouse_id=warehouse_id, status="active"))
    else:
        access.status = "active" if active else "revoked"
    db.commit()


def update_workspace_user(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    display_name: str | None = None,
    role: str | None = None,
) -> dict:
    membership = _membership(db, workspace_id=workspace_id, user_id=user_id)
    user = db.get(UserAccount, user_id)
    if membership is None or user is None:
        raise ValueError("USER_NOT_FOUND")
    if display_name is not None:
        display_name = display_name.strip()
        if not display_name:
            raise ValueError("INVALID_USER_INPUT")
        user.display_name = display_name
    if role is not None:
        if role not in ROLES:
            raise ValueError("INVALID_ROLE")
        if membership.role == "admin" and role != "admin":
            other_admin = db.scalar(
                select(WorkspaceMembership.id).where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.role == "admin",
                    WorkspaceMembership.status == "active",
                    WorkspaceMembership.user_id != user_id,
                )
            )
            if other_admin is None:
                raise ValueError("LAST_ADMIN_FORBIDDEN")
        if role == "warehouse" and not _workspace_warehouse_ids(db, workspace_id=workspace_id, user_id=user_id):
            raise ValueError("WAREHOUSE_SCOPE_REQUIRED")
        membership.role = role
    db.commit()
    return _user_response(db, workspace_id=workspace_id, membership=membership)


def reset_user_password(db: Session, *, workspace_id: int, user_id: int, password: str) -> None:
    if len(password) < 8:
        raise ValueError("PASSWORD_TOO_SHORT")
    membership = _membership(db, workspace_id=workspace_id, user_id=user_id)
    user = db.get(UserAccount, user_id)
    if membership is None or user is None:
        raise ValueError("USER_NOT_FOUND")
    user.password_hash = hash_password(password)
    now = datetime.utcnow()
    for session in db.scalars(
        select(AuthSession).where(
            AuthSession.user_id == user_id,
            AuthSession.workspace_id == workspace_id,
            AuthSession.revoked_at.is_(None),
        )
    ).all():
        session.revoked_at = now
    db.commit()


def set_user_status(db: Session, *, workspace_id: int, user_id: int, active: bool) -> None:
    membership = _membership(db, workspace_id=workspace_id, user_id=user_id)
    user = db.get(UserAccount, user_id)
    if membership is None or user is None:
        raise ValueError("USER_NOT_FOUND")
    if not active and membership.role == "admin":
        admin_count = db.scalar(
            select(WorkspaceMembership.id).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.role == "admin",
                WorkspaceMembership.status == "active",
                WorkspaceMembership.user_id != user_id,
            )
        )
        if admin_count is None:
            raise ValueError("LAST_ADMIN_FORBIDDEN")
    membership.status = "active" if active else "inactive"
    now = datetime.utcnow()
    if active:
        user.status = "active"
    else:
        for session in db.scalars(
            select(AuthSession).where(
                AuthSession.user_id == user_id,
                AuthSession.workspace_id == workspace_id,
                AuthSession.revoked_at.is_(None),
            )
        ).all():
            session.revoked_at = now
        has_active_membership = db.scalar(
            select(WorkspaceMembership.id).where(
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.status == "active",
                WorkspaceMembership.id != membership.id,
            )
        )
        if has_active_membership is None:
            user.status = "inactive"
    db.commit()


def bind_legacy_warehouse(db: Session, *, workspace_id: int, warehouse_id: int) -> Warehouse:
    workspace = db.get(Workspace, workspace_id)
    if workspace is None or workspace.status != "active":
        raise ValueError("WORKSPACE_NOT_FOUND")
    warehouse = db.get(Warehouse, warehouse_id)
    if warehouse is None or not warehouse.is_active:
        raise ValueError("WAREHOUSE_NOT_FOUND")
    if warehouse.workspace_id is not None:
        if warehouse.workspace_id == workspace_id:
            return warehouse
        raise ValueError("WAREHOUSE_ALREADY_BOUND")
    warehouse.workspace_id = workspace_id
    db.commit()
    db.refresh(warehouse)
    return warehouse


def list_workspace_warehouses(
    db: Session, *, workspace_id: int, include_unbound: bool = False
) -> list[Warehouse]:
    conditions = [Warehouse.is_active.is_(True)]
    if include_unbound:
        from sqlalchemy import or_
        conditions.append(or_(Warehouse.workspace_id == workspace_id, Warehouse.workspace_id.is_(None)))
    else:
        conditions.append(Warehouse.workspace_id == workspace_id)
    return db.scalars(select(Warehouse).where(*conditions).order_by(Warehouse.id)).all()
