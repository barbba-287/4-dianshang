"""电商商品与客服工作台的 FastAPI 应用入口。

用途：提供商品采集、分页筛选、商品详情、价格历史、任务状态和健康检查
接口，并承载第一周演示用的极简网页。S1 新增异步任务提交 / 详情 / 取
消接口，保留旧 fixture 同步入口作为演示兼容路径。S2 新增文档上传与版
本列表接口。
"""

from contextlib import asynccontextmanager
from pathlib import Path

import json
import uuid

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.background import request_cancel_job, submit_crawl_fixture, submit_document_import
from app.config import get_settings
from app.crawler import CrawlError, collect_fixture
from app.db import (
    CrawlJob,
    Document,
    DocumentVersion,
    Product,
    ProductPriceHistory,
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
)
from app.storage import StorageError, enforce_size_limit, save_upload
from app.versioning import content_sha256


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title=get_settings().app_name, version="0.2.0-week2", lifespan=lifespan)


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
    except StorageError as exc:
        raise HTTPException(
            status_code=413,
            detail={"code": exc.code, "message": exc.message},
        ) from exc

    sha = content_sha256(content)
    ext = (file.filename or "").rsplit(".", 1)[-1] if "." in (file.filename or "") else source_type

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


@app.get("/api/settings", response_model=SettingsResponse)
def get_runtime_settings(db: Session = Depends(get_db)) -> SettingsResponse:
    """展示当前运行时配置与数据库 / 向量库规模，便于面试演示。"""
    from app.db import Document, DocumentChunk
    from app.rag_runtime import get_vector_store

    settings = get_settings()
    return SettingsResponse(
        app_name=settings.app_name,
        database_url=settings.database_url,
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
def invoke_agent_tool(payload: AgentActionRequest) -> AgentActionResponse:
    from app.agent.orchestrator import AgentAction, AgentOrchestrator
    from app.agent.registry import ToolContext

    orchestrator = AgentOrchestrator()
    context = ToolContext(request_id=f"agent-{uuid.uuid4().hex[:8]}")
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


# ---------- 第一周兼容接口（同步 fixture，仅作演示） ----------


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
