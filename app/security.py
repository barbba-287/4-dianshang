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
    auth_type: str = "api_key"


def demo_principal(settings: Settings) -> Principal:
    return Principal(subject="anonymous", tenant_id=settings.api_tenant_id or "default")


def validate_auth_config(settings: Settings) -> None:
    if not settings.api_auth_enabled:
        return
    if len(settings.api_key) < 32:
        raise ValueError("API_AUTH_ENABLED=true 时必须配置至少 32 位 API_KEY")
    if not settings.api_tenant_id.strip():
        raise ValueError("API_AUTH_ENABLED=true 时必须配置 API_TENANT_ID")


def authenticate_api_key(value: str | None, settings: Settings) -> Principal | None:
    validate_auth_config(settings)
    if not settings.api_auth_enabled:
        return demo_principal(settings)
    if not value or not hmac.compare_digest(value, settings.api_key):
        return None
    return Principal(
        subject=settings.api_user_id or "api-user",
        tenant_id=settings.api_tenant_id,
        key_id="static-api-key",
        scopes=("*",),
        auth_type="api_key",
    )
