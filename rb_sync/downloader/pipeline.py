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
from types import SimpleNamespace

from ..model import ENCRYPTED_SUFFIXES
from .plan import AudioIndex, SelectionTrack, scan_audio_files
from .runner import run_post

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


def adopt_decrypted(
    inbox: str | Path,
    dest: str | Path,
    before: set[str],
) -> IngestResult:
    """把解密产出的文件搬进曲库（包括对不上任何缺失曲目的那些）。

    解密是用户主动触发的（容器是他自己放进来的），所以产物不该被清理掉；
    但如果曲库里已有同名文件，就不覆盖，留着让人看。
    """
    inbox_path = Path(inbox)
    dest_path = Path(dest)
    result = IngestResult()
    if not inbox_path.is_dir():
        return result
    dest_path.mkdir(parents=True, exist_ok=True)

    for audio in scan_audio_files([str(inbox_path)]):
        if audio.encrypted or str(audio.path) in before:
            continue
        src = Path(audio.path)
        target = dest_path / src.name
        if target.exists():
            result.failed.append(
                {"title": src.stem, "reason": f"曲库已有同名文件，未覆盖：{target.name}"}
            )
            continue
        ok, error = _move_with_retry(src, target)
        if ok:
            try:
                size = target.stat().st_size
            except OSError:
                size = 0
            result.moved.append({"title": src.stem, "file": target.name, "size": size})
        else:
            result.failed.append({"title": src.stem, "reason": error})
    return result


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


def clear_inbox(
    inbox: str | Path,
    keep: tuple[str, ...] = DEFAULT_KEEP,
    protect: tuple[str, ...] = (),
) -> IngestResult:
    """清空暂存区，只保留 cookie 之类的凭据文件。

    ``protect`` 里是绝对路径：**解密失败或没能处理的文件必须保留**，
    宁可让暂存区不干净，也不能把还没处理过的文件删掉。
    """
    inbox_path = Path(inbox)
    result = IngestResult()
    if not inbox_path.is_dir():
        return result

    keep_lower = {name.lower() for name in keep}
    protected = {str(Path(p).resolve()).lower() for p in protect}
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
        if str(path.resolve()).lower() in protected:
            result.kept.append(str(path.relative_to(inbox_path)))
            continue
        try:
            path.unlink()
            result.cleared += 1
        except OSError:
            pass
    return result


# ------------------------------------------------------------------ 解密


def encrypted_files(inbox: str | Path) -> list[Path]:
    """暂存区里还没解开的加密容器。"""
    if not Path(inbox).is_dir():
        return []
    return [Path(f.path) for f in scan_audio_files([str(inbox)]) if f.encrypted]


def _shim_item(path: Path):
    """给"对不上任何缺失曲目"的加密文件造一个最小上下文，供模板渲染。"""
    track = SelectionTrack(source="", track_id=path.stem, title=path.stem)
    return SimpleNamespace(track=track, matched_path=str(path), uid="")


def decrypt_inbox(
    post_template: str,
    inbox: str | Path,
    dest: str | Path,
    missing_items,
    timeout: float = 600.0,
    on_progress=None,
) -> dict:
    """对暂存区里的加密容器跑用户配置的后处理命令。

    解出来的文件**先落在暂存区**，再由 harvest 统一挑进曲库 —— 这样
    「只有确实缺的才进库」这条约束不会被解密环节绕过去。

    解开成功 → 删掉原容器（内容已经变成可播放文件）；
    失败或没配命令 → **保留**，由调用方在清理时保护起来。
    """
    files = encrypted_files(inbox)
    if not files:
        return {"configured": bool(post_template), "results": [], "removed": [], "failed": []}
    if not post_template:
        return {
            "configured": False,
            "results": [],
            "removed": [],
            "failed": [str(p) for p in files],
        }

    index = AudioIndex.from_roots([str(inbox)])
    items = []
    for path in files:
        # 缺失项本身没有路径（missing 就是说"磁盘上找不到"），所以这里必须
        # 用真实文件路径重建上下文；只借用它的曲目元数据，让模板里的
        # {name}/{artists} 之类能渲染出来。
        owner = None
        for candidate in missing_items:
            match = index.lookup(candidate.track)
            if match.file and match.file.path == str(path):
                owner = candidate
                break
        shim = _shim_item(path)
        if owner is not None:
            shim.track = owner.track
            shim.uid = owner.uid
        items.append(shim)

    results = run_post(
        post_template, items, out_dir=str(inbox), timeout=timeout, on_progress=on_progress
    )

    removed: list[str] = []
    failed: list[str] = []
    for item, result in zip(items, results):
        src = Path(item.matched_path or "")
        if result.ok:
            try:
                src.unlink()
                removed.append(str(src))
            except OSError:
                pass
        else:
            failed.append(str(src))
    return {
        "configured": True,
        "results": [r.to_dict() for r in results],
        "removed": removed,
        "failed": failed,
    }


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
    "decrypt_inbox",
    "encrypted_files",
    "harvest",
    "inbox_audio_count",
    "is_credentials_file",
    "playlist_targets",
    "render_playlist_command",
    "run_playlist_command",
]
