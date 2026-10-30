"""P3-S1 采购单审批领域服务：approve / reject / cancel（幂等 + 单条 CAS 乐观锁）。

设计红线（来自 dev_task.p3s1-purchase-approval.md）：
- **纯领域模块**：不 import fastapi，不抛 HTTP 异常；错误一律用 `ValueError(错误码)` 表达，
  由 `app/main.py` 的 `_replenishment_error` 映射成 HTTP 状态。
- **单条 CAS UPDATE 是唯一正确性来源**：禁止"先 SELECT 校验版本、再 ORM 赋值 commit"的
  既有窗口（`app/replenishment.py` 的老范式），本模块一律用 core `update()` 的原子条件更新
  + `rowcount` 顺序判定。
- **幂等命中检查先于一切状态闸**：同键同哈希 → 直接返回当前单（零写入）；
  同键不同哈希 → 409 `IDEMPOTENCY_KEY_REUSE`。
- **禁止自审（U3）**：审批人不得是制单人/提交人，且**无任何豁免分支、无任何开关**。
- **零物理删除**：撤回只改状态位与留痕列。
- 撤回的连带：把该单明细对应的补货建议回退到 `modified`（否则这批建议永久 `SUGGESTION_NOT_READY`）。
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import (
    InboundOrder,
    PurchaseRequest,
    PurchaseRequestAction,
    PurchaseRequestLine,
    ReplenishmentSuggestion,
)

# 状态机常量（写死，测试锁死）。
APPROVABLE = {"submitted"}
REJECTABLE = {"submitted"}
CANCELLABLE = {"draft", "submitted", "approved"}

# 终态：对任何决策动作一律 409 INVALID_PURCHASE_REQUEST_STATE。
TERMINAL_STATES = {"rejected", "cancelled"}

# 审批结论目标状态。
_TO_STATUS = {"approve": "approved", "reject": "rejected", "cancel": "cancelled"}


def _canonical_hash(value: dict) -> str:
    """与 `app/replenishment.py` 同口径的规范化哈希（sha256: + sorted-key JSON）。"""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _require_idempotency_key(idempotency_key: str | None) -> str:
    """幂等键校验：三者同构，缺失或超长一律 422 IDEMPOTENCY_KEY_REQUIRED。"""
    if not idempotency_key or len(idempotency_key) > 128:
        raise ValueError("IDEMPOTENCY_KEY_REQUIRED")
    return idempotency_key


def _decision_payload_hash(
    action: str,
    *,
    request_id: int,
    expected_version: int,
    reason: str | None,
    note: str | None,
) -> str:
    """决策动作的载荷哈希：approve/reject/cancel 三者同构。

    approve 用 `note`，reject/cancel 用 `reason`；未使用的那个键恒为 None，
    保证同一动作的哈希口径稳定。
    """
    return _canonical_hash({
        "action": action,
        "request_id": request_id,
        "expected_version": expected_version,
        "reason": reason,
        "note": note,
    })


def _load_request(db: Session, *, workspace_id: int, request_id: int) -> PurchaseRequest:
    request = db.scalar(select(PurchaseRequest).where(
        PurchaseRequest.id == request_id,
        PurchaseRequest.workspace_id == workspace_id,
    ))
    if request is None:
        raise ValueError("PURCHASE_REQUEST_NOT_FOUND")
    return request


def _check_self_approval(request: PurchaseRequest, actor: str) -> None:
    """U3 自审红线：审批人不得是制单人/提交人，**无豁免分支**。

    身份基准是 `principal.subject`（employee 会话下等于 `user_accounts.login`，
    而 login 有唯一约束，字符串相等即同一 User）。制单/提交两列都查：
    draft 路径写 `created_by`，直接提交路径只写 `submitted_by`。
    """
    if request.created_by is not None and request.created_by == actor:
        raise ValueError("SELF_APPROVAL_FORBIDDEN")
    if request.submitted_by is not None and request.submitted_by == actor:
        raise ValueError("SELF_APPROVAL_FORBIDDEN")


def _check_owner(request: PurchaseRequest, actor: str, *, is_admin: bool) -> None:
    """撤回归属校验：制单人/提交人本人才能撤回；管理员豁免（不是"另开口子"，
    而是 admin 本就具备 `purchasing.submit` 权限）。"""
    if is_admin:
        return
    if request.created_by is not None and request.created_by == actor:
        return
    if request.submitted_by is not None and request.submitted_by == actor:
        return
    raise ValueError("PURCHASE_REQUEST_NOT_OWNER")


def _resolve_cas_failure(
    db: Session,
    *,
    workspace_id: int,
    request_id: int,
    allowed_from_statuses: set[str],
    expected_version: int,
) -> None:
    """CAS 失败（rowcount == 0）后的**固定顺序**判定。

    顺序：行不存在 → 404；状态不在允许集合 → 409 INVALID_PURCHASE_REQUEST_STATE；
    版本不等 → 409 PURCHASE_REQUEST_VERSION_CONFLICT。

    为什么状态先于版本：并发场景下"别人已经把它审批掉了"是**状态**问题
    （A-6 要求这个码），"版本落后"只有在状态仍可决策时才是版本问题（A-5）。
    """
    request = db.scalar(select(PurchaseRequest).where(
        PurchaseRequest.id == request_id,
        PurchaseRequest.workspace_id == workspace_id,
    ))
    if request is None:
        raise ValueError("PURCHASE_REQUEST_NOT_FOUND")
    if request.status not in allowed_from_statuses:
        raise ValueError("INVALID_PURCHASE_REQUEST_STATE")
    if request.version != expected_version:
        raise ValueError("PURCHASE_REQUEST_VERSION_CONFLICT")
    # 状态与版本都与预期一致却仍未命中：说明并发窗口内被别的事务抢先改过。
    raise ValueError("PURCHASE_REQUEST_VERSION_CONFLICT")


def _revert_suggestions(
    db: Session,
    *,
    workspace_id: int,
    request_id: int,
    now: datetime,
) -> None:
    """撤回的连带：把该单明细对应的补货建议回退为 `modified`。

    不回退会让这批建议永久卡在 `submitted`（再次申请时 `SUGGESTION_NOT_READY`），
    业务闭环断裂。代价：`confirmed` / `modified` 的区分在撤回后退化为 `modified`（R-51）。
    """
    suggestion_ids = db.scalars(select(PurchaseRequestLine.suggestion_id).where(
        PurchaseRequestLine.workspace_id == workspace_id,
        PurchaseRequestLine.purchase_request_id == request_id,
    )).all()
    if not suggestion_ids:
        return
    db.execute(
        update(ReplenishmentSuggestion)
        .where(
            ReplenishmentSuggestion.workspace_id == workspace_id,
            ReplenishmentSuggestion.id.in_(list(suggestion_ids)),
        )
        .values(
            status="modified",
            submitted_by=None,
            submitted_at=None,
            active_slot=None,
            version=ReplenishmentSuggestion.version + 1,
            updated_at=now,
        )
    )


def decide_purchase_request(
    db: Session,
    *,
    workspace_id: int,
    request_id: int,
    action: str,
    actor: str,
    user_id: int | None,
    idempotency_key: str | None,
    expected_version: int,
    reason: str | None = None,
    note: str | None = None,
    request_ref: str | None = None,
    is_admin: bool = False,
) -> PurchaseRequest:
    """approve / reject / cancel 的统一实现（三者同构）。

    执行顺序写死，不得调换：
    1. 幂等键校验；
    2. 幂等命中检查（**先于一切状态闸**）；
    3. 自审红线（approve/reject）或归属校验（cancel）；
    4. 单条 CAS UPDATE + `rowcount` 顺序判定；
    5. 审计行（同一事务）；
    6. cancel 的建议回退（同一事务）；
    7. 单次提交。
    """
    if action not in _TO_STATUS:
        raise ValueError("INVALID_PURCHASE_REQUEST_STATE")
    key = _require_idempotency_key(idempotency_key)

    request = _load_request(db, workspace_id=workspace_id, request_id=request_id)
    from_status = request.status
    to_status = _TO_STATUS[action]

    if action == "approve":
        allowed_from_statuses = APPROVABLE
    elif action == "reject":
        allowed_from_statuses = REJECTABLE
    else:
        allowed_from_statuses = CANCELLABLE

    # ---- 幂等命中检查（先于状态闸）----
    payload_hash = _decision_payload_hash(
        action, request_id=request_id, expected_version=expected_version,
        reason=reason, note=note,
    )
    existing = db.scalar(select(PurchaseRequestAction).where(
        PurchaseRequestAction.workspace_id == workspace_id,
        PurchaseRequestAction.purchase_request_id == request_id,
        PurchaseRequestAction.action_type == action,
        PurchaseRequestAction.idempotency_key == key,
    ))
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise ValueError("IDEMPOTENCY_KEY_REUSE")
        # 重放语义：零写入，直接返回当前单。
        return request

    # ---- 自审红线 / 归属校验（零写入）----
    if action in {"approve", "reject"}:
        _check_self_approval(request, actor)
    else:
        _check_owner(request, actor, is_admin=is_admin)
        if request.status == "approved":
            inbound_exists = db.scalar(select(InboundOrder.id).where(
                InboundOrder.workspace_id == workspace_id,
                InboundOrder.purchase_request_id == request_id,
            ))
            if inbound_exists is not None:
                raise ValueError("PURCHASE_REQUEST_ALREADY_INBOUND")

    now = datetime.utcnow()
    values: dict = {
        "status": to_status,
        "version": PurchaseRequest.version + 1,
        "updated_at": now,
    }
    if action == "approve":
        values.update({
            "approver_id": actor,
            "approver_user_id": user_id,
            "decided_at": now,
        })
    elif action == "reject":
        values.update({
            "approver_id": actor,
            "approver_user_id": user_id,
            "decided_at": now,
            "reject_reason": reason,
        })
    else:
        values.update({
            "cancelled_by": actor,
            "cancelled_at": now,
        })

    conditions = [
        PurchaseRequest.id == request_id,
        PurchaseRequest.workspace_id == workspace_id,
        PurchaseRequest.version == expected_version,
    ]
    if len(allowed_from_statuses) == 1:
        conditions.append(PurchaseRequest.status == next(iter(allowed_from_statuses)))
    else:
        conditions.append(PurchaseRequest.status.in_(sorted(allowed_from_statuses)))

    # ---- 单条 CAS UPDATE（唯一正确性来源）----
    result = db.execute(update(PurchaseRequest).where(*conditions).values(**values))
    if result.rowcount != 1:
        db.rollback()
        _resolve_cas_failure(
            db, workspace_id=workspace_id, request_id=request_id,
            allowed_from_statuses=allowed_from_statuses, expected_version=expected_version,
        )
        raise ValueError("PURCHASE_REQUEST_VERSION_CONFLICT")

    db.add(PurchaseRequestAction(
        workspace_id=workspace_id, purchase_request_id=request_id, action_type=action,
        from_status=from_status, to_status=to_status, idempotency_key=key,
        payload_hash=payload_hash, expected_version=expected_version, actor=actor,
        request_id=request_ref,
    ))

    if action == "cancel":
        _revert_suggestions(db, workspace_id=workspace_id, request_id=request_id, now=now)

    db.commit()
    db.expire(request)
    db.refresh(request)
    return request


def approve_purchase_request(
    db: Session,
    *,
    workspace_id: int,
    request_id: int,
    actor: str,
    user_id: int | None = None,
    idempotency_key: str | None = None,
    expected_version: int,
    note: str | None = None,
    request_ref: str | None = None,
) -> PurchaseRequest:
    """审批通过。权限点 `purchasing.approve`，与 `purchasing.submit` 相互独立。"""
    return decide_purchase_request(
        db, workspace_id=workspace_id, request_id=request_id, action="approve",
        actor=actor, user_id=user_id, idempotency_key=idempotency_key,
        expected_version=expected_version, note=note, request_ref=request_ref,
    )


def reject_purchase_request(
    db: Session,
    *,
    workspace_id: int,
    request_id: int,
    actor: str,
    user_id: int | None = None,
    idempotency_key: str | None = None,
    expected_version: int,
    reason: str,
    request_ref: str | None = None,
) -> PurchaseRequest:
    """驳回（终态）。理由必填，由调用方（端点层）先行校验长度与非空。"""
    return decide_purchase_request(
        db, workspace_id=workspace_id, request_id=request_id, action="reject",
        actor=actor, user_id=user_id, idempotency_key=idempotency_key,
        expected_version=expected_version, reason=reason, request_ref=request_ref,
    )


def cancel_purchase_request(
    db: Session,
    *,
    workspace_id: int,
    request_id: int,
    actor: str,
    user_id: int | None = None,
    idempotency_key: str | None = None,
    expected_version: int,
    reason: str | None = None,
    request_ref: str | None = None,
    is_admin: bool = False,
) -> PurchaseRequest:
    """撤回（终态）。权限点 `purchasing.submit`；非制单人/提交人一律 403。"""
    return decide_purchase_request(
        db, workspace_id=workspace_id, request_id=request_id, action="cancel",
        actor=actor, user_id=user_id, idempotency_key=idempotency_key,
        expected_version=expected_version, reason=reason, request_ref=request_ref,
        is_admin=is_admin,
    )


def generate_inbound_order_for_request(
    db: Session,
    *,
    workspace_id: int,
    request_id: int,
    actor: str,
    note: str | None = None,
):
    """由已审批通过的采购单生成入库单（自然键幂等：一采购单至多一张入库单）。

    返回 `(inbound_order, created)`：`created` 为 True 表示本次新建（端点返回 201），
    False 表示命中幂等（端点返回 200 + 既有单）。

    - 前置：`status == "approved"`，否则 409 INVALID_PURCHASE_REQUEST_STATE；
    - 明细：按 `purchase_request_lines.id` 升序，`expected_qty = requested_qty`（精确相等，
      不取整/不合并/不裁剪）；
    - 调用**既有** `create_inbound` 服务，不新写库存逻辑：本函数**零库存副作用**
      （不写 InventoryBalance / InventoryTransaction）；
    - 幂等：已存在则直接返回既有单；并发撞唯一索引时捕获 IntegrityError →
      rollback → 重查 → 返回既有单。
    """
    request = _load_request(db, workspace_id=workspace_id, request_id=request_id)
    if request.status != "approved":
        raise ValueError("INVALID_PURCHASE_REQUEST_STATE")

    existing = db.scalar(select(InboundOrder).where(
        InboundOrder.workspace_id == workspace_id,
        InboundOrder.purchase_request_id == request_id,
    ))
    if existing is not None:
        return existing, False

    lines = db.scalars(select(PurchaseRequestLine).where(
        PurchaseRequestLine.workspace_id == workspace_id,
        PurchaseRequestLine.purchase_request_id == request_id,
    ).order_by(PurchaseRequestLine.id)).all()
    if not lines:
        raise ValueError("PURCHASE_REQUEST_HAS_NO_LINES")

    from app.repository import create_inbound

    try:
        order = create_inbound(
            db,
            warehouse_id=request.warehouse_id,
            reference_no=f"INB-{request.request_no}",
            lines=[{"sku_id": line.sku_id, "expected_qty": line.requested_qty} for line in lines],
            note=note,
            created_by=actor,
            workspace_id=workspace_id,
            purchase_request_id=request.id,
        )
    except IntegrityError:
        # 并发生成：唯一索引 uq_inbound_order_purchase_request 兜底。
        db.rollback()
        existing = db.scalar(select(InboundOrder).where(
            InboundOrder.workspace_id == workspace_id,
            InboundOrder.purchase_request_id == request_id,
        ))
        if existing is None:
            raise
        return existing, False
    return order, True


def get_inbound_order_for_request(
    db: Session,
    *,
    workspace_id: int,
    request_id: int,
):
    """读取采购单关联的入库单（未生成时返回 None）。"""
    _load_request(db, workspace_id=workspace_id, request_id=request_id)
    return db.scalar(select(InboundOrder).where(
        InboundOrder.workspace_id == workspace_id,
        InboundOrder.purchase_request_id == request_id,
    ))
