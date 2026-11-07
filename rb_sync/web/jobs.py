"""后台任务：把可能超过 2 秒的操作（读歌单、扫目录、读库）丢进线程。

前端 POST 后拿到 job_id，再轮询 `/api/jobs/{id}`。这样界面不会被一个慢请求卡住，
也不需要 SSE（W1 阶段够用，W3 再换推送）。
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Job:
    id: str
    label: str
    status: str = "pending"  # pending | running | done | error
    result: Any = None
    error: str = ""
    detail: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        start = self.started_at or self.created_at
        return round(end - start, 2)

    def to_dict(self, *, include_result: bool = True) -> dict:
        payload = {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "error": self.error,
            "detail": self.detail,
            "elapsed": self.elapsed,
        }
        if include_result:
            payload["result"] = self.result
        return payload


class JobRegistry:
    def __init__(self, max_workers: int = 2, keep: int = 20) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rb-job")
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._keep = keep

    # ------------------------------------------------------------------

    def submit(self, label: str, fn: Callable[[], Any]) -> str:
        job = Job(id=uuid.uuid4().hex[:12], label=label)
        with self._lock:
            self._jobs[job.id] = job
            self._trim_locked()
        self._executor.submit(self._run, job, fn)
        return job.id

    def _run(self, job: Job, fn: Callable[[], Any]) -> None:
        with self._lock:
            job.status = "running"
            job.started_at = time.time()
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - 任何异常都要变成结构化错误
            with self._lock:
                job.status = "error"
                job.error = str(exc) or exc.__class__.__name__
                job.detail = traceback.format_exc(limit=3)
                job.finished_at = time.time()
            return
        with self._lock:
            job.status = "done"
            job.result = result
            job.finished_at = time.time()

    def _trim_locked(self) -> None:
        if len(self._jobs) <= self._keep:
            return
        finished = [j for j in self._jobs.values() if j.status in ("done", "error")]
        finished.sort(key=lambda j: j.finished_at)
        for stale in finished[: len(self._jobs) - self._keep]:
            self._jobs.pop(stale.id, None)

    # ------------------------------------------------------------------

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.to_dict() if job else None

    def recent(self, limit: int = 10) -> list[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            return [j.to_dict(include_result=False) for j in jobs[:limit]]
