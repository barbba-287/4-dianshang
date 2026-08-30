"""电商商品与客服工作台的输入输出数据模型。

用途：使用 Pydantic 约束采集商品字段，并统一 FastAPI 商品、价格历史
和采集任务接口的响应结构，隔离外部数据与数据库模型。
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class ProductRecord(BaseModel):
    source: str = Field(min_length=1, max_length=64)
    external_product_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=255)
    url: HttpUrl
    category: str | None = Field(default=None, max_length=128)
    description: str | None = None
    rating: Decimal | None = Field(default=None, ge=0, le=5)
    current_price: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = Field(default="CNY", min_length=3, max_length=8)
    observed_at: datetime = Field(default_factory=datetime.utcnow)


class ProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    external_product_id: str
    title: str
    url: str
    category: str | None
    description: str | None
    rating: Decimal | None
    current_price: Decimal
    currency: str
    first_seen_at: datetime
    last_seen_at: datetime


class ProductPage(BaseModel):
    items: list[ProductResponse]
    page: int
    page_size: int
    total: int


class PriceHistoryResponse(BaseModel):
    id: int
    product_id: int
    price: Decimal
    currency: str
    observed_at: datetime
    source_run_id: str | None


class CrawlJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    keyword: str | None
    status: str
    cursor: str | None
    retry_count: int
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    error_message: str | None


class CrawlJobDetailResponse(CrawlJobResponse):
    """任务详情（S1）：相比 CrawlJobResponse 暴露并发控制相关字段。"""

    type: str
    max_retries: int
    attempt: int
    worker_id: str | None
    lease_until: datetime | None
    next_run_at: datetime | None
    run_id: str | None
    cancel_requested: bool


# ---------- S2 文档相关 ---------- =========


class DocumentUploadResponse(BaseModel):
    document_id: int
    version_id: int
    job_id: int
    version_no: int
    status: str


class DocumentVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    document_id: int
    version_no: int
    sha256: str
    size_bytes: int
    storage_uri: str
    status: str
    parser_version: str
    error_code: str | None
    error_message: str | None
    created_at: datetime
    published_at: datetime | None
    chunk_count: int | None = None


# ---------- S3 RAG ----------


class RagCitation(BaseModel):
    chunk_id: int
    document_id: int
    document_version_id: int
    snippet: str
    score: float
    locator: dict


class RagQueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int | None = Field(default=None, ge=1, le=20)
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    product_id: int | None = None
    source_type: str | None = None


class RagQueryResponse(BaseModel):
    answer: str | None
    citations: list[RagCitation]
    no_answer: bool
    reason: str | None = None
    retrieval_diagnostics: dict


# ---------- S4 设置端点 ----------


class SettingsResponse(BaseModel):
    app_name: str
    database_url: str
    worker_concurrency: int
    worker_max_attempts: int
    worker_lease_seconds: int
    worker_heartbeat_seconds: int
    upload_max_bytes: int
    imports_dir: str
    embedder_dim: int
    vector_store_path: str
    rag_top_k: int
    rag_min_score: float
    document_count: int
    chunk_count: int
    vector_record_count: int


# ---------- S5 Agent ----------


class AgentActionRequest(BaseModel):
    tool: str = Field(min_length=1, max_length=64)
    input: dict = Field(default_factory=dict)
    call_id: str | None = None


class AgentActionResponse(BaseModel):
    ok: bool
    output: dict | None = None
    error_code: str | None = None
    error_message: str | None = None
    duration_ms: int = 0
    call_id: str | None = None


class AgentToolSpec(BaseModel):
    name: str
    description: str
    is_readonly: bool
    input_schema: dict
    output_schema: dict | None = None
    max_calls_per_minute: int
