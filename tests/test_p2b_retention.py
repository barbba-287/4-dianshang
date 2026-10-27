"""P2-0b 项 4b：Shopify live 响应原样留存的测试（设计文档 §2.8 五例 + CLI 读取）。

用例编号与 `docs/p2-retention-design-20261027.md` §2.8 一一对应：

| 编号 | 设计要求 | 测试函数 |
|---|---|---|
| §2.8-1 | live sync 成功 → 留存目录的正文文件数 = 实际请求数，正文与 fake 响应字节级一致 | `test_s28_1_live_sync_retains_one_body_file_per_request_with_identical_bytes` |
| §2.8-2 | 台账引用正确：`retention.status=ok`、`files` 计数、`dir` 指向存在目录 | `test_s28_2_ledger_retention_reference_is_correct` |
| §2.8-3 | 留存写失败 → run 仍成功、台账 `status=failed`、日志有 warning | `test_s28_3_retention_write_failure_degrades_to_warning_and_run_still_succeeds` |
| §2.8-4 | fixture 路径零留存（不产生留存目录） | `test_s28_4_fixture_path_creates_zero_retention_files` |
| §2.8-5 | 超限 → `.json.truncated`、字节数=上限、内容=原始前缀、meta 正确、台账 truncated；凭证断言覆盖正常文件与截断文件 | `test_s28_5_over_limit_writes_truncated_file_and_ledger_marks_truncated` |
| §2.7 | CLI 读取：`retention list` / `retention get`（截断拒绝解析且退出码非 0） | `test_s27_cli_list_and_get_read_retention`、`test_s27_cli_get_refuses_truncated_response` |

说明（与实现的一致性记录）：

- 本文件不改动任何既有测试文件与产品代码（除新增 `app/cli.py` 的 `retention` 子命令），
  只新增本文件；
- 不触碰真实 Shopify API：`app.platform_adapters.shopify` 的 `httpx` 被替换为只含假
  `post` 的 shim（保留 `TimeoutException` / `NetworkError`），假响应带
  `.text` / `.status_code` / `.json()`；
- 走真实 `ShopifyLiveTransport`（真实留存实现），不走 fixture transport 的 live 分支；
- 凭证断言用足够长的唯一 fake token 字符串（真实 token 只在请求头里），
  正常文件与 `.json.truncated` 文件两路都断言"全文不含 token"。
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import app.config as cfg


# 足够长且唯一的假 token：断言"留存文件全文不含它"时不会误判。
FAKE_TOKEN = "shpat_FAKE_0123456789abcdefGHIJKLMNOP_fake_token_value"


# ---------------------------------------------------------------------------
# 假 HTTP 层
# ---------------------------------------------------------------------------


class FakeResponse:
    """最小可用的 httpx.Response 替身。"""

    def __init__(self, payload: dict):
        self._payload = payload
        # 留存写入的是 response.text 的字节，必须与 json() 同源。
        self.text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.status_code = 200
        self.headers: dict[str, str] = {}

    def json(self) -> dict:
        return self._payload


class HttpxShim:
    """替换 `app.platform_adapters.shopify.httpx`，只保留被用到的属性。"""

    def __init__(self, post):
        self.post = post
        self.TimeoutException = httpx.TimeoutException
        self.NetworkError = httpx.NetworkError


ORDER_NODE = {
    "id": "gid://shopify/Order/3001",
    "name": "#3001",
    "createdAt": "2026-10-01T10:00:00Z",
    "updatedAt": "2026-10-02T10:00:00Z",
    "processedAt": "2026-10-01T10:05:00Z",
    "displayFinancialStatus": "PAID",
    "displayFulfillmentStatus": "FULLED",
    "currentTotalPriceSet": {"shopMoney": {"amount": "99.00", "currencyCode": "USD"}},
    "lineItems": {
        "nodes": [
            {
                "id": "line-3001",
                "sku": "SKU-3001",
                "quantity": 2,
                "originalTotalSet": {"shopMoney": {"amount": "99.00", "currencyCode": "USD"}},
                "variant": {"id": "variant-3001", "title": "高山绿茶"},
            }
        ]
    },
}

INVENTORY_NODE = {
    "id": "gid://shopify/InventoryItem/4001",
    "sku": "SKU-3001",
    "inventoryLevels": {
        "edges": [
            {
                "node": {
                    "id": "level-4001",
                    "quantities": [{"name": "available", "quantity": 7}],
                    "location": {"id": "location-1"},
                }
            }
        ]
    },
}


def _orders_payload() -> dict:
    return {
        "data": {
            "orders": {
                "edges": [{"cursor": "orders-0", "node": ORDER_NODE}],
                "pageInfo": {"hasNextPage": False, "endCursor": "orders-0"},
            }
        }
    }


def _inventory_payload() -> dict:
    return {
        "data": {
            "inventoryItems": {
                "edges": [{"cursor": "inventory-0", "node": INVENTORY_NODE}],
                "pageInfo": {"hasNextPage": False, "endCursor": "inventory-0"},
            }
        }
    }


def _install_fake_post(monkeypatch) -> list[dict]:
    """把 shopify 模块的 httpx 换成假 post；返回记录到的调用列表。"""
    import app.platform_adapters.shopify as shopify_mod

    calls: list[dict] = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "body": json, "headers": headers or {}})
        query = (json or {}).get("query", "")
        payload = _inventory_payload() if "inventoryItems(" in query else _orders_payload()
        return FakeResponse(payload)

    monkeypatch.setattr(shopify_mod, "httpx", HttpxShim(fake_post))
    return calls


# ---------------------------------------------------------------------------
# sync 路由 harness（与 tests/test_p2a_sync_guardrails.py 同构，但不改那个文件）
# ---------------------------------------------------------------------------


def _login(client: TestClient, login: str, password: str = "pw") -> TestClient:
    response = client.post("/login", data={"login": login, "password": password, "next": "/"}, follow_redirects=False)
    assert response.status_code in (302, 303), response.text
    csrf = client.cookies.get("dianshang_csrf")
    assert csrf, "缺少 CSRF cookie"
    client.headers.update({"X-CSRF-Token": csrf})
    return client


class Harness:
    def __init__(self, client: TestClient, main: Any, db_mod: Any, artifacts_dir: Path):
        self.client = client
        self.main = main
        self.db_mod = db_mod
        self.artifacts_dir = artifacts_dir


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """已登录 admin 的 Shopify live 同步环境（真实 live transport + 假 HTTP 层）。"""
    artifacts_dir = tmp_path / "artifacts"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'p2b.db'}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(artifacts_dir))
    monkeypatch.setenv("IMPORTS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("SHOPIFY_LIVE_ENABLED", "true")
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "probe.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("SHOPIFY_SYNC_BUDGET_SECONDS", "300")
    # 默认上限固定，避免仓库外 .env 漂移。
    monkeypatch.setenv("SHOPIFY_RETENTION_MAX_BYTES", "10485760")

    import app.background as bg_mod
    import app.db as db_mod
    import app.repository as repo_mod

    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(repo_mod)
    importlib.reload(bg_mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    bg_mod.reset_executor()

    import app.runtime_flags as runtime_flags

    runtime_flags.set_live_enabled(True)

    from app.employee_auth import hash_password

    with db_mod.SessionLocal() as db:
        workspace = db_mod.Workspace(tenant_key="p2b", name="P2-0b 留存商家")
        db.add(workspace)
        db.flush()
        admin = db_mod.UserAccount(login="admin", password_hash=hash_password("pw"), display_name="留存管理员")
        db.add(admin)
        db.flush()
        db.add(db_mod.WorkspaceMembership(workspace_id=workspace.id, user_id=admin.id, role="admin"))
        db.commit()

    import app.main as main_mod

    importlib.reload(main_mod)

    client = TestClient(main_mod.app)
    _login(client, "admin")

    yield Harness(client=client, main=main_mod, db_mod=db_mod, artifacts_dir=artifacts_dir)

    runtime_flags.set_live_enabled(True)
    bg_mod.reset_executor()
    cfg.get_settings.cache_clear()


def _run_row(db_mod, run_id: str) -> dict[str, Any]:
    with db_mod.SessionLocal() as db:
        run = db.query(db_mod.ExternalSyncRun).filter(db_mod.ExternalSyncRun.run_id == run_id).one()
        return {
            "run_id": run.run_id,
            "status": run.status,
            "error_code": run.error_code,
            "resource_status": json.loads(run.resource_status_json or "{}"),
        }


def _retention_dir(artifacts_dir: Path, run_id: str) -> Path:
    return artifacts_dir / "shopify_sync" / run_id


def _body_files(directory: Path) -> list[Path]:
    """正文文件（不含 `.meta.json` 元数据 sidecar）。"""
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.name.endswith(".json") and not path.name.endswith(".meta.json")
    )


def _meta_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.iterdir() if path.is_file() and path.name.endswith(".meta.json"))


def _truncated_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.iterdir() if path.is_file() and path.name.endswith(".json.truncated"))


# ---------------------------------------------------------------------------
# §2.8-1  live sync 成功 → 每个请求一份留存正文，字节级一致
# ---------------------------------------------------------------------------


def test_s28_1_live_sync_retains_one_body_file_per_request_with_identical_bytes(harness, monkeypatch):
    """§2.8-1：走 live transport 的 sync 成功后，每个 HTTP 请求对应一份留存正文，字节与响应一致。"""
    import app.platform_adapters.shopify as shopify_mod

    calls = _install_fake_post(monkeypatch)

    response = harness.client.post("/api/external/shopify/sync?resources=orders,inventory")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded", body

    # 一次 orders + 一次 inventory = 2 个 HTTP 请求。
    assert len(calls) == 2, calls

    directory = _retention_dir(harness.artifacts_dir, body["run_id"])
    assert directory.is_dir(), directory

    body_files = _body_files(directory)
    meta_files = _meta_files(directory)
    # 正文文件数 = 请求数；元数据 sidecar 每个请求一份（设计 §2.2 的目录布局）。
    assert len(body_files) == len(calls) == 2, [p.name for p in body_files]
    assert len(meta_files) == len(calls) == 2, [p.name for p in meta_files]
    assert _truncated_files(directory) == []

    # 每个留存正文与 fake 响应的 response.text 字节级一致。
    expected_by_request = {}
    for call in calls:
        request_id = call["headers"]["X-Request-ID"]
        query = call["body"]["query"]
        payload = _inventory_payload() if "inventoryItems(" in query else _orders_payload()
        expected_by_request[request_id] = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")

    for path in body_files:
        request_id = path.name[: -len(".json")]
        assert request_id in expected_by_request, path.name
        assert path.read_bytes() == expected_by_request[request_id], path.name

    # meta 记录与请求一一对应。
    meta_request_ids = set()
    for path in meta_files:
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta_request_ids.add(meta["request_id"])
        assert meta["http_status"] == 200
        assert meta["truncated"] is False
        assert meta["stored_bytes"] == meta["original_size"]
    assert meta_request_ids == set(expected_by_request)

    # 凭证零落盘（§2.4.1-2，正常文件一路）。
    for path in body_files + meta_files:
        assert FAKE_TOKEN.encode("utf-8") not in path.read_bytes(), path.name


# ---------------------------------------------------------------------------
# §2.8-2  台账引用正确
# ---------------------------------------------------------------------------


def test_s28_2_ledger_retention_reference_is_correct(harness, monkeypatch):
    """§2.8-2：台账 resource_status_json 的 retention 子对象：status=ok、files 计数、dir 指向存在目录。"""
    _install_fake_post(monkeypatch)

    response = harness.client.post("/api/external/shopify/sync?resources=orders,inventory")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded", body

    run = _run_row(harness.db_mod, body["run_id"])
    assert run["status"] == "succeeded"
    directory = _retention_dir(harness.artifacts_dir, body["run_id"])

    for resource in ("orders", "inventory"):
        entry = run["resource_status"][resource]
        assert entry["status"] == "succeeded", entry
        retention = entry["retention"]
        assert retention["status"] == "ok", retention
        # 每个资源各 1 页 = 1 份留存正文。
        assert retention["files"] == 1, retention
        assert Path(retention["dir"]).is_dir(), retention
        assert Path(retention["dir"]) == directory, retention
        assert Path(retention["dir"]).name == body["run_id"]


# ---------------------------------------------------------------------------
# §2.8-3  留存写失败 → 降级：run 仍成功 + 台账 failed + warning
# ---------------------------------------------------------------------------


def test_s28_3_retention_write_failure_degrades_to_warning_and_run_still_succeeds(harness, monkeypatch, caplog):
    """§2.8-3：留存写盘失败只降级为 warning 与台账 failed，同步本身照常成功。"""
    import app.platform_adapters.shopify as shopify_mod

    _install_fake_post(monkeypatch)

    def boom_open(path, mode="r", *args, **kwargs):
        raise OSError("simulated retention write failure")

    monkeypatch.setattr(shopify_mod, "open", boom_open, raising=False)

    with caplog.at_level(logging.WARNING, logger="app.platform_adapters.shopify"):
        response = harness.client.post("/api/external/shopify/sync?resources=orders")
    assert response.status_code == 200, response.text
    body = response.json()

    # 留存失败不打断业务主链路。
    assert body["status"] == "succeeded", body
    assert body["resources"]["orders"]["inserted"] == 1, body

    run = _run_row(harness.db_mod, body["run_id"])
    assert run["status"] == "succeeded"
    assert run["resource_status"]["orders"]["status"] == "succeeded"
    retention = run["resource_status"]["orders"]["retention"]
    assert retention["status"] == "failed", retention
    assert retention["files"] == 0, retention

    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert warnings, "留存写失败没有产生 warning"
    assert any("留存" in record.getMessage() for record in warnings), [record.getMessage() for record in warnings]


# ---------------------------------------------------------------------------
# §2.8-4  fixture 路径零留存
# ---------------------------------------------------------------------------


def test_s28_4_fixture_path_creates_zero_retention_files(harness, monkeypatch):
    """§2.8-4：fixture（simulated）链路不产生任何留存文件。"""
    from app.platform_adapters.shopify import ShopifyFixtureTransport, ShopifyReadOnlyAdapter

    responses = {
        "orders": {
            "data": {
                "orders": {
                    "edges": [{"cursor": "c1", "node": ORDER_NODE}],
                    "pageInfo": {"hasNextPage": False, "endCursor": "c1"},
                }
            }
        }
    }
    adapter = ShopifyReadOnlyAdapter(ShopifyFixtureTransport(responses), simulated=True)
    records = adapter.fetch_orders()
    assert len(records) == 1

    # 路由层的 preview（live=false）同样走 fixture 链路。
    preview = harness.client.post("/api/external/shopify/preview", json={"resource": "orders", "live": False})
    assert preview.status_code == 200, preview.text
    assert preview.json()["simulated"] is True

    # 零留存：连 shopify_sync 目录都不应存在。
    assert not (harness.artifacts_dir / "shopify_sync").exists()
    assert list(harness.artifacts_dir.rglob("*")) == []


# ---------------------------------------------------------------------------
# §2.8-5  超限 → .json.truncated + meta 正确 + 台账 truncated + 凭证断言
# ---------------------------------------------------------------------------


def test_s28_5_over_limit_writes_truncated_file_and_ledger_marks_truncated(harness, monkeypatch):
    """§2.8-5：响应超过上限时只写前 N 字节并改名为 .json.truncated，meta 与台账标记 truncated。"""
    limit = 64
    monkeypatch.setenv("SHOPIFY_RETENTION_MAX_BYTES", str(limit))
    cfg.get_settings.cache_clear()

    _install_fake_post(monkeypatch)

    response = harness.client.post("/api/external/shopify/sync?resources=orders")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded", body

    directory = _retention_dir(harness.artifacts_dir, body["run_id"])
    assert directory.is_dir(), directory

    # 不写 .json 后缀（避免被误解析），只写 .json.truncated。
    assert _body_files(directory) == [], [p.name for p in directory.iterdir()]
    truncated = _truncated_files(directory)
    assert len(truncated) == 1, [p.name for p in directory.iterdir()]
    truncated_path = truncated[0]
    request_id = truncated_path.name[: -len(".json.truncated")]

    original = json.dumps(_orders_payload(), ensure_ascii=False, sort_keys=True).encode("utf-8")
    assert len(original) > limit, "构造的响应必须超过上限，否则测不到截断分支"
    # 字节数 == 上限，内容 == 原始前缀（不含任何截断标记字节）。
    assert truncated_path.read_bytes() == original[:limit]
    assert truncated_path.stat().st_size == limit

    meta_files = _meta_files(directory)
    assert len(meta_files) == 1, [p.name for p in directory.iterdir()]
    meta = json.loads(meta_files[0].read_text(encoding="utf-8"))
    assert meta["request_id"] == request_id
    assert meta["truncated"] is True
    assert meta["stored_bytes"] == limit
    assert meta["original_size"] == len(original)

    # 台账标记 truncated。
    run = _run_row(harness.db_mod, body["run_id"])
    assert run["status"] == "succeeded"
    retention = run["resource_status"]["orders"]["retention"]
    assert retention["status"] == "truncated", retention
    assert retention["files"] == 1, retention

    # 凭证断言覆盖截断场景（§2.4.1-2 / 实现约束 2）：截断文件与 meta 全文都不含 token。
    assert FAKE_TOKEN.encode("utf-8") not in truncated_path.read_bytes()
    assert FAKE_TOKEN.encode("utf-8") not in meta_files[0].read_bytes()


# ---------------------------------------------------------------------------
# §2.7  CLI 读取
# ---------------------------------------------------------------------------


def _write_retention_fixture(artifacts_dir: Path, run_id: str, request_id: str, *, body: bytes, truncated: bool, stored: int, original: int) -> Path:
    directory = artifacts_dir / "shopify_sync" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    body_name = f"{request_id}.json.truncated" if truncated else f"{request_id}.json"
    (directory / body_name).write_bytes(body)
    meta = {
        "request_id": request_id,
        "attempt": 1,
        "http_status": 200,
        "elapsed_ms": 12,
        "query": "query Orders { orders { edges { node { id } } } }",
        "variables": {"first": 50, "after": None},
        "captured_at": "2026-10-27T00:00:00+00:00",
        "truncated": truncated,
        "stored_bytes": stored,
        "original_size": original,
        "resource": "orders",
    }
    (directory / f"{request_id}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    return directory


def test_s27_cli_list_and_get_read_retention(tmp_path, monkeypatch, capsys):
    """§2.7：CLI `retention list` 列出文件与 meta 摘要；`retention get` 输出正文。"""
    import app.cli as cli_mod

    artifacts_dir = tmp_path / "artifacts"
    request_id = "shopify-req-0001"
    payload = json.dumps({"data": {"orders": {"edges": []}}}, ensure_ascii=False).encode("utf-8")
    _write_retention_fixture(
        artifacts_dir,
        run_id="run-cli-1",
        request_id=request_id,
        body=payload,
        truncated=False,
        stored=len(payload),
        original=len(payload),
    )

    monkeypatch.setenv("ARTIFACTS_DIR", str(artifacts_dir))
    cli_mod.get_settings.cache_clear()

    assert cli_mod.main(["retention", "list", "--run-id", "run-cli-1"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["ok"] is True
    assert listed["run_id"] == "run-cli-1"
    assert listed["summary"] == {"requests": 1, "body_files": 1, "truncated": 0}
    item = listed["items"][0]
    assert item["request_id"] == request_id
    assert item["body_file"] == f"{request_id}.json"
    assert item["truncated"] is False
    assert item["http_status"] == 200
    assert item["body_bytes"] == len(payload)

    assert cli_mod.main(["retention", "get", "--run-id", "run-cli-1", "--request-id", request_id]) == 0
    assert capsys.readouterr().out.strip() == payload.decode("utf-8")

    # 目录不存在 → 非 0 退出码 + 稳定错误码。
    assert cli_mod.main(["retention", "list", "--run-id", "run-missing"]) == 2
    missing = json.loads(capsys.readouterr().out)
    assert missing["ok"] is False
    assert missing["error_code"] == "RETENTION_RUN_NOT_FOUND"

    cli_mod.get_settings.cache_clear()


def test_s27_cli_get_refuses_truncated_response(tmp_path, monkeypatch, capsys):
    """§2.7：meta 标记 truncated 时，CLI 拒绝解析正文、明示截断并返回非 0 退出码。"""
    import app.cli as cli_mod

    artifacts_dir = tmp_path / "artifacts"
    request_id = "shopify-req-0002"
    original = b"x" * 500
    _write_retention_fixture(
        artifacts_dir,
        run_id="run-cli-2",
        request_id=request_id,
        body=original[:64],
        truncated=True,
        stored=64,
        original=len(original),
    )

    monkeypatch.setenv("ARTIFACTS_DIR", str(artifacts_dir))
    cli_mod.get_settings.cache_clear()

    exit_code = cli_mod.main(["retention", "get", "--run-id", "run-cli-2", "--request-id", request_id])
    out = capsys.readouterr().out
    assert exit_code != 0
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error_code"] == "RETENTION_TRUNCATED"
    assert payload["truncated"] is True
    assert "截断" in payload["error"] and "非完整响应" in payload["error"]
    # 截断正文不得被当作完整响应输出。
    assert "xxxx" not in out

    # list 侧仍能看出截断状态。
    assert cli_mod.main(["retention", "list", "--run-id", "run-cli-2"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["summary"] == {"requests": 1, "body_files": 1, "truncated": 1}
    assert listed["items"][0]["truncated"] is True

    cli_mod.get_settings.cache_clear()
