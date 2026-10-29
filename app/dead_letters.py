"""P2-4 补偿机制：死信登记 / 分类 / 退避 / 重放前置校验 / 僵尸判定（纯领域模块）。

红线（dev_task.p2s4-compensation.md）：
- **零自动触发**：本模块不含任何常驻执行体（R-27 单进程定调），到期判定全部在
  请求时惰性计算——`next_replay_at` 只是"放行门槛"，没有到点自动跑的机制。
- **零网络**：本模块不构造任何上游客户端，重放的实际同步本体走既有链路。
- **不依赖 Web 框架**：本模块不 import fastapi，也不抛 HTTP 异常；
  领域拒绝统一用 `DeadLetterRejected`（带 code + message），由调用方翻译成
  API 的 409 / CLI 的错误 JSON。
- **凭据红线**：登记内容里的错误信息一律过 `sanitize_error_message`
  （截断 500 + 抹掉 token 形态字符串），死信表不落任何凭据材料。

模块常量 `RETRY_CLASS_BY_CODE` 是分类表的**唯一出处**（测试锁死）：
恰好 3 个瞬时码进 auto，其余一切（含未知码）归 manual。
"""

from __future__ import annotations

import random
import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import ExternalSyncDeadLetter, ExternalSyncRun


# 死信状态（三态，见任务书 §4 T3 状态流转）。
DEAD_LETTER_STATUS_OPEN = "open"
DEAD_LETTER_STATUS_REPLAYED = "replayed"
DEAD_LETTER_STATUS_IGNORED = "ignored"

# 重试分类。
RETRY_CLASS_AUTO = "auto"
RETRY_CLASS_MANUAL = "manual"

# 分类表（写死，测试锁死）：三个瞬时码 → auto；其余一切 → manual。
# - SHOPIFY_SYNC_BUDGET_EXCEEDED：资源耗尽状态，归人工（用户 2026-10-29 裁定）。
# - SHOPIFY_SYNC_CANCELLED：人工取消永不自动重试。
# - 未列出的码（含未知码）：缺数据不硬给结论 → manual。
RETRY_CLASS_BY_CODE: dict[str, str] = {
    "SHOPIFY_TIMEOUT": RETRY_CLASS_AUTO,
    "SHOPIFY_RATE_LIMITED": RETRY_CLASS_AUTO,
    "SHOPIFY_UPSTREAM_ERROR": RETRY_CLASS_AUTO,
    "SHOPIFY_SYNC_BUDGET_EXCEEDED": RETRY_CLASS_MANUAL,
    "SHOPIFY_SYNC_CANCELLED": RETRY_CLASS_MANUAL,
}

# 退避抖动上限：next_replay_at = failed_at + base × 2^(attempt−1) × (1 + jitter)。
BACKOFF_JITTER_RATIO = 0.25

# 领域拒绝码（API 侧一律 409）。
REJECT_NOT_OPEN = "DEAD_LETTER_NOT_OPEN"
REJECT_EXHAUSTED = "DEAD_LETTER_EXHAUSTED"
REJECT_MANUAL_ONLY = "DEAD_LETTER_MANUAL_ONLY"
REJECT_BACKOFF_NOT_REACHED = "DEAD_LETTER_BACKOFF_NOT_REACHED"

# 死信表名（供只读视图与 CLI 复用）。
DEAD_LETTER_TABLE = "external_sync_dead_letters"

# 凭据形态字符串（Shopify access token 前缀）——登记前一律抹掉。
_TOKEN_PATTERNS = (
    re.compile(r"shp[a-z]{2}_[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\b(token|secret|password|authorization)\b\s*[:=]\s*\S+"),
)


class DeadLetterRejected(Exception):
    """领域拒绝（不带 HTTP 语义）：code + message，由 API / CLI 各自翻译。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def classify_retry_class(error_code: str | None) -> str:
    """按分类表判定重试类别；未知码（含空）一律 manual。"""
    if not error_code:
        return RETRY_CLASS_MANUAL
    return RETRY_CLASS_BY_CODE.get(str(error_code).strip().upper(), RETRY_CLASS_MANUAL)


def sanitize_error_message(message: str | None, *, limit: int = 500) -> str | None:
    """抹掉凭据形态字符串并截断——死信表不落任何凭据材料。"""
    if message is None:
        return None
    text = str(message)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub("***", text)
    return text[:limit]


def backoff_seconds(attempt: int, base_seconds: int, jitter: float | None = None) -> float:
    """退避时长：base × 2^(attempt−1) × (1 + jitter)，jitter ∈ [0, 0.25] 均匀随机。"""
    number = max(1, int(attempt or 1))
    factor = 2 ** (number - 1)
    if jitter is None:
        jitter = random.uniform(0.0, BACKOFF_JITTER_RATIO)
    bounded = max(0.0, min(BACKOFF_JITTER_RATIO, float(jitter)))
    return float(base_seconds) * factor * (1.0 + bounded)


def compute_next_replay_at(
    failed_at: datetime,
    attempt: int,
    base_seconds: int,
    jitter: float | None = None,
) -> datetime:
    """退避门槛时刻：写入时**一次算死**（不随读取漂移）。"""
    return failed_at + timedelta(seconds=backoff_seconds(attempt, base_seconds, jitter))


def resolve_error_code(run) -> str | None:
    """登记用的错误码：run.error_code 优先；否则取资源台账里第一个失败资源的错误码。"""
    code = getattr(run, "error_code", None)
    if code:
        return str(code)[:64]
    from app.external_sync import resource_status

    statuses = resource_status(run)
    for name in sorted(statuses):
        entry = statuses.get(name)
        if not isinstance(entry, dict) or entry.get("status") != "failed":
            continue
        item_code = entry.get("error_code")
        if item_code:
            return str(item_code)[:64]
    return None


def get_dead_letter(db: Session, *, workspace_id: int | None, run_id: str) -> ExternalSyncDeadLetter | None:
    """按 (workspace_id, run_id) 取死信行；跨 workspace 取不到（多租户隔离）。"""
    if not run_id:
        return None
    return db.scalar(
        select(ExternalSyncDeadLetter).where(
            ExternalSyncDeadLetter.run_id == run_id,
            ExternalSyncDeadLetter.workspace_id == workspace_id,
        )
    )


def list_dead_letters(
    db: Session,
    *,
    workspace_id: int | None = None,
    status: str | None = None,
    retry_class: str | None = None,
    limit: int = 50,
) -> list[ExternalSyncDeadLetter]:
    """死信列表（最新在前）。"""
    stmt = select(ExternalSyncDeadLetter)
    if workspace_id is not None:
        stmt = stmt.where(ExternalSyncDeadLetter.workspace_id == workspace_id)
    if status:
        stmt = stmt.where(ExternalSyncDeadLetter.status == status)
    if retry_class:
        stmt = stmt.where(ExternalSyncDeadLetter.retry_class == retry_class)
    stmt = stmt.order_by(ExternalSyncDeadLetter.id.desc()).limit(max(1, int(limit or 50)))
    return list(db.scalars(stmt))


def register_dead_letter(
    db: Session,
    run,
    *,
    base_seconds: int | None = None,
    max_attempts: int | None = None,
) -> ExternalSyncDeadLetter | None:
    """把失败的 run 登记成死信行。

    幂等：先查后插——同 run 至多一条死信（唯一约束 (workspace_id, run_id) 兜底），
    finalize 重入安全。分类 / 退避 / 上限标记**登记时一次算死**。
    """
    from app.config import get_settings

    settings = get_settings()
    if base_seconds is None:
        base_seconds = settings.sync_compensation_backoff_base_seconds
    if max_attempts is None:
        max_attempts = settings.sync_compensation_max_attempts
    existing = get_dead_letter(db, workspace_id=run.workspace_id, run_id=run.run_id)
    if existing is not None:
        return existing
    error_code = resolve_error_code(run)
    retry_class = classify_retry_class(error_code)
    attempt = int(run.attempt or 1)
    failed_at = getattr(run, "finished_at", None) or datetime.utcnow()
    next_replay_at = (
        compute_next_replay_at(failed_at, attempt, base_seconds)
        if retry_class == RETRY_CLASS_AUTO
        else None
    )
    now = datetime.utcnow()
    letter = ExternalSyncDeadLetter(
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        platform=run.platform,
        sync_type=run.sync_type,
        source_mode=run.source_mode,
        error_code=error_code,
        error_message=sanitize_error_message(getattr(run, "error_message", None)),
        retry_class=retry_class,
        attempt=attempt,
        next_replay_at=next_replay_at,
        exhausted=attempt >= int(max_attempts),
        quality_status=getattr(run, "quality_status", None),
        status=DEAD_LETTER_STATUS_OPEN,
        actor=getattr(run, "actor", None),
        request_id=getattr(run, "request_id", None),
        created_at=now,
        updated_at=now,
    )
    db.add(letter)
    try:
        db.commit()
    except IntegrityError:
        # 并发登记撞唯一约束：回滚后取既有行（幂等登记）。
        db.rollback()
        existing = get_dead_letter(db, workspace_id=run.workspace_id, run_id=run.run_id)
        if existing is not None:
            return existing
        raise
    db.refresh(letter)
    return letter


def check_replay_preconditions(letter: ExternalSyncDeadLetter, *, now: datetime, max_attempts: int) -> None:
    """replay 的前三道闸门（状态 → 分类 → 退避），**顺序固定不得颠倒**。

    第四道（并发闸 `_sync_gate_lock`）在调用方（API / CLI）抢，顺序接在本函数之后。
    拒绝一律抛 `DeadLetterRejected`（code 语义见任务书 §4 T4）。
    """
    if letter.status != DEAD_LETTER_STATUS_OPEN:
        raise DeadLetterRejected(REJECT_NOT_OPEN, "该死信不在 open 状态，不能重放")
    if int(letter.attempt or 1) >= int(max_attempts):
        raise DeadLetterRejected(REJECT_EXHAUSTED, "该死信已达重试上限，只能人工归档")
    if letter.retry_class != RETRY_CLASS_AUTO:
        raise DeadLetterRejected(REJECT_MANUAL_ONLY, "该死信为人工类，不允许重放")
    if letter.next_replay_at is not None and now < letter.next_replay_at:
        raise DeadLetterRejected(
            REJECT_BACKOFF_NOT_REACHED,
            "退避未到期，暂不可重放",
        )


def refresh_exhausted_flag(letter: ExternalSyncDeadLetter, *, max_attempts: int) -> None:
    """按**当前配置**刷新 exhausted 标记（配置调大即解锁，不另设"强制"参数）。"""
    letter.exhausted = int(letter.attempt or 1) >= int(max_attempts)


def find_replay_run(
    db: Session,
    *,
    workspace_id: int | None,
    retry_of_run_id: str,
    idempotency_key: str,
):
    """按既有唯一约束的语义找"同幂等键的既有重放 run"（run 行幂等，T5-2）。"""
    if not idempotency_key:
        return None
    return db.scalar(
        select(ExternalSyncRun).where(
            ExternalSyncRun.workspace_id == workspace_id,
            ExternalSyncRun.retry_of_run_id == retry_of_run_id,
            ExternalSyncRun.retry_idempotency_key == idempotency_key,
        )
    )


def mark_replayed(
    db: Session,
    letter: ExternalSyncDeadLetter,
    *,
    replayed_run_id: str,
    actor: str | None,
    request_id: str | None,
) -> ExternalSyncDeadLetter:
    """死信 → replayed（终态）。"""
    letter.status = DEAD_LETTER_STATUS_REPLAYED
    letter.replayed_run_id = replayed_run_id
    letter.actor = actor
    letter.request_id = request_id
    letter.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(letter)
    return letter


def mark_ignored(
    db: Session,
    letter: ExternalSyncDeadLetter,
    *,
    reason: str,
    actor: str | None,
    request_id: str | None,
) -> ExternalSyncDeadLetter:
    """死信 → ignored（终态，reason 必填）。"""
    if letter.status != DEAD_LETTER_STATUS_OPEN:
        raise DeadLetterRejected(REJECT_NOT_OPEN, "该死信不在 open 状态，不能归档")
    letter.status = DEAD_LETTER_STATUS_IGNORED
    letter.ignore_reason = sanitize_error_message(reason)
    letter.actor = actor
    letter.request_id = request_id
    letter.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(letter)
    return letter


def archive_zombie_run(
    db: Session,
    run,
    *,
    reason: str,
    actor: str | None,
    request_id: str | None,
    max_attempts: int,
) -> ExternalSyncDeadLetter:
    """把僵尸 run 归档成一条 ignored 死信（只读判定的唯一写出口）。

    不改 run 状态、不 kill、不生成新 run（任务书 §4 T6）。
    """
    existing = get_dead_letter(db, workspace_id=run.workspace_id, run_id=run.run_id)
    if existing is not None:
        return mark_ignored(db, existing, reason=reason, actor=actor, request_id=request_id)
    now = datetime.utcnow()
    letter = ExternalSyncDeadLetter(
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        platform=run.platform,
        sync_type=run.sync_type,
        source_mode=run.source_mode,
        error_code=resolve_error_code(run),
        error_message=sanitize_error_message(getattr(run, "error_message", None)),
        retry_class=RETRY_CLASS_MANUAL,
        attempt=int(run.attempt or 1),
        next_replay_at=None,
        exhausted=int(run.attempt or 1) >= int(max_attempts),
        quality_status=getattr(run, "quality_status", None),
        status=DEAD_LETTER_STATUS_IGNORED,
        actor=actor,
        request_id=request_id,
        ignore_reason=sanitize_error_message(reason),
        created_at=now,
        updated_at=now,
    )
    db.add(letter)
    db.commit()
    db.refresh(letter)
    return letter


def zombie_runs(
    db: Session,
    *,
    workspace_id: int | None = None,
    stale_after_seconds: int = 900,
    now: datetime | None = None,
    limit: int = 50,
) -> list[ExternalSyncRun]:
    """僵尸 run 的只读判定：status=running 且 finished_at IS NULL 且 heartbeat 过期。"""
    moment = now or datetime.utcnow()
    threshold = moment - timedelta(seconds=int(stale_after_seconds))
    stmt = select(ExternalSyncRun).where(
        ExternalSyncRun.status == "running",
        ExternalSyncRun.finished_at.is_(None),
        ExternalSyncRun.heartbeat_at.is_not(None),
        ExternalSyncRun.heartbeat_at < threshold,
    )
    if workspace_id is not None:
        stmt = stmt.where(ExternalSyncRun.workspace_id == workspace_id)
    stmt = stmt.order_by(ExternalSyncRun.id.desc()).limit(max(1, int(limit or 50)))
    return list(db.scalars(stmt))


def quality_view(
    db: Session,
    *,
    workspace_id: int | None = None,
    quality_status: str = "failed",
    limit: int = 50,
) -> list[ExternalSyncRun]:
    """质量只读视图：直接查 run 表，**不落死信行、不可 replay**（R-45 观察态边界）。"""
    stmt = select(ExternalSyncRun).where(ExternalSyncRun.quality_status == quality_status)
    if workspace_id is not None:
        stmt = stmt.where(ExternalSyncRun.workspace_id == workspace_id)
    stmt = stmt.order_by(ExternalSyncRun.id.desc()).limit(max(1, int(limit or 50)))
    return list(db.scalars(stmt))
