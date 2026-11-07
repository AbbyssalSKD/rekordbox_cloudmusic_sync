"""JSON API。所有 handler 都返回 (status_code, payload)。

约定：
- 只读接口直接返回结果；
- 可能慢的接口返回 ``{"job_id": ...}``，由前端轮询；
- 错误统一 ``{"error": {"code", "message", "hint"}}``，hint 是给用户看的下一步。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import services
from ..config import load_settings, update_settings
from ..model import load_json
from ..state import StateStore
from .jobs import JobRegistry

MAX_LIBRARY_JSON_TRACKS = 500


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.hint = hint


class Api:
    def __init__(self, state_dir: str | Path = "state") -> None:
        self.state_dir = Path(state_dir)
        self.jobs = JobRegistry()

    # ------------------------------------------------------------- 路由

    def dispatch(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, dict]:
        body = body or {}
        try:
            return self._dispatch(method, path, query or {}, body)
        except ApiError as exc:
            payload: dict[str, Any] = {"error": {"code": exc.code, "message": exc.message}}
            if exc.hint:
                payload["error"]["hint"] = exc.hint
            return exc.status, payload
        except Exception as exc:  # noqa: BLE001 - 兜底，避免 500 空白页
            return 500, {
                "error": {
                    "code": "internal",
                    "message": str(exc) or exc.__class__.__name__,
                    "hint": "看终端日志；如果反复出现，把这条报错发出来",
                }
            }

    def _dispatch(self, method: str, path: str, query: dict, body: dict) -> tuple[int, dict]:
        if path == "/api/status" and method == "GET":
            return 200, self.status()
        if path == "/api/settings":
            if method == "GET":
                return 200, {"settings": load_settings().to_dict(mask_secrets=True)}
            if method in ("PUT", "POST"):
                update_settings(body)
                return 200, {"settings": load_settings().to_dict(mask_secrets=True)}
        if path == "/api/settings/probe" and method == "POST":
            return 200, services.probe_environment(body.get("db_path") or None)
        if path == "/api/library" and method == "GET":
            return 200, self.library()
        if path == "/api/selection" and method == "GET":
            return 200, self.selection()
        if path == "/api/plan" and method == "POST":
            return 200, self.plan(body)
        if path == "/api/relink/preview" and method == "POST":
            return 200, self.relink_preview(body)
        if path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            job = self.jobs.get(job_id)
            if job is None:
                raise ApiError(404, "job_not_found", "任务不存在或已过期")
            return 200, job
        if path == "/api/jobs" and method == "GET":
            return 200, {"jobs": self.jobs.recent()}
        if path == "/api/backups" and method == "GET":
            return 200, {"backups": []}  # W2 才有
        raise ApiError(404, "not_found", f"没有这个接口：{method} {path}")

    # ----------------------------------------------------------- handler

    def status(self) -> dict:
        from .. import __version__
        from ..rekordbox import RekordboxError, is_rekordbox_running, resolve_db_path

        settings = load_settings()
        db_ok = False
        db_path = settings.db_path
        try:
            db_path = str(resolve_db_path(settings.db_path or None))
            db_ok = True
        except RekordboxError:
            pass
        return {
            "version": __version__,
            "state_dir": str(self.state_dir.resolve()),
            "db_path": db_path,
            "db_found": db_ok,
            "configured": bool(settings.db_path),
            "music_roots": settings.music_roots,
            "rekordbox_running": is_rekordbox_running(),
            "readonly": True,
        }

    def library(self) -> dict:
        from ..rekordbox import RekordboxError, is_rekordbox_running

        settings = load_settings()
        try:
            report = services.inspect_library(settings.db_path or None, self.state_dir)
        except RekordboxError as exc:
            raise ApiError(
                400,
                "library_unavailable",
                str(exc),
                "在「设置」里填好 master.db 的完整路径，再点「探测」",
            ) from exc
        payload = report.to_dict()
        # 缺失清单可能上千条，界面只用于展示，先截断避免响应过大
        payload["missing_tracks"] = payload["missing_tracks"][:MAX_LIBRARY_JSON_TRACKS]
        payload["rekordbox_running"] = is_rekordbox_running()
        return payload

    def selection(self) -> dict:
        store = StateStore(self.state_dir)
        selection = store.load_selection()
        if selection is None:
            return {"playlists": [], "generated_at": ""}
        return selection.to_dict()

    def plan(self, body: dict) -> dict:
        inputs = body.get("inputs") or []
        if isinstance(inputs, str):
            inputs = [line.strip() for line in inputs.splitlines() if line.strip()]
        if not inputs:
            raise ApiError(400, "no_input", "至少填一个歌单链接", "每行一个链接，或先放一个本地 CSV 路径")

        settings = load_settings()
        source = body.get("source") or None
        if source in ("auto", ""):
            source = None

        def job():
            result = services.run_plan(
                inputs,
                forced_source=source,
                state_dir=self.state_dir,
                refresh=bool(body.get("refresh")),
                append=bool(body.get("append")),
                name=body.get("name") or None,
                timeout=float(body.get("timeout") or 25.0),
            )
            return {
                "selection": str(result.selection_path) if result.selection_path else None,
                "selection_count": result.selection_count,
                "dropped_previous": result.dropped_previous,
                "playlists": result.playlists,
                "changes": result.report,
                "errors": result.errors,
            }

        return {"job_id": self.jobs.submit("读取歌单", job)}

    def relink_preview(self, body: dict) -> dict:
        from_root = (body.get("from_root") or "").strip()
        to_root = (body.get("to_root") or "").strip()
        if not from_root or not to_root:
            raise ApiError(
                400,
                "need_roots",
                "需要同时给出「旧根」和「新根」",
                "例如旧根 E:/Music、新根 F:/Music。旧根可以从体检页的缺失分布里看出来。",
            )
        search = body.get("search_roots") or []
        if isinstance(search, str):
            search = [line.strip() for line in search.splitlines() if line.strip()]
        settings = load_settings()

        def job():
            plan, out = services.preview_relink(
                settings.db_path or None,
                from_root,
                to_root,
                search,
                state_dir=self.state_dir,
            )
            return {"plan": plan.to_dict(), "out": str(out)}

        return {"job_id": self.jobs.submit("计算重定位映射", job)}
