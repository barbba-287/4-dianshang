"""后台 worker 线程登记表（进程级，不随 `importlib.reload` 丢失）。

## 为什么需要这个模块

`app/background.py` 的调度线程 `ds-dispatcher` 由模块级 `_dispatch_thread` 持有，
其停止信号 `_dispatch_stop` 通过闭包传进 `_dispatch_loop`。

测试里常见写法是 `importlib.reload(app.background)`，这会把模块级
`_dispatch_thread` / `_dispatch_stop` 重置为 `None`，**但旧线程对象仍然存活**，
且它闭包引用的是**旧模块**的 `_dispatch_stop`。此后任何
`stop_background_workers()` 调用操作的都是新模块的 `None`，
**永远停不掉旧线程**。

旧线程会继续用旧 engine 扫库；当它所属的 `tmp_path` 被 pytest 清理后，
下一个用例 `setenv` 到新路径，旧线程仍写旧路径 → 父目录不存在 →
`sqlite3.OperationalError: attempt to write a readonly database`。
表现为"某个用例随机爆、单跑必过、换个顺序就换位置"（R-19）。

**本模块的作用**：把 `(thread, stop_event)` 登记在一个**独立模块**里。
独立模块自身不会被 `reload(app.background)` 影响，因此测试侧始终能拿到
旧线程的 `stop_event` 并 `set()`，从而真正停掉它。

## 设计约束

- 登记表按**线程对象去重**，重复调用 `register` 不会产生重复项。
- `reap_all()` 是幂等的：已停止的线程会被自动移出登记表。
- 本模块**不主动创建线程**，只记账；线程的创建与启动仍由 `app.background` 负责。
- 生产环境同样会登记，但只有测试会调用 `reap_all()`；正常运行时登记表
  至多含一个条目（调度线程是单例），无内存增长风险。
"""

from __future__ import annotations

import threading
from typing import Iterable

# 线程 -> 停止信号。用 dict 而非 list：以线程对象为键天然去重。
_registry: dict[threading.Thread, threading.Event] = {}
_lock = threading.Lock()


def register(thread: threading.Thread, stop_event: threading.Event) -> None:
    """登记一个后台线程及其停止信号（重复登记同一线程为空操作）。"""
    with _lock:
        _registry[thread] = stop_event


def unregister(thread: threading.Thread) -> None:
    """移除登记（线程正常停止后调用）。"""
    with _lock:
        _registry.pop(thread, None)


def snapshot() -> list[tuple[threading.Thread, threading.Event]]:
    """返回当前登记表的快照（供测试断言用，避免持锁遍历）。"""
    with _lock:
        return list(_registry.items())


def live_threads(name: str | None = None) -> list[threading.Thread]:
    """返回仍存活的已登记线程；`name` 非空时按线程名过滤。"""
    with _lock:
        items = list(_registry.items())
    return [
        thread
        for thread, _event in items
        if thread.is_alive() and (name is None or thread.name == name)
    ]


def reap_all(timeout: float = 1.0) -> int:
    """向所有已登记线程发停止信号并等待退出，返回成功停掉的线程数。

    **这是本模块存在的唯一目的**：停掉那些已经失去模块级引用的旧线程。
    幂等：已停止的线程会被移出登记表；异常一律内部消化，不向调用方抛
    （清理动作发生在测试 teardown，抛出会掩盖真正的断言结果）。

    `timeout` 是**每个线程**的 join 上限，不是总时长。调用方需自行控制总耗时
    （当前唯一调用点是 `tests/conftest.py` 的 autouse fixture）。
    """
    with _lock:
        items = list(_registry.items())

    reaped = 0
    for thread, stop_event in items:
        if not thread.is_alive():
            unregister(thread)
            continue
        try:
            stop_event.set()
        except Exception:  # noqa: BLE001 - 停止信号本身不应抛，保险起见
            pass
        try:
            thread.join(timeout=timeout)
        except Exception:  # noqa: BLE001 - 非本线程启动的线程 join 可能抛
            pass
        if thread.is_alive():
            # 没停下来：保留登记，下次再试；不在这里死等。
            continue
        unregister(thread)
        reaped += 1
    return reaped


def clear() -> None:
    """清空登记表（仅供测试在会话结束时调用，不停止线程）。"""
    with _lock:
        _registry.clear()
