"""整歌单下载 → 挑出真缺的 → 入库 → 清空暂存区。

设计边界（和 ``commands`` / ``post_command`` 一致）：
本模块**不实现任何下载**，只是「按歌单跑一条你配置的外部命令」，
然后把落盘的文件和缺口清单对账，只把真正缺的搬进曲库。

为什么要整歌单模式：多数下载器（如 yun-playlist-downloader）只接受
歌单 / 专辑 / 电台级 URL，没有「给我这一首」的接口。
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .plan import AudioIndex, scan_audio_files

# 整歌单命令支持的占位符
PLAYLIST_PLACEHOLDERS = "{playlist_url} {out} {name} {source} {id}"

# 清空暂存区时保留的文件（cookie 之类的凭据文件）
DEFAULT_KEEP = ("yun.cookie.txt",)

MAX_CAPTURE = 2000


@dataclass
class PlaylistTarget:
    """一个待下载的歌单。"""

    source: str
    playlist_id: str
    name: str
    url: str
    missing: int = 0

    @property
    def key(self) -> str:
        return f"{self.source}:{self.playlist_id}"

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "playlist_id": self.playlist_id,
            "name": self.name,
            "url": self.url,
            "missing": self.missing,
        }


@dataclass
class PlaylistRun:
    target: PlaylistTarget
    command: str
    ok: bool
    returncode: int = -1
    stderr: str = ""
    timed_out: bool = False

    def to_dict(self) -> dict:
        return {
            "playlist": self.target.name,
            "source": self.target.source,
            "command": self.command,
            "ok": self.ok,
            "returncode": self.returncode,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
        }


@dataclass
class IngestResult:
    moved: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    cleared: int = 0
    kept: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "moved": self.moved,
            "failed": self.failed,
            "cleared": self.cleared,
            "kept": self.kept,
        }


# ------------------------------------------------------------------ 目标计算


def playlist_targets(selection_playlists, plan) -> list[PlaylistTarget]:
    """挑出「还有缺失曲目」的歌单。没有缺口的歌单不下载，省流量。"""
    missing_by_playlist: dict[str, int] = {}
    for item in plan.missing:
        for name in item.track.playlists:
            missing_by_playlist[name] = missing_by_playlist.get(name, 0) + 1

    targets: list[PlaylistTarget] = []
    for entry in selection_playlists:
        name = str(entry.get("name") or "")
        count = missing_by_playlist.get(name, 0)
        if count <= 0:
            continue
        targets.append(
            PlaylistTarget(
                source=str(entry.get("source") or ""),
                playlist_id=str(entry.get("id") or ""),
                name=name,
                url=str(entry.get("url") or ""),
                missing=count,
            )
        )
    return targets


def render_playlist_command(template: str, target: PlaylistTarget, out_dir: str = "") -> str:
    mapping = {
        "playlist_url": target.url,
        "name": target.name,
        "source": target.source,
        "id": target.playlist_id,
        "out": out_dir,
    }
    text = template
    for key, value in mapping.items():
        text = text.replace("{" + key + "}", str(value))
    return text


# ------------------------------------------------------------------ 执行命令


def run_playlist_command(
    template: str,
    target: PlaylistTarget,
    cwd: str | Path,
    out_dir: str = "",
    timeout: float = 3600.0,
) -> PlaylistRun:
    """在暂存目录里执行一次整歌单下载。

    ``cwd`` 很重要：多数下载器没有输出目录参数，只会往当前目录写。
    """
    command = render_playlist_command(template, target, out_dir)
    Path(cwd).mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return PlaylistRun(
            target=target,
            command=command,
            ok=completed.returncode == 0,
            returncode=completed.returncode,
            stderr=((completed.stderr or "") + (completed.stdout or ""))[-MAX_CAPTURE:],
        )
    except subprocess.TimeoutExpired:
        return PlaylistRun(
            target=target,
            command=command,
            ok=False,
            stderr=f"命令超过 {timeout:.0f}s 未结束",
            timed_out=True,
        )
    except OSError as exc:
        return PlaylistRun(
            target=target,
            command=command,
            ok=False,
            stderr=f"无法启动命令：{exc}",
        )


# ------------------------------------------------------------------ 入库


def _move_with_retry(src: Path, dst: Path, attempts: int = 3, delay: float = 0.6) -> tuple[bool, str]:
    """搬文件，带重试。

    刚下载完的大文件常被杀软 / 索引器短暂锁住（WinError 5），
    一次失败不代表永久失败。
    """
    last_error = ""
    for index in range(attempts):
        try:
            shutil.move(str(src), str(dst))
            return True, ""
        except OSError as exc:  # 含 PermissionError
            last_error = str(exc)
            # 失败的拷贝可能留下半个文件，清掉再试
            try:
                if dst.exists() and src.exists():
                    dst.unlink()
            except OSError:
                pass
            time.sleep(delay * (index + 1))
    return False, last_error


def harvest(
    inbox: str | Path,
    dest: str | Path,
    missing_items,
    attempts: int = 3,
) -> IngestResult:
    """把暂存区里「确实缺」的文件搬进曲库，其余留在原地等清理。"""
    inbox_path = Path(inbox)
    dest_path = Path(dest)
    dest_path.mkdir(parents=True, exist_ok=True)

    result = IngestResult()
    if not inbox_path.is_dir():
        return result

    index = AudioIndex.from_roots([str(inbox_path)])
    claimed: set[str] = set()

    for item in missing_items:
        match = index.lookup(item.track)
        if not match.file:
            continue
        if match.encrypted:
            result.failed.append(
                {"title": item.track.title, "reason": "只有加密容器，需要先解密"}
            )
            continue
        src = Path(match.file.path)
        if str(src) in claimed:
            continue
        claimed.add(str(src))
        target = dest_path / src.name
        if target.exists():
            result.failed.append(
                {"title": item.track.title, "reason": f"目标已存在，未覆盖：{target.name}"}
            )
            continue
        ok, error = _move_with_retry(src, target, attempts=attempts)
        if ok:
            try:
                size = target.stat().st_size
            except OSError:
                size = 0
            result.moved.append(
                {"title": item.track.title, "file": target.name, "size": size}
            )
        else:
            result.failed.append({"title": item.track.title, "reason": error})
    return result


def clear_inbox(inbox: str | Path, keep: tuple[str, ...] = DEFAULT_KEEP) -> IngestResult:
    """清空暂存区，只保留 cookie 之类的凭据文件。"""
    inbox_path = Path(inbox)
    result = IngestResult()
    if not inbox_path.is_dir():
        return result

    keep_lower = {name.lower() for name in keep}
    # 深的先删，方便之后删空目录
    paths = sorted(inbox_path.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    for path in paths:
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
            continue
        name = path.name.lower()
        if name in keep_lower or (name.endswith(".txt") and "cookie" in name):
            result.kept.append(str(path.relative_to(inbox_path)))
            continue
        try:
            path.unlink()
            result.cleared += 1
        except OSError:
            pass
    return result


def inbox_audio_count(inbox: str | Path) -> int:
    return len(scan_audio_files([str(inbox)])) if Path(inbox).is_dir() else 0


def is_credentials_file(name: str) -> bool:
    low = name.lower()
    return low in {k.lower() for k in DEFAULT_KEEP} or (low.endswith(".txt") and "cookie" in low)


__all__ = [
    "DEFAULT_KEEP",
    "PLAYLIST_PLACEHOLDERS",
    "IngestResult",
    "PlaylistRun",
    "PlaylistTarget",
    "clear_inbox",
    "harvest",
    "inbox_audio_count",
    "is_credentials_file",
    "playlist_targets",
    "render_playlist_command",
    "run_playlist_command",
]
