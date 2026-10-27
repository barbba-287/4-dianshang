"""demo / 未登录身份路径的契约测试（规格锁定）。

本文件锁定的是**规格**，而非对实现偶然行为的记录。规格来源：

- **D-2 裁定（2026-10-27 用户裁定，P2-0b 项 1）**：未登录访问 `/api/*` 返回 **401 JSON**、
  页面路由返回 **303**，此即规格语义。裁定依据：API 语义应 401 而非 302/303（浏览器跳转语义
  只适用页面路由），API 客户端才能解析到稳定错误码 `UNAUTHORIZED` 而非 HTML 重定向。
  正式裁定全文见 `docs/demo-path-audit-20261026.md` §5。
- 中间件实现依据 `app/main.py:316` 的 `if request.url.path.startswith("/api/"):` 分支：
  API 分支产出 401 JSON，`:341` `else:` 页面分支产出 303。

据此：C1/C4/C7 断言 401 JSON，C2 断言页面 303，C9 锁定 `/api/` 前缀的精确边界
（`/apifoo` 无尾斜杠 → 走页面分支 303，不被 401 分支误伤）。

其余未裁定项（**不在本文件的裁定范围内**）：
- D-1：`DEMO_MODE_ENABLED` 与 `EMPLOYEE_AUTH_ENABLED` 并存时，前者对鉴权零影响
  （见 `docs/demo-path-audit-20261026.md` §8，尚未裁定；本文件仅按当前规格断言，不预判结论）

本文件不锁定实现细节以外的行为；产品规格变更须先更新裁定文档，再同步对应用例并注明依据。
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
# C1：未登录访问 /api/* —— 规格为 401 JSON（2026-10-27 D-2 裁定）
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


# --------------------------------------------------------------------------
# C9：`/api/` 前缀的精确边界 —— `/apifoo` 不进 401 分支
# --------------------------------------------------------------------------


def test_api_prefix_requires_trailing_slash(anonymous_client):
    """`/apifoo`（无尾斜杠）按页面路由处理：303，不是 401。

    中间件的 API 分支条件是 `startswith("/api/")`（含尾斜杠），因此 `/apifoo`
    不被 401 分支误伤，落入页面分支 → 303 → `/login?next=/apifoo`。
    2026-10-27 实测确认（`.testtmp/probe_apifoo_test.py`），与 D-2 裁定一致。
    """
    r = anonymous_client.get("/apifoo", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/apifoo"
