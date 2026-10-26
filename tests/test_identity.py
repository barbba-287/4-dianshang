"""R2 身份上下文统一的 contract tests。

覆盖任务文件 §7.2 要求：

1. `Principal.can` / `has_role` 语义（demo / api_key / admin / 角色权限表）。
2. `authenticate_api_key`：auth 关闭 → demo principal；key 错 → None；
   API 开启但工作空间缺失 → None（**不自动创建**）。
3. `_resolve_compat_workspace`：停用同名空间不被复活。
4. `app.security` 已删除（`import app.security` 抛 ImportError）。
5. re-export 路径可用：`from app.employee_auth import ROLE_ADMIN, hash_password,
   Principal, ROLE_PERMISSIONS`。
6. `get_request_context`：ctx 缺失 → `DomainError("UNAUTHORIZED")`；
   ctx 存在时返回 request_id / principal。
7. 中间件三个成功路径均挂上 `request.state.ctx`，且与旧字段一致。
"""

import importlib
import re
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

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
    _resolve_compat_workspace,
    authenticate_api_key,
    demo_principal,
    validate_auth_config,
)
from app.request_context import RequestContext, get_request_context


# --------------------------------------------------------------------------
# 1. Principal.can / has_role
# --------------------------------------------------------------------------


def test_can_is_always_false_for_demo_principal():
    """demo 身份的 `can` 恒为 False（不受角色影响）。"""
    principal = Principal(subject="anonymous", tenant_id="default", auth_type="demo", roles=(ROLE_ADMIN,))
    assert principal.can("inventory.read") is False
    assert principal.can("anything.at.all") is False


def test_can_for_api_key_uses_scopes_and_wildcard():
    """api_key 身份按 scope 判定，`*` 通配全部放行。"""
    scoped = Principal(subject="api-user", tenant_id="t1", key_id="static-api-key", scopes=("inventory.read",), auth_type="api_key")
    assert scoped.can("inventory.read") is True
    assert scoped.can("inventory.write") is False

    wildcard = Principal(subject="api-user", tenant_id="t1", key_id="static-api-key", scopes=("*",), auth_type="api_key")
    assert wildcard.can("inventory.read") is True
    assert wildcard.can("whatever") is True


def test_can_for_admin_session_is_full_access():
    """admin 角色（权限表为空集合）代表全权。"""
    admin = Principal(subject="admin", tenant_id="1", roles=(ROLE_ADMIN,), auth_type="session")
    assert admin.can("inventory.read") is True
    assert admin.can("content.export") is True


def test_can_for_session_follows_role_permission_table():
    """普通员工按角色权限表命中与否。"""
    warehouse = Principal(subject="w", tenant_id="1", roles=(ROLE_WAREHOUSE,), auth_type="session")
    assert warehouse.can("inbound.read") is True
    assert warehouse.can("inventory.read") is True
    assert warehouse.can("catalog.write") is False

    customer_service = Principal(subject="cs", tenant_id="1", roles=(ROLE_CUSTOMER_SERVICE,), auth_type="session")
    assert customer_service.can("knowledge.write") is True
    assert customer_service.can("inbound.create") is False

    readonly = Principal(subject="ro", tenant_id="1", roles=(ROLE_READONLY,), auth_type="session")
    assert readonly.can("catalog.read") is True
    assert readonly.can("content.write") is False

    operations = Principal(subject="op", tenant_id="1", roles=(ROLE_OPERATIONS,), auth_type="session")
    assert operations.can("replenishment.confirm") is True
    assert operations.can("inbound.receive") is False


def test_has_role_matches_any_required_role():
    principal = Principal(subject="op", tenant_id="1", roles=(ROLE_OPERATIONS,), auth_type="session")
    assert principal.has_role(ROLE_OPERATIONS) is True
    assert principal.has_role(ROLE_ADMIN, ROLE_OPERATIONS) is True
    assert principal.has_role(ROLE_ADMIN) is False
    assert principal.has_role() is False


def test_is_employee_only_for_session_auth_type():
    assert Principal(subject="a", tenant_id="1", auth_type="session").is_employee is True
    assert Principal(subject="a", tenant_id="1", auth_type="api_key").is_employee is False
    assert Principal(subject="a", tenant_id="1", auth_type="demo").is_employee is False


def test_role_labels_and_permissions_table_are_intact():
    """角色常量与权限表保持原值（收敛不改变语义）。"""
    assert ROLE_ADMIN == "admin"
    assert ROLE_OPERATIONS == "operations"
    assert ROLE_WAREHOUSE == "warehouse"
    assert ROLE_CUSTOMER_SERVICE == "customer_service"
    assert ROLE_READONLY == "readonly"
    assert ROLE_PERMISSIONS[ROLE_ADMIN] == frozenset()
    assert "inventory.read" in ROLE_PERMISSIONS[ROLE_READONLY]
    assert "catalog.write" not in ROLE_PERMISSIONS[ROLE_READONLY]
    assert set(ROLE_LABELS) == {
        ROLE_ADMIN,
        ROLE_OPERATIONS,
        ROLE_WAREHOUSE,
        ROLE_CUSTOMER_SERVICE,
        ROLE_READONLY,
    }


# --------------------------------------------------------------------------
# 2. authenticate_api_key
# --------------------------------------------------------------------------


def _settings(**overrides):
    from app.config import Settings

    base = {
        "api_auth_enabled": False,
        "api_key": "",
        "api_tenant_id": "default",
        "api_user_id": "api-user",
        "employee_auth_enabled": False,
        "session_ttl_seconds": 3600,
        "auth_bootstrap_tenant_key": "default",
        "auth_bootstrap_workspace_name": "默认商家",
    }
    base.update(overrides)
    return Settings(**base)


def test_authenticate_api_key_returns_demo_principal_when_api_auth_disabled():
    """API 关闭 → demo principal（走 compat 语义，不做 key 校验）。"""
    principal = authenticate_api_key(None, _settings())
    assert principal is not None
    assert principal.auth_type == "demo"
    assert principal.subject == "anonymous"


def test_authenticate_api_key_returns_none_on_wrong_key():
    """API 开启 + key 不匹配 → None。"""
    settings = _settings(api_auth_enabled=True, api_key="k" * 40, api_tenant_id="t1")
    assert authenticate_api_key("wrong-key", settings) is None
    assert authenticate_api_key(None, settings) is None
    assert authenticate_api_key("", settings) is None


def _temp_session(tmp_path, name: str):
    """在临时 SQLite 上建表并返回 (SessionLocal, engine)，不触碰项目真实库。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.db as db_mod

    engine = create_engine(f"sqlite:///{tmp_path / name}")
    db_mod.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False), engine


def test_authenticate_api_key_returns_none_when_workspace_missing(tmp_path):
    """API 开启但工作空间缺失 → None，且**不自动创建**工作空间。

    这是 R2 相对旧 `app/security.py` 的有意行为变更（安全收紧）：
    旧版会静默创建工作空间并放行，新版直接 401。
    """
    from app.db import Workspace

    SessionLocal, _engine = _temp_session(tmp_path, "r2-missing.db")
    settings = _settings(api_auth_enabled=True, api_key="k" * 40, api_tenant_id="r2-missing")

    with SessionLocal() as db:
        before = db.query(Workspace).count()
        principal = authenticate_api_key("k" * 40, settings, db=db)
        after = db.query(Workspace).count()

    assert principal is None
    assert after == before, "工作空间缺失时不得自动创建"


def test_authenticate_api_key_matches_active_workspace_by_tenant_id(tmp_path):
    """API 开启 + 存在 active 且 tenant_key 匹配的工作空间 → api_key principal。"""
    from app.db import Workspace

    SessionLocal, _engine = _temp_session(tmp_path, "r2-tenant.db")
    settings = _settings(api_auth_enabled=True, api_key="k" * 40, api_tenant_id="r2-tenant")

    with SessionLocal() as db:
        workspace = Workspace(tenant_key="r2-tenant", name="R2 测试商家")
        db.add(workspace)
        db.commit()
        db.refresh(workspace)

        principal = authenticate_api_key("k" * 40, settings, db=db)
        assert principal is not None
        assert principal.auth_type == "api_key"
        assert principal.workspace_id == workspace.id
        assert principal.scopes == ("*",)


def test_validate_auth_config_requires_api_key_and_tenant_when_enabled():
    settings = _settings(api_auth_enabled=True, api_key="short", api_tenant_id="t1")
    with pytest.raises(ValueError):
        validate_auth_config(settings)

    settings = _settings(api_auth_enabled=True, api_key="k" * 40, api_tenant_id="  ")
    with pytest.raises(ValueError):
        validate_auth_config(settings)

    settings = _settings(api_auth_enabled=True, api_key="k" * 40, api_tenant_id="t1")
    validate_auth_config(settings)  # 不抛


def test_demo_principal_uses_configured_tenant_and_workspace():
    settings = _settings(api_tenant_id="r2-tenant")
    principal = demo_principal(settings, workspace_id=7)
    assert principal.tenant_id == "r2-tenant"
    assert principal.workspace_id == 7
    assert principal.auth_type == "demo"
    assert principal.is_employee is False


# --------------------------------------------------------------------------
# 3. _resolve_compat_workspace：停用空间不复活
# --------------------------------------------------------------------------


def test_resolve_compat_workspace_does_not_revive_disabled_workspace(tmp_path):
    """停用的同名空间不被复活，也不重复创建。"""
    from app.db import Workspace

    SessionLocal, _engine = _temp_session(tmp_path, "r2-disabled.db")
    settings = _settings(auth_bootstrap_tenant_key="r2-disabled")

    with SessionLocal() as db:
        disabled = Workspace(tenant_key="r2-disabled", name="已停用商家", status="disabled")
        db.add(disabled)
        db.commit()
        db.refresh(disabled)

        resolved = _resolve_compat_workspace(db, settings)
        assert resolved is None

        remaining = db.query(Workspace).filter(Workspace.tenant_key == "r2-disabled").all()
        assert len(remaining) == 1
        assert remaining[0].status == "disabled"


# --------------------------------------------------------------------------
# 4. app.security 已删除
# --------------------------------------------------------------------------


def test_app_security_module_is_deleted():
    """`app.security` 不再存在，导入抛 ImportError。"""
    with pytest.raises(ImportError):
        importlib.import_module("app.security")


def test_identity_and_request_context_do_not_import_fastapi():
    """领域层模块与 Web 框架解耦（P1 项 5 的达成形态）。"""
    import app.identity
    import app.request_context

    for module in (app.identity, app.request_context):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert re.search(r"^\s*(from|import)\s+fastapi", source, re.M) is None


# --------------------------------------------------------------------------
# 5. re-export 路径
# --------------------------------------------------------------------------


def test_employee_auth_reexports_identity_names():
    """既有导入路径保持不变。"""
    from app.employee_auth import (  # noqa: F401
        ROLE_ADMIN,
        ROLE_PERMISSIONS,
        Principal as ReExportedPrincipal,
        hash_password,
    )
    import app.employee_auth as employee_auth
    import app.identity as identity

    assert ReExportedPrincipal is identity.Principal
    assert employee_auth.Principal is identity.Principal
    assert employee_auth.ROLE_PERMISSIONS is identity.ROLE_PERMISSIONS
    assert employee_auth.ROLE_ADMIN == identity.ROLE_ADMIN
    assert callable(hash_password)


def test_employee_auth_no_longer_has_adapter_layer():
    """适配层 `_as_employee_principal` 已删除。"""
    import app.employee_auth as employee_auth

    assert not hasattr(employee_auth, "_as_employee_principal")


# --------------------------------------------------------------------------
# 6. get_request_context
# --------------------------------------------------------------------------


class _FakeState:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class _FakeRequest:
    def __init__(self, **state):
        self.state = _FakeState(**state)


def test_get_request_context_raises_domain_error_when_ctx_missing():
    """ctx 缺失 → DomainError(UNAUTHORIZED, 401)。"""
    with pytest.raises(DomainError) as excinfo:
        get_request_context(_FakeRequest(principal=None))

    assert excinfo.value.code == "UNAUTHORIZED"
    assert excinfo.value.http_status == 401


def test_get_request_context_returns_ctx_when_present():
    principal = Principal(subject="admin", tenant_id="1", roles=(ROLE_ADMIN,), auth_type="session")
    ctx = RequestContext(request_id="req-123", principal=principal)
    request = _FakeRequest(ctx=ctx, principal=principal)

    assert get_request_context(request) is ctx
    assert ctx.request_id == "req-123"
    assert ctx.principal is principal


def test_request_context_is_frozen():
    """RequestContext 是不可变值对象。"""
    ctx = RequestContext(request_id="req-1", principal=None)
    with pytest.raises(Exception):
        ctx.request_id = "req-2"  # type: ignore[misc]


# --------------------------------------------------------------------------
# 7. 中间件三个成功路径都挂 ctx
# --------------------------------------------------------------------------


@pytest.fixture
def worker_client(tmp_path, monkeypatch):
    """员工登录态客户端（沿用既有测试的构造方式）。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'r2.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IMPORTS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")

    import app.background as bg_mod
    import app.config as cfg
    import app.db as db_mod
    import app.repository as repo_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(repo_mod)
    importlib.reload(bg_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)

    from app.employee_auth import hash_password

    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="r2", name="R2 身份上下文商家")
        db.add(workspace)
        db.flush()
        user = db_mod.UserAccount(
            login="admin",
            password_hash=hash_password("admin-password"),
            display_name="测试管理员",
        )
        db.add(user)
        db.flush()
        db.add(
            db_mod.WorkspaceMembership(
                workspace_id=workspace.id, user_id=user.id, role="admin"
            )
        )
        db.commit()

    import app.main as main_mod

    importlib.reload(main_mod)

    with TestClient(main_mod.app) as client:
        login = client.post(
            "/login",
            data={"login": "admin", "password": "admin-password", "next": "/"},
            follow_redirects=False,
        )
        assert login.status_code in (302, 303), login.text
        yield client, main_mod

    bg_mod.reset_executor()
    cfg.get_settings.cache_clear()


def _ctx_of(client, main_mod, path: str):
    """通过挂载了生产中间件的测试应用读取 `request.state.ctx`。"""
    probe = FastAPI()
    probe.middleware("http")(main_mod.request_context_middleware)

    @probe.get(path)
    def _probe(request: Request):
        ctx = getattr(request.state, "ctx", None)
        principal = getattr(request.state, "principal", None)
        return {
            "has_ctx": ctx is not None,
            "request_id": getattr(request.state, "request_id", None),
            "ctx_request_id": None if ctx is None else ctx.request_id,
            "ctx_principal_subject": None if ctx is None or ctx.principal is None else ctx.principal.subject,
            "ctx_principal_type": None if ctx is None or ctx.principal is None else ctx.principal.auth_type,
            "state_principal_subject": None if principal is None else principal.subject,
        }

    return TestClient(probe)


def test_middleware_attaches_ctx_on_api_success_path(worker_client):
    """API 分支成功时 ctx 与 state.principal 一致。"""
    client, main_mod = worker_client
    probe = _ctx_of(client, main_mod, "/api/probe")
    probe.cookies.update(client.cookies)

    response = probe.get("/api/probe", headers={"X-Request-ID": "req-r2-api"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["has_ctx"] is True
    assert payload["ctx_request_id"] == payload["request_id"] == "req-r2-api"
    assert payload["ctx_principal_subject"] == payload["state_principal_subject"]


def test_middleware_attaches_ctx_on_session_success_path(worker_client):
    """session 分支成功时 ctx 与 state.principal 一致。"""
    client, main_mod = worker_client
    probe = _ctx_of(client, main_mod, "/probe")
    probe.cookies.update(client.cookies)

    response = probe.get("/probe", headers={"X-Request-ID": "req-r2-session"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["has_ctx"] is True
    assert payload["ctx_request_id"] == payload["request_id"] == "req-r2-session"
    assert payload["ctx_principal_subject"] == payload["state_principal_subject"]
    assert payload["ctx_principal_type"] == "session"


def test_middleware_attaches_ctx_on_demo_success_path(tmp_path, monkeypatch):
    """demo 分支（认证关闭）成功时 ctx 与 state.principal 一致。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'r2-demo.db'}")
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "false")

    import app.config as cfg
    import app.db as db_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)

    import app.main as main_mod

    importlib.reload(main_mod)

    probe = _ctx_of(None, main_mod, "/probe")
    response = probe.get("/probe", headers={"X-Request-ID": "req-r2-demo"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["has_ctx"] is True
    assert payload["ctx_request_id"] == payload["request_id"] == "req-r2-demo"
    assert payload["ctx_principal_type"] == "demo"
    assert payload["ctx_principal_subject"] == payload["state_principal_subject"]

    cfg.get_settings.cache_clear()
