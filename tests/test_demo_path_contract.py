"""demo / 未登录身份路径的契约测试（现状锁定）。

本文件固定的是 2026-10-26 的**现状**，不等于"期望行为"。已登记待拍板项：
- D-1：DEMO_MODE_ENABLED 与 EMPLOYEE_AUTH_ENABLED 并存时，前者对鉴权零影响
- D-2：**未能复现**。任务文件 `dev_task.demo-contract-tests.md` §2.2 称
  "代码中没有 `/api/` 前缀判断，未登录访问 `/api/*` 返回 303"；但 `app/main.py:311`
  存在显式的 `if request.url.path.startswith("/api/"):` 分支，未登录访问 `/api/*`
  实际返回 **401 JSON**（`{"detail": {"code": "UNAUTHORIZED", "message": "认证失败"}}`）。
  本文件按**实测现状**编写：C1/C4/C7 断言 401 JSON（而非任务文件写的 303），
  303 只出现在**页面路由**（C2）。详见 `.testtmp/delivery.md` 的偏差说明。

拍板后修改产品代码时，对应用例需同步更新并注明变更依据。
"""

import importlib

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

import app.background as bg_mod
import app.config as cfg
import app.db as db_mod
import app.main as main_mod
import app.repository as repo_mod
from app.employee_auth import require_principal
from app.errors import DomainError
from app.identity import demo_principal


# --------------------------------------------------------------------------
# 公共 fixture
# --------------------------------------------------------------------------


@pytest.fixture
def anonymous_client(tmp_path, monkeypatch):
    """employee_auth 开启、无 session 的客户端（未登录）。

    与 `tests/test_identity.py:362-417` 的 `worker_client` 同一套 reload 序列，
    但**不建 UserAccount、不登录**——未登录才有意义。
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'demo-contract.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IMPORTS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("API_AUTH_ENABLED", "false")  # 否则 authenticate_api_key 走 key 校验
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEMO_MODE_ENABLED", "true")

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(repo_mod)
    importlib.reload(bg_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)

    importlib.reload(main_mod)
    # 立即固化本 app 绑定的 Settings：后续用例若改动 env 并再次 reload(cfg)，
    # 本 app 仍应按"未登录 + employee_auth 开启"的快照行为。
    cfg.get_settings()

    with TestClient(main_mod.app) as client:
        yield client

    bg_mod.reset_executor()
    cfg.get_settings.cache_clear()


@pytest.fixture
def demo_mode_client(tmp_path, monkeypatch):
    """employee_auth 关闭、demo_mode 关闭的客户端（demo 身份可过中间件）。

    该组合下中间件放行 demo principal，请求会真正抵达路由，
    因此可观测到**路由级**第二道门（`require_principal`）的判定。
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'demo-mode-contract.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IMPORTS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "false")
    monkeypatch.setenv("DEMO_MODE_ENABLED", "false")

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(repo_mod)
    importlib.reload(bg_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)

    importlib.reload(main_mod)
    cfg.get_settings()

    with TestClient(main_mod.app) as client:
        yield client

    bg_mod.reset_executor()
    cfg.get_settings.cache_clear()


def _make_request(path: str = "/x") -> Request:
    """构造一个最小可用的 Starlette Request（`require_principal` 只读 state）。"""
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 12345),
            "root_path": "",
            "path": path,
            "query_string": b"",
            "headers": [],
            "state": {},
        }
    )


# --------------------------------------------------------------------------
# C1：未登录访问 /api/* —— 实测为 401 JSON（任务文件写的 303 未能复现）
# --------------------------------------------------------------------------


def test_unauthenticated_api_request_returns_401_json(anonymous_client):
    """未登录访问业务 API：401 JSON，不重定向。"""
    r = anonymous_client.get("/api/inventory", follow_redirects=False)

    assert r.status_code == 401
    assert r.headers.get("location") is None
    assert r.json()["detail"]["code"] == "UNAUTHORIZED"


# --------------------------------------------------------------------------
# C2：未登录访问页面路由 —— 303 → /login?next=
# --------------------------------------------------------------------------


def test_unauthenticated_page_request_redirects_to_login(anonymous_client):
    """未登录访问页面路由：303 重定向到登录页。"""
    r = anonymous_client.get("/dashboard", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/dashboard"


# --------------------------------------------------------------------------
# C3：公开端点不经过登录门
# --------------------------------------------------------------------------


def test_public_paths_bypass_login_gate(anonymous_client):
    """`/health`、`/ready` 在 public_paths 内，未登录不被拦。"""
    health = anonymous_client.get("/health", follow_redirects=False)
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    ready = anonymous_client.get("/ready", follow_redirects=False)
    assert ready.status_code != 303  # 关键：未被登录门拦截
    assert ready.status_code in (200, 503)  # 503 = 真实就绪状态，不是鉴权拦截


# --------------------------------------------------------------------------
# C4：live 入口匿名不可达（中间件层 + 路由层两道门）
# --------------------------------------------------------------------------


def test_live_capabilities_not_reachable_anonymously(anonymous_client, demo_mode_client):
    """Shopify live 入口：匿名被中间件 401 拦下；demo 身份被路由守卫 401 拦下。"""
    r = anonymous_client.get("/api/external/shopify/capabilities", follow_redirects=False)
    assert r.status_code == 401
    assert r.headers.get("location") is None
    assert r.json()["detail"]["code"] == "UNAUTHORIZED"

    # 第二道门：demo 身份能过中间件，但 require_principal 仍拒绝。
    r2 = demo_mode_client.get("/api/external/shopify/capabilities", follow_redirects=False)
    assert r2.status_code == 401
    assert r2.json()["detail"]["code"] == "UNAUTHORIZED"


# --------------------------------------------------------------------------
# C5：require_principal 拒绝 demo 身份（employee_auth 开启时）
# --------------------------------------------------------------------------


def test_require_principal_rejects_demo_identity_when_employee_auth_enabled(monkeypatch):
    """第二道门：employee_auth 开启时，demo 身份一律 UNAUTHORIZED/401。"""
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEMO_MODE_ENABLED", "true")
    cfg.get_settings.cache_clear()
    settings = cfg.get_settings()

    req = _make_request()
    req.state.principal = demo_principal(settings)

    with pytest.raises(DomainError) as ei:
        require_principal(req)

    assert ei.value.code == "UNAUTHORIZED"
    assert ei.value.http_status == 401


# --------------------------------------------------------------------------
# C6：require_principal 放行 demo 身份（employee_auth 关闭且 demo_mode 开启时）
# --------------------------------------------------------------------------


def test_require_principal_accepts_demo_identity_when_employee_auth_disabled(monkeypatch):
    """第一道门的放行条件：demo_mode_enabled and not employee_auth_enabled。"""
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "false")
    monkeypatch.setenv("DEMO_MODE_ENABLED", "true")
    cfg.get_settings.cache_clear()

    principal = demo_principal(cfg.get_settings())
    req = _make_request()
    req.state.principal = principal

    assert require_principal(req) is principal

    # 同一身份，demo_mode 关闭 → 拒绝（锁住 :157 的 demo_mode_enabled 条件）
    monkeypatch.setenv("DEMO_MODE_ENABLED", "false")
    cfg.get_settings.cache_clear()
    req2 = _make_request()
    req2.state.principal = demo_principal(cfg.get_settings())

    with pytest.raises(DomainError) as ei:
        require_principal(req2)

    assert ei.value.code == "UNAUTHORIZED"
    assert ei.value.http_status == 401


# --------------------------------------------------------------------------
# C7：DEMO_MODE_ENABLED 不授予任何 API 访问权（D-1 语义）
# --------------------------------------------------------------------------


def test_demo_mode_flag_does_not_grant_api_access(anonymous_client):
    """D-1：employee_auth 开启时，demo_mode 开关对鉴权零影响。"""
    settings = cfg.get_settings()
    assert settings.demo_mode_enabled is True
    assert settings.employee_auth_enabled is True

    api = anonymous_client.get("/api/inventory", follow_redirects=False)
    assert api.status_code == 401
    assert api.headers.get("location") is None

    page = anonymous_client.get("/dashboard", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/login?next=/dashboard"


# --------------------------------------------------------------------------
# C8：demo_mode 的唯一实际效果是放行 seed-demo CLI
# --------------------------------------------------------------------------


def test_seed_demo_is_the_only_effect_of_demo_mode(monkeypatch):
    """seed-demo 的两个前置检查：demo_mode 开关 + demo- 前缀。"""
    from app.cli import cmd_seed_demo

    # (a) demo_mode 关闭 → 拒绝，返回 2
    monkeypatch.setenv("DEMO_MODE_ENABLED", "false")
    cfg.get_settings.cache_clear()
    assert cmd_seed_demo(["--workspace", "demo-x"]) == 2

    # (b) demo_mode 开启但 workspace 无 demo- 前缀 → 拒绝，返回 2
    monkeypatch.setenv("DEMO_MODE_ENABLED", "true")
    cfg.get_settings.cache_clear()
    assert cmd_seed_demo(["--workspace", "prod-shop"]) == 2
