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

    # 0) 配置文件本身
    from .config import config_error

    if config_error():
        checks.append(
            {
                "level": "error",
                "key": "config",
                "message": (
                    f"配置文件有误：{config_error()}"
                    "（已回退到默认值，下面看到的路径可能不是你配置的）"
                ),
            }
        )

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

# ---------------------------------------------------------------- fetch


def fetch_plan(
    state_dir: str | Path = DEFAULT_STATE_DIR,
    music_roots: list[str] | None = None,
    only_source: str | None = None,
):
    """算出「本地已有 / 缺失 / 歧义」，返回 FetchPlan。不下载任何东西。"""
    from .downloader import build_fetch_plan

    settings = load_settings()
    roots = list(music_roots) if music_roots is not None else list(settings.music_roots)
    return build_fetch_plan(state_dir, roots, only_source)


def fetch_write_manifest(plan, out_dir: str | Path) -> dict:
    """把缺失清单按平台写成 URL 列表，交给用户自己的下载工具。"""
    from .downloader import write_manifest

    return write_manifest(plan, out_dir)


def _sync_state_with_plan(state, plan) -> dict:
    """让 fetch_state 与磁盘实际状态对齐。

    语义上分两档：
    - ``present``：磁盘上真的找到了（可信，带路径）
    - ``downloaded``：外部命令返回 0，但可能还没按预期落盘或文件名不含艺人

    所以这里只把「找到的」标为 present、「本来是 present 但文件没了」降级为 missing；
    ``downloaded`` 不动 —— 由 ``unverified`` 数量提醒用户去人工确认。
    """
    marked_present = 0
    lost = 0
    encrypted = 0
    for item in plan.present:
        entry = state.get(item.uid) or {}
        if entry.get("status") != "present" or entry.get("path") != item.matched_path:
            state.mark(item.uid, "present", path=item.matched_path)
            marked_present += 1
    for item in plan.encrypted:
        # 加密容器：文件在，但不可用。标成 encrypted 让它**不被跳过**，
        # 这样配了后处理命令时会重试（而不是因为"下载成功"就永远不再处理）。
        state.mark(item.uid, "encrypted", path=item.matched_path)
        encrypted += 1
    for item in plan.missing:
        entry = state.get(item.uid) or {}
        if entry.get("status") == "present":
            state.mark(item.uid, "missing")
            lost += 1
    state.save()
    return {"marked_present": marked_present, "marked_encrypted": encrypted, "lost": lost}


def _unverified_uids(state, plan) -> list[str]:
    """命令成功了、但磁盘上没确认到的曲目。"""
    present = {item.uid for item in plan.present}
    return [
        uid
        for uid, entry in state.items.items()
        if entry.get("status") == "downloaded" and uid not in present
    ]


def fetch_run(
    state_dir: str | Path = DEFAULT_STATE_DIR,
    music_roots: list[str] | None = None,
    only_source: str | None = None,
    out_dir: str | None = None,
    limit: int | None = None,
    timeout: float | None = None,
    on_progress=None,
    commands: dict[str, str] | None = None,
    force: bool = False,
    post_command: str | None = None,
    on_post_progress=None,
) -> dict:
    """对缺失曲目调用**用户配置的外部命令**。项目本身不下载。

    没有为某个平台配置命令时，该平台的曲目会被计入 ``skipped_no_command`` ——
    这不是错误，是 handoff 模式：清单已经生成，交给用户自己的工具。
    """
    from .downloader import FetchState, run_items

    settings = load_settings()
    roots = list(music_roots) if music_roots is not None else list(settings.music_roots)
    plan = fetch_plan(state_dir, roots, only_source)
    state = FetchState(Path(state_dir) / "fetch_state.json")

    queued = [item for item in plan.missing if force or not state.can_skip(item.uid)]
    if limit and limit > 0:
        queued = queued[:limit]

    out = str(out_dir or settings.fetch_out_dir or "")
    per_item_timeout = float(timeout or settings.fetch_timeout or 600)

    by_source: dict[str, list] = {}
    for item in queued:
        by_source.setdefault(item.track.source, []).append(item)

    command_map = dict(settings.fetch_commands) if commands is None else dict(commands)

    summaries: dict[str, dict] = {}
    skipped: dict[str, int] = {}
    for source, items in by_source.items():
        template = command_map.get(source, "")
        if not template:
            skipped[source] = len(items)
            continue
        summary = run_items(
            template,
            items,
            out_dir=out,
            timeout=per_item_timeout,
            on_progress=on_progress,
        )
        summaries[source] = summary.to_dict()
        for result in summary.results:
            state.mark(
                result.uid,
                "downloaded" if result.ok else "failed",
                error="" if result.ok else (result.stderr or result.stdout),
            )
    state.save()

    # 第一次重扫：确认哪些真的落盘了（其中可能有加密容器）
    after = fetch_plan(state_dir, roots, only_source)

    # 后处理：只对「只有加密容器」的曲目跑用户配置的通用命令。
    # 项目不内置任何解密实现 —— 这里只是"跑一条你给的命令"。
    post_template = (
        post_command if post_command is not None else settings.fetch_post_command
    ) or ""
    encrypted_before = len(after.encrypted)
    post_results: list[dict] = []
    if post_template and after.encrypted:
        from .downloader import run_post

        results = run_post(
            post_template,
            after.encrypted,
            out_dir=out,
            timeout=per_item_timeout,
            on_progress=on_post_progress,
        )
        post_results = [r.to_dict() for r in results]
        # 后处理会改变磁盘 → 再扫一次
        after = fetch_plan(state_dir, roots, only_source)

    synced = _sync_state_with_plan(state, after)
    unverified = _unverified_uids(state, after)

    return {
        "queued": len(queued),
        "out_dir": out,
        "per_source": {
            source: {
                "queued": len(items),
                "ok": summaries.get(source, {}).get("counts", {}).get("ok", 0),
                "failed": summaries.get(source, {}).get("counts", {}).get("failed", 0),
                "skipped_no_command": skipped.get(source, 0),
            }
            for source, items in by_source.items()
        },
        "results": summaries,
        "before": plan.to_dict()["counts"],
        "after": after.to_dict()["counts"],
        "state_sync": synced,
        "unverified": unverified,
        "encrypted": {
            "before": encrypted_before,
            "after": len(after.encrypted),
            "post_configured": bool(post_template),
            "post": post_results,
        },
    }


def fetch_verify(
    state_dir: str | Path = DEFAULT_STATE_DIR,
    music_roots: list[str] | None = None,
    only_source: str | None = None,
) -> dict:
    """只重扫磁盘并更新状态，不执行任何命令。"""
    from .downloader import FetchState

    plan = fetch_plan(state_dir, music_roots, only_source)
    state = FetchState(Path(state_dir) / "fetch_state.json")
    synced = _sync_state_with_plan(state, plan)
    return {
        "counts": plan.to_dict()["counts"],
        "state_sync": synced,
        "unverified": _unverified_uids(state, plan),
    }


def fetch_run_playlists(
    state_dir: str | Path = DEFAULT_STATE_DIR,
    music_roots: list[str] | None = None,
    only_source: str | None = None,
    inbox_dir: str | None = None,
    dest_dir: str | None = None,
    template: str | None = None,
    timeout: float | None = None,
    on_progress=None,
    keep_inbox: bool = False,
) -> dict:
    """整歌单模式：下载 → 挑出真缺的入库 → 清空暂存区。

    项目仍然不实现下载：这里只是「按歌单跑一条你配置的外部命令」，
    再把落盘结果和缺口清单对账。多数下载器是歌单级的，逐曲接口并不存在，
    所以要按歌单走一趟、再自己挑。

    没有缺口的歌单不会被下载，省流量。
    """
    from .downloader import FetchState
    from .downloader.pipeline import (
        IngestResult,
        clear_inbox,
        harvest,
        playlist_targets,
        run_playlist_command,
    )

    settings = load_settings()
    roots = list(music_roots) if music_roots is not None else list(settings.music_roots)
    plan = fetch_plan(state_dir, roots, only_source)

    store = StateStore(state_dir)
    selection = store.load_selection()
    if selection is None or not selection.playlists:
        raise ValueError("还没有选择配置：先读一次歌单（rb-sync plan 或 WebUI「歌单」页）")

    inbox = str(inbox_dir or settings.fetch_inbox_dir or "")
    if not inbox:
        raise ValueError("未配置暂存目录：设置 fetch.inbox_dir（或加 --inbox）")

    dest = str(dest_dir or settings.fetch_out_dir or (roots[0] if roots else ""))
    if not dest:
        raise ValueError("未配置入库目录：设置 fetch.out_dir 或 rekordbox.music_roots")

    command = (template if template is not None else settings.fetch_playlist_command) or ""
    if not command:
        raise ValueError(
            "未配置整歌单下载命令：设置 fetch.playlist_command（或加 --playlist-command）"
        )

    per_playlist_timeout = float(timeout or 3600)

    targets = playlist_targets(selection.playlists, plan)
    runs = []
    for index, target in enumerate(targets, start=1):
        if on_progress:
            on_progress(index, len(targets), target)
        runs.append(
            run_playlist_command(
                command, target, cwd=inbox, out_dir=dest, timeout=per_playlist_timeout
            )
        )

    ingested = harvest(inbox, dest, plan.missing)
    cleared = IngestResult() if keep_inbox else clear_inbox(inbox)

    after = fetch_plan(state_dir, roots, only_source)
    state = FetchState(Path(state_dir) / "fetch_state.json")
    synced = _sync_state_with_plan(state, after)

    return {
        "inbox": inbox,
        "dest": dest,
        "before": plan.to_dict()["counts"],
        "after": after.to_dict()["counts"],
        "playlists": [run.to_dict() for run in runs],
        "moved": ingested.moved,
        "failed": ingested.failed,
        "cleared": cleared.cleared,
        "kept": cleared.kept,
        "state_sync": synced,
    }
