"""电商工作台的后台任务执行模块。

用途：使用模块级 ThreadPoolExecutor 提供“真正的并发”能力，结合 SQL
claim / lease / retry / cancel 实现任务工程化。无 Redis / 无独立
worker 进程；生产环境可平滑迁移到 Celery/Redis，只需把 submit_job 与
claim_job 替换为 Celery 装饰器与 Redis broker。
"""

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import CrawlJob, SessionLocal
from app.jobs import JobCancelled, JobClaimLost, JobStatus
from app.repository import (
    claim_job,
    compute_backoff_seconds,
    enqueue_job,
    finish_job,
    is_cancel_requested,
    new_worker_id,
    renew_lease,
    request_cancel,
    retry_job,
    upsert_product_batch,
)


logger = logging.getLogger(__name__)


_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                settings = get_settings()
                _executor = ThreadPoolExecutor(
                    max_workers=settings.worker_concurrency,
                    thread_name_prefix="ds-job",
                )
    return _executor


def reset_executor() -> None:
    """测试中关闭 executor 并释放线程；不要在生产代码里调用。"""
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False, cancel_futures=True)
            _executor = None


# ---------- 公共入口：提交与取消 ----------


def submit_crawl_fixture(
    db: Session,
    *,
    fixture_path: Path,
    keyword: str | None = "fixture",
    page_size: int = 1000,
    max_retries: int | None = None,
) -> CrawlJob:
    """把 fixture 采集任务入队；立刻返回 queued 任务对象。

    实际执行交给后台 executor，由 run_loop 抢占并运行。
    """
    job = enqueue_job(
        db,
        type_="crawl_fixture",
        source="fixture",
        keyword=keyword,
        payload={"fixture_path": str(fixture_path), "page_size": page_size},
        max_retries=max_retries,
    )
    future = get_executor().submit(_run_one_job, job.id, new_worker_id())
    _attach_cancel_future(job.id, future)
    return job


def submit_document_import(
    db: Session,
    *,
    document_id: int,
    version_id: int,
    storage_uri: str,
    filename: str,
    source_type: str,
    max_retries: int | None = None,
) -> CrawlJob:
    """把文档导入任务入队。"""
    job = enqueue_job(
        db,
        type_="document_import",
        source=source_type,
        keyword=filename[:255],
        payload={
            "document_id": document_id,
            "version_id": version_id,
            "storage_uri": storage_uri,
        },
        max_retries=max_retries,
    )
    future = get_executor().submit(_run_one_job, job.id, new_worker_id())
    _attach_cancel_future(job.id, future)
    return job


def request_cancel_job(db: Session, *, job_id: int) -> CrawlJob | None:
    return request_cancel(db, job_id=job_id)


# ---------- 内部：cancel futures 与心跳续约 ----------


_cancel_futures: dict[int, Future] = {}
_cancel_futures_lock = threading.Lock()


def _attach_cancel_future(job_id: int, future: Future) -> None:
    with _cancel_futures_lock:
        _cancel_futures[job_id] = future


def _pop_cancel_future(job_id: int) -> Future | None:
    with _cancel_futures_lock:
        return _cancel_futures.pop(job_id, None)


# ---------- 内部：单任务执行 ----------


def _run_one_job(job_id: int, worker_id: str) -> None:
    """线程入口：claim + heartbeat + 执行 + finish/retry。

    设计原则：
    - claim 失败立即返回；
    - 每次 DB 写操作前检查 cancel_requested；
    - 后台心跳线程定期续约 lease_until；
    - 异常统一走 retry_job。
    """
    settings = get_settings()
    db = SessionLocal()
    try:
        try:
            job = claim_job(
                db,
                job_id=job_id,
                worker_id=worker_id,
                lease_seconds=settings.worker_lease_seconds,
            )
        except JobClaimLost:
            logger.info("job %s claim lost (already claimed/cancelled)", job_id)
            return

        stop_event = threading.Event()
        heartbeat = threading.Thread(
            target=_heartbeat_loop,
            args=(job_id, worker_id, settings.worker_heartbeat_seconds, settings.worker_lease_seconds, stop_event),
            daemon=True,
            name=f"heartbeat-{job_id}",
        )
        heartbeat.start()
        try:
            type_ = job.type or "crawl_fixture"
            if type_ == "crawl_fixture":
                _execute_crawl_fixture(db, job)
            elif type_ == "document_import":
                _execute_document_import(db, job)
            elif type_ == "document_index":
                _execute_document_index(db, job)
            else:
                raise RuntimeError(f"未知任务类型: {type_}")
            finish_job(db, job_id=job_id, worker_id=worker_id, cursor=str(job.cursor or ""))
            logger.info("job %s succeeded", job_id)
        except JobCancelled:
            logger.info("job %s cancelled during execution", job_id)
        except Exception as exc:  # noqa: BLE001 - 顶层兜底
            logger.warning("job %s failed: %s", job_id, exc)
            backoff = compute_backoff_seconds(job.attempt)
            retry_job(
                db,
                job_id=job_id,
                worker_id=worker_id,
                error_code=type(exc).__name__,
                error_message=str(exc)[:500],
                backoff_seconds=backoff,
            )
        finally:
            stop_event.set()
            heartbeat.join(timeout=2)
    finally:
        db.close()
        _pop_cancel_future(job_id)


def _heartbeat_loop(
    job_id: int,
    worker_id: str,
    interval: int,
    lease_seconds: int,
    stop_event: threading.Event,
) -> None:
    while not stop_event.wait(interval):
        db = SessionLocal()
        try:
            renew_lease(db, job_id=job_id, worker_id=worker_id, lease_seconds=lease_seconds)
        except Exception:  # noqa: BLE001
            pass
        finally:
            db.close()


def _execute_crawl_fixture(db: Session, job: CrawlJob) -> None:
    """执行 fixture 采集；S1 阶段直接调用第一周 collect_fixture。"""
    from app.crawler import collect_fixture

    payload_cursor = job.cursor or "0"
    try:
        page_size = int(payload_cursor)
    except ValueError:
        page_size = 1000

    fixture_path = _resolve_fixture_path(job)

    settings = get_settings()
    records = collect_fixture(fixture_path)
    n = upsert_product_batch(db, records, source_run_id=job.run_id)
    db.commit()
    job.cursor = str(n)


def _resolve_fixture_path(job: CrawlJob) -> Path:
    """从 job.keyword 与默认项目根推断 fixture 路径。"""
    if job.keyword and job.keyword != "fixture":
        return Path(job.keyword)
    project_root = Path(__file__).resolve().parent.parent
    return project_root / "fixtures" / "products.html"


def _execute_document_import(db: Session, job: CrawlJob) -> None:
    """S2 文档导入：从 storage_uri 读取文件，解析后写 chunks 并关联商品。

    解析完成后，提交 S3 索引任务把 chunks embed 到向量库；索引任务
    单独失败不影响本次文档导入的成功状态。
    """
    import hashlib

    from app.db import DocumentVersion, Product
    from app.importers import ChunkDraft, ParserError, get_parser
    from app.repository import (
        append_document_chunk,
        link_document_product,
        mark_version_failed,
        mark_version_ready,
    )
    from app.versioning import content_sha256, estimate_token_count
    from app.storage import storage_root

    payload = _decode_job_payload(job)
    storage_uri = payload["storage_uri"]
    document_id = int(payload["document_id"])
    version_id = int(payload["version_id"])

    full_path = storage_root() / storage_uri
    if not full_path.exists():
        raise FileNotFoundError(f"存储文件不存在: {full_path}")

    content = full_path.read_bytes()
    actual_sha = content_sha256(content)

    document_version = db.get(DocumentVersion, version_id)
    if document_version is None:
        raise FileNotFoundError(f"document_version {version_id} 不存在")
    parser = get_parser(job.source)

    try:
        chunks: list[ChunkDraft] = list(parser.parse(content, filename=full_path.name))
    except ParserError as exc:
        mark_version_failed(
            db, version_id=version_id, code=exc.code, message=exc.message
        )
        raise

    for chunk_no, draft in enumerate(chunks, start=1):
        text_hash = hashlib.sha256(draft.text.encode("utf-8")).hexdigest()
        append_document_chunk(
            db,
            version_id=version_id,
            chunk_no=chunk_no,
            text=draft.text,
            content_hash=text_hash,
            token_count=estimate_token_count(draft.text),
            page_no=draft.page_no,
            paragraph_no=draft.paragraph_no,
        )

    # 启发式商品关联
    products = db.scalars(select(Product)).all()
    haystack = "\n".join(draft.text for draft in chunks)
    for product in products:
        candidates = [product.external_product_id, product.url, product.title]
        for needle in candidates:
            if needle and len(needle) >= 6 and needle in haystack:
                link_document_product(
                    db,
                    document_version_id=version_id,
                    product_id=product.id,
                    relation="mention",
                )
                break

    db.commit()
    mark_version_ready(db, version_id=version_id)
    job.cursor = str(len(chunks))

    # 入队 S3 索引任务
    submit_chunk_index(
        db,
        document_id=document_id,
        version_id=version_id,
    )


def submit_chunk_index(
    db: Session,
    *,
    document_id: int,
    version_id: int,
    max_retries: int | None = None,
) -> CrawlJob:
    """S3 索引任务入队：把 version 下所有 chunks embed 到向量库。"""
    job = enqueue_job(
        db,
        type_="document_index",
        source="index",
        keyword=f"v{version_id}",
        payload={"document_id": document_id, "version_id": version_id},
        max_retries=max_retries,
    )
    future = get_executor().submit(_run_one_job, job.id, new_worker_id())
    _attach_cancel_future(job.id, future)
    return job


def _execute_document_index(db: Session, job: CrawlJob) -> None:
    """S3 索引任务：embed chunks 并追加到向量库。"""
    from app.db import DocumentChunk, DocumentVersion, Product
    from app.rag_runtime import get_embedder, get_vector_store
    from app.rag import VectorRecord

    payload = _decode_job_payload(job)
    version_id = int(payload["version_id"])
    document_id = int(payload["document_id"])

    version = db.get(DocumentVersion, version_id)
    if version is None:
        raise FileNotFoundError(f"document_version {version_id} 不存在")

    chunks = db.scalars(
        select(DocumentChunk)
        .where(DocumentChunk.document_version_id == version_id)
        .order_by(DocumentChunk.chunk_no)
    ).all()
    if not chunks:
        job.cursor = "0"
        return

    embedder = get_embedder()
    store = get_vector_store()

    # 删除旧索引（重新索引）
    store.delete_by_version(version_id)

    records = []
    for chunk in chunks:
        vec = embedder.embed(chunk.text)
        snippet = chunk.text[:200]
        locator = {
            "page_no": chunk.page_no,
            "paragraph_no": chunk.paragraph_no,
        }
        records.append(
            VectorRecord(
                id=f"v{version_id}-c{chunk.chunk_no}",
                vector=vec,
                payload={
                    "chunk_id": chunk.id,
                    "document_id": document_id,
                    "document_version_id": version_id,
                    "snippet": snippet,
                    "locator": locator,
                    "version_no": version.version_no,
                },
            )
        )
    store.add(records)
    job.cursor = str(len(records))


def _decode_job_payload(job: CrawlJob) -> dict:
    """从 keyword 与 cursor 字段还原提交时的 payload。"""
    import json

    cursor = job.cursor
    if cursor:
        try:
            return json.loads(cursor)
        except (ValueError, TypeError):
            return {}
    return {}


def is_cancel_requested_for(job_id: int, db: Session) -> bool:
    return is_cancel_requested(db, job_id=job_id)


def utcnow() -> datetime:
    return datetime.utcnow()


# 用于测试：等待所有 submitted job 完成
def wait_for_jobs(timeout: float = 10.0) -> None:
    """测试辅助：等到所有提交的 future 完成。

    注意：文档导入完成后会再入队文档索引任务，存在小窗口期内
    `_cancel_futures` 暂时为空的情况；本函数用多次轮询降低这种
    边界条件导致的不稳定。
    """
    import time as _time

    deadline = _time.time() + timeout
    empty_seen_count = 0
    while _time.time() < deadline:
        with _cancel_futures_lock:
            if not _cancel_futures:
                empty_seen_count += 1
                # 连续两次看到空列表，认为真的完成了
                if empty_seen_count >= 2:
                    return
                _time.sleep(0.05)
                continue
            empty_seen_count = 0
            futures = list(_cancel_futures.values())
        for f in futures:
            try:
                f.result(timeout=0.5)
            except TimeoutError:
                continue
            except Exception:  # noqa: BLE001
                pass
        _time.sleep(0.05)