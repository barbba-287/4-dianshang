"""受控的淘宝公开搜索页 Canary 采集器。

该模块只读取无需登录的公开搜索页，不使用 Cookie、Token、隐藏接口、
代理或验证码/风控绕过。真实网络默认关闭，必须由调用方显式打开配置并
提供人工确认；采集结果只返回内存记录和脱敏运行摘要，不写业务数据库。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.config import get_settings
from app.crawler import CrawlError, normalize_product
from app.schemas import ProductRecord


CONTRACT_VERSION = "taobao_search_v1"
_SEARCH_HOST = "s.taobao.com"
_SEARCH_PATH = "/search"
_PRODUCT_HOSTS = {"item.taobao.com", "detail.tmall.com"}
_PRODUCT_PATH = "/item.htm"
_APPROVED_CDN_PATHS = {
    "g.alicdn.com": ("/main-search/", "/alilog/"),
    "o.alicdn.com": ("/tbhome/tbnav/", "/tbpc/securitySDK/", "/tbpc/traceSDK/"),
    "img.alicdn.com": ("/imgextra/",),
}
_ALLOWED_NEXT_KEYS = {"q", "s", "page", "pageNo", "page_num"}
_BLOCKED_MARKERS = (
    "验证码",
    "人机验证",
    "安全验证",
    "请登录",
    "登录后查看",
    "访问受限",
    "访问太频繁",
    "异常访问",
    "风险",
    "captcha",
    "robot check",
    "access denied",
)
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_PRICE_RANGE = re.compile(r"(?:[¥￥]\s*)?\d[\d,.]*\s*[-~至]\s*(?:[¥￥]\s*)?\d")


DataCompleteness = Literal["complete", "partial"]


class CatalogLiveError(CrawlError):
    """真实公开页面采集的稳定错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        pages_seen: int = 0,
        data_completeness: DataCompleteness = "complete",
        artifact_refs: list[str] | None = None,
    ) -> None:
        super().__init__(code, message)
        self.retryable = retryable
        self.pages_seen = pages_seen
        self.data_completeness = data_completeness
        self.artifact_refs = artifact_refs or []


@dataclass(frozen=True)
class CatalogLiveLimits:
    max_pages: int = 1
    min_delay_seconds: float = 3.0
    page_timeout_seconds: int = 20
    total_timeout_seconds: int = 90
    max_products: int = 100

    def validate(self) -> None:
        if self.max_pages < 1 or self.max_pages > 3:
            raise CatalogLiveError("INVALID_REPLAY_INPUT", "max_pages 必须在 1 到 3 之间")
        if self.min_delay_seconds < 1 or self.min_delay_seconds > 60:
            raise CatalogLiveError("INVALID_REPLAY_INPUT", "min_delay_seconds 必须在 1 到 60 之间")
        if self.page_timeout_seconds < 1 or self.page_timeout_seconds > 60:
            raise CatalogLiveError("INVALID_REPLAY_INPUT", "page_timeout_seconds 必须在 1 到 60 之间")
        if self.total_timeout_seconds < self.page_timeout_seconds or self.total_timeout_seconds > 300:
            raise CatalogLiveError("INVALID_REPLAY_INPUT", "total_timeout_seconds 必须不小于单页超时且不超过 300 秒")
        if self.max_products < 1 or self.max_products > 500:
            raise CatalogLiveError("INVALID_REPLAY_INPUT", "max_products 必须在 1 到 500 之间")


@dataclass
class CatalogLiveResult:
    records: list[ProductRecord]
    pages_seen: int
    data_completeness: DataCompleteness
    run_id: str
    artifact_refs: list[str] = field(default_factory=list)
    http_statuses: list[int] = field(default_factory=list)


def _clean_keyword(keyword: str) -> str:
    if not isinstance(keyword, str):
        raise CatalogLiveError("INVALID_REPLAY_INPUT", "keyword 必须是字符串")
    value = keyword.strip()
    if not value:
        raise CatalogLiveError("INVALID_REPLAY_INPUT", "keyword 不能为空")
    if _CONTROL_CHARS.search(value):
        raise CatalogLiveError("INVALID_REPLAY_INPUT", "keyword 不能包含控制字符")
    if len(value) > 200:
        raise CatalogLiveError("INVALID_REPLAY_INPUT", "keyword 不能超过 200 个字符")
    return value


def build_taobao_search_url(keyword: str) -> str:
    """根据关键词构造唯一、可审计的淘宝公开搜索 URL。"""
    value = _clean_keyword(keyword)
    return f"https://{_SEARCH_HOST}{_SEARCH_PATH}?{urlencode({'q': value}, quote_via=quote)}"


def _validate_search_parts(url: str, expected_keyword: str, *, allow_paging: bool) -> str:
    keyword = _clean_keyword(expected_keyword)
    if not isinstance(url, str) or len(url) > 4096:
        raise CatalogLiveError("INVALID_SEARCH_URL", "搜索 URL 无效")
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or parsed.hostname != _SEARCH_HOST:
        raise CatalogLiveError("INVALID_SEARCH_URL", "搜索 URL 必须是 s.taobao.com 的 HTTPS 地址")
    try:
        port = parsed.port
    except ValueError as exc:
        raise CatalogLiveError("INVALID_SEARCH_URL", "搜索 URL 端口无效") from exc
    if port not in (None, 443) or parsed.username or parsed.password or parsed.fragment:
        raise CatalogLiveError("INVALID_SEARCH_URL", "搜索 URL 包含不允许的端口、凭证或 fragment")
    if parsed.path != _SEARCH_PATH:
        raise CatalogLiveError("INVALID_SEARCH_URL", "搜索 URL path 必须是 /search")
    if _CONTROL_CHARS.search(url):
        raise CatalogLiveError("INVALID_SEARCH_URL", "搜索 URL 不能包含控制字符")

    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if not pairs:
        raise CatalogLiveError("INVALID_SEARCH_URL", "搜索 URL 缺少 q 参数")
    q_values = [value for key, value in pairs if key == "q"]
    if len(q_values) != 1 or q_values[0] != keyword:
        raise CatalogLiveError("INVALID_SEARCH_URL", "搜索 URL 的 q 参数与 keyword 不一致")
    allowed = {"q"} | (_ALLOWED_NEXT_KEYS - {"q"} if allow_paging else set())
    unknown = {key for key, _value in pairs if key not in allowed}
    if unknown:
        raise CatalogLiveError("INVALID_SEARCH_URL", "搜索 URL 包含不允许的查询参数")
    if not allow_paging and len(pairs) != 1:
        raise CatalogLiveError("INVALID_SEARCH_URL", "初始搜索 URL 只能包含一个 q 参数")

    for key, value in pairs:
        if key == "q":
            continue
        if not value.isdigit() or int(value) < 0 or int(value) > 100000:
            raise CatalogLiveError("INVALID_SEARCH_URL", "分页参数必须是有限的非负整数")
    return urlunparse(("https", _SEARCH_HOST, _SEARCH_PATH, "", urlencode(pairs, doseq=True, quote_via=quote), ""))


def validate_search_url(url: str, expected_keyword: str) -> str:
    return _validate_search_parts(url, expected_keyword, allow_paging=False)


def validate_next_url(url: str, expected_keyword: str) -> str:
    return _validate_search_parts(url, expected_keyword, allow_paging=True)


def canonicalize_product_url(url: str) -> tuple[str, str]:
    """校验商品链接并只保留明确的数字商品 ID。"""
    if not isinstance(url, str) or len(url) > 2048:
        raise CatalogLiveError("INVALID_PRODUCT", "商品链接无效")
    parsed = urlparse(url)
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname not in _PRODUCT_HOSTS
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.path != _PRODUCT_PATH
    ):
        raise CatalogLiveError("INVALID_PRODUCT", "商品链接不在允许的淘宝/天猫详情域名内")
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    ids = [value for key, value in pairs if key == "id"]
    if len(ids) != 1 or not ids[0].isdigit() or not (1 <= int(ids[0]) <= 10**20):
        raise CatalogLiveError("INVALID_PRODUCT", "商品链接缺少明确的数字 id")
    product_id = ids[0]
    return f"https://{parsed.hostname}{_PRODUCT_PATH}?id={product_id}", product_id


def _safe_text(value: str | None, *, max_length: int = 255) -> str:
    value = _CONTROL_CHARS.sub(" ", value or "")
    return " ".join(value.split())[:max_length].strip()


def _parse_visible_rows(rows: list[dict[str, Any]], *, page_url: str) -> list[ProductRecord]:
    """把已由 DOM 明确抽出的行转换为领域记录；不猜测字段。"""
    if not rows:
        raise CatalogLiveError("DOM_SCHEMA_MISMATCH", "未找到符合 taobao_search_v1 的可见商品卡片")
    records: list[ProductRecord] = []
    seen_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise CatalogLiveError("INVALID_PRODUCT", "商品卡片结构无效")
        raw_url = _safe_text(row.get("url"), max_length=2048)
        canonical_url, product_id = canonicalize_product_url(raw_url)
        title = _safe_text(row.get("title"))
        price = _safe_text(row.get("price"), max_length=64)
        if not title:
            raise CatalogLiveError("INVALID_PRODUCT", "商品标题不能为空")
        if not price or _PRICE_RANGE.search(price) or "券" in price or "起" in price:
            raise CatalogLiveError("PRICE_UNAVAILABLE", "商品价格不是明确的单一公开价格")
        if product_id in seen_ids:
            continue
        seen_ids.add(product_id)
        try:
            records.append(
                normalize_product(
                    {
                        "source": "taobao_catalog_live",
                        "external_product_id": product_id,
                        "title": title,
                        "url": canonical_url,
                        "category": _safe_text(row.get("category"), max_length=128) or None,
                        "description": None,
                        "rating": None,
                        "current_price": price,
                        "currency": "CNY",
                    },
                    canonical_url,
                )
            )
        except CrawlError as exc:
            raise CatalogLiveError("INVALID_PRODUCT", "商品字段校验失败") from exc
    return records


def _extract_visible_rows(page) -> list[dict[str, str | None]]:
    return page.evaluate(
        """
        () => {
          const visible = element => {
            if (!element) return false;
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' &&
              rect.width > 0 && rect.height > 0;
          };
          const text = element => (element?.innerText || '').replace(/\\s+/g, ' ').trim();
          const result = [];
          const links = Array.from(document.querySelectorAll(
            'a[href*="item.taobao.com/item.htm"], a[href*="detail.tmall.com/item.htm"]'
          )).filter(visible);
          for (const link of links) {
            const card = link.closest('[data-product-card], li, [class*="Card"], [class*="card"]') || link.parentElement;
            if (!visible(card)) continue;
            const field = name => card.querySelector(`[data-field="${name}"]`);
            const price = field('price') || card.querySelector('[class*="price"], [class*="Price"]');
            const title = field('title') || card.querySelector('[class*="title"], [class*="Title"]');
            result.push({
              url: link.href || link.getAttribute('href') || '',
              title: title?.getAttribute('title') || text(title) || link.getAttribute('title') || text(link),
              price: text(price),
              category: card.getAttribute('data-category') || '',
            });
          }
          return result;
        }
        """
    )


def _blocked_page(page) -> bool:
    marker = page.locator("body").get_attribute("data-replay-outcome")
    if marker and marker.lower() in {"blocked", "captcha", "login", "rate_limited"}:
        return True
    text = page.locator("body").inner_text().lower()
    return any(marker.lower() in text for marker in _BLOCKED_MARKERS)


def _artifact_ref(artifact_base: Path, path: Path) -> str:
    return path.relative_to(artifact_base).as_posix()


def _write_run_metadata(
    path: Path,
    *,
    run_id: str,
    keyword: str,
    status: str,
    pages_seen: int,
    products_count: int,
    data_completeness: DataCompleteness,
    limits: CatalogLiveLimits,
    http_statuses: list[int],
    error_code: str | None,
    error_message: str | None,
    artifact_refs: list[str],
    blocked_resources: list[str],
    resource_records: list[dict[str, Any]],
) -> None:
    payload = {
        "run_id": run_id,
        "mode": "taobao_catalog_live",
        "contract_version": CONTRACT_VERSION,
        "status": status,
        "pages_seen": pages_seen,
        "products_count": products_count,
        "data_completeness": data_completeness,
        "keyword_sha256": hashlib.sha256(keyword.encode("utf-8")).hexdigest(),
        "keyword_length": len(keyword),
        "source": {"host": _SEARCH_HOST, "path": _SEARCH_PATH},
        "limits": {
            "max_pages": limits.max_pages,
            "min_delay_seconds": limits.min_delay_seconds,
            "page_timeout_seconds": limits.page_timeout_seconds,
            "total_timeout_seconds": limits.total_timeout_seconds,
            "max_products": limits.max_products,
        },
        "allow_network": True,
        "http_statuses": http_statuses,
        "blocked_resources": blocked_resources[:50],
        "resource_records": resource_records[:100],
        "error_code": error_code,
        "error_message": error_message,
        "artifacts": artifact_refs,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def collect_taobao_public_catalog(
    keyword: str,
    *,
    enabled: bool | None = None,
    confirm_public_read_only: bool = False,
    limits: CatalogLiveLimits | None = None,
    artifacts_dir: str | Path | None = None,
    cancel_check: Callable[[], bool] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    headed: bool = False,
    allow_same_origin_public_requests: bool = False,
    allow_approved_cdn: bool = False,
) -> CatalogLiveResult:
    """人工确认后单次读取淘宝公开搜索页，不访问商品详情页。"""
    keyword = _clean_keyword(keyword)
    limits = limits or CatalogLiveLimits(
        page_timeout_seconds=get_settings().crawl_timeout_seconds,
    )
    limits.validate()
    live_enabled = get_settings().taobao_catalog_live_enabled if enabled is None else bool(enabled)
    if not live_enabled:
        raise CatalogLiveError("LIVE_NETWORK_DISABLED", "淘宝真实公开采集开关未启用")
    if not confirm_public_read_only:
        raise CatalogLiveError("LIVE_CONFIRMATION_REQUIRED", "缺少公开只读人工确认")
    if not allow_same_origin_public_requests:
        raise CatalogLiveError("RESOURCE_POLICY_REQUIRED", "未配置允许的公开页面资源策略")
    if not allow_approved_cdn:
        raise CatalogLiveError("CDN_CONFIRMATION_REQUIRED", "未确认使用指定淘宝 CDN 资源")

    start_url = build_taobao_search_url(keyword)
    validate_search_url(start_url, keyword)
    settings = get_settings()
    artifact_base = Path(artifacts_dir) if artifacts_dir else settings.resolve_path(settings.artifacts_dir)
    artifact_base = artifact_base.resolve()
    run_id = uuid4().hex
    run_root = artifact_base / "catalog-live" / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    metadata_path = run_root / "run.json"
    products_path = run_root / "products.json"
    artifact_refs: list[str] = []
    records: list[ProductRecord] = []
    http_statuses: list[int] = []
    pages_seen = 0
    data_completeness: DataCompleteness = "complete"
    status = "failed"
    error: CatalogLiveError | None = None
    browser = context = page = None
    stopping = False
    trace_records: list[dict[str, Any]] = []
    blocked_resources: list[str] = []
    deadline = time.monotonic() + limits.total_timeout_seconds

    def remaining_ms() -> int:
        return max(1, int((deadline - time.monotonic()) * 1000))

    def check_cancel() -> None:
        if cancel_check is not None and cancel_check():
            raise CatalogLiveError("CANCELLED", "采集被取消", pages_seen=pages_seen)
        if time.monotonic() >= deadline:
            raise CatalogLiveError("PAGE_TIMEOUT", "达到采集总时间限制", pages_seen=pages_seen)

    def record_response(response) -> None:
        if len(trace_records) >= 100:
            return
        parsed = urlparse(response.url)
        trace_records.append(
            {
                "resource_type": response.request.resource_type,
                "host": parsed.hostname or "",
                "path": parsed.path,
                "status": response.status,
            }
        )
        if response.request.resource_type == "document":
            http_statuses.append(response.status)

    def route_handler(route) -> None:
        if stopping:
            return
        request = route.request
        parsed = urlparse(request.url)
        headers = {key.lower() for key in request.headers}
        resource_type = request.resource_type
        same_origin = parsed.scheme == "https" and parsed.hostname == _SEARCH_HOST and parsed.port in (None, 443)
        approved_cdn = parsed.scheme == "https" and parsed.hostname in _APPROVED_CDN_PATHS and any(parsed.path.startswith(prefix) for prefix in _APPROVED_CDN_PATHS[parsed.hostname])
        allowed_static = resource_type in {"document", "script", "stylesheet", "image", "font"}
        try:
            if "authorization" in headers or "proxy-authorization" in headers:
                blocked_resources.append("sensitive-header")
                try:
                    route.abort("blockedbyclient")
                except (PlaywrightError, asyncio.CancelledError):
                    pass
                return
            if request.method not in {"GET", "HEAD"} or not allowed_static or not (same_origin or (approved_cdn and allow_approved_cdn)):
                blocked_resources.append(f"{resource_type}:{parsed.hostname or ''}{parsed.path}")
                try:
                    route.abort("blockedbyclient")
                except (PlaywrightError, asyncio.CancelledError):
                    pass
                return
            if resource_type == "document" and parsed.path != _SEARCH_PATH:
                blocked_resources.append(f"document:{parsed.hostname or ''}{parsed.path}")
                try:
                    route.abort("blockedbyclient")
                except (PlaywrightError, asyncio.CancelledError):
                    pass
                return
            try:
                route.continue_()
            except (PlaywrightError, asyncio.CancelledError):
                pass
        except (PlaywrightError, asyncio.CancelledError):
            # The page may be closed while pending resource callbacks finish.
            # Do not turn cleanup races into noisy background tracebacks.
            return

    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=not headed)
            except PlaywrightError as exc:
                code = "BROWSER_NOT_INSTALLED" if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc) else "BROWSER_START_FAILED"
                raise CatalogLiveError(code, "Playwright Chromium 不可用") from exc
            context = browser.new_context(service_workers="block")
            context.route("**/*", route_handler)
            context.tracing.start(screenshots=False, snapshots=False, sources=False)
            page = context.new_page()
            page.set_default_timeout(min(limits.page_timeout_seconds * 1000, remaining_ms()))
            page.on("response", record_response)

            while True:
                check_cancel()
                if pages_seen >= limits.max_pages:
                    data_completeness = "partial"
                    raise CatalogLiveError(
                        "PAGE_LIMIT_EXCEEDED",
                        "达到页面上限但仍存在下一页",
                        pages_seen=pages_seen,
                        data_completeness="partial",
                    )
                response = page.goto(start_url if pages_seen == 0 else next_url, wait_until="domcontentloaded", timeout=min(limits.page_timeout_seconds * 1000, remaining_ms()))
                pages_seen += 1
                if response is None:
                    raise CatalogLiveError("PAGE_LOAD_FAILED", "搜索页没有返回主文档响应", pages_seen=pages_seen)
                status_code = response.status
                if status_code in {401, 403, 407, 451}:
                    raise CatalogLiveError("ACCESS_DENIED", "淘宝拒绝访问公开搜索页", pages_seen=pages_seen)
                if status_code == 429:
                    raise CatalogLiveError("RATE_LIMITED", "淘宝返回限流响应", pages_seen=pages_seen)
                if 400 <= status_code < 500:
                    raise CatalogLiveError("HTTP_STATUS", "淘宝搜索页返回客户端错误", pages_seen=pages_seen)
                if status_code >= 500:
                    raise CatalogLiveError("REMOTE_HTTP_ERROR", "淘宝搜索页返回服务端错误", retryable=True, pages_seen=pages_seen)
                try:
                    if pages_seen == 1:
                        validate_search_url(page.url, keyword)
                    else:
                        validate_next_url(page.url, keyword)
                except CatalogLiveError as exc:
                    if any(marker in page.url.lower() for marker in ("login", "sec", "captcha", "verify")):
                        raise CatalogLiveError("BLOCKED_PAGE", "页面重定向到登录或安全验证页面", pages_seen=pages_seen) from exc
                    raise CatalogLiveError("REDIRECT_NOT_ALLOWED", "搜索页重定向到不允许的地址", pages_seen=pages_seen) from exc
                page.wait_for_load_state("domcontentloaded", timeout=min(limits.page_timeout_seconds * 1000, remaining_ms()))
                check_cancel()
                if _blocked_page(page):
                    stopping = True
                    try:
                        page.wait_for_timeout(750)
                    except PlaywrightError:
                        pass
                    raise CatalogLiveError("BLOCKED_PAGE", "页面出现登录、验证码或访问限制", pages_seen=pages_seen)
                if blocked_resources:
                    stopping = True
                    try:
                        page.wait_for_timeout(750)
                    except PlaywrightError:
                        pass
                    raise CatalogLiveError("RESOURCE_BLOCKED", "页面请求了未允许的资源", pages_seen=pages_seen)
                rows = _extract_visible_rows(page)
                page_records = _parse_visible_rows(rows, page_url=page.url)
                if len(records) + len(page_records) > limits.max_products:
                    raise CatalogLiveError("INVALID_PRODUCT", "商品数量超过本次运行上限", pages_seen=pages_seen)
                existing = {record.external_product_id for record in records}
                records.extend(record for record in page_records if record.external_product_id not in existing)
                next_link = page.locator("a[data-next-page]").first
                next_url = None
                if next_link.count() > 0:
                    href = next_link.get_attribute("href")
                    if href:
                        next_url = validate_next_url(urljoin(page.url, href), keyword)
                if not next_url:
                    break
                if pages_seen >= limits.max_pages:
                    data_completeness = "partial"
                    raise CatalogLiveError("PAGE_LIMIT_EXCEEDED", "达到页面上限但仍存在下一页", pages_seen=pages_seen, data_completeness="partial")
                sleeper(limits.min_delay_seconds)
                check_cancel()

            if not records:
                raise CatalogLiveError("NO_PRODUCTS", "搜索页没有明确的公开商品结果", pages_seen=pages_seen)
            products_path.write_text(
                json.dumps([record.model_dump(mode="json") for record in records], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            artifact_refs.append(_artifact_ref(artifact_base, products_path))
            status = "succeeded"
    except PlaywrightTimeoutError as exc:
        error = CatalogLiveError("PAGE_TIMEOUT", "淘宝公开搜索页加载或解析超时", pages_seen=pages_seen)
        error.__cause__ = exc
    except CatalogLiveError as exc:
        error = exc
        data_completeness = exc.data_completeness
    except PlaywrightError as exc:
        error = CatalogLiveError("LIVE_FAILED", "淘宝公开搜索页浏览器操作失败", pages_seen=pages_seen)
        error.__cause__ = exc
    except Exception as exc:  # noqa: BLE001 - 公开 Canary 必须稳定失败
        error = CatalogLiveError("LIVE_FAILED", "淘宝公开搜索页采集失败", pages_seen=pages_seen)
        error.__cause__ = exc
    finally:
        stopping = True
        if context is not None:
            try:
                context.unroute("**/*")
            except PlaywrightError:
                pass
            try:
                context.tracing.stop()
            except PlaywrightError:
                pass
            try:
                context.close()
            except PlaywrightError:
                pass
        if browser is not None:
            try:
                browser.close()
            except PlaywrightError:
                pass
        if error is not None:
            status = "partial" if error.data_completeness == "partial" else "failed"
        if products_path.is_file() and _artifact_ref(artifact_base, products_path) not in artifact_refs:
            artifact_refs.append(_artifact_ref(artifact_base, products_path))
        try:
            _write_run_metadata(
                metadata_path,
                run_id=run_id,
                keyword=keyword,
                status=status,
                pages_seen=pages_seen,
                products_count=len(records),
                data_completeness=data_completeness,
                limits=limits,
                http_statuses=http_statuses,
                error_code=error.code if error else None,
                error_message=error.message if error else None,
                artifact_refs=artifact_refs,
                blocked_resources=blocked_resources,
                resource_records=trace_records,
            )
            artifact_refs.append(_artifact_ref(artifact_base, metadata_path))
        except OSError:
            if error is None:
                error = CatalogLiveError("LIVE_FAILED", "运行产物写入失败", pages_seen=pages_seen)

    if error is not None:
        error.pages_seen = pages_seen
        error.artifact_refs = artifact_refs
        raise error
    return CatalogLiveResult(records, pages_seen, data_completeness, run_id, artifact_refs, http_statuses)
