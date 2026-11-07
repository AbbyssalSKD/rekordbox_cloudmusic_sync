"""fetch 的增量状态：每首曲目的下载结果，用于断点续跑与「别重复下载」。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

STATE_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class FetchState:
    def __init__(self, path: str | Path = "state/fetch_state.json") -> None:
        self.path = Path(path)
        self.items: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        items = data.get("items")
        if isinstance(items, dict):
            self.items = items

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "updated_at": _now(),
            "items": self.items,
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return self.path

    # ------------------------------------------------------------------

    def get(self, uid: str) -> dict | None:
        return self.items.get(uid)

    def status_of(self, uid: str) -> str:
        entry = self.items.get(uid)
        return str(entry.get("status")) if entry else ""

    def mark(self, uid: str, status: str, path: str = "", error: str = "") -> None:
        entry = {"status": status, "at": _now()}
        if path:
            entry["path"] = path
        if error:
            entry["error"] = error[:500]
        self.items[uid] = entry

    def can_skip(self, uid: str) -> bool:
        """跳过条件分两档：

        - ``present``：磁盘上确认到了，且文件还在 → 跳过
        - ``downloaded``：外部命令返回过 0 → 也跳过（不重复调用），但仍会出现在
          ``unverified`` 里提醒人工核对

        没有路径的 ``downloaded`` 也跳过，否则「命令成功但没按预期落盘」会导致
        每次重跑都重复下载同一批。
        """
        entry = self.items.get(uid)
        if not entry:
            return False
        status = entry.get("status")
        if status == "downloaded":
            return True
        if status == "present":
            recorded = entry.get("path") or ""
            return bool(recorded) and Path(recorded).is_file()
        return False

    def downloaded_count(self) -> int:
        return sum(1 for e in self.items.values() if e.get("status") == "downloaded")
