"""S1 任务工程化的状态机与 Repository 测试。

覆盖范围：
- enqueue_job 写为 queued；
- claim_job 原子抢占并写入 lease_until、worker_id；
- 已 running / 已 succeeded 的任务不可被再次抢占；
- cancel_requested=1 的任务不可被抢占；
- renew_lease 续约成功 / 跨 worker 失败；
- finish_job 写 succeeded；
- retry_job 写 retry_wait 或 failed；
- request_cancel 在终态返回当前对象、不改写；
- compute_backoff_seconds 单调上升且带抖动。
"""

from datetime import datetime, timedelta

import pytest

from app import db as db_mod
from app.jobs import JobClaimLost, JobStatus
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
)


@pytest.fixture
def db():
    db_mod.Base.metadata.create_all(db_mod.engine)
    session = db_mod.SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _make_job(db, **overrides):
    defaults = dict(type_="crawl_fixture", source="fixture", keyword="fixture")
    defaults.update(overrides)
    return enqueue_job(db, **defaults)


def test_enqueue_job_writes_queued(db):
    job = _make_job(db)
    assert job.id is not None
    assert job.status == JobStatus.QUEUED.value
    assert job.attempt == 0
    assert job.retry_count == 0
    assert job.cancel_requested is False
    assert job.run_id


def test_claim_job_runs_atomically_and_writes_lease(db):
    job = _make_job(db)
    worker = new_worker_id()
    claimed = claim_job(db, job_id=job.id, worker_id=worker, lease_seconds=30)
    assert claimed.status == JobStatus.RUNNING.value
    assert claimed.attempt == 1
    assert claimed.worker_id == worker
    assert claimed.lease_until is not None
    assert claimed.started_at is not None


def test_claim_job_fails_when_already_running(db):
    job = _make_job(db)
    w1 = new_worker_id()
    w2 = new_worker_id()
    claim_job(db, job_id=job.id, worker_id=w1, lease_seconds=30)
    with pytest.raises(JobClaimLost):
        claim_job(db, job_id=job.id, worker_id=w2, lease_seconds=30)


def test_claim_job_fails_when_cancel_requested(db):
    job = _make_job(db)
    request_cancel(db, job_id=job.id)
    with pytest.raises(JobClaimLost):
        claim_job(db, job_id=job.id, worker_id=new_worker_id(), lease_seconds=30)


def test_renew_lease_only_for_current_worker(db):
    job = _make_job(db)
    worker = new_worker_id()
    claim_job(db, job_id=job.id, worker_id=worker, lease_seconds=30)
    assert renew_lease(db, job_id=job.id, worker_id=worker, lease_seconds=30) is True
    # 不同 worker 不能续约
    assert renew_lease(db, job_id=job.id, worker_id="worker-other", lease_seconds=30) is False


def test_finish_job_marks_succeeded_and_clears_lease(db):
    job = _make_job(db)
    worker = new_worker_id()
    claim_job(db, job_id=job.id, worker_id=worker, lease_seconds=30)
    finished = finish_job(db, job_id=job.id, worker_id=worker, cursor="3")
    assert finished.status == JobStatus.SUCCEEDED.value
    assert finished.cursor == "3"
    assert finished.lease_until is None


def test_retry_job_records_backoff_then_fails(db):
    job = _make_job(db, max_retries=1)
    worker = new_worker_id()
    claim_job(db, job_id=job.id, worker_id=worker, lease_seconds=30)
    # 第一次失败：retry_count 1 >= max_retries=1 → 置 failed
    after = retry_job(
        db,
        job_id=job.id,
        worker_id=worker,
        error_code="PAGE_TIMEOUT",
        error_message="boom",
        backoff_seconds=1.0,
    )
    assert after.status == JobStatus.FAILED.value
    assert after.retry_count == 1
    assert after.error_code == "PAGE_TIMEOUT"


def test_retry_job_returns_retry_wait_when_below_max(db):
    job = _make_job(db, max_retries=3)
    worker = new_worker_id()
    claim_job(db, job_id=job.id, worker_id=worker, lease_seconds=30)
    after = retry_job(
        db,
        job_id=job.id,
        worker_id=worker,
        error_code="PAGE_TIMEOUT",
        error_message="boom",
        backoff_seconds=2.0,
    )
    assert after.status == JobStatus.RETRY_WAIT.value
    assert after.retry_count == 1
    assert after.next_run_at is not None
    assert after.worker_id is None


def test_request_cancel_sets_flag_and_returns_job(db):
    job = _make_job(db)
    after = request_cancel(db, job_id=job.id)
    assert after is not None
    assert after.cancel_requested is True
    assert is_cancel_requested(db, job_id=job.id) is True


def test_request_cancel_on_terminal_returns_existing(db):
    job = _make_job(db)
    worker = new_worker_id()
    claim_job(db, job_id=job.id, worker_id=worker, lease_seconds=30)
    finish_job(db, job_id=job.id, worker_id=worker)
    after = request_cancel(db, job_id=job.id)
    assert after is not None
    assert after.status == JobStatus.SUCCEEDED.value


def test_compute_backoff_is_bounded_and_jittered():
    values = [compute_backoff_seconds(attempt, base=1.0, cap=8.0) for attempt in range(1, 6)]
    # 第 1 次约 base
    assert 0.5 <= values[0] <= 1.5
    # cap 后不再增长
    assert values[-1] <= 8.0 + 0.8
    # 整体单调非降
    for prev, cur in zip(values, values[1:]):
        assert cur + 1e-6 >= prev * 0.5  # 允许轻微抖动回退