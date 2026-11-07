"""WebUI 依赖的后台任务与 HTTP 服务（W1：只读）。"""

from .jobs import Job, JobRegistry

__all__ = ["Job", "JobRegistry"]
