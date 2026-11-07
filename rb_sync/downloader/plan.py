"""fetch 阶段：算出「还缺哪些曲目」，并准备下载清单。

**本模块不做任何下载。** 项目约束 C3：不内置下载器、不内置 NCM 解密。
这里只负责：
1. 把选择配置里的歌单成员合并成「曲目 → 所属歌单」的映射（R2 的落点）；
2. 扫曲库目录，判断哪些曲目本地已经有了（三层匹配）；
3. 产出清单：缺什么、每首在各平台的页面地址、每条附带的歌单归属。

真正的下载由用户配置的外部命令完成（见 ``runner.py``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..mapping.match import contains_words, normalize, normalize_words, track_key
from ..model import AUDIO_SUFFIXES, ENCRYPTED_SUFFIXES, PlaylistSnapshot, load_json
from ..state import StateStore

# 匹配策略（由强到弱，便于解释和人工复核）
STRATEGY_PATH = "path"  # 文件路径与库记录完全一致（只在有库信息时用）
STRATEGY_FILENAME = "filename_artist_title"  # 文件名能拆成「艺人 - 曲名」
STRATEGY_TITLE = "title_in_filename"  # 曲名出现在文件名里

URL_TEMPLATES = {
    "netease": "https://music.163.com/song?id={id}",
    "qq": "https://y.qq.com/n/ryqq/songDetail/{id}",
    "kugou": "https://www.kugou.com/song/#hash={id}",
    "local": "",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def track_url(source: str, track_id: str) -> str:
    template = URL_TEMPLATES.get(source, "")
    return template.format(id=track_id) if template else ""


# --------------------------------------------------------------- 歌单成员


@dataclass
class SelectionTrack:
    """合并后的一首曲目：带它所属的全部歌单名（多对多）。"""

    source: str
    track_id: str
    title: str
    artists: tuple[str, ...] = ()
    album: str = ""
    duration_ms: int = 0
    available: bool = True
    playlists: list[str] = field(default_factory=list)

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.track_id}"

    @property
    def artist_text(self) -> str:
        return " / ".join(a for a in self.artists if a)

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "source": self.source,
            "track_id": self.track_id,
            "title": self.title,
            "artists": list(self.artists),
            "album": self.album,
            "duration_ms": self.duration_ms,
            "playlists": list(self.playlists),
            "url": track_url(self.source, self.track_id),
        }


def load_selection_tracks(
    state_dir: str | Path = "state", only_source: str | None = None
) -> list[SelectionTrack]:
    """读取选择配置 + 各歌单快照，合并成「曲目 → [歌单…]」。

    同一首歌出现在多个歌单里时只保留一条，``playlists`` 里记全部歌单名 ——
    这正是写库阶段需要的多歌单归属。
    """
    store = StateStore(state_dir)
    selection = store.load_selection()
    if selection is None:
        return []

    merged: dict[str, SelectionTrack] = {}
    for entry in selection.playlists:
        source = str(entry.get("source") or "")
        playlist_id = str(entry.get("id") or "")
        name = str(entry.get("name") or playlist_id)
        if only_source and source != only_source:
            continue

        snapshot_path = entry.get("snapshot") or store.snapshot_path(source, playlist_id)
        data = load_json(Path(snapshot_path))
        if data is None:
            continue
        snapshot = PlaylistSnapshot.from_dict(data)
        for track in snapshot.tracks:
            uid = f"{source}:{track.id}"
            item = merged.get(uid)
            if item is None:
                item = SelectionTrack(
                    source=source,
                    track_id=track.id,
                    title=track.name,
                    artists=track.artists,
                    album=track.album,
                    duration_ms=track.duration_ms,
                    available=track.available,
                )
                merged[uid] = item
            if name not in item.playlists:
                item.playlists.append(name)

    return list(merged.values())


# --------------------------------------------------------------- 本地索引


@dataclass
class AudioFile:
    path: str
    stem: str
    key: str  # 「曲名|主艺人」归一化键（文件名能拆时才有）
    title_norm: str  # 归一化后的曲名
    stem_norm: str = ""  # 整个文件名的归一化形式（去标点），用于查艺人
    stem_words: str = ""  # 保留词边界的归一化形式，用于查曲名
    # True 表示加密容器（如 .ncm）：能被扫到，但**不能**算"已有曲目"
    encrypted: bool = False


def _split_stem(stem: str) -> tuple[str, str]:
    """把文件名主干拆成 (标题, 艺人)。常见形态：``艺人 - 曲名``。"""
    if " - " in stem:
        left, _, right = stem.partition(" - ")
        return right.strip(), left.strip()
    return stem.strip(), ""


def scan_audio_files(roots: list[str | Path]) -> list[AudioFile]:
    """扫曲库目录，返回所有音频文件（含加密容器，后者带 ``encrypted`` 标记）。"""
    files: list[AudioFile] = []
    seen: set[str] = set()
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in AUDIO_SUFFIXES:
                continue
            full = str(path)
            if full in seen:
                continue
            seen.add(full)
            title, artist = _split_stem(path.stem)
            files.append(
                AudioFile(
                    path=full,
                    stem=path.stem,
                    key=track_key(title, artist) if title and artist else "",
                    title_norm=normalize(title),
                    stem_norm=normalize(path.stem),
                    stem_words=normalize_words(path.stem),
                    encrypted=path.suffix.lower() in ENCRYPTED_SUFFIXES,
                )
            )
    return files


@dataclass
class Match:
    """一次匹配的结果。分成"可播放"和"加密容器"两种，语义完全不同。"""

    file: AudioFile | None = None
    strategy: str = ""
    candidates: int = 0
    encrypted: bool = False

    @property
    def found(self) -> bool:
        return self.file is not None


class AudioIndex:
    """按两种键索引文件，用于把曲目判定为「本地已有 / 只有加密文件」。"""

    def __init__(self, files: list[AudioFile]) -> None:
        self.files = files
        self._by_key: dict[str, list[AudioFile]] = {}
        self._by_title: dict[str, list[AudioFile]] = {}
        for item in files:
            if item.key:
                self._by_key.setdefault(item.key, []).append(item)
            if item.title_norm:
                self._by_title.setdefault(item.title_norm, []).append(item)

    @classmethod
    def from_roots(cls, roots: list[str | Path]) -> "AudioIndex":
        return cls(scan_audio_files(roots))

    def lookup(self, track: SelectionTrack) -> Match:
        """找这首歌的本地文件。

        规则：
        1. 文件名键精确命中且只有一份 → 用
        2. 曲名按词边界命中 + 艺人也在文件名里 → 用（能容忍 ``Song (Extended Mix)``）
        3. 多个候选 → 不猜
        4. **可播放文件优先**：同一首既有 .flac 又有 .ncm 时算「已有」；
           只有 .ncm 时返回 ``encrypted=True``，由调用方标成"待解密"而不是"已有"

        第二层必须同时命中艺人是踩出来的：只比曲名会把「颜人中 - My love」
        错配到「MK (JPN) - My Love.flac」。
        """
        key = track_key(track.title, track.artists)
        matched = self._pick(self._by_key.get(key, []), STRATEGY_FILENAME)
        if matched.found or matched.candidates > 1:
            return matched

        artist_norm = normalize(track.artists[0]) if track.artists else ""
        title_words = normalize_words(track.title)
        if not artist_norm or not title_words:
            return Match()
        hits = [
            f
            for f in self.files
            if artist_norm in f.stem_norm and contains_words(f.stem_words, title_words)
        ]
        return self._pick(hits, STRATEGY_TITLE)

    @staticmethod
    def _pick(hits: list[AudioFile], strategy: str) -> Match:
        if not hits:
            return Match()
        playable = [f for f in hits if not f.encrypted]
        if len(playable) == 1:
            return Match(playable[0], strategy, 1, encrypted=False)
        if len(playable) > 1:
            return Match(None, strategy, len(playable), encrypted=False)
        # 只剩加密文件
        if len(hits) == 1:
            return Match(hits[0], strategy, 1, encrypted=True)
        return Match(None, strategy, len(hits), encrypted=True)


# ------------------------------------------------------------------ 计划


@dataclass
class FetchItem:
    track: SelectionTrack
    status: str  # present | encrypted | ambiguous | missing
    matched_path: str = ""
    strategy: str = ""
    candidates: int = 0

    @property
    def uid(self) -> str:
        return self.track.uid

    def to_dict(self) -> dict:
        data = self.track.to_dict()
        data.update(
            {
                "status": self.status,
                "matched_path": self.matched_path,
                "strategy": self.strategy,
                "candidates": self.candidates,
            }
        )
        return data


@dataclass
class FetchPlan:
    music_roots: list[str]
    items: list[FetchItem] = field(default_factory=list)
    generated_at: str = ""

    @property
    def present(self) -> list[FetchItem]:
        return [i for i in self.items if i.status == "present"]

    @property
    def missing(self) -> list[FetchItem]:
        return [i for i in self.items if i.status == "missing"]

    @property
    def ambiguous(self) -> list[FetchItem]:
        return [i for i in self.items if i.status == "ambiguous"]

    @property
    def encrypted(self) -> list[FetchItem]:
        return [i for i in self.items if i.status == "encrypted"]

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at or now_iso(),
            "music_roots": self.music_roots,
            "counts": {
                "total": len(self.items),
                "present": len(self.present),
                "missing": len(self.missing),
                "encrypted": len(self.encrypted),
                "ambiguous": len(self.ambiguous),
            },
            "missing": [i.to_dict() for i in self.missing],
            "encrypted": [i.to_dict() for i in self.encrypted],
            "ambiguous": [i.to_dict() for i in self.ambiguous],
            "present": [i.to_dict() for i in self.present],
        }

    def missing_by_source(self) -> dict[str, list[FetchItem]]:
        grouped: dict[str, list[FetchItem]] = {}
        for item in self.missing:
            grouped.setdefault(item.track.source, []).append(item)
        return grouped


def build_fetch_plan(
    state_dir: str | Path = "state",
    music_roots: list[str | Path] | None = None,
    only_source: str | None = None,
    index: AudioIndex | None = None,
) -> FetchPlan:
    """算出「本地已有 / 待解密 / 缺失 / 有歧义」四类曲目。"""
    tracks = load_selection_tracks(state_dir, only_source)
    roots = [str(r) for r in (music_roots or [])]
    audio_index = index or AudioIndex.from_roots(roots)

    items: list[FetchItem] = []
    for track in tracks:
        match = audio_index.lookup(track)
        if match.found and not match.encrypted:
            items.append(
                FetchItem(track, "present", match.file.path, match.strategy or STRATEGY_FILENAME, 1)
            )
        elif match.found and match.encrypted:
            # 只有加密容器：不能算"已有"，Rekordbox 放不了它
            items.append(FetchItem(track, "encrypted", match.file.path, match.strategy, 1))
        elif match.candidates > 1:
            items.append(FetchItem(track, "ambiguous", "", match.strategy, match.candidates))
        else:
            items.append(FetchItem(track, "missing"))

    return FetchPlan(music_roots=roots, items=items, generated_at=now_iso())


def write_manifest(plan: FetchPlan, out_dir: str | Path) -> dict[str, Path]:
    """按平台写「URL 清单」，方便直接喂给外部下载工具（不执行任何命令）。"""
    base = Path(out_dir)
    base.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for source, items in plan.missing_by_source().items():
        path = base / f"{source}.txt"
        lines = []
        for item in items:
            url = track_url(item.track.source, item.track.track_id)
            playlists = "、".join(item.track.playlists)
            lines.append(f"{url}\t# {playlists}" if url else f"# {playlists}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written[source] = path
    return written
