"""Rekordbox 库访问层（只读侧）。

设计要点：
1. pyrekordbox 是**可选依赖** —— 只有 doctor / relink / 将来的 write 需要它，
   `plan`（读歌单）永远不需要联网也不需要它。
2. 只读模式下**永远操作副本**：把 master.db 复制到 state/.cache/ 再打开，
   真库一个字节都不动。这样即使 pyrekordbox 或我们的代码有 bug，也不会
   在你那份唯一的库上留下痕迹（你机器上已经有过两次 Corrupt）。
"""

from __future__ import annotations

import gc
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REKORDBOX_PROCESSES = ("rekordbox.exe", "rekordboxAgent.exe")


class RekordboxError(RuntimeError):
    """库访问相关的错误（找不到库 / 依赖缺失 / 库被占用）。"""


def default_db_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RekordboxError("无法确定 APPDATA，请用 --db 显式指定 master.db 路径")
    return Path(appdata) / "Pioneer" / "rekordbox" / "master.db"


def resolve_db_path(explicit: str | Path | None = None) -> Path:
    path = Path(explicit).expanduser() if explicit else default_db_path()
    if not path.is_file():
        raise RekordboxError(
            f"找不到 Rekordbox 库：{path}\n"
            "  用 --db 指定 master.db 的完整路径（便携/迁移场景常见）"
        )
    return path


def is_rekordbox_running() -> bool:
    """Rekordbox 是否在运行。

    写库前必须为 False —— pyrekordbox 在软件运行时无法可靠提交变更，
    强行写正是把库写坏（Corrupt）的主要成因。
    """
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        # 探测失败时按"可能在运行"处理，宁可拒绝写也不冒险
        return True
    return any(name.lower() in out for name in REKORDBOX_PROCESSES)


@dataclass
class PlaylistRow:
    id: str
    name: str
    attribute: int
    parent_id: str | None
    song_count: int

    @property
    def is_folder(self) -> bool:
        return self.attribute == 1

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "attribute": self.attribute,
            "is_folder": self.is_folder,
            "parent_id": self.parent_id,
            "song_count": self.song_count,
        }


@dataclass
class TrackRow:
    id: str
    title: str
    artist: str
    path: str
    exists: bool
    is_factory: bool

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "artist": self.artist,
            "path": self.path,
            "exists": self.exists,
            "is_factory": self.is_factory,
        }


@dataclass
class LibraryReport:
    db_path: Path
    db_size: int = 0
    db_mtime: str = ""
    working_copy: Path | None = None
    integrity: str = ""
    usn: int = 0
    tracks: list[TrackRow] = field(default_factory=list)
    playlists: list[PlaylistRow] = field(default_factory=list)

    @property
    def factory_tracks(self) -> list[TrackRow]:
        return [t for t in self.tracks if t.is_factory]

    @property
    def user_tracks(self) -> list[TrackRow]:
        return [t for t in self.tracks if not t.is_factory]

    @property
    def present(self) -> list[TrackRow]:
        return [t for t in self.user_tracks if t.exists]

    @property
    def missing(self) -> list[TrackRow]:
        return [t for t in self.user_tracks if not t.exists]

    def to_dict(self) -> dict:
        return {
            "db_path": str(self.db_path),
            "db_size": self.db_size,
            "db_mtime": self.db_mtime,
            "integrity": self.integrity,
            "usn": self.usn,
            "counts": {
                "tracks": len(self.tracks),
                "factory": len(self.factory_tracks),
                "present": len(self.present),
                "missing": len(self.missing),
            },
            "playlists": [p.to_dict() for p in self.playlists],
            "missing_tracks": [t.to_dict() for t in self.missing],
        }


_FACTORY_MARKERS = ("/Sampler/", "/Demo Tracks/")


class Library:
    """只读打开 Rekordbox 库。始终操作副本。"""

    def __init__(self, db_path: str | Path | None = None, cache_dir: str | Path = "state/.cache"):
        self.source_path = resolve_db_path(db_path)
        self.cache_dir = Path(cache_dir)
        self._db = None
        self.report: LibraryReport | None = None
        self._copy_path: Path | None = None

    # ------------------------------------------------------------------

    def __enter__(self) -> "Library":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def open(self) -> "Library":
        try:
            from pyrekordbox import MasterDatabase
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise RekordboxError(
                "需要 pyrekordbox 才能读取 Rekordbox 库：\n"
                "  pip install -e .[rekordbox]\n"
                "（也可以 pip install pyrekordbox）"
            ) from exc

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_stale_copies()
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        copy_path = self.cache_dir / f"master_{stamp}.db"
        shutil.copy2(self.source_path, copy_path)
        self._copy_path = copy_path

        self._db = MasterDatabase(path=copy_path)
        self._db.open()
        return self

    def _cleanup_stale_copies(self) -> None:
        """清掉上次没能删除的副本（SQLAlchemy 有时会短暂占用文件句柄）。"""
        if not self.cache_dir.exists():
            return
        for old in self.cache_dir.glob("master_*.db"):
            try:
                old.unlink()
            except OSError:
                pass

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.session.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._db.close()
            except Exception:  # noqa: BLE001 - 关闭失败不该掩盖主流程
                pass
            self._db = None
        gc.collect()  # 让 Windows 释放文件句柄，副本才删得掉
        if self._copy_path and self._copy_path.exists():
            try:
                self._copy_path.unlink()
            except OSError:
                pass
        self._copy_path = None

    # ------------------------------------------------------------------

    @property
    def db(self):
        if self._db is None:
            raise RekordboxError("库还没打开，请先调用 open() 或使用 with 语句")
        return self._db

    def integrity_check(self) -> str:
        import sqlalchemy as sa

        return str(self.db.session.execute(sa.text("PRAGMA integrity_check")).scalar())

    def read_tracks(self) -> list[TrackRow]:
        rows: list[TrackRow] = []
        for content in list(self.db.get_content()):
            path = str(content.FolderPath or "")
            artist = ""
            try:
                artist = content.Artist.Name if content.Artist else ""
            except Exception:  # noqa: BLE001 - 关联缺失不该中断
                artist = ""
            rows.append(
                TrackRow(
                    id=str(content.ID),
                    title=str(content.Title or ""),
                    artist=str(artist or ""),
                    path=path,
                    exists=bool(path) and Path(path).is_file(),
                    is_factory=any(marker in path.replace("\\", "/") for marker in _FACTORY_MARKERS),
                )
            )
        return rows

    def read_playlists(self) -> list[PlaylistRow]:
        rows: list[PlaylistRow] = []
        for playlist in list(self.db.get_playlist()):
            try:
                count = len(list(playlist.Songs or []))
            except Exception:  # noqa: BLE001
                count = 0
            rows.append(
                PlaylistRow(
                    id=str(playlist.ID),
                    name=str(playlist.Name or ""),
                    attribute=int(playlist.Attribute or 0),
                    parent_id=str(playlist.ParentID) if playlist.ParentID else None,
                    song_count=count,
                )
            )
        return rows

    def inspect(self) -> LibraryReport:
        stat = self.source_path.stat()
        report = LibraryReport(
            db_path=self.source_path,
            db_size=stat.st_size,
            db_mtime=datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            working_copy=self._copy_path,
            integrity=self.integrity_check(),
            usn=self.db.get_local_usn(),
            tracks=self.read_tracks(),
            playlists=self.read_playlists(),
        )
        self.report = report
        return report
