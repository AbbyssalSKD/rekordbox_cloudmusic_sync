"""fetch 阶段的离线测试：索引匹配、清单、外部命令适配、增量状态。"""

from __future__ import annotations

import contextlib
import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path

from rb_sync import services
from rb_sync.downloader import (
    AudioIndex,
    FetchState,
    build_fetch_plan,
    load_selection_tracks,
    render_command,
    run_items,
    scan_audio_files,
    track_url,
    write_manifest,
)
from rb_sync.downloader.plan import SelectionTrack
from rb_sync.model import PlaylistSnapshot, Selection, TrackRef
from rb_sync.state import StateStore

TMP_ROOT = Path(__file__).resolve().parent / "_tmp"


@contextlib.contextmanager
def temp_dir():
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TMP_ROOT / f"fetch-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def make_track(tid: str, name: str, artists: tuple[str, ...]) -> TrackRef:
    return TrackRef(id=tid, name=name, artists=artists, album="", duration_ms=180000)


def write_selection(state_dir: Path, playlists: list[tuple[str, str, str, list[TrackRef]]]) -> None:
    """playlists: [(source, id, name, tracks)]"""
    store = StateStore(state_dir)
    entries = []
    for source, pid, name, tracks in playlists:
        snapshot = PlaylistSnapshot(
            source=source, playlist_id=pid, name=name, tracks=list(tracks),
            track_count=len(tracks),
        )
        store.save_snapshot(snapshot)
        entries.append(
            {
                "source": source,
                "id": pid,
                "name": name,
                "track_count": len(tracks),
                "snapshot": store.snapshot_path(source, pid).as_posix(),
            }
        )
    store.save_selection(Selection(playlists=entries, generated_at="2026-11-06T00:00:00+08:00"))


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    return path


class TestTrackUrl(unittest.TestCase):
    def test_per_source(self):
        self.assertEqual(track_url("netease", "123"), "https://music.163.com/song?id=123")
        self.assertIn("songDetail/abc", track_url("qq", "abc"))
        self.assertIn("hash=ABC", track_url("kugou", "ABC"))
        self.assertEqual(track_url("local", "local-1"), "")


class TestSelectionTracks(unittest.TestCase):
    def test_same_track_in_two_playlists_is_merged(self):
        with temp_dir() as tmp:
            state = tmp / "state"
            track = make_track("1", "Strobe", ("deadmau5",))
            write_selection(
                state,
                [
                    ("netease", "A", "electro house", [track]),
                    ("netease", "B", "future house", [track]),
                ],
            )
            tracks = load_selection_tracks(state)
            self.assertEqual(len(tracks), 1)
            self.assertEqual(tracks[0].playlists, ["electro house", "future house"])

    def test_missing_snapshot_is_skipped(self):
        with temp_dir() as tmp:
            state = tmp / "state"
            store = StateStore(state)
            store.save_selection(
                Selection(
                    playlists=[{"source": "netease", "id": "9", "name": "gone", "snapshot": "nope.json"}],
                )
            )
            self.assertEqual(load_selection_tracks(state), [])

    def test_empty_selection(self):
        with temp_dir() as tmp:
            self.assertEqual(load_selection_tracks(tmp / "nothing"), [])


class TestMatching(unittest.TestCase):
    def test_filename_artist_title(self):
        with temp_dir() as tmp:
            music = tmp / "music"
            target = touch(music / "deadmau5 - Strobe.flac")
            index = AudioIndex.from_roots([music])
            track = SelectionTrack("netease", "1", "Strobe", ("deadmau5",))
            match = index.lookup(track)
            self.assertTrue(match.found)
            self.assertEqual(Path(match.file.path), target)
            self.assertEqual(match.strategy, "filename_artist_title")
            self.assertEqual(match.candidates, 1)
            self.assertFalse(match.encrypted)

    def test_title_match_requires_artist(self):
        """回归：只比曲名会把「颜人中 - My love」错配到「MK (JPN) - My Love.flac」。"""
        with temp_dir() as tmp:
            music = tmp / "music"
            touch(music / "MK (JPN) - My Love.flac")
            index = AudioIndex.from_roots([music])
            other = SelectionTrack("netease", "2", "My love", ("颜人中",))
            match = index.lookup(other)
            self.assertFalse(match.found, "艺人不同，不该判定为已有")

    def test_title_match_with_artist_present(self):
        """原曲在，只是文件名带后缀（Extended Mix）时仍能对上。"""
        with temp_dir() as tmp:
            music = tmp / "music"
            touch(music / "Artist - Song (Extended Mix).flac")
            index = AudioIndex.from_roots([music])
            track = SelectionTrack("netease", "3", "Song", ("Artist",))
            match = index.lookup(track)
            self.assertTrue(match.found)
            self.assertEqual(match.strategy, "title_in_filename")

    def test_ambiguous_is_reported_not_guessed(self):
        with temp_dir() as tmp:
            music = tmp / "music"
            touch(music / "a" / "Artist - Song.flac")
            touch(music / "b" / "Artist - Song.flac")
            index = AudioIndex.from_roots([music])
            track = SelectionTrack("netease", "4", "Song", ("Artist",))
            match = index.lookup(track)
            self.assertFalse(match.found)
            self.assertEqual(match.candidates, 2)

    def test_encrypted_only_is_marked_encrypted(self):
        """.ncm 能被扫到，但**不能**算"已有"——Rekordbox 放不了它。"""
        with temp_dir() as tmp:
            music = tmp / "music"
            touch(music / "Artist - Song.ncm")
            index = AudioIndex.from_roots([music])
            match = index.lookup(SelectionTrack("netease", "5", "Song", ("Artist",)))
            self.assertTrue(match.found)
            self.assertTrue(match.encrypted)

    def test_playable_wins_over_encrypted(self):
        """同一首既有 .flac 又有 .ncm 时，算已有（可播放的优先）。"""
        with temp_dir() as tmp:
            music = tmp / "music"
            touch(music / "Artist - Song.ncm")
            target = touch(music / "Artist - Song.flac")
            index = AudioIndex.from_roots([music])
            match = index.lookup(SelectionTrack("netease", "6", "Song", ("Artist",)))
            self.assertTrue(match.found)
            self.assertFalse(match.encrypted)
            self.assertEqual(Path(match.file.path), target)

    def test_non_audio_files_ignored(self):
        with temp_dir() as tmp:
            music = tmp / "music"
            touch(music / "Artist - Song.jpg")
            touch(music / "Artist - Song.txt")
            self.assertEqual(scan_audio_files([music]), [])

    def test_missing_root_is_tolerated(self):
        self.assertEqual(scan_audio_files(["Z:\\definitely\\not\\here"]), [])


class TestPlanAndManifest(unittest.TestCase):
    def test_counts(self):
        with temp_dir() as tmp:
            state = tmp / "state"
            music = tmp / "music"
            touch(music / "Have - One.flac")
            write_selection(
                state,
                [
                    (
                        "netease",
                        "A",
                        "mix",
                        [
                            make_track("1", "One", ("Have",)),
                            make_track("2", "Two", ("Missing",)),
                        ],
                    )
                ],
            )
            plan = build_fetch_plan(state, [music])
            counts = plan.to_dict()["counts"]
            self.assertEqual(
                counts,
                {"total": 2, "present": 1, "missing": 1, "encrypted": 0, "ambiguous": 0},
            )
            self.assertEqual(plan.missing[0].track.title, "Two")

    def test_encrypted_is_its_own_bucket(self):
        """.ncm 不算 present，也不算 missing，单独一档「待解密」。"""
        with temp_dir() as tmp:
            state = tmp / "state"
            music = tmp / "music"
            touch(music / "Artist - Song.ncm")
            write_selection(
                state, [("netease", "A", "mix", [make_track("1", "Song", ("Artist",))])]
            )
            plan = build_fetch_plan(state, [music])
            counts = plan.to_dict()["counts"]
            self.assertEqual(counts["present"], 0)
            self.assertEqual(counts["missing"], 0)
            self.assertEqual(counts["encrypted"], 1)
            self.assertEqual(len(plan.encrypted), 1)
            self.assertTrue(plan.encrypted[0].matched_path.endswith(".ncm"))

    def test_manifest_per_source(self):
        with temp_dir() as tmp:
            state = tmp / "state"
            write_selection(
                state,
                [("netease", "A", "mix", [make_track("9", "Nine", ("X",))])],
            )
            plan = build_fetch_plan(state, [tmp / "empty"])
            written = write_manifest(plan, tmp / "out")
            self.assertIn("netease", written)
            text = written["netease"].read_text(encoding="utf-8")
            self.assertIn("https://music.163.com/song?id=9", text)
            self.assertIn("# mix", text)


class TestRunner(unittest.TestCase):
    def test_render_placeholders(self):
        track = SelectionTrack("netease", "42", "Song", ("Artist A", "Artist B"))
        track.playlists = ["mix"]
        item = type("Item", (), {"track": track, "uid": track.uid})()
        command = render_command(
            'tool "{url}" --id {id} --name "{name}" --artists "{artists}" --out "{out}"',
            item,
            out_dir="D:\\out",
        )
        self.assertIn("https://music.163.com/song?id=42", command)
        self.assertIn('--name "Song"', command)
        self.assertIn('--artists "Artist A / Artist B"', command)
        self.assertIn('--out "D:\\out"', command)

    def test_run_items_success_and_failure(self):
        with temp_dir() as tmp:
            script = tmp / "fake_download.py"
            script.write_text(
                "import sys, pathlib\n"
                "out = sys.argv[1]\n"
                "name = sys.argv[2]\n"
                "pathlib.Path(out).mkdir(parents=True, exist_ok=True)\n"
                "if name == 'FAIL':\n"
                "    print('boom', file=sys.stderr); sys.exit(3)\n"
                "pathlib.Path(out, name + '.flac').write_text('x')\n",
                encoding="utf-8",
            )
            template = f'"{sys.executable}" "{script}" "{{out}}" "{{name}}"'

            ok_track = SelectionTrack("netease", "1", "Good", ("A",))
            bad_track = SelectionTrack("netease", "2", "FAIL", ("B",))
            items = [type("I", (), {"track": ok_track, "uid": ok_track.uid})(),
                     type("I", (), {"track": bad_track, "uid": bad_track.uid})()]

            summary = run_items(template, items, out_dir=str(tmp / "out"), timeout=60)
            self.assertEqual(summary.ok_count, 1)
            self.assertEqual(len(summary.failed), 1)
            self.assertEqual(summary.results[1].returncode, 3)
            self.assertIn("boom", summary.results[1].stderr)

    def test_non_utf8_default_output_does_not_crash(self):
        """外部工具输出编码不可控，不能因为解码失败而丢掉结果。"""
        with temp_dir() as tmp:
            script = tmp / "say.py"
            script.write_text(
                "import sys\nsys.stdout.buffer.write('中文输出'.encode('utf-8'))\n",
                encoding="utf-8",
            )
            template = f'"{sys.executable}" "{script}"'
            track = SelectionTrack("netease", "1", "X", ("A",))
            items = [type("I", (), {"track": track, "uid": track.uid})()]
            summary = run_items(template, items, out_dir=str(tmp), timeout=60)
            self.assertEqual(summary.ok_count, 1)
            self.assertIn("中文输出", summary.results[0].stdout)


class TestFetchState(unittest.TestCase):
    def test_roundtrip_and_skip(self):
        with temp_dir() as tmp:
            path = tmp / "fetch_state.json"
            existing = touch(tmp / "music" / "A - B.flac")
            state = FetchState(path)
            self.assertFalse(state.can_skip("netease:1"))
            state.mark("netease:1", "present", path=str(existing))
            state.save()

            reloaded = FetchState(path)
            self.assertTrue(reloaded.can_skip("netease:1"))
            self.assertEqual(reloaded.status_of("netease:1"), "present")

            existing.unlink()
            self.assertFalse(reloaded.can_skip("netease:1"), "文件没了就不能跳过")


class TestFetchRun(unittest.TestCase):
    def test_run_then_rescan_marks_downloaded(self):
        with temp_dir() as tmp:
            state = tmp / "state"
            music = tmp / "music"
            music.mkdir()
            out = tmp / "out"
            write_selection(
                state,
                [("netease", "A", "mix", [make_track("7", "Seven", ("Artist",))])],
            )
            script = tmp / "dl.py"
            script.write_text(
                "import sys, pathlib\n"
                "out, artists, name = sys.argv[1], sys.argv[2], sys.argv[3]\n"
                "pathlib.Path(out).mkdir(parents=True, exist_ok=True)\n"
                "pathlib.Path(out, artists + ' - ' + name + '.flac').write_text('x')\n",
                encoding="utf-8",
            )
            template = f'"{sys.executable}" "{script}" "{{out}}" "{{artists}}" "{{name}}"'

            outcome = services.fetch_run(
                state_dir=state,
                music_roots=[str(out)],  # 下载落点同时作为曲库目录
                out_dir=str(out),
                commands={"netease": template},
            )
            self.assertEqual(outcome["queued"], 1)
            self.assertEqual(outcome["per_source"]["netease"]["ok"], 1)
            self.assertEqual(outcome["after"]["present"], 1)
            self.assertEqual(outcome["after"]["missing"], 0)

            saved = json.loads((state / "fetch_state.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["items"]["netease:7"]["status"], "present")

    def test_downloaded_but_not_found_is_reported_unverified(self):
        """命令返回 0 但文件没落盘 / 文件名不含艺人 → 记 downloaded，但要报 unverified。"""
        with temp_dir() as tmp:
            state = tmp / "state"
            out = tmp / "out"
            out.mkdir()
            write_selection(
                state,
                [("netease", "A", "mix", [make_track("7", "Seven", ("Artist",))])],
            )
            # 命令成功但什么都不做（模拟"下到了别的地方"）
            template = f'"{sys.executable}" -c "print(1)"'

            outcome = services.fetch_run(
                state_dir=state,
                music_roots=[str(out)],
                out_dir=str(out),
                commands={"netease": template},
            )
            self.assertEqual(outcome["per_source"]["netease"]["ok"], 1)
            self.assertEqual(outcome["after"]["present"], 0)
            self.assertEqual(len(outcome["unverified"]), 1)

            saved = json.loads((state / "fetch_state.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["items"]["netease:7"]["status"], "downloaded")

    def test_no_command_means_handoff(self):
        with temp_dir() as tmp:
            state = tmp / "state"
            write_selection(
                state, [("netease", "A", "mix", [make_track("8", "Eight", ("X",))])]
            )
            outcome = services.fetch_run(
                state_dir=state, music_roots=[str(tmp / "empty")], commands={}
            )
            self.assertEqual(outcome["queued"], 1)
            self.assertEqual(outcome["per_source"]["netease"]["skipped_no_command"], 1)
            self.assertEqual(outcome["after"]["missing"], 1)


class TestPostCommand(unittest.TestCase):
    """后处理钩子：项目只负责"跑你给的命令"，不关心它做什么。"""

    def _download_script(self, tmp: Path) -> Path:
        script = tmp / "dl_ncm.py"
        script.write_text(
            "import sys, pathlib\n"
            "out, artists, name = sys.argv[1], sys.argv[2], sys.argv[3]\n"
            "pathlib.Path(out).mkdir(parents=True, exist_ok=True)\n"
            "pathlib.Path(out, artists + ' - ' + name + '.ncm').write_text('enc')\n",
            encoding="utf-8",
        )
        return script

    def test_post_command_turns_encrypted_into_present(self):
        with temp_dir() as tmp:
            state = tmp / "state"
            out = tmp / "out"
            out.mkdir()
            write_selection(
                state, [("netease", "A", "mix", [make_track("7", "Seven", ("Artist",))])]
            )
            dl = self._download_script(tmp)
            # 模拟"你自己的解密工具"：.ncm → .flac
            post = tmp / "decrypt.py"
            post.write_text(
                "import sys, pathlib\n"
                "f = pathlib.Path(sys.argv[1])\n"
                "f.with_suffix('.flac').write_text('audio')\n"
                "f.unlink()\n",
                encoding="utf-8",
            )
            template = f'"{sys.executable}" "{dl}" "{{out}}" "{{artists}}" "{{name}}"'
            post_template = f'"{sys.executable}" "{post}" "{{file}}"'

            outcome = services.fetch_run(
                state_dir=state,
                music_roots=[str(out)],
                out_dir=str(out),
                commands={"netease": template},
                post_command=post_template,
            )

            self.assertEqual(outcome["encrypted"]["before"], 1)
            self.assertEqual(outcome["encrypted"]["after"], 0)
            self.assertEqual(outcome["after"]["present"], 1)
            self.assertEqual(outcome["after"]["missing"], 0)
            self.assertTrue(outcome["encrypted"]["post"][0]["ok"])
            self.assertFalse((out / "Artist - Seven.ncm").exists())
            self.assertTrue((out / "Artist - Seven.flac").exists())

    def test_without_post_command_encrypted_stays_pending(self):
        with temp_dir() as tmp:
            state = tmp / "state"
            out = tmp / "out"
            out.mkdir()
            write_selection(
                state, [("netease", "A", "mix", [make_track("8", "Eight", ("Artist",))])]
            )
            dl = self._download_script(tmp)
            template = f'"{sys.executable}" "{dl}" "{{out}}" "{{artists}}" "{{name}}"'

            outcome = services.fetch_run(
                state_dir=state,
                music_roots=[str(out)],
                out_dir=str(out),
                commands={"netease": template},
                post_command="",  # 明确不配
            )

            self.assertEqual(outcome["encrypted"]["before"], 1)
            self.assertEqual(outcome["encrypted"]["after"], 1)
            self.assertFalse(outcome["encrypted"]["post_configured"])

            saved = json.loads((state / "fetch_state.json").read_text(encoding="utf-8"))
            # 关键：不能被记成 downloaded，否则下次会被跳过、永远不解密
            self.assertEqual(saved["items"]["netease:8"]["status"], "encrypted")


if __name__ == "__main__":
    unittest.main()
