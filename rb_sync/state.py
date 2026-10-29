"""本地状态库：歌单快照 + 选择配置。

一切中间产物都是磁盘上的 JSON，可人工查看、可 diff、可重跑；
不引入数据库，也不把状态藏在内存里。

快照文件名是 ``<source>_<playlist_id>.json`` —— 多平台共存时不会互相覆盖。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .model import PlaylistSnapshot, Selection, dump_json, load_json

DEFAULT_STATE_DIR = Path("state")

_UNSAFE = re.compile(r"[^0-9A-Za-z._-]+")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class StateStore:
    def __init__(self, root: str | Path = DEFAULT_STATE_DIR) -> None:
        self.root = Path(root)
        self.playlists_dir = self.root / "playlists"

    # ------------------------------------------------------------- 路径

    @property
    def selection_path(self) -> Path:
        return self.root / "selection.json"

    def snapshot_path(self, source: str, playlist_id: str) -> Path:
        safe_id = _UNSAFE.sub("_", str(playlist_id))[:80]
        return self.playlists_dir / f"{source}_{safe_id}.json"

    # ----------------------------------------------------------- 快照

    def load_snapshot(self, source: str, playlist_id: str) -> PlaylistSnapshot | None:
        data = load_json(self.snapshot_path(source, playlist_id))
        if data is None:
            return None
        return PlaylistSnapshot.from_dict(data)

    def save_snapshot(self, snapshot: PlaylistSnapshot) -> Path:
        path = self.snapshot_path(snapshot.source, snapshot.playlist_id)
        dump_json(path, snapshot.to_dict())
        return path

    # ----------------------------------------------------------- 选择

    def load_selection(self) -> Selection | None:
        data = load_json(self.selection_path)
        if data is None:
            return None
        return Selection.from_dict(data)

    def save_selection(self, selection: Selection) -> Path:
        dump_json(self.selection_path, selection.to_dict())
        return self.selection_path
