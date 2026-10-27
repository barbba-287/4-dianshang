"""S4 单飞闸门（live 同步并发互斥）回归测试。

用例编号与 `dev_task.s4-concurrency.md` §4 一一对应：

| 编号 | 场景 | 测试函数 |
|---|---|---|
| S4-1  | 第一个 run 运行中，第二个 sync 请求进来 | `test_s4_1_second_request_rejected_while_first_running` |
| S4-2  | 第一个 run 成功结束后再请求 | `test_s4_2_sequential_runs_after_success` |
| S4-3a | run 因预算超限终止后再请求 | `test_s4_3a_gate_released_after_budget_exceeded` |
| S4-3b | run 因取消终止后再请求 | `test_s4_3b_gate_released_after_cancelled` |
| S4-4  | 并发同一 run 的 cancel | `test_s4_4_cancel_not_blocked_by_gate` |

设计说明（与实现的一致性记录）：

- 复用 `tests/test_p2a_sync_guardrails.py` 的 `harness` fixture、
  `FakeShopifyTransport`、`_install_fake_transport`、`_run_row`（p2a 的 harness
  已覆盖 admin 登录 / live 配置 / 建表 / 跳过 lifespan），本文件不重复实现；
- 并发用例**不使用 `time.sleep` 赌时序**（`dev_task.s4-concurrency.md` §4.1）：
  唯一同步手段是 `threading.Event`，所有 `wait()` 带超时，超时即 fail；
- 每个用例结束前断言 `main_mod._sync_gate_lock.locked() is False`
  —— 锁泄漏的直接探针，比"第二个请求成功"更早暴露问题；
- 后台线程一律 `daemon=True`，并在 `finally` 里 `release.set()` + `join(timeout=10)`，
  避免异常时把后续用例拖死。
"""

import threading
from typing import Any

import pytest

import app.config as cfg

# 与 p2a 同源：harness / FakeShopifyTransport / _install_fake_transport / _run_row
# 直接复用，改动（如需）必须同步检查两边的一致性。
from tests.test_p2a_sync_guardrails import (  # noqa: F401 - harness 作为 fixture 注入
    FakeShopifyTransport,
    _install_fake_transport,
    _run_row,
    harness,
)

# 所有并发等待的上限（秒）。超时即视为卡住，直接 fail 而非继续跑。
_WAIT_TIMEOUT = 10


# ---------------------------------------------------------------------------
# 辅助：可确定性地"卡住第一页"的 page_hook
# ---------------------------------------------------------------------------


class _FirstPageGate:
    """把 transport 的第一页卡在 `release.wait()` 上，直到主线程放行。

    只允许第一页等待一次（`armed`），后续页直接返回，避免第一页放行后
    又被卡住导致测试无法结束。
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self._armed = False
        self._lock = threading.Lock()

    def hook(self, data_key: str, index: int) -> None:
        if data_key != "orders" or index != 0:
            return
        with self._lock:
            if self._armed:
                return
            self._armed = True
        self.entered.set()
        # 不用断言：hook 在后台线程里跑，断言会变成线程异常而非测试失败。
        self.release.wait(_WAIT_TIMEOUT)

    def open(self) -> None:
        """放行第一页（幂等）。"""
        self.release.set()

    def close(self) -> None:
        """确保后台线程能退出（幂等），在 finally 里调用。"""
        self.release.set()


def _assert_gate_free(main_mod: Any) -> None:
    """锁泄漏的直接探针：用例结束前闸门必须处于未持有状态。"""
    assert main_mod._sync_gate_lock.locked() is False, "单飞闸门未释放：锁泄漏"
    assert main_mod._sync_gate_owner is None, "单飞闸门的 owner 未清空"


def _running_run_id(db_mod: Any, *, exclude: set[str] | None = None) -> str | None:
    """读台账里最新的 running run（不用 sleep 轮询，由调用方配合 Event 使用）。"""
    with db_mod.SessionLocal() as db:
        query = db.query(db_mod.ExternalSyncRun).filter(db_mod.ExternalSyncRun.status == "running")
        if exclude:
            query = query.filter(~db_mod.ExternalSyncRun.run_id.in_(exclude))
        run = query.order_by(db_mod.ExternalSyncRun.id.desc()).first()
        return run.run_id if run is not None else None


# ---------------------------------------------------------------------------
# S4-1 第一个 run 运行中，第二个请求被拒
# ---------------------------------------------------------------------------


def test_s4_1_second_request_rejected_while_first_running(harness, monkeypatch):
    """S4-1：运行中再来一个 sync → 409 SHOPIFY_SYNC_IN_PROGRESS，第一个 run 不受影响。"""
    gate = _FirstPageGate()
    _install_fake_transport(monkeypatch, FakeShopifyTransport(pages=1, page_hook=gate.hook))

    box: dict[str, Any] = {}

    def worker() -> None:
        box["response"] = harness.client.post("/api/external/shopify/sync?resources=orders")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        assert gate.entered.wait(_WAIT_TIMEOUT), "第一个同步没有进入第一页"

        # 此刻第一个请求已持锁且卡在第一页；第二个请求必须立刻 409（不排队）。
        second = harness.client.post("/api/external/shopify/sync?resources=orders")

        assert second.status_code == 409, second.text
        assert second.json()["detail"]["code"] == "SHOPIFY_SYNC_IN_PROGRESS"
        # 409 文案稳定，run_id 只进日志（§2.4：不扩 DomainError 结构）。
        assert second.json()["detail"]["message"] == "已有 live 同步正在运行，请等待其结束或先调用取消接口"
        # 闸门此刻确实被第一个请求持有。
        assert harness.main._sync_gate_lock.locked() is True
    finally:
        gate.open()
        thread.join(timeout=_WAIT_TIMEOUT)

    assert not thread.is_alive(), "第一个同步线程没有退出"
    first = box["response"]
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "succeeded"
    # 第一个 run 正常跑完，没有被第二个请求影响。
    assert body["resources"]["orders"]["inserted"] == 1
    _assert_gate_free(harness.main)


# ---------------------------------------------------------------------------
# S4-2 成功后再次请求（finally 释放的正面证明）
# ---------------------------------------------------------------------------


def test_s4_2_sequential_runs_after_success(harness, monkeypatch):
    """S4-2：顺序两次请求都成功，产生两个不同 run —— 证明锁在 finally 释放。"""
    _install_fake_transport(monkeypatch, FakeShopifyTransport(pages=1))

    first = harness.client.post("/api/external/shopify/sync?resources=orders")
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "succeeded"
    _assert_gate_free(harness.main)

    second = harness.client.post("/api/external/shopify/sync?resources=orders")
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "succeeded"
    _assert_gate_free(harness.main)

    first_id = first.json()["run_id"]
    second_id = second.json()["run_id"]
    assert first_id != second_id, "两次同步复用了同一个 run_id"
    assert _run_row(harness.db_mod, first_id)["status"] == "succeeded"
    assert _run_row(harness.db_mod, second_id)["status"] == "succeeded"


# ---------------------------------------------------------------------------
# S4-3a 预算超限终止后再次请求
# ---------------------------------------------------------------------------


def test_s4_3a_gate_released_after_budget_exceeded(harness, monkeypatch):
    """S4-3a：run 因预算超限 failed 后，闸门已释放 —— 第二个请求 200 而非 409。"""
    monkeypatch.setenv("SHOPIFY_SYNC_BUDGET_SECONDS", "0")
    cfg.get_settings.cache_clear()
    _install_fake_transport(monkeypatch, FakeShopifyTransport(always_next=True))

    first = harness.client.post("/api/external/shopify/sync?resources=orders")
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "failed"
    assert first.json()["error_code"] == "SHOPIFY_SYNC_BUDGET_EXCEEDED"
    # 异常分支结束后锁必须已释放。
    _assert_gate_free(harness.main)

    # 预算恢复，第二次请求应正常放行。
    monkeypatch.setenv("SHOPIFY_SYNC_BUDGET_SECONDS", "300")
    cfg.get_settings.cache_clear()
    # 第二次换成单页 transport：本用例只验证闸门放行，不验证页数预算
    # （always_next=True 会让第二个 run 因 SHOPIFY_PAGE_LIMIT 失败，与闸门无关）。
    _install_fake_transport(monkeypatch, FakeShopifyTransport(pages=1))

    second = harness.client.post("/api/external/shopify/sync?resources=orders")
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "succeeded"
    _assert_gate_free(harness.main)


# ---------------------------------------------------------------------------
# S4-3b 取消终止后再次请求
# ---------------------------------------------------------------------------


def test_s4_3b_gate_released_after_cancelled(harness, monkeypatch):
    """S4-3b：run 因取消 failed 后，闸门已释放 —— 第二个请求 200 而非 409。"""
    gate = _FirstPageGate()
    _install_fake_transport(monkeypatch, FakeShopifyTransport(always_next=True, page_hook=gate.hook))

    box: dict[str, Any] = {}

    def worker() -> None:
        box["response"] = harness.client.post("/api/external/shopify/sync?resources=orders")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    run_id: str | None = None
    try:
        assert gate.entered.wait(_WAIT_TIMEOUT), "第一个同步没有进入第一页"
        # run 在进入第一页之前就已在 begin_sync_run 落库，此处必然能取到。
        run_id = _running_run_id(harness.db_mod)
        assert run_id, "没有找到运行中的 sync run"

        cancel = harness.client.post(f"/api/external/sync/runs/{run_id}/cancel")
        assert cancel.status_code == 202, cancel.text
    finally:
        gate.open()
        thread.join(timeout=_WAIT_TIMEOUT)

    assert not thread.is_alive(), "同步线程没有在取消后退出"
    first = box["response"]
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "failed"
    assert first.json()["error_code"] == "SHOPIFY_SYNC_CANCELLED"
    # 取消分支结束后锁必须已释放。
    _assert_gate_free(harness.main)

    # 第二次请求（无 page_hook，正常跑完）必须放行。
    _install_fake_transport(monkeypatch, FakeShopifyTransport(pages=1))
    second = harness.client.post("/api/external/shopify/sync?resources=orders")
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "succeeded"
    _assert_gate_free(harness.main)


# ---------------------------------------------------------------------------
# S4-4 取消不被闸门拦截
# ---------------------------------------------------------------------------


def test_s4_4_cancel_not_blocked_by_gate(harness, monkeypatch):
    """S4-4：第一个 run 运行中调 /cancel 返回 202（闸门不拦取消），run 以取消收尾。"""
    gate = _FirstPageGate()
    _install_fake_transport(monkeypatch, FakeShopifyTransport(always_next=True, page_hook=gate.hook))

    box: dict[str, Any] = {}

    def worker() -> None:
        box["response"] = harness.client.post("/api/external/shopify/sync?resources=orders")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    run_id: str | None = None
    try:
        assert gate.entered.wait(_WAIT_TIMEOUT), "第一个同步没有进入第一页"
        # 闸门被持有的前提下，取消接口仍必须可用，否则闸门会把自己锁死。
        assert harness.main._sync_gate_lock.locked() is True

        run_id = _running_run_id(harness.db_mod)
        assert run_id, "没有找到运行中的 sync run"

        cancel = harness.client.post(f"/api/external/sync/runs/{run_id}/cancel")
        assert cancel.status_code == 202, cancel.text
        assert cancel.json()["run_id"] == run_id
        assert cancel.json()["cancel_requested"] is True
    finally:
        gate.open()
        thread.join(timeout=_WAIT_TIMEOUT)

    assert not thread.is_alive(), "同步线程没有在取消后退出"
    body = box["response"].json()
    assert body["run_id"] == run_id
    assert body["status"] == "failed"
    assert body["error_code"] == "SHOPIFY_SYNC_CANCELLED"
    _assert_gate_free(harness.main)
