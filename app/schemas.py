"""电商商品与客服工作台的输入输出数据模型。"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator
class DashboardSummaryResponse(BaseModel):
    range: dict
    kpis: dict
    recent_inbounds: list[dict]
    unsupported_metrics: list[str]
    limitations: list[str]


class InventoryPolicyCreate(BaseModel):
    warehouse_id: int = Field(gt=0)
    sku_id: int = Field(gt=0)
    safety_stock_qty: int = Field(default=0, ge=0)
    reorder_point_qty: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_thresholds(self):
        if self.safety_stock_qty > self.reorder_point_qty:
            raise ValueError("safety_stock_qty 不能大于 reorder_point_qty")
        return self


class InventoryPolicyResponse(InventoryPolicyCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime


class AlertResponse(BaseModel):
    id: int
    kind: str
    severity: str
    status: str
    dedupe_key: str
    title: str
    message: str
    warehouse_id: int | None
    sku_id: int | None
    platform: str | None
    created_at: datetime
    last_seen_at: datetime
    acknowledged_at: datetime | None


class AdminUserCreate(BaseModel):
    login: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    role: str = Field(pattern="^(operations|warehouse|customer_service|readonly)$")
    warehouse_ids: list[int] = Field(default_factory=list)


class AdminUserResponse(BaseModel):
    id: int
    login: str
    display_name: str
    status: str
    role: str
    warehouse_ids: list[int]


class AdminUserPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    role: str | None = Field(default=None, pattern="^(operations|warehouse|customer_service|readonly)$")


class AdminUserPasswordReset(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class WarehouseAccessRequest(BaseModel):
    warehouse_id: int = Field(gt=0)


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
    type: str
    max_retries: int
    attempt: int
    worker_id: str | None
    lease_until: datetime | None
    next_run_at: datetime | None
    run_id: str | None
    cancel_requested: bool


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


class WarehouseCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    warehouse_type: str = Field(default="own", pattern="^(own|third_party)$")
    integration_mode: str = Field(default="manual", pattern="^(manual|csv|api)$")
    external_ref: str | None = Field(default=None, max_length=128)


class WarehouseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    warehouse_type: str
    integration_mode: str
    external_ref: str | None
    workspace_id: int | None
    is_active: bool
    created_at: datetime


class ProductSkuCreate(BaseModel):
    product_id: int = Field(gt=0)
    sku_code: str = Field(min_length=1, max_length=128)
    variant_label: str | None = Field(default=None, max_length=255)
    barcode: str | None = Field(default=None, max_length=64)
    unit: str = Field(default="件", min_length=1, max_length=32)


class ProductSkuResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    sku_code: str
    variant_label: str | None
    barcode: str | None
    unit: str
    is_active: bool


class InboundLineCreate(BaseModel):
    sku_id: int = Field(gt=0)
    expected_qty: int = Field(gt=0)


class InboundCreate(BaseModel):
    warehouse_id: int = Field(gt=0)
    reference_no: str = Field(min_length=1, max_length=64)
    lines: list[InboundLineCreate] = Field(min_length=1)
    note: str | None = Field(default=None, max_length=1000)


class InboundReceiveLine(BaseModel):
    sku_id: int = Field(gt=0)
    received_qty: int = Field(ge=0)
    damaged_qty: int = Field(default=0, ge=0)


class InboundReceive(BaseModel):
    lines: list[InboundReceiveLine] = Field(min_length=1)
    note: str | None = Field(default=None, max_length=1000)
    note: str | None = Field(default=None, max_length=1000)


class InboundLineResponse(BaseModel):
    id: int
    sku_id: int
    expected_qty: int
    received_qty: int | None
    damaged_qty: int
    accepted_qty: int | None = None
    difference: int | None = None


class InboundResponse(BaseModel):
    id: int
    warehouse_id: int
    reference_no: str
    status: str
    note: str | None
    lines: list[InboundLineResponse]
    created_at: datetime
    received_at: datetime | None
    confirmed_at: datetime | None


class InventoryItemResponse(BaseModel):
    warehouse_id: int
    warehouse_code: str
    sku_id: int
    sku_code: str
    product_id: int
    on_hand_qty: int
    updated_at: datetime


class InventoryPage(BaseModel):
    items: list[InventoryItemResponse]
    page: int
    page_size: int
    total: int


class InboundListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    warehouse_id: int
    reference_no: str
    status: str
    created_at: datetime
    received_at: datetime | None
    confirmed_at: datetime | None


class InboundListPage(BaseModel):
    items: list[InboundListItem]
    page: int
    page_size: int
    total: int

class ExternalInventoryPreviewRequest(BaseModel):
    platform: str = Field(pattern="^(taobao|jd|pdd|douyin|amazon)$")
    source_mode: str = Field(default="json", pattern="^(json|csv|mock)$")
    content: str = Field(min_length=1, max_length=2_000_000)


class ExternalInventoryPreviewResponse(BaseModel):
    platform: str
    source_mode: str
    simulated: bool
    live_enabled: bool
    normalized_rows: list[dict]
    total: int
    errors: list[str]
    writes: list[str]


class ExternalInventoryIngestRequest(ExternalInventoryPreviewRequest):
    pass


class ExternalInventoryIngestResponse(BaseModel):
    platform: str
    source_mode: str
    simulated: bool
    live_enabled: bool
    inserted: int
    no_op: int
    conflict: int
    total: int
    snapshot_ids: list[int]


class ExternalEventIngestRequest(BaseModel):
    platform: str = Field(pattern="^(taobao|jd|pdd|douyin|amazon)$")
    source_mode: str = Field(default="json", pattern="^(json|mock)$")
    content: str = Field(min_length=1, max_length=2_000_000)


class ExternalEventIngestResponse(BaseModel):
    platform: str
    inserted: int
    no_op: int
    conflict: int
    total: int


class ReconciliationResponse(BaseModel):
    snapshot_id: int
    items: list[dict]
