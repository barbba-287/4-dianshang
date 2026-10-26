"""电商商品与客服工作台的 FastAPI 应用入口。

用途：提供商品采集、分页筛选、商品详情、价格历史、任务状态和健康检查
接口，并承载第一周演示用的极简网页。S1 新增异步任务提交 / 详情 / 取
消接口，保留旧 fixture 同步入口作为演示兼容路径。S2 新增文档上传与版
本列表接口。
"""

from contextlib import asynccontextmanager
from pathlib import Path
from datetime import date, datetime
from typing import Literal

from hashlib import sha256
import html
import json
import logging
import re
import threading
import time
import uuid
from urllib.parse import quote

from fastapi import Body, Depends, File, Form, FastAPI, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.background import (
    is_accepting_jobs,
    request_cancel_job,
    start_background_workers,
    stop_background_workers,
    submit_crawl_fixture,
    submit_document_import,
)
from app.config import get_settings
from app.crawler import CrawlError, collect_fixture
from app.db import (
    CrawlJob,
    Document,
    DocumentVersion,
    Product,
    ProductPriceHistory,
    ProductSku,
    ProductMediaAsset,
    ProductContentRevision,
    ProductContentExport,
    ProductContentAction,
    Warehouse,
    InboundOrder,
    UserAccount,
    Workspace,
    WorkspaceMembership,
    WarehouseAccess,
    get_db,
    init_db,
)
from app.errors import DomainError
from app.importers import ParserError, detect_source_type
from app.jobs import JobStatus
from app.repository import (
    count_chunks,
    fail_job,
    import_records,
    list_inventory,
    list_versions_for_document,
    mark_version_failed,
    start_job,
    upsert_document,
    upsert_document_version,
)
from app.schemas import (
    AgentActionRequest,
    AgentActionResponse,
    AgentToolSpec,
    AgentAssistantRequest,
    AgentAssistantTrace,
    AgentAssistantResponse,
    AssistantQueryRequest,
    AssistantQueryResponse,
    CrawlJobDetailResponse,
    CrawlJobResponse,
    DocumentUploadResponse,
    DocumentVersionResponse,
    PriceHistoryResponse,
    ProductPage,
    ProductResponse,
    RagCitation,
    RagQueryRequest,
    RagQueryResponse,
    SettingsResponse,
    WarehouseCreate,
    WarehouseResponse,
    ProductSkuCreate,
    ProductSkuResponse,
    InboundCreate,
    InboundReceive,
    InboundResponse,
    InventoryPage,
    InboundListPage,
    InboundListItem,
    ExternalEventIngestRequest,
    ExternalEventIngestResponse,
    ExternalOrderPreviewRequest,
    ExternalOrderPreviewResponse,
    ExternalOrderIngestRequest,
    ExternalOrderIngestResponse,
    ExternalOrderResponse,
    ExternalOrderLineResponse,
    ExternalOrderListItem,
    ExternalOrderPage,
    ExternalOrderDetailResponse,
    ExternalProductMappingRequest,
    ExternalProductMappingResponse,
    ExternalAccountResponse,
    DailySkuSalePage,
    ReplenishmentGenerateRequest,
    ReplenishmentDecisionRequest,
    ReplenishmentSuggestionResponse,
    ReplenishmentSuggestionPage,
    ReplenishmentEvaluationPage,
    ReplenishmentEvaluationResponse,
    ProductQuadrantItem,
    ProductQuadrantPage,
    ProductQuadrantResponse,
    PurchaseRequestCreate,
    PurchaseRequestDraftCreate,
    PurchaseRequestDraftUpdate,
    PurchaseRequestDraftSubmit,
    PurchaseRequestResponse,
    PurchaseRequestLineResponse,
    PurchaseRequestPage,
    TaobaoCapabilitiesResponse,
    TaobaoPreviewRequest,
    TaobaoPreviewResponse,
    FixtureSyncRequest,
    FixtureSyncResponse,
    ShopifyPreviewRequest,
    ShopifyPreviewResponse,
    ExternalInventoryIngestRequest,
    ExternalInventoryIngestResponse,
    ExternalInventoryPreviewRequest,
    ExternalInventoryPreviewResponse,
    DashboardSummaryResponse,
    ExternalSyncRetryRequest,
    ExternalSyncRunResponse,
    InventoryPolicyCreate,
    InventoryPolicyResponse,
    AdminUserCreate,
    AdminUserResponse,
    AdminUserPatch,
    AdminUserPasswordReset,
    WarehouseAccessRequest,
    AlertResponse,
    ContentAssetResponse,
    ContentRevisionResponse,
    ContentJobCreate,
    ContentJobResponse,
    ContentReviewRequest,
    ContentExportRequest,
    ContentExportResponse,
    OperationsTodayResponse,
    TicketResponse,
    TicketPage,
    TicketCreateRequest,
    TicketEventRequest,
    TicketTransitionRequest,
    TicketAssignRequest,
)

from app.storage import StorageError, enforce_size_limit, save_upload
from app.versioning import content_sha256
from app.upload_security import validate_content, safe_storage_path
from app.employee_auth import (
    ROLE_ADMIN,
    ROLE_CUSTOMER_SERVICE,
    ROLE_OPERATIONS,
    ROLE_READONLY,
    ROLE_WAREHOUSE,
    authenticate_api_key,
    create_session,
    demo_principal,
    hash_password,
    principal_from_session,
    require_principal,
    require_warehouse_access,
    validate_auth_config,
    validate_csrf,
    verify_password,
)
from app.request_context import RequestContext
from app.runtime_flags import live_enabled as runtime_live_enabled
from app.runtime_flags import set_live_enabled as set_runtime_live_enabled


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    validate_auth_config(get_settings())
    start_background_workers()
    try:
        yield
    finally:
        stop_background_workers()


app = FastAPI(title=get_settings().app_name, version="0.2.0-week2", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


_WORKSPACE_NAV_ITEMS = (
    ("/dashboard", "运营驾驶舱", "▦", (ROLE_ADMIN, ROLE_OPERATIONS), "inventory.read"),
    ("/orders", "订单事实", "▣", (ROLE_ADMIN, ROLE_OPERATIONS), "inventory.read"),
    ("/ops", "运营工作台", "◫", (ROLE_ADMIN, ROLE_OPERATIONS), None),
    ("/content", "商品页生产", "✦", (ROLE_ADMIN, ROLE_OPERATIONS), "content.read"),
    ("/shopify", "Shopify 接入", "◇", (ROLE_ADMIN, ROLE_OPERATIONS), "inventory.read"),
    ("/assistant", "AI 运营助手", "✦", (ROLE_ADMIN, ROLE_OPERATIONS), "analytics.read"),
    ("/warehouse", "仓库收货", "▤", (ROLE_ADMIN, ROLE_OPERATIONS, ROLE_WAREHOUSE), "inbound.read"),
    ("/admin/users", "成员权限", "♙", (ROLE_ADMIN,), None),
)
_SYSTEM_NAV_ITEMS = (
    ("/customer-service", "客服知识", "?", (ROLE_ADMIN, ROLE_OPERATIONS, ROLE_CUSTOMER_SERVICE, ROLE_READONLY), "knowledge.read"),
)
_PAGE_ANCHORS = {
    "/dashboard": (
        ("sales-trend-panel", "销量趋势"),
        ("inventory-risk-panel", "库存风险"),
        ("product-quadrant-panel", "商品表现四象限"),
        ("sync-health-panel", "数据同步健康"),
        ("sku-health-panel", "SKU 库存健康"),
        ("alerts-panel", "异常收件箱"),
        ("recent-inbounds-panel", "最近入库"),
        ("metric-definitions-panel", "指标口径"),
    ),
    "/ops": (
        ("product-materials-panel", "商品资料"),
        ("warehouse-step-base-data", "基础资料"),
        ("warehouse-step-expected-inbound", "创建预计入库"),
        ("warehouse-step-confirmation", "运营确认入账"),
        ("warehouse-step-inventory", "当前库存"),
        ("purchase-workflow", "补货与采购"),
    ),
}


def _visible_navigation(principal, active_path: str) -> str:
    def can_see(roles: tuple[str, ...], permission: str | None) -> bool:
        if getattr(principal, "auth_type", None) == "demo":
            return True
        return principal.has_role(*roles) and (permission is None or principal.can(permission))

    def render_item(item: tuple[str, str, str, tuple[str, ...], str | None]) -> str:
        path, label, icon, roles, permission = item
        if not can_see(roles, permission):
            return ""
        active = ' class="nav-page-link active" aria-current="page"' if path == active_path else ' class="nav-page-link"'
        anchors = _PAGE_ANCHORS.get(path, ()) if path == active_path else ()
        anchor_html = ''.join(f'<a class="nav-sub nav-anchor" href="{path}#{anchor_id}">{anchor_label}</a>' for anchor_id, anchor_label in anchors)
        return f'<a href="{path}"{active} aria-label="{label}"><span class="nav-icon" aria-hidden="true">{icon}</span><span class="nav-text">{label}</span></a>{anchor_html}'

    nav_by_path = {item[0]: item for item in _WORKSPACE_NAV_ITEMS}
    groups = (
        ("运营分析", "Workspace", tuple(nav_by_path[path] for path in ("/dashboard", "/orders", "/ops", "/assistant", "/content", "/shopify") if path in nav_by_path)),
        ("仓储履约", "Warehouse", tuple(nav_by_path[path] for path in ("/warehouse",) if path in nav_by_path)),
        ("系统管理", "System", tuple(nav_by_path[path] for path in ("/admin/users",) if path in nav_by_path)),
        ("客服知识", "Knowledge", _SYSTEM_NAV_ITEMS),
    )
    rendered_groups = []
    for group_label, group_id, items in groups:
        links = "".join(render_item(item) for item in items)
        if not links:
            continue
        is_open = any(item[0] == active_path for item in items)
        open_attr = " open" if is_open else ""
        rendered_groups.append(
            f'<details class="nav-group" id="nav-group-{group_id.lower()}"{open_attr}>'
            f'<summary class="nav-group-toggle">{group_label}</summary>'
            f'<nav class="nav" aria-label="{group_label}导航">{links}</nav></details>'
        )
    return '<a class="brand" href="/ops"><span class="brand-mark">商</span><span class="brand-text">电商运营台</span></a>' + "".join(rendered_groups)


def _render_workspace_page(template_name: str, principal, active_path: str) -> HTMLResponse:
    template = (Path(__file__).parent / "templates" / template_name).read_text(encoding="utf-8")
    navigation = _visible_navigation(principal, active_path)
    template = re.sub(
        r'<aside class="sidebar">.*?</aside>',
        f'<aside class="sidebar">{navigation}</aside>',
        template,
        count=1,
        flags=re.DOTALL,
    )
    return HTMLResponse(content=template, headers={"Cache-Control": "no-store, max-age=0"})


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or f"req-{uuid.uuid4().hex[:12]}"
    request.state.request_id = request_id
    settings = get_settings()
    session_principal = None
    if settings.employee_auth_enabled and request.cookies.get(settings.session_cookie_name):
        with get_db_session_for_health() as auth_db:
            session_principal = principal_from_session(
                auth_db, request.cookies.get(settings.session_cookie_name)
            )

    if request.url.path.startswith("/api/"):
        with get_db_session_for_health() as auth_db:
            principal = session_principal or authenticate_api_key(
                request.headers.get("X-API-Key"), settings, db=auth_db
            )
        if settings.employee_auth_enabled and (
            principal is None or getattr(principal, "auth_type", None) in ("demo", "api_key")
        ):
            response = JSONResponse(
                status_code=401,
                content={"detail": {"code": "UNAUTHORIZED", "message": "认证失败"}},
                headers={"WWW-Authenticate": "ApiKey"},
            )
            response.headers["X-Request-ID"] = request_id
            return response
        if principal is None:
            response = JSONResponse(
                status_code=401,
                content={"detail": {"code": "UNAUTHORIZED", "message": "认证失败"}},
                headers={"WWW-Authenticate": "ApiKey"},
            )
            response.headers["X-Request-ID"] = request_id
            return response
        request.state.principal = principal
        request.state.ctx = RequestContext(request_id=request_id, principal=principal)
    else:
        public_paths = {"/login", "/health", "/ready", "/docs", "/openapi.json", "/redoc"}
        is_public_path = request.url.path in public_paths or request.url.path.startswith("/static/")
        if settings.employee_auth_enabled and not is_public_path:
            if session_principal is None:
                return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)
            request.state.principal = session_principal
            request.state.ctx = RequestContext(request_id=request_id, principal=session_principal)
        else:
            principal = session_principal or demo_principal(settings)
            request.state.principal = principal
            request.state.ctx = RequestContext(request_id=request_id, principal=principal)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path != "/login":
        try:
            with get_db_session_for_health() as csrf_db:
                validate_csrf(request, csrf_db)
        except DomainError as exc:
            response = JSONResponse(
                status_code=exc.http_status,
                content={"detail": {"code": exc.code, "message": exc.message}},
            )
            response.headers["X-Request-ID"] = request_id
            return response
    try:
        response = await call_next(request)
    except Exception as exc:
        logger.exception("unhandled request error: %s", exc, extra={"request_id": request_id})
        response = JSONResponse(
            status_code=500,
            content={"detail": {"code": "INTERNAL_ERROR", "message": "服务内部错误"}},
        )
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.exception_handler(DomainError)
async def domain_error_handler(_request: Request, exc: DomainError):
    """把领域异常映射为统一错误体：{"detail": {"code", "message"}}。

    响应外形与迁移前 FastAPI 对 HTTPException 的默认处理逐字节一致。
    """
    return JSONResponse(
        status_code=exc.http_status,
        content={"detail": {"code": exc.code, "message": exc.message}},
    )


@app.get("/ready")
def ready() -> dict[str, str]:
    """依赖就绪检查，与只表示进程存活的 /health 分离。"""
    if not is_accepting_jobs():
        raise DomainError(
            code="WORKER_NOT_READY",
            message="后台任务服务未就绪",
            http_status=503,
        )
    try:
        with get_db_session_for_health() as db:
            db.execute(text("SELECT 1"))
    except Exception:
        logger.exception("readiness database check failed")
        raise DomainError(
            code="DATABASE_NOT_READY",
            message="数据库未就绪",
            http_status=503,
        )
    return {"status": "ready"}


def get_db_session_for_health():
    from app.db import SessionLocal

    return SessionLocal()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> str:
    settings = get_settings()
    if not settings.employee_auth_enabled:
        principal = getattr(request.state, "principal", None)
        return _render_workspace_page("index.html", principal, "/ops")
    principal = require_principal(request)
    if principal.has_role(ROLE_WAREHOUSE):
        return RedirectResponse(url="/warehouse", status_code=303)
    if principal.has_role(ROLE_CUSTOMER_SERVICE, ROLE_READONLY) and not principal.has_role(ROLE_ADMIN, ROLE_OPERATIONS, ROLE_WAREHOUSE):
        return RedirectResponse(url="/customer-service", status_code=303)
    return RedirectResponse(url="/ops", status_code=303)

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> str:
    next_path = request.query_params.get("next", "/")
    workspace_key = request.query_params.get("workspace", "")
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"
    next_path = html.escape(next_path, quote=True)
    workspace_key = html.escape(workspace_key, quote=True)
    return f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>登录 - 电商工作台</title><style>body{{max-width:420px;margin:5rem auto;padding:1rem;font-family:system-ui,sans-serif}}form{{display:grid;gap:.8rem}}input,button{{padding:.7rem;border:1px solid #cbd5e1;border-radius:.4rem;font:inherit}}button{{background:#2563eb;color:white;border:0}}</style></head><body><h1>员工登录</h1><p id='message'>请输入账户和密码</p><form method='post' action='/login'><label>账户<input name='login' required autocomplete='username'></label><label>密码<input name='password' type='password' required autocomplete='current-password'></label><label>工作空间（多空间账户必填）<input name='workspace' value='{workspace_key}' autocomplete='organization'></label><input type='hidden' name='next' value='{next_path}'><button type='submit'>登录</button></form></body></html>"""


@app.post("/login")
def login(request: Request, login: str = Form(...), password: str = Form(...), workspace: str = Form(default=""), next: str = Form(default="/"), db: Session = Depends(get_db)):
    if not get_settings().employee_auth_enabled:
        return RedirectResponse(url=next if next.startswith("/") and not next.startswith("//") else "/", status_code=303)
    user = db.scalar(select(UserAccount).where(UserAccount.login == login.strip()))
    memberships = []
    if user is not None and user.status == "active":
        memberships = db.scalars(
            select(WorkspaceMembership)
            .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
            .where(
                WorkspaceMembership.user_id == user.id,
                WorkspaceMembership.status == "active",
                Workspace.status == "active",
            )
            .order_by(WorkspaceMembership.id)
        ).all()
    if user is None or user.status != "active" or not verify_password(password, user.password_hash):
        raise DomainError(code="INVALID_CREDENTIALS", message="账户或密码错误", http_status=401)
    if len(memberships) == 0:
        raise DomainError(code="WORKSPACE_NOT_AVAILABLE", message="账户没有可用工作空间", http_status=401)
    workspace = workspace.strip()
    if workspace:
        selected = [item for item in memberships if str(item.workspace_id) == workspace]
        if len(selected) != 1:
            selected = db.scalars(
                select(WorkspaceMembership)
                .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
                .where(
                    WorkspaceMembership.user_id == user.id,
                    WorkspaceMembership.status == "active",
                    Workspace.status == "active",
                    Workspace.tenant_key == workspace,
                )
            ).all()
        if len(selected) != 1:
            raise DomainError(code="INVALID_WORKSPACE", message="工作空间选择无效", http_status=401)
        membership = selected[0]
    elif len(memberships) == 1:
        membership = memberships[0]
    else:
        raise DomainError(code="WORKSPACE_SELECTION_REQUIRED", message="多工作空间账户必须明确选择工作空间", http_status=409)
    from app.db import backfill_legacy_workspace
    active_workspace_ids = db.scalars(
        select(Workspace.id).where(Workspace.status == "active").order_by(Workspace.id)
    ).all()
    if len(active_workspace_ids) == 1 and active_workspace_ids[0] == membership.workspace_id:
        backfill_legacy_workspace(db, membership.workspace_id)
    token, csrf, _session = create_session(db, user=user, membership=membership, settings=get_settings(), ip_address=request.client.host if request.client else None, user_agent=request.headers.get("user-agent"))
    from datetime import datetime
    user.last_login_at = datetime.utcnow()
    db.commit()
    response = RedirectResponse(
        url=next if next.startswith("/") and not next.startswith("//") else "/",
        status_code=303,
    )
    response.set_cookie(get_settings().session_cookie_name, token, httponly=True, secure=get_settings().session_cookie_secure, samesite="lax", max_age=get_settings().session_ttl_seconds)
    response.set_cookie("dianshang_csrf", csrf, httponly=False, secure=get_settings().session_cookie_secure, samesite="lax", max_age=get_settings().session_ttl_seconds)
    return response


@app.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(get_settings().session_cookie_name)
    principal = principal_from_session(db, token)
    if principal and principal.session_id:
        session = db.get(__import__("app.db", fromlist=["AuthSession"]).AuthSession, principal.session_id)
        if session:
            from datetime import datetime
            session.revoked_at = datetime.utcnow()
            db.commit()
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(get_settings().session_cookie_name)
    response.delete_cookie("dianshang_csrf")
    return response


@app.get("/me")
def me(request: Request):
    principal = require_principal(request)
    return {"subject": principal.subject, "workspace_id": principal.workspace_id, "roles": principal.roles, "warehouse_ids": principal.warehouse_ids}


@app.get("/content", response_class=HTMLResponse)
def content_page(request: Request) -> HTMLResponse:
    principal = require_principal(request, permission="content.read", roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    return _render_workspace_page("content.html", principal, "/content")


@app.get("/shopify", response_class=HTMLResponse)
def shopify_page(request: Request) -> HTMLResponse:
    principal = require_principal(request, permission="inventory.read", roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    return _render_workspace_page("shopify.html", principal, "/shopify")


@app.get("/ops", response_class=HTMLResponse)
def ops_page(request: Request) -> HTMLResponse:
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    return _render_workspace_page("index.html", principal, "/ops")


@app.get("/warehouse", response_class=HTMLResponse)
def warehouse_page(request: Request) -> HTMLResponse:
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS, ROLE_WAREHOUSE))
    return _render_workspace_page("warehouse.html", principal, "/warehouse")


@app.get("/customer-service", response_class=HTMLResponse)
def customer_service_page(request: Request) -> HTMLResponse:
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_CUSTOMER_SERVICE, ROLE_READONLY))
    return _render_workspace_page("customer_service.html", principal, "/customer-service")
def _require_csrf(request: Request, db: Session) -> None:
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        validate_csrf(request, db)


@app.get("/api/admin/users", response_model=list[AdminUserResponse])
def admin_list_users(request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN,))
    from app.account_admin import list_workspace_users
    return list_workspace_users(db, workspace_id=principal.workspace_id)


@app.post("/api/admin/users", response_model=AdminUserResponse, status_code=201)
def admin_create_user(payload: AdminUserCreate, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN,))
    from app.account_admin import create_workspace_user
    try:
        return create_workspace_user(db, workspace_id=principal.workspace_id, login=payload.login, display_name=payload.display_name, password=payload.password, role=payload.role, warehouse_ids=payload.warehouse_ids)
    except ValueError as exc:
        raise DomainError(code=str(exc), message="账户或仓库范围无效", http_status=409 if str(exc).endswith("EXISTS") else 422) from exc


@app.post("/api/admin/users/{user_id}/warehouses/{warehouse_id}", status_code=204)
def admin_grant_warehouse(user_id: int, warehouse_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN,))
    from app.account_admin import set_warehouse_access
    try:
        set_warehouse_access(db, workspace_id=principal.workspace_id, user_id=user_id, warehouse_id=warehouse_id, active=True)
    except ValueError as exc:
        raise DomainError(code=str(exc), message="账户或仓库不属于当前工作空间", http_status=422) from exc


@app.delete("/api/admin/users/{user_id}/warehouses/{warehouse_id}", status_code=204)
def admin_revoke_warehouse(user_id: int, warehouse_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN,))
    from app.account_admin import set_warehouse_access
    try:
        set_warehouse_access(db, workspace_id=principal.workspace_id, user_id=user_id, warehouse_id=warehouse_id, active=False)
    except ValueError as exc:
        raise DomainError(code=str(exc), message="账户或仓库不属于当前工作空间", http_status=422) from exc

@app.get("/api/admin/warehouses", response_model=list[WarehouseResponse])
def admin_list_warehouses(request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN,))
    from app.account_admin import list_workspace_warehouses
    return list_workspace_warehouses(db, workspace_id=principal.workspace_id, include_unbound=True)


@app.post("/api/admin/warehouses/{warehouse_id}/bind", response_model=WarehouseResponse)
def admin_bind_warehouse(warehouse_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN,))
    from app.account_admin import bind_legacy_warehouse
    try:
        return bind_legacy_warehouse(db, workspace_id=principal.workspace_id, warehouse_id=warehouse_id)
    except ValueError as exc:
        code = str(exc)
        status = 409 if code == "WAREHOUSE_ALREADY_BOUND" else 404
        raise DomainError(code=code, message="旧仓库绑定失败", http_status=status) from exc


@app.get("/api/roles")
def list_roles(request: Request):
    require_principal(request, roles=(ROLE_ADMIN,))
    from app.employee_auth import ROLE_LABELS, ROLE_PERMISSIONS
    return {
        "items": [
            {
                "role": role,
                "label": ROLE_LABELS[role],
                "permissions": sorted(permissions),
                "warehouse_scope": "all_current_workspace" if role in {ROLE_ADMIN, ROLE_OPERATIONS} else "assigned_warehouses",
            }
            for role, permissions in ROLE_PERMISSIONS.items()
        ]
    }


@app.get("/roles", response_class=HTMLResponse)
def roles_page(request: Request) -> str:
    require_principal(request, roles=(ROLE_ADMIN,))
    from app.employee_auth import ROLE_LABELS, ROLE_PERMISSIONS
    rows = "".join(
        f"<tr><td>{html.escape(ROLE_LABELS[role])}</td><td>{html.escape(role)}</td><td>{html.escape(', '.join(sorted(permissions)) or '全部管理权限')}</td></tr>"
        for role, permissions in ROLE_PERMISSIONS.items()
    )
    return f"<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>角色权限</title><body><h1>角色权限</h1><p>角色权限为系统固定策略，管理员可在账户页分配角色。</p><table><thead><tr><th>角色</th><th>标识</th><th>权限</th></tr></thead><tbody>{rows}</tbody></table><p><a href='/admin/users'>返回账户管理</a></p></body></html>"


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request) -> HTMLResponse:
    principal = require_principal(request, roles=(ROLE_ADMIN,))
    return _render_workspace_page("admin_users.html", principal, "/admin/users")


@app.post("/api/admin/users/{user_id}/deactivate", status_code=204)
def admin_deactivate_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN,))
    if principal.user_id == user_id:
        raise DomainError(code="SELF_DEACTIVATE_FORBIDDEN", message="不能停用当前登录账户", http_status=409)
    from app.account_admin import set_user_status
    try:
        set_user_status(db, workspace_id=principal.workspace_id, user_id=user_id, active=False)
    except ValueError as exc:
        code = str(exc)
        status = 409 if code == "LAST_ADMIN_FORBIDDEN" else 404
        raise DomainError(code=code, message="账户状态更新失败", http_status=status) from exc


@app.patch("/api/admin/users/{user_id}", response_model=AdminUserResponse)
def admin_update_user(user_id: int, payload: AdminUserPatch, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN,))
    from app.account_admin import update_workspace_user
    try:
        return update_workspace_user(db, workspace_id=principal.workspace_id, user_id=user_id, display_name=payload.display_name, role=payload.role)
    except ValueError as exc:
        code = str(exc)
        status = 409 if code == "LAST_ADMIN_FORBIDDEN" else 422 if code in {"INVALID_USER_INPUT", "INVALID_ROLE"} else 404
        raise DomainError(code=code, message="账户或角色更新失败", http_status=status) from exc


@app.post("/api/admin/users/{user_id}/activate", status_code=204)
def admin_activate_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN,))
    from app.account_admin import set_user_status
    try:
        set_user_status(db, workspace_id=principal.workspace_id, user_id=user_id, active=True)
    except ValueError as exc:
        raise DomainError(code=str(exc), message="账户不存在", http_status=404) from exc


@app.post("/api/admin/users/{user_id}/reset-password", status_code=204)
def admin_reset_password(user_id: int, payload: AdminUserPasswordReset, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN,))
    from app.account_admin import reset_user_password
    try:
        reset_user_password(db, workspace_id=principal.workspace_id, user_id=user_id, password=payload.password)
    except ValueError as exc:
        raise DomainError(code=str(exc), message="密码或账户无效", http_status=422 if str(exc).endswith("SHORT") else 404) from exc


class LiveSwitchRequest(BaseModel):
    """运行期 Shopify live 热开关的请求体（仅进程内生效，不落库）。"""

    live_enabled: bool


@app.post("/api/admin/live-switch")
def admin_set_live_switch(payload: LiveSwitchRequest, request: Request):
    """切换运行期 Shopify live 热开关。

    仅 admin 可调用；状态只保存在进程内存（app/runtime_flags.py），不落库、
    不写 .env，进程重启后回落 settings.shopify_live_enabled。
    """
    principal = require_principal(request, roles=(ROLE_ADMIN,))
    set_runtime_live_enabled(payload.live_enabled)
    current = runtime_live_enabled()
    logger.info(
        "shopify live 运行期开关切换: live_enabled=%s by user=%s",
        current,
        principal.user_id,
    )
    return {"live_enabled": current, "source": "runtime_flag"}


@app.post("/api/crawl", response_model=CrawlJobDetailResponse, status_code=202)
def enqueue_crawl(
    request: Request,
    db: Session = Depends(get_db),
) -> CrawlJob:
    principal = require_principal(request, permission="catalog.write")
    if principal.workspace_id is None:
        raise DomainError(code="WORKSPACE_CONTEXT_REQUIRED", message="工作空间上下文缺失", http_status=503)
    fixture = Path(__file__).resolve().parent.parent / "fixtures" / "products.html"
    job = submit_crawl_fixture(db, fixture_path=fixture, keyword="fixture", workspace_id=principal.workspace_id)
    return job


# 注意：固定路径 /api/crawl/jobs 必须在 /api/crawl/{job_id} 之前注册，
# 否则 FastAPI 会把字面量 "jobs" 解析成 job_id。
@app.get("/api/crawl/jobs", response_model=list[CrawlJobDetailResponse])
def list_jobs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)
) -> list[CrawlJob]:
    principal = require_principal(request, permission="catalog.read")
    return db.scalars(select(CrawlJob).where(CrawlJob.workspace_id == principal.workspace_id).order_by(CrawlJob.id.desc()).limit(limit)).all()


@app.get("/api/crawl/{job_id}", response_model=CrawlJobDetailResponse)
def get_crawl_job(job_id: int, request: Request, db: Session = Depends(get_db)) -> CrawlJob:
    principal = require_principal(request, permission="catalog.read")
    job = db.scalar(select(CrawlJob).where(CrawlJob.id == job_id, CrawlJob.workspace_id == principal.workspace_id))
    if job is None:
        raise DomainError(code="JOB_NOT_FOUND", message="任务不存在", http_status=404)
    return job


@app.post("/api/crawl/{job_id}/cancel", response_model=CrawlJobDetailResponse)
def cancel_crawl_job(job_id: int, request: Request, db: Session = Depends(get_db)) -> CrawlJob:
    principal = require_principal(request, permission="catalog.write")
    job = request_cancel_job(db, job_id=job_id, workspace_id=principal.workspace_id)
    if job is None:
        raise DomainError(code="JOB_NOT_FOUND", message="任务不存在", http_status=404)
    return job


# ---------- S2 文档导入 API ----------


@app.post(
    "/api/documents",
    response_model=DocumentUploadResponse,
    status_code=202,
)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    db: Session = Depends(get_db)
) -> DocumentUploadResponse:
    principal = require_principal(request, permission="knowledge.write")
    """上传 PDF / DOCX；按 sha256 自动复用 / 新建 document + version。

    立即返回任务 ID，实际解析在后台 executor 中执行。
    """
    if file is None:
        raise DomainError(code="NO_FILE", message="未提供文件", http_status=400)

    try:
        source_type = detect_source_type(file.filename, file.content_type)
    except ParserError as exc:
        raise DomainError(
            code=exc.code,
            message=exc.message,
            http_status=415,
        ) from exc

    content = await file.read()
    try:
        enforce_size_limit(len(content))
        source_type = validate_content(
            content,
            source_type=source_type,
            filename=file.filename,
            content_type=file.content_type,
            max_zip_members=get_settings().upload_max_zip_members,
            max_zip_uncompressed_bytes=get_settings().upload_max_zip_uncompressed_bytes,
        )
    except (StorageError, ParserError) as exc:
        status_code = 413 if getattr(exc, "code", "") == "FILE_TOO_LARGE" else 415
        raise DomainError(
            code=exc.code,
            message=exc.message,
            http_status=status_code,
        ) from exc

    sha = content_sha256(content)
    ext = source_type

    document, _doc_created = upsert_document(
        db,
        workspace_id=principal.workspace_id,
        source_type=source_type,
        title=title or file.filename or sha,
        filename=file.filename or sha,
        sha256_hex=sha,
    )
    storage_uri = save_upload(
        source_type=source_type,
        sha256_hex=sha,
        ext=ext,
        content=content,
    )
    version, version_created = upsert_document_version(
        db,
        document=document,
        sha256_hex=sha,
        size_bytes=len(content),
        storage_uri=storage_uri,
        workspace_id=principal.workspace_id,
    )
    db.commit()

    if version_created:
        job = submit_document_import(
            db,
            document_id=document.id,
            version_id=version.id,
            storage_uri=storage_uri,
            filename=document.filename,
            source_type=source_type,
            workspace_id=principal.workspace_id,
        )
    else:
        # 重复上传相同内容：复用现有 version，不重复入队
        from app.db import CrawlJob

        job = CrawlJob(
            workspace_id=principal.workspace_id,
            source=source_type,
            keyword=document.filename[:255],
            type="document_import",
            status="succeeded",
            cursor=json.dumps(
                {"document_id": document.id, "version_id": version.id, "storage_uri": storage_uri},
                ensure_ascii=False,
            ),
            run_id=version.id,
        )
        db.add(job)
        db.commit()
        db.refresh(job)

    return DocumentUploadResponse(
        document_id=document.id,
        version_id=version.id,
        job_id=job.id,
        version_no=version.version_no,
        status=version.status,
    )


@app.get(
    "/api/documents/{document_id}/versions",
    response_model=list[DocumentVersionResponse],
)
def list_document_versions(
    document_id: int, request: Request, db: Session = Depends(get_db)
) -> list[DocumentVersionResponse]:
    principal = require_principal(request, permission="knowledge.read")
    document = db.scalar(select(Document).where(Document.id == document_id, Document.workspace_id == principal.workspace_id))
    if document is None:
        raise DomainError(
            code="DOCUMENT_NOT_FOUND",
            message="文档不存在",
            http_status=404,
        )
    versions = list_versions_for_document(db, document_id=document_id, workspace_id=principal.workspace_id)
    items: list[DocumentVersionResponse] = []
    for version in versions:
        items.append(
            DocumentVersionResponse(
                id=version.id,
                document_id=version.document_id,
                version_no=version.version_no,
                sha256=version.sha256,
                size_bytes=version.size_bytes,
                storage_uri=version.storage_uri,
                status=version.status,
                parser_version=version.parser_version,
                error_code=version.error_code,
                error_message=version.error_message,
                created_at=version.created_at,
                published_at=version.published_at,
                chunk_count=count_chunks(db, version_id=version.id, workspace_id=principal.workspace_id),
            )
        )
    return items


# ---------- S3 RAG 客服问答 ----------


@app.post("/api/rag/query", response_model=RagQueryResponse)
def rag_query(payload: RagQueryRequest, request: Request, db: Session = Depends(get_db)) -> RagQueryResponse:
    principal = require_principal(request, permission="knowledge.read")
    workspace_id = principal.workspace_id
    filter_payload: dict[str, object] = {"workspace_id": workspace_id}
    """基于已上传文档与本地向量库回答客服问题。

    返回结构：
    - answer: 文本答案（无证据或低置信度时为 null）
    - citations: 命中的 chunk 引用（chunk_id / doc / version / snippet / score）
    - no_answer: true 表示命中为空或最高分低于阈值
    - retrieval_diagnostics: 实际使用的 top_k / min_score / 命中数等
    """
    from app.rag_runtime import get_answerer, get_embedder, get_vector_store

    if payload.product_id is not None:
        filter_payload["product_id"] = payload.product_id
    if payload.source_type is not None:
        filter_payload["source_type"] = payload.source_type

    answerer = get_answerer()
    result = answerer.answer(
        payload.query,
        top_k=payload.top_k,
        min_score=payload.min_score,
        filter_payload=filter_payload or None,
    )

    citations = [
        RagCitation(
            chunk_id=hit.chunk_id,
            document_id=hit.document_id,
            document_version_id=hit.document_version_id,
            snippet=hit.snippet,
            score=hit.score,
            locator=hit.locator,
        )
        for hit in result.hits
    ]
    diagnostics = {
        "vector_records": sum(1 for record in get_vector_store().records if record.payload.get("workspace_id") == workspace_id),
        "hits_count": len(result.hits),
        "top_k": payload.top_k or answerer.retriever.top_k,
        "min_score": payload.min_score
        if payload.min_score is not None
        else answerer.retriever.min_score,
        "embedder_dim": get_embedder().dim,
    }
    return RagQueryResponse(
        answer=result.answer,
        citations=citations,
        no_answer=result.no_answer,
        reason=result.reason,
        retrieval_diagnostics=diagnostics,
    )


# ---------- S4 运行设置端点 ----------


def safe_database_url(value: str) -> str:
    try:
        return make_url(value).render_as_string(hide_password=True)
    except Exception:
        return "<redacted>"


@app.get("/api/settings", response_model=SettingsResponse)
def get_runtime_settings(request: Request, db: Session = Depends(get_db)) -> SettingsResponse:
    principal = require_principal(request, permission="knowledge.read")
    from app.db import Document, DocumentChunk
    from app.rag_runtime import get_vector_store
    settings = get_settings()
    return SettingsResponse(
        app_name=settings.app_name,
        database_url=safe_database_url(settings.database_url),
        worker_concurrency=settings.worker_concurrency,
        worker_max_attempts=settings.worker_max_attempts,
        worker_lease_seconds=settings.worker_lease_seconds,
        worker_heartbeat_seconds=settings.worker_heartbeat_seconds,
        upload_max_bytes=settings.upload_max_bytes,
        imports_dir=settings.imports_dir,
        embedder_dim=settings.embedder_dim,
        vector_store_path=settings.vector_store_path,
        rag_top_k=settings.rag_top_k,
        rag_min_score=settings.rag_min_score,
        document_count=db.scalar(select(func.count(Document.id)).where(Document.workspace_id == principal.workspace_id)) or 0,
        chunk_count=db.scalar(select(func.count(DocumentChunk.id)).where(DocumentChunk.workspace_id == principal.workspace_id)) or 0,
        vector_record_count=sum(1 for record in get_vector_store().records if record.payload.get("workspace_id") == principal.workspace_id),
    )


@app.post("/api/assistant/query", response_model=AssistantQueryResponse)
def assistant_query(payload: AssistantQueryRequest, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="analytics.read", roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    from app.assistant import query_assistant
    return query_assistant(db, workspace_id=principal.workspace_id, message=payload.message)


@app.get("/assistant", response_class=HTMLResponse)
def assistant_page(request: Request) -> HTMLResponse:
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    return _render_workspace_page("assistant.html", principal, "/assistant")




def _register_agent_tools() -> None:
    """Register every read-only Agent tool before serving any Agent route."""
    from app.agent.impl import register_default_tools
    from app.agent.registry import default_registry
    from app.agent.skills import register_operator_skills

    register_default_tools(default_registry)
    register_operator_skills(default_registry)


def _agent_context(request: Request, principal) :
    """Build Agent context only from the authenticated principal."""
    from app.agent.registry import ToolContext
    from app.employee_auth import ROLE_ADMIN, ROLE_OPERATIONS

    permissions = set(getattr(principal, "scopes", ()))
    if getattr(principal, "auth_type", None) == "session":
        for permission in ("inventory.read", "replenishment.read"):
            if principal.can(permission):
                permissions.add(permission)
    has_role = getattr(principal, "has_role", lambda *roles: False)
    return ToolContext(
        tenant_id=principal.tenant_id,
        workspace_id=principal.workspace_id,
        user_id=principal.subject,
        request_id=request.state.request_id,
        extras={
            "permissions": tuple(permissions),
            "warehouse_ids": tuple(getattr(principal, "warehouse_ids", ())),
            "all_warehouse_access": bool(has_role(ROLE_ADMIN, ROLE_OPERATIONS)),
        },
    )


@app.get("/api/agent/tools", response_model=list[AgentToolSpec])
def list_agent_tools(request: Request) -> list[AgentToolSpec]:
    require_principal(request, permission="knowledge.read")
    _register_agent_tools()
    from app.agent.orchestrator import AgentOrchestrator

    orchestrator = AgentOrchestrator()
    tools = orchestrator.list_tools()
    return [
        AgentToolSpec(
            name=t.name,
            description=t.description,
            is_readonly=t.is_readonly,
            input_schema=t.input_schema,
            output_schema=t.output_schema,
            max_calls_per_minute=t.max_calls_per_minute,
        )
        for t in tools
    ]


@app.post("/api/agent/invoke", response_model=AgentActionResponse)
def invoke_agent_tool(
    payload: AgentActionRequest, request: Request
) -> AgentActionResponse:
    principal = require_principal(request, permission="knowledge.read")
    _register_agent_tools()
    from app.agent.orchestrator import AgentAction, AgentOrchestrator

    context = _agent_context(request, principal)
    orchestrator = AgentOrchestrator()
    result = orchestrator.invoke(
        AgentAction(tool=payload.tool, input=payload.input, call_id=payload.call_id),
        context=context,
    )
    return AgentActionResponse(
        ok=result.ok,
        output=result.output if isinstance(result.output, dict) else None,
        error_code=result.error_code,
        error_message=result.error_message,
        duration_ms=result.duration_ms,
        call_id=payload.call_id,
    )


# ---------- V3 单核运营助手 ----------


@app.post("/api/agent/assistant", response_model=AgentAssistantResponse)
def operator_assistant(payload: AgentAssistantRequest, request: Request) -> AgentAssistantResponse:
    principal = require_principal(request, permission="knowledge.read")
    _register_agent_tools()
    from app.agent.adapters import MockAdapter
    from app.agent.core import AgentCore
    from app.agent.orchestrator import AgentOrchestrator
    from app.agent.registry import default_registry

    context = _agent_context(request, principal)
    core = AgentCore(
        adapter=MockAdapter(),
        registry=default_registry,
        orchestrator=AgentOrchestrator(registry=default_registry),
        max_turns=payload.max_turns,
        max_tool_calls=payload.max_tool_calls,
    )
    result = core.run(payload.message, context=context, input_hints=payload.input_hints)
    return AgentAssistantResponse(
        ok=result.ok,
        answer=result.answer,
        error_code=result.error_code,
        turns=result.turns,
        traces=[AgentAssistantTrace(tool=t.tool, input=t.input, ok=t.result.ok, output=t.result.output, error_code=t.result.error_code, duration_ms=t.result.duration_ms) for t in result.traces],
    )

def _warehouse_error(exc: ValueError) -> DomainError:
    code = str(exc)
    status = 404 if code.endswith("NOT_FOUND") else 409 if code.endswith(("EXISTS", "RECEIVED", "STATE")) else 422
    messages = {
        "PRODUCT_NOT_FOUND": "商品不存在",
        "SKU_NOT_FOUND": "SKU 不存在或已停用",
        "WAREHOUSE_NOT_FOUND": "仓库不存在",
        "WAREHOUSE_INACTIVE": "仓库已停用",
        "SKU_CODE_EXISTS": "SKU 编码已存在",
        "WAREHOUSE_CODE_EXISTS": "仓库编码已存在",
        "INBOUND_REFERENCE_EXISTS": "入库单号已存在",
        "DUPLICATE_SKU": "入库明细中 SKU 重复",
        "INBOUND_LINES_MISMATCH": "实收明细与入库单不匹配",
        "DAMAGED_QTY_INVALID": "破损数量不能大于实收数量",
        "INBOUND_ALREADY_RECEIVED": "入库单已提交收货",
        "IDEMPOTENCY_KEY_REQUIRED": "必须提供 Idempotency-Key",
        "INVALID_INBOUND_STATE": "当前入库单状态不允许此操作",
    }
    return DomainError(code=code, message=messages.get(code, "仓储操作失败"), http_status=status)


@app.post("/api/skus", response_model=ProductSkuResponse, status_code=201)
def create_sku(payload: ProductSkuCreate, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="catalog.write")
    from app.repository import create_sku as create_sku_record
    try:
        return create_sku_record(db, product_id=payload.product_id, sku_code=payload.sku_code, variant_label=payload.variant_label, barcode=payload.barcode, unit=payload.unit, workspace_id=principal.workspace_id)
    except ValueError as exc:
        raise _warehouse_error(exc) from exc


@app.get("/api/skus", response_model=list[ProductSkuResponse])
def list_skus(request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="catalog.read")
    from app.db import ProductSku
    return db.scalars(select(ProductSku).where(ProductSku.is_active.is_(True), ProductSku.workspace_id == principal.workspace_id).order_by(ProductSku.id)).all()


@app.post("/api/warehouses", response_model=WarehouseResponse, status_code=201)
def create_warehouse(payload: WarehouseCreate, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="catalog.write")
    from app.repository import create_warehouse as create_warehouse_record
    try:
        return create_warehouse_record(db, code=payload.code, name=payload.name, warehouse_type=payload.warehouse_type, integration_mode=payload.integration_mode, external_ref=payload.external_ref, workspace_id=principal.workspace_id)
    except ValueError as exc:
        raise _warehouse_error(exc) from exc


@app.get("/api/warehouses", response_model=list[WarehouseResponse])
def list_warehouses(request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="inbound.read")
    from app.db import Warehouse
    query = select(Warehouse).where(
        Warehouse.is_active.is_(True),
        Warehouse.workspace_id == principal.workspace_id,
    )
    if not principal.has_role(ROLE_ADMIN, ROLE_OPERATIONS):
        if not principal.warehouse_ids:
            return []
        query = query.where(Warehouse.id.in_(principal.warehouse_ids))
    return db.scalars(query.order_by(Warehouse.id)).all()


@app.post("/api/inbounds", response_model=InboundResponse, status_code=201)
def create_inbound(payload: InboundCreate, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="inbound.create")
    from app.repository import _inbound_response_data, create_inbound as create_inbound_record, get_inbound
    try:
        require_warehouse_access(principal, payload.warehouse_id, db=db)
        order = create_inbound_record(db, warehouse_id=payload.warehouse_id, reference_no=payload.reference_no, lines=[item.model_dump() for item in payload.lines], note=payload.note, created_by=principal.subject, workspace_id=principal.workspace_id)
        result = get_inbound(db, inbound_id=order.id, workspace_id=principal.workspace_id)
        assert result is not None
        return _inbound_response_data(*result)
    except ValueError as exc:
        raise _warehouse_error(exc) from exc


@app.get("/api/inbounds/{inbound_id}", response_model=InboundResponse)
def get_inbound_detail(inbound_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="inbound.read")
    from app.repository import _inbound_response_data, get_inbound
    result = get_inbound(db, inbound_id=inbound_id, workspace_id=principal.workspace_id)
    if result is None:
        raise _warehouse_error(ValueError("INBOUND_NOT_FOUND"))
    require_warehouse_access(principal, result[0].warehouse_id, db=db)
    return _inbound_response_data(*result)


@app.get("/api/inbounds", response_model=InboundListPage)
def list_inbounds(
    warehouse_id: int | None = Query(default=None, gt=0),
    status: str | None = Query(default=None, max_length=24),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    request: Request = None,
    db: Session = Depends(get_db),
):
    principal = require_principal(request, permission="inbound.read")
    from app.db import InboundOrder

    filters = [InboundOrder.workspace_id == principal.workspace_id]
    if warehouse_id is not None:
        require_warehouse_access(principal, warehouse_id, db=db)
        filters.append(InboundOrder.warehouse_id == warehouse_id)
    elif not principal.has_role(ROLE_ADMIN, ROLE_OPERATIONS):
        if not principal.warehouse_ids:
            return InboundListPage(items=[], page=page, page_size=page_size, total=0)
        filters.append(InboundOrder.warehouse_id.in_(principal.warehouse_ids))
    else:
        filters.append(InboundOrder.warehouse_id.in_(
            select(Warehouse.id).where(
                Warehouse.workspace_id == principal.workspace_id,
                Warehouse.is_active.is_(True),
            )
        ))
    if status:
        filters.append(InboundOrder.status == status)
    total = db.scalar(select(func.count(InboundOrder.id)).where(*filters)) or 0
    orders = db.scalars(
        select(InboundOrder)
        .where(*filters)
        .order_by(InboundOrder.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return InboundListPage(
        items=[InboundListItem.model_validate(order) for order in orders],
        page=page,
        page_size=page_size,
        total=int(total),
    )


@app.post("/api/inbounds/{inbound_id}/receive", response_model=InboundResponse)
def receive_inbound(inbound_id: int, payload: InboundReceive, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="inbound.receive")
    key = request.headers.get("Idempotency-Key")
    if not key or not key.strip():
        raise _warehouse_error(ValueError("IDEMPOTENCY_KEY_REQUIRED"))
    from app.repository import _inbound_response_data, receive_inbound as receive_inbound_record
    try:
        existing = db.scalar(select(InboundOrder).where(InboundOrder.id == inbound_id, InboundOrder.workspace_id == principal.workspace_id))
        if existing is None:
            raise ValueError("INBOUND_NOT_FOUND")
        require_warehouse_access(principal, existing.warehouse_id, db=db)
        result = receive_inbound_record(
            db,
            inbound_id=inbound_id,
            lines=[item.model_dump() for item in payload.lines],
            idempotency_key=key,
            payload_hash=sha256(payload.model_dump_json().encode()).hexdigest(),
            workspace_id=principal.workspace_id,
        )
        return _inbound_response_data(*result)
    except ValueError as exc:
        raise _warehouse_error(exc) from exc


@app.post("/api/inbounds/{inbound_id}/confirm", response_model=InboundResponse)
def confirm_inbound(inbound_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="inbound.confirm")
    from app.repository import _inbound_response_data, confirm_inbound as confirm_inbound_record
    try:
        existing = db.scalar(select(InboundOrder).where(InboundOrder.id == inbound_id, InboundOrder.workspace_id == principal.workspace_id))
        if existing is None:
            raise ValueError("INBOUND_NOT_FOUND")
        require_warehouse_access(principal, existing.warehouse_id, db=db)
        result = confirm_inbound_record(
            db,
            inbound_id=inbound_id,
            confirmed_by=getattr(request.state.principal, "subject", None),
            idempotency_key=request.headers.get("Idempotency-Key"),
            workspace_id=principal.workspace_id,
        )
        return _inbound_response_data(*result)
    except ValueError as exc:
        raise _warehouse_error(exc) from exc


@app.get("/api/inventory", response_model=InventoryPage)
def inventory_page(page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100), warehouse_id: int | None = Query(default=None, gt=0), sku_id: int | None = Query(default=None, gt=0), request: Request = None, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="inventory.read")
    if warehouse_id is not None:
        require_warehouse_access(principal, warehouse_id, db=db)
    items, total = list_inventory(
        db,
        warehouse_id=warehouse_id,
        sku_id=sku_id,
        warehouse_ids=None if principal.has_role(ROLE_ADMIN, ROLE_OPERATIONS) else principal.warehouse_ids,
        workspace_id=principal.workspace_id,
        page=page,
        page_size=page_size,
    )
    return InventoryPage(items=items, page=page, page_size=page_size, total=total)




@app.get("/api/inventory/policies", response_model=list[InventoryPolicyResponse])
def list_inventory_policies(request: Request, warehouse_id: int | None = Query(default=None, gt=0), db: Session = Depends(get_db)):
    principal = require_principal(request, permission="inventory.read")
    from app.db import InventoryPolicy
    filters = [InventoryPolicy.workspace_id == principal.workspace_id]
    if warehouse_id is not None:
        require_warehouse_access(principal, warehouse_id, db=db)
        filters.append(InventoryPolicy.warehouse_id == warehouse_id)
    return db.scalars(select(InventoryPolicy).where(*filters).order_by(InventoryPolicy.id)).all()


@app.put("/api/inventory/policies", response_model=InventoryPolicyResponse)
def save_inventory_policy(payload: InventoryPolicyCreate, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="inventory.write")
    require_warehouse_access(principal, payload.warehouse_id, db=db)
    from app.alerts import upsert_inventory_policy
    try:
        return upsert_inventory_policy(db, workspace_id=principal.workspace_id, warehouse_id=payload.warehouse_id, sku_id=payload.sku_id, safety_stock_qty=payload.safety_stock_qty, reorder_point_qty=payload.reorder_point_qty)
    except ValueError as exc:
        code = str(exc)
        status = 404 if code.endswith("NOT_FOUND") else 422
        raise DomainError(code=code, message="库存策略无效", http_status=status) from exc


@app.post("/api/inventory/alerts/refresh", response_model=list[AlertResponse])
def refresh_inventory_alerts(request: Request, warehouse_id: int | None = Query(default=None, gt=0), db: Session = Depends(get_db)):
    principal = require_principal(request, permission="inventory.write")
    if warehouse_id is not None:
        require_warehouse_access(principal, warehouse_id, db=db)
    from app.alerts import refresh_low_stock_alerts
    return refresh_low_stock_alerts(db, workspace_id=principal.workspace_id, warehouse_id=warehouse_id)


@app.get("/api/inventory/alerts", response_model=list[AlertResponse])
def list_inventory_alerts(request: Request, warehouse_id: int | None = Query(default=None, gt=0), status: str | None = Query(default=None, max_length=16), db: Session = Depends(get_db)):
    principal = require_principal(request, permission="inventory.read")
    if warehouse_id is not None:
        require_warehouse_access(principal, warehouse_id, db=db)
    from app.alerts import list_alerts
    return list_alerts(db, workspace_id=principal.workspace_id, warehouse_id=warehouse_id, status=status)


@app.post("/api/inventory/alerts/{alert_id}/ack", response_model=AlertResponse)
def acknowledge_inventory_alert(alert_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="inventory.write")
    from app.alerts import acknowledge_alert
    alert = acknowledge_alert(db, workspace_id=principal.workspace_id, alert_id=alert_id)
    if alert is None:
        raise DomainError(code="ALERT_NOT_FOUND", message="告警不存在", http_status=404)
    return alert


@app.post("/api/crawl/fixture", response_model=CrawlJobResponse, status_code=201)
def crawl_fixture(request: Request, db: Session = Depends(get_db)) -> CrawlJob:
    principal = require_principal(request, permission="catalog.write")
    if principal.workspace_id is None:
        raise DomainError(code="WORKSPACE_CONTEXT_REQUIRED", message="工作空间上下文缺失", http_status=503)
    """第一周同步入口：直接执行 fixture 采集并写入。保留用于演示。"""
    job = start_job(db, source="fixture", keyword="fixture", workspace_id=principal.workspace_id)
    try:
        fixture = Path(__file__).resolve().parent.parent / "fixtures" / "products.html"
        records = collect_fixture(fixture)
        return import_records(db, records, keyword="fixture", job=job, workspace_id=principal.workspace_id)
    except CrawlError as exc:
        db.rollback()
        job = db.get(CrawlJob, job.id)
        if job is not None:
            fail_job(db, job, error_code=exc.code, message=exc.message)
        raise DomainError(
            code=exc.code,
            message=exc.message,
            http_status=422,
        ) from exc


@app.get("/api/operations/today", response_model=OperationsTodayResponse)
def operations_today(
    request: Request,
    warehouse_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Return a read-only, workspace-scoped operational todo aggregation."""
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    if warehouse_id is not None:
        require_warehouse_access(principal, warehouse_id, db=db)
        warehouse_ids = (warehouse_id,)
    elif principal.has_role(ROLE_ADMIN, ROLE_OPERATIONS):
        warehouse_ids = None
    else:
        warehouse_ids = principal.warehouse_ids
    from app.operations_workbench import build_operations_today
    try:
        return build_operations_today(
            db,
            workspace_id=principal.workspace_id,
            warehouse_ids=warehouse_ids,
            limit=limit,
        )
    except ValueError as exc:
        code = str(exc)
        status = 404 if code == "WORKSPACE_NOT_FOUND" else 422
        raise DomainError(code=code, message="运营待办查询参数无效", http_status=status) from exc


@app.get("/api/operations/today/export")
def export_operations_today(
    request: Request,
    warehouse_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=100, ge=1, le=200),
    todo_ids: str | None = Query(default=None, max_length=10_000),
    types: str | None = Query(default=None, max_length=2_000),
    priorities: str | None = Query(default=None, max_length=500),
    db: Session = Depends(get_db),
):
    """Export the current user's visible operational todos as CSV."""
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    if warehouse_id is not None:
        require_warehouse_access(principal, warehouse_id, db=db)
        warehouse_ids = (warehouse_id,)
    elif principal.has_role(ROLE_ADMIN, ROLE_OPERATIONS):
        warehouse_ids = None
    else:
        warehouse_ids = principal.warehouse_ids
    from app.operations_workbench import build_operations_today
    from app.exports import operations_todo_csv

    try:
        body = build_operations_today(db, workspace_id=principal.workspace_id, warehouse_ids=warehouse_ids, limit=limit)
    except ValueError as exc:
        code = str(exc)
        status = 404 if code == "WORKSPACE_NOT_FOUND" else 422
        raise DomainError(code=code, message="运营待办导出参数无效", http_status=status) from exc

    def values(raw: str | None) -> set[str] | None:
        if raw is None or not raw.strip():
            return None
        parsed = {part.strip() for part in raw.split(",") if part.strip()}
        return parsed or None

    selected_ids = values(todo_ids)
    selected_types = values(types)
    selected_priorities = values(priorities)
    items = [
        item for item in body["items"]
        if (selected_ids is None or item["todo_id"] in selected_ids)
        and (selected_types is None or item["type"] in selected_types)
        and (selected_priorities is None or item["priority"] in selected_priorities)
    ]
    content, export_meta = operations_todo_csv(items)
    filename = f"今日待办清单-{body['as_of']:%Y.%m.%d}.csv"
    filename_ascii = f"operations-today-{body['as_of']:%Y.%m.%d}.csv"
    from fastapi.responses import Response
    response = Response(content=content, media_type="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f"attachment; filename=\"{filename_ascii}\"; filename*=UTF-8''{quote(filename)}"
    response.headers["X-Export-Sha256"] = export_meta["sha256"]
    response.headers["X-Export-Row-Count"] = str(export_meta["row_count"])
    response.headers["X-Export-Source"] = body["source_mode"]
    response.headers["X-Export-Simulated"] = str(body["simulated"]).lower()
    response.headers["X-Export-Evidence-Level"] = body["evidence_level"]
    response.headers["X-Export-Data-Completeness"] = body["meta"]["data_completeness"]
    response.headers["X-Export-Workspace-Id"] = str(body["workspace_id"])
    return response


@app.get("/api/operations/today/export.xlsx")
def export_operations_today_xlsx(
    request: Request,
    warehouse_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=100, ge=1, le=200),
    todo_ids: str | None = Query(default=None, max_length=10_000),
    types: str | None = Query(default=None, max_length=2_000),
    priorities: str | None = Query(default=None, max_length=500),
    db: Session = Depends(get_db),
):
    """Export visible operational todos as a formatted Excel workbook."""
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    if warehouse_id is not None:
        require_warehouse_access(principal, warehouse_id, db=db)
        warehouse_ids = (warehouse_id,)
    elif principal.has_role(ROLE_ADMIN, ROLE_OPERATIONS):
        warehouse_ids = None
    else:
        warehouse_ids = principal.warehouse_ids
    from app.operations_workbench import build_operations_today
    try:
        from app.exports import operations_todo_xlsx
    except ImportError as exc:
        raise DomainError(code="EXPORT_XLSX_UNAVAILABLE", message="Excel 导出依赖不可用，请联系管理员", http_status=503) from exc

    try:
        body = build_operations_today(db, workspace_id=principal.workspace_id, warehouse_ids=warehouse_ids, limit=limit)
    except ValueError as exc:
        code = str(exc)
        status = 404 if code == "WORKSPACE_NOT_FOUND" else 422
        raise DomainError(code=code, message="运营待办导出参数无效", http_status=status) from exc

    def values(raw: str | None) -> set[str] | None:
        if raw is None or not raw.strip():
            return None
        parsed = {part.strip() for part in raw.split(",") if part.strip()}
        return parsed or None

    selected_ids = values(todo_ids)
    selected_types = values(types)
    selected_priorities = values(priorities)
    items = [
        item for item in body["items"]
        if (selected_ids is None or item["todo_id"] in selected_ids)
        and (selected_types is None or item["type"] in selected_types)
        and (selected_priorities is None or item["priority"] in selected_priorities)
    ]
    try:
        content, export_meta = operations_todo_xlsx(items)
    except Exception as exc:
        logger.exception("operations XLSX export generation failed")
        raise DomainError(code="EXPORT_XLSX_FAILED", message="Excel 文件生成失败，请稍后重试", http_status=500) from exc
    filename = f"今日待办清单-{body['as_of']:%Y.%m.%d}.xlsx"
    filename_ascii = f"operations-today-{body['as_of']:%Y.%m.%d}.xlsx"
    from fastapi.responses import Response
    response = Response(content=content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response.headers["Content-Disposition"] = f"attachment; filename=\"{filename_ascii}\"; filename*=UTF-8''{quote(filename)}"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Access-Control-Expose-Headers"] = "Content-Disposition, X-Export-Sha256, X-Export-Row-Count, X-Export-Source, X-Export-Simulated, X-Export-Evidence-Level, X-Export-Data-Completeness, X-Export-Workspace-Id"
    response.headers["X-Export-Sha256"] = export_meta["sha256"]
    response.headers["X-Export-Row-Count"] = str(export_meta["row_count"])
    response.headers["X-Export-Source"] = body["source_mode"]
    response.headers["X-Export-Simulated"] = str(body["simulated"]).lower()
    response.headers["X-Export-Evidence-Level"] = body["evidence_level"]
    response.headers["X-Export-Data-Completeness"] = body["meta"]["data_completeness"]
    response.headers["X-Export-Workspace-Id"] = str(body["workspace_id"])
    return response


@app.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request) -> HTMLResponse:
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    return _render_workspace_page("orders.html", principal, "/orders")


@app.get("/api/orders", response_model=ExternalOrderPage)
def list_external_orders(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    platform: str | None = Query(default=None, max_length=32),
    account_ref: str | None = Query(default=None, max_length=128),
    store_ref: str | None = Query(default=None, max_length=128),
    order_status: str | None = Query(default=None, max_length=32),
    fulfillment_view: str | None = Query(default=None, max_length=32),
    keyword: str | None = Query(default=None, max_length=255),
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
):
    principal = require_principal(request, permission="inventory.read", roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    from app.order_workbench import list_orders
    try:
        return list_orders(db, workspace_id=principal.workspace_id, page=page, page_size=page_size, platform=platform, account_ref=account_ref, store_ref=store_ref, order_status=order_status, fulfillment_view=fulfillment_view, keyword=keyword, date_from=date_from, date_to=date_to)
    except ValueError as exc:
        code = str(exc)
        raise DomainError(code=code, message="订单查询参数无效", http_status=422) from exc


@app.get("/api/orders/fulfillment-proposal.xlsx")
def export_fulfillment_proposal(
    request: Request,
    platform: str | None = Query(default=None, max_length=32),
    account_ref: str | None = Query(default=None, max_length=128),
    store_ref: str | None = Query(default=None, max_length=128),
    order_status: str | None = Query(default="paid", max_length=32),
    keyword: str | None = Query(default=None, max_length=255),
    date_from: date | None = None,
    date_to: date | None = None,
    page_size: int = Query(default=200, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Export paid external-order facts as a manual-only fulfillment proposal."""
    principal = require_principal(request, permission="inventory.read", roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    if order_status not in (None, "paid"):
        raise DomainError(code="FULFILLMENT_PROPOSAL_STATUS_INVALID", message="待发清单只接受外部已支付订单", http_status=422)
    from app.order_workbench import get_orders_with_lines
    from app.exports import fulfillment_proposal_xlsx
    try:
        result = get_orders_with_lines(db, workspace_id=principal.workspace_id, order_status="paid", platform=platform, account_ref=account_ref, store_ref=store_ref, keyword=keyword, date_from=date_from, date_to=date_to, limit=page_size)
    except ValueError as exc:
        raise DomainError(code=str(exc), message="订单导出筛选参数无效", http_status=422) from exc
    orders = result["items"]
    sources = {order["source_mode"] for order in orders}
    completeness = "unknown" if not orders else "complete" if all(order["data_completeness"] == "complete" for order in orders) else "partial"
    source_mode = next(iter(sources)) if len(sources) == 1 else "mixed" if sources else "unknown"
    meta = {"workspace_id": principal.workspace_id, "as_of": datetime.utcnow(), "filter_summary": "外部状态=paid" + (f"；平台={platform}" if platform else "") + (f"；关键词={keyword}" if keyword else ""), "source_mode": source_mode, "simulated": all(order["simulated"] for order in orders) if orders else "unknown", "data_completeness": completeness}
    try:
        content, export_meta = fulfillment_proposal_xlsx(orders, meta=meta)
    except Exception as exc:
        logger.exception("fulfillment proposal XLSX generation failed")
        raise DomainError(code="FULFILLMENT_PROPOSAL_EXPORT_FAILED", message="待发清单生成失败，请稍后重试", http_status=500) from exc
    filename = f"待发清单-人工履约-{datetime.utcnow():%Y.%m.%d}.xlsx"
    filename_ascii = f"fulfillment-proposal-{datetime.utcnow():%Y.%m.%d}.xlsx"
    from fastapi.responses import Response
    response = Response(content=content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response.headers["Content-Disposition"] = f"attachment; filename=\"{filename_ascii}\"; filename*=UTF-8''{quote(filename)}"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Access-Control-Expose-Headers"] = "Content-Disposition, X-Export-Sha256, X-Export-Row-Count, X-Export-Order-Count, X-Export-Total-Orders, X-Export-Data-Completeness, X-Export-Source, X-Export-Simulated"
    response.headers["X-Export-Sha256"] = export_meta["sha256"]
    response.headers["X-Export-Row-Count"] = str(export_meta["row_count"])
    response.headers["X-Export-Order-Count"] = str(export_meta["order_count"])
    response.headers["X-Export-Total-Orders"] = str(result["total"])
    response.headers["X-Export-Data-Completeness"] = completeness
    response.headers["X-Export-Source"] = source_mode
    response.headers["X-Export-Simulated"] = str(meta["simulated"]).lower()
    return response


@app.get("/api/orders/{order_id}", response_model=ExternalOrderDetailResponse)
def get_external_order(order_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="inventory.read", roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    from app.order_workbench import get_order
    result = get_order(db, workspace_id=principal.workspace_id, order_id=order_id)
    if result is None:
        raise DomainError(code="ORDER_NOT_FOUND", message="订单不存在", http_status=404)
    result["lines"] = [ExternalOrderLineResponse.model_validate(line) for line in result["lines"]]
    return result


@app.get("/api/tickets", response_model=TicketPage)
def list_tickets_api(request: Request, page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100), status: str | None = Query(default=None, max_length=24), priority: str | None = Query(default=None, max_length=16), keyword: str | None = Query(default=None, max_length=255), db: Session = Depends(get_db)):
    principal = require_principal(request, permission="knowledge.read")
    from app.tickets import list_tickets
    rows, total = list_tickets(db, workspace_id=principal.workspace_id, page=page, page_size=page_size, status=status, priority=priority, keyword=keyword)
    return TicketPage(items=[TicketResponse.model_validate(row) for row in rows], page=page, page_size=page_size, total=total)


@app.post("/api/tickets", response_model=TicketResponse, status_code=201)
def create_ticket_api(payload: TicketCreateRequest, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="knowledge.write", roles=(ROLE_ADMIN, ROLE_OPERATIONS, ROLE_CUSTOMER_SERVICE))
    from app.tickets import create_ticket
    try:
        ticket = create_ticket(db, workspace_id=principal.workspace_id, subject=payload.subject, priority=payload.priority, issue_type=payload.issue_type, description=payload.description, sku_ref=payload.sku_ref, customer_ref=payload.customer_ref, external_order_ref=payload.external_order_ref, actor=principal.subject, idempotency_key=request.headers.get("Idempotency-Key"))
        return ticket
    except ValueError as exc:
        raise DomainError(code=str(exc), message="工单创建失败", http_status=422) from exc


@app.get("/api/tickets/{ticket_id}")
def get_ticket_api(ticket_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="knowledge.read")
    from app.tickets import ticket_detail
    result = ticket_detail(db, workspace_id=principal.workspace_id, ticket_id=ticket_id)
    if result is None: raise DomainError(code="TICKET_NOT_FOUND", message="工单不存在", http_status=404)
    return {"ticket": TicketResponse.model_validate(result["ticket"]), "events": [{"id": e.id, "event_type": e.event_type, "body": e.body, "author": e.author, "created_at": e.created_at, "source_mode": e.source_mode, "simulated": e.simulated} for e in result["events"]], "assignments": [{"assignee": a.assignee, "actor": a.actor, "created_at": a.created_at, "ended_at": a.ended_at} for a in result["assignments"]], "links": [{"entity_type": l.entity_type, "entity_id": l.entity_id, "relation": l.relation} for l in result["links"]]}


@app.post("/api/tickets/{ticket_id}/events")
def add_ticket_event_api(ticket_id: int, payload: TicketEventRequest, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="knowledge.write", roles=(ROLE_ADMIN, ROLE_OPERATIONS, ROLE_CUSTOMER_SERVICE))
    from app.tickets import add_event
    try:
        event = add_event(db, workspace_id=principal.workspace_id, ticket_id=ticket_id, body=payload.body, author=principal.subject, idempotency_key=request.headers.get("Idempotency-Key"))
        return {"id": event.id, "event_type": event.event_type, "body": event.body, "created_at": event.created_at}
    except ValueError as exc: raise DomainError(code=str(exc), message="工单事件失败", http_status=422) from exc


@app.post("/api/tickets/{ticket_id}/transition", response_model=TicketResponse)
def transition_ticket_api(ticket_id: int, payload: TicketTransitionRequest, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="knowledge.write", roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    from app.tickets import transition_ticket
    try: return transition_ticket(db, workspace_id=principal.workspace_id, ticket_id=ticket_id, target=payload.target, actor=principal.subject, expected_version=payload.expected_version, idempotency_key=request.headers.get("Idempotency-Key") or "")
    except ValueError as exc: raise DomainError(code=str(exc), message="工单状态更新失败", http_status=409 if "CONFLICT" in str(exc) or "TRANSITION" in str(exc) else 422) from exc


@app.post("/api/tickets/{ticket_id}/assign", response_model=TicketResponse)
def assign_ticket_api(ticket_id: int, payload: TicketAssignRequest, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="knowledge.write", roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    from app.tickets import assign_ticket
    try: return assign_ticket(db, workspace_id=principal.workspace_id, ticket_id=ticket_id, assignee=payload.assignee, actor=principal.subject, expected_version=payload.expected_version, idempotency_key=request.headers.get("Idempotency-Key") or "")
    except ValueError as exc: raise DomainError(code=str(exc), message="工单分配失败", http_status=409 if "CONFLICT" in str(exc) else 422) from exc


@app.get("/api/dashboard/summary", response_model=DashboardSummaryResponse)
def dashboard_summary(
    request: Request,
    days: int = Query(default=7, ge=1, le=90),
    as_of: date | None = Query(default=None),
    warehouse_id: int | None = Query(default=None, gt=0),
    sku_id: int | None = Query(default=None, gt=0),
    recent_limit: int = Query(default=10, ge=1, le=100),
    db: Session = Depends(get_db),
):
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    if warehouse_id is not None:
        require_warehouse_access(principal, warehouse_id, db=db)
    if sku_id is not None:
        from app.db import ProductSku
        sku = db.scalar(select(ProductSku).where(ProductSku.id == sku_id, ProductSku.workspace_id == principal.workspace_id, ProductSku.is_active.is_(True)))
        if sku is None:
            raise DomainError(code="SKU_NOT_FOUND", message="SKU 不存在", http_status=404)
    from app.analytics_queries import AnalyticsQueryService
    return AnalyticsQueryService(db).dashboard_summary(
        workspace_id=principal.workspace_id,
        days=days,
        as_of=as_of,
        warehouse_id=warehouse_id,
        sku_id=sku_id,
        recent_limit=recent_limit,
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request) -> HTMLResponse:
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    return _render_workspace_page("dashboard.html", principal, "/dashboard")





@app.get("/api/external/shopify/capabilities")
def shopify_capabilities(request: Request):
    require_principal(request, permission="inventory.read")
    settings = get_settings()
    # 热开关（运行期开关）与 .env 配置共同决定是否放行 live 通道；
    # 运行时被关掉时 live_enabled=false 且 simulated=true。
    live_allowed = bool(settings.shopify_live_enabled and runtime_live_enabled())
    live_ready = bool(live_allowed and settings.shopify_store_domain and settings.shopify_access_token)
    return {"platform": "shopify", "read_only": True, "live_enabled": live_ready, "simulated": not live_allowed, "store_domain": settings.shopify_store_domain or None, "api_version": settings.shopify_api_version, "granted_scopes": ["read_customers", "read_inventory", "read_orders", "read_products"], "limitations": ["当前只读，不执行 Shopify mutation", "订单历史范围受 granted scopes 和 Shopify 规则限制", "库存 GraphQL 字段以当前店铺 schema 为准"]}


@app.post("/api/external/shopify/preview", response_model=ShopifyPreviewResponse)
def shopify_preview(payload: ShopifyPreviewRequest, request: Request):
    require_principal(request, permission="inventory.read")
    settings = get_settings()
    from app.platform_adapters.shopify import ShopifyFixtureTransport, ShopifyLiveTransport, ShopifyReadOnlyAdapter
    if payload.live:
        if not (settings.shopify_live_enabled and runtime_live_enabled() and settings.shopify_store_domain and settings.shopify_access_token):
            raise DomainError(code="SHOPIFY_LIVE_NOT_CONFIGURED", message="Shopify live 未配置店铺、Token 或启用开关", http_status=409)
        transport = ShopifyLiveTransport(settings.shopify_store_domain, settings.shopify_access_token, settings.shopify_api_version, settings.shopify_request_timeout_seconds, settings.shopify_max_retries)
        adapter = ShopifyReadOnlyAdapter(transport, live_enabled=True, simulated=False, page_size=settings.shopify_max_page_size, max_pages=settings.shopify_max_pages)
    else:
        empty = {"data": {"products": {"edges": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}, "orders": {"edges": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}, "locations": {"edges": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
        adapter = ShopifyReadOnlyAdapter(ShopifyFixtureTransport({"products": empty, "orders": empty, "inventory": empty}), simulated=True, page_size=settings.shopify_max_page_size, max_pages=settings.shopify_max_pages)
    records = getattr(adapter, f"fetch_{payload.resource}")()
    return ShopifyPreviewResponse(resource=payload.resource, live_enabled=adapter.live_enabled, simulated=adapter.simulated, store_domain=settings.shopify_store_domain or None, total=len(records), records=records, request_ids=[], limitations=["Preview 不写入内部事实层；使用 sync 入口才会写入订单/库存外部事实"])


# ---------------------------------------------------------------------------
# Shopify live sync 的运行期护栏：取消 token（进程内）
#
# 单进程假设与 app/background.py 一致：取消标记只保存在本进程内存中；
# 进程重启或多 worker 部署时不可跨进程生效（已知限制见交付说明）。
# ---------------------------------------------------------------------------
_sync_cancel_tokens: dict[str, threading.Event] = {}
_sync_cancel_lock = threading.Lock()
# 记录当前线程登记的 run_id，保证请求结束时能精确注销自己的 token。
_sync_token_local = threading.local()

# 护栏类错误码：走既有错误码体系（AdapterError.code → resource_status_json
# 与 run.error_code），不新增 run 终态，也不改响应模型。
GUARDRAIL_ERROR_CODES = {"SHOPIFY_SYNC_BUDGET_EXCEEDED", "SHOPIFY_SYNC_CANCELLED"}


def _register_sync_cancel_token(run_id: str, *, track_current: bool = True) -> threading.Event:
    """注册（或复用）某个 run 的取消 token。

    复用而非覆盖：取消请求可能先于同步线程登记到达，先到的取消标记
    不能被后到的登记动作清掉。

    track_current=True 时把 run_id 记到线程局部变量，供同步路由结束时
    精确注销自己的 token；取消路由用 track_current=False，避免把别人的
    run 记成"当前线程的 token"。
    """
    with _sync_cancel_lock:
        token = _sync_cancel_tokens.get(run_id)
        if token is None:
            token = threading.Event()
            _sync_cancel_tokens[run_id] = token
    if track_current:
        _sync_token_local.run_id = run_id
    return token


def _unregister_sync_cancel_token(run_id: str) -> None:
    """run 结束（成功/失败/取消/异常）后注销取消 token，避免内存泄漏。"""
    with _sync_cancel_lock:
        _sync_cancel_tokens.pop(run_id, None)
    if getattr(_sync_token_local, "run_id", None) == run_id:
        _sync_token_local.run_id = None


def _shopify_sync_impl(request: Request, db: Session = Depends(get_db), resources: str = "orders,inventory"):
    """Shopify live 同步的内部实现（路由壳见 _shopify_sync_route）。

    拆成 impl + 壳的原因：壳负责在请求结束时（含异常路径）注销取消 token，
    因此同步执行体必须能整体放进一个 try/finally。
    """
    principal = require_principal(request, permission="inventory.write")
    settings = get_settings()
    if not (settings.shopify_live_enabled and runtime_live_enabled() and settings.shopify_store_domain and settings.shopify_access_token):
        raise DomainError(code="SHOPIFY_LIVE_NOT_CONFIGURED", message="Shopify live 未配置", http_status=409)
    from app.platform_adapters.base import AdapterError
    from app.platform_adapters.shopify import ShopifyLiveTransport, ShopifyReadOnlyAdapter
    from app.external_orders import ensure_external_account, ingest_orders
    from app.external_sync import begin_sync_run, finalize_resource_run, initialize_resources, mark_resource_failed, mark_resource_running, mark_resource_succeeded, record_resource_metadata, ImportStats, ingest_inventory
    selected = [item.strip() for item in resources.split(",") if item.strip()]
    allowed = {"orders", "inventory"}
    if not selected or not set(selected).issubset(allowed):
        raise DomainError(code="SHOPIFY_RESOURCE_INVALID", message="当前同步只支持 orders/inventory", http_status=422)
    # run 级 wall-clock 预算：进入路由即开始计时，覆盖 transport 等待与入库。
    budget_seconds = int(settings.shopify_sync_budget_seconds)
    deadline = time.monotonic() + budget_seconds
    transport = ShopifyLiveTransport(settings.shopify_store_domain, settings.shopify_access_token, settings.shopify_api_version, settings.shopify_request_timeout_seconds, settings.shopify_max_retries)
    account_ref = settings.shopify_store_domain
    run = begin_sync_run(db, workspace_id=principal.workspace_id, platform="shopify", sync_type="shopify_live", account_ref=account_ref, store_ref=settings.shopify_store_domain, source_mode="shopify_live", simulated=False)
    # 取消 token：run 建立后立即登记，run 结束（成功/失败/取消/异常）后注销。
    cancel_token = _register_sync_cancel_token(run.run_id)
    adapter = ShopifyReadOnlyAdapter(
        transport,
        live_enabled=True,
        simulated=False,
        page_size=settings.shopify_max_page_size,
        max_pages=settings.shopify_max_pages,
        deadline=deadline,
        cancel_check=cancel_token.is_set,
    )
    initialize_resources(db, run, selected)
    result = {"run_id": run.run_id, "platform": "shopify", "source_mode": "shopify_live", "simulated": False, "resources": {}}
    guardrail_error = None
    for resource in selected:
        mark_resource_running(db, run, resource)
        try:
            # 资源级检查点：资源之间不翻页时也能被取消/预算中止。
            if cancel_token.is_set():
                raise AdapterError("SHOPIFY_SYNC_CANCELLED", "同步已被请求取消")
            if time.monotonic() > deadline:
                raise AdapterError("SHOPIFY_SYNC_BUDGET_EXCEEDED", "同步超出 wall-clock 预算")
            records = adapter.fetch_orders() if resource == "orders" else adapter.fetch_inventory()
            if resource == "orders":
                account = ensure_external_account(db, workspace_id=principal.workspace_id, platform="shopify", account_ref=account_ref, store_ref=settings.shopify_store_domain, source_mode="shopify_live", simulated=False)
                normalized = []
                for row in records:
                    lines = []
                    for line in ((row.get("lineItems") or {}).get("nodes") or []):
                        variant = line.get("variant") or {}
                        amount_set = line.get("originalTotalSet") or {}
                        shop_money = amount_set.get("shopMoney") or {}
                        order_total_set = row.get("currentTotalPriceSet") or {}
                        order_shop_money = order_total_set.get("shopMoney") or {}
                        currency = shop_money.get("currencyCode") or order_shop_money.get("currencyCode") or "USD"
                        line_amount = shop_money.get("amount") or "0"
                        order_amount = order_shop_money.get("amount") or "0"
                        # Amounts are available in the Shopify GraphQL response;
                        # preserve their currency instead of coercing to CNY.
                        lines.append({
                            "external_line_id": line.get("id") or "line",
                            "external_sku": (line.get("sku") or variant.get("id") or "unknown"),
                            "ordered_qty": line.get("quantity", 0),
                            "cancelled_qty": 0,
                            "refunded_qty": 0,
                            "gross_amount": line_amount,
                            "refund_amount": "0",
                            "currency": currency,
                        })
                    financial_status = (row.get("displayFinancialStatus") or "").lower()
                    status = {"paid": "paid", "partially_paid": "paid", "refunded": "completed", "partially_refunded": "completed", "voided": "cancelled", "pending": "pending"}.get(financial_status, "unknown")
                    normalized.append({
                        "account_ref": account_ref,
                        "store_ref": settings.shopify_store_domain,
                        "external_order_no": row.get("id") or row.get("name"),
                        "order_status": status,
                        "external_created_at": row.get("createdAt"),
                        "external_updated_at": row.get("updatedAt") or row.get("createdAt"),
                        "paid_at": row.get("processedAt"),
                        "gross_amount": order_amount,
                        "refund_amount": "0",
                        "currency": (row.get("currentTotalPriceSet") or {}).get("shopMoney", {}).get("currencyCode") or "USD",
                        "lines": lines,
                        "source_mode": "shopify_live",
                        "simulated": False,
                        "status_reason": "Shopify 当前只读查询未包含退款明细；订单与行金额可用，退款金额为 unknown",
                    })
                from app.connectors import load_orders
                from dataclasses import replace
                # Normalize through the existing canonical order contract.
                content = json.dumps(normalized, ensure_ascii=False)
                canonical = [
                    replace(record, source_mode="shopify_live", simulated=False)
                    for record in load_orders(content, platform="shopify", source_mode="json")
                ]
                stats = ingest_orders(db, canonical, workspace_id=principal.workspace_id, sync_run_id=run.run_id)
                mark_resource_succeeded(db, run, resource, ImportStats(total=stats.total, inserted=stats.inserted, updated=stats.updated, no_op=stats.no_op, conflict=stats.conflict, stale=stats.stale))
                result["resources"][resource] = {"total": stats.total, "inserted": stats.inserted, "updated": stats.updated, "no_op": stats.no_op, "conflict": stats.conflict, "data_completeness": "partial", "note": "退款明细未在当前只读查询中请求，退款金额保持 unknown"}
            else:
                from app.connectors import normalize_inventory_row
                canonical = [normalize_inventory_row(row, platform="shopify", source_mode="shopify_live", index=index) for index, row in enumerate(records)]
                stats = ingest_inventory(db, canonical, workspace_id=principal.workspace_id, sync_run_id=run.run_id)
                mark_resource_succeeded(db, run, resource, ImportStats(total=stats.total, inserted=stats.inserted, no_op=stats.no_op, conflict=stats.conflict, snapshot_ids=stats.snapshot_ids))
                result["resources"][resource] = {"total": stats.total, "inserted": stats.inserted, "no_op": stats.no_op, "conflict": stats.conflict, "snapshot_ids": stats.snapshot_ids or [], "stored": True, "data_completeness": "complete"}
            # 页数预算留痕：实际消耗页数 + 本次 wall-clock 预算，写入 resource_status_json。
            record_resource_metadata(db, run, resource, {"pages_used": adapter.pages_used, "budget_seconds": budget_seconds, "max_pages": settings.shopify_max_pages})
            result["resources"][resource]["pages_used"] = adapter.pages_used
        except Exception as exc:
            mark_resource_failed(db, run, resource, exc)
            record_resource_metadata(db, run, resource, {"pages_used": adapter.pages_used, "budget_seconds": budget_seconds, "max_pages": settings.shopify_max_pages})
            result["resources"][resource] = {"error_code": getattr(exc, "code", type(exc).__name__), "error": str(exc), "pages_used": adapter.pages_used}
            if getattr(exc, "code", "") in GUARDRAIL_ERROR_CODES:
                # 护栏终止：不再处理后续资源，run 终态为 failed + 对应错误码。
                guardrail_error = exc
                break
    if guardrail_error is not None:
        # 尚未开始的资源记为同一护栏错误码，保证 run 终态为 failed。
        for resource in selected:
            if resource in result["resources"]:
                continue
            mark_resource_failed(db, run, resource, guardrail_error)
            record_resource_metadata(db, run, resource, {"pages_used": 0, "budget_seconds": budget_seconds, "max_pages": settings.shopify_max_pages})
            result["resources"][resource] = {"error_code": getattr(guardrail_error, "code", type(guardrail_error).__name__), "error": str(guardrail_error), "pages_used": 0}
        finalize_resource_run(db, run, error=guardrail_error)
        # 护栏终止的 run 一律为 failed（即使部分资源此前已成功）。
        if run.status != "failed":
            run.status = "failed"
            db.commit()
            db.refresh(run)
    else:
        finalize_resource_run(db, run)
    result["status"] = run.status
    result["error_code"] = run.error_code
    return result


@app.post("/api/external/shopify/sync")
def shopify_sync(request: Request, db: Session = Depends(get_db), resources: str = "orders,inventory"):
    """Shopify live 同步入口（同步执行；护栏 = wall-clock 预算 + 取消 token）。"""
    try:
        return _shopify_sync_impl(request, db, resources)
    finally:
        # run 结束（成功/失败/取消/异常）即注销本线程登记的取消 token。
        run_id = getattr(_sync_token_local, "run_id", None)
        if run_id:
            _unregister_sync_cancel_token(run_id)


@app.post("/api/external/sync/runs/{run_id}/cancel", status_code=202)
def cancel_external_sync_run(run_id: str, request: Request, db: Session = Depends(get_db)):
    """请求取消运行中的同步 run（当前只有 Shopify live sync 消费该取消标记）。

    语义：
    - run 不存在或不属于当前 workspace → 404 SYNC_RUN_NOT_FOUND；
    - run 已是终态（succeeded/partial/failed）→ 409 SYNC_RUN_NOT_CANCELLABLE；
    - 运行中 → 202，实际终止由同步路径在下一个分页/资源检查点完成。
    """
    principal = require_principal(request, permission="inventory.write")
    from app.db import ExternalSyncRun
    run = db.scalar(select(ExternalSyncRun).where(ExternalSyncRun.run_id == run_id, ExternalSyncRun.workspace_id == principal.workspace_id))
    if run is None:
        raise DomainError(code="SYNC_RUN_NOT_FOUND", message="同步运行不存在", http_status=404)
    if run.status != "running":
        raise DomainError(code="SYNC_RUN_NOT_CANCELLABLE", message="同步运行已结束，无法取消", http_status=409)
    effective = bool(run.platform == "shopify" and run.source_mode == "shopify_live")
    if effective:
        token = _register_sync_cancel_token(run.run_id)
        token.set()
    logger.info("shopify sync 取消请求已登记: run_id=%s effective=%s by user=%s", run.run_id, effective, principal.user_id)
    return {
        "run_id": run.run_id,
        "cancel_requested": True,
        "effective": effective,
        "status": run.status,
        "note": "取消将在下一个分页/资源检查点生效" if effective else "该 run 不是 Shopify live 同步，取消标记不会被消费",
    }


def external_orders_preview(payload: ExternalOrderPreviewRequest, request: Request):
    require_principal(request, permission="inventory.read")
    from app.connectors import load_orders
    try:
        records = load_orders(payload.content, platform=payload.platform, source_mode=payload.source_mode)
    except ValueError as exc:
        raise DomainError(code=str(exc) if str(exc) else "EXTERNAL_INPUT_INVALID", message=str(exc), http_status=422) from exc
    return ExternalOrderPreviewResponse(
        platform=payload.platform, source_mode=payload.source_mode, simulated=True,
        live_enabled=False, normalized_rows=[record.as_dict() for record in records],
        total=len(records), errors=[], writes=[],
    )


@app.post("/api/external/orders/ingest", response_model=ExternalOrderIngestResponse)
def external_orders_ingest(payload: ExternalOrderIngestRequest, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="catalog.write")
    from app.connectors import load_orders
    from app.external_orders import ingest_orders
    from app.external_sync import begin_sync_run, complete_sync_run, fail_sync_run, ImportStats
    run = None
    try:
        records = load_orders(payload.content, platform=payload.platform, source_mode=payload.source_mode)
        account_ref = records[0].account_ref if records else None
        store_ref = records[0].store_ref if records else None
        if any(record.account_ref != account_ref or record.store_ref != store_ref for record in records):
            raise ValueError("MIXED_EXTERNAL_ACCOUNT")
        run = begin_sync_run(db, workspace_id=principal.workspace_id, platform=payload.platform, sync_type="orders", account_ref=account_ref, store_ref=store_ref, source_mode=payload.source_mode)
        result = ingest_orders(db, records, workspace_id=principal.workspace_id, sync_run_id=run.run_id)
        sync_stats = ImportStats(total=result.total, inserted=result.inserted, no_op=result.no_op, conflict=result.conflict)
        complete_sync_run(db, run, sync_stats)
    except ValueError as exc:
        if run is not None:
            fail_sync_run(db, run, exc)
        raise DomainError(code=str(exc) if str(exc) else "EXTERNAL_INPUT_INVALID", message=str(exc), http_status=422) from exc
    except Exception as exc:
        if run is not None:
            fail_sync_run(db, run, exc)
        raise DomainError(code="EXTERNAL_SYNC_FAILED", message="外部订单同步失败", http_status=500) from exc
    return ExternalOrderIngestResponse(
        platform=payload.platform, source_mode=payload.source_mode, simulated=True, live_enabled=False,
        total=result.total, inserted=result.inserted, updated=result.updated, no_op=result.no_op,
        conflict=result.conflict, stale=result.stale, affected_dates=result.affected_dates or [],
        sync_run_id=result.sync_run_id, sync_status="succeeded",
    )


@app.get("/api/external/accounts", response_model=list[ExternalAccountResponse])
def external_accounts(request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="inventory.read")
    from app.db import ExternalAccount
    return db.scalars(select(ExternalAccount).where(ExternalAccount.workspace_id == principal.workspace_id).order_by(ExternalAccount.id)).all()


@app.get("/api/external/product-mappings", response_model=list[ExternalProductMappingResponse])
def external_product_mappings(request: Request, external_account_id: int | None = Query(default=None, gt=0), db: Session = Depends(get_db)):
    principal = require_principal(request, permission="inventory.read")
    from app.db import ExternalProductMapping
    filters = [ExternalProductMapping.workspace_id == principal.workspace_id]
    if external_account_id is not None:
        filters.append(ExternalProductMapping.external_account_id == external_account_id)
    return db.scalars(select(ExternalProductMapping).where(*filters).order_by(ExternalProductMapping.id)).all()


@app.put("/api/external/product-mappings", response_model=ExternalProductMappingResponse)
def save_external_product_mapping(payload: ExternalProductMappingRequest, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="catalog.write")
    from app.external_orders import set_product_mapping
    try:
        return set_product_mapping(db, workspace_id=principal.workspace_id, external_account_id=payload.external_account_id, external_sku=payload.external_sku, internal_sku_id=payload.internal_sku_id)
    except ValueError as exc:
        code = str(exc)
        status = 404 if code.endswith("NOT_FOUND") else 422
        raise DomainError(code=code, message="外部 SKU 映射无效", http_status=status) from exc


@app.get("/api/external/sales/daily", response_model=DailySkuSalePage)
def external_daily_sales(
    request: Request,
    platform: str | None = None,
    account_ref: str | None = None,
    store_ref: str | None = None,
    external_sku: str | None = None,
    internal_sku_id: int | None = Query(default=None, gt=0),
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    principal = require_principal(request, permission="inventory.read")
    if date_from and date_to and date_from > date_to:
        raise DomainError(code="INVALID_DATE_RANGE", message="起始日期不能晚于结束日期", http_status=422)
    from app.external_orders import list_daily_sales
    rows, total = list_daily_sales(db, workspace_id=principal.workspace_id, platform=platform, account_ref=account_ref, store_ref=store_ref, external_sku=external_sku, internal_sku_id=internal_sku_id, start=date_from, end=date_to, limit=page_size, offset=(page - 1) * page_size)
    return DailySkuSalePage(items=rows, page=page, page_size=page_size, total=total)


def _replenishment_error(exc: ValueError) -> DomainError:
    code = str(exc)
    if str(exc) == "IDEMPOTENCY_KEY_REQUIRED":
        return DomainError(code=str(exc), message="必须提供 Idempotency-Key", http_status=422)
    special = {"OPEN_SUGGESTION_EXISTS", "IDEMPOTENCY_KEY_REUSE", "SUGGESTION_VERSION_CONFLICT", "INVALID_SUGGESTION_STATE", "SUGGESTION_NOT_READY", "MIXED_WAREHOUSE", "SUGGESTION_ALREADY_SUBMITTED", "SUGGESTION_ALREADY_IN_PURCHASE_REQUEST", "INVALID_PURCHASE_REQUEST_STATE", "PURCHASE_REQUEST_VERSION_CONFLICT", "SUGGESTION_STALE"}
    status = 404 if code.endswith("NOT_FOUND") else 409 if code in special else 422
    if code in {"INVALID_EVALUATION_WINDOW", "EVALUATION_WINDOW_BEFORE_SUGGESTION", "SUGGESTION_DATA_INCOMPLETE"}:
        status = 422
    return DomainError(code=code, message="补货决策操作失败", http_status=status)


def _purchase_request_response(db: Session, request_record):
    from app.db import PurchaseRequestLine
    lines = db.scalars(select(PurchaseRequestLine).where(
        PurchaseRequestLine.workspace_id == request_record.workspace_id,
        PurchaseRequestLine.purchase_request_id == request_record.id,
    ).order_by(PurchaseRequestLine.id)).all()
    return PurchaseRequestResponse.model_validate({
        "id": request_record.id, "workspace_id": request_record.workspace_id,
        "warehouse_id": request_record.warehouse_id, "request_no": request_record.request_no,
        "status": request_record.status, "note": request_record.note,
        "submitted_by": request_record.submitted_by, "submitted_at": request_record.submitted_at,
        "created_by": request_record.created_by, "version": request_record.version,
        "supplier_ref": request_record.supplier_ref, "expected_arrival_date": request_record.expected_arrival_date,
        "idempotency_key": request_record.idempotency_key,
        "created_at": request_record.created_at, "updated_at": request_record.updated_at,
        "lines": [PurchaseRequestLineResponse.model_validate(line) for line in lines],
    })


@app.get("/api/analytics/replenishment-evaluation", response_model=ReplenishmentEvaluationPage)
def list_replenishment_evaluations_api(
    request: Request,
    suggestion_id: int | None = Query(default=None, gt=0),
    warehouse_id: int | None = Query(default=None, gt=0),
    sku_id: int | None = Query(default=None, gt=0),
    status: str | None = Query(default=None, max_length=16),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    principal = require_principal(request, permission="replenishment.read")
    if warehouse_id is not None:
        require_warehouse_access(principal, warehouse_id, db=db)
    from app.replenishment import list_replenishment_evaluations
    rows, total = list_replenishment_evaluations(
        db, workspace_id=principal.workspace_id, suggestion_id=suggestion_id,
        warehouse_id=warehouse_id, sku_id=sku_id, status=status,
        limit=page_size, offset=(page - 1) * page_size,
    )
    return {
        "items": [ReplenishmentEvaluationResponse.model_validate(item).model_dump(mode="json") for item in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@app.get("/api/analytics/replenishment-decisions")
def replenishment_decision_metrics_api(
    request: Request,
    as_of: date | None = Query(default=None),
    db: Session = Depends(get_db),
):
    principal = require_principal(request, permission="replenishment.read")
    from app.replenishment import build_replenishment_decision_metrics
    result = build_replenishment_decision_metrics(db, workspace_id=principal.workspace_id, as_of=as_of)
    return {**result, "meta": {"workspace_id": principal.workspace_id, "as_of": result["as_of"], "timezone": "UTC", "source": "ReplenishmentSuggestion", "data_completeness": result["data_completeness"], "metric_version": "1", "limitations": ["当前 PurchaseRequestLine 与 InboundOrder 没有明确外键关联，生成到人工入库确认耗时返回 unknown"]}}


@app.get("/api/analytics/sales")
def analytics_sales(
    request: Request,
    as_of: date | None = Query(default=None),
    db: Session = Depends(get_db),
):
    principal = require_principal(request, permission="analytics.read")
    from app.analytics_queries import AnalyticsQueryService
    return AnalyticsQueryService(db).sales(workspace_id=principal.workspace_id, as_of=as_of)


@app.get("/api/analytics/inventory-health")
def analytics_inventory_health(
    request: Request,
    coverage_days: Literal[7, 14, 30] = Query(default=14),
    as_of: date | None = Query(default=None),
    warehouse_id: int | None = Query(default=None, gt=0),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    principal = require_principal(request, permission="analytics.read")
    if warehouse_id is not None:
        require_warehouse_access(principal, warehouse_id, db=db)
    from app.analytics_queries import AnalyticsQueryService
    return AnalyticsQueryService(db).inventory_health(
        workspace_id=principal.workspace_id,
        coverage_days=coverage_days,
        as_of=as_of,
        warehouse_id=warehouse_id,
        page=page,
        page_size=page_size,
    )


@app.get("/api/analytics/product-quadrant", response_model=ProductQuadrantResponse)
def product_quadrant(
    request: Request,
    as_of: date | None = Query(default=None),
    growth_window: int = Query(default=7),
    baseline_window: int = Query(default=14),
    days_window: int = Query(default=14),
    warehouse_id: int | None = Query(default=None, gt=0),
    growth_high: float | None = Query(default=None),
    days_low: float | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    principal = require_principal(request, permission="analytics.read")
    if warehouse_id is not None:
        require_warehouse_access(principal, warehouse_id, db=db)
    from app.analytics_queries import AnalyticsQueryError, AnalyticsQueryService
    try:
        summary = AnalyticsQueryService(db).product_quadrant(
            workspace_id=principal.workspace_id,
            as_of=as_of,
            growth_window=growth_window,
            baseline_window=baseline_window,
            days_window=days_window,
            warehouse_id=warehouse_id,
            growth_high=growth_high,
            days_low=days_low,
            page=page,
            page_size=page_size,
        )
    except AnalyticsQueryError as exc:
        code = exc.code
        status = 422 if code.endswith("WINDOW") or code.endswith("RANGE") else 404
        raise DomainError(code=code, message="商品四象限参数无效", http_status=status) from exc
    return ProductQuadrantResponse(
        items=[ProductQuadrantItem.model_validate(item) for item in summary.pop("items")],
        **summary,
    )


@app.post("/api/analytics/replenishment-evaluation", response_model=ReplenishmentEvaluationResponse, status_code=201)
def evaluate_replenishment_api(
    suggestion_id: int = Query(..., gt=0),
    window_start: date = Query(...),
    window_end: date = Query(...),
    request: Request = None,
    db: Session = Depends(get_db),
):
    principal = require_principal(request, permission="replenishment.read")
    from app.replenishment import evaluate_replenishment
    from app.db import ReplenishmentSuggestion
    try:
        suggestion = db.scalar(select(ReplenishmentSuggestion).where(
            ReplenishmentSuggestion.id == suggestion_id,
            ReplenishmentSuggestion.workspace_id == principal.workspace_id,
        ))
        if suggestion is None:
            raise ValueError("SUGGESTION_NOT_FOUND")
        require_warehouse_access(principal, suggestion.warehouse_id, db=db)
        result = evaluate_replenishment(
            db, workspace_id=principal.workspace_id, suggestion_id=suggestion_id,
            window_start=window_start, window_end=window_end,
        )
        return ReplenishmentEvaluationResponse.model_validate(result)
    except ValueError as exc:
        raise _replenishment_error(exc) from exc



@app.get("/api/replenishment/suggestions", response_model=ReplenishmentSuggestionPage)
def list_replenishment_suggestions(request: Request, warehouse_id: int | None = Query(default=None, gt=0), sku_id: int | None = Query(default=None, gt=0), status: str | None = Query(default=None, max_length=16), page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)):
    principal = require_principal(request, permission="replenishment.read")
    if warehouse_id is not None: require_warehouse_access(principal, warehouse_id, db=db)
    from app.replenishment import list_suggestions
    scoped_warehouse_ids = None if principal.has_role(ROLE_ADMIN, ROLE_OPERATIONS) else principal.warehouse_ids
    rows, total = list_suggestions(db, workspace_id=principal.workspace_id, warehouse_id=warehouse_id, warehouse_ids=scoped_warehouse_ids, status=status, sku_id=sku_id, limit=page_size, offset=(page-1)*page_size)
    return ReplenishmentSuggestionPage(items=rows, page=page, page_size=page_size, total=total)


@app.post("/api/replenishment/suggestions/generate", response_model=ReplenishmentSuggestionResponse, status_code=201)
def generate_replenishment_suggestion(payload: ReplenishmentGenerateRequest, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="replenishment.write")
    require_warehouse_access(principal, payload.warehouse_id, db=db)
    if not request.headers.get("Idempotency-Key"):
        raise _replenishment_error(ValueError("IDEMPOTENCY_KEY_REQUIRED"))
    from app.replenishment import generate_suggestion
    try:
        return generate_suggestion(db, workspace_id=principal.workspace_id, warehouse_id=payload.warehouse_id, sku_id=payload.sku_id, actor=principal.subject, coverage_days=payload.coverage_days, as_of=payload.as_of_date)
    except ValueError as exc: raise _replenishment_error(exc) from exc


@app.post("/api/replenishment/suggestions/{suggestion_id}/decision", response_model=ReplenishmentSuggestionResponse)
def decide_replenishment_suggestion(suggestion_id: int, payload: ReplenishmentDecisionRequest, request: Request, db: Session = Depends(get_db)):
    permission = "replenishment.confirm" if payload.action == "confirm" else "replenishment.ignore" if payload.action == "ignore" else "replenishment.write"
    principal = require_principal(request, permission=permission)
    from app.db import ReplenishmentSuggestion
    suggestion = db.scalar(select(ReplenishmentSuggestion).where(ReplenishmentSuggestion.id == suggestion_id, ReplenishmentSuggestion.workspace_id == principal.workspace_id))
    if suggestion is None: raise _replenishment_error(ValueError("SUGGESTION_NOT_FOUND"))
    require_warehouse_access(principal, suggestion.warehouse_id, db=db)
    from app.replenishment import decide_suggestion
    try:
        return decide_suggestion(db, workspace_id=principal.workspace_id, suggestion_id=suggestion_id, action=payload.action, actor=principal.subject, idempotency_key=request.headers.get("Idempotency-Key") or "", decision_qty=payload.decision_qty, reason=payload.reason, expected_version=payload.expected_version)
    except ValueError as exc: raise _replenishment_error(exc) from exc


@app.post("/api/purchase-requests/drafts", response_model=PurchaseRequestResponse, status_code=201)
def create_purchase_request_draft_api(payload: PurchaseRequestDraftCreate, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="purchasing.submit")
    for suggestion_id in payload.suggestion_ids:
        from app.db import ReplenishmentSuggestion
        suggestion = db.scalar(select(ReplenishmentSuggestion).where(ReplenishmentSuggestion.id == suggestion_id, ReplenishmentSuggestion.workspace_id == principal.workspace_id))
        if suggestion is None:
            raise _replenishment_error(ValueError("SUGGESTION_NOT_FOUND"))
        require_warehouse_access(principal, suggestion.warehouse_id, db=db)
    from app.replenishment import create_purchase_request_draft
    try:
        result = create_purchase_request_draft(db, workspace_id=principal.workspace_id, suggestion_ids=payload.suggestion_ids, actor=principal.subject, idempotency_key=request.headers.get("Idempotency-Key") or "", note=payload.note, supplier_ref=payload.supplier_ref, expected_arrival_date=payload.expected_arrival_date)
        return _purchase_request_response(db, result)
    except ValueError as exc:
        raise _replenishment_error(exc) from exc


@app.patch("/api/purchase-requests/{request_id}", response_model=PurchaseRequestResponse)
def edit_purchase_request_draft_api(request_id: int, payload: PurchaseRequestDraftUpdate, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="purchasing.submit")
    from app.db import PurchaseRequest

    # 先按 workspace 定位并校验仓库范围，再进入会 commit 的领域服务。
    # 越权请求必须保持数据库零变化。
    draft = db.scalar(select(PurchaseRequest).where(
        PurchaseRequest.id == request_id,
        PurchaseRequest.workspace_id == principal.workspace_id,
    ))
    if draft is None:
        raise _replenishment_error(ValueError("PURCHASE_REQUEST_NOT_FOUND"))
    require_warehouse_access(principal, draft.warehouse_id, db=db)
    from app.replenishment import edit_purchase_request_draft
    try:
        result = edit_purchase_request_draft(db, workspace_id=principal.workspace_id, request_id=request_id, actor=principal.subject, idempotency_key=request.headers.get("Idempotency-Key") or "", expected_version=payload.expected_version, note=payload.note, supplier_ref=payload.supplier_ref, expected_arrival_date=payload.expected_arrival_date)
        return _purchase_request_response(db, result)
    except ValueError as exc:
        raise _replenishment_error(exc) from exc


@app.post("/api/purchase-requests/{request_id}/submit", response_model=PurchaseRequestResponse)
def submit_purchase_request_draft_api(request_id: int, payload: PurchaseRequestDraftSubmit, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="purchasing.submit")
    from app.db import PurchaseRequest

    # 提交同样必须在领域服务写库前完成 workspace/warehouse 鉴权。
    draft = db.scalar(select(PurchaseRequest).where(
        PurchaseRequest.id == request_id,
        PurchaseRequest.workspace_id == principal.workspace_id,
    ))
    if draft is None:
        raise _replenishment_error(ValueError("PURCHASE_REQUEST_NOT_FOUND"))
    require_warehouse_access(principal, draft.warehouse_id, db=db)
    from app.replenishment import submit_purchase_request_draft
    try:
        result = submit_purchase_request_draft(db, workspace_id=principal.workspace_id, request_id=request_id, actor=principal.subject, idempotency_key=request.headers.get("Idempotency-Key") or "", expected_version=payload.expected_version)
        return _purchase_request_response(db, result)
    except ValueError as exc:
        raise _replenishment_error(exc) from exc
@app.post("/api/purchase-requests", response_model=PurchaseRequestResponse, status_code=201)
def submit_purchase_request_api(payload: PurchaseRequestCreate, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="purchasing.submit")
    from app.db import ReplenishmentSuggestion
    suggestions = db.scalars(select(ReplenishmentSuggestion).where(ReplenishmentSuggestion.workspace_id == principal.workspace_id, ReplenishmentSuggestion.id.in_(payload.suggestion_ids))).all()
    if not suggestions:
        raise _replenishment_error(ValueError("SUGGESTION_NOT_FOUND"))
    for suggestion in suggestions:
        require_warehouse_access(principal, suggestion.warehouse_id, db=db)
    from app.replenishment import submit_purchase_request
    try:
        result = submit_purchase_request(db, workspace_id=principal.workspace_id, suggestion_ids=payload.suggestion_ids, actor=principal.subject, idempotency_key=request.headers.get("Idempotency-Key") or "", note=payload.note)
        return _purchase_request_response(db, result)
    except ValueError as exc:
        raise _replenishment_error(exc) from exc


@app.get("/api/purchase-requests/{request_id}", response_model=PurchaseRequestResponse)
def get_purchase_request(request_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="purchase.read")
    from app.db import PurchaseRequest
    result = db.scalar(select(PurchaseRequest).where(PurchaseRequest.id == request_id, PurchaseRequest.workspace_id == principal.workspace_id))
    if result is None: raise DomainError(code="PURCHASE_REQUEST_NOT_FOUND", message="采购申请不存在", http_status=404)
    require_warehouse_access(principal, result.warehouse_id, db=db)
    return _purchase_request_response(db, result)


@app.get("/api/purchase-requests", response_model=PurchaseRequestPage)
def list_purchase_requests(request: Request, page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100), warehouse_id: int | None = Query(default=None, gt=0), status: str | None = Query(default=None, max_length=16), db: Session = Depends(get_db)):
    principal = require_principal(request, permission="purchase.read")
    if warehouse_id is not None: require_warehouse_access(principal, warehouse_id, db=db)
    from app.db import PurchaseRequest
    from sqlalchemy import func
    filters = [PurchaseRequest.workspace_id == principal.workspace_id]
    if warehouse_id is not None: filters.append(PurchaseRequest.warehouse_id == warehouse_id)
    if not principal.has_role(ROLE_ADMIN, ROLE_OPERATIONS):
        filters.append(PurchaseRequest.warehouse_id.in_(principal.warehouse_ids))
    if status is not None: filters.append(PurchaseRequest.status == status)
    total = db.scalar(select(func.count(PurchaseRequest.id)).where(*filters)) or 0
    rows = db.scalars(select(PurchaseRequest).where(*filters).order_by(PurchaseRequest.id.desc()).offset((page-1)*page_size).limit(page_size)).all()
    return PurchaseRequestPage(items=[_purchase_request_response(db, row) for row in rows], page=page, page_size=page_size, total=int(total))


@app.post("/api/external/sync/fixture", response_model=FixtureSyncResponse)
def external_fixture_sync(payload: FixtureSyncRequest, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="catalog.write")
    from app.sync_application import SyncApplicationError, SyncApplicationService, fixture_response_data

    try:
        result = SyncApplicationService(db).run_fixture_bundle(
            workspace_id=principal.workspace_id,
            platform=payload.platform,
            account_ref=payload.account_ref,
            store_ref=payload.store_ref,
            orders_content=payload.orders_content,
            inventory_content=payload.inventory_content,
            source_mode=payload.source_mode,
        )
    except SyncApplicationError as exc:
        raise DomainError(code=exc.code, message=exc.message, http_status=422) from exc

    return FixtureSyncResponse(
        platform=payload.platform,
        account_ref=payload.account_ref,
        store_ref=payload.store_ref,
        simulated=True,
        live_enabled=False,
        **fixture_response_data(result),
    )


@app.get("/api/external/sync/runs/{run_id}", response_model=ExternalSyncRunResponse)
def get_external_sync_run(run_id: str, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="inventory.read")
    from app.db import ExternalSyncRun
    from app.external_sync import resource_status, retryable_resources
    run = db.scalar(select(ExternalSyncRun).where(ExternalSyncRun.run_id == run_id, ExternalSyncRun.workspace_id == principal.workspace_id))
    if run is None:
        raise DomainError(code="SYNC_RUN_NOT_FOUND", message="同步运行不存在", http_status=404)
    return _external_sync_run_response(run)


def _external_sync_run_response(run):
    from app.external_sync import resource_status, retryable_resources
    from datetime import datetime
    now = datetime.utcnow()
    heartbeat = run.heartbeat_at or run.started_at
    return ExternalSyncRunResponse(
        run_id=run.run_id, workspace_id=run.workspace_id, platform=run.platform,
        account_ref=run.account_ref, store_ref=run.store_ref, sync_type=run.sync_type,
        source_mode=run.source_mode, simulated=run.simulated, status=run.status,
        attempt=run.attempt or 1, retry_of_run_id=run.retry_of_run_id,
        started_at=run.started_at, finished_at=run.finished_at, heartbeat_at=run.heartbeat_at,
        duration_seconds=int(((run.finished_at or now) - run.started_at).total_seconds()) if run.started_at else None,
        heartbeat_age_seconds=max(0, int((now - heartbeat).total_seconds())) if heartbeat else None,
        total=run.total, inserted=run.inserted, updated=getattr(run, "updated", 0), no_op=run.no_op, conflict=run.conflict, stale=getattr(run, "stale", 0),
        error_code=run.error_code, error_message=run.error_message,
        resource_status=resource_status(run), retryable_resources=retryable_resources(run),
    )


@app.post("/api/external/sync/runs/{run_id}/retry", response_model=ExternalSyncRunResponse)
def retry_external_sync_run(run_id: str, payload: ExternalSyncRetryRequest, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="catalog.write")
    key = request.headers.get("Idempotency-Key")
    if not key:
        raise DomainError(code="IDEMPOTENCY_KEY_REQUIRED", message="必须提供 Idempotency-Key", http_status=422)
    if len(key) > 128:
        raise DomainError(code="IDEMPOTENCY_KEY_TOO_LONG", message="Idempotency-Key 超过长度限制", http_status=422)
    from app.sync_application import SyncApplicationError, SyncApplicationService

    try:
        result = SyncApplicationService(db).retry_fixture_bundle(
            workspace_id=principal.workspace_id,
            run_id=run_id,
            resources=payload.resources,
            orders_content=payload.orders_content,
            inventory_content=payload.inventory_content,
            idempotency_key=key,
        )
    except SyncApplicationError as exc:
        status_by_code = {
            "SYNC_RUN_NOT_FOUND": 404,
            "SYNC_RUN_NOT_RETRYABLE": 409,
            "SYNC_RESOURCE_NOT_RETRYABLE": 409,
            "IDEMPOTENCY_KEY_REUSE": 409,
            "SYNC_RETRY_PERSIST_FAILED": 500,
        }
        status_code = status_by_code.get(exc.code, 422)
        raise DomainError(code=exc.code, message=exc.message, http_status=status_code) from exc

    response = _external_sync_run_response(result["run"])
    if result["replayed"]:
        return JSONResponse(status_code=200, content=response.model_dump(mode="json"))
    return JSONResponse(status_code=201, content=response.model_dump(mode="json"))


@app.get("/api/external/sync/runs", response_model=list[ExternalSyncRunResponse])
def list_external_sync_runs(request: Request, platform: str | None = None, status: str | None = None, limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)):
    principal = require_principal(request, permission="inventory.read")
    from app.db import ExternalSyncRun
    filters = [ExternalSyncRun.workspace_id == principal.workspace_id]
    if platform: filters.append(ExternalSyncRun.platform == platform)
    if status: filters.append(ExternalSyncRun.status == status)
    rows = db.scalars(select(ExternalSyncRun).where(*filters).order_by(ExternalSyncRun.id.desc()).limit(limit)).all()
    return [_external_sync_run_response(row) for row in rows]



@app.get("/api/external/snapshots/freshness")
def external_snapshot_freshness(request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="inventory.read")
    from app.external_sync import snapshot_freshness
    return {"items": snapshot_freshness(db, workspace_id=principal.workspace_id, stale_after_seconds=get_settings().external_snapshot_stale_after_seconds)}


@app.post("/api/external/alerts/refresh", response_model=list[AlertResponse])
def refresh_external_alerts_api(request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="inventory.write")
    from app.alerts import refresh_external_alerts
    return refresh_external_alerts(db, workspace_id=principal.workspace_id, stale_after_seconds=get_settings().external_snapshot_stale_after_seconds, sync_stale_after_seconds=get_settings().external_sync_run_stale_after_seconds)


@app.get("/api/external/connectors")
def external_connectors(request: Request):
    principal = require_principal(request, permission="inventory.read")
    from app.connectors import connector_capabilities
    return {"items": connector_capabilities()}


@app.get("/api/external/taobao/capabilities", response_model=TaobaoCapabilitiesResponse)
def taobao_capabilities(request: Request):
    require_principal(request, permission="inventory.read")
    settings = get_settings()
    return TaobaoCapabilitiesResponse(
        platform="taobao", read_only=True, live_enabled=False, simulated=True,
        enabled=settings.taobao_adapter_enabled,
        resources=["orders", "inventory"],
        limitations=["仅只读", "当前 preview 仅接受脱敏 fixture，不访问远程 URL", "不会自动下单、付款、退款、取消、改价或回写库存"],
    )


@app.post("/api/external/taobao/preview", response_model=TaobaoPreviewResponse)
def taobao_preview(payload: TaobaoPreviewRequest, request: Request):
    require_principal(request, permission="inventory.read")
    from app.platform_adapters.base import AdapterError
    from app.platform_adapters.taobao import TaobaoAdapter, fixture_transport
    from app.platform_sync import preview_adapter
    try:
        adapter = TaobaoAdapter(enabled=True, simulated=True, transport=fixture_transport(payload.content, resource=payload.resource), max_page_size=payload.max_pages)
        adapter.live_enabled = True
        result = preview_adapter(adapter, account_ref=payload.account_ref, store_ref=payload.store_ref, resource=payload.resource, max_pages=payload.max_pages)
    except AdapterError as exc:
        raise DomainError(code=exc.code, message=str(exc), http_status=422) from exc
    except ValueError as exc:
        raise DomainError(code="TAOBAO_FIELD_INVALID", message=str(exc), http_status=422) from exc
    return TaobaoPreviewResponse(
        platform="taobao", resource=payload.resource, read_only=True,
        live_enabled=False, simulated=True, total=result["total"],
        normalized_rows=result["records"], data_completeness="complete", errors=[],
    )


@app.post("/api/external/inventory/preview", response_model=ExternalInventoryPreviewResponse)
def external_inventory_preview(payload: ExternalInventoryPreviewRequest, request: Request):
    principal = require_principal(request, permission="inventory.read")
    from app.connectors import load_records
    try:
        records = load_records(payload.content, platform=payload.platform, source_mode=payload.source_mode)
    except ValueError as exc:
        raise DomainError(code=str(exc) if str(exc) else "EXTERNAL_INPUT_INVALID", message=str(exc), http_status=422) from exc
    return ExternalInventoryPreviewResponse(
        platform=payload.platform,
        source_mode=payload.source_mode,
        simulated=True,
        live_enabled=False,
        normalized_rows=[record.as_dict() for record in records],
        total=len(records),
        errors=[],
        writes=[],
    )


@app.post("/api/external/inventory/ingest", response_model=ExternalInventoryIngestResponse)
def external_inventory_ingest(payload: ExternalInventoryIngestRequest, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="catalog.write")
    from app.sync_application import SyncApplicationError, SyncApplicationService, inventory_response_data

    try:
        result = SyncApplicationService(db).ingest_inventory(
            workspace_id=principal.workspace_id,
            platform=payload.platform,
            source_mode=payload.source_mode,
            content=payload.content,
        )
    except SyncApplicationError as exc:
        status_code = 500 if exc.code == "EXTERNAL_SYNC_FAILED" else 422
        raise DomainError(code=exc.code, message=exc.message, http_status=status_code) from exc
    return ExternalInventoryIngestResponse(**inventory_response_data(result))


@app.post("/api/external/events/ingest", response_model=ExternalEventIngestResponse)
def external_events_ingest(payload: ExternalEventIngestRequest, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="catalog.write")
    from app.connectors import load_events
    from app.external_sync import begin_sync_run, complete_sync_run, fail_sync_run, ingest_events, validate_event_batch_scope
    run = None
    try:
        events = load_events(payload.content, platform=payload.platform, source_mode=payload.source_mode)
        account_ref = validate_event_batch_scope(events)
        run = begin_sync_run(db, workspace_id=principal.workspace_id, platform=payload.platform, sync_type="events", account_ref=account_ref, source_mode=payload.source_mode)
        result = ingest_events(db, events, workspace_id=principal.workspace_id, sync_run_id=run.run_id)
        complete_sync_run(db, run, result)
    except ValueError as exc:
        if 'run' in locals() and run is not None:
            fail_sync_run(db, run, exc)
        raise DomainError(code=str(exc) if str(exc) else "EXTERNAL_INPUT_INVALID", message=str(exc), http_status=422) from exc
    except Exception as exc:
        if 'run' in locals() and run is not None:
            fail_sync_run(db, run, exc)
        raise DomainError(code="EXTERNAL_SYNC_FAILED", message="外部事件同步失败", http_status=500) from exc
    return ExternalEventIngestResponse(platform=payload.platform, inserted=result.inserted, no_op=result.no_op, conflict=result.conflict, total=result.total, sync_run_id=result.sync_run_id, sync_status=result.sync_status)


@app.get("/api/external/reconciliation/{snapshot_id}")
def external_reconciliation(snapshot_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="inventory.read")
    from app.external_sync import reconcile_inventory
    try:
        return {"snapshot_id": snapshot_id, "items": reconcile_inventory(db, snapshot_id, workspace_id=principal.workspace_id)}
    except ValueError as exc:
        raise DomainError(code=str(exc), message="外部快照不存在", http_status=404) from exc




@app.get("/api/content/workflows")
def list_content_workflows(request: Request):
    require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    from app.visual_workflows import WORKFLOWS
    from app.workflow_schema import workflow_hash
    return {"items": [workflow.plan() | {"workflow_hash": workflow_hash(workflow.graph()), "nodes": workflow.graph()["nodes"], "edges": workflow.graph()["edges"]} for workflow in WORKFLOWS.values()]}


@app.get("/api/content/workflows/{workflow_key}")
def get_content_workflow(workflow_key: str, request: Request):
    require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    try:
        from app.visual_workflows import get_workflow
        from app.workflow_schema import workflow_hash
        workflow = get_workflow(workflow_key)
        graph = workflow.graph()
        return workflow.plan() | {"workflow_hash": workflow_hash(graph), "nodes": graph["nodes"], "edges": graph["edges"]}
    except ValueError as exc:
        raise DomainError(code="WORKFLOW_NOT_FOUND", message="工作流不存在", http_status=404) from exc


@app.post("/api/content/runs", response_model=ContentJobResponse, status_code=202)
def create_content_run(payload: ContentJobCreate, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    key = request.headers.get("Idempotency-Key") or ""
    if not key or len(key) > 128:
        raise DomainError(code="IDEMPOTENCY_KEY_REQUIRED", message="必须提供有效的 Idempotency-Key", http_status=422)
    from app.content_application import ContentApplicationError, ContentApplicationService

    try:
        result = ContentApplicationService(db).create_generation(
            workspace_id=principal.workspace_id,
            actor=principal.subject,
            idempotency_key=key,
            **payload.model_dump(),
        )
    except ContentApplicationError as exc:
        status_by_code = {
            "PRODUCT_NOT_FOUND": 404,
            "SKU_NOT_FOUND": 404,
            "IDEMPOTENCY_KEY_REUSE": 409,
        }
        raise DomainError(code=exc.code, message=exc.message, http_status=status_by_code.get(exc.code, 422)) from exc

    revision = result["revision"]
    job = result["job"]
    if not result["replayed"]:
        from app.background import _submit_future
        _submit_future(job.id)
    return ContentJobResponse(
        job_id=job.id if job else 0,
        run_id=job.run_id if job else None,
        revision_id=revision.id,
        job_status=job.status if job else "succeeded",
        revision_status=revision.status,
        simulated=revision.simulated,
        provider=revision.provider,
    )


@app.get("/api/content/jobs/{job_id}")
def get_content_job(job_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    job = db.scalar(select(CrawlJob).where(CrawlJob.id == job_id, CrawlJob.workspace_id == principal.workspace_id, CrawlJob.type == "content_generate"))
    if job is None:
        raise DomainError(code="CONTENT_JOB_NOT_FOUND", message="内容任务不存在", http_status=404)
    revision = db.scalar(select(ProductContentRevision).where(ProductContentRevision.generation_job_id == job.id, ProductContentRevision.workspace_id == principal.workspace_id))
    return {"job_id": job.id, "run_id": job.run_id, "job_status": job.status, "revision_status": revision.status if revision else "unknown", "revision_id": revision.id if revision else None, "simulated": revision.simulated if revision else True, "provider": revision.provider if revision else job.source}


@app.get("/api/content/runs")
def list_content_runs(request: Request, product_id: int | None = Query(default=None, gt=0), limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    filters = [ProductContentRevision.workspace_id == principal.workspace_id]
    if product_id:
        filters.append(ProductContentRevision.product_id == product_id)
    revisions = db.scalars(select(ProductContentRevision).where(*filters).order_by(ProductContentRevision.id.desc()).limit(limit)).all()
    rows = []
    for revision in revisions:
        job = db.get(CrawlJob, revision.generation_job_id) if revision.generation_job_id else None
        rows.append({"revision_id": revision.id, "product_id": revision.product_id, "revision_no": revision.revision_no, "status": revision.status, "quality_status": revision.quality_status, "provider": revision.provider, "simulated": revision.simulated, "job_id": job.id if job else None, "job_status": job.status if job else "unknown", "created_at": revision.created_at})
    return {"items": rows}


@app.get("/api/content/runs/{job_id}")
def get_content_run(job_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    job = db.scalar(select(CrawlJob).where(CrawlJob.id == job_id, CrawlJob.workspace_id == principal.workspace_id, CrawlJob.type == "content_generate"))
    if job is None:
        raise DomainError(code="CONTENT_JOB_NOT_FOUND", message="内容任务不存在", http_status=404)
    revision = db.scalar(select(ProductContentRevision).where(ProductContentRevision.generation_job_id == job.id, ProductContentRevision.workspace_id == principal.workspace_id))
    if revision is None:
        raise DomainError(code="CONTENT_REVISION_NOT_FOUND", message="内容版本不存在", http_status=404)
    assets = db.scalars(select(ProductMediaAsset).where(ProductMediaAsset.revision_id == revision.id, ProductMediaAsset.workspace_id == principal.workspace_id).order_by(ProductMediaAsset.id)).all()
    return {"job_id": job.id, "job_status": job.status, "revision": {**ContentRevisionResponse.model_validate(revision).model_dump(mode="json"), "assets": [ContentAssetResponse.model_validate(x).model_dump(mode="json") for x in assets]}}


@app.get("/api/products/{product_id}/media", response_model=list[ContentAssetResponse])
def list_product_media(product_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    if db.scalar(select(Product.id).where(Product.id == product_id, Product.workspace_id == principal.workspace_id)) is None:
        raise DomainError(code="PRODUCT_NOT_FOUND", message="商品不存在", http_status=404)
    return db.scalars(select(ProductMediaAsset).where(ProductMediaAsset.product_id == product_id, ProductMediaAsset.workspace_id == principal.workspace_id).order_by(ProductMediaAsset.id.desc())).all()


@app.post("/api/products/{product_id}/media", response_model=ContentAssetResponse, status_code=201)
async def upload_product_media(product_id: int, request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    product = db.scalar(select(Product).where(Product.id == product_id, Product.workspace_id == principal.workspace_id))
    if product is None:
        raise DomainError(code="PRODUCT_NOT_FOUND", message="商品不存在", http_status=404)
    content = await file.read()
    limit = get_settings().aigc_max_upload_bytes
    if not content or len(content) > limit:
        raise DomainError(code="MEDIA_TOO_LARGE", message="基础图为空或超过大小限制", http_status=413)
    from app.content_production import _artifact_root, save_asset_bytes
    import io
    try:
        image = __import__("PIL").Image.open(io.BytesIO(content))
        image.verify()
        with __import__("PIL").Image.open(io.BytesIO(content)) as checked:
            width, height = checked.size
            mime = checked.get_format_mimetype() or file.content_type or "image/png"
    except Exception as exc:
        raise DomainError(code="MEDIA_INVALID", message="基础图不是有效图片", http_status=415) from exc
    digest = sha256(content).hexdigest()
    asset = db.scalar(select(ProductMediaAsset).where(ProductMediaAsset.workspace_id == principal.workspace_id, ProductMediaAsset.product_id == product_id, ProductMediaAsset.role == "source", ProductMediaAsset.content_sha256 == digest))
    if asset is not None:
        asset.selected = True
        db.commit(); db.refresh(asset)
        return asset
    db.query(ProductMediaAsset).filter(ProductMediaAsset.workspace_id == principal.workspace_id, ProductMediaAsset.product_id == product_id, ProductMediaAsset.role == "source").update({"selected": False}, synchronize_session=False)
    path = save_asset_bytes(workspace_id=principal.workspace_id, run_id=f"source-{product_id}", name=f"{digest}.bin", data=content)
    asset = ProductMediaAsset(workspace_id=principal.workspace_id, product_id=product_id, role="source", storage_uri=path, content_sha256=digest, mime_type=mime, size_bytes=len(content), width=width, height=height, provider="upload", source_mode="upload", simulated=False, selected=True)
    db.add(asset); db.commit(); db.refresh(asset)
    return asset


@app.get("/api/content/assets/{asset_id}")
def download_content_asset(asset_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    asset = db.scalar(select(ProductMediaAsset).where(ProductMediaAsset.id == asset_id, ProductMediaAsset.workspace_id == principal.workspace_id))
    if asset is None:
        raise DomainError(code="CONTENT_ASSET_NOT_FOUND", message="素材不存在", http_status=404)
    root = get_settings().resolve_path(get_settings().artifacts_dir) / "content"
    try:
        path = safe_storage_path(root, asset.storage_uri)
    except ValueError as exc:
        raise DomainError(code="CONTENT_ASSET_NOT_FOUND", message="素材不存在", http_status=404) from exc
    if not path.is_file():
        raise DomainError(code="CONTENT_ASSET_NOT_FOUND", message="素材文件不存在", http_status=404)
    return FileResponse(path, media_type=asset.mime_type)


@app.post("/api/content/revisions/{revision_id}/review")
def review_content_revision(revision_id: int, payload: ContentReviewRequest, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    key = request.headers.get("Idempotency-Key") or ""
    from app.content_application import ContentApplicationError, ContentApplicationService

    try:
        result = ContentApplicationService(db).review_revision(
            workspace_id=principal.workspace_id,
            revision_id=revision_id,
            action=payload.action,
            note=payload.note,
            expected_version=payload.expected_version,
            idempotency_key=key,
            actor=principal.subject,
            request_id=getattr(request.state, "request_id", None),
        )
    except ContentApplicationError as exc:
        status_by_code = {
            "CONTENT_REVISION_NOT_FOUND": 404,
            "CONTENT_VERSION_CONFLICT": 409,
            "CONTENT_QUALITY_BLOCKED": 409,
            "CONTENT_INVALID_STATE": 409,
            "IDEMPOTENCY_KEY_REUSE": 409,
        }
        raise DomainError(code=exc.code, message=exc.message, http_status=status_by_code.get(exc.code, 422)) from exc
    revision = result["revision"]
    return {"id": revision.id, "status": revision.status, "version": revision.version, "reviewed_by": revision.reviewed_by, "replayed": result["replayed"]}


@app.post("/api/content/revisions/{revision_id}/export", response_model=ContentExportResponse, status_code=202)
def export_content_revision(revision_id: int, payload: ContentExportRequest, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    key = request.headers.get("Idempotency-Key") or ""
    if not key or len(key) > 128:
        raise DomainError(code="IDEMPOTENCY_KEY_REQUIRED", message="必须提供 Idempotency-Key", http_status=422)
    from app.content_application import ContentApplicationError, ContentApplicationService

    try:
        result = ContentApplicationService(db).export_revision(
            workspace_id=principal.workspace_id,
            revision_id=revision_id,
            request_revision_id=payload.revision_id,
            export_format=payload.format,
            idempotency_key=key,
            actor=principal.subject,
        )
    except ContentApplicationError as exc:
        status_by_code = {
            "CONTENT_NOT_APPROVED": 409,
            "REVISION_MISMATCH": 422,
            "IDEMPOTENCY_KEY_REUSE": 409,
            "CONTENT_EXPORT_PERSIST_FAILED": 500,
        }
        if exc.code == "CONTENT_EXPORT_PERSIST_FAILED":
            logger.error(
                "内容导出持久化失败 code=%s workspace_id=%s revision_id=%s",
                exc.code, principal.workspace_id, revision_id,
            )
        raise DomainError(code=exc.code, message=exc.message, http_status=status_by_code.get(exc.code, 422)) from exc

    export = result["export"]
    if not result["replayed"]:
        from app.background import _submit_future
        _submit_future(result["job"].id)
    return export


@app.get("/api/content/exports/{export_id}", response_model=ContentExportResponse)
def get_content_export(export_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    export = db.scalar(select(ProductContentExport).where(ProductContentExport.id == export_id, ProductContentExport.workspace_id == principal.workspace_id))
    if export is None:
        raise DomainError(code="CONTENT_EXPORT_NOT_FOUND", message="导出任务不存在", http_status=404)
    return export


@app.get("/api/products", response_model=ProductPage)
def list_products(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None, max_length=100),
    category: str | None = Query(default=None, max_length=128),
    db: Session = Depends(get_db),
) -> ProductPage:
    principal = require_principal(request, permission="catalog.read")
    filters = [Product.workspace_id == principal.workspace_id]
    if keyword:
        filters.append(Product.title.contains(keyword))
    if category:
        filters.append(Product.category == category)
    total = db.scalar(select(func.count(Product.id)).where(*filters)) or 0
    items = db.scalars(
        select(Product)
        .where(*filters)
        .order_by(Product.last_seen_at.desc(), Product.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return ProductPage(items=items, page=page, page_size=page_size, total=total)


@app.get("/api/products/{product_id}", response_model=ProductResponse)
def get_product(product_id: int, request: Request, db: Session = Depends(get_db)) -> Product:
    principal = require_principal(request, permission="catalog.read")
    product = db.scalar(select(Product).where(Product.id == product_id, Product.workspace_id == principal.workspace_id))
    if product is None:
        raise DomainError(code="PRODUCT_NOT_FOUND", message="商品不存在", http_status=404)
    return product


@app.get("/api/products/{product_id}/price-history", response_model=list[PriceHistoryResponse])
def price_history(product_id: int, request: Request, db: Session = Depends(get_db)) -> list[ProductPriceHistory]:
    principal = require_principal(request, permission="catalog.read")
    if db.scalar(select(Product.id).where(Product.id == product_id, Product.workspace_id == principal.workspace_id)) is None:
        raise DomainError(code="PRODUCT_NOT_FOUND", message="商品不存在", http_status=404)
    return db.scalars(
        select(ProductPriceHistory)
        .where(ProductPriceHistory.product_id == product_id, ProductPriceHistory.workspace_id == principal.workspace_id)
        .order_by(ProductPriceHistory.observed_at.asc(), ProductPriceHistory.id.asc())
    ).all()
