"""领域模型：源无关的曲目与歌单表示。

这里的结构就是中间产物（快照 JSON / 选择配置 JSON）的 schema，
下游 fetch / convert / write 阶段只依赖这些字段，不依赖任何平台的原始返回。

重要：``TrackRef.id`` 一律是**字符串**。三个平台的 ID 类型并不一致
（网易云是数字、QQ 是 songmid、酷狗是 32 位 hash），统一成字符串后
下游不需要做任何类型判断。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class TrackRef:
    """一首歌的最小可用标识。

    ``id`` 是**源侧** ID，只在 ``source`` 语境下唯一。跨源全局唯一键用
    ``uid(source)``（形如 ``netease:287398`` / ``qq:002xTzGb2UBQRk``）。
    """

    id: str
    name: str = ""
    artists: tuple[str, ...] = ()
    album: str = ""
    duration_ms: int = 0
    available: bool = True
    # 各平台特有但后续阶段可能用到的字段（例如酷狗下载需要的 album_audio_id）
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    def uid(self, source: str) -> str:
        return f"{source}:{self.id}"

    @property
    def artist_text(self) -> str:
        return " / ".join(a for a in self.artists if a)

    @property
    def display(self) -> str:
        artist = self.artist_text or "<未知艺人>"
        return f"{self.name or '<未知曲名>'} - {artist}"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": str(self.id),
            "name": self.name,
            "artists": list(self.artists),
            "album": self.album,
            "duration_ms": self.duration_ms,
            "available": self.available,
        }
        if self.extra:
            out["extra"] = self.extra
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrackRef":
        return cls(
            id=str(data["id"]),
            name=data.get("name") or "",
            artists=tuple(data.get("artists") or ()),
            album=data.get("album") or "",
            duration_ms=int(data.get("duration_ms") or 0),
            available=bool(data.get("available", True)),
            extra=dict(data.get("extra") or {}),
        )


@dataclass
class PlaylistSnapshot:
    """某个歌单在某一时刻的完整成员关系。

    ``content_hash`` 是对「有序成员 ID 列表」的摘要，用来跨平台统一判断
    「成员有没有变」——不依赖各平台是否提供变更时间戳。
    """

    source: str
    playlist_id: str
    name: str
    tracks: list[TrackRef] = field(default_factory=list)
    track_count: int = 0
    update_time: int = 0
    track_update_time: int = 0
    ordered: bool = True
    privacy: int = 0
    creator: str = ""
    fetched_at: str = ""
    content_hash: str = ""
    url: str = ""

    @property
    def track_ids(self) -> list[str]:
        return [t.id for t in self.tracks]

    def metadata_by_id(self) -> dict[str, TrackRef]:
        return {t.id: t for t in self.tracks}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "source": self.source,
            "playlist_id": str(self.playlist_id),
            "name": self.name,
            "url": self.url,
            "track_count": self.track_count,
            "update_time": self.update_time,
            "track_update_time": self.track_update_time,
            "ordered": self.ordered,
            "privacy": self.privacy,
            "creator": self.creator,
            "fetched_at": self.fetched_at,
            "content_hash": self.content_hash,
            "tracks": [t.to_dict() for t in self.tracks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlaylistSnapshot":
        return cls(
            source=data.get("source") or "",
            playlist_id=str(data["playlist_id"]),
            name=data.get("name") or "",
            tracks=[TrackRef.from_dict(t) for t in data.get("tracks") or ()],
            track_count=int(data.get("track_count") or 0),
            update_time=int(data.get("update_time") or 0),
            track_update_time=int(data.get("track_update_time") or 0),
            ordered=bool(data.get("ordered", True)),
            privacy=int(data.get("privacy") or 0),
            creator=data.get("creator") or "",
            fetched_at=data.get("fetched_at") or "",
            content_hash=data.get("content_hash") or "",
            url=data.get("url") or "",
        )


@dataclass
class Selection:
    """选择配置：下游阶段唯一的事实来源（禁止硬编码歌单）。"""

    playlists: list[dict[str, Any]] = field(default_factory=list)
    generated_at: str = ""

    def playlist_keys(self) -> list[tuple[str, str]]:
        return [(str(p["source"]), str(p["id"])) for p in self.playlists]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "playlists": self.playlists,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Selection":
        return cls(
            playlists=list(data.get("playlists") or ()),
            generated_at=data.get("generated_at") or "",
        )


def dump_json(path, payload: dict[str, Any]) -> None:
    """统一写盘格式：UTF-8、2 空格缩进、保留中文、末尾换行。"""
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_json(path) -> dict[str, Any] | None:
    from pathlib import Path

    p = Path(path)
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def ids_of(tracks: Iterable[TrackRef]) -> list[str]:
    return [t.id for t in tracks]
