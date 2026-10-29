"""授权引用实体（P2-2 / R-38）：只记"哪把钥匙"，不记钥匙本身。

设计红线（不可放松）：

1. **明文凭据任何形态都不得入库**——值、可逆编码、全文 hash 一律禁止。
   本模块只落库两样东西：env **键名**（如 `SHOPIFY_ACCESS_TOKEN`）
   与 8 位 sha256 指纹（`sha256(明文)` 的前 8 位十六进制）。
   指纹只用于人工比对"是不是同一把钥匙"，不可逆推明文。
2. **凭证注入链路完全不变**——`ShopifyLiveTransport` 仍然从 `Settings`/`.env`
   读 `shopify_access_token` 构造；本模块不参与、不改写注入路径，
   只在 run 行上留下"这次用的是哪一把钥匙"的引用与一致性校验。
3. **明文只在内存瞬态存在**：计算指纹时读一次即用，不写库、不写日志。
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import ExternalCredentialRef

# 指纹长度：sha256 前 8 位十六进制（任务文件写死）。
FINGERPRINT_LENGTH = 8

# 已知的 env 键 → Settings 字段映射（用于 Settings 已加载 .env 时的兜底读取）。
_SETTING_FIELDS_BY_ENV_KEY: dict[str, str] = {
    "SHOPIFY_ACCESS_TOKEN": "shopify_access_token",
    "SHOPIFY_STORE_DOMAIN": "shopify_store_domain",
}

# CLI 明确拒绝的入参：管理面**不收明文**。
FORBIDDEN_SECRET_OPTIONS: tuple[str, ...] = (
    "--secret",
    "--token",
    "--value",
    "--plaintext",
    "--password",
)


class CredentialRefError(Exception):
    """授权引用管理面的业务错误（错误码固定，不新增 HTTP 语义）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def fingerprint_of(secret: str) -> str:
    """凭据指纹：sha256 前 8 位十六进制。

    只保留 8 位是为了"人工比对是不是同一把钥匙"，
    不是为了做不可逆存储——所以明文绝不入库、全文 hash 也不入库。
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


def read_env_secret(env_key: str) -> str | None:
    """读取某个 env 键当前的值（进程环境 → .env 文件 → Settings 字段）。

    读取到的明文**只在内存瞬态存在**：调用方应当立刻用它算指纹并丢弃。
    """
    value = os.environ.get(env_key)
    if value:
        return value

    from app.config import get_settings

    settings = get_settings()
    env_path: Path = settings.resolve_path(".env")
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, raw = stripped.partition("=")
            if key.strip() != env_key:
                continue
            candidate = raw.strip().strip("'").strip('"')
            if candidate:
                return candidate

    field_name = _SETTING_FIELDS_BY_ENV_KEY.get(env_key)
    if field_name:
        candidate = getattr(settings, field_name, "")
        if candidate:
            return candidate
    return None


def list_credential_refs(
    db: Session,
    *,
    platform: str | None = None,
    workspace_id: int | None = None,
) -> list[ExternalCredentialRef]:
    """列出授权引用（可按平台 / 工作空间过滤）。"""
    stmt = select(ExternalCredentialRef).order_by(ExternalCredentialRef.id)
    if platform:
        stmt = stmt.where(ExternalCredentialRef.platform == platform)
    if workspace_id is not None:
        stmt = stmt.where(ExternalCredentialRef.workspace_id == workspace_id)
    return list(db.scalars(stmt).all())


def add_credential_ref(
    db: Session,
    *,
    platform: str,
    ref_name: str,
    env_key: str,
    secret: str | None = None,
    workspace_id: int | None = None,
    created_by: str | None = None,
) -> ExternalCredentialRef:
    """登记一条授权引用。

    - `secret` 省略时从当前 env 键值读取（管理面**不收明文入参**）；
    - 落库的只有 env 键名与 8 位指纹；
    - `ref_name` 全局唯一（唯一约束兜底 + 显式检查）。
    """
    if not ref_name:
        raise CredentialRefError("CREDENTIAL_REF_NAME_REQUIRED", "缺少授权引用名")
    if not env_key:
        raise CredentialRefError("CREDENTIAL_REF_ENV_KEY_REQUIRED", "缺少 env 键名")
    if not platform:
        raise CredentialRefError("CREDENTIAL_REF_PLATFORM_REQUIRED", "缺少平台")

    if secret is None:
        secret = read_env_secret(env_key)
    if not secret:
        raise CredentialRefError(
            "CREDENTIAL_REF_SECRET_NOT_FOUND",
            f"环境变量 {env_key} 当前没有取值，无法计算指纹",
        )

    existing = db.scalar(select(ExternalCredentialRef).where(ExternalCredentialRef.ref_name == ref_name))
    if existing is not None:
        raise CredentialRefError("CREDENTIAL_REF_NAME_EXISTS", f"授权引用名已存在：{ref_name}")

    ref = ExternalCredentialRef(
        workspace_id=workspace_id,
        platform=platform,
        ref_name=ref_name,
        env_key=env_key,
        secret_fingerprint=fingerprint_of(secret),
        created_by=created_by,
    )
    db.add(ref)
    db.commit()
    db.refresh(ref)
    return ref


def remove_credential_ref(db: Session, *, ref_name: str) -> ExternalCredentialRef | None:
    """删除一条授权引用（**硬删**：整行删除，不留软删标记）。

    语义锁定：删除后该引用不可恢复；历史 run 行上已有的 `credential_ref_id`
    保留原值（归因留痕优先于引用完整性），不因删除而改写。
    """
    ref = db.scalar(select(ExternalCredentialRef).where(ExternalCredentialRef.ref_name == ref_name))
    if ref is None:
        return None
    db.delete(ref)
    db.commit()
    return ref


@dataclass(frozen=True)
class CredentialResolution:
    """授权引用解析结果。

    - `ref`：指纹匹配到的引用（没有则为 None）；
    - `conflict`：该平台 + 键名下**存在引用但没有一把与当前凭据指纹匹配**
      （换过钥匙的失配情形，见 R-41）——此时 live 路径必须拒绝放行。
    """

    ref: ExternalCredentialRef | None
    conflict: bool


def resolve_credential_ref(
    db: Session,
    *,
    platform: str,
    env_key: str,
    secret: str | None,
) -> CredentialResolution:
    """按"平台 + env 键名 + 当前凭据指纹"解析授权引用。

    口径（写死，避免随机应变）：

    - 该平台 + 键名下**没有任何引用** → 不阻塞（`ref=None, conflict=False`），
      live 路径照旧放行，只是 run 行没有 `credential_ref_id`；
    - 有引用且有一把指纹匹配 → `ref` 为该引用；
    - 有引用但**没有一把匹配** → `conflict=True`，调用方按既有
      `409 SHOPIFY_LIVE_NOT_CONFIGURED` 语义拒绝（不新增错误码）。
    """
    candidates = list(
        db.scalars(
            select(ExternalCredentialRef).where(
                ExternalCredentialRef.platform == platform,
                ExternalCredentialRef.env_key == env_key,
            )
        ).all()
    )
    if not candidates:
        return CredentialResolution(ref=None, conflict=False)
    if not secret:
        return CredentialResolution(ref=None, conflict=True)
    fingerprint = fingerprint_of(secret)
    for candidate in candidates:
        if candidate.secret_fingerprint == fingerprint:
            return CredentialResolution(ref=candidate, conflict=False)
    return CredentialResolution(ref=None, conflict=True)
