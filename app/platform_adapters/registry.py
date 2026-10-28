"""平台适配器注册表：适配器的统一注册、发现与构造入口（P2-1）。

本模块是适配器的**单一入口**：
- `register_platform(platform, factory)`：注册平台工厂（只在 import 期发生）。
- `get_adapter(platform, **kwargs)`：按平台构造适配器实例。
- `registered_platforms()`：已注册平台清单。
- `platform_capabilities()`：按注册表返回各平台**真实**能力状态（不再写死假数据）。

设计约束（与任务文件 `dev_task.p2s1-connector.md` §3 对齐）：

1. **构造参数逐字节等价搬移**：工厂只负责"把参数转成适配器实例"，
   live/fixture 的分支判断与传输层构造参数按原样搬入（原先散在 `app/main.py`），
   调用点传入的适配器参数与搬移前完全一致，既有 preview / sync / dry-run 行为零变化。
2. **失败一律抛错，不返回 None**：未注册平台抛 `CONNECTOR_UNKNOWN_PLATFORM`，
   同一平台重复注册抛 `CONNECTOR_DUPLICATE_PLATFORM`（注册只在 import 期发生，
   覆盖既有注册是隐患，直接报错比静默覆盖安全）。
3. **不反向依赖适配器实现**：shopify / taobao 的模块导入放在工厂内部（惰性导入），
   导入注册表本身不会把适配器实现整包拉进来，也避免与 `app.connectors` 形成循环导入。
"""
from __future__ import annotations

from typing import Any, Callable

from app.platform_adapters.base import AdapterError

# 工厂签名：接收构造参数，返回适配器实例。
AdapterFactory = Callable[..., Any]

# 已注册平台 → 工厂。import 期填充，运行期只读。
_FACTORIES: dict[str, AdapterFactory] = {}


# ---------------------------------------------------------------------------
# 注册 / 构造 / 发现
# ---------------------------------------------------------------------------


def register_platform(platform: str, factory: AdapterFactory) -> None:
    """注册平台工厂。

    重复注册直接报错（`CONNECTOR_DUPLICATE_PLATFORM`）：注册本应只在 import 期发生，
    运行期覆盖既有注册会让"同一个平台对应两套构造逻辑"变成静默故障。
    """
    if platform in _FACTORIES:
        raise AdapterError(code="CONNECTOR_DUPLICATE_PLATFORM", message=f"平台已注册，禁止重复注册：{platform}")
    _FACTORIES[platform] = factory


def get_adapter(platform: str, **kwargs: Any) -> Any:
    """按平台构造适配器实例。

    未注册平台抛 `AdapterError(code="CONNECTOR_UNKNOWN_PLATFORM")`，
    **绝不返回 None**（返回 None 会把"平台没接"变成调用方的空指针式失败）。
    """
    factory = _FACTORIES.get(platform)
    if factory is None:
        raise AdapterError(code="CONNECTOR_UNKNOWN_PLATFORM", message=f"未注册的平台适配器：{platform}")
    return factory(**kwargs)


def registered_platforms() -> tuple[str, ...]:
    """已注册平台清单（按字典序，便于断言与展示）。"""
    return tuple(sorted(_FACTORIES))


# ---------------------------------------------------------------------------
# 各平台工厂
#
# 工厂参数与搬移前的构造调用逐项对应；新增参数一律带默认值，
# 缺省值即搬移前适配器构造函数的默认值。
# ---------------------------------------------------------------------------


def _shopify_factory(
    *,
    transport: Any = None,
    live: bool = False,
    settings: Any = None,
    live_enabled: bool = False,
    simulated: bool = True,
    page_size: int = 50,
    max_pages: int = 100,
    deadline: float | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> Any:
    """构造 Shopify 只读适配器。

    - `transport` 显式给出时直接使用（sync / resume 路径需要保留 transport 句柄，
      供 live 响应留存留痕使用）；
    - `transport` 未给出时按 `live` 走传输形态分支（搬移自 `app/main.py` 的 preview 分支）：
      live 用 `ShopifyLiveTransport`，离线用空 `ShopifyFixtureTransport`。
    """
    from app.platform_adapters.shopify import ShopifyFixtureTransport, ShopifyLiveTransport, ShopifyReadOnlyAdapter

    if transport is None:
        if live:
            if settings is None:
                raise AdapterError(code="CONNECTOR_SHOPIFY_SETTINGS_REQUIRED", message="构造 Shopify live 适配器需要 settings")
            transport = ShopifyLiveTransport(
                settings.shopify_store_domain,
                settings.shopify_access_token,
                settings.shopify_api_version,
                settings.shopify_request_timeout_seconds,
                settings.shopify_max_retries,
            )
        else:
            empty = {"data": {"products": {"edges": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}, "orders": {"edges": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}, "locations": {"edges": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
            transport = ShopifyFixtureTransport({"products": empty, "orders": empty, "inventory": empty})
    return ShopifyReadOnlyAdapter(
        transport,
        live_enabled=live_enabled,
        simulated=simulated,
        page_size=page_size,
        max_pages=max_pages,
        deadline=deadline,
        cancel_check=cancel_check,
    )


def _taobao_factory(
    *,
    transport: Any = None,
    enabled: bool = False,
    simulated: bool = True,
    max_page_size: int = 100,
) -> Any:
    """构造淘宝只读适配器（骨架保持 disabled-by-default，不接真实 transport）。"""
    from app.platform_adapters.taobao import TaobaoAdapter

    return TaobaoAdapter(
        enabled=enabled,
        transport=transport,
        simulated=simulated,
        max_page_size=max_page_size,
    )


# ---------------------------------------------------------------------------
# 能力真实化
#
# 旧实现对六个平台一律返回 live_enabled=False / simulated=True 的写死假数据，
# 与真实适配器状态脱节（演练已证明 shopify 可真实 live，能力端点在撒谎）。
# 这里改为按注册表与运行期状态如实计算。
# ---------------------------------------------------------------------------


def _shopify_capability() -> dict[str, Any]:
    """Shopify 的真实能力：热开关与 .env 配置共同决定是否放行 live 通道。"""
    from app.config import get_settings
    from app.runtime_flags import live_enabled as runtime_live_enabled

    settings = get_settings()
    live_allowed = bool(settings.shopify_live_enabled and runtime_live_enabled())
    return {
        "platform": "shopify",
        "source_modes": list(_source_modes()),
        "read_only": True,
        "live_enabled": live_allowed,
        # 传输形态决定 simulated：走 live 通道即模拟关闭，否则是离线 fixture 形态。
        "simulated": not live_allowed,
        "implemented": True,
    }


def _taobao_capability() -> dict[str, Any]:
    """淘宝的真实能力：骨架未接真实 transport，如实报模拟态。"""
    return {
        "platform": "taobao",
        "source_modes": list(_source_modes()),
        "read_only": True,
        "live_enabled": False,
        "simulated": True,
        "implemented": True,
    }


def _unimplemented_capability(platform: str) -> dict[str, Any]:
    """未接适配器实现的平台：如实暴露"未实现"，不假装自己是可用连接器（R-37）。"""
    return {
        "platform": platform,
        "source_modes": list(_source_modes()),
        "read_only": True,
        "live_enabled": False,
        "simulated": True,
        "implemented": False,
    }


_CAPABILITY_PROVIDERS: dict[str, Callable[[], dict[str, Any]]] = {
    "shopify": _shopify_capability,
    "taobao": _taobao_capability,
}


def _source_modes() -> tuple[str, ...]:
    """离线来源模式清单（与 `app.connectors` 的 MODES 同源，避免两处漂移）。"""
    from app.connectors import MODES

    return MODES


def platform_capabilities() -> list[dict[str, Any]]:
    """返回各平台的真实能力状态。

    条目数量与 `platform` 键值与旧实现保持一致（六平台），仅条目内状态字段真实化。
    """
    from app.connectors import PLATFORMS

    items: list[dict[str, Any]] = []
    for platform in PLATFORMS:
        provider = _CAPABILITY_PROVIDERS.get(platform)
        if provider is None:
            items.append(_unimplemented_capability(platform))
            continue
        item = provider()
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# 内置平台注册（import 期发生，运行期不再变更）
# ---------------------------------------------------------------------------


def _register_builtin_platforms() -> None:
    for platform, factory in (("shopify", _shopify_factory), ("taobao", _taobao_factory)):
        if platform not in _FACTORIES:
            register_platform(platform, factory)


_register_builtin_platforms()
