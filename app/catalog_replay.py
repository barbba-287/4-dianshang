"""受控的本地商品类目页面回放采集器。

该模块只读取 manifest 明确列出的本地 HTML 文件，不访问真实淘宝/天猫网络，
也不使用 Cookie、Token、隐藏接口或验证码绕过。它用于在未来接入公开竞品
页面前验证选择器、分页、阻断检测和产物契约。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import unquote, urljoin, urlparse
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.config import get_settings
from app.crawler import CrawlError, normalize_product
from app.schemas import ProductRecord


DataCompleteness = Literal["complete", "partial"]


class CatalogReplayError(CrawlError):
    """本地回放错误，带有是否可重试和数据完整度信息。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        data_completeness: DataCompleteness = "complete",
        artifact_refs: list[str] | None = None,
    ) -> None:
        super().__init__(code, message)
        self.retryable = retryable
        self.data_completeness = data_completeness
        self.artifact_refs = artifact_refs or []


@dataclass(frozen=True)
class ReplayLimits:
    max_pages: int = 3
    delay_seconds: float = 1.0
    timeout_seconds: int = 10
    allow_network: bool = False

    def validate(self) -> None:
        if self.max_pages < 1 or self.max_pages > 20:
            raise CatalogReplayError("INVALID_REPLAY_INPUT", "max_pages 必须在 1 到 20 之间")
        if self.delay_seconds < 0 or self.delay_seconds > 60:
            raise CatalogReplayError("INVALID_REPLAY_INPUT", "delay_seconds 必须在 0 到 60 之间")
        if self.timeout_seconds < 1 or self.timeout_seconds > 120:
            raise CatalogReplayError("INVALID_REPLAY_INPUT", "timeout_seconds 必须在 1 到 120 之间")
        if self.allow_network:
            raise CatalogReplayError("NETWORK_DISABLED", "本地回放不允许启用网络")


@dataclass(frozen=True)
class ReplayResult:
    records: list[ProductRecord]
    pages_seen: int
    data_completeness: DataCompleteness
    artifact_refs: list[str]
    run_id: str


_BLOCKED_MARKERS = (
    "验证码",
    "人机验证",
    "安全验证",
    "请登录",
    "登录后查看",
    "访问受限",
    "异常访问",
    "captcha",
    "access denied",
    "robot check",
)


def _relative_ref(artifact_root: Path, path: Path) -> str:
    return path.relative_to(artifact_root.parent.parent).as_posix()


def _safe_child(root: Path, value: str, *, code: str = "SOURCE_NOT_ALLOWED") -> Path:
    if not isinstance(value, str) or not value.strip():
        raise CatalogReplayError("INVALID_REPLAY_INPUT", "页面路径不能为空")
    parsed = urlparse(value)
    if parsed.scheme and parsed.scheme != "file":
        raise CatalogReplayError("NETWORK_DISABLED", "本地回放拒绝远程页面")
    is_file_uri = parsed.scheme == "file"
    if is_file_uri:
        # On Windows, file:///C:/... is parsed as /C:/...; remove the
        # synthetic leading slash before converting it to a local path.
        file_path = unquote(parsed.path)
        if len(file_path) >= 3 and file_path[0] == "/" and file_path[2] == ":":
            file_path = file_path[1:]
        candidate = Path(file_path)
        resolved = candidate.resolve()
    else:
        candidate = Path(value)
        # A page link produced by urljoin can be an absolute file path. It is
        # still accepted only after the root containment check below.
        resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise CatalogReplayError(code, "页面路径超出 manifest root") from exc
    if not resolved.is_file():
        raise CatalogReplayError("INVALID_REPLAY_INPUT", "页面文件不存在")
    return resolved


def _load_manifest(manifest_path: str | Path) -> tuple[Path, Path, dict]:
    path = Path(manifest_path).resolve()
    if not path.is_file():
        raise CatalogReplayError("INVALID_REPLAY_INPUT", "manifest 文件不存在")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogReplayError("INVALID_REPLAY_INPUT", "manifest 无效") from exc
    if not isinstance(payload, dict) or payload.get("mode") != "local_html_replay":
        raise CatalogReplayError("INVALID_REPLAY_INPUT", "manifest mode 必须是 local_html_replay")
    root_value = payload.get("root", ".")
    if not isinstance(root_value, str) or Path(root_value).is_absolute():
        raise CatalogReplayError("INVALID_REPLAY_INPUT", "manifest root 必须是相对路径")
    root = (path.parent / root_value).resolve()
    if not root.is_dir():
        raise CatalogReplayError("INVALID_REPLAY_INPUT", "manifest root 目录不存在")
    entrypoint = payload.get("entrypoint")
    if not isinstance(entrypoint, str):
        raise CatalogReplayError("INVALID_REPLAY_INPUT", "manifest 缺少 entrypoint")
    return path, root, {**payload, "entrypoint": entrypoint}


def _extract_page_products(page) -> list[dict[str, str | None]]:
    return page.evaluate(
        """
        () => Array.from(document.querySelectorAll('[data-product-card]')).map(card => {
          const field = name => card.querySelector(`[data-field="${name}"]`);
          const link = field('url');
          return {
            external_product_id: card.getAttribute('data-product-id'),
            title: field('title')?.innerText || '',
            url: link?.getAttribute('href') || '',
            category: card.getAttribute('data-category'),
            description: field('description')?.innerText || '',
            rating: card.getAttribute('data-rating'),
            current_price: field('price')?.innerText || '',
            currency: card.getAttribute('data-currency') || 'CNY',
          };
        })
        """
    )


def _blocked(page) -> bool:
    marker = page.locator("body").get_attribute("data-replay-outcome")
    if marker and marker.lower() in {"blocked", "captcha", "login", "rate_limited"}:
        return True
    text = page.locator("body").inner_text().lower()
    return any(marker.lower() in text for marker in _BLOCKED_MARKERS)


def _next_page_path(page) -> str | None:
    locator = page.locator("a[data-next-page]").first
    if locator.count() == 0:
        return None
    return locator.get_attribute("href")


def _canonical_product(raw: dict[str, str | None], *, page_url: str) -> ProductRecord:
    required = {
        "external_product_id": "商品 ID",
        "title": "商品标题",
        "url": "商品链接",
        "current_price": "商品价格",
    }
    for key, label in required.items():
        if not (raw.get(key) or "").strip():
            raise CatalogReplayError("INVALID_PRODUCT", f"{label}不能为空")
    try:
        return normalize_product(
            {**raw, "source": "catalog_replay"},
            "https://replay.local",
        )
    except CrawlError as exc:
        raise CatalogReplayError(
            "INVALID_PRODUCT",
            f"商品字段校验失败（页面 {page_url}）",
        ) from exc


def replay_catalog(
    manifest_path: str | Path,
    *,
    limits: ReplayLimits | None = None,
    artifacts_dir: str | Path | None = None,
    cancel_check: Callable[[], bool] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> ReplayResult:
    """在本地 manifest 页面上执行只读商品回放。"""
    limits = limits or ReplayLimits(timeout_seconds=get_settings().crawl_timeout_seconds)
    limits.validate()
    _manifest, root, manifest = _load_manifest(manifest_path)
    current = _safe_child(root, manifest["entrypoint"])

    settings = get_settings()
    artifact_base = Path(artifacts_dir) if artifacts_dir else settings.resolve_path(settings.artifacts_dir)
    run_id = uuid4().hex
    artifact_root = artifact_base / "catalog-replay" / run_id
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_refs: list[str] = []
    records: list[ProductRecord] = []
    seen_ids: set[str] = set()
    visited: set[Path] = set()
    pages_seen = 0
    status = "failed"
    data_completeness: DataCompleteness = "complete"
    error_code: str | None = None
    error_message: str | None = None
    browser = context = page = None
    trace_started = False

    def add_ref(path: Path) -> None:
        try:
            artifact_refs.append(_relative_ref(artifact_root, path))
        except ValueError:
            artifact_refs.append(path.name)

    def write_run() -> None:
        run_path = artifact_root / "run.json"
        run_path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "mode": "local_html_replay",
                    "status": status,
                    "pages_seen": pages_seen,
                    "products_count": len(records),
                    "data_completeness": data_completeness,
                    "max_pages": limits.max_pages,
                    "delay_seconds": limits.delay_seconds,
                    "allow_network": False,
                    "error_code": error_code,
                    "error_message": error_message,
                    "artifacts": artifact_refs,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        add_ref(run_path)

    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except PlaywrightError as exc:
                raise CatalogReplayError(
                    "BROWSER_NOT_INSTALLED" if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc) else "BROWSER_START_FAILED",
                    "Playwright Chromium 不可用",
                ) from exc
            context = browser.new_context()
            blocked_external = False

            def route_handler(route) -> None:
                nonlocal blocked_external
                if route.request.url.startswith("file://"):
                    route.continue_()
                else:
                    blocked_external = True
                    route.abort()

            context.route("**/*", route_handler)
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            trace_started = True
            page = context.new_page()

            while True:
                if cancel_check is not None and cancel_check():
                    raise CatalogReplayError("CANCELLED", "采集被取消")
                if current in visited:
                    raise CatalogReplayError("PAGINATION_LOOP", "分页页面重复")
                if pages_seen >= limits.max_pages:
                    data_completeness = "partial"
                    raise CatalogReplayError(
                        "PAGE_LIMIT_EXCEEDED",
                        "达到页面上限但仍存在下一页",
                        data_completeness="partial",
                    )
                visited.add(current)
                pages_seen += 1
                page.goto(current.as_uri(), wait_until="domcontentloaded", timeout=limits.timeout_seconds * 1000)
                if blocked_external:
                    raise CatalogReplayError("NETWORK_DISABLED", "本地回放页面尝试访问外部资源")
                if _blocked(page):
                    raise CatalogReplayError("BLOCKED_PAGE", "页面出现登录、验证码或访问限制")
                screenshot = artifact_root / f"page-{pages_seen:03d}.png"
                page.screenshot(path=str(screenshot), full_page=True)
                add_ref(screenshot)

                raw_products = _extract_page_products(page)
                for raw in raw_products:
                    record = _canonical_product(raw, page_url=current.as_uri())
                    if record.external_product_id in seen_ids:
                        continue
                    seen_ids.add(record.external_product_id)
                    records.append(record)

                next_href = _next_page_path(page)
                if not next_href:
                    break
                # Keep pagination relative to the current validated local file.
                # urljoin() produces a Windows file URI whose parsed path starts
                # with /C:/; normalize it before applying root containment.
                next_url = urljoin(current.as_uri(), next_href)
                next_parsed = urlparse(next_url)
                if next_parsed.scheme != "file":
                    raise CatalogReplayError("NETWORK_DISABLED", "分页链接必须指向本地文件")
                next_file_path = unquote(next_parsed.path)
                if len(next_file_path) >= 3 and next_file_path[0] == "/" and next_file_path[2] == ":":
                    next_file_path = next_file_path[1:]
                current = _safe_child(root, Path(next_file_path).as_uri())
                if limits.delay_seconds:
                    sleeper(limits.delay_seconds)

            if not records:
                raise CatalogReplayError("NO_PRODUCTS", "回放页面未找到商品")
            raw_path = artifact_root / "raw-products.json"
            raw_path.write_text(
                json.dumps([record.model_dump(mode="json") for record in records], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            add_ref(raw_path)
            status = "succeeded"
            write_run()
            return ReplayResult(records, pages_seen, data_completeness, artifact_refs, run_id)
    except PlaywrightTimeoutError as exc:
        error_code = "PAGE_TIMEOUT"
        error_message = "页面加载超时"
        raise CatalogReplayError(error_code, error_message, artifact_refs=artifact_refs) from exc
    except CatalogReplayError as exc:
        error_code = exc.code
        error_message = exc.message
        data_completeness = exc.data_completeness
        if page is not None:
            screenshot = artifact_root / f"failure-page-{max(pages_seen, 1):03d}.png"
            try:
                page.screenshot(path=str(screenshot), full_page=True)
                add_ref(screenshot)
            except PlaywrightError:
                pass
        raise CatalogReplayError(
            exc.code,
            exc.message,
            retryable=exc.retryable,
            data_completeness=exc.data_completeness,
            artifact_refs=artifact_refs,
        ) from exc
    except PlaywrightError as exc:
        error_code = "PAGE_LOAD_FAILED"
        error_message = "本地页面加载失败"
        raise CatalogReplayError(error_code, error_message, artifact_refs=artifact_refs) from exc
    finally:
        if trace_started and context is not None:
            trace_path = artifact_root / "trace.zip"
            try:
                context.tracing.stop(path=str(trace_path))
                add_ref(trace_path)
            except PlaywrightError:
                pass
        if context is not None:
            try:
                context.close()
            except PlaywrightError:
                pass
        if browser is not None:
            try:
                browser.close()
            except PlaywrightError:
                pass
        if status != "succeeded":
            try:
                write_run()
            except OSError:
                pass
