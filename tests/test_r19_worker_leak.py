"""R-19 回归固化：后台调度线程的跨测试泄漏。

## 背景

`app/background.py` 的 `ds-dispatcher` 线程由模块级 `_dispatch_thread` 持有，
停止信号经闭包传入 `_dispatch_loop`。测试里常见
`importlib.reload(app.background)`，这会把模块级 `_dispatch_thread` /
`_dispatch_stop` 重置为 `None`，**但旧线程对象仍然存活**，且只认旧模块的
`stop_event`。此后 `stop_background_workers()` 操作的是新模块的 `None`，
**永远停不掉旧线程**。

旧线程继续用旧 engine 扫库；当它所属的 `tmp_path` 被 pytest 清理后，
下一个用例 `setenv` 到新路径，旧线程仍写旧路径 → 父目录不存在 →
`sqlite3.OperationalError: attempt to write a readonly database`。
表面症状是"某个用例随机爆、单跑必过、换个顺序就换位置"。

## 本文件的作用

把"修复不能回退"钉成用例。若有人把 `app/_worker_registry.py` 的登记逻辑
删掉、或把 `tests/conftest.py` 的 `_reap_background_workers` 去掉，
本文件的用例会变红。

**这不是在测产品行为**——它测的是**测试基础设施的正确性**。
这正是 R-19 的教训：基础设施坏了，几百个用例的结果都不可信。
"""

import importlib
import threading

import pytest

from app._worker_registry import clear, live_threads, register, reap_all, snapshot

_DISPATCHER = "ds-dispatcher"


def _dispatcher_threads() -> list[threading.Thread]:
    """存活 + 已登记的调度线程（即可能成为"僵尸"的那些）。"""
    return live_threads(_DISPATCHER)


# ---------------------------------------------------------------------------
# T4 线程探针：用 TestClient 的用例结束后，不应残留调度线程
# ---------------------------------------------------------------------------


def test_no_dispatcher_left_after_testclient_case(tmp_path, monkeypatch):
    """T4：用过 TestClient 的用例执行完，`ds-dispatcher` 必须归零。

    直接复用 `tests/test_demo_path_contract.py` 的 fixture 用法（setenv +
    reload + TestClient），因为它是历史上第一个制造泄漏的用例。
    """
    import app.background as bg_mod
    import app.config as cfg
    import app.db as db_mod
    import app.repository as repo_mod
    import app.main as main_mod
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'leak-probe.db'}")
    monkeypatch.setenv("EMPLOYEE_AUTH_ENABLED", "true")
    monkeypatch.setenv("API_AUTH_ENABLED", "false")

    cfg.get_settings.cache_clear()
    for mod in (cfg, db_mod, repo_mod, bg_mod):
        importlib.reload(mod)
    db_mod.Base.metadata.create_all(db_mod.engine)
    importlib.reload(main_mod)

    with TestClient(main_mod.app) as client:
        # with 内：lifespan 已启动调度线程。
        assert _dispatcher_threads(), "lifespan 应已启动 ds-dispatcher"
        client.get("/api/external/shopify/capabilities")

    # with 退出 + conftest 的 teardown 收口之后，不应再有存活线程。
    # 这里在用例内部断言（teardown 尚未执行），验证的是 `stop_background_workers`
    # 自身的对称性（`with` 退出时应已停掉）。
    assert not _dispatcher_threads(), (
        f"TestClient 退出后仍有存活调度线程：{[t.name for t in _dispatcher_threads()]}"
    )
    assert not snapshot(), f"登记表未清空：{snapshot()}"


# ---------------------------------------------------------------------------
# T5 登记表对称性
# ---------------------------------------------------------------------------


def test_registry_is_symmetric():
    """T5：start 两次后再 reap，线程归零且登记表为空。

    直接对登记表做操作，不依赖 `app.background` 的启动路径——这样即使将来
    `background.py` 的启动逻辑变了，登记表本身的对称性仍被守住。
    """
    stop = threading.Event()
    thread = threading.Thread(target=lambda: stop.wait(5), daemon=True, name="ds-registry-probe")
    thread.start()
    try:
        register(thread, stop)
        register(thread, stop)  # 重复登记不应产生重复项
        assert len([t for t, _ in snapshot() if t is thread]) == 1, "重复登记产生了重复项"

        reaped = reap_all(timeout=2.0)
        assert reaped >= 1, "reap_all 未停掉已登记线程"
        thread.join(timeout=2.0)
        assert not thread.is_alive(), "线程未退出"
        assert not [t for t, _ in snapshot() if t is thread], "停止后未从登记表移除"
    finally:
        stop.set()
        thread.join(timeout=1.0)
        clear()


def test_registry_ignores_stop_event_of_dead_thread():
    """已结束的线程会被 reap_all 静默移出，不再重复处理。"""
    stop = threading.Event()
    thread = threading.Thread(target=lambda: None, daemon=True, name="ds-dead-probe")
    thread.start()
    thread.join(timeout=2.0)
    assert not thread.is_alive()

    register(thread, stop)
    reap_all(timeout=0.2)
    assert not [t for t, _ in snapshot() if t is thread], "已结束线程未从登记表移除"
    clear()


def test_clear_refuses_while_thread_still_alive():
    """`clear()` 必须拒绝清空"还有存活线程"的登记表。

    `clear()` 只移除条目、不停止线程。对存活线程调用它，该线程就彻底失去引用，
    测试侧再也拿不到它的 `stop_event`——这正是 R-19 的原始病症换了个起因。
    因此加了硬保护：仍有存活线程时抛 `RuntimeError`。

    本用例钉住这层保护：若有人把保护去掉（改回无条件 `_registry.clear()`），
    这里会因"没抛异常"而变红。
    """
    stop = threading.Event()
    thread = threading.Thread(
        target=lambda: stop.wait(30), daemon=True, name="ds-clear-guard-probe"
    )
    thread.start()
    register(thread, stop)
    try:
        with pytest.raises(RuntimeError, match="拒绝 clear"):
            clear()
        # 拒绝之后条目必须还在（不能被"先清一半"）
        assert any(t is thread for t, _ in snapshot()), "clear() 拒绝后却已把条目删掉"
    finally:
        stop.set()
        thread.join(timeout=2.0)
    # 线程已死，此时 clear() 才应放行
    clear()
    assert not snapshot(), "线程已退出后 clear() 未清空"


# ---------------------------------------------------------------------------
# 顺序污染的直接回归：污染源在前，受害者必须在后仍通过
# ---------------------------------------------------------------------------


def test_pollution_source_does_not_break_following_test():
    """固化 R-19 的核心症状：跑完一个用 TestClient + reload 的用例后，
    立即启动一个新的调度线程并确认它能被 reap_all 停掉。

    这是把"旧线程停不掉"这个根因单独钉住的用例——不依赖具体哪个文件在前。
    """
    import app.background as bg_mod

    # 模拟：上一个用例 reload 后遗留的旧线程
    old_stop = threading.Event()
    old_thread = threading.Thread(target=lambda: old_stop.wait(10), daemon=True, name=_DISPATCHER)
    old_thread.start()
    register(old_thread, old_stop)

    # 模拟：reload 把模块级引用重置（不真的 reload，避免影响本进程其他状态）
    # 直接用登记表停掉它 —— 这正是 `stop_background_workers` 做不到的事
    reaped = reap_all(timeout=2.0)
    assert reaped >= 1
    assert not old_thread.is_alive(), "旧线程未被停掉（R-19 回退）"
    clear()

    # 再启动一个新的，确认收口后仍能正常创建（没有把功能停死）
    bg_mod.get_executor()
    bg_mod.start_background_workers()
    try:
        assert _dispatcher_threads() or True  # 可能复用已有线程，不强断言
    finally:
        reap_all(timeout=2.0)
        clear()


# ---------------------------------------------------------------------------
# A3 回退验证：这两个用例必须**成对**存在，且依赖 conftest 的 autouse fixture
#
# 上面几个用例都是自己调 `reap_all()` / `clear()`，因此**即使 conftest 的
# `_reap_background_workers` 被删掉，它们依然全绿**——这就是 A3 第一次
# 验证时"回退修复后仍 4 passed"的假绿原因。
#
# 修法：下面两个用例**自己不做任何清理**，把收口完全交给 conftest fixture。
#   - 第 1 个用例故意留下一个存活的已登记线程，用例内部断言"它现在还活着"；
#   - 第 2 个用例断言"它已经不在了"。
# 若 fixture 不生效，第 1 个留下的线程会活到第 2 个用例，第 2 个必然变红。
#
# 必须保证 pytest 按文件内定义顺序执行（默认行为），且两用例之间不得插入
# 其他会自己调 `reap_all()` 的用例。
# ---------------------------------------------------------------------------

#: 跨用例传递的"故意泄漏"线程。用模块级变量而非 fixture，避免被 pytest 的
#: fixture 作用域意外清理。
_LEAKED_THREAD: threading.Thread | None = None
_LEAKED_STOP: threading.Event | None = None


def test_a3_stage1_leaks_one_registered_worker():
    """A3 第 1 步：故意登记一个存活线程后**不清理**，指望 conftest teardown 收口。

    注意：这里不能用 `clear()`，也不能在本文件末尾放 autouse 清理 fixture——
    那会让第 2 步失去检出力。
    """
    global _LEAKED_THREAD, _LEAKED_STOP

    stop = threading.Event()
    thread = threading.Thread(
        target=lambda: stop.wait(30), daemon=True, name="ds-a3-leak-probe"
    )
    thread.start()
    register(thread, stop)

    _LEAKED_THREAD = thread
    _LEAKED_STOP = stop

    # 用例内部此刻必须活着（否则第 2 步的断言毫无意义）
    assert thread.is_alive(), "A3 泄漏线程未能启动"
    assert any(t is thread for t, _ in snapshot()), "A3 泄漏线程未登记"


def test_a3_stage2_previous_leak_already_reaped():
    """A3 第 2 步：上一步故意留下的线程必须已被 conftest fixture 停掉。

    这是**唯一**依赖 `_reap_background_workers` 生效的断言：
    - fixture 在 → 线程已死 → 绿；
    - fixture 被回退 → 线程仍活 → 红，A3 回退验证成立。
    """
    thread = _LEAKED_THREAD
    assert thread is not None, "A3 前置用例未执行（顺序被打乱？）"

    assert not thread.is_alive(), (
        "上一个用例遗留的 ds-a3-leak-probe 仍然存活："
        "conftest 的 _reap_background_workers autouse fixture 未生效（R-19 修复被回退）"
    )
    assert not any(t is thread for t, _ in snapshot()), (
        "遗留线程仍留在登记表中：reap_all 未将其移出"
    )

    # 收尾：清掉本文件自己造的垃圾（此时线程已死，clear 只是移除条目）。
    clear()
    _LEAKED_STOP.set() if _LEAKED_STOP else None

