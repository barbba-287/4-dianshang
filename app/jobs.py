"""电商工作台的任务状态机与领域异常。

用途：定义 JobStatus 枚举、合法状态转移表和任务相关异常，供
app/background.py、app/repository.py 与 app/main.py 共用。
"""

from enum import Enum
from typing import Final


class JobStatus(str, Enum):
    """任务状态枚举。

    - queued：已入队，等待 claim
    - running：被某个 worker 抢占，正在执行
    - retry_wait：失败后等待 backoff 后重试
    - cancelled：被取消（执行前或执行中）
    - succeeded：成功完成
    - failed：达到最大重试次数，永久失败
    """

    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# 合法状态转移表：from -> {to}
_ALLOWED_TRANSITIONS: Final[dict[str, set[str]]] = {
    JobStatus.QUEUED.value: {JobStatus.RUNNING.value, JobStatus.CANCELLED.value, JobStatus.FAILED.value},
    JobStatus.RUNNING.value: {
        JobStatus.SUCCEEDED.value,
        JobStatus.RETRY_WAIT.value,
        JobStatus.FAILED.value,
        JobStatus.CANCELLED.value,
    },
    JobStatus.RETRY_WAIT.value: {
        JobStatus.RUNNING.value,
        JobStatus.CANCELLED.value,
        JobStatus.FAILED.value,
    },
    JobStatus.SUCCEEDED.value: set(),
    JobStatus.FAILED.value: set(),
    JobStatus.CANCELLED.value: set(),
}


def is_terminal(status: str) -> bool:
    return status in (JobStatus.SUCCEEDED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value)


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in _ALLOWED_TRANSITIONS.get(from_status, set())


class JobError(Exception):
    """任务相关异常的基类。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class InvalidTransition(JobError):
    def __init__(self, from_status: str, to_status: str):
        super().__init__(
            "INVALID_TRANSITION",
            f"无法从 {from_status} 转移到 {to_status}",
        )
        self.from_status = from_status
        self.to_status = to_status


class JobClaimLost(JobError):
    """任务被其他 worker 抢占或被取消。"""

    def __init__(self, job_id: int):
        super().__init__(
            "JOB_CLAIM_LOST",
            f"任务 #{job_id} 未被当前 worker 抢占",
        )
        self.job_id = job_id


class JobCancelled(JobError):
    """任务在执行中被取消。"""

    def __init__(self, job_id: int):
        super().__init__(
            "JOB_CANCELLED",
            f"任务 #{job_id} 已被取消",
        )
        self.job_id = job_id