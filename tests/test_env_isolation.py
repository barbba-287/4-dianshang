"""E1：测试进程与仓库外 `.env` 解耦的防漂移用例。

背景（D-3）：`app/config.py` 的 `Settings` 使用 `env_file=".env"`、`case_sensitive=False`，
若开发机仓库外的 `.env` 写了 `SHOPIFY_LIVE_ENABLED=true` / 真 `SHOPIFY_ACCESS_TOKEN`，
测试进程就会读到本地真值，测试结果随本地 `.env` 漂移。

`tests/conftest.py` 现在在模块级与 `_isolate_env` 两处统一固定 Shopify 关闭基线
（环境变量优先级高于 dotenv）。本用例是这条约束的**防漂移哨兵**：一旦有人删掉
conftest 里的固定项、或改动固定值，本用例立即失败。

注意：本文件独立成文件而非并入 `tests/test_demo_path_contract.py`——后者的 docstring
已把范围限定为「D-2 裁定后的页面/API 路由契约」（C1–C9），塞入配置隔离断言会破坏该
文件「单一契约主题」的可读性；且配置隔离是**独立的横切关注点**，需要能单独被 grep 定位。
"""

from app.config import Settings


def test_settings_ignores_local_dotenv_shopify_values():
    """构造 `Settings()`，断言 Shopify live 关闭且 token 为空，与本地 `.env` 无关。"""
    settings = Settings()

    # 核心断言：不受仓库外 `.env` 影响。
    assert settings.shopify_live_enabled is False
    assert settings.shopify_access_token == ""

    # 同一批固定项的其余部分：与 `app/config.py` 既有默认值一致，且不随本地 `.env` 漂移。
    assert settings.shopify_store_domain == ""
    assert settings.shopify_sync_budget_seconds == 300
    assert settings.shopify_max_pages == 100
    assert settings.shopify_api_version == "2026-07"
