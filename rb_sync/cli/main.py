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

from .. import services
from ..sources import SourceError, display_name, source_names
from ..state import DEFAULT_STATE_DIR, StateStore

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


def cmd_plan(args: argparse.Namespace) -> int:
    store = StateStore(args.state_dir)

    try:
        result = services.run_plan(
            args.playlist,
            forced_source=args.source,
            name=args.name,
            state_dir=args.state_dir,
            refresh=args.refresh,
            append=args.append,
            cookie=args.cookie,
            timeout=args.timeout,
            interval=args.interval,
        )
    except SourceError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    for item in result.errors:
        print(f"错误：{item['error']}", file=sys.stderr)

    if args.json:
        print(
            json.dumps(
                {
                    "selection": str(result.selection_path) if result.selection_path else None,
                    "selection_count": result.selection_count,
                    "dropped_from_previous_selection": result.dropped_previous,
                    "changes": result.report,
                    "errors": result.errors,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _print_report(store, result.report, result.playlists, result.selection_path)
        if result.dropped_previous and not args.append:
            print()
            print(
                f"提示：上次选择里有 {result.dropped_previous} 个歌单不在本次范围内，"
                "已从选择配置中移除。如需累加，请加 --append。"
            )

    return 0 if result.ok else 1


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
    print("下一步：rb-sync fetch            # 算缺口、生成下载清单")
    print("        rb-sync serve            # 或者用图形界面")


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
    from ..rekordbox import RekordboxError, is_rekordbox_running

    try:
        report = services.inspect_library(args.db, args.state_dir)
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
    from ..rekordbox import RekordboxError

    try:
        plan, out_path = services.preview_relink(
            args.db,
            args.from_root,
            args.to_root,
            list(args.search or []),
            state_dir=args.state_dir,
            out=args.out,
        )
    except RekordboxError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if not plan.items:
        print("没有失效路径，无需重定位。")
        return 0

    if args.json:
        print(
            json.dumps(
                {"plan": plan.to_dict(), "out": str(out_path)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(f"失效曲目 : {len(plan.items)}")
    print(f"可映射   : {len(plan.matched)}")
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


def cmd_serve(args: argparse.Namespace) -> int:
    from ..web.server import serve

    try:
        serve(
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
            state_dir=args.state_dir,
        )
    except OSError as exc:
        print(
            f"错误：无法启动服务（{exc}）。\n"
            "  端口被占用时可以换一个：--port 8766",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


# ------------------------------------------------------------------- fetch


def parse_commands(raw: list[str] | None) -> dict[str, str] | None:
    """把 ``--command netease=...`` 解析成 {source: template}。"""
    if not raw:
        return None
    commands: dict[str, str] = {}
    for entry in raw:
        source, sep, template = entry.partition("=")
        if not sep or not source.strip() or not template.strip():
            raise ValueError(f"--command 需要写成 平台=命令，收到：{entry}")
        commands[source.strip()] = template.strip()
    return commands


def cmd_fetch(args: argparse.Namespace) -> int:
    roots = list(args.music_root or []) or None

    if args.verify:
        result = services.fetch_verify(args.state_dir, roots, args.source)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        counts = result["counts"]
        print(f"本地已有 : {counts['present']}")
        print(f"仍缺失   : {counts['missing']}")
        print(f"有歧义   : {counts['ambiguous']}")
        synced = result["state_sync"]
        print(f"状态更新 : 新标记已有 {synced['marked_present']}，标记丢失 {synced['lost']}")
        return 0

    try:
        plan = services.fetch_plan(args.state_dir, roots, args.source)
    except Exception as exc:  # noqa: BLE001 - 配置/IO 问题统一提示
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if not plan.items:
        print("还没有选择配置（state/selection.json 为空）。")
        print("先运行：rb-sync plan --playlist <歌单链接>")
        return 1

    counts = plan.to_dict()["counts"]
    manifest_dir = Path(args.manifest_dir) if args.manifest_dir else Path(args.state_dir) / "fetch"
    written: dict = {}
    if counts["missing"]:
        try:
            written = services.fetch_write_manifest(plan, manifest_dir)
        except OSError as exc:
            print(f"提示：清单写入失败（{exc}），继续。", file=sys.stderr)

    if args.json and not args.run:
        payload = plan.to_dict()
        payload["manifest"] = {k: str(v) for k, v in written.items()}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"歌单成员 : {counts['total']} 首（已按曲目去重）")
    print(f"曲库目录 : {', '.join(plan.music_roots) if plan.music_roots else '（未设置）'}")
    print(
        f"本地已有 : {counts['present']}    缺失 : {counts['missing']}    "
        f"有歧义 : {counts['ambiguous']}"
    )

    if not plan.music_roots:
        print()
        print("提示：还没设置曲库目录，所以全部曲目都会被判为缺失。")
        print("      WebUI「设置」或 config.local.json 里的 rekordbox.music_roots")

    if counts["missing"]:
        print()
        print("缺失（按平台）：")
        for source, items in plan.missing_by_source().items():
            target = written.get(source)
            suffix = f" → 清单 {target}" if target else ""
            print(f"  {source:<8}{len(items):>5} 首{suffix}")

    if counts["ambiguous"]:
        print()
        print("有歧义（同名文件多处命中，未计入下载）：")
        for item in plan.ambiguous[:10]:
            print(f"  {_truncate(item.track.title or '(无标题)', 40)}  "
                  f"{item.candidates} 个候选")
        if len(plan.ambiguous) > 10:
            print(f"  …… 另有 {len(plan.ambiguous) - 10} 条")

    if not args.run:
        print()
        if written:
            print("这是 handoff 模式：清单已生成，交给你的下载工具即可。")
        print("要执行外部下载命令，加 --run（需先在配置里为对应平台设置 fetch.commands）")
        return 0

    # ---- 真正执行外部命令 ----
    print()
    print("执行外部下载命令…")

    def progress(index: int, total: int, item, command: str) -> None:
        print(f"  [{index}/{total}] {item.track.title} - {item.track.artist_text}")

    try:
        outcome = services.fetch_run(
            state_dir=args.state_dir,
            music_roots=roots,
            only_source=args.source,
            out_dir=args.out,
            limit=args.limit,
            timeout=args.timeout,
            on_progress=progress,
            commands=parse_commands(args.command),
            force=args.force,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(outcome, ensure_ascii=False, indent=2))
        return 0 if not outcome.get("after", {}).get("missing") else 1

    print()
    for source, stats in outcome["per_source"].items():
        if stats["skipped_no_command"]:
            print(
                f"  {source}: {stats['skipped_no_command']} 首未执行 —— "
                "没有配置 fetch.commands（清单可直接喂给外部工具）"
            )
        elif stats["queued"]:
            print(f"  {source}: 成功 {stats['ok']} / 失败 {stats['failed']}")

    after = outcome["after"]
    print()
    print(f"重扫结果 : 本地已有 {after['present']}    仍缺失 {after['missing']}")
    if after["missing"]:
        print("仍缺失的可以重跑一次 fetch --run（已成功的会跳过）")
    return 0 if not after["missing"] else 1


# ------------------------------------------------------------------ argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rb-sync",
        description="Rekordbox 离线曲库同步编排器（多平台歌单 → 选择配置 → 映射 → 写库）",
    )
    parser.add_argument("--version", action="version", version="rb-sync 0.5.0")
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

    serve_cmd = sub.add_parser(
        "serve",
        help="启动本机 WebUI（W1：只读功能）",
        description=(
            "在本机 127.0.0.1 启动一个网页界面，用于设置库路径、读歌单、预览变更、"
            "体检与重定位预览。当前不包含写库功能。"
        ),
    )
    serve_cmd.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    serve_cmd.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    serve_cmd.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    serve_cmd.add_argument(
        "--state-dir", default=str(DEFAULT_STATE_DIR), help=f"状态目录（默认 {DEFAULT_STATE_DIR}）"
    )
    serve_cmd.set_defaults(func=cmd_serve)

    fetch_cmd = sub.add_parser(
        "fetch",
        help="算清缺哪些曲目，生成清单 / 调用外部下载命令",
        description=(
            "读取选择配置与歌单快照，扫曲库目录判断哪些曲目本地已有，"
            "输出缺失清单。加 --run 才会调用你在配置里指定的外部下载命令；"
            "项目本身不下载、不解密任何内容。"
        ),
    )
    fetch_cmd.add_argument(
        "--music-root",
        action="append",
        metavar="目录",
        help="曲库目录，可重复（默认用配置里的 rekordbox.music_roots）",
    )
    fetch_cmd.add_argument(
        "--source", default=None, choices=["netease", "qq", "kugou", "local"],
        help="只处理某个平台",
    )
    fetch_cmd.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    fetch_cmd.add_argument(
        "--manifest-dir", default=None, metavar="目录",
        help="缺失清单输出目录（默认 <state-dir>/fetch）",
    )
    fetch_cmd.add_argument("--out", default=None, metavar="目录",
                           help="外部命令的 {out} 占位符值 / 下载落点")
    fetch_cmd.add_argument("--limit", type=int, default=None, help="本次最多执行几条")
    fetch_cmd.add_argument("--timeout", type=float, default=None, help="单条命令超时秒数")
    fetch_cmd.add_argument("--run", action="store_true", help="真正执行外部下载命令")
    fetch_cmd.add_argument(
        "--force", action="store_true", help="忽略 fetch_state，重新执行（默认跳过已成功的）"
    )
    fetch_cmd.add_argument(
        "--command",
        action="append",
        metavar="平台=命令",
        help='临时指定某个平台的下载命令，例如 --command netease=\'yun "{url}" -o "{out}"\'',
    )
    fetch_cmd.add_argument("--verify", action="store_true", help="只重扫磁盘并更新状态")
    fetch_cmd.add_argument("--json", action="store_true", help="以 JSON 输出")
    fetch_cmd.set_defaults(func=cmd_fetch)
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
