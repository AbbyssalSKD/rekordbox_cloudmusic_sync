"""跨平台曲目匹配：同一首歌在不同平台 ID 不同，靠「曲名 + 艺人」归一化对齐。

用途（下一步 MVP-2 会真正用上）：
- 网易云下的歌，QQ/酷狗歌单里出现同一首时不要重复入库；
- 你的 Rekordbox collection 里已有一份，另一平台歌单又要求它时能识别出「已有」。
"""

from __future__ import annotations

import re
import unicodedata

# 只剥离「非语义」标记（官方/MV/歌词/高清…）。
# Remix、Live、Acoustic 等是**不同录音**，必须保留 —— 否则会把现场版和
# 原版错误合并，写进 Rekordbox 就是错歌。
_NOISE_BRACKETS = re.compile(
    r"[\(\（\[\【]\s*[^\)\）\]\】]{0,30}?"
    r"(?:official|music\s*video|\bmv\b|lyric|歌词|高清|无损|试听|完整版|官方)"
    r"[^\)\）\]\】]{0,30}?[\)\）\]\】]",
    re.IGNORECASE,
)
# feat. / ft. 后面的客串艺人
_FEAT = re.compile(r"\b(?:feat\.?|ft\.?|featuring)\b.*$", re.IGNORECASE)
_PUNCT = re.compile(r"[\s\-_·・,，.。!！?？'\"“”‘’()（）\[\]【】/\\+&、~]+")
_MULTI_SEP = re.compile(r"[、,;/&]")


def normalize(text: str) -> str:
    """统一全半角、大小写、标点，用于比较而非展示。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).lower()
    text = _NOISE_BRACKETS.sub("", text)
    text = _FEAT.sub("", text)
    text = _PUNCT.sub("", text)
    return text.strip()


def split_artists(artists: str | tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(artists, str):
        return tuple(a.strip() for a in _MULTI_SEP.split(artists) if a.strip())
    return tuple(a for a in artists if a)


def track_key(title: str, artists: str | tuple[str, ...]) -> str:
    """跨平台对齐用的键：``归一化曲名|归一化主艺人``。

    只取第一个艺人 —— 客串顺序在各平台差异很大。
    """
    artist_list = split_artists(artists)
    primary = normalize(artist_list[0]) if artist_list else ""
    return f"{normalize(title)}|{primary}"


def duration_bucket(duration_ms: int, bucket_ms: int = 2000) -> int:
    """时长分桶，用来给 ``track_key`` 兜底消歧（同名不同版本）。"""
    if duration_ms <= 0:
        return -1
    return round(duration_ms / bucket_ms)


def same_track(
    a_title: str,
    a_artists: str | tuple[str, ...],
    a_duration_ms: int,
    b_title: str,
    b_artists: str | tuple[str, ...],
    b_duration_ms: int,
    tolerance_ms: int = 5000,
) -> bool:
    if track_key(a_title, a_artists) != track_key(b_title, b_artists):
        return False
    if a_duration_ms <= 0 or b_duration_ms <= 0:
        return True
    return abs(a_duration_ms - b_duration_ms) <= tolerance_ms
