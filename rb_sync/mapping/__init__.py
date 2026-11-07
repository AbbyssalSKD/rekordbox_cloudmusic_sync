"""映射层：差异计算、跨平台匹配、后续的 track -> [playlist] 归属表。"""

from .diff import ChangeSet, Rename, diff_snapshots
from .match import (
    contains_words,
    duration_bucket,
    normalize,
    normalize_words,
    same_track,
    track_key,
)

__all__ = [
    "ChangeSet",
    "Rename",
    "contains_words",
    "diff_snapshots",
    "duration_bucket",
    "normalize",
    "normalize_words",
    "same_track",
    "track_key",
]
