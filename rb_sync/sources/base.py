"""源适配器的公共基类与工具。

新增平台只需要：
1. 继承 ``HttpSource``（或直接实现 ``Source`` 协议）；
2. 定义 ``name`` / ``display_name`` / ``host_patterns`` / ``url_template``；
3. 实现 ``resolve_id`` 与 ``fetch``；
4. 在 ``registry.py`` 里登记一行。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, runtime_checkable

from ..model import PlaylistSnapshot


class SourceError(RuntimeError):
    """源侧返回了非预期结果（无此歌单 / 私密 / 被风控 / 网络失败）。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def content_hash(ids: list[str]) -> str:
    """有序成员 ID 列表的稳定摘要。

    三个平台的变更探测都靠它，避免依赖「各平台是否提供更新时间戳」。
    """
    digest = hashlib.sha1()
    for track_id in ids:
        digest.update(str(track_id).encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()[:16]


@runtime_checkable
class Source(Protocol):
    name: str
    display_name: str

    def resolve_id(self, text: str) -> str: ...

    def fetch(
        self,
        playlist_id: str,
        previous: PlaylistSnapshot | None = None,
        refresh_metadata: bool = False,
    ) -> PlaylistSnapshot: ...


class HttpSource:
    """带节流、超时、错误归一化的 HTTP 源基类。"""

    name = ""
    display_name = ""
    referer = ""
    # 用于从文本里识别该平台（正则字符串，按顺序尝试）
    host_patterns: tuple[str, ...] = ()
    url_template = ""
    default_headers: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    interval = 0.25

    def __init__(
        self,
        cookie: str = "",
        timeout: float = 25.0,
        interval: float | None = None,
    ) -> None:
        self.timeout = timeout
        if interval is not None:
            self.interval = interval
        self.headers = dict(self.default_headers)
        if self.referer:
            self.headers["Referer"] = self.referer
        if cookie:
            # 用户 Cookie 覆盖默认值：私密歌单 / 需要登录态的接口
            self.headers["Cookie"] = cookie
        self._last_request = 0.0

    # ---------------------------------------------------------------- HTTP

    def _throttle(self) -> None:
        if self.interval <= 0:
            return
        wait = self.interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def request_text(
        self,
        url: str,
        form: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        self._throttle()
        data = urllib.parse.urlencode(form).encode("utf-8") if form else None
        hdrs = dict(self.headers)
        if headers:
            hdrs.update(headers)
        if data is not None:
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(
            url, data=data, headers=hdrs, method="POST" if data else "GET"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise SourceError(f"{self.display_name}返回 HTTP {exc.code}：{url}") from exc
        except urllib.error.URLError as exc:
            raise SourceError(f"无法连接{self.display_name}（{exc.reason}）：{url}") from exc

    def request_json(
        self,
        url: str,
        form: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        text = self.request_text(url, form=form, headers=headers)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise SourceError(
                f"{self.display_name}返回的不是 JSON：{text[:200]!r}"
            ) from exc

    # ------------------------------------------------------------- 通用

    def playlist_url(self, playlist_id: str) -> str:
        return self.url_template.format(id=playlist_id)

    def build_snapshot(
        self,
        playlist_id: str,
        *,
        name: str,
        tracks: list,
        **kwargs: Any,
    ) -> PlaylistSnapshot:
        return PlaylistSnapshot(
            source=self.name,
            playlist_id=str(playlist_id),
            name=(name or "").strip(),
            tracks=tracks,
            track_count=kwargs.pop("track_count", len(tracks)),
            url=self.playlist_url(str(playlist_id)),
            fetched_at=now_iso(),
            content_hash=content_hash([t.id for t in tracks]),
            **kwargs,
        )

    def assemble(
        self,
        playlist_id: str,
        *,
        name: str,
        ids: list[str],
        fetch_details: Callable[[list[str]], dict[str, Any]],
        previous: PlaylistSnapshot | None,
        refresh_metadata: bool,
        unavailable: set[str] | None = None,
        **kwargs: Any,
    ) -> PlaylistSnapshot:
        """三个平台共用的组装逻辑。

        1. 成员 ID 的有序列表已拿到 → 先算 content_hash；
        2. 与上次快照一致则直接复用上次的曲目元数据（0 次元数据请求）；
        3. 有变化时只为**新增** ID 拉元数据，其余走缓存。
        """
        from ..model import TrackRef

        clean_name = (name or "").strip()
        if (
            previous is not None
            and not refresh_metadata
            and previous.content_hash == content_hash(ids)
            # 防御：快照被手工改坏（长度与 hash 不一致）时不要走缓存
            and len(previous.tracks) == len(ids)
            and previous.name == clean_name
        ):
            return dataclasses.replace(previous, fetched_at=now_iso())

        cache: dict[str, Any] = (
            {} if (refresh_metadata or previous is None) else previous.metadata_by_id()
        )
        need = [i for i in ids if i not in cache]
        fresh = fetch_details(need) if need else {}
        banned = unavailable or set()

        tracks = []
        for track_id in ids:
            ref = cache.get(track_id) or fresh.get(track_id)
            if ref is None:
                tracks.append(TrackRef(id=track_id, available=False))
            elif track_id in banned and ref.available:
                tracks.append(dataclasses.replace(ref, available=False))
            else:
                tracks.append(ref)

        return self.build_snapshot(playlist_id, name=clean_name, tracks=tracks, **kwargs)


def matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    import re

    return any(re.search(p, text, re.IGNORECASE) for p in patterns)
