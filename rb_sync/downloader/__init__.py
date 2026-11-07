"""fetch 阶段：判断缺口、生成清单、调用外部下载命令。

本包**不实现下载，也不做任何解密**。真正的下载能力由用户自己配置的外部命令提供。
"""

from .plan import (
    STRATEGY_FILENAME,
    STRATEGY_PATH,
    STRATEGY_TITLE,
    AudioIndex,
    FetchItem,
    FetchPlan,
    Match,
    SelectionTrack,
    build_fetch_plan,
    load_selection_tracks,
    scan_audio_files,
    track_url,
    write_manifest,
)
from .runner import (
    POST_PLACEHOLDERS,
    PostResult,
    RunResult,
    RunSummary,
    render_command,
    render_post,
    run_items,
    run_post,
)
from .state import FetchState

__all__ = [
    "POST_PLACEHOLDERS",
    "STRATEGY_FILENAME",
    "STRATEGY_PATH",
    "STRATEGY_TITLE",
    "AudioIndex",
    "FetchItem",
    "FetchPlan",
    "FetchState",
    "Match",
    "PostResult",
    "RunResult",
    "RunSummary",
    "SelectionTrack",
    "build_fetch_plan",
    "load_selection_tracks",
    "render_command",
    "render_post",
    "run_items",
    "run_post",
    "scan_audio_files",
    "track_url",
    "write_manifest",
]
