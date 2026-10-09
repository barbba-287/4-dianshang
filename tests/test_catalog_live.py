"""淘宝公开搜索 Canary 的纯逻辑测试；默认不访问公网。"""

from urllib.parse import parse_qs, urlparse

import pytest

from app.catalog_live import (
    CatalogLiveError,
    CatalogLiveLimits,
    build_taobao_search_url,
    canonicalize_product_url,
    validate_next_url,
    validate_search_url,
)


KEYWORD = "白色波点半身裙女夏秋季2026新款高腰a字包臀半裙miu里miu气短裙"


def test_build_search_url_has_one_encoded_keyword():
    url = build_taobao_search_url(KEYWORD)
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "s.taobao.com"
    assert parsed.path == "/search"
    assert parse_qs(parsed.query) == {"q": [KEYWORD]}
    assert "%E7%99%BD%E8%89%B2" in url


def test_validate_search_url_requires_exact_keyword_and_shape():
    url = build_taobao_search_url(KEYWORD)
    assert validate_search_url(url, KEYWORD) == url

    invalid_urls = [
        url.replace("https://", "http://"),
        url.replace("s.taobao.com", "evil.example.com"),
        url.replace("/search?", "/login?"),
        f"{url}&page=2",
        url.replace("&", "&q=other&", 1) if "&" in url else f"{url}&q=other",
        f"https://user:password@s.taobao.com/search?q={KEYWORD}",
        f"{url}#fragment",
    ]
    for invalid in invalid_urls:
        with pytest.raises(CatalogLiveError) as error:
            validate_search_url(invalid, KEYWORD)
        assert error.value.code == "INVALID_SEARCH_URL"


def test_validate_next_url_allows_only_numeric_paging_parameters():
    next_url = build_taobao_search_url(KEYWORD) + "&page=2"
    assert validate_next_url(next_url, KEYWORD).endswith("&page=2")

    with pytest.raises(CatalogLiveError, match="查询参数"):
        validate_next_url(build_taobao_search_url(KEYWORD) + "&sort=sale", KEYWORD)

    with pytest.raises(CatalogLiveError, match="分页参数"):
        validate_next_url(build_taobao_search_url(KEYWORD) + "&page=next", KEYWORD)


def test_product_url_is_canonicalized_without_tracking_parameters():
    canonical, product_id = canonicalize_product_url(
        "https://item.taobao.com/item.htm?id=123456789&spm=a1z10.1-c-s.w5003"
    )
    assert canonical == "https://item.taobao.com/item.htm?id=123456789"
    assert product_id == "123456789"

    canonical, product_id = canonicalize_product_url(
        "https://detail.tmall.com/item.htm?foo=bar&id=987654321"
    )
    assert canonical == "https://detail.tmall.com/item.htm?id=987654321"
    assert product_id == "987654321"


def test_product_url_rejects_non_allowlisted_links():
    for url in (
        "http://item.taobao.com/item.htm?id=1",
        "https://item.taobao.com/item.htm?id=1#x",
        "https://evil.example/item.htm?id=1",
        "https://item.taobao.com/item.htm?id=not-a-number",
        "https://item.taobao.com/item.htm",
    ):
        with pytest.raises(CatalogLiveError) as error:
            canonicalize_product_url(url)
        assert error.value.code == "INVALID_PRODUCT"


def test_live_limits_reject_unsafe_values():
    invalid_limits = (
        CatalogLiveLimits(max_pages=0),
        CatalogLiveLimits(min_delay_seconds=0),
        CatalogLiveLimits(page_timeout_seconds=0),
        CatalogLiveLimits(total_timeout_seconds=10, page_timeout_seconds=20),
        CatalogLiveLimits(max_products=0),
    )
    for limits in invalid_limits:
        with pytest.raises(CatalogLiveError) as error:
            limits.validate()
        assert error.value.code == "INVALID_REPLAY_INPUT"


def test_live_gate_rejects_before_browser_is_started(monkeypatch):
    import app.catalog_live as live

    def fail_if_started():
        raise AssertionError("sync_playwright must not start when live is disabled")

    monkeypatch.setattr(live, "sync_playwright", fail_if_started)
    with pytest.raises(CatalogLiveError) as error:
        live.collect_taobao_public_catalog(
            KEYWORD,
            enabled=False,
            confirm_public_read_only=True,
        )
    assert error.value.code == "LIVE_NETWORK_DISABLED"

    with pytest.raises(CatalogLiveError) as error:
        live.collect_taobao_public_catalog(
            KEYWORD,
            enabled=True,
            confirm_public_read_only=False,
        )
    assert error.value.code == "LIVE_CONFIRMATION_REQUIRED"
