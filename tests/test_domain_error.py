"""R1 错误体系统一回归测试。

覆盖三件事：

1. **正向**：`DomainError` 经 `app/main.py` 注册的唯一 handler 映射为
   `{"detail": {"code", "message"}}`，状态码取 `http_status`（默认 400）。
2. **负向**：中间件 CSRF 分支在异常类型迁移后仍返回 403 + `CSRF_INVALID`。
3. **故障**：未注册 handler 的任意异常仍由中间件兜底为
   500 + `{"detail": {"code": "INTERNAL_ERROR", "message": "服务内部错误"}}`，
   且 `DomainError` 在路由层被 handler 拦截、不会穿透到中间件兜底。
"""

import importlib
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors import DomainError


def _production_handler():
    """取 `app/main.py` 注册的 DomainError handler（生产实现，非测试副本）。"""
    import app.main as main_mod

    return main_mod.domain_error_handler


def test_domain_error_response_shape_and_status():
    """正向：状态码 + 错误体外形逐字固定。"""
    app = FastAPI()
    app.add_exception_handler(DomainError, _production_handler())

    @app.get("/missing")
    def missing():
        raise DomainError(code="X_NOT_FOUND", message="msg", http_status=404)

    client = TestClient(app)
    response = client.get("/missing")

    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "X_NOT_FOUND", "message": "msg"}}


def test_domain_error_default_status_is_400():
    """正向：`http_status` 缺省时为 400。"""
    app = FastAPI()
    app.add_exception_handler(DomainError, _production_handler())

    @app.get("/invalid")
    def invalid():
        raise DomainError(code="X_BAD_REQUEST", message="默认状态码")

    client = TestClient(app)
    response = client.get("/invalid")

    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "X_BAD_REQUEST", "message": "默认状态码"}}


def test_errors_module_does_not_import_fastapi():
    """`app/errors.py` 不得依赖 fastapi（领域层与 Web 框架解耦）。"""
    import app.errors

    source = Path(app.errors.__file__).read_text(encoding="utf-8")
    assert re.search(r"^\s*(from|import)\s+fastapi", source, re.M) is None


@pytest.fixture
def employee_client(tmp_path, monkeypatch):
    """带员工登录态的真实应用客户端（沿用 tests/test_admin_users.py 的构造方式）。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'r1.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IMPORTS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")

    import app.background as bg_mod
    import app.config as cfg
    import app.db as db_mod
    import app.main as main_mod
    import app.repository as repo_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(repo_mod)
    importlib.reload(bg_mod)
    importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)

    from app.employee_auth import hash_password

    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="r1-domain-error", name="R1 错误体测试商家")
        db.add(workspace)
        db.flush()
        db.add(
            db_mod.UserAccount(
                login="admin",
                password_hash=hash_password("admin-password"),
                display_name="测试管理员",
            )
        )
        db.flush()
        db.add(
            db_mod.WorkspaceMembership(
                workspace_id=workspace.id, user_id=1, role="admin"
            )
        )
        db.commit()

    with TestClient(main_mod.app) as test_client:
        login = test_client.post(
            "/login",
            data={"login": "admin", "password": "admin-password", "next": "/admin/users"},
            follow_redirects=False,
        )
        assert login.status_code == 303, login.text
        yield test_client

    bg_mod.reset_executor()
    cfg.get_settings.cache_clear()


def test_csrf_failure_path_still_returns_403(employee_client):
    """负向：缺 CSRF token 的写请求仍被中间件拦为 403 + CSRF_INVALID。"""
    response = employee_client.post(
        "/api/admin/users",
        json={
            "login": "blocked",
            "display_name": "应被拦截",
            "password": "blocked-password",
            "role": "readonly",
            "warehouse_ids": [],
        },
    )

    assert response.status_code == 403
    assert response.json() == {"detail": {"code": "CSRF_INVALID", "message": "CSRF 校验失败"}}


def test_unregistered_exception_falls_back_to_internal_error(monkeypatch):
    """故障：未注册 handler 的异常仍由中间件兜底为 500 INTERNAL_ERROR。"""
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "false")
    monkeypatch.setenv("API_AUTH_ENABLED", "false")

    import app.config as cfg
    import app.main as main_mod

    cfg.get_settings.cache_clear()
    main_mod.get_settings.cache_clear()

    test_app = FastAPI()
    test_app.middleware("http")(main_mod.request_context_middleware)
    test_app.add_exception_handler(DomainError, main_mod.domain_error_handler)

    @test_app.get("/boom-runtime")
    def boom_runtime():
        raise RuntimeError("boom")

    @test_app.get("/boom-domain")
    def boom_domain():
        raise DomainError(code="BOOM_DOMAIN", message="领域错误", http_status=409)

    client = TestClient(test_app)

    unhandled = client.get("/boom-runtime")
    assert unhandled.status_code == 500
    assert unhandled.json() == {"detail": {"code": "INTERNAL_ERROR", "message": "服务内部错误"}}

    handled = client.get("/boom-domain")
    assert handled.status_code == 409
    assert handled.json() == {"detail": {"code": "BOOM_DOMAIN", "message": "领域错误"}}
