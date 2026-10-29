"""歌单差异计算：判断「上次运行之后歌单被改了什么」。

跨平台通用 —— 只依赖「有序成员 ID 列表 + 曲目元数据」，
不需要任何平台提供变更时间戳。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..model import PlaylistSnapshot, TrackRef

# 成员数量或顺序都没变时，用 content_hash 就能提前判定（见 sources/base.py）


@dataclass
class Rename:
    track_id: str
    old_name: str
    new_name: str


@dataclass
class ChangeSet:
    playlist_id: str
    playlist_name: str
    source: str = ""
    first_seen: bool = False
    unchanged: bool = False
    name_changed: tuple[str, str] | None = None
    added: list[TrackRef] = field(default_factory=list)
    removed: list[TrackRef] = field(default_factory=list)
    reordered: bool = False
    moved_count: int = 0
    renamed: list[Rename] = field(default_factory=list)
    became_unavailable: list[TrackRef] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.first_seen
            or self.name_changed
            or self.added
            or self.removed
            or self.reordered
            or self.renamed
            or self.became_unavailable
        )

    def summary(self) -> str:
        if self.first_seen:
            return "首次读取"
        if not self.has_changes:
            return "无变化"
        parts: list[str] = []
        if self.added:
            parts.append(f"+{len(self.added)}")
        if self.removed:
            parts.append(f"-{len(self.removed)}")
        if self.reordered:
            parts.append(f"顺序变动 {self.moved_count}")
        if self.renamed:
            parts.append(f"改名 {len(self.renamed)}")
        if self.became_unavailable:
            parts.append(f"变灰 {len(self.became_unavailable)}")
        if self.name_changed:
            parts.append("歌单改名")
        return " ".join(parts) or "有变化"

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "playlist_id": self.playlist_id,
            "playlist_name": self.playlist_name,
            "first_seen": self.first_seen,
            "unchanged": self.unchanged,
            "name_changed": list(self.name_changed) if self.name_changed else None,
            "added": [t.to_dict() for t in self.added],
            "removed": [t.to_dict() for t in self.removed],
            "reordered": self.reordered,
            "moved_count": self.moved_count,
            "renamed": [
                {"track_id": r.track_id, "old_name": r.old_name, "new_name": r.new_name}
                for r in self.renamed
            ],
            "became_unavailable": [t.to_dict() for t in self.became_unavailable],
        }


def diff_snapshots(old: PlaylistSnapshot | None, new: PlaylistSnapshot) -> ChangeSet:
    """对比同一歌单的两次快照。``old is None`` 表示首次读取。"""
    if old is None:
        return ChangeSet(
            source=new.source,
            playlist_id=new.playlist_id,
            playlist_name=new.name,
            first_seen=True,
            added=list(new.tracks),
        )

    old_ids = old.track_ids
    new_ids = new.track_ids
    old_set, new_set = set(old_ids), set(new_ids)
    old_by_id = old.metadata_by_id()
    new_by_id = new.metadata_by_id()

    added = [new_by_id[i] for i in new_ids if i not in old_set]
    removed = [old_by_id[i] for i in old_ids if i not in new_set]

    common_old = [i for i in old_ids if i in new_set]
    common_new = [i for i in new_ids if i in old_set]
    reordered = common_old != common_new
    moved = 0
    if reordered:
        old_pos = {tid: idx for idx, tid in enumerate(common_old)}
        moved = sum(1 for idx, tid in enumerate(common_new) if old_pos[tid] != idx)

    renamed = [
        Rename(i, old_by_id[i].name, new_by_id[i].name)
        for i in common_new
        if i in old_by_id and old_by_id[i].name != new_by_id[i].name
    ]
    became_unavailable = [
        new_by_id[i]
        for i in common_new
        if old_by_id.get(i) and old_by_id[i].available and not new_by_id[i].available
    ]

    name_changed = (old.name, new.name) if old.name != new.name else None

    change = ChangeSet(
        source=new.source,
        playlist_id=new.playlist_id,
        playlist_name=new.name,
        name_changed=name_changed,
        added=added,
        removed=removed,
        reordered=reordered,
        moved_count=moved,
        renamed=renamed,
        became_unavailable=became_unavailable,
    )
    change.unchanged = not change.has_changes
    return change
