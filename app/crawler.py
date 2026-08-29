"""电商商品与客服工作台的 Playwright 采集模块。

用途：从本地 HTML fixture（后续可扩展为公开或获授权页面）提取商品字段，
完成标准化、质量校验、原始快照保存，并在失败时输出截图和 Trace。
"""

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urljoin
from uuid import uuid4

from pydantic import ValidationError
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.config import get_settings
from app.schemas import ProductRecord


class CrawlError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _parse_price(value: str) -> Decimal:
    cleaned = value.replace(",", "").replace("￥", "").replace("¥", "").strip()
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise CrawlError("INVALID_PRICE", f"无法解析价格: {value}") from exc


def _parse_rating(value: str | None) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(value.strip().replace("分", ""))
    except InvalidOperation as exc:
        raise CrawlError("INVALID_RATING", f"无法解析评分: {value}") from exc


def normalize_product(raw: dict[str, str | None], base_url: str) -> ProductRecord:
    url = urljoin(base_url, raw.get("url") or "")
    try:
        return ProductRecord(
            source=raw.get("source") or "fixture",
            external_product_id=(raw.get("external_product_id") or "").strip(),
            title=(raw.get("title") or "").strip(),
            url=url,
            category=(raw.get("category") or "").strip() or None,
            description=(raw.get("description") or "").strip() or None,
            rating=_parse_rating(raw.get("rating")),
            current_price=_parse_price(raw.get("current_price") or ""),
            currency=(raw.get("currency") or "CNY").strip().upper(),
            observed_at=datetime.utcnow(),
        )
    except CrawlError:
        raise
    except ValidationError as exc:
        raise CrawlError("VALIDATION_ERROR", str(exc)) from exc


def _write_raw_snapshot(artifact_dir: Path, raw_products: list[dict[str, str | None]]) -> None:
    snapshot = artifact_dir / f"raw-products-{uuid4().hex}.json"
    snapshot.write_text(
        json.dumps(raw_products, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def collect_fixture(path: str | Path, *, attempt: int = 0) -> list[ProductRecord]:
    settings = get_settings()
    fixture_path = Path(path).resolve()
    artifact_dir = settings.resolve_path(settings.artifacts_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_id = uuid4().hex

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:
            message = str(exc)
            if "Executable doesn't exist" in message or "playwright install" in message:
                raise CrawlError(
                    "BROWSER_NOT_INSTALLED",
                    "Playwright 浏览器未安装，请执行: python -m playwright install chromium",
                ) from exc
            raise CrawlError("BROWSER_START_FAILED", message) from exc

        context = browser.new_context()
        trace_started = False
        page = None
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            trace_started = True
            page = context.new_page()
            page.goto(
                fixture_path.as_uri(),
                wait_until="domcontentloaded",
                timeout=settings.crawl_timeout_seconds * 1000,
            )
            cards = page.locator("[data-product-card]")
            raw_products: list[dict[str, str | None]] = []
            for index in range(cards.count()):
                card = cards.nth(index)
                raw_products.append(
                    {
                        "source": card.get_attribute("data-source") or "fixture",
                        "external_product_id": card.get_attribute("data-product-id"),
                        "title": card.locator("[data-field='title']").inner_text(),
                        "url": card.locator("a[data-field='url']").get_attribute("href"),
                        "category": card.get_attribute("data-category"),
                        "description": card.locator("[data-field='description']").inner_text(),
                        "rating": card.get_attribute("data-rating"),
                        "current_price": card.locator("[data-field='price']").inner_text(),
                        "currency": card.get_attribute("data-currency") or "CNY",
                    }
                )
            if not raw_products:
                raise CrawlError("NO_PRODUCTS", "页面未找到商品卡片")
            _write_raw_snapshot(artifact_dir, raw_products)
            return [normalize_product(raw, "https://fixture.local") for raw in raw_products]
        except PlaywrightTimeoutError as exc:
            if page is not None:
                page.screenshot(
                    path=str(artifact_dir / f"crawl-timeout-{artifact_id}-{attempt}.png"),
                    full_page=True,
                )
            raise CrawlError("PAGE_TIMEOUT", "页面加载超时") from exc
        except CrawlError:
            if page is not None:
                page.screenshot(
                    path=str(artifact_dir / f"crawl-failure-{artifact_id}-{attempt}.png"),
                    full_page=True,
                )
            raise
        except PlaywrightError as exc:
            if page is not None:
                page.screenshot(
                    path=str(artifact_dir / f"crawl-failure-{artifact_id}-{attempt}.png"),
                    full_page=True,
                )
            raise CrawlError("SELECTOR_ERROR", str(exc)) from exc
        except Exception as exc:
            if page is not None:
                page.screenshot(
                    path=str(artifact_dir / f"crawl-failure-{artifact_id}-{attempt}.png"),
                    full_page=True,
                )
            raise CrawlError("CRAWL_FAILED", str(exc)) from exc
        finally:
            if trace_started:
                context.tracing.stop(
                    path=str(artifact_dir / f"crawl-{artifact_id}-{attempt}.zip")
                )
            context.close()
            browser.close()
