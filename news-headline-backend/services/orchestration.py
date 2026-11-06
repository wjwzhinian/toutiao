"""采集任务状态机和有限重试策略。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from models.operations import CollectionJob
from models.news import NewsFeedSource


RETRYABLE_COLLECTION_ERRORS = frozenset(
    {"fetch_timeout", "fetch_failed", "internal_error", "http_status_408", "http_status_429", "http_status_500", "http_status_502", "http_status_503", "http_status_504"}
)


def retry_delay_seconds(attempt: int, *, base: int = 60, maximum: int = 3600) -> int:
    """指数退避并设置上限，避免失败来源持续占满采集线程。"""
    attempt = max(1, int(attempt))
    return min(maximum, base * (2 ** (attempt - 1)))


def next_job_state(
    *,
    current: str,
    error_code: str | None,
    attempt_count: int,
    max_attempts: int,
) -> tuple[str, datetime | None]:
    """返回任务下一状态和下次重试时间；状态转换不依赖模型输出。"""
    if not error_code:
        return "succeeded", None
    if error_code in RETRYABLE_COLLECTION_ERRORS and attempt_count < max_attempts:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return "retrying", now + timedelta(seconds=retry_delay_seconds(attempt_count))
    return "failed", None


def serialize_job_result(result: dict[str, Any]) -> str:
    """限制任务结果为可审计 JSON，不保存异常对象或密钥。"""
    return json.dumps(result, ensure_ascii=False, default=str)[:20_000]


def start_collection_job(
    db: Any,
    source: NewsFeedSource,
    *,
    trigger: str,
    requested_by: int | None = None,
) -> CollectionJob:
    """建立一次可审计的采集执行记录。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    job = CollectionJob(
        source_id=source.id,
        trigger=trigger,
        status="running",
        attempt_count=source.collection_failures + 1,
        max_attempts=5,
        started_at=now,
        requested_by=requested_by,
    )
    db.add(job)
    return job


def finish_collection_job(job: CollectionJob, source: NewsFeedSource, result: dict[str, Any]) -> None:
    """成功时清除重试状态并保存有限的任务结果。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    source.collection_failures = 0
    source.next_retry_at = None
    source.last_error = None
    source.last_fetched_at = now
    job.status = "succeeded"
    job.error_code = None
    job.next_retry_at = None
    job.finished_at = now
    job.result_json = serialize_job_result(result)


def fail_collection_job(job: CollectionJob, source: NewsFeedSource, error_code: str) -> None:
    """失败时根据错误类型和次数决定重试或转人工处理。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    source.collection_failures += 1
    source.last_fetched_at = now
    source.last_error = error_code[:100]
    retryable = error_code in RETRYABLE_COLLECTION_ERRORS and source.collection_failures < job.max_attempts
    source.next_retry_at = (
        now + timedelta(seconds=retry_delay_seconds(source.collection_failures))
        if retryable else None
    )
    job.status = "retrying" if retryable else "failed"
    job.error_code = error_code[:100]
    job.next_retry_at = source.next_retry_at
    job.finished_at = now
    job.result_json = serialize_job_result({"error": error_code[:100]})


def job_payload(job: CollectionJob) -> dict[str, Any]:
    """将任务模型转换成前端和 Agent 可安全展示的字段。"""
    return {
        "id": job.id,
        "sourceId": job.source_id,
        "trigger": job.trigger,
        "status": job.status,
        "attemptCount": job.attempt_count,
        "maxAttempts": job.max_attempts,
        "nextRetryAt": job.next_retry_at,
        "startedAt": job.started_at,
        "finishedAt": job.finished_at,
        "errorCode": job.error_code,
        "result": json.loads(job.result_json) if job.result_json else None,
        "createdAt": job.created_at,
    }
