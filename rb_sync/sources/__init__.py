"""源注册表 + 自动识别。

``detect`` 只按链接域名/本地文件判断 —— 三个平台的 ID 都是纯数字，
互相之间有歧义，所以**裸数字必须显式指定 --source**。
"""

from __future__ import annotations

from .base import HttpSource, Source, SourceError, content_hash, matches_any, now_iso
from .kugou import KugouSource, split_filename
from .local import LocalSource, looks_local
from .netease import NeteaseSource
from .qq import QqSource

HTTP_SOURCES: dict[str, type[HttpSource]] = {
    "netease": NeteaseSource,
    "qq": QqSource,
    "kugou": KugouSource,
}

ALL_SOURCE_NAMES = [*HTTP_SOURCES, "local"]

# 展示用
DISPLAY_NAMES = {
    "netease": "网易云音乐",
    "qq": "QQ音乐",
    "kugou": "酷狗音乐",
    "local": "本地文件",
}


def source_names() -> list[str]:
    return list(ALL_SOURCE_NAMES)


def display_name(name: str) -> str:
    return DISPLAY_NAMES.get(name, name)


def build_source(
    name: str,
    cookie: str = "",
    timeout: float = 25.0,
    interval: float | None = None,
) -> Source:
    if name == "local":
        return LocalSource()
    try:
        cls = HTTP_SOURCES[name]
    except KeyError:
        raise SourceError(
            f"未知的源 {name!r}，可选：{'、'.join(source_names())}"
        ) from None
    return cls(cookie=cookie, timeout=timeout, interval=interval)


def detect(text: str) -> str | None:
    """按链接域名或本地文件判断源；识别不出返回 None。"""
    raw = (text or "").strip().strip("\"'")
    if not raw:
        return None
    if looks_local(raw):
        return "local"
    if not raw.startswith(("http://", "https://")):
        return None
    for name, cls in HTTP_SOURCES.items():
        if matches_any(raw, cls.host_patterns):
            return name
    return None


__all__ = [
    "ALL_SOURCE_NAMES",
    "HTTP_SOURCES",
    "LocalSource",
    "Source",
    "SourceError",
    "build_source",
    "content_hash",
    "detect",
    "display_name",
    "looks_local",
    "matches_any",
    "now_iso",
    "source_names",
    "split_filename",
]
