"""电商商品与客服工作台的 FastAPI 应用入口。

用途：提供商品采集、分页筛选、商品详情、价格历史、任务状态和健康检查
接口，并承载第一周演示用的极简网页。S1 新增异步任务提交 / 详情 / 取
消接口，保留旧 fixture 同步入口作为演示兼容路径。S2 新增文档上传与版
本列表接口。
"""

from contextlib import asynccontextmanager
from pathlib import Path
from datetime import date

from hashlib import sha256
import html
import json
import logging
import uuid

from fastapi import Body, Depends, File, Form, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
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
    Warehouse,
    InboundOrder,
    UserAccount,
    Workspace,
    WorkspaceMembership,
    WarehouseAccess,
    get_db,
    init_db,
)
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
    ExternalProductMappingRequest,
    ExternalProductMappingResponse,
    ExternalAccountResponse,
    DailySkuSalePage,
    ReplenishmentGenerateRequest,
    ReplenishmentDecisionRequest,
    ReplenishmentSuggestionResponse,
    ReplenishmentSuggestionPage,
    PurchaseRequestCreate,
    PurchaseRequestResponse,
    PurchaseRequestLineResponse,
    PurchaseRequestPage,
    TaobaoCapabilitiesResponse,
    TaobaoPreviewRequest,
    TaobaoPreviewResponse,
    FixtureSyncRequest,
    FixtureSyncResponse,
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
)

from app.storage import StorageError, enforce_size_limit, save_upload
from app.versioning import content_sha256
from app.upload_security import validate_content
from app.security import Principal as ApiPrincipal, authenticate_api_key
from app.employee_auth import (
    ROLE_ADMIN,
    ROLE_CUSTOMER_SERVICE,
    ROLE_OPERATIONS,
    ROLE_READONLY,
    ROLE_WAREHOUSE,
    create_session,
    hash_password,
    principal_from_session,
    require_principal,
    require_warehouse_access,
    validate_csrf,
    verify_password,
    validate_auth_config as validate_employee_auth_config,
)


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from app.security import validate_auth_config

    init_db()
    validate_auth_config(get_settings())
    validate_employee_auth_config(get_settings())
    start_background_workers()
    try:
        yield
    finally:
        stop_background_workers()


app = FastAPI(title=get_settings().app_name, version="0.2.0-week2", lifespan=lifespan)


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
    else:
        from app.security import demo_principal

        public_paths = {"/login", "/health", "/ready", "/docs", "/openapi.json", "/redoc"}
        if settings.employee_auth_enabled and request.url.path not in public_paths:
            if session_principal is None:
                return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)
            request.state.principal = session_principal
        else:
            request.state.principal = session_principal or demo_principal(settings)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path != "/login":
        try:
            with get_db_session_for_health() as csrf_db:
                validate_csrf(request, csrf_db)
        except HTTPException as exc:
            response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
            response.headers["X-Request-ID"] = request_id
            return response
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("unhandled request error", extra={"request_id": request_id})
        response = JSONResponse(
            status_code=500,
            content={"detail": {"code": "INTERNAL_ERROR", "message": "服务内部错误"}},
        )
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.get("/ready")
def ready() -> dict[str, str]:
    """依赖就绪检查，与只表示进程存活的 /health 分离。"""
    if not is_accepting_jobs():
        raise HTTPException(
            status_code=503,
            detail={"code": "WORKER_NOT_READY", "message": "后台任务服务未就绪"},
        )
    try:
        with get_db_session_for_health() as db:
            db.execute(text("SELECT 1"))
    except Exception:
        logger.exception("readiness database check failed")
        raise HTTPException(
            status_code=503,
            detail={"code": "DATABASE_NOT_READY", "message": "数据库未就绪"},
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
        template = Path(__file__).parent / "templates" / "index.html"
        return template.read_text(encoding="utf-8")
    principal = require_principal(request)
    if principal.has_role(ROLE_WAREHOUSE):
        return RedirectResponse(url="/warehouse", status_code=303)
    if principal.has_role(ROLE_CUSTOMER_SERVICE, ROLE_READONLY) and not principal.has_role(ROLE_ADMIN, ROLE_OPERATIONS, ROLE_WAREHOUSE):
        return RedirectResponse(url="/customer-service", status_code=303)
    return RedirectResponse(url="/ops", status_code=303)

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> str:
    next_path = request.query_params.get("next", "/")
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"
    next_path = html.escape(next_path, quote=True)
    return f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>登录 - 电商工作台</title><style>body{{max-width:420px;margin:5rem auto;padding:1rem;font-family:system-ui,sans-serif}}form{{display:grid;gap:.8rem}}input,button{{padding:.7rem;border:1px solid #cbd5e1;border-radius:.4rem;font:inherit}}button{{background:#2563eb;color:white;border:0}}</style></head><body><h1>员工登录</h1><p id='message'>请输入账户和密码</p><form method='post' action='/login'><label>账户<input name='login' required autocomplete='username'></label><label>密码<input name='password' type='password' required autocomplete='current-password'></label><input type='hidden' name='next' value='{next_path}'><button type='submit'>登录</button></form></body></html>"""


@app.post("/login")
def login(request: Request, login: str = Form(...), password: str = Form(...), next: str = Form(default="/"), db: Session = Depends(get_db)):
    if not get_settings().employee_auth_enabled:
        return RedirectResponse(url=next if next.startswith("/") and not next.startswith("//") else "/", status_code=303)
    user = db.scalar(select(UserAccount).where(UserAccount.login == login.strip()))
    membership = db.scalar(select(WorkspaceMembership).where(WorkspaceMembership.user_id == user.id, WorkspaceMembership.status == "active")) if user else None
    if user is None or user.status != "active" or membership is None or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail={"code": "INVALID_CREDENTIALS", "message": "账户或密码错误"})
    from app.db import backfill_legacy_workspace
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


@app.get("/ops", response_class=HTMLResponse)
def ops_page(request: Request) -> HTMLResponse:
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    template = (Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(
        content=template.replace("电商运营工作台", "运营工作台").replace("仓储协同", "入库审核与库存").replace("实收总数（含破损）", "实收总数（含破损）"),
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/warehouse", response_class=HTMLResponse)
def warehouse_page(request: Request) -> str:
    require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS, ROLE_WAREHOUSE))
    template = (Path(__file__).parent / "templates" / "warehouse.html").read_text(encoding="utf-8")
    return template


@app.get("/customer-service", response_class=HTMLResponse)
def customer_service_page(request: Request) -> str:
    require_principal(request, roles=(ROLE_ADMIN, ROLE_CUSTOMER_SERVICE, ROLE_READONLY))
    return """<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>客服工作台</title><body><h1>客服工作台</h1><p>当前账户拥有商品、库存和知识库只读权限。</p><p><a href='/'>返回入口</a></p></body></html>"""
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
        raise HTTPException(status_code=409 if str(exc).endswith("EXISTS") else 422, detail={"code": str(exc), "message": "账户或仓库范围无效"}) from exc


@app.post("/api/admin/users/{user_id}/warehouses/{warehouse_id}", status_code=204)
def admin_grant_warehouse(user_id: int, warehouse_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN,))
    from app.account_admin import set_warehouse_access
    try:
        set_warehouse_access(db, workspace_id=principal.workspace_id, user_id=user_id, warehouse_id=warehouse_id, active=True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": str(exc), "message": "账户或仓库不属于当前工作空间"}) from exc


@app.delete("/api/admin/users/{user_id}/warehouses/{warehouse_id}", status_code=204)
def admin_revoke_warehouse(user_id: int, warehouse_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN,))
    from app.account_admin import set_warehouse_access
    try:
        set_warehouse_access(db, workspace_id=principal.workspace_id, user_id=user_id, warehouse_id=warehouse_id, active=False)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": str(exc), "message": "账户或仓库不属于当前工作空间"}) from exc

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
        raise HTTPException(status_code=status, detail={"code": code, "message": "旧仓库绑定失败"}) from exc


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
def admin_users_page(request: Request) -> str:
    require_principal(request, roles=(ROLE_ADMIN,))
    return (Path(__file__).parent / "templates" / "admin_users.html").read_text(encoding="utf-8")


@app.post("/api/admin/users/{user_id}/deactivate", status_code=204)
def admin_deactivate_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN,))
    if principal.user_id == user_id:
        raise HTTPException(status_code=409, detail={"code": "SELF_DEACTIVATE_FORBIDDEN", "message": "不能停用当前登录账户"})
    from app.account_admin import set_user_status
    try:
        set_user_status(db, workspace_id=principal.workspace_id, user_id=user_id, active=False)
    except ValueError as exc:
        code = str(exc)
        status = 409 if code == "LAST_ADMIN_FORBIDDEN" else 404
        raise HTTPException(status_code=status, detail={"code": code, "message": "账户状态更新失败"}) from exc


@app.patch("/api/admin/users/{user_id}", response_model=AdminUserResponse)
def admin_update_user(user_id: int, payload: AdminUserPatch, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN,))
    from app.account_admin import update_workspace_user
    try:
        return update_workspace_user(db, workspace_id=principal.workspace_id, user_id=user_id, display_name=payload.display_name, role=payload.role)
    except ValueError as exc:
        code = str(exc)
        status = 409 if code == "LAST_ADMIN_FORBIDDEN" else 422 if code in {"INVALID_USER_INPUT", "INVALID_ROLE"} else 404
        raise HTTPException(status_code=status, detail={"code": code, "message": "账户或角色更新失败"}) from exc


@app.post("/api/admin/users/{user_id}/activate", status_code=204)
def admin_activate_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN,))
    from app.account_admin import set_user_status
    try:
        set_user_status(db, workspace_id=principal.workspace_id, user_id=user_id, active=True)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": str(exc), "message": "账户不存在"}) from exc


@app.post("/api/admin/users/{user_id}/reset-password", status_code=204)
def admin_reset_password(user_id: int, payload: AdminUserPasswordReset, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, roles=(ROLE_ADMIN,))
    from app.account_admin import reset_user_password
    try:
        reset_user_password(db, workspace_id=principal.workspace_id, user_id=user_id, password=payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422 if str(exc).endswith("SHORT") else 404, detail={"code": str(exc), "message": "密码或账户无效"}) from exc



@app.post("/api/crawl", response_model=CrawlJobDetailResponse, status_code=202)
def enqueue_crawl(
    request: Request,
    db: Session = Depends(get_db),
) -> CrawlJob:
    principal = require_principal(request, permission="catalog.write")
    if principal.workspace_id is None:
        raise HTTPException(status_code=503, detail={"code": "WORKSPACE_CONTEXT_REQUIRED", "message": "工作空间上下文缺失"})
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
        raise HTTPException(status_code=404, detail={"code": "JOB_NOT_FOUND", "message": "任务不存在"})
    return job


@app.post("/api/crawl/{job_id}/cancel", response_model=CrawlJobDetailResponse)
def cancel_crawl_job(job_id: int, request: Request, db: Session = Depends(get_db)) -> CrawlJob:
    principal = require_principal(request, permission="catalog.write")
    job = request_cancel_job(db, job_id=job_id, workspace_id=principal.workspace_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "JOB_NOT_FOUND", "message": "任务不存在"})
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
        raise HTTPException(status_code=400, detail={"code": "NO_FILE", "message": "未提供文件"})

    try:
        source_type = detect_source_type(file.filename, file.content_type)
    except ParserError as exc:
        raise HTTPException(
            status_code=415,
            detail={"code": exc.code, "message": exc.message},
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
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": exc.message},
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
        raise HTTPException(
            status_code=404, detail={"code": "DOCUMENT_NOT_FOUND", "message": "文档不存在"}
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


# ---------- S5 受控 Agent ----------


@app.get("/api/agent/tools", response_model=list[AgentToolSpec])
def list_agent_tools(request: Request) -> list[AgentToolSpec]:
    require_principal(request, permission="knowledge.read")
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
    from app.agent.orchestrator import AgentAction, AgentOrchestrator
    from app.agent.registry import ToolContext

    orchestrator = AgentOrchestrator()
    principal = request.state.principal
    context = ToolContext(
        tenant_id=principal.tenant_id,
        workspace_id=principal.workspace_id,
        user_id=principal.subject,
        request_id=request.state.request_id,
    )
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


# ---------- 仓储协同与库存 ----------


def _warehouse_error(exc: ValueError) -> HTTPException:
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
        "INVALID_INBOUND_STATE": "当前入库单状态不允许此操作",
    }
    return HTTPException(status_code=status, detail={"code": code, "message": messages.get(code, "仓储操作失败")})


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

    filters = []
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
            idempotency_key=request.headers.get("Idempotency-Key"),
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
        raise HTTPException(status_code=status, detail={"code": code, "message": "库存策略无效"}) from exc


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
        raise HTTPException(status_code=404, detail={"code": "ALERT_NOT_FOUND", "message": "告警不存在"})
    return alert


@app.post("/api/crawl/fixture", response_model=CrawlJobResponse, status_code=201)
def crawl_fixture(request: Request, db: Session = Depends(get_db)) -> CrawlJob:
    principal = require_principal(request, permission="catalog.write")
    if principal.workspace_id is None:
        raise HTTPException(status_code=503, detail={"code": "WORKSPACE_CONTEXT_REQUIRED", "message": "工作空间上下文缺失"})
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
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


@app.get("/api/dashboard/summary", response_model=DashboardSummaryResponse)
def dashboard_summary(
    request: Request,
    days: int = Query(default=7, ge=1, le=90),
    warehouse_id: int | None = Query(default=None, gt=0),
    recent_limit: int = Query(default=10, ge=1, le=100),
    db: Session = Depends(get_db),
):
    principal = require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    if warehouse_id is not None:
        require_warehouse_access(principal, warehouse_id, db=db)
    from app.dashboard import build_dashboard_summary
    return build_dashboard_summary(db, workspace_id=principal.workspace_id, days=days, warehouse_id=warehouse_id, recent_limit=recent_limit)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request) -> str:
    require_principal(request, roles=(ROLE_ADMIN, ROLE_OPERATIONS))
    return (Path(__file__).parent / "templates" / "dashboard.html").read_text(encoding="utf-8")





@app.post("/api/external/orders/preview", response_model=ExternalOrderPreviewResponse)
def external_orders_preview(payload: ExternalOrderPreviewRequest, request: Request):
    require_principal(request, permission="inventory.read")
    from app.connectors import load_orders
    try:
        records = load_orders(payload.content, platform=payload.platform, source_mode=payload.source_mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "EXTERNAL_INPUT_INVALID", "message": str(exc)}) from exc
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
        raise HTTPException(status_code=422, detail={"code": str(exc) if str(exc) else "EXTERNAL_INPUT_INVALID", "message": str(exc)}) from exc
    except Exception as exc:
        if run is not None:
            fail_sync_run(db, run, exc)
        raise HTTPException(status_code=500, detail={"code": "EXTERNAL_SYNC_FAILED", "message": "外部订单同步失败"}) from exc
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
        raise HTTPException(status_code=status, detail={"code": code, "message": "外部 SKU 映射无效"}) from exc


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
        raise HTTPException(status_code=422, detail={"code": "INVALID_DATE_RANGE", "message": "起始日期不能晚于结束日期"})
    from app.external_orders import list_daily_sales
    rows, total = list_daily_sales(db, workspace_id=principal.workspace_id, platform=platform, account_ref=account_ref, store_ref=store_ref, external_sku=external_sku, internal_sku_id=internal_sku_id, start=date_from, end=date_to, limit=page_size, offset=(page - 1) * page_size)
    return DailySkuSalePage(items=rows, page=page, page_size=page_size, total=total)


def _replenishment_error(exc: ValueError) -> HTTPException:
    code = str(exc)
    if str(exc) == "IDEMPOTENCY_KEY_REQUIRED":
        return HTTPException(status_code=422, detail={"code": str(exc), "message": "必须提供 Idempotency-Key"})
    status = 404 if code.endswith("NOT_FOUND") else 409 if code in {"OPEN_SUGGESTION_EXISTS", "IDEMPOTENCY_KEY_REUSE", "SUGGESTION_VERSION_CONFLICT", "INVALID_SUGGESTION_STATE", "SUGGESTION_NOT_READY", "MIXED_WAREHOUSE", "SUGGESTION_ALREADY_SUBMITTED"} else 422
    return HTTPException(status_code=status, detail={"code": code, "message": "补货决策操作失败"})


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
        "idempotency_key": request_record.idempotency_key,
        "created_at": request_record.created_at, "updated_at": request_record.updated_at,
        "lines": [PurchaseRequestLineResponse.model_validate(line) for line in lines],
    })


@app.get("/api/replenishment/suggestions", response_model=ReplenishmentSuggestionPage)
def list_replenishment_suggestions(request: Request, warehouse_id: int | None = Query(default=None, gt=0), sku_id: int | None = Query(default=None, gt=0), status: str | None = Query(default=None, max_length=16), page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)):
    principal = require_principal(request, permission="replenishment.read")
    if warehouse_id is not None: require_warehouse_access(principal, warehouse_id, db=db)
    from app.replenishment import list_suggestions
    rows, total = list_suggestions(db, workspace_id=principal.workspace_id, warehouse_id=warehouse_id, status=status, sku_id=sku_id, limit=page_size, offset=(page-1)*page_size)
    if warehouse_id is None and not principal.has_role(ROLE_ADMIN, ROLE_OPERATIONS):
        rows = [row for row in rows if row.warehouse_id in principal.warehouse_ids]
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


@app.post("/api/purchase-requests", response_model=PurchaseRequestResponse, status_code=201)
def submit_purchase_request_api(payload: PurchaseRequestCreate, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="purchasing.submit")
    from app.db import ReplenishmentSuggestion
    suggestions = db.scalars(select(ReplenishmentSuggestion).where(ReplenishmentSuggestion.workspace_id == principal.workspace_id, ReplenishmentSuggestion.id.in_(payload.suggestion_ids))).all()
    if not suggestions: raise _replenishment_error(ValueError("SUGGESTION_NOT_FOUND"))
    for suggestion in suggestions: require_warehouse_access(principal, suggestion.warehouse_id, db=db)
    from app.replenishment import submit_purchase_request
    try:
        result = submit_purchase_request(db, workspace_id=principal.workspace_id, suggestion_ids=payload.suggestion_ids, actor=principal.subject, idempotency_key=request.headers.get("Idempotency-Key") or "", note=payload.note)
        return _purchase_request_response(db, result)
    except ValueError as exc: raise _replenishment_error(exc) from exc


@app.get("/api/purchase-requests/{request_id}", response_model=PurchaseRequestResponse)
def get_purchase_request(request_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="purchase.read")
    from app.db import PurchaseRequest
    result = db.scalar(select(PurchaseRequest).where(PurchaseRequest.id == request_id, PurchaseRequest.workspace_id == principal.workspace_id))
    if result is None: raise HTTPException(status_code=404, detail={"code": "PURCHASE_REQUEST_NOT_FOUND", "message": "采购申请不存在"})
    require_warehouse_access(principal, result.warehouse_id, db=db)
    return _purchase_request_response(db, result)


@app.get("/api/purchase-requests", response_model=PurchaseRequestPage)
def list_purchase_requests(request: Request, page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100), warehouse_id: int | None = Query(default=None, gt=0), db: Session = Depends(get_db)):
    principal = require_principal(request, permission="purchase.read")
    if warehouse_id is not None: require_warehouse_access(principal, warehouse_id, db=db)
    from app.db import PurchaseRequest
    from sqlalchemy import func
    filters = [PurchaseRequest.workspace_id == principal.workspace_id]
    if warehouse_id is not None: filters.append(PurchaseRequest.warehouse_id == warehouse_id)
    elif not principal.has_role(ROLE_ADMIN, ROLE_OPERATIONS): filters.append(PurchaseRequest.warehouse_id.in_(principal.warehouse_ids))
    total = db.scalar(select(func.count(PurchaseRequest.id)).where(*filters)) or 0
    rows = db.scalars(select(PurchaseRequest).where(*filters).order_by(PurchaseRequest.id.desc()).offset((page-1)*page_size).limit(page_size)).all()
    return PurchaseRequestPage(items=[_purchase_request_response(db, row) for row in rows], page=page, page_size=page_size, total=int(total))


@app.post("/api/external/sync/fixture", response_model=FixtureSyncResponse)
def external_fixture_sync(payload: FixtureSyncRequest, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="catalog.write")
    from app.platform_sync import run_fixture_sync
    try:
        result = run_fixture_sync(db, workspace_id=principal.workspace_id, platform=payload.platform, account_ref=payload.account_ref, store_ref=payload.store_ref, orders_content=payload.orders_content, inventory_content=payload.inventory_content, source_mode=payload.source_mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": str(exc), "message": "离线同步失败"}) from exc
    order_stats = result["orders"]
    inventory_stats = result["inventory"]
    return FixtureSyncResponse(
        platform=payload.platform, account_ref=payload.account_ref, store_ref=payload.store_ref,
        simulated=True, live_enabled=False, sync_run_id=result["run"].run_id,
        sync_status=result["run"].status,
        orders={"total": order_stats.total, "inserted": order_stats.inserted, "updated": order_stats.updated, "no_op": order_stats.no_op, "conflict": order_stats.conflict, "stale": order_stats.stale} if order_stats else None,
        inventory={"total": inventory_stats.total, "inserted": inventory_stats.inserted, "no_op": inventory_stats.no_op, "conflict": inventory_stats.conflict, "snapshot_ids": inventory_stats.snapshot_ids or []} if inventory_stats else None,
    )


@app.get("/api/external/sync/runs/{run_id}", response_model=ExternalSyncRunResponse)
def get_external_sync_run(run_id: str, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="inventory.read")
    from app.db import ExternalSyncRun
    from app.external_sync import resource_status, retryable_resources
    run = db.scalar(select(ExternalSyncRun).where(ExternalSyncRun.run_id == run_id, ExternalSyncRun.workspace_id == principal.workspace_id))
    if run is None:
        raise HTTPException(status_code=404, detail={"code": "SYNC_RUN_NOT_FOUND", "message": "同步运行不存在"})
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
        raise HTTPException(status_code=422, detail={"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "必须提供 Idempotency-Key"})
    if len(key) > 128:
        raise HTTPException(status_code=422, detail={"code": "IDEMPOTENCY_KEY_TOO_LONG", "message": "Idempotency-Key 超过长度限制"})
    from app.db import ExternalSyncRun
    from app.external_sync import resource_status, retryable_resources
    from app.platform_sync import run_fixture_sync
    import hashlib
    import sqlalchemy.exc
    parent = db.scalar(select(ExternalSyncRun).where(ExternalSyncRun.run_id == run_id, ExternalSyncRun.workspace_id == principal.workspace_id))
    if parent is None:
        raise HTTPException(status_code=404, detail={"code": "SYNC_RUN_NOT_FOUND", "message": "同步运行不存在"})
    if parent.status not in {"failed", "partial"} or parent.sync_type not in {"fixture_bundle", "orders", "inventory"}:
        raise HTTPException(status_code=409, detail={"code": "SYNC_RUN_NOT_RETRYABLE", "message": "该同步运行不支持补偿"})
    available = set(retryable_resources(parent))
    selected = set(payload.resources or available)
    if not selected or not selected.issubset({"orders", "inventory"}) or not selected.issubset(available):
        raise HTTPException(status_code=409, detail={"code": "SYNC_RESOURCE_NOT_RETRYABLE", "message": "选定资源不可补偿"})
    if "orders" in selected and not payload.orders_content or "inventory" in selected and not payload.inventory_content:
        raise HTTPException(status_code=422, detail={"code": "FIXTURE_CONTENT_REQUIRED", "message": "补偿资源必须提供 fixture"})
    content_hash = hashlib.sha256(json.dumps({
        "run_id": run_id,
        "resources": sorted(selected),
        "orders_content": payload.orders_content if "orders" in selected else None,
        "inventory_content": payload.inventory_content if "inventory" in selected else None,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    existing = db.scalar(select(ExternalSyncRun).where(ExternalSyncRun.workspace_id == principal.workspace_id, ExternalSyncRun.retry_of_run_id == run_id, ExternalSyncRun.retry_idempotency_key == key))
    if existing is not None:
        if existing.retry_payload_hash != content_hash:
            raise HTTPException(status_code=409, detail={"code": "IDEMPOTENCY_KEY_REUSE", "message": "补偿幂等键对应内容已改变"})
        response = _external_sync_run_response(existing)
        return JSONResponse(status_code=200, content=response.model_dump(mode="json"))
    carried = resource_status(parent)
    try:
        result = run_fixture_sync(db, workspace_id=principal.workspace_id, platform=parent.platform, account_ref=parent.account_ref or "", store_ref=parent.store_ref or "default", orders_content=payload.orders_content if "orders" in selected else None, inventory_content=payload.inventory_content if "inventory" in selected else None, source_mode=parent.source_mode, attempt=(parent.attempt or 1) + 1, retry_of_run_id=parent.run_id, retry_idempotency_key=key, retry_payload_hash=content_hash, carried_forward={name: item for name, item in carried.items() if name not in selected}, sync_type_override=parent.sync_type)
    except sqlalchemy.exc.IntegrityError as exc:
        db.rollback()
        competing = db.scalar(select(ExternalSyncRun).where(
            ExternalSyncRun.workspace_id == principal.workspace_id,
            ExternalSyncRun.retry_of_run_id == run_id,
            ExternalSyncRun.retry_idempotency_key == key,
        ))
        if competing is None:
            raise HTTPException(status_code=500, detail={"code": "SYNC_RETRY_PERSIST_FAILED", "message": "同步补偿无法持久化"}) from exc
        if competing.retry_payload_hash != content_hash:
            raise HTTPException(status_code=409, detail={"code": "IDEMPOTENCY_KEY_REUSE", "message": "补偿幂等键对应内容已改变"}) from exc
        response = _external_sync_run_response(competing)
        return JSONResponse(status_code=200, content=response.model_dump(mode="json"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": str(exc), "message": "同步补偿失败"}) from exc
    child = result["run"]
    db.commit(); db.refresh(child)
    return JSONResponse(status_code=201, content=_external_sync_run_response(child).model_dump(mode="json"))


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
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "TAOBAO_FIELD_INVALID", "message": str(exc)}) from exc
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
        raise HTTPException(status_code=422, detail={"code": "EXTERNAL_INPUT_INVALID", "message": str(exc)}) from exc
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
    from app.connectors import load_records
    from app.external_sync import begin_sync_run, complete_sync_run, fail_sync_run, ingest_inventory
    try:
        records = load_records(payload.content, platform=payload.platform, source_mode=payload.source_mode)
        account_ref = records[0].account_ref if records else None
        store_ref = records[0].store_ref if records else None
        run = begin_sync_run(db, workspace_id=principal.workspace_id, platform=payload.platform, sync_type="inventory", account_ref=account_ref, store_ref=store_ref, source_mode=payload.source_mode)
        result = ingest_inventory(db, records, workspace_id=principal.workspace_id, sync_run_id=run.run_id)
        complete_sync_run(db, run, result)
    except ValueError as exc:
        if 'run' in locals() and run is not None:
            fail_sync_run(db, run, exc)
        raise HTTPException(status_code=422, detail={"code": "EXTERNAL_INPUT_INVALID", "message": str(exc)}) from exc
    except Exception as exc:
        if 'run' in locals() and run is not None:
            fail_sync_run(db, run, exc)
        raise HTTPException(status_code=500, detail={"code": "EXTERNAL_SYNC_FAILED", "message": "外部库存同步失败"}) from exc
    return ExternalInventoryIngestResponse(
        platform=payload.platform,
        source_mode=payload.source_mode,
        simulated=True,
        live_enabled=False,
        inserted=result.inserted,
        no_op=result.no_op,
        conflict=result.conflict,
        total=result.total,
        snapshot_ids=result.snapshot_ids or [],
        sync_run_id=result.sync_run_id,
        sync_status=result.sync_status,
    )


@app.post("/api/external/events/ingest", response_model=ExternalEventIngestResponse)
def external_events_ingest(payload: ExternalEventIngestRequest, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="catalog.write")
    from app.connectors import load_events
    from app.external_sync import begin_sync_run, complete_sync_run, fail_sync_run, ingest_events
    try:
        events = load_events(payload.content, platform=payload.platform, source_mode=payload.source_mode)
        account_ref = events[0].account_ref if events else None
        run = begin_sync_run(db, workspace_id=principal.workspace_id, platform=payload.platform, sync_type="events", account_ref=account_ref, source_mode=payload.source_mode)
        result = ingest_events(db, events, workspace_id=principal.workspace_id, sync_run_id=run.run_id)
        complete_sync_run(db, run, result)
    except ValueError as exc:
        if 'run' in locals() and run is not None:
            fail_sync_run(db, run, exc)
        raise HTTPException(status_code=422, detail={"code": "EXTERNAL_INPUT_INVALID", "message": str(exc)}) from exc
    except Exception as exc:
        if 'run' in locals() and run is not None:
            fail_sync_run(db, run, exc)
        raise HTTPException(status_code=500, detail={"code": "EXTERNAL_SYNC_FAILED", "message": "外部事件同步失败"}) from exc
    return ExternalEventIngestResponse(platform=payload.platform, inserted=result.inserted, no_op=result.no_op, conflict=result.conflict, total=result.total, sync_run_id=result.sync_run_id, sync_status=result.sync_status)


@app.get("/api/external/reconciliation/{snapshot_id}")
def external_reconciliation(snapshot_id: int, request: Request, db: Session = Depends(get_db)):
    principal = require_principal(request, permission="inventory.read")
    from app.external_sync import reconcile_inventory
    try:
        return {"snapshot_id": snapshot_id, "items": reconcile_inventory(db, snapshot_id, workspace_id=principal.workspace_id)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": str(exc), "message": "外部快照不存在"}) from exc




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
        raise HTTPException(status_code=404, detail={"code": "PRODUCT_NOT_FOUND", "message": "商品不存在"})
    return product


@app.get("/api/products/{product_id}/price-history", response_model=list[PriceHistoryResponse])
def price_history(product_id: int, request: Request, db: Session = Depends(get_db)) -> list[ProductPriceHistory]:
    principal = require_principal(request, permission="catalog.read")
    if db.scalar(select(Product.id).where(Product.id == product_id, Product.workspace_id == principal.workspace_id)) is None:
        raise HTTPException(status_code=404, detail={"code": "PRODUCT_NOT_FOUND", "message": "商品不存在"})
    return db.scalars(
        select(ProductPriceHistory)
        .where(ProductPriceHistory.product_id == product_id, ProductPriceHistory.workspace_id == principal.workspace_id)
        .order_by(ProductPriceHistory.observed_at.asc(), ProductPriceHistory.id.asc())
    ).all()
