"""QQ 音乐歌单源（只读）。

实测（2026-10）：
    GET c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg?disstid=<id>
        -> cdlist[0].songlist **一次返回全部成员**（实测 1286 首也完整返回）
           cdlist[0].mtime 是歌单修改时间；songlist 内含 songmid / songname /
           singer[].name / albumname / interval（秒）

不需要登录、不需要签名，只需要 Referer: https://y.qq.com/。
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request

from ..model import PlaylistSnapshot, TrackRef
from .base import HttpSource, SourceError

API_DETAIL = "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg"

# 支持：
#   https://y.qq.com/n/ryqq/playlist/7707261125
#   https://i.y.qq.com/n2/m/share/details/taoge.html?id=7707261125
#   ...&disstid=7707261125
#   7707261125
_ID_PATTERNS = (
    re.compile(r"/playlist/(\d+)"),
    re.compile(r"[?&](?:id|disstid)=(\d+)"),
    re.compile(r"^(\d+)$"),
)


class QqSource(HttpSource):
    name = "qq"
    display_name = "QQ音乐"
    referer = "https://y.qq.com/"
    url_template = "https://y.qq.com/n/ryqq/playlist/{id}"
    host_patterns = (r"y\.qq\.com", r"i\.y\.qq\.com", r"qq\.com/n/ryqq/playlist")

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
            f"无法从 {text!r} 识别 QQ 音乐歌单 ID（请粘贴歌单链接或纯数字 ID）"
        )

    def _resolve_from_redirect(self, url: str) -> str:
        try:
            req = urllib.request.Request(url, headers=self.headers)
            body = ""
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
        url = (
            f"{API_DETAIL}?type=1&json=1&utf8=1&onlysong=0&disstid={playlist_id}"
            "&format=json&g_tk=5381&loginUin=0&hostUin=0&inCharset=utf8"
            "&outCharset=utf-8&notice=0&platform=yqq.json&needNewCode=0"
        )
        data = self.request_json(url)
        if data.get("code") != 0:
            raise SourceError(
                f"读取 QQ 音乐歌单 {playlist_id} 失败：code={data.get('code')} "
                f"subcode={data.get('subcode')} msg={data.get('message') or ''}"
            )
        cdlist = data.get("cdlist") or []
        if not cdlist:
            raise SourceError(
                f"QQ 音乐歌单 {playlist_id} 不存在、已删除，或为私密歌单（需配置 Cookie）"
            )
        cd = cdlist[0]
        songs = cd.get("songlist") or []
        total = int(cd.get("total_song_num") or len(songs))

        # 一次返回全量；万一服务端截断，明确报错而不是静默丢歌
        if total > len(songs):
            raise SourceError(
                f"QQ 音乐歌单 {playlist_id} 共 {total} 首但只返回了 {len(songs)} 首，"
                "接口可能已变更分页行为，请反馈此问题"
            )

        # 元数据随成员列表一并返回，直接构造，不额外发请求
        details: dict[str, TrackRef] = {}
        ids: list[str] = []
        for song in songs:
            ref = _song_to_track(song)
            if ref is None:
                continue
            ids.append(ref.id)
            details[ref.id] = ref

        return self.assemble(
            str(cd.get("disstid") or playlist_id),
            name=cd.get("dissname") or "",
            ids=ids,
            fetch_details=lambda missing: {i: details[i] for i in missing if i in details},
            previous=previous,
            refresh_metadata=refresh_metadata,
            track_count=total,
            update_time=_int_or_zero(cd.get("mtime") or cd.get("commit_time")),
            privacy=0 if cd.get("dir_show", 1) else 1,
            creator=(cd.get("nickname") or cd.get("creator", {}).get("nick") or "")
            if isinstance(cd.get("creator"), dict)
            else (cd.get("nickname") or ""),
        )


def _int_or_zero(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _song_to_track(song: dict) -> TrackRef | None:
    songmid = song.get("songmid")
    if not songmid:
        return None
    singers = song.get("singer") or ()
    return TrackRef(
        id=str(songmid),
        name=(song.get("songname") or song.get("songorig") or "").strip(),
        artists=tuple((s.get("name") or "").strip() for s in singers if isinstance(s, dict)),
        album=(song.get("albumname") or "").strip(),
        duration_ms=int(song.get("interval") or 0) * 1000,
        available=bool(song.get("songmid")),
        extra={
            "album_mid": song.get("albummid") or "",
            "pay": song.get("pay"),
            "size320": song.get("size320"),
        },
    )
