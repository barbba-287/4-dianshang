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
from app.crawler import CrawlError
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
    recover_expired_jobs,
    upsert_product_batch,
)


logger = logging.getLogger(__name__)


_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_dispatch_stop: threading.Event | None = None
_dispatch_thread: threading.Thread | None = None
_accepting_jobs = True


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


def _submit_future(job_id: int) -> Future:
    """提交一个尚未执行的任务，并记录 Future 供取消和 shutdown 使用。"""
    global _executor
    # Admission check and executor.submit must be one critical section so a
    # shutdown cannot accept a future after it has stopped the dispatcher.
    with _executor_lock:
        if not _accepting_jobs:
            raise RuntimeError("后台任务服务正在停止，不再接受新任务")
        if _executor is None:
            settings = get_settings()
            _executor = ThreadPoolExecutor(
                max_workers=settings.worker_concurrency,
                thread_name_prefix="ds-job",
            )
        future = _executor.submit(_run_one_job, job_id, new_worker_id())
        _attach_cancel_future(job_id, future)
    # A very short task can finish before it is attached to the registry.
    # Remove it here so wait_for_jobs() does not retain a completed Future.
    if future.done():
        _pop_cancel_future(job_id)
    return future


def _dispatch_loop(stop_event: threading.Event) -> None:
    """扫描新任务和到期重试任务，确保任务不会停在 retry_wait。"""
    settings = get_settings()
    first_scan = True
    while first_scan or not stop_event.wait(settings.worker_poll_seconds):
        first_scan = False
        db = SessionLocal()
        try:
            recovered = recover_expired_jobs(db)
            if recovered:
                logger.warning("recovered expired jobs", extra={"count": recovered})
            now = datetime.utcnow()
            rows = db.scalars(
                select(CrawlJob.id)
                .where(
                    (CrawlJob.status == JobStatus.QUEUED.value)
                    | (
                        (CrawlJob.status == JobStatus.RETRY_WAIT.value)
                        & (
                            CrawlJob.next_run_at.is_(None)
                            | (CrawlJob.next_run_at <= now)
                        )
                    )
                )
                .order_by(CrawlJob.id)
                .limit(settings.worker_concurrency * 2)
            ).all()
        except Exception:
            logger.exception("后台任务调度扫描失败")
            rows = []
        finally:
            db.close()

        with _cancel_futures_lock:
            active_ids = set(_cancel_futures)
        for job_id in rows:
            if job_id in active_ids:
                continue
            try:
                _submit_future(job_id)
            except RuntimeError:
                return
            except Exception:
                logger.exception("任务 %s 重新提交失败", job_id)


def is_accepting_jobs() -> bool:
    """返回当前 worker 是否接受新任务。"""
    with _executor_lock:
        return _accepting_jobs and _executor is not None


def start_background_workers() -> None:
    """启动任务调度线程；重复调用安全。"""
    global _dispatch_stop, _dispatch_thread, _accepting_jobs
    # Do not call get_executor while holding _executor_lock: get_executor
    # acquires the same non-reentrant lock during lazy initialization.
    get_executor()
    with _executor_lock:
        _accepting_jobs = True
        if _dispatch_thread is not None and _dispatch_thread.is_alive():
            return
        _dispatch_stop = threading.Event()
        _dispatch_thread = threading.Thread(
            target=_dispatch_loop,
            args=(_dispatch_stop,),
            daemon=True,
            name="ds-dispatcher",
        )
        _dispatch_thread.start()


def stop_background_workers(timeout: float = 5.0) -> None:
    """停止接收任务，等待有限时间后释放 executor。"""
    global _executor, _dispatch_stop, _dispatch_thread, _accepting_jobs
    with _executor_lock:
        _accepting_jobs = False
        stop_event = _dispatch_stop
        dispatch_thread = _dispatch_thread
        executor = _executor
        _dispatch_stop = None
        _dispatch_thread = None
    if stop_event is not None:
        stop_event.set()
    if dispatch_thread is not None:
        dispatch_thread.join(timeout=timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _cancel_futures_lock:
            futures = list(_cancel_futures.values())
        if not futures:
            break
        if all(future.done() for future in futures):
            break
        time.sleep(0.05)
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)
    with _cancel_futures_lock:
        _cancel_futures.clear()
    with _executor_lock:
        _executor = None


def reset_executor() -> None:
    """测试中关闭 executor 并释放线程；不要在生产代码里调用。"""
    stop_background_workers(timeout=2.0)


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
    _submit_future(job.id)
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
    _submit_future(job.id)
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
    """线程入口：claim + heartbeat + 执行 + finish/retry。"""
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
            if is_cancel_requested(db, job_id=job_id):
                _cancel_job(db, job_id=job_id, worker_id=worker_id)
            else:
                finish_job(db, job_id=job_id, worker_id=worker_id, cursor=str(job.cursor or ""))
                logger.info("job %s succeeded", job_id)
        except (JobCancelled, CrawlError) as exc:
            if getattr(exc, "code", None) == "CANCELLED" or isinstance(exc, JobCancelled):
                _cancel_job(db, job_id=job_id, worker_id=worker_id)
                logger.info("job %s cancelled during execution", job_id)
            else:
                _retry_after_rollback(db, job, worker_id, exc)
        except Exception as exc:  # noqa: BLE001 - 顶层兜底
            _retry_after_rollback(db, job, worker_id, exc)
        finally:
            stop_event.set()
            heartbeat.join(timeout=2)
    finally:
        db.close()
        _pop_cancel_future(job_id)


def _cancel_job(db: Session, *, job_id: int, worker_id: str) -> None:
    db.rollback()
    job = db.get(CrawlJob, job_id)
    if job is None or job.worker_id != worker_id:
        return
    job.status = JobStatus.CANCELLED.value
    job.cancel_requested = True
    job.lease_until = None
    job.finished_at = datetime.utcnow()
    db.commit()


def _retry_after_rollback(db: Session, job: CrawlJob, worker_id: str, exc: Exception) -> None:
    db.rollback()
    code = getattr(exc, "code", type(exc).__name__)
    message = getattr(exc, "message", str(exc))
    try:
        retry_job(
            db,
            job_id=job.id,
            worker_id=worker_id,
            error_code=code,
            error_message=message[:500],
            backoff_seconds=compute_backoff_seconds(job.attempt),
        )
    except Exception:
        db.rollback()
        logger.exception("job %s could not be moved to retry/failed", job.id)

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
    from app.upload_security import safe_storage_path

    payload = _decode_job_payload(job)
    storage_uri = payload["storage_uri"]
    document_id = int(payload["document_id"])
    version_id = int(payload["version_id"])

    full_path = safe_storage_path(storage_root(), storage_uri)
    if not full_path.is_file():
        raise FileNotFoundError("存储文件不存在")

    content = full_path.read_bytes()
    actual_sha = content_sha256(content)

    document_version = db.get(DocumentVersion, version_id)
    if document_version is None:
        raise FileNotFoundError(f"document_version {version_id} 不存在")
    if actual_sha != document_version.sha256:
        mark_version_failed(
            db, version_id=version_id, code="CONTENT_HASH_MISMATCH", message="存储文件完整性校验失败"
        )
        raise ParserError("CONTENT_HASH_MISMATCH", "存储文件完整性校验失败")
    parser = get_parser(job.source)

    try:
        chunks: list[ChunkDraft] = list(parser.parse(content, filename=full_path.name))
    except ParserError as exc:
        mark_version_failed(
            db, version_id=version_id, code=exc.code, message=exc.message
        )
        raise
    except Exception as exc:  # noqa: BLE001 - parser 库异常统一转为失败版本
        mark_version_failed(
            db, version_id=version_id, code="PARSER_ERROR", message="文档解析失败"
        )
        raise ParserError("PARSER_ERROR", "文档解析失败") from exc

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
    _submit_future(job.id)
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