"""Rekordbox 库层：只读体检（doctor）、路径重定位（relink）、后续的安全写入。

注意：本包依赖 pyrekordbox，属于可选依赖。
`rb-sync plan`（读歌单）不需要这里，也不会 import 它。
"""

from .library import (
    Library,
    LibraryReport,
    PlaylistRow,
    RekordboxError,
    TrackRow,
    default_db_path,
    is_rekordbox_running,
    resolve_db_path,
)
from .relink import RelinkItem, RelinkPlan, build_name_index, build_plan, remap_by_root

__all__ = [
    "Library",
    "LibraryReport",
    "PlaylistRow",
    "RekordboxError",
    "RelinkItem",
    "RelinkPlan",
    "TrackRow",
    "build_name_index",
    "build_plan",
    "default_db_path",
    "is_rekordbox_running",
    "remap_by_root",
    "resolve_db_path",
]
