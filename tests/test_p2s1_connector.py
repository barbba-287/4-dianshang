"""P2-1 连接器生产化用例（C-1..C-10）。

对应任务文件 `dev_task.p2s1-connector.md` §6。每条用例都可自动断言；
C-5 用 httpx 打桩保证**不发起真实网络请求**；C-10 用拼装出来的名字做全仓扫描，
保证本文件自身不会成为"死协议仍有引用"的假阳性来源。
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.platform_adapters.base import AdapterError
from app.platform_adapters.registry import (
    get_adapter,
    platform_capabilities,
    register_platform,
    registered_platforms,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
# 死协议名拆开拼装：本文件源码里不能出现该字符串的完整形态，
# 否则 C-10 的"全仓零命中"扫描会被本文件自己命中（假阳性）。
DEAD_PROTOCOL_NAME = "Platform" + "Connector"


# ---------------------------------------------------------------------------
# C-1 / C-2 / C-3：注册表本体
# ---------------------------------------------------------------------------


def test_c1_registry_builds_shopify_fixture_adapter():
    """C-1：注册表取 shopify（fixture 传输形态）→ 适配器可用。"""
    adapter = get_adapter("shopify", simulated=True)
    assert adapter.platform == "shopify"
    assert adapter.read_only is True
    # fixture 传输形态可正常调用取数方法（空 fixture → 空结果，不抛错）。
    records = adapter.fetch_inventory()
    assert isinstance(records, list)
    assert records == []
    assert "shopify" in registered_platforms()


def test_c2_unknown_platform_raises_instead_of_returning_none():
    """C-2：未注册平台必须抛错（CONNECTOR_UNKNOWN_PLATFORM），不得返回 None。"""
    with pytest.raises(AdapterError) as ei:
        get_adapter("jd")
    assert ei.value.code == "CONNECTOR_UNKNOWN_PLATFORM"


def test_c3_duplicate_registration_raises():
    """C-3：同一平台重复注册必须报错（CONNECTOR_DUPLICATE_PLATFORM）。"""
    with pytest.raises(AdapterError) as ei:
        register_platform("shopify", lambda **kwargs: None)
    assert ei.value.code == "CONNECTOR_DUPLICATE_PLATFORM"

    # 运行期注册一个新平台再注册一次，同样必须报错（Q5 裁定）。
    from app.platform_adapters import registry as registry_mod

    try:
        register_platform("p2s1_test_platform", lambda **kwargs: None)
        with pytest.raises(AdapterError) as ei2:
            register_platform("p2s1_test_platform", lambda **kwargs: None)
        assert ei2.value.code == "CONNECTOR_DUPLICATE_PLATFORM"
    finally:
        registry_mod._FACTORIES.pop("p2s1_test_platform", None)


# ---------------------------------------------------------------------------
# C-4 / C-5 / C-6：能力真实化
# ---------------------------------------------------------------------------


def test_c4_capabilities_reflect_closed_state():
    """C-4：关闭态（默认 settings）→ shopify 不放行 live；taobao 如实报骨架态。"""
    items = {item["platform"]: item for item in platform_capabilities()}
    assert items["shopify"]["live_enabled"] is False
    assert items["shopify"]["simulated"] is True
    assert items["taobao"]["live_enabled"] is False
    assert items["taobao"]["simulated"] is True
    # 六平台条目数量与 platform 键值保持不变（只改条目内状态字段）。
    assert [item["platform"] for item in platform_capabilities()] == [
        "taobao", "jd", "pdd", "douyin", "amazon", "shopify",
    ]


def test_c5_capabilities_reflect_live_switch_without_network(monkeypatch):
    """C-5：monkeypatch settings + 运行期热开关 → shopify 报 live_enabled=True。

    httpx 被打桩成"一旦被调用就失败"，用来证明能力判定**不发起真实网络请求**。
    """
    from app.config import get_settings
    from app.runtime_flags import live_enabled as runtime_live_enabled, set_live_enabled

    def _no_network(*args, **kwargs):  # pragma: no cover - 一旦被调用即失败
        raise AssertionError("能力判定不得发起网络请求")

    monkeypatch.setattr(httpx, "post", _no_network)
    monkeypatch.setenv("SHOPIFY_LIVE_ENABLED", "true")
    get_settings.cache_clear()
    previous = runtime_live_enabled()
    set_live_enabled(True)
    try:
        items = {item["platform"]: item for item in platform_capabilities()}
        assert items["shopify"]["live_enabled"] is True
        assert items["shopify"]["simulated"] is False
    finally:
        set_live_enabled(previous)
        get_settings.cache_clear()


def test_c6_unimplemented_platforms_are_marked():
    """C-6：未接适配器实现的平台显式带 implemented=False（R-37）。"""
    items = {item["platform"]: item for item in platform_capabilities()}
    for platform in ("jd", "pdd", "douyin", "amazon"):
        assert items[platform]["implemented"] is False
        assert items[platform]["live_enabled"] is False
    assert items["shopify"]["implemented"] is True
    assert items["taobao"]["implemented"] is True


# ---------------------------------------------------------------------------
# C-7 / C-8：对外契约（HTTP 端点与 CLI）
# ---------------------------------------------------------------------------


def _client(tmp_path, monkeypatch):
    """构造"员工鉴权开启"的客户端（登录前后同一套 app 与数据库）。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'p2s1.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("IMPORTS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEMO_MODE_ENABLED", "false")
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    import app.background as bg_mod
    import app.config as cfg
    import app.db as db_mod
    import app.main as main_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(bg_mod)
    importlib.reload(main_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)

    from app.employee_auth import hash_password

    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="p2s1", name="P2-1 用例空间")
        db.add(workspace)
        db.flush()
        user = db_mod.UserAccount(login="operator", password_hash=hash_password("operator-password"), display_name="运营")
        db.add(user)
        db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="operations"))
        db.commit()
    return TestClient(main_mod.app), db_mod, bg_mod


def test_c7_connectors_endpoint_end_to_end(tmp_path, monkeypatch):
    """C-7：/api/external/connectors —— 未登录 401、无权限 403、登录后与注册表一致。"""
    from app.identity import ROLE_PERMISSIONS

    client, db_mod, bg_mod = _client(tmp_path, monkeypatch)
    try:
        # 未登录：中间件层 401。
        anonymous = TestClient(client.app)
        unauthorized = anonymous.get("/api/external/connectors")
        assert unauthorized.status_code == 401, unauthorized.text
        assert unauthorized.json()["detail"]["code"] == "UNAUTHORIZED"

        # 登录：operations 角色含 inventory.read → 200。
        login = client.post("/login", data={"login": "operator", "password": "operator-password", "next": "/"}, follow_redirects=False)
        assert login.status_code == 303, login.text
        client.headers.update({"X-CSRF-Token": client.cookies.get("dianshang_csrf")})
        ok = client.get("/api/external/connectors")
        assert ok.status_code == 200, ok.text
        items = ok.json()["items"]
        assert items == platform_capabilities()

        # 无 inventory.read 权限：路由层 403。
        monkeypatch.setitem(ROLE_PERMISSIONS, "operations", frozenset({"catalog.read"}))
        forbidden = client.get("/api/external/connectors")
        assert forbidden.status_code == 403, forbidden.text
        assert forbidden.json()["detail"]["code"] == "FORBIDDEN"
    finally:
        bg_mod.reset_executor()


def test_c8_cli_capabilities_matches_registry(capsys):
    """C-8：CLI capabilities 命令的输出与注册表直查完全一致。"""
    from app.cli import cmd_external_list_connectors

    assert cmd_external_list_connectors([]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == platform_capabilities()


# ---------------------------------------------------------------------------
# C-9 / C-10：等价搬移与死协议清除
# ---------------------------------------------------------------------------


def test_c9_construction_sites_go_through_registry():
    """C-9：5 处构造点全部改经注册表，既有 preview/sync/dry-run 语义不变。

    静态部分锁"不再有直接构造"；行为部分锁"离线路径行为未变"（preview 仍返回
    空 fixture 结果且 simulated=True、不落库）。旧测试零改动全绿由全量回归证明。
    """
    source = (REPO_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "ShopifyReadOnlyAdapter(" not in source
    assert "TaobaoAdapter(" not in source
    assert source.count("get_adapter(") == 5


def test_c9_preview_path_keeps_offline_semantics(tmp_path, monkeypatch):
    """C-9（行为面）：离线路径经注册表改造后行为不变（只读、不落库、模拟态）。"""
    client, db_mod, bg_mod = _client(tmp_path, monkeypatch)
    try:
        login = client.post("/login", data={"login": "operator", "password": "operator-password", "next": "/"}, follow_redirects=False)
        assert login.status_code == 303, login.text
        client.headers.update({"X-CSRF-Token": client.cookies.get("dianshang_csrf")})
        preview = client.post("/api/external/shopify/preview", json={"resource": "orders"})
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["live_enabled"] is False
        assert body["simulated"] is True
        assert body["total"] == 0
        with db_mod.SessionLocal() as db:
            assert db.query(db_mod.ExternalOrder).count() == 0
            assert db.query(db_mod.ExternalInventorySnapshot).count() == 0
    finally:
        bg_mod.reset_executor()


def test_c10_dead_protocol_removed_everywhere():
    """C-10：死协议删除后，app/ 与 tests/ 全仓零命中，且导入无损。"""
    hits = []
    for folder in ("app", "tests"):
        for path in (REPO_ROOT / folder).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if DEAD_PROTOCOL_NAME in path.read_text(encoding="utf-8", errors="ignore"):
                hits.append(str(path))
    assert hits == []

    # 导入无损：主应用与注册表仍可正常导入。
    import app.main as main_mod
    from app.platform_adapters import registry as registry_mod

    assert registry_mod.registered_platforms() == ("shopify", "taobao")
    assert any(getattr(route, "path", "") == "/api/external/connectors" for route in main_mod.app.routes)


# ---------------------------------------------------------------------------
# 用例编号 → 测试函数名映射（交付说明按此表登记）
# ---------------------------------------------------------------------------

CASE_MAP = {
    "C-1": "test_c1_registry_builds_shopify_fixture_adapter",
    "C-2": "test_c2_unknown_platform_raises_instead_of_returning_none",
    "C-3": "test_c3_duplicate_registration_raises",
    "C-4": "test_c4_capabilities_reflect_closed_state",
    "C-5": "test_c5_capabilities_reflect_live_switch_without_network",
    "C-6": "test_c6_unimplemented_platforms_are_marked",
    "C-7": "test_c7_connectors_endpoint_end_to_end",
    "C-8": "test_c8_cli_capabilities_matches_registry",
    "C-9": "test_c9_construction_sites_go_through_registry / test_c9_preview_path_keeps_offline_semantics",
    "C-10": "test_c10_dead_protocol_removed_everywhere",
}
