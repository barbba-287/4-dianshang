"""进程内运行期开关（热开关的存储与读取）。

用途：让 Shopify live 通道可以在运行期被关掉，不必改 .env 重启进程。
单进程假设与 app/background.py 相同；进程重启后回落到 .env 配置值
（settings.shopify_live_enabled），本模块只做"额外的放行/拦截"，不放大权限。
"""
import threading


_live_enabled = threading.Event()
# 默认放行：是否真的走 live 仍由 settings.shopify_live_enabled 决定，
# 运行期开关不额外拦截，只提供"运行期关掉"的能力。
_live_enabled.set()


def set_live_enabled(value: bool) -> None:
    """打开或关闭 live 通道（仅进程内，不落库、不写 .env）。"""
    if value:
        _live_enabled.set()
    else:
        _live_enabled.clear()


def live_enabled() -> bool:
    """当前运行期 live 开关是否放行。"""
    return _live_enabled.is_set()
