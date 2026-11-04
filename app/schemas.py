"""电商商品与客服工作台的输入输出数据模型。"""

from datetime import date, datetime
from decimal import Decimal

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class MetricMeta(BaseModel):
    """统一只读指标元数据；缺失事实必须显式标记完整度。"""

    metric: str
    value: int | float | Decimal | str | None = None
    numerator: int | float | Decimal | None = None
    denominator: int | float | Decimal | None = None
    workspace_id: int | None = None
    platform: str | None = None
    account_ref: str | None = None
    store_ref: str | None = None
    warehouse_id: int | None = None
    sku_id: int | None = None
    business_date: date | None = None
    timezone: str = "UTC"
    as_of: date | datetime | str | None = None
    source: str | None = None
    data_completeness: Literal["complete", "partial", "insufficient", "unknown"] = "unknown"
    metric_version: str = "1"
    reason: str | None = None


class AnalyticsMeta(BaseModel):
    """分析响应共享的范围、时间和数据质量说明。"""

    workspace_id: int
    as_of: date | str | None = None
    timezone: str = "UTC"
    source: str | None = None
    data_completeness: Literal["complete", "partial", "insufficient", "unknown"] = "unknown"
    metric_version: str = "1"
    metrics: list[MetricMeta] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class DashboardSummaryResponse(BaseModel):
    range: dict
    kpis: dict
    recent_inbounds: list[dict]
    unsupported_metrics: list[str]
    limitations: list[str]
    alert_summary: dict = Field(default_factory=dict)
    snapshot_freshness: list[dict] = Field(default_factory=list)
    sync_health: dict = Field(default_factory=dict)
    sales_summary: dict = Field(default_factory=dict)
    sales_trend: dict = Field(default_factory=dict)
    inventory_chart: dict = Field(default_factory=dict)
    sku_health: list[dict] = Field(default_factory=list)
    external_facts: dict = Field(default_factory=dict)


class CockpitMeta(BaseModel):
    """经营驾驶舱的时间基准与口径说明。"""

    workspace_id: int
    as_of: str
    timezone: str = "UTC"
    generated_at: str | None = None
    days: int
    from_: str = Field(default="", alias="from")
    to: str
    prev_from: str
    prev_to: str
    data_completeness: Literal["complete", "partial", "insufficient", "unknown"] = "unknown"
    limitations: list[str] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


class CockpitKpi(BaseModel):
    """单张指标卡：主值、标签、变化值、辅助信息与口径来源。"""

    key: str
    label: str
    value: int | float | str | None = None
    unit: str | None = None
    change_ratio: float | None = None
    change_basis: str | None = None
    quality: Literal["complete", "partial", "insufficient", "unknown"] = "unknown"
    reason: str | None = None
    source: str | None = None
    note: str | None = None
    multi_currency: bool = False


class CockpitCurrencyAmount(BaseModel):
    """按币种分组的金额与订单数；分组返回，绝不折算。"""

    currency: str
    gmv: str
    order_count: int


class CockpitSalesTrend(BaseModel):
    """销售趋势：缺失日留 null 断开，不补 0。"""

    dates: list[str]
    gmv: list[str | None] = Field(default_factory=list)
    order_counts: list[int] = Field(default_factory=list)
    gmv_by_currency: dict[str, list[str | None]] = Field(default_factory=dict)
    days_with_data: list[bool] = Field(default_factory=list)
    missing_days: int = 0
    currencies: list[str] = Field(default_factory=list)


class CockpitInventoryDistribution(BaseModel):
    """库存分布四类计数；unknown_policy 单列不占饼。"""

    out_of_stock: int = 0
    below_reorder: int = 0
    healthy: int = 0
    overstock: int = 0
    unknown_policy: int = 0


class CockpitTodo(BaseModel):
    """底部四个待办列表。"""

    shortage: list[dict] = Field(default_factory=list)
    overstock: list[dict] = Field(default_factory=list)
    purchase_approvals: list[dict] = Field(default_factory=list)
    pending_inbounds: list[dict] = Field(default_factory=list)
    pending_purchase_count: int = 0
    pending_inbound_count: int = 0


class CockpitResponse(BaseModel):
    """GET /api/dashboard/cockpit 响应（只读聚合）。"""

    meta: CockpitMeta
    days: int
    from_: str = Field(default="", alias="from")
    to: str
    prev_from: str
    prev_to: str
    window: dict = Field(default_factory=dict)
    kpis: dict[str, CockpitKpi] = Field(default_factory=dict)
    # P7-S3 预估利润：by_currency / ranking / warning / formula。
    # 结构刻意用 dict 而非嵌套模型：口径字段会随财务模块演进（P8 加佣金阶梯等），
    # 用宽松结构避免每加一个字段就动一次响应模型。
    profit: dict = Field(default_factory=dict)
    by_currency: list[CockpitCurrencyAmount] = Field(default_factory=list)
    fx_todo: str
    sales_trend: CockpitSalesTrend
    inventory_distribution: CockpitInventoryDistribution
    knowledge: dict = Field(default_factory=dict)
    todo: CockpitTodo
    limitations: list[str] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


class OperationsTodoItem(BaseModel):
    todo_id: str
    type: str
    priority: str
    status: str
    title: str
    reason: str
    entity_type: str
    entity_id: int | str | None = None
    source: str
    warehouse_id: int | None = None
    due_at: datetime | None = None
    allowed_actions: list[str] = Field(default_factory=list)
    target_path: str | None = None


class OperationsTodoSummary(BaseModel):
    total: int
    matched_total: int
    by_type: dict[str, int] = Field(default_factory=dict)
    unknown_types: dict[str, int | None] = Field(default_factory=dict)


class OperationsTodoMeta(BaseModel):
    data_completeness: Literal["complete", "partial", "insufficient", "unknown"]
    limitations: list[str] = Field(default_factory=list)
    scope: dict = Field(default_factory=dict)
    limit: int


class OperationsTodayResponse(BaseModel):
    workspace_id: int
    as_of: datetime
    timezone: str
    source_mode: str
    simulated: bool
    evidence_level: str
    summary: OperationsTodoSummary
    items: list[OperationsTodoItem] = Field(default_factory=list)
    meta: OperationsTodoMeta


class ExternalSyncRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resources: list[Literal["orders", "inventory"]] = Field(default_factory=list)
    orders_content: str | None = Field(default=None, max_length=2_000_000)
    inventory_content: str | None = Field(default=None, max_length=2_000_000)


class ExternalSyncRunResponse(BaseModel):
    run_id: str
    workspace_id: int | None
    platform: str
    account_ref: str | None
    store_ref: str | None
    sync_type: str
    source_mode: str
    simulated: bool
    status: str
    attempt: int = 1
    retry_of_run_id: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None
    duration_seconds: int | None = None
    heartbeat_age_seconds: int | None = None
    total: int
    inserted: int
    updated: int = 0
    no_op: int
    conflict: int
    stale: int = 0
    error_code: str | None = None
    error_message: str | None = None
    resource_status: dict = Field(default_factory=dict)
    retryable_resources: list[str] = Field(default_factory=list)
    # P2-2 归因可见（只增不改：既有键值逐字节不变）。
    actor: str | None = None
    request_id: str | None = None
    upstream_request_count: int = 0
    # P2-3 数据质量门禁（观察态）：只透出结论，全量报告走 CLI `quality show <run_id>`。
    quality_status: str | None = None
    # P6-S3：该批次被跳过的脏行数（仅 CSV 导入批次可能 > 0；其它批次恒 0）。
    rejected_count: int = 0


class ExternalSyncRetryResponse(ExternalSyncRunResponse):
    pass


# ---------------------------------------------------------------------------
# P2-4 补偿机制：死信（只增不改既有 schema）
# ---------------------------------------------------------------------------


class ExternalSyncDeadLetterResponse(BaseModel):
    """死信行（run 级失败的隔离带）。"""

    id: int
    workspace_id: int | None = None
    run_id: str | None = None
    platform: str | None = None
    sync_type: str | None = None
    source_mode: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    # auto / manual（登记时算死）。
    retry_class: str
    attempt: int = 1
    # 退避门槛（auto 类写死，manual 类恒 NULL）。
    next_replay_at: datetime | None = None
    exhausted: bool = False
    quality_status: str | None = None
    # open / replayed / ignored。
    status: str
    replayed_run_id: str | None = None
    actor: str | None = None
    request_id: str | None = None
    ignore_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class ExternalSyncDeadLetterListResponse(BaseModel):
    count: int
    items: list[ExternalSyncDeadLetterResponse] = Field(default_factory=list)


class ExternalSyncDeadLetterIgnoreRequest(BaseModel):
    """归档请求体：reason 必填（缺省 422）。"""

    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)


class ExternalSyncDeadLetterReplayResponse(BaseModel):
    """重放结果：死信终态 + 新建的 run。"""

    dead_letter: ExternalSyncDeadLetterResponse
    run: ExternalSyncRunResponse


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
    model_config = ConfigDict(extra="forbid")

    login: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    role: str = Field(pattern="^(admin|operations|warehouse|customer_service|readonly)$")
    warehouse_ids: list[int] = Field(default_factory=list)


class AdminUserResponse(BaseModel):
    id: int
    login: str
    display_name: str
    status: str
    role: str
    warehouse_ids: list[int]


class AdminUserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    role: str | None = Field(default=None, pattern="^(admin|operations|warehouse|customer_service|readonly)$")


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


class DocumentVersionSummary(BaseModel):
    """版本摘要（含 D2 派生的 `superseded` 标记，不落库）。"""

    id: int
    document_id: int
    version_no: int
    sha256: str
    size_bytes: int
    status: str
    created_at: datetime
    published_at: datetime | None
    superseded: bool
    chunk_count: int = 0


class DocumentListItem(BaseModel):
    """文档列表行：谱系摘要 + 最新版本状态 + 发布位。"""

    id: int
    filename: str
    title: str
    source_type: str
    sha256: str
    version_count: int
    latest_version_no: int | None = None
    latest_version_id: int | None = None
    latest_status: str | None = None
    published_version_no: int | None = None
    published_version_id: int | None = None
    superseded_version_nos: list[int] = Field(default_factory=list)
    created_at: datetime


class DocumentListResponse(BaseModel):
    items: list[DocumentListItem]
    total: int
    page: int
    page_size: int


class DocumentDetailResponse(BaseModel):
    """文档详情：谱系信息 + 版本链（含 superseded 派生标记）。"""

    id: int
    filename: str
    title: str
    source_type: str
    sha256: str
    version_count: int
    published_version_no: int | None = None
    published_version_id: int | None = None
    versions: list[DocumentVersionSummary]
    created_at: datetime


class DocumentPublishResponse(BaseModel):
    document_id: int
    version_id: int
    version_no: int
    published_at: datetime
    indexing_queued: bool
    superseded_version_ids: list[int] = Field(default_factory=list)


class DocumentUnpublishResponse(BaseModel):
    document_id: int
    version_id: int
    version_no: int
    published_at: datetime | None = None
    removed_vectors: int


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


class AssistantQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=2000)


class AssistantQueryResponse(BaseModel):
    ok: bool
    intent: str
    answer: str
    data: dict | None = None
    error_code: str | None = None
    limitations: list[str] = Field(default_factory=list)
    read_only: bool = True


class AgentAssistantRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    input_hints: dict = Field(default_factory=dict)
    max_turns: int = Field(default=3, ge=1, le=5)
    max_tool_calls: int = Field(default=3, ge=1, le=5)


class AgentAssistantTrace(BaseModel):
    tool: str
    input: dict
    ok: bool
    output: object | None = None
    error_code: str | None = None
    duration_ms: int = 0


class AgentAssistantResponse(BaseModel):
    ok: bool
    answer: str
    error_code: str | None = None
    turns: int = 0
    traces: list[AgentAssistantTrace] = Field(default_factory=list)


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
    # P7-S1：实收时申报的入库成本单价。可空 —— 存量入库单与「暂不填价」场景必须照常收货，
    # 不能因为缺成本就把仓库的作业卡住。空值语义是「未知」，不是 0（0 = 免费成本）。
    unit_cost: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)


class InboundReceive(BaseModel):
    lines: list[InboundReceiveLine] = Field(min_length=1)
    note: str | None = Field(default=None, max_length=1000)


class InboundLineResponse(BaseModel):
    id: int
    sku_id: int
    expected_qty: int
    received_qty: int | None
    damaged_qty: int
    accepted_qty: int | None = None
    difference: int | None = None
    # P7-S1：末尾追加（既有 7 键逐字节不变），前端据此回显单价与算预估总成本。
    unit_cost: Decimal | None = None


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
    # P3-S2：追加两个可选键（既有 9 键逐字节不变，只在末尾追加）。
    purchase_request_id: int | None = None
    purchase_request_no: str | None = None


class ReceiptProgressLine(BaseModel):
    sku_id: int
    requested_qty: int
    received_qty: int | None
    damaged_qty: int | None
    accepted_qty: int | None
    remaining_qty: int


class ReceiptProgressTotals(BaseModel):
    requested_qty: int
    received_qty: int
    damaged_qty: int
    accepted_qty: int
    remaining_qty: int


class ReceiptProgressResponse(BaseModel):
    purchase_request_id: int
    progress_status: str
    inbound_order_id: int | None
    inbound_reference_no: str | None
    inbound_status: str | None
    lines: list[ReceiptProgressLine]
    totals: ReceiptProgressTotals


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
    sync_run_id: str | None = None
    sync_status: str | None = None


class ExternalImportPreviewResponse(BaseModel):
    """P6-S1 报表导入预览（不写库）；P6-S2 增补列头映射四键；P6-S3 增补脏行明细。"""

    platform: str
    kind: str
    total: int
    detected_encoding: str
    normalized_rows: list[dict[str, Any]]
    errors: list[dict[str, str]]
    csv_headers: list[str] = []
    mapping_suggestion: dict[str, str] = {}
    unmapped_columns: list[str] = []
    missing_required_fields: list[str] = []
    # P6-S3：行级校验失败明细（最多 MAX_REJECTED_ROWS 条；总数另计不受截断影响）。
    rejected_rows: list[dict[str, Any]] = []
    rejected_total: int = 0


class ExternalImportCommitResponse(BaseModel):
    """P6-S1 报表导入入库（建来源批次，幂等语义与既有 ingest 一致）。"""

    platform: str
    kind: str
    total: int
    inserted: int
    updated: int
    no_op: int
    conflict: int
    sync_run_id: str | None = None
    sync_status: str | None = None
    detected_encoding: str
    # P6-S3：本次跳过的脏行数（allow_skipped 模式下 > 0）。
    rejected_count: int = 0


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
    sync_run_id: str | None = None
    sync_status: str | None = None


class ReconciliationResponse(BaseModel):
    snapshot_id: int
    items: list[dict]


class ExternalOrderPreviewRequest(BaseModel):
    platform: str = Field(pattern="^(taobao|jd|pdd|douyin|amazon)$")
    source_mode: str = Field(default="json", pattern="^(json|csv|mock)$")
    content: str = Field(min_length=1, max_length=2_000_000)


class ExternalOrderPreviewResponse(BaseModel):
    platform: str
    source_mode: str
    simulated: bool
    live_enabled: bool
    normalized_rows: list[dict]
    total: int
    errors: list[str]
    writes: list[str]


class ExternalOrderIngestRequest(ExternalOrderPreviewRequest):
    pass


class ExternalOrderIngestResponse(BaseModel):
    platform: str
    source_mode: str
    simulated: bool
    live_enabled: bool
    total: int
    inserted: int
    updated: int
    no_op: int
    conflict: int
    stale: int
    affected_dates: list[str]
    sync_run_id: str | None = None
    sync_status: str | None = None


class ExternalOrderLineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    external_line_id: str
    external_sku: str
    internal_sku_id: int | None
    ordered_qty: int
    cancelled_qty: int
    refunded_qty: int
    gross_amount: Decimal
    refund_amount: Decimal
    currency: str
    mapping_status: str
    data_completeness: str


class ExternalOrderListItem(BaseModel):
    id: int
    platform: str
    account_ref: str
    store_ref: str
    external_order_no: str
    order_status: str
    external_created_at: datetime
    paid_at: datetime | None
    external_updated_at: datetime
    gross_amount: Decimal
    refund_amount: Decimal
    currency: str
    data_completeness: str
    status_reason: str | None
    source_mode: str
    simulated: bool
    line_count: int
    net_item_qty: int


class ExternalOrderPage(BaseModel):
    items: list[ExternalOrderListItem]
    page: int
    page_size: int
    total: int
    summary: dict
    meta: dict


class ExternalOrderDetailResponse(ExternalOrderListItem):
    lines: list[ExternalOrderLineResponse] = Field(default_factory=list)
    meta: dict = Field(default_factory=dict)
class TicketResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    workspace_id: int
    ticket_no: str
    subject: str
    issue_type: str
    description: str | None
    sku_ref: str | None
    status: str
    priority: str
    channel: str
    customer_ref: str | None
    external_order_ref: str | None
    source_mode: str
    simulated: bool
    created_by: str
    assigned_to: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None


class TicketPage(BaseModel):
    items: list[TicketResponse]
    page: int
    page_size: int
    total: int


class TicketCreateRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=255)
    issue_type: str = Field(default="other", pattern="^(presale|order_payment|logistics|return_refund|product_quality|complaint|other)$")
    description: str = Field(default="", max_length=5000)
    customer_ref: str | None = Field(default=None, max_length=255)
    external_order_ref: str | None = Field(default=None, max_length=255)
    sku_ref: str | None = Field(default=None, max_length=255)
    priority: str = Field(default="normal", pattern="^(low|normal|high|urgent)$")


class TicketEventRequest(BaseModel):
    body: str = Field(min_length=1, max_length=5000)


class TicketTransitionRequest(BaseModel):
    target: str = Field(pattern="^(open|assigned|in_progress|waiting|resolved|closed)$")
    expected_version: int = Field(ge=1)


class TicketAssignRequest(BaseModel):
    assignee: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=1)
class ExternalOrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    platform: str
    account_ref: str
    store_ref: str
    external_order_no: str
    order_status: str
    external_created_at: datetime
    paid_at: datetime | None
    external_updated_at: datetime
    event_version: int | None
    gross_amount: Decimal
    refund_amount: Decimal
    currency: str
    data_completeness: str
    status_reason: str | None
    created_at: datetime
    updated_at: datetime



class ExternalProductMappingRequest(BaseModel):
    external_account_id: int = Field(gt=0)
    external_sku: str = Field(min_length=1, max_length=255)
    internal_sku_id: int | None = Field(default=None, gt=0)


class ExternalProductMappingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    external_account_id: int
    external_sku: str
    internal_sku_id: int | None
    mapping_status: str
    source: str


class ExternalAccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    platform: str
    account_ref: str
    store_ref: str
    name: str | None
    timezone: str
    status: str
    source_mode: str
    simulated: bool


class DailySkuSaleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    platform: str
    account_ref: str
    store_ref: str
    external_sku: str
    internal_sku_id: int | None
    sales_date: date
    gross_qty: int
    cancelled_qty: int
    refunded_qty: int
    net_qty: int
    gross_amount: Decimal
    refund_amount: Decimal
    net_amount: Decimal
    order_count: int
    data_completeness: str


class DailySkuSalePage(BaseModel):
    items: list[DailySkuSaleResponse]
    page: int
    page_size: int
    total: int


class ReplenishmentGenerateRequest(BaseModel):
    warehouse_id: int = Field(gt=0)
    sku_id: int = Field(gt=0)
    coverage_days: Literal[7, 14, 30] = 14
    as_of_date: date | None = None


class ReplenishmentDecisionRequest(BaseModel):
    action: str = Field(pattern="^(confirm|modify|ignore)$")
    decision_qty: int | None = Field(default=None, ge=0)
    reason: str | None = Field(default=None, max_length=1000)
    expected_version: int = Field(ge=1)


class ReplenishmentSuggestionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    workspace_id: int
    warehouse_id: int
    sku_id: int
    status: str
    suggested_qty: int | None
    decision_qty: int | None
    decision_reason: str | None
    formula_version: str
    coverage_days: int
    daily_avg_qty: Decimal | None
    on_hand_qty: int
    safety_stock_qty: int
    reorder_point_qty: int
    sales_qty: int
    effective_sale_days: int
    data_completeness: str
    reason: str | None
    source_hash: str
    as_of_date: date
    version: int
    created_at: datetime
    updated_at: datetime


class ProductQuadrantWindows(BaseModel):
    short: dict
    long: dict
    baseline: dict
    days: dict
    as_of: date
    business_timezone: str = "UTC"


class ProductQuadrantItem(BaseModel):
    sku_id: int
    sku_code: str
    product_id: int
    product_title: str | None
    category: str | None
    platform: str | None
    on_hand_qty: int
    days_of_inventory: float | None
    growth_rate: float | None
    growth_numerator: int | None
    growth_denominator: int | None
    quadrant: str
    data_completeness: str
    reason: str | None
    windows: ProductQuadrantWindows


class ProductQuadrantSummary(BaseModel):
    focal_supplement: int = 0
    healthy: int = 0
    watch: int = 0
    slow_risk: int = 0
    insufficient: int = 0
    total_scored: int = 0


class ProductQuadrantResponse(BaseModel):
    as_of: date
    business_timezone: str
    growth_window: int
    baseline_window: int
    days_window: int
    growth_high: float
    days_low: float
    summary: ProductQuadrantSummary
    items: list[ProductQuadrantItem]
    page: int = 1
    page_size: int = 20
    total: int = 0
    unsupported_metrics: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    meta: AnalyticsMeta | None = None


class ProductQuadrantPage(BaseModel):
    items: list[ProductQuadrantItem]
    page: int
    page_size: int
    total: int


class ReplenishmentEvaluationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    workspace_id: int
    suggestion_id: int
    sku_id: int
    warehouse_id: int
    formula_version: str
    suggested_qty: int
    window_start: date
    window_end: date
    actual_sales_qty: int | None
    stockout_days: int | None
    post_replenishment_coverage_days: Decimal | None
    absolute_error: int | None
    evaluation_status: str
    data_completeness: str
    source_snapshot_hash: str
    source_mode: str
    simulated: bool
    created_at: datetime
    updated_at: datetime
    meta: AnalyticsMeta | None = None


class ReplenishmentEvaluationPage(BaseModel):
    items: list[ReplenishmentEvaluationResponse]
    page: int
    page_size: int
    total: int
    meta: AnalyticsMeta | None = None


class ReplenishmentSuggestionPage(BaseModel):
    items: list[ReplenishmentSuggestionResponse]
    page: int
    page_size: int
    total: int


class PurchaseRequestCreate(BaseModel):
    suggestion_ids: list[int] = Field(min_length=1)
    note: str | None = Field(default=None, max_length=1000)


class PurchaseRequestDraftCreate(BaseModel):
    suggestion_ids: list[int] = Field(min_length=1)
    note: str | None = Field(default=None, max_length=1000)
    supplier_ref: str | None = Field(default=None, max_length=128)
    expected_arrival_date: date | None = None
    # P3-S1 Q2 血缘列：指向被驳回后重办的旧单（软引用，不 FK）。
    supersedes_request_id: int | None = Field(default=None, ge=1)


class PurchaseRequestApprove(BaseModel):
    expected_version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=500)


class PurchaseRequestReject(BaseModel):
    expected_version: int = Field(ge=1)
    # 理由必填由端点层显式校验（缺失/空串/全空格统一返回 REJECT_REASON_REQUIRED），
    # 故这里不设 min_length，避免被 FastAPI 的通用 422 抢先短路。
    reason: str | None = Field(default=None, max_length=500)


class PurchaseRequestCancel(BaseModel):
    expected_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=500)


class PurchaseRequestInboundCreate(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class PurchaseRequestDraftUpdate(BaseModel):
    note: str | None = Field(default=None, max_length=1000)
    supplier_ref: str | None = Field(default=None, max_length=128)
    expected_arrival_date: date | None = None
    expected_version: int = Field(ge=1)


class PurchaseRequestDraftSubmit(BaseModel):
    expected_version: int = Field(ge=1)


class PurchaseRequestLineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    purchase_request_id: int
    warehouse_id: int
    sku_id: int
    suggestion_id: int
    requested_qty: int
    source_suggestion_version: int
    note: str | None


class PurchaseRequestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    workspace_id: int
    warehouse_id: int
    request_no: str
    status: str
    note: str | None
    submitted_by: str | None
    submitted_at: datetime | None
    created_by: str | None
    version: int
    supplier_ref: str | None
    expected_arrival_date: date | None
    idempotency_key: str
    created_at: datetime
    updated_at: datetime
    lines: list[PurchaseRequestLineResponse] = Field(default_factory=list)
    # P3-S1：新增 7 键（既有 16 键逐字节不变，只追加）。
    approver_id: str | None = None
    approver_user_id: int | None = None
    decided_at: datetime | None = None
    reject_reason: str | None = None
    cancelled_by: str | None = None
    cancelled_at: datetime | None = None
    supersedes_request_id: int | None = None


class PurchaseRequestInboundOrderResponse(BaseModel):
    inbound_order: InboundResponse | None = None


class PurchaseRequestPage(BaseModel):
    items: list[PurchaseRequestResponse]
    page: int
    page_size: int
    total: int


class TaobaoCapabilitiesResponse(BaseModel):
    platform: str
    read_only: bool
    live_enabled: bool
    simulated: bool
    enabled: bool
    resources: list[str]
    limitations: list[str]


class TaobaoPreviewRequest(BaseModel):
    resource: Literal["orders", "inventory"]
    account_ref: str = Field(min_length=1, max_length=128)
    store_ref: str = Field(default="default", min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=2_000_000)
    max_pages: int = Field(default=100, ge=1, le=100)


class TaobaoPreviewResponse(BaseModel):
    platform: str
    resource: str
    read_only: bool
    live_enabled: bool
    simulated: bool
    total: int
    normalized_rows: list[dict]
    data_completeness: str
    errors: list[str] = Field(default_factory=list)


class ShopifyPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource: Literal["products", "orders", "inventory"] = "products"
    live: bool = False


class ShopifyPreviewResponse(BaseModel):
    platform: str = "shopify"
    resource: str
    live_enabled: bool
    simulated: bool
    store_domain: str | None = None
    total: int
    records: list[dict]
    request_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class FixtureSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: str = Field(pattern="^(taobao|jd|pdd|douyin|amazon)$")
    account_ref: str = Field(min_length=1, max_length=128)
    store_ref: str = Field(default="default", min_length=1, max_length=128)
    source_mode: Literal["json", "csv", "mock"] = "mock"
    orders_content: str | None = Field(default=None, max_length=2_000_000)
    inventory_content: str | None = Field(default=None, max_length=2_000_000)


class FixtureSyncResponse(BaseModel):
    platform: str
    account_ref: str
    store_ref: str
    simulated: bool
    live_enabled: bool
    sync_run_id: str
    sync_status: str
    orders: dict | None = None
    inventory: dict | None = None


class ContentAssetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    revision_id: int | None
    role: str
    storage_uri: str
    content_sha256: str
    mime_type: str
    size_bytes: int
    width: int | None
    height: int | None
    provider: str
    provider_model: str | None
    source_mode: str
    simulated: bool
    selected: bool
    created_at: datetime


class ContentRevisionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    workspace_id: int
    product_id: int
    sku_id: int | None
    revision_no: int
    status: str
    channel: str
    content_json: str | None
    source_snapshot_json: str
    source_snapshot_hash: str
    content_hash: str | None
    provider: str
    provider_model: str | None
    provider_request_id: str | None
    source_mode: str
    simulated: bool
    prompt_snapshot: str | None
    quality_status: str
    quality_issues_json: str | None
    created_by: str | None
    reviewed_by: str | None
    reviewed_at: datetime | None
    approved_at: datetime | None
    rejection_reason: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    assets: list[ContentAssetResponse] = Field(default_factory=list)


class ContentJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int = Field(gt=0)
    sku_id: int | None = Field(default=None, gt=0)
    scene: str = Field(default="电商主图优化", min_length=1, max_length=64)
    style: str = Field(default="清新自然", min_length=1, max_length=64)
    operation: str = Field(default="background_replace", pattern="^(background_replace|scene_extend|compose)$")
    provider: str = Field(default="local", pattern="^(local|local_compositor|mock|wan)$")
    brief: str = Field(default="", max_length=800)
    width: int = Field(default=800, ge=256, le=2048)
    height: int = Field(default=800, ge=256, le=2048)
    candidate_count: int = Field(default=1, ge=1, le=4)
    workflow_key: str | None = Field(default=None, pattern="^(package_preserve|scene_extend|model_atmosphere)$")


class ContentJobResponse(BaseModel):
    job_id: int
    run_id: str | None
    revision_id: int
    job_status: str
    revision_status: str
    simulated: bool
    provider: str


class ContentReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["submit", "approve", "request_changes", "reject"]
    note: str | None = Field(default=None, max_length=1000)
    expected_version: int = Field(default=1, ge=1)


class ContentExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: int = Field(gt=0)
    format: Literal["json", "markdown"] = "json"


class ContentExportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    revision_id: int
    format: str
    status: str
    artifact_uri: str | None
    artifact_sha256: str | None
    artifact_size: int | None
    simulated: bool
    created_at: datetime
    finished_at: datetime | None


# ---------- P7-S1 成本快照（append-only 成本事实层） ----------


class CostSnapshotCreate(BaseModel):
    """单条手工录入成本。`sku_code` 支持内部编码，未命中会回落外部编码映射。"""

    model_config = ConfigDict(extra="forbid")
    sku_code: str = Field(min_length=1, max_length=128)
    unit_cost: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    # 可空 = 现在生效（由服务层补 datetime.utcnow()）；补录历史成本时填真实生效时间。
    effective_time: datetime | None = None
    currency: str = Field(default="CNY", max_length=8)
    note: str | None = Field(default=None, max_length=1000)


class CostSnapshotItem(BaseModel):
    id: int
    sku_id: int
    sku_code: str
    unit_cost: str          # 两位小数字符串（金额不落 float）
    currency: str
    effective_time: str
    source: str             # inbound_confirm | manual
    source_ref: str
    note: str | None = None
    created_at: str | None = None
    created_by: str | None = None


class CostSnapshotListResponse(BaseModel):
    items: list[CostSnapshotItem] = Field(default_factory=list)
    total: int = 0


class CostImportPreviewResponse(BaseModel):
    """成本 CSV 预览：只解析不写库，与 P6 报表预览同一套语义（缺列 200 + missing 列表）。"""

    platform: str | None = None
    kind: str = "costs"
    total: int = 0
    detected_encoding: str = ""
    csv_headers: list[str] = Field(default_factory=list)
    mapping_suggestion: dict[str, str] = Field(default_factory=dict)
    unmapped_columns: list[str] = Field(default_factory=list)
    missing_required_fields: list[str] = Field(default_factory=list)
    rejected_rows: list[dict] = Field(default_factory=list)
    rejected_total: int = 0
    # 预览用前 20 条（金额已转两位小数字符串，避免 Decimal 无法 JSON 序列化）
    normalized_rows: list[dict] = Field(default_factory=list)


class CostImportCommitResponse(BaseModel):
    """成本 CSV 入库结果：imported + skipped + rejected_total == total（计数不变量）。"""

    kind: str = "costs"
    total: int = 0
    imported: int = 0
    skipped: int = 0
    rejected_total: int = 0
    detected_encoding: str = ""
    rejected_rows: list[dict] = Field(default_factory=list)


# ---------- P7-S2 成本回填（唯一允许改写历史成本的通道） ----------


class CostBackfillRequest(BaseModel):
    """成本回填请求。

    `dry_run` 默认 **True**：这是财务动作，先预演再落库是纪律不是可选。
    调用方必须显式传 `dry_run=false` 才真正写库。
    """

    model_config = ConfigDict(extra="forbid")
    # next_after_sale：取销售时刻**之后**最早的一条快照（视作当次销售的实际补货成本，推荐）
    # nearest：绝对时间差最小的一条（会引入未来成本，更激进）
    mode: Literal["next_after_sale", "nearest"] = "next_after_sale"
    sku_ids: list[int] | None = None
    dry_run: bool = True
    limit: int = Field(default=1000, ge=1, le=5000)


class CostBackfillSample(BaseModel):
    order_line_id: int
    external_sku: str
    internal_sku_id: int
    sale_time: str
    cost_snapshot_id: int
    unit_cost: str
    fee_rate: str | None = None


class CostBackfillResponse(BaseModel):
    """回填结果。

    `scanned` = 扫到的 missing 行数；`updated` = 能补上成本的行数；
    `skipped` = 扫到但找不到候选快照（保持 missing，不动）。
    不变式：`updated + skipped == scanned`。
    """

    mode: str
    dry_run: bool
    scanned: int = 0
    updated: int = 0
    skipped: int = 0
    samples: list[CostBackfillSample] = Field(default_factory=list)
