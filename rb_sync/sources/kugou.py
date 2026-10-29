"""酷狗音乐歌单源（只读）。

实测（2026-10）：
    GET mobilecdn.kugou.com/api/v3/special/song?specialid=<id>&page=N&pagesize=300
        -> data.total 为总数，data.info[] 为曲目；**必须分页**
           字段：hash(32 位大写) / filename("艺人 - 曲名") / duration(秒) /
                 album_id / album_audio_id / privilege
           注意：没有独立的艺人、专辑字段，只能从 filename 里拆；
                 ``remark`` 不等于专辑名，是曲名备注，不要误用。

不需要登录、不需要签名。
"""

from __future__ import annotations

import html as html_module
import re

from ..model import PlaylistSnapshot, TrackRef
from .base import HttpSource, SourceError

API_SONGS = "http://mobilecdn.kugou.com/api/v3/special/song"

PAGE_SIZE = 300
MAX_PAGES = 40  # 12000 首，防御性上限

# 支持：
#   https://www.kugou.com/yy/special/single/1063852.html
#   https://m.kugou.com/plist/list/1063852?json=true
#   ...&specialid=1063852
#   1063852
_ID_PATTERNS = (
    re.compile(r"/special/single/(\d+)"),
    re.compile(r"/plist/list/(\d+)"),
    re.compile(r"[?&#]specialid=(\d+)"),
    re.compile(r"^(\d+)$"),
)

# 酷狗用全角/顿号分隔多艺人
_ARTIST_SEP = re.compile(r"[、,/&]")


class KugouSource(HttpSource):
    name = "kugou"
    display_name = "酷狗音乐"
    referer = "https://m.kugou.com/"
    url_template = "https://www.kugou.com/yy/special/single/{id}.html"
    host_patterns = (r"kugou\.com",)
    default_headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    # ------------------------------------------------------------- ID

    def resolve_id(self, text: str) -> str:
        raw = text.strip().strip("\"'")
        for pattern in _ID_PATTERNS:
            m = pattern.search(raw)
            if m:
                return m.group(1)
        raise SourceError(
            f"无法从 {text!r} 识别酷狗歌单 ID（请粘贴歌单链接或纯数字 ID）"
        )

    # ------------------------------------------------------------ 抓取

    def fetch(
        self,
        playlist_id: str,
        previous: PlaylistSnapshot | None = None,
        refresh_metadata: bool = False,
    ) -> PlaylistSnapshot:
        ids: list[str] = []
        details: dict[str, TrackRef] = {}
        total = 0

        for page in range(1, MAX_PAGES + 1):
            url = (
                f"{API_SONGS}?specialid={playlist_id}&page={page}"
                f"&pagesize={PAGE_SIZE}&plat=0&version=8000"
            )
            data = self.request_json(url)
            if data.get("errcode") not in (0, None) or data.get("status") not in (1, None):
                raise SourceError(
                    f"读取酷狗歌单 {playlist_id} 失败：errcode={data.get('errcode')} "
                    f"error={data.get('error') or ''}"
                )
            payload = data.get("data") or {}
            total = int(payload.get("total") or 0)
            info = payload.get("info") or []
            if not info:
                break
            for song in info:
                ref = _song_to_track(song)
                if ref is None:
                    continue
                ids.append(ref.id)
                details[ref.id] = ref
            if total and len(ids) >= total:
                break
            if len(info) < PAGE_SIZE:
                break

        if not ids:
            raise SourceError(
                f"酷狗歌单 {playlist_id} 不存在、为空，或为私密歌单"
            )

        # 歌单接口不返回歌单名，只能从分享页 <title> 里取（拿不到就留空）
        name = self._fetch_name(playlist_id)

        return self.assemble(
            str(playlist_id),
            name=name,
            ids=ids,
            fetch_details=lambda missing: {i: details[i] for i in missing if i in details},
            previous=previous,
            refresh_metadata=refresh_metadata,
            track_count=total or len(ids),
        )

    def _fetch_name(self, playlist_id: str) -> str:
        try:
            page = self.request_text(self.url_template.format(id=playlist_id))
        except SourceError:
            return ""
        m = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
        if not m:
            return ""
        title = html_module.unescape(m.group(1)).strip()
        for suffix in ("_精选集_乐库频道_酷狗网", "_精选集_酷狗网", "-酷狗音乐", "_酷狗音乐"):
            title = title.replace(suffix, "")
        return title.strip(" _-|")


def split_filename(filename: str) -> tuple[tuple[str, ...], str]:
    """把酷狗的 ``"艺人1、艺人2 - 曲名"`` 拆成 (艺人元组, 曲名)。

    酷狗没有结构化字段，只能按第一个 ``" - "`` 切开；
    没有分隔符时整串当作曲名，艺人留空。
    """
    text = (filename or "").strip()
    if not text:
        return (), ""
    if " - " in text:
        artist_part, _, title = text.partition(" - ")
    else:
        artist_part, title = "", text
    artists = tuple(
        a.strip() for a in _ARTIST_SEP.split(artist_part) if a.strip()
    )
    return artists, title.strip()


def _song_to_track(song: dict) -> TrackRef | None:
    track_hash = (song.get("hash") or "").strip().upper()
    if not track_hash:
        return None
    artists, title = split_filename(song.get("filename") or "")
    return TrackRef(
        id=track_hash,
        name=title,
        artists=artists,
        album="",  # 酷狗歌单接口不返回专辑名
        duration_ms=int(song.get("duration") or 0) * 1000,
        available=True,
        extra={
            "album_id": song.get("album_id"),
            "album_audio_id": song.get("album_audio_id"),
            "audio_id": song.get("audio_id"),
            "privilege": song.get("privilege"),
        },
    )
