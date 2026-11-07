"""业务逻辑层：CLI 与 WebUI 共用。

这里不打印任何东西、不解析命令行 —— 只接收参数、返回结构化结果。
CLI 负责把它渲染成文本，Web 负责把它序列化成 JSON。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import load_cookie, load_settings
from .mapping.diff import diff_snapshots
from .model import Selection, dump_json
from .sources import SourceError, build_source, detect, source_names
from .state import DEFAULT_STATE_DIR, StateStore, now_iso


# ------------------------------------------------------------------ plan


@dataclass
class PlanResult:
    # 本次运行涉及到的歌单（用于展示）
    playlists: list[dict] = field(default_factory=list)
    # 落盘后的完整选择配置（--append 时包含之前的歌单）
    selection_playlists: list[dict] = field(default_factory=list)
    report: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    selection_path: Path | None = None
    dropped_previous: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def selection_count(self) -> int:
        return len(self.selection_playlists)


def resolve_targets(inputs: list[str], forced_source: str | None = None) -> list[tuple[str, str]]:
    """把输入解析成 (源名, 原始输入)。纯数字 ID 无法判平台，必须显式指定。"""
    targets: list[tuple[str, str]] = []
    for raw in inputs:
        source_name = forced_source or detect(raw)
        if source_name is None:
            if raw.strip().isdigit():
                raise SourceError(
                    f"{raw!r} 是纯数字 ID，三个平台之间会有歧义。\n"
                    f"  请直接粘贴歌单链接（推荐），或加 --source "
                    f"{'/'.join(n for n in source_names() if n != 'local')}"
                )
            raise SourceError(
                f"无法识别 {raw!r} 属于哪个平台。请粘贴歌单链接，"
                f"或改用 --source {source_names()[0]} 显式指定"
            )
        targets.append((source_name, raw))
    return targets


def run_plan(
    inputs: list[str],
    *,
    forced_source: str | None = None,
    name: str | None = None,
    state_dir: str | Path = DEFAULT_STATE_DIR,
    refresh: bool = False,
    append: bool = False,
    cookie: str | None = None,
    timeout: float = 25.0,
    interval: float | None = None,
) -> PlanResult:
    """读取歌单 → 更新快照 → 生成/更新选择配置。

    单个歌单失败不会中断其它歌单；失败信息进 ``errors``。
    """
    store = StateStore(state_dir)
    targets = resolve_targets(inputs, forced_source)
    if not targets:
        raise SourceError("至少需要一个歌单链接或 ID")
    if name and len(targets) != 1:
        raise SourceError("--name 只能用于单个歌单")

    result = PlanResult()
    adapters: dict[str, object] = {}

    for source_name, raw in targets:
        adapter = adapters.get(source_name)
        if adapter is None:
            adapter = build_source(
                source_name,
                cookie=load_cookie(source_name, cookie),
                timeout=timeout,
                interval=interval,
            )
            adapters[source_name] = adapter

        try:
            playlist_id = adapter.resolve_id(raw)
            previous = None if refresh else store.load_snapshot(source_name, playlist_id)
            snapshot = adapter.fetch(playlist_id, previous=previous, refresh_metadata=refresh)
        except SourceError as exc:
            result.errors.append({"source": source_name, "input": raw, "error": str(exc)})
            continue

        if name:
            snapshot.name = name

        change = diff_snapshots(previous, snapshot)
        store.save_snapshot(snapshot)
        result.report.append(change.to_dict())
        result.playlists.append(
            {
                "source": source_name,
                "id": snapshot.playlist_id,
                "name": snapshot.name,
                "track_count": snapshot.track_count,
                "url": snapshot.url,
                "snapshot": store.snapshot_path(source_name, snapshot.playlist_id).as_posix(),
                "content_hash": snapshot.content_hash,
            }
        )

    if result.playlists:
        previous_selection = store.load_selection()
        if append and previous_selection is not None:
            new_keys = {(p["source"], p["id"]) for p in result.playlists}
            kept = [
                p
                for p in previous_selection.playlists
                if (str(p.get("source")), str(p.get("id"))) not in new_keys
            ]
            merged = kept + result.playlists
        else:
            merged = result.playlists
            if previous_selection is not None:
                new_keys = {(p["source"], p["id"]) for p in result.playlists}
                result.dropped_previous = sum(
                    1
                    for p in previous_selection.playlists
                    if (str(p.get("source")), str(p.get("id"))) not in new_keys
                )
        result.selection_path = store.save_selection(
            Selection(playlists=merged, generated_at=now_iso())
        )
        result.selection_playlists = merged

    return result


# --------------------------------------------------------------- library


def inspect_library(db_path: str | Path | None = None, state_dir: str | Path = DEFAULT_STATE_DIR):
    """只读体检。需要 pyrekordbox（可选依赖）。"""
    from .rekordbox import Library

    with Library(db_path, cache_dir=Path(state_dir) / ".cache") as lib:
        return lib.inspect()


def preview_relink(
    db_path: str | Path | None,
    from_root: str,
    to_root: str,
    search_roots: list[str] | None = None,
    *,
    state_dir: str | Path = DEFAULT_STATE_DIR,
    out: str | Path | None = None,
):
    """算出重定位映射并落盘（只算不写库）。"""
    from .rekordbox import build_plan

    report = inspect_library(db_path, state_dir)
    missing = report.missing
    roots = list(search_roots or []) or [to_root]
    plan = build_plan(missing, from_root, to_root, roots)
    out_path = Path(out) if out else Path(state_dir) / "relink_plan.json"
    dump_json(out_path, plan.to_dict())
    return plan, out_path


# ---------------------------------------------------------------- probe


def probe_environment(db_path: str | Path | None = None) -> dict:
    """设置页「探测」用：库能不能开、曲库目录里有多少音频。

    永不抛异常 —— 所有问题都变成结构化的 checks，交给界面显示。
    """
    from .rekordbox import RekordboxError, is_rekordbox_running, resolve_db_path

    settings = load_settings()
    checks: list[dict] = []

    # 1) 库
    library_info: dict = {}
    try:
        path = resolve_db_path(db_path or settings.db_path or None)
        report = inspect_library(path)
        library_info = {
            "path": str(report.db_path),
            "tracks": len(report.tracks),
            "playlists": len([p for p in report.playlists if not p.is_folder]),
            "folders": len([p for p in report.playlists if p.is_folder]),
            "missing_files": len(report.missing),
            "present_files": len(report.present),
            "integrity": report.integrity,
            "usn": report.usn,
        }
        checks.append(
            {
                "level": "ok",
                "key": "library",
                "message": (
                    f"库可打开：{library_info['tracks']:,} 首 / "
                    f"{library_info['playlists']} 个歌单 / integrity: {library_info['integrity']}"
                ),
            }
        )
    except RekordboxError as exc:
        checks.append({"level": "error", "key": "library", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 - 探测不该把异常抛给界面
        checks.append({"level": "error", "key": "library", "message": f"打开库失败：{exc}"})

    # 2) 曲库目录
    roots = list(settings.music_roots)
    if roots:
        from .rekordbox.relink import AUDIO_SUFFIXES

        for root in roots:
            base = Path(root)
            if not base.is_dir():
                checks.append(
                    {"level": "error", "key": "music_root", "message": f"曲库目录不存在：{root}"}
                )
                continue
            count = sum(
                1
                for p in base.rglob("*")
                if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
            )
            checks.append(
                {
                    "level": "ok",
                    "key": "music_root",
                    "message": f"{root} 下有 {count:,} 个音频文件",
                }
            )
    else:
        checks.append(
            {
                "level": "warn",
                "key": "music_root",
                "message": "还没设置曲库目录 —— 曲目匹配与路径重定位会无法工作",
            }
        )

    # 3) Rekordbox 进程
    running = is_rekordbox_running()
    checks.append(
        {
            "level": "warn" if running else "ok",
            "key": "process",
            "message": (
                "Rekordbox 正在运行 —— 读取不受影响，写入会被拒绝"
                if running
                else "Rekordbox 未运行"
            ),
        }
    )

    return {"library": library_info, "checks": checks, "rekordbox_running": running}
