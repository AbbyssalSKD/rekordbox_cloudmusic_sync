"""网易云音乐歌单源（只读）。

实测（2026-10）：
    GET /api/v6/playlist/detail?id=<id>
        -> playlist.trackIds 完整且有序；updateTime / trackUpdateTime / trackCount
    POST /api/v3/song/detail  c=[{"id":..}]
        -> 批量元数据（name / ar / al / dt）
    ``/api/v6/playlist/track/all`` 与 ``/api/v6/song/detail`` 已 404，勿用。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Iterable

from ..model import PlaylistSnapshot, TrackRef
from .base import HttpSource, SourceError

API_DETAIL = "https://music.163.com/api/v6/playlist/detail"
API_SONG_DETAIL = "https://music.163.com/api/v3/song/detail"

SONG_BATCH = 200

_ID_PATTERNS = (
    re.compile(r"[?&#]id=(\d+)"),
    re.compile(r"/playlist/(\d+)"),
    re.compile(r"^(\d+)$"),
)


class NeteaseSource(HttpSource):
    name = "netease"
    display_name = "网易云音乐"
    referer = "https://music.163.com/"
    url_template = "https://music.163.com/playlist?id={id}"
    host_patterns = (r"music\.163\.com", r"163cn\.tv")
    default_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
        # os=pc 让接口返回 PC 端结构（ar/al/dt 字段名）
        "Cookie": "os=pc; appver=8.9.70",
    }

    # ------------------------------------------------------------- ID

    def resolve_id(self, text: str) -> str:
        raw = text.strip().strip("\"'")
        for pattern in _ID_PATTERNS:
            m = pattern.search(raw)
            if m:
                return m.group(1)
        if "http" in raw:
            return self._resolve_from_redirect(raw)
        raise SourceError(
            f"无法从 {text!r} 识别网易云歌单 ID（请粘贴歌单链接或纯数字 ID）"
        )

    def _resolve_from_redirect(self, url: str) -> str:
        # 短链（163cn.tv）跟随跳转，再从最终地址/页面里找 ID
        final_url = url
        body = ""
        try:
            req = urllib.request.Request(url, headers=self.headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                final_url = resp.geturl()
                body = resp.read(200_000).decode("utf-8", "replace")
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            raise SourceError(f"打开分享链接失败（{exc}）：{url}") from exc
        for candidate in (final_url, body):
            for pattern in _ID_PATTERNS:
                m = pattern.search(candidate)
                if m:
                    return m.group(1)
        raise SourceError(f"分享链接里找不到歌单 ID：{url}")

    # ------------------------------------------------------------ 抓取

    def fetch(
        self,
        playlist_id: str,
        previous: PlaylistSnapshot | None = None,
        refresh_metadata: bool = False,
    ) -> PlaylistSnapshot:
        detail = self.request_json(f"{API_DETAIL}?id={playlist_id}")
        if detail.get("code") != 200:
            raise SourceError(
                f"读取歌单 {playlist_id} 失败：code={detail.get('code')} "
                f"msg={detail.get('msg') or detail.get('message') or ''}"
            )
        playlist = detail.get("playlist")
        if not playlist:
            raise SourceError(
                f"歌单 {playlist_id} 不存在、已删除，或为私密歌单（需配置 Cookie）"
            )

        ids = [str(item["id"]) for item in playlist.get("trackIds") or () if "id" in item]
        banned = {str(i) for i in playlist.get("bannedTrackIds") or ()}
        creator = playlist.get("creator") or {}

        return self.assemble(
            str(playlist.get("id") or playlist_id),
            name=playlist.get("name") or "",
            ids=ids,
            fetch_details=self.fetch_track_details,
            previous=previous,
            refresh_metadata=refresh_metadata,
            unavailable=banned,
            track_count=int(playlist.get("trackCount") or len(ids)),
            update_time=int(playlist.get("updateTime") or 0),
            track_update_time=int(playlist.get("trackUpdateTime") or 0),
            ordered=bool(playlist.get("ordered", True)),
            privacy=int(playlist.get("privacy") or 0),
            creator=(creator.get("nickname") or "") if isinstance(creator, dict) else "",
        )

    def fetch_track_details(self, track_ids: Iterable[str]) -> dict[str, TrackRef]:
        """批量取曲目元数据。缺失的 ID 不会出现在返回里（多为下架）。"""
        ids = [str(i) for i in track_ids]
        out: dict[str, TrackRef] = {}
        for start in range(0, len(ids), SONG_BATCH):
            batch = ids[start : start + SONG_BATCH]
            payload = json.dumps([{"id": int(i)} for i in batch], separators=(",", ":"))
            data = self.request_json(API_SONG_DETAIL, form={"c": payload})
            if data.get("code") != 200:
                raise SourceError(f"批量取曲目元数据失败：code={data.get('code')}")
            for song in data.get("songs") or ():
                ref = _song_to_track(song)
                if ref is not None:
                    out[ref.id] = ref
        return out


def _song_to_track(song: dict) -> TrackRef | None:
    try:
        song_id = str(int(song["id"]))
    except (KeyError, TypeError, ValueError):
        return None
    artists = song.get("ar") or song.get("artists") or ()
    album = song.get("al") or song.get("album") or {}
    album_name = ""
    if isinstance(album, dict):
        album_name = album.get("name") or ""
    elif isinstance(album, str):
        album_name = album
    return TrackRef(
        id=song_id,
        name=(song.get("name") or "").strip(),
        artists=tuple((a.get("name") or "").strip() for a in artists if isinstance(a, dict)),
        album=(album_name or "").strip(),
        duration_ms=int(song.get("dt") or song.get("duration") or 0),
        available=True,
        extra={"fee": song.get("fee")},
    )
