"""rb-sync 命令行入口。

已实现：
    rb-sync plan    --playlist <链接|ID|本地文件> [...]   读歌单 → 选择配置
    rb-sync doctor  --db <master.db>                     库体检（只读，操作副本）
    rb-sync relink  --from <旧根> --to <新根>             失效路径重映射（只算不写）

支持网易云 / QQ音乐 / 酷狗 / 本地 CSV-JSON；链接自动识别平台。
后续阶段（接口位置已预留）：
    rb-sync fetch / convert / write
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path

from ..config import load_cookie
from ..mapping.diff import diff_snapshots
from ..model import Selection, dump_json
from ..sources import SourceError, build_source, detect, display_name, source_names
from ..state import DEFAULT_STATE_DIR, StateStore, now_iso

MAX_LISTED = 20


# --------------------------------------------------------------- 输出辅助


def _width(text: str) -> int:
    """终端显示宽度：CJK 全角字符算 2 列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    text = text or ""
    return text + " " * max(0, width - _width(text))


def _truncate(text: str, width: int) -> str:
    if _width(text) <= width:
        return text
    out, used = "", 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + w > width - 1:
            return out + "…"
        out += ch
        used += w
    return out


# ------------------------------------------------------------------ plan


def _resolve_targets(args: argparse.Namespace) -> list[tuple[str, str]]:
    """把 --playlist 的每一项解析成 (源名, 原始输入)。"""
    targets: list[tuple[str, str]] = []
    for raw in args.playlist:
        source_name = args.source or detect(raw)
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


def cmd_plan(args: argparse.Namespace) -> int:
    store = StateStore(args.state_dir)

    try:
        targets = _resolve_targets(args)
    except SourceError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if not targets:
        print("错误：至少需要一个 --playlist", file=sys.stderr)
        return 2

    if args.name and len(targets) != 1:
        print("错误：--name 只能用于单个 --playlist", file=sys.stderr)
        return 2

    adapters: dict[str, object] = {}
    report: list[dict] = []
    playlists: list[dict] = []
    failures = 0

    for source_name, raw in targets:
        adapter = adapters.get(source_name)
        if adapter is None:
            adapter = build_source(
                source_name,
                cookie=load_cookie(source_name, args.cookie),
                timeout=args.timeout,
                interval=args.interval,
            )
            adapters[source_name] = adapter

        try:
            playlist_id = adapter.resolve_id(raw)
            previous = None if args.refresh else store.load_snapshot(source_name, playlist_id)
            snapshot = adapter.fetch(
                playlist_id, previous=previous, refresh_metadata=args.refresh
            )
        except SourceError as exc:
            failures += 1
            print(f"错误：{exc}", file=sys.stderr)
            report.append({"source": source_name, "input": raw, "error": str(exc)})
            continue

        if args.name:
            snapshot.name = args.name

        change = diff_snapshots(previous, snapshot)
        store.save_snapshot(snapshot)

        playlists.append(
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
        report.append(change.to_dict())

    selection_path = None
    replaced = 0
    if playlists:
        previous_selection = store.load_selection()
        if args.append and previous_selection is not None:
            kept = [
                p
                for p in previous_selection.playlists
                if (str(p.get("source")), str(p.get("id")))
                not in {(p2["source"], p2["id"]) for p2 in playlists}
            ]
            playlists = kept + playlists
        elif previous_selection is not None:
            new_keys = {(p["source"], p["id"]) for p in playlists}
            replaced = sum(
                1
                for p in previous_selection.playlists
                if (str(p.get("source")), str(p.get("id"))) not in new_keys
            )
        selection_path = store.save_selection(
            Selection(playlists=playlists, generated_at=now_iso())
        )

    if args.json:
        print(
            json.dumps(
                {
                    "selection": str(selection_path) if selection_path else None,
                    "selection_count": len(playlists),
                    "dropped_from_previous_selection": replaced,
                    "changes": report,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _print_report(store, report, playlists, selection_path)
        if replaced and not args.append:
            print()
            print(
                f"提示：上次选择里有 {replaced} 个歌单不在本次范围内，已从选择配置中移除。"
                "如需累加，请加 --append。"
            )

    return 0 if not failures else 1


def _print_report(
    store: StateStore,
    report: list[dict],
    playlists: list[dict],
    selection_path: Path | None,
) -> None:
    print(f"状态目录：{store.root.resolve()}")
    print()

    if not playlists:
        print("（本次没有成功读取的歌单，未写入任何快照）")
        return

    multi_source = len({p["source"] for p in playlists}) > 1
    name_w = max([28] + [_width(p["name"]) for p in playlists]) + 2
    src_w = 12 if multi_source else 0

    if multi_source:
        print(f"  {_pad('源', src_w)}{_pad('歌单', name_w)}{_pad('曲目', 6)}变更")
        print("  " + "─" * (src_w + name_w + 6 + 12))
    else:
        print(f"  {_pad('歌单', name_w)}{_pad('曲目', 6)}变更")
        print("  " + "─" * (name_w + 6 + 12))

    for item in report:
        if "error" in item:
            continue
        name = _truncate(item["playlist_name"], name_w - 2)
        prefix = _pad(display_name(item["source"]), src_w) if multi_source else ""
        status = _status_text(item)
        count = next(
            (
                p["track_count"]
                for p in playlists
                if p["id"] == item["playlist_id"] and p["source"] == item["source"]
            ),
            0,
        )
        print(f"  {prefix}{_pad(name, name_w)}{_pad(str(count), 6)}{status}")

    for item in report:
        if "error" in item:
            continue
        if not (item["added"] or item["removed"] or item["renamed"] or item["name_changed"]):
            continue
        title = _truncate(item["playlist_name"], 40)
        print()
        if item["name_changed"]:
            old, new = item["name_changed"]
            print(f"  「{old}」已改名为「{new}」")
        if item["added"]:
            label = "曲目" if item["first_seen"] else "新增"
            print(f"  【{label}】{title}")
            for track in item["added"][:MAX_LISTED]:
                print(f"    + {_format_track(track)}")
            if len(item["added"]) > MAX_LISTED:
                print(f"    …… 另有 {len(item['added']) - MAX_LISTED} 首")
        if item["removed"]:
            print(f"  【移除】{title}")
            for track in item["removed"][:MAX_LISTED]:
                print(f"    - {_format_track(track)}")
            if len(item["removed"]) > MAX_LISTED:
                print(f"    …… 另有 {len(item['removed']) - MAX_LISTED} 首")
        if item["renamed"]:
            print(f"  【曲名变动】{title}")
            for rename in item["renamed"][:MAX_LISTED]:
                print(f"    ~ {rename['old_name']} → {rename['new_name']}")

    print()
    print(f"选择配置：{selection_path.resolve() if selection_path else '-'}")
    print(f"歌单快照：{store.playlists_dir.resolve()}")
    print()
    print("下一步（尚未实现）：rb-sync fetch   # 按选择配置拉取缺失曲目")


def _status_text(item: dict) -> str:
    if item["first_seen"]:
        return f"首次读取（{len(item['added'])} 首）"
    if item["unchanged"]:
        return "无变化"
    bits = []
    if item["added"]:
        bits.append(f"+{len(item['added'])}")
    if item["removed"]:
        bits.append(f"-{len(item['removed'])}")
    if item["reordered"]:
        bits.append(f"顺序变动 {item['moved_count']}")
    if item["renamed"]:
        bits.append(f"改名 {len(item['renamed'])}")
    if item["became_unavailable"]:
        bits.append(f"变灰 {len(item['became_unavailable'])}")
    if item["name_changed"]:
        bits.append("歌单改名")
    return " ".join(bits) or "有变化"


def _format_track(track: dict) -> str:
    artists = " / ".join(track.get("artists") or ()) or "<未知艺人>"
    name = track.get("name") or "<未知曲名>"
    return f"{name} - {artists}"


# ------------------------------------------------------------------ doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    from ..rekordbox import Library, RekordboxError, is_rekordbox_running

    try:
        with Library(args.db, cache_dir=Path(args.state_dir) / ".cache") as lib:
            report = lib.inspect()
    except RekordboxError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    running = is_rekordbox_running()
    if args.json:
        payload = report.to_dict()
        payload["rekordbox_running"] = running
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    _print_doctor(report, running)
    return 0


def _print_doctor(report, running: bool) -> None:
    print(f"库文件 : {report.db_path}")
    print(f"         {report.db_size:,} 字节   最后写入 {report.db_mtime}")
    print(f"完整性 : {report.integrity}    usn: {report.usn}")
    print(
        f"曲目   : {len(report.tracks):,}   自带素材 {len(report.factory_tracks)} / "
        f"文件在 {len(report.present):,} / 缺失 {len(report.missing):,}"
    )
    print(f"Rekordbox 进程 : {'运行中（此刻不能写库）' if running else '未运行'}")
    print()

    folders = {p.id: p for p in report.playlists if p.is_folder}
    print("歌单结构：")
    for folder in folders.values():
        children = [p for p in report.playlists if p.parent_id == folder.id]
        print(f"  [{folder.name}]  ({len(children)} 个歌单)")
        for child in children:
            print(f"    {_pad(_truncate(child.name, 40), 42)}{child.song_count:>5} 首")
    root_playlists = [
        p for p in report.playlists if not p.is_folder and p.parent_id not in folders
    ]
    if root_playlists:
        print("  [根级]")
        for child in root_playlists:
            print(f"    {_pad(_truncate(child.name, 40), 42)}{child.song_count:>5} 首")

    missing = report.missing
    if missing:
        print()
        print(f"缺失文件分布（共 {len(missing)} 首，按前三级目录归类）：")
        buckets: dict[str, int] = {}
        for track in missing:
            parts = track.path.replace("\\", "/").split("/")
            key = "/".join(parts[:3]) + "/..."
            buckets[key] = buckets.get(key, 0) + 1
        for key, count in sorted(buckets.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {count:>5}  {key}")
        print()
        print("下一步：rb-sync relink --from <旧根> --to <新根>   # 先算映射，不写库")


# ------------------------------------------------------------------ relink


def cmd_relink(args: argparse.Namespace) -> int:
    from ..rekordbox import Library, RekordboxError, build_plan

    try:
        with Library(args.db, cache_dir=Path(args.state_dir) / ".cache") as lib:
            report = lib.inspect()
    except RekordboxError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    missing = report.missing
    if not missing:
        print("没有失效路径，无需重定位。")
        return 0

    search_roots = list(args.search or [])
    if not search_roots:
        search_roots = [args.to_root]

    plan = build_plan(missing, args.from_root, args.to_root, search_roots)
    out_path = Path(args.out)
    dump_json(out_path, plan.to_dict())

    if args.json:
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return 0

    matched = len(plan.matched)
    print(f"失效曲目 : {len(missing)}")
    print(f"可映射   : {matched}")
    for strategy, count in sorted(plan.by_strategy().items()):
        note = "（换了扩展名，音质可能不同）" if strategy == "basename_ext" else ""
        print(f"    {strategy:<14}{count:>4}  {note}")
    print(f"歧义     : {len(plan.ambiguous)}")
    print(f"未匹配   : {len(plan.unmatched)}")
    print()
    print(f"重定位计划（dry-run，未修改任何库）：{out_path.resolve()}")

    if plan.unmatched:
        print()
        print("未匹配（前 15 条，需要人工处理）：")
        for item in plan.unmatched[:15]:
            print(f"  {_truncate(item.title or '(无标题)', 46)}  {_truncate(item.old_path, 60)}")
        if len(plan.unmatched) > 15:
            print(f"  …… 另有 {len(plan.unmatched) - 15} 条")
    if plan.ambiguous:
        print()
        print("歧义（同名文件多处命中，不猜）：")
        for item in plan.ambiguous[:10]:
            print(f"  {_truncate(item.title or '(无标题)', 46)}  {_truncate(item.old_path, 60)}")

    print()
    print("写库（--apply）尚未实现 —— 先把映射表给你过一遍。")
    return 0


# ------------------------------------------------------------------ argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rb-sync",
        description="Rekordbox 离线曲库同步编排器（多平台歌单 → 选择配置 → 映射 → 写库）",
    )
    parser.add_argument("--version", action="version", version="rb-sync 0.3.0")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser(
        "plan",
        help="读取歌单并生成/更新选择配置",
        description=(
            "支持网易云 / QQ音乐 / 酷狗 / 本地 CSV-JSON。"
            "粘贴歌单链接会自动识别平台；纯数字 ID 需要 --source。"
        ),
    )
    plan.add_argument(
        "--playlist",
        action="append",
        required=True,
        metavar="链接|ID|文件",
        help="歌单链接、纯数字 ID 或本地 .csv/.json，可重复",
    )
    plan.add_argument(
        "--source",
        choices=source_names(),
        default=None,
        help="强制指定平台（不给则按链接域名/文件自动识别）",
    )
    plan.add_argument("--name", default=None, help="覆盖歌单名（仅单个 --playlist 可用）")
    plan.add_argument(
        "--append",
        action="store_true",
        help="把本次歌单追加到已有选择配置（默认是整体替换）",
    )
    plan.add_argument(
        "--state-dir",
        default=str(DEFAULT_STATE_DIR),
        help=f"状态目录（默认 {DEFAULT_STATE_DIR}）",
    )
    plan.add_argument(
        "--refresh", action="store_true", help="忽略本地快照缓存，重新抓取全部曲目元数据"
    )
    plan.add_argument("--cookie", default=None, help="该平台 Cookie（私密歌单才需要）")
    plan.add_argument("--timeout", type=float, default=25.0, help="单次请求超时秒数")
    plan.add_argument("--interval", type=float, default=None, help="请求间隔秒数（防风控）")
    plan.add_argument("--json", action="store_true", help="以 JSON 输出变更报告")
    plan.set_defaults(func=cmd_plan)

    doctor = sub.add_parser(
        "doctor",
        help="体检 Rekordbox 库（只读；始终操作副本）",
        description="打印库路径、完整性、曲目/歌单结构、失效文件分布。不会修改库。",
    )
    _add_db_args(doctor)
    doctor.set_defaults(func=cmd_doctor)

    relink = sub.add_parser(
        "relink",
        help="失效路径重映射（只算不写）",
        description=(
            "把库里失效的绝对路径映射到文件新位置：先试「旧根→新根」，"
            "再试「同名文件（可忽略扩展名）」。只生成计划，不修改库。"
        ),
    )
    _add_db_args(relink)
    relink.add_argument("--from", dest="from_root", required=True, metavar="旧根",
                        help="库里记录的旧路径前缀，如 E:/Music")
    relink.add_argument("--to", dest="to_root", required=True, metavar="新根",
                        help="文件现在所在的前缀，如 F:/Music")
    relink.add_argument("--search", action="append", metavar="目录",
                        help="同名匹配的搜索目录，可重复（默认就是 --to）")
    relink.add_argument("--out", default="state/relink_plan.json",
                        metavar="文件", help="重定位计划输出路径")
    relink.set_defaults(func=cmd_relink)
    return parser


def _add_db_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=None,
        metavar="master.db",
        help="Rekordbox 库路径（默认 %%APPDATA%%\\Pioneer\\rekordbox\\master.db）",
    )
    parser.add_argument(
        "--state-dir",
        default=str(DEFAULT_STATE_DIR),
        help=f"状态目录（默认 {DEFAULT_STATE_DIR}）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
