"""电商商品与客服工作台的 FastAPI 应用入口。

用途：提供商品采集、分页筛选、商品详情、价格历史、任务状态和健康检查
接口，并承载第一周演示用的极简网页。S1 新增异步任务提交 / 详情 / 取
消接口，保留旧 fixture 同步入口作为演示兼容路径。S2 新增文档上传与版
本列表接口。
"""

from contextlib import asynccontextmanager
from pathlib import Path

from hashlib import sha256
import json
import logging
import uuid

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
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
    get_db,
    init_db,
)
from app.importers import ParserError, detect_source_type
from app.jobs import JobStatus
from app.repository import (
    count_chunks,
    fail_job,
    import_records,
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
)
from app.storage import StorageError, enforce_size_limit, save_upload
from app.versioning import content_sha256
from app.upload_security import validate_content
from app.security import authenticate_api_key


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from app.security import validate_auth_config

    init_db()
    validate_auth_config(get_settings())
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
    if request.url.path.startswith("/api/"):
        principal = authenticate_api_key(request.headers.get("X-API-Key"), get_settings())
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

        request.state.principal = demo_principal(get_settings())
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
def index() -> str:
    template = Path(__file__).parent / "templates" / "index.html"
    return template.read_text(encoding="utf-8")


# ---------- S1 任务工程化 API ----------


@app.post("/api/crawl", response_model=CrawlJobDetailResponse, status_code=202)
def enqueue_crawl(
    db: Session = Depends(get_db),
) -> CrawlJob:
    """入队一个 fixture 采集任务，立刻返回任务详情。"""
    fixture = Path(__file__).resolve().parent.parent / "fixtures" / "products.html"
    job = submit_crawl_fixture(db, fixture_path=fixture, keyword="fixture")
    return job


# 注意：固定路径 /api/crawl/jobs 必须在 /api/crawl/{job_id} 之前注册，
# 否则 FastAPI 会把字面量 "jobs" 解析成 job_id。
@app.get("/api/crawl/jobs", response_model=list[CrawlJobDetailResponse])
def list_jobs(
    limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)
) -> list[CrawlJob]:
    return db.scalars(select(CrawlJob).order_by(CrawlJob.id.desc()).limit(limit)).all()


@app.get("/api/crawl/{job_id}", response_model=CrawlJobDetailResponse)
def get_crawl_job(job_id: int, db: Session = Depends(get_db)) -> CrawlJob:
    job = db.get(CrawlJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "JOB_NOT_FOUND", "message": "任务不存在"})
    return job


@app.post("/api/crawl/{job_id}/cancel", response_model=CrawlJobDetailResponse)
def cancel_crawl_job(job_id: int, db: Session = Depends(get_db)) -> CrawlJob:
    job = request_cancel_job(db, job_id=job_id)
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
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    db: Session = Depends(get_db),
) -> DocumentUploadResponse:
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
        )
    else:
        # 重复上传相同内容：复用现有 version，不重复入队
        from app.db import CrawlJob

        job = CrawlJob(
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
    document_id: int, db: Session = Depends(get_db)
) -> list[DocumentVersionResponse]:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(
            status_code=404, detail={"code": "DOCUMENT_NOT_FOUND", "message": "文档不存在"}
        )
    versions = list_versions_for_document(db, document_id=document_id)
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
                chunk_count=count_chunks(db, version_id=version.id),
            )
        )
    return items


# ---------- S3 RAG 客服问答 ----------


@app.post("/api/rag/query", response_model=RagQueryResponse)
def rag_query(payload: RagQueryRequest, db: Session = Depends(get_db)) -> RagQueryResponse:
    """基于已上传文档与本地向量库回答客服问题。

    返回结构：
    - answer: 文本答案（无证据或低置信度时为 null）
    - citations: 命中的 chunk 引用（chunk_id / doc / version / snippet / score）
    - no_answer: true 表示命中为空或最高分低于阈值
    - retrieval_diagnostics: 实际使用的 top_k / min_score / 命中数等
    """
    from app.db import Document, DocumentVersion
    from app.rag_runtime import get_answerer, get_embedder, get_vector_store

    filter_payload: dict[str, object] = {}
    if payload.product_id is not None:
        # 通过 product_id 过滤需要商品 → document 关联（document_product_links）
        # 这里以 product_id 为 payload 字段，让向量库过滤时按此字段匹配
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
        "vector_records": len(get_vector_store().records),
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
def get_runtime_settings(db: Session = Depends(get_db)) -> SettingsResponse:
    """展示当前运行时配置与数据库 / 向量库规模，便于面试演示。"""
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
        document_count=db.scalar(select(func.count(Document.id))) or 0,
        chunk_count=db.scalar(select(func.count(DocumentChunk.id))) or 0,
        vector_record_count=len(get_vector_store().records),
    )


# ---------- S5 受控 Agent ----------


@app.get("/api/agent/tools", response_model=list[AgentToolSpec])
def list_agent_tools() -> list[AgentToolSpec]:
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
    from app.agent.orchestrator import AgentAction, AgentOrchestrator
    from app.agent.registry import ToolContext

    orchestrator = AgentOrchestrator()
    principal = request.state.principal
    context = ToolContext(
        tenant_id=principal.tenant_id,
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
def create_sku(payload: ProductSkuCreate, db: Session = Depends(get_db)):
    from app.repository import create_sku as create_sku_record
    try:
        return create_sku_record(db, product_id=payload.product_id, sku_code=payload.sku_code, variant_label=payload.variant_label, barcode=payload.barcode, unit=payload.unit)
    except ValueError as exc:
        raise _warehouse_error(exc) from exc


@app.get("/api/skus", response_model=list[ProductSkuResponse])
def list_skus(db: Session = Depends(get_db)):
    from app.db import ProductSku
    return db.scalars(select(ProductSku).where(ProductSku.is_active.is_(True)).order_by(ProductSku.id)).all()


@app.post("/api/warehouses", response_model=WarehouseResponse, status_code=201)
def create_warehouse(payload: WarehouseCreate, db: Session = Depends(get_db)):
    from app.repository import create_warehouse as create_warehouse_record
    try:
        return create_warehouse_record(db, code=payload.code, name=payload.name, warehouse_type=payload.warehouse_type, integration_mode=payload.integration_mode, external_ref=payload.external_ref)
    except ValueError as exc:
        raise _warehouse_error(exc) from exc


@app.get("/api/warehouses", response_model=list[WarehouseResponse])
def list_warehouses(db: Session = Depends(get_db)):
    from app.db import Warehouse
    return db.scalars(select(Warehouse).where(Warehouse.is_active.is_(True)).order_by(Warehouse.id)).all()


@app.post("/api/inbounds", response_model=InboundResponse, status_code=201)
def create_inbound(payload: InboundCreate, db: Session = Depends(get_db)):
    from app.repository import _inbound_response_data, create_inbound as create_inbound_record, get_inbound
    try:
        order = create_inbound_record(db, warehouse_id=payload.warehouse_id, reference_no=payload.reference_no, lines=[item.model_dump() for item in payload.lines], note=payload.note)
        result = get_inbound(db, inbound_id=order.id)
        assert result is not None
        return _inbound_response_data(*result)
    except ValueError as exc:
        raise _warehouse_error(exc) from exc


@app.get("/api/inbounds/{inbound_id}", response_model=InboundResponse)
def get_inbound_detail(inbound_id: int, db: Session = Depends(get_db)):
    from app.repository import _inbound_response_data, get_inbound
    result = get_inbound(db, inbound_id=inbound_id)
    if result is None:
        raise _warehouse_error(ValueError("INBOUND_NOT_FOUND"))
    return _inbound_response_data(*result)


@app.post("/api/inbounds/{inbound_id}/receive", response_model=InboundResponse)
def receive_inbound(inbound_id: int, payload: InboundReceive, request: Request, db: Session = Depends(get_db)):
    from app.repository import _inbound_response_data, receive_inbound as receive_inbound_record
    try:
        result = receive_inbound_record(
            db,
            inbound_id=inbound_id,
            lines=[item.model_dump() for item in payload.lines],
            idempotency_key=request.headers.get("Idempotency-Key"),
            payload_hash=sha256(payload.model_dump_json().encode()).hexdigest(),
        )
        return _inbound_response_data(*result)
    except ValueError as exc:
        raise _warehouse_error(exc) from exc


@app.post("/api/inbounds/{inbound_id}/confirm", response_model=InboundResponse)
def confirm_inbound(inbound_id: int, request: Request, db: Session = Depends(get_db)):
    from app.repository import _inbound_response_data, confirm_inbound as confirm_inbound_record
    try:
        result = confirm_inbound_record(
            db,
            inbound_id=inbound_id,
            confirmed_by=getattr(request.state.principal, "subject", None),
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
        return _inbound_response_data(*result)
    except ValueError as exc:
        raise _warehouse_error(exc) from exc


@app.get("/api/inventory", response_model=InventoryPage)
def inventory_page(page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100), warehouse_id: int | None = Query(default=None, gt=0), sku_id: int | None = Query(default=None, gt=0), db: Session = Depends(get_db)):
    from app.repository import list_inventory
    items, total = list_inventory(db, warehouse_id=warehouse_id, sku_id=sku_id, page=page, page_size=page_size)
    return InventoryPage(items=items, page=page, page_size=page_size, total=total)




@app.post("/api/crawl/fixture", response_model=CrawlJobResponse, status_code=201)
def crawl_fixture(db: Session = Depends(get_db)) -> CrawlJob:
    """第一周同步入口：直接执行 fixture 采集并写入。保留用于演示。"""
    job = start_job(db, source="fixture", keyword="fixture")
    try:
        fixture = Path(__file__).resolve().parent.parent / "fixtures" / "products.html"
        records = collect_fixture(fixture)
        return import_records(db, records, keyword="fixture", job=job)
    except CrawlError as exc:
        db.rollback()
        job = db.get(CrawlJob, job.id)
        if job is not None:
            fail_job(db, job, error_code=exc.code, message=exc.message)
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


# ---------- 商品查询接口（第一周） ----------


@app.get("/api/products", response_model=ProductPage)
def list_products(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None, max_length=100),
    category: str | None = Query(default=None, max_length=128),
    db: Session = Depends(get_db),
) -> ProductPage:
    filters = []
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
def get_product(product_id: int, db: Session = Depends(get_db)) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail={"code": "PRODUCT_NOT_FOUND", "message": "商品不存在"})
    return product


@app.get("/api/products/{product_id}/price-history", response_model=list[PriceHistoryResponse])
def price_history(product_id: int, db: Session = Depends(get_db)) -> list[ProductPriceHistory]:
    if db.get(Product, product_id) is None:
        raise HTTPException(status_code=404, detail={"code": "PRODUCT_NOT_FOUND", "message": "商品不存在"})
    return db.scalars(
        select(ProductPriceHistory)
        .where(ProductPriceHistory.product_id == product_id)
        .order_by(ProductPriceHistory.observed_at.asc(), ProductPriceHistory.id.asc())
    ).all()
