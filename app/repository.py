"""电商商品与客服工作台的数据写入服务。

用途：将标准化商品记录以业务唯一键幂等写入数据库，维护当前价格、
价格历史和采集任务状态，保证重复采集不会制造重复商品；并提供任务
状态机的原子操作（claim、heartbeat、cancel、retry），支撑 S1 任务
工程化所需的 SQL 抢占语义。
"""

import secrets
import hashlib
import json
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.db import (
    CrawlJob,
    Document,
    DocumentChunk,
    DocumentProductLink,
    DocumentVersion,
    Product,
    ProductPriceHistory,
    ProductSku,
    Warehouse,
    InboundOrder,
    InboundLine,
    InventoryTransaction,
    InventoryBalance,
)
from app.jobs import JobClaimLost, JobStatus
from app.schemas import ProductRecord


def _now() -> datetime:
    return datetime.utcnow()


def upsert_product(db: Session, record: ProductRecord, *, source_run_id: str | None = None) -> Product:
    product = db.scalar(
        select(Product).where(
            Product.source == record.source,
            Product.external_product_id == record.external_product_id,
        )
    )
    observed_at = record.observed_at
    if product is None:
        product = Product(
            source=record.source,
            external_product_id=record.external_product_id,
            title=record.title,
            url=str(record.url),
            category=record.category,
            description=record.description,
            rating=record.rating,
            current_price=record.current_price,
            currency=record.currency,
            first_seen_at=observed_at,
            last_seen_at=observed_at,
            updated_at=observed_at,
        )
        db.add(product)
        db.flush()
        db.add(
            ProductPriceHistory(
                product_id=product.id,
                price=record.current_price,
                currency=record.currency,
                observed_at=observed_at,
                source_run_id=source_run_id,
            )
        )
    else:
        price_changed = product.current_price != record.current_price
        product.title = record.title
        product.url = str(record.url)
        product.category = record.category
        product.description = record.description
        product.rating = record.rating
        product.last_seen_at = observed_at
        product.updated_at = observed_at
        if price_changed:
            product.current_price = record.current_price
            db.add(
                ProductPriceHistory(
                    product_id=product.id,
                    price=record.current_price,
                    currency=record.currency,
                    observed_at=observed_at,
                    source_run_id=source_run_id,
                )
            )
    return product


def upsert_product_batch(
    db: Session,
    records: list[ProductRecord],
    *,
    source_run_id: str | None = None,
) -> int:
    """批量写入商品，返回本次实际新增或更新的商品数。

    设计上与 upsert_product 保持一致；后续可在此函数内加入每 N 条
    一次 commit、cancel 检查等节奏控制。S1 阶段保持简单。
    """
    for record in records:
        upsert_product(db, record, source_run_id=source_run_id)
    return len(records)


def import_records(
    db: Session,
    records: list[ProductRecord],
    *,
    keyword: str | None = None,
    job: CrawlJob | None = None,
) -> CrawlJob:
    run_id = uuid4().hex
    if job is None:
        job = CrawlJob(
            source=records[0].source if records else "fixture",
            keyword=keyword,
            status="running",
            started_at=_now(),
        )
        db.add(job)
        db.flush()
    try:
        for record in records:
            upsert_product(db, record, source_run_id=run_id)
        job.status = "succeeded"
        job.finished_at = _now()
        job.cursor = str(len(records))
        db.commit()
    except Exception as exc:
        db.rollback()
        job = db.get(CrawlJob, job.id)
        if job:
            job.status = "failed"
            job.finished_at = _now()
            job.error_code = "IMPORT_FAILED"
            job.error_message = str(exc)
            db.commit()
        raise
    return job


def start_job(db: Session, *, source: str, keyword: str | None = None) -> CrawlJob:
    job = CrawlJob(
        source=source,
        keyword=keyword,
        status="running",
        started_at=_now(),
    )
    db.add(job)
    db.commit()
    return job


def fail_job(db: Session, job: CrawlJob, *, error_code: str, message: str) -> None:
    job.status = "failed"
    job.finished_at = _now()
    job.error_code = error_code
    job.error_message = message
    db.commit()


# ---------- S1 任务工程化：状态机原子操作 ----------


def enqueue_job(
    db: Session,
    *,
    type_: str,
    source: str,
    keyword: str | None,
    payload: dict | None = None,
    max_retries: int | None = None,
    worker_id: str | None = None,
) -> CrawlJob:
    """插入一条 queued 任务并返回。

    payload 序列化为 JSON 写入 cursor 字段，供后台执行器读取上下文
    （document_id / version_id / storage_uri 等）。
    """
    import json as _json

    job = CrawlJob(
        source=source,
        keyword=keyword,
        type=type_,
        status=JobStatus.QUEUED.value,
        max_retries=max_retries if max_retries is not None else 2,
        run_id=uuid4().hex,
    )
    if payload:
        job.cursor = _json.dumps(payload, ensure_ascii=False)
    db.add(job)
    db.flush()
    db.commit()
    db.refresh(job)
    return job


def claim_job(
    db: Session,
    *,
    job_id: int,
    worker_id: str,
    lease_seconds: int,
) -> CrawlJob:
    """原子抢占一条任务。

    SQLite 使用 BEGIN IMMEDIATE + UPDATE WHERE status 条件；MySQL 部署下应改用
    SELECT ... FOR UPDATE SKIP LOCKED。抢占成功后 attempt +1，状态变为 running，
    设置 worker_id 与 lease_until。
    """
    now = _now()
    lease_until = now + timedelta(seconds=lease_seconds)
    result = db.execute(
        update(CrawlJob)
        .where(
            and_(
                CrawlJob.id == job_id,
                or_(
                    CrawlJob.status == JobStatus.QUEUED.value,
                    and_(
                        CrawlJob.status == JobStatus.RETRY_WAIT.value,
                        or_(CrawlJob.next_run_at.is_(None), CrawlJob.next_run_at <= now),
                    ),
                ),
                CrawlJob.cancel_requested.is_(False),
            )
        )
        .values(
            status=JobStatus.RUNNING.value,
            worker_id=worker_id,
            lease_until=lease_until,
            attempt=CrawlJob.attempt + 1,
            started_at=now,
            next_run_at=None,
        )
    )
    if result.rowcount == 0:
        db.rollback()
        raise JobClaimLost(job_id)
    db.commit()
    job = db.get(CrawlJob, job_id)
    assert job is not None
    return job


def recover_expired_jobs(db: Session, *, now: datetime | None = None) -> int:
    """回收 lease 已过期的 running 任务，并返回回收数量。

    回收视为一次执行失败：取消中的任务直接进入 cancelled；其余任务
    增加 retry_count，未达到 max_retries 时立即进入 retry_wait，达到上限
    则进入 failed。所有更新都带上 running + lease_until 条件，避免旧
    worker 已经完成任务后被调度器错误覆盖。
    """
    now = now or _now()
    running_expired = and_(
        CrawlJob.status == JobStatus.RUNNING.value,
        CrawlJob.lease_until.is_not(None),
        CrawlJob.lease_until <= now,
    )
    cancelled = db.execute(
        update(CrawlJob)
        .where(running_expired, CrawlJob.cancel_requested.is_(True))
        .values(
            status=JobStatus.CANCELLED.value,
            worker_id=None,
            lease_until=None,
            next_run_at=None,
            finished_at=now,
            error_code="CANCELLED",
            error_message="任务租约过期时收到取消请求",
        )
    ).rowcount

    retry_count = func.coalesce(CrawlJob.retry_count, 0) + 1
    failed = db.execute(
        update(CrawlJob)
        .where(
            running_expired,
            CrawlJob.cancel_requested.is_(False),
            func.coalesce(CrawlJob.max_retries, 2) <= retry_count,
        )
        .values(
            status=JobStatus.FAILED.value,
            retry_count=retry_count,
            worker_id=None,
            lease_until=None,
            next_run_at=None,
            finished_at=now,
            error_code="LEASE_EXPIRED",
            error_message="任务 worker 租约已过期，已达到最大重试次数",
        )
    ).rowcount
    retry_wait = db.execute(
        update(CrawlJob)
        .where(
            running_expired,
            CrawlJob.cancel_requested.is_(False),
            func.coalesce(CrawlJob.max_retries, 2) > retry_count,
        )
        .values(
            status=JobStatus.RETRY_WAIT.value,
            retry_count=retry_count,
            worker_id=None,
            lease_until=None,
            next_run_at=now,
            finished_at=None,
            error_code="LEASE_EXPIRED",
            error_message="任务 worker 租约已过期，等待重新执行",
        )
    ).rowcount
    db.commit()
    return int(cancelled or 0) + int(failed or 0) + int(retry_wait or 0)


def renew_lease(
    db: Session, *, job_id: int, worker_id: str, lease_seconds: int
) -> bool:
    """续约 lease_until；返回是否成功。

    仅当 job 仍属于当前 worker 且状态为 running 时续约；否则视为丢任务。
    """
    lease_until = _now() + timedelta(seconds=lease_seconds)
    result = db.execute(
        update(CrawlJob)
        .where(
            and_(
                CrawlJob.id == job_id,
                CrawlJob.worker_id == worker_id,
                CrawlJob.status == JobStatus.RUNNING.value,
            )
        )
        .values(lease_until=lease_until)
    )
    db.commit()
    return result.rowcount == 1


def finish_job(
    db: Session,
    *,
    job_id: int,
    worker_id: str,
    cursor: str | None = None,
) -> CrawlJob:
    """标记任务成功完成。"""
    result = db.execute(
        update(CrawlJob)
        .where(
            and_(
                CrawlJob.id == job_id,
                CrawlJob.worker_id == worker_id,
                CrawlJob.status == JobStatus.RUNNING.value,
            )
        )
        .values(
            status=JobStatus.SUCCEEDED.value,
            finished_at=_now(),
            cursor=cursor,
            lease_until=None,
        )
    )
    if result.rowcount == 0:
        db.rollback()
        raise JobClaimLost(job_id)
    db.commit()
    job = db.get(CrawlJob, job_id)
    assert job is not None
    return job


def retry_job(
    db: Session,
    *,
    job_id: int,
    worker_id: str,
    error_code: str,
    error_message: str,
    backoff_seconds: float,
    increment_retry: bool = True,
) -> CrawlJob:
    """将任务标记为 retry_wait 或 failed（达到 max_retries）。"""
    job = db.get(CrawlJob, job_id)
    if job is None or job.worker_id != worker_id:
        raise JobClaimLost(job_id)

    job.error_code = error_code
    job.error_message = error_message
    job.finished_at = None
    job.lease_until = None

    if increment_retry:
        job.retry_count = (job.retry_count or 0) + 1

    if (job.retry_count or 0) >= job.max_retries:
        job.status = JobStatus.FAILED.value
        job.finished_at = _now()
    else:
        job.status = JobStatus.RETRY_WAIT.value
        job.next_run_at = _now() + timedelta(seconds=backoff_seconds)
        job.worker_id = None

    db.commit()
    db.refresh(job)
    return job


def request_cancel(db: Session, *, job_id: int) -> CrawlJob | None:
    """请求取消任务：写 cancel_requested=1。已终态任务返回 None。"""
    job = db.get(CrawlJob, job_id)
    if job is None:
        return None
    if job.status in (JobStatus.SUCCEEDED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value):
        return job
    job.cancel_requested = True
    db.commit()
    db.refresh(job)
    return job


def is_cancel_requested(db: Session, *, job_id: int) -> bool:
    job = db.get(CrawlJob, job_id)
    return bool(job and job.cancel_requested)


def new_worker_id() -> str:
    return f"worker-{secrets.token_hex(6)}"


def compute_backoff_seconds(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """指数退避：min(base * 2**(attempt-1), cap) + 抖动。

    attempt 从 1 开始；attempt=1 → ~base 秒；attempt=2 → ~2*base；最多 cap。
    """
    import random

    if attempt <= 0:
        attempt = 1
    raw = min(base * (2 ** (attempt - 1)), cap)
    jitter = raw * 0.1 * (random.random() * 2 - 1)
    return max(0.0, raw + jitter)


# ---------- 仓储协同与库存 ----------


def create_sku(db: Session, *, product_id: int, sku_code: str, variant_label: str | None = None, barcode: str | None = None, unit: str = "件") -> ProductSku:
    if db.get(Product, product_id) is None:
        raise ValueError("PRODUCT_NOT_FOUND")
    if db.scalar(select(ProductSku).where(ProductSku.sku_code == sku_code)) is not None:
        raise ValueError("SKU_CODE_EXISTS")
    sku = ProductSku(product_id=product_id, sku_code=sku_code, variant_label=variant_label, barcode=barcode, unit=unit)
    db.add(sku)
    db.commit()
    db.refresh(sku)
    return sku


def create_warehouse(db: Session, *, code: str, name: str, warehouse_type: str, integration_mode: str = "manual", external_ref: str | None = None, workspace_id: int | None = None) -> Warehouse:
    query = select(Warehouse).where(Warehouse.code == code)
    if workspace_id is not None:
        query = query.where(Warehouse.workspace_id == workspace_id)
    if db.scalar(query) is not None:
        raise ValueError("WAREHOUSE_CODE_EXISTS")
    warehouse = Warehouse(code=code, name=name, warehouse_type=warehouse_type, integration_mode=integration_mode, external_ref=external_ref, workspace_id=workspace_id)
    db.add(warehouse)
    db.commit()
    db.refresh(warehouse)
    return warehouse


def _inbound_response_data(order: InboundOrder, lines: list[InboundLine]) -> dict:
    return {
        "id": order.id,
        "warehouse_id": order.warehouse_id,
        "reference_no": order.reference_no,
        "status": order.status,
        "note": order.note,
        "lines": [
            {
                "id": line.id,
                "sku_id": line.sku_id,
                "expected_qty": line.expected_qty,
                "received_qty": line.received_qty if order.status != "expected" else None,
                "damaged_qty": line.damaged_qty,
                "accepted_qty": (line.received_qty - line.damaged_qty) if order.status == "confirmed" else None,
                "difference": (line.received_qty - line.expected_qty) if order.status != "expected" else None,
            }
            for line in lines
        ],
        "created_at": order.created_at,
        "received_at": order.received_at,
        "confirmed_at": order.confirmed_at,
    }


def create_inbound(db: Session, *, warehouse_id: int, reference_no: str, lines: list[dict], note: str | None = None, created_by: str | None = None) -> InboundOrder:
    warehouse = db.get(Warehouse, warehouse_id)
    if warehouse is None:
        raise ValueError("WAREHOUSE_NOT_FOUND")
    if not warehouse.is_active:
        raise ValueError("WAREHOUSE_INACTIVE")
    if db.scalar(select(InboundOrder).where(InboundOrder.reference_no == reference_no)) is not None:
        raise ValueError("INBOUND_REFERENCE_EXISTS")
    sku_ids = [int(item["sku_id"]) for item in lines]
    if len(sku_ids) != len(set(sku_ids)):
        raise ValueError("DUPLICATE_SKU")
    if any(db.get(ProductSku, sku_id) is None or not db.get(ProductSku, sku_id).is_active for sku_id in sku_ids):
        raise ValueError("SKU_NOT_FOUND")
    order = InboundOrder(warehouse_id=warehouse_id, reference_no=reference_no, note=note, created_by=created_by)
    db.add(order)
    db.flush()
    for item in lines:
        db.add(InboundLine(inbound_order_id=order.id, sku_id=item["sku_id"], expected_qty=item["expected_qty"]))
    db.commit()
    db.refresh(order)
    return order


def get_inbound(db: Session, *, inbound_id: int) -> tuple[InboundOrder, list[InboundLine]] | None:
    order = db.get(InboundOrder, inbound_id)
    if order is None:
        return None
    lines = db.scalars(select(InboundLine).where(InboundLine.inbound_order_id == inbound_id).order_by(InboundLine.id)).all()
    return order, lines


def receive_inbound(db: Session, *, inbound_id: int, lines: list[dict], idempotency_key: str | None = None, payload_hash: str | None = None) -> tuple[InboundOrder, list[InboundLine]]:
    result = get_inbound(db, inbound_id=inbound_id)
    if result is None:
        raise ValueError("INBOUND_NOT_FOUND")
    order, inbound_lines = result
    if order.status != "expected":
        if order.receive_idempotency_key == idempotency_key and order.receive_payload_hash == payload_hash:
            return order, inbound_lines
        raise ValueError("INBOUND_ALREADY_RECEIVED")

    request_skus = [int(item["sku_id"]) for item in lines]
    if len(request_skus) != len(set(request_skus)):
        raise ValueError("DUPLICATE_SKU")
    by_sku = {line.sku_id: line for line in inbound_lines}
    if set(by_sku) != set(request_skus):
        raise ValueError("INBOUND_LINES_MISMATCH")
    for item in lines:
        if item["damaged_qty"] > item["received_qty"]:
            raise ValueError("DAMAGED_QTY_INVALID")

    try:
        for item in lines:
            line = by_sku[int(item["sku_id"])]
            line.received_qty = item["received_qty"]
            line.damaged_qty = item["damaged_qty"]
        order.status = "received"
        order.received_at = _now()
        order.receive_idempotency_key = idempotency_key
        order.receive_payload_hash = payload_hash
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_inbound(db, inbound_id=inbound_id)  # type: ignore[return-value]


def confirm_inbound(db: Session, *, inbound_id: int, confirmed_by: str | None = None, idempotency_key: str | None = None) -> tuple[InboundOrder, list[InboundLine]]:
    result = get_inbound(db, inbound_id=inbound_id)
    if result is None:
        raise ValueError("INBOUND_NOT_FOUND")
    order, lines = result
    if order.status == "confirmed":
        return order, lines
    if order.status != "received":
        raise ValueError("INVALID_INBOUND_STATE")

    try:
        order.confirm_idempotency_key = idempotency_key
        order.confirm_payload_hash = hashlib.sha256((idempotency_key or "confirm").encode()).hexdigest()
        for line in lines:
            accepted = line.received_qty - line.damaged_qty
            key = f"inbound:{order.id}:line:{line.id}:confirm"
            transaction = db.scalar(select(InventoryTransaction).where(InventoryTransaction.idempotency_key == key))
            if transaction is None:
                db.add(InventoryTransaction(inbound_order_id=order.id, warehouse_id=order.warehouse_id, sku_id=line.sku_id, quantity_delta=accepted, movement_type="inbound_confirm", idempotency_key=key, created_by=confirmed_by))
                balance = db.scalar(select(InventoryBalance).where(InventoryBalance.warehouse_id == order.warehouse_id, InventoryBalance.sku_id == line.sku_id))
                if balance is None:
                    balance = InventoryBalance(warehouse_id=order.warehouse_id, sku_id=line.sku_id, on_hand_qty=0)
                    db.add(balance)
                balance.on_hand_qty += accepted
        order.status = "confirmed"
        order.confirmed_at = _now()
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_inbound(db, inbound_id=inbound_id)  # type: ignore[return-value]


def list_inventory(
    db: Session,
    *,
    warehouse_id: int | None = None,
    sku_id: int | None = None,
    warehouse_ids: list[int] | tuple[int, ...] | None = None,
    workspace_id: int | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    filters = []
    if warehouse_id is not None:
        filters.append(InventoryBalance.warehouse_id == warehouse_id)
    if warehouse_ids is not None:
        if not warehouse_ids:
            return [], 0
        filters.append(InventoryBalance.warehouse_id.in_(warehouse_ids))
    if sku_id is not None:
        filters.append(InventoryBalance.sku_id == sku_id)
    if workspace_id is not None:
        filters.append(Warehouse.workspace_id == workspace_id)
    total = db.scalar(
        select(func.count(InventoryBalance.id))
        .join(Warehouse, Warehouse.id == InventoryBalance.warehouse_id)
        .where(*filters)
    ) or 0
    rows = db.execute(
        select(InventoryBalance, Warehouse, ProductSku)
        .join(Warehouse, Warehouse.id == InventoryBalance.warehouse_id)
        .join(ProductSku, ProductSku.id == InventoryBalance.sku_id)
        .where(*filters)
        .order_by(InventoryBalance.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return [
        {
            "warehouse_id": balance.warehouse_id,
            "warehouse_code": warehouse.code,
            "sku_id": balance.sku_id,
            "sku_code": sku.sku_code,
            "product_id": sku.product_id,
            "on_hand_qty": balance.on_hand_qty,
            "updated_at": balance.updated_at,
        }
        for balance, warehouse, sku in rows
    ], int(total)




def upsert_document(
    db: Session,
    *,
    source_type: str,
    title: str,
    filename: str,
    sha256_hex: str,
) -> tuple[Document, bool]:
    """按 sha256 复用 document；返回 (document, created)。"""
    existing = db.scalar(select(Document).where(Document.sha256 == sha256_hex))
    if existing is not None:
        return existing, False
    doc = Document(
        source_type=source_type,
        title=title,
        filename=filename,
        sha256=sha256_hex,
    )
    db.add(doc)
    db.flush()
    return doc, True


def upsert_document_version(
    db: Session,
    *,
    document: Document,
    sha256_hex: str,
    size_bytes: int,
    storage_uri: str,
) -> tuple[DocumentVersion, bool]:
    """同一 document 下若 sha256 已存在则复用；否则 version_no 自增。

    返回 (version, created)。
    """
    existing = db.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document.id,
            DocumentVersion.sha256 == sha256_hex,
        )
    )
    if existing is not None:
        return existing, False
    current_max = db.scalar(
        select(func.max(DocumentVersion.version_no)).where(
            DocumentVersion.document_id == document.id
        )
    )
    version_no = (current_max or 0) + 1
    version = DocumentVersion(
        document_id=document.id,
        version_no=version_no,
        sha256=sha256_hex,
        size_bytes=size_bytes,
        storage_uri=storage_uri,
        status="parsing",
    )
    db.add(version)
    db.flush()
    return version, True


def append_document_chunk(
    db: Session,
    *,
    version_id: int,
    chunk_no: int,
    text: str,
    content_hash: str,
    token_count: int,
    page_no: int | None = None,
    paragraph_no: int | None = None,
) -> DocumentChunk:
    chunk = DocumentChunk(
        document_version_id=version_id,
        chunk_no=chunk_no,
        text=text,
        content_hash=content_hash,
        token_count=token_count,
        page_no=page_no,
        paragraph_no=paragraph_no,
    )
    db.add(chunk)
    db.flush()
    return chunk


def mark_version_ready(db: Session, *, version_id: int) -> None:
    db.execute(
        update(DocumentVersion)
        .where(DocumentVersion.id == version_id)
        .values(status="ready")
    )
    db.commit()


def mark_version_failed(
    db: Session, *, version_id: int, code: str, message: str
) -> None:
    db.execute(
        update(DocumentVersion)
        .where(DocumentVersion.id == version_id)
        .values(status="failed", error_code=code, error_message=message[:500])
    )
    db.commit()


def publish_version(db: Session, *, version_id: int) -> DocumentVersion:
    version = db.get(DocumentVersion, version_id)
    if version is None:
        raise ValueError(f"version {version_id} 不存在")
    if version.status != "ready":
        raise ValueError(f"version {version_id} 状态为 {version.status}，无法发布")
    version.published_at = _now()
    db.commit()
    db.refresh(version)
    return version


def link_document_product(
    db: Session,
    *,
    document_version_id: int,
    product_id: int,
    relation: str,
) -> DocumentProductLink | None:
    """幂等写入：同三元组重复返回 None。"""
    existing = db.scalar(
        select(DocumentProductLink).where(
            DocumentProductLink.document_version_id == document_version_id,
            DocumentProductLink.product_id == product_id,
            DocumentProductLink.relation == relation,
        )
    )
    if existing is not None:
        return None
    link = DocumentProductLink(
        document_version_id=document_version_id,
        product_id=product_id,
        relation=relation,
    )
    db.add(link)
    db.flush()
    return link


def list_products_by_external_ids(
    db: Session, *, external_ids: list[str]
) -> list[Product]:
    if not external_ids:
        return []
    return db.scalars(
        select(Product).where(Product.external_product_id.in_(external_ids))
    ).all()


def list_all_products(db: Session, *, limit: int = 1000) -> list[Product]:
    """用于文档启发式关联的兜底扫描。"""
    return db.scalars(select(Product).limit(limit)).all()


def count_chunks(db: Session, *, version_id: int) -> int:
    return db.scalar(
        select(func.count(DocumentChunk.id)).where(
            DocumentChunk.document_version_id == version_id
        )
    ) or 0


def list_versions_for_document(db: Session, *, document_id: int) -> list[DocumentVersion]:
    return db.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version_no.desc())
    ).all()
