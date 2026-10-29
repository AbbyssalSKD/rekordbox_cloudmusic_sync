"""本地文件歌单源：从 CSV / JSON 读取一份曲目清单。

用途：
- 任何平台都能导出成文本，API 变更时用它兜底；
- 手搓/整理的歌单（例如「这周要放的 20 首」）；
- 测试与离线调试（不联网）。

CSV 表头（大小写不敏感，中英文别名都认）：
    title,artist,album,duration_ms     # 也认 name/歌曲名、artists/歌手、时长
可选的 id 列；没有 id 时用「曲名+艺人」的哈希生成稳定 ID。

JSON 两种形态：
    [{"title": "...", "artist": "...", ...}, ...]
    {"name": "歌单名", "tracks": [{...}, ...]}
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path

from ..model import PlaylistSnapshot, TrackRef
from .base import Source, SourceError, now_iso

_TITLE_KEYS = ("title", "name", "song", "songname", "曲名", "歌曲名", "歌名")
_ARTIST_KEYS = ("artist", "artists", "singer", "singers", "艺人", "歌手")
_ALBUM_KEYS = ("album", "albumname", "专辑")
_DURATION_MS_KEYS = ("duration_ms", "durationms", "时长毫秒")
_DURATION_S_KEYS = ("duration", "interval", "时长", "时长秒")
_ID_KEYS = ("id", "songid", "songmid", "hash", "track_id")

_LOCAL_SUFFIXES = {".csv", ".json", ".txt"}


def looks_local(text: str) -> bool:
    """判断输入是不是一个本地文件（而不是链接或平台 ID）。"""
    raw = text.strip().strip("\"'")
    if raw.startswith(("http://", "https://")):
        return False
    path = Path(raw)
    if path.suffix.lower() in _LOCAL_SUFFIXES:
        return True
    return path.is_file()


def _pick(row: dict, keys: tuple[str, ...]) -> str:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for key in keys:
        value = lowered.get(key)
        if value in (None, ""):
            continue
        # JSON 里艺人常常写成数组
        if isinstance(value, (list, tuple)):
            return "、".join(str(v).strip() for v in value if str(v).strip())
        return str(value).strip()
    return ""


def _split_artists(text: str) -> tuple[str, ...]:
    if not text:
        return ()
    return tuple(part.strip() for part in re.split(r"[、,;/&]", text) if part.strip())


def _stable_id(title: str, artists: str) -> str:
    raw = f"{title.strip().lower()}|{artists.strip().lower()}".encode("utf-8")
    return "local-" + hashlib.sha1(raw).hexdigest()[:16]


def _playlist_key(stem: str, parent: str) -> str:
    """歌单级稳定键：同一文件在不同次运行里得到同一个 ID。"""
    raw = f"{stem.strip().lower()}|{parent.strip().lower()}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


class LocalSource(Source):
    name = "local"
    display_name = "本地文件"
    host_patterns: tuple[str, ...] = ()

    def __init__(self) -> None:
        # resolve_id 把「稳定 ID」映射到真实路径，供同一次运行内的 fetch 使用
        self._paths: dict[str, Path] = {}

    def playlist_url(self, playlist_id: str) -> str:
        path = self._paths.get(playlist_id)
        return path.as_uri() if path else ""

    def resolve_id(self, text: str) -> str:
        path = Path(text.strip().strip("\"'")).expanduser()
        if not path.is_file():
            raise SourceError(f"本地文件不存在：{path}")
        if path.suffix.lower() not in _LOCAL_SUFFIXES:
            raise SourceError(
                f"不支持的本地文件类型 {path.suffix}（目前支持 .csv / .json）"
            )
        resolved = path.resolve()
        key = _playlist_key(resolved.stem, str(resolved.parent))
        self._paths[key] = resolved
        return key

    def fetch(
        self,
        playlist_id: str,
        previous: PlaylistSnapshot | None = None,
        refresh_metadata: bool = False,
    ) -> PlaylistSnapshot:
        path = self._paths.get(playlist_id)
        if path is None:
            raise SourceError(
                "本地文件源需要先用完整路径调用一次 resolve_id（这是内部调用顺序问题）"
            )
        rows, playlist_name = _read_file(path)

        tracks: list[TrackRef] = []
        for row in rows:
            title = _pick(row, _TITLE_KEYS)
            artists_text = _pick(row, _ARTIST_KEYS)
            album = _pick(row, _ALBUM_KEYS)
            duration_ms = _pick(row, _DURATION_MS_KEYS)
            duration_s = _pick(row, _DURATION_S_KEYS)
            if not title and not artists_text:
                continue
            try:
                ms = int(float(duration_ms)) if duration_ms else 0
            except ValueError:
                ms = 0
            if not ms and duration_s:
                try:
                    ms = int(float(duration_s) * 1000)
                except ValueError:
                    ms = 0
            tracks.append(
                TrackRef(
                    id=_pick(row, _ID_KEYS) or _stable_id(title, artists_text),
                    name=title,
                    artists=_split_artists(artists_text),
                    album=album,
                    duration_ms=ms,
                )
            )

        if not tracks:
            raise SourceError(f"{path.name} 里没有解析出任何曲目（检查表头/字段名）")

        from .base import content_hash

        return PlaylistSnapshot(
            source=self.name,
            playlist_id=playlist_id,
            name=playlist_name or path.stem,
            url=path.as_uri(),
            tracks=tracks,
            track_count=len(tracks),
            fetched_at=now_iso(),
            content_hash=content_hash([t.id for t in tracks]),
        )


def _read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise SourceError(f"{path.name} 编码无法识别（请另存为 UTF-8）")


def _read_file(path: Path) -> tuple[list[dict], str]:
    text = _read_text(path)
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            rows = data.get("tracks") or data.get("songs") or data.get("playlist") or []
            return list(rows), str(data.get("name") or "")
        if isinstance(data, list):
            return data, ""
        raise SourceError(f"{path.name} 的 JSON 结构无法识别")
    # CSV / TXT
    reader = csv.DictReader(io.StringIO(text))
    return [row for row in reader], ""
