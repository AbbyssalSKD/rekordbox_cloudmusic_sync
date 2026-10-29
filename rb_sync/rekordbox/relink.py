"""路径重定位（relink）：把库里失效的绝对路径映射到文件的新位置。

为什么需要它：Rekordbox 把绝对路径写进 `DjmdContent.FolderPath`。移动硬盘
换盘符、目录改名、甚至换扩展名（.flac → .wav），库里记录就全部失效 —— 你
那份库里 1231 首有 338 首是这么失效的，其中 332 首只是从 E: 搬到了 F:。

匹配是分层的、每层都可解释（不做模糊猜测）：
  1. root_remap  旧根 → 新根，逐字符替换后文件存在
  2. basename    同文件名（可忽略扩展名）在搜索目录里唯一命中
  3. 其余        标记 unmatched，交人工处理

本模块**只做计算**，不碰数据库。写库是后续独立步骤。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

AUDIO_SUFFIXES = (".mp3", ".flac", ".wav", ".aiff", ".aif", ".m4a", ".ogg", ".mp4", ".ncm")


@dataclass
class RelinkItem:
    content_id: str
    title: str
    artist: str
    old_path: str
    new_path: str | None = None
    strategy: str = "unmatched"

    @property
    def matched(self) -> bool:
        return self.new_path is not None

    def to_dict(self) -> dict:
        return {
            "content_id": self.content_id,
            "title": self.title,
            "artist": self.artist,
            "old_path": self.old_path,
            "new_path": self.new_path,
            "strategy": self.strategy,
        }


@dataclass
class RelinkPlan:
    from_root: str
    to_root: str
    items: list[RelinkItem] = field(default_factory=list)
    ambiguous: list[RelinkItem] = field(default_factory=list)

    @property
    def matched(self) -> list[RelinkItem]:
        return [i for i in self.items if i.matched]

    @property
    def unmatched(self) -> list[RelinkItem]:
        return [i for i in self.items if not i.matched]

    def by_strategy(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for item in self.matched:
            counts[item.strategy] += 1
        return dict(counts)

    def to_dict(self) -> dict:
        return {
            "from_root": self.from_root,
            "to_root": self.to_root,
            "counts": {
                "total": len(self.items),
                "matched": len(self.matched),
                "unmatched": len(self.unmatched),
                "ambiguous": len(self.ambiguous),
                "by_strategy": self.by_strategy(),
            },
            "items": [i.to_dict() for i in self.matched],
            "unmatched": [i.to_dict() for i in self.unmatched],
        }


def normalize_root(root: str) -> str:
    text = (root or "").strip().replace("\\", "/")
    if not text:
        return ""
    return text if text.endswith("/") else text + "/"


def remap_by_root(path: str, from_root: str, to_root: str) -> str | None:
    """把 ``path`` 的前缀 ``from_root`` 换成 ``to_root``；不匹配返回 None。"""
    old = normalize_root(from_root)
    new = normalize_root(to_root)
    if not old or not new:
        return None
    normalized = (path or "").replace("\\", "/")
    lower_path = normalized.lower()
    if not lower_path.startswith(old.lower()):
        return None
    return new + normalized[len(old) :]


def build_name_index(roots: list[str | Path]) -> dict[str, list[str]]:
    """按「文件名(小写)」和「去扩展名的文件名(小写)」两个键建索引。"""
    index: dict[str, list[str]] = defaultdict(list)
    for root in roots:
        base = Path(root)
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in AUDIO_SUFFIXES:
                continue
            full = str(path).replace("\\", "/")
            index[path.name.lower()].append(full)
            index[path.stem.lower()].append(full)
    return dict(index)


def build_plan(
    tracks,
    from_root: str,
    to_root: str,
    search_roots: list[str | Path] | None = None,
    name_index: dict[str, list[str]] | None = None,
) -> RelinkPlan:
    """对一批失效曲目生成重定位计划（纯计算）。

    ``tracks`` 需要具备 ``id`` / ``title`` / ``artist`` / ``path`` 属性。
    """
    plan = RelinkPlan(from_root=from_root, to_root=to_root)
    index = name_index if name_index is not None else build_name_index(
        search_roots or [to_root]
    )

    for track in tracks:
        item = RelinkItem(
            content_id=str(track.id),
            title=getattr(track, "title", "") or "",
            artist=getattr(track, "artist", "") or "",
            old_path=getattr(track, "path", "") or "",
        )
        plan.items.append(item)
        if not item.old_path:
            continue

        # 1) 旧根 → 新根
        candidate = remap_by_root(item.old_path, from_root, to_root)
        if candidate and Path(candidate).is_file():
            item.new_path = candidate
            item.strategy = "root_remap"
            continue

        # 2) 同名文件（允许换目录；换扩展名时单独标记，因为可能换了音质版本）
        name = Path(item.old_path).name.lower()
        stem = Path(item.old_path).stem.lower()
        hits = index.get(name) or index.get(stem) or []
        hits = list(dict.fromkeys(hits))
        if len(hits) == 1:
            item.new_path = hits[0]
            same_ext = Path(item.old_path).suffix.lower() == Path(hits[0]).suffix.lower()
            item.strategy = "basename" if same_ext else "basename_ext"
        elif len(hits) > 1:
            plan.ambiguous.append(item)

    return plan
