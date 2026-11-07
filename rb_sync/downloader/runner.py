"""外部下载命令的适配器（不实现下载本身）。

用户为每个平台配置一条命令模板，例如::

    "fetch": {
      "commands": {
        "netease": "yun \\"{url}\\" -q 320 -o \\"{out}\\""
      },
      "out_dir": "F:\\\\Music\\\\Cloud Music\\\\Sync"
    }

可用占位符：``{url} {id} {name} {artists} {album} {source} {uid} {playlists} {out}``

项目本身不下载、不解密任何内容；没配置命令时，fetch 只会生成清单（handoff 模式）。
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

MAX_CAPTURE = 2000  # 每条命令最多保留多少字符的输出


@dataclass
class RunResult:
    uid: str
    command: str
    ok: bool
    returncode: int = -1
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    timed_out: bool = False

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "command": self.command,
            "ok": self.ok,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration": round(self.duration, 2),
            "timed_out": self.timed_out,
        }


def render_command(template: str, item, out_dir: str = "") -> str:
    """把清单项填进命令模板。``item`` 是 downloader.plan.FetchItem。"""
    track = item.track
    mapping = {
        "url": track_url_safe(track.source, track.track_id),
        "id": track.track_id,
        "name": track.title,
        "artists": track.artist_text,
        "album": track.album,
        "source": track.source,
        "uid": track.uid,
        "playlists": "、".join(track.playlists),
        "out": out_dir,
    }
    text = template
    for key, value in mapping.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def track_url_safe(source: str, track_id: str) -> str:
    from .plan import track_url

    return track_url(source, track_id)


@dataclass
class RunSummary:
    results: list[RunResult] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> list[RunResult]:
        return [r for r in self.results if not r.ok]

    def to_dict(self) -> dict:
        return {
            "counts": {
                "total": len(self.results),
                "ok": self.ok_count,
                "failed": len(self.failed),
            },
            "results": [r.to_dict() for r in self.results],
        }


def run_items(
    template: str,
    items,
    out_dir: str = "",
    timeout: float = 600.0,
    on_progress=None,
) -> RunSummary:
    """逐条执行外部命令。串行执行，避免把用户机器打满。"""
    summary = RunSummary()
    base = Path(out_dir) if out_dir else None
    if base is not None:
        base.mkdir(parents=True, exist_ok=True)

    for index, item in enumerate(items, start=1):
        command = render_command(template, item, out_dir)
        if on_progress:
            on_progress(index, len(items), item, command)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                # 外部工具的输出编码不可控：中文 Windows 默认 cp936，但很多工具写 UTF-8。
                # 不显式指定就会在读取线程里抛 UnicodeDecodeError（真实踩过）。
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
            result = RunResult(
                uid=item.uid,
                command=command,
                ok=completed.returncode == 0,
                returncode=completed.returncode,
                stdout=(completed.stdout or "")[-MAX_CAPTURE:],
                stderr=(completed.stderr or "")[-MAX_CAPTURE:],
                duration=time.monotonic() - started,
            )
        except subprocess.TimeoutExpired:
            result = RunResult(
                uid=item.uid,
                command=command,
                ok=False,
                stderr=f"命令超过 {timeout:.0f}s 未结束",
                duration=time.monotonic() - started,
                timed_out=True,
            )
        except OSError as exc:
            result = RunResult(
                uid=item.uid,
                command=command,
                ok=False,
                stderr=f"无法启动命令：{exc}",
                duration=time.monotonic() - started,
            )
        summary.results.append(result)

    return summary
