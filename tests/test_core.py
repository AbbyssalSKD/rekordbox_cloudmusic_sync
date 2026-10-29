"""离线单元测试（不联网）：python -m unittest discover -s tests

覆盖：三个平台的 ID 解析、酷狗 filename 拆分、平台自动识别、
快照差异、跨平台曲目匹配、本地文件源、状态库读写。
"""

from __future__ import annotations

import contextlib
import csv
import shutil
import unittest
import uuid
from pathlib import Path

from rb_sync.mapping.diff import diff_snapshots
from rb_sync.mapping.match import normalize, same_track, track_key
from rb_sync.model import PlaylistSnapshot, Selection, TrackRef, dump_json, load_json
from rb_sync.sources import detect
from rb_sync.sources.base import HttpSource, content_hash
from rb_sync.sources.kugou import KugouSource, split_filename
from rb_sync.sources.netease import NeteaseSource
from rb_sync.sources.qq import QqSource
from rb_sync.state import StateStore

# 临时目录建在仓库内：系统 TEMP 在受限环境下不允许清理。
# 用 mkdir 而不是 mkdtemp —— mkdtemp 会建 0o700 目录，后续写入会被拒。
TMP_ROOT = Path(__file__).resolve().parent / "_tmp"


@contextlib.contextmanager
def temp_dir():
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TMP_ROOT / f"case-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def make_snapshot(
    ids: list[str], *, name: str = "electro house", source: str = "netease", pid: str = "42"
) -> PlaylistSnapshot:
    tracks = [
        TrackRef(id=i, name=f"track-{i}", artists=("artist",), album="album", duration_ms=180000)
        for i in ids
    ]
    return PlaylistSnapshot(
        source=source,
        playlist_id=pid,
        name=name,
        tracks=tracks,
        track_count=len(tracks),
        content_hash=content_hash(ids),
        fetched_at="2026-10-28T00:00:00+08:00",
    )


class TestResolveIds(unittest.TestCase):
    def test_netease(self):
        src = NeteaseSource()
        cases = {
            "https://music.163.com/playlist?id=123456": "123456",
            "https://music.163.com/#/playlist?id=123456": "123456",
            "https://y.music.163.com/m/playlist?id=123456&userid=1": "123456",
            "https://music.163.com/playlist/123456": "123456",
            " 123456 ": "123456",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(src.resolve_id(text), expected)

    def test_qq(self):
        src = QqSource()
        cases = {
            "https://y.qq.com/n/ryqq/playlist/7707261125": "7707261125",
            "https://i.y.qq.com/n2/m/share/details/taoge.html?id=7707261125": "7707261125",
            "https://y.qq.com/n/ryqq/playlist?disstid=7707261125": "7707261125",
            "7707261125": "7707261125",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(src.resolve_id(text), expected)

    def test_kugou(self):
        src = KugouSource()
        cases = {
            "https://www.kugou.com/yy/special/single/1063852.html": "1063852",
            "https://m.kugou.com/plist/list/1063852?json=true": "1063852",
            "https://www.kugou.com/song/#specialid=1063852": "1063852",
            "1063852": "1063852",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(src.resolve_id(text), expected)


class TestDetect(unittest.TestCase):
    def test_hosts(self):
        cases = {
            "https://music.163.com/playlist?id=1": "netease",
            "https://y.music.163.com/m/playlist?id=1": "netease",
            "https://y.qq.com/n/ryqq/playlist/1": "qq",
            "https://i.y.qq.com/n2/m/share/details/taoge.html?id=1": "qq",
            "https://www.kugou.com/yy/special/single/1.html": "kugou",
            "https://m.kugou.com/plist/list/1": "kugou",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(detect(text), expected)

    def test_ambiguous_number_returns_none(self):
        # 纯数字在三个平台间有歧义，必须靠 --source
        self.assertIsNone(detect("123456"))


class TestKugouFilename(unittest.TestCase):
    def test_single_artist(self):
        self.assertEqual(
            split_filename("Alan Walker - Forever Young"), (("Alan Walker",), "Forever Young")
        )

    def test_multi_artist(self):
        self.assertEqual(
            split_filename("Steve Aoki、Alan Walker、ISÁK、徐梦圆 - Are You Lonely (YUAN Remix)"),
            (("Steve Aoki", "Alan Walker", "ISÁK", "徐梦圆"), "Are You Lonely (YUAN Remix)"),
        )

    def test_no_separator(self):
        self.assertEqual(split_filename("只有曲名"), ((), "只有曲名"))

    def test_title_containing_dash(self):
        artists, title = split_filename("A - B - C")
        self.assertEqual(artists, ("A",))
        self.assertEqual(title, "B - C")


class TestContentHash(unittest.TestCase):
    def test_stable_and_order_sensitive(self):
        self.assertEqual(content_hash(["a", "b"]), content_hash(["a", "b"]))
        self.assertNotEqual(content_hash(["a", "b"]), content_hash(["b", "a"]))
        self.assertNotEqual(content_hash(["a", "b"]), content_hash(["a", "b", "c"]))


class TestDiff(unittest.TestCase):
    def test_first_seen(self):
        change = diff_snapshots(None, make_snapshot(["1", "2", "3"]))
        self.assertTrue(change.first_seen)
        self.assertEqual([t.id for t in change.added], ["1", "2", "3"])

    def test_unchanged(self):
        change = diff_snapshots(make_snapshot(["1", "2", "3"]), make_snapshot(["1", "2", "3"]))
        self.assertFalse(change.has_changes)
        self.assertEqual(change.summary(), "无变化")

    def test_add_remove(self):
        change = diff_snapshots(
            make_snapshot(["1", "2", "3"]), make_snapshot(["1", "3", "4", "5"])
        )
        self.assertEqual([t.id for t in change.added], ["4", "5"])
        self.assertEqual([t.id for t in change.removed], ["2"])
        self.assertFalse(change.reordered)
        self.assertEqual(change.summary(), "+2 -1")

    def test_reorder_only(self):
        change = diff_snapshots(make_snapshot(["1", "2", "3"]), make_snapshot(["3", "1", "2"]))
        self.assertTrue(change.reordered)
        self.assertEqual(change.moved_count, 3)
        self.assertFalse(change.added)

    def test_rename_and_unavailable(self):
        old = make_snapshot(["1", "2"])
        new = make_snapshot(["1", "2"])
        new.tracks[0] = TrackRef(id="1", name="new-name", artists=("artist",))
        new.tracks[1] = TrackRef(id="2", name="track-2", artists=("artist",), available=False)
        change = diff_snapshots(old, new)
        self.assertEqual(len(change.renamed), 1)
        self.assertEqual([t.id for t in change.became_unavailable], ["2"])

    def test_playlist_renamed(self):
        change = diff_snapshots(
            make_snapshot(["1"], name="electro house"),
            make_snapshot(["1"], name="Electro House"),
        )
        self.assertEqual(change.name_changed, ("electro house", "Electro House"))


class TestMatch(unittest.TestCase):
    def test_normalize_handles_width_and_case(self):
        self.assertEqual(normalize("ＡＢＣ　ＤＥＦ"), "abcdef")
        self.assertEqual(normalize("Song (Official Video)"), normalize("song"))

    def test_normalize_keeps_semantic_variants(self):
        # 混音/现场是不同录音，不能归一化成同一首
        self.assertNotEqual(normalize("Song (Remix)"), normalize("song"))
        self.assertNotEqual(normalize("Song - Live"), normalize("song"))

    def test_track_key_ignores_feat_and_punctuation(self):
        self.assertEqual(
            track_key("Are You Lonely (feat. ISÁK)", ("Steve Aoki", "ISÁK")),
            track_key("are you lonely", "steve aoki"),
        )

    def test_same_track_duration_tolerance(self):
        self.assertTrue(same_track("X", "Y", 180000, "x", "y", 182000))
        self.assertFalse(same_track("X", "Y", 180000, "x", "y", 200000))
        self.assertFalse(same_track("X", "Y", 180000, "Z", "y", 180000))


class TestLocalSource(unittest.TestCase):
    def test_csv(self):
        with temp_dir() as tmp:
            path = tmp / "my list.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(["title", "artist", "album", "duration_ms"])
                writer.writerow(["Track A", "Artist One、Artist Two", "Album", "185000"])
                writer.writerow(["Track B", "Artist Three", "", ""])

            from rb_sync.sources.local import LocalSource

            src = LocalSource()
            snap = src.fetch(src.resolve_id(str(path)))
            self.assertEqual(snap.name, "my list")
            self.assertEqual(len(snap.tracks), 2)
            self.assertEqual(snap.tracks[0].artists, ("Artist One", "Artist Two"))
            self.assertEqual(snap.tracks[0].duration_ms, 185000)
            self.assertTrue(snap.tracks[0].id.startswith("local-"))
            self.assertEqual(snap.content_hash, content_hash(snap.track_ids))

    def test_json(self):
        with temp_dir() as tmp:
            path = tmp / "set.json"
            dump_json(
                path,
                {
                    "name": "future house",
                    "tracks": [
                        {"title": "A", "artist": "B", "duration": 180},
                        {"id": "fixed-id", "title": "C", "artists": ["D", "E"]},
                    ],
                },
            )
            from rb_sync.sources.local import LocalSource

            src = LocalSource()
            snap = src.fetch(src.resolve_id(str(path)))
            self.assertEqual(snap.name, "future house")
            self.assertEqual(snap.tracks[0].duration_ms, 180000)
            self.assertEqual(snap.tracks[1].id, "fixed-id")
            self.assertEqual(snap.tracks[1].artists, ("D", "E"))

    def test_missing_file(self):
        from rb_sync.sources.base import SourceError
        from rb_sync.sources.local import LocalSource

        with self.assertRaises(SourceError):
            LocalSource().resolve_id("does-not-exist.csv")


class TestStateStore(unittest.TestCase):
    def test_roundtrip(self):
        with temp_dir() as tmp:
            store = StateStore(tmp)
            snap = make_snapshot(["11", "22"])
            store.save_snapshot(snap)

            loaded = store.load_snapshot("netease", "42")
            assert loaded is not None
            self.assertEqual(loaded.track_ids, ["11", "22"])
            self.assertEqual(loaded.name, "electro house")
            self.assertEqual(loaded.content_hash, snap.content_hash)

            sel = Selection(
                playlists=[
                    {"source": "qq", "id": "7707261125", "name": "甜度爆表", "track_count": 66}
                ],
                generated_at="2026-10-28T00:00:00+08:00",
            )
            store.save_selection(sel)
            reread = store.load_selection()
            assert reread is not None
            self.assertEqual(reread.playlist_keys(), [("qq", "7707261125")])

    def test_same_id_different_source_no_collision(self):
        with temp_dir() as tmp:
            store = StateStore(tmp)
            store.save_snapshot(make_snapshot(["1"], source="netease", name="netease 版"))
            store.save_snapshot(make_snapshot(["2"], source="qq", name="qq 版"))
            a = store.load_snapshot("netease", "42")
            b = store.load_snapshot("qq", "42")
            assert a and b
            self.assertEqual(a.name, "netease 版")
            self.assertEqual(b.name, "qq 版")

    def test_json_utf8(self):
        with temp_dir() as tmp:
            path = tmp / "x.json"
            dump_json(path, {"name": "电音"})
            self.assertIn("电音", path.read_text(encoding="utf-8"))
            self.assertEqual(load_json(path), {"name": "电音"})


class FakeSource(HttpSource):
    """只用来测 assemble 的假源，不发任何网络请求。"""

    name = "fake"
    display_name = "假源"
    url_template = "https://example.invalid/playlist/{id}"

    def resolve_id(self, text: str) -> str:
        return text

    def fetch(self, playlist_id, previous=None, refresh_metadata=False):  # pragma: no cover
        raise NotImplementedError


class TestAssemble(unittest.TestCase):
    def test_fast_path_makes_zero_metadata_requests(self):
        src = FakeSource()
        calls: list[list[str]] = []
        prev = make_snapshot(["1", "2"])
        snap = src.assemble(
            "42",
            name="electro house",
            ids=["1", "2"],
            fetch_details=lambda missing: calls.append(list(missing)) or {},
            previous=prev,
            refresh_metadata=False,
        )
        self.assertEqual(calls, [])
        self.assertEqual(snap.track_count, 2)
        self.assertEqual(snap.content_hash, prev.content_hash)

    def test_only_new_ids_hit_the_network(self):
        src = FakeSource()
        calls: list[list[str]] = []

        def details(missing):
            calls.append(list(missing))
            return {i: TrackRef(id=i, name=f"t{i}") for i in missing}

        snap = src.assemble(
            "42",
            name="electro house",
            ids=["1", "2", "3"],
            fetch_details=details,
            previous=make_snapshot(["1", "2"]),
            refresh_metadata=False,
        )
        self.assertEqual(calls, [["3"]])
        self.assertEqual(snap.track_ids, ["1", "2", "3"])
        self.assertEqual(snap.tracks[2].name, "t3")

    def test_broken_snapshot_does_not_shortcut(self):
        # hash 与曲目数量不一致（快照被手工改坏）时不能走缓存
        src = FakeSource()
        broken = make_snapshot(["1", "2", "9"])
        broken.tracks = broken.tracks[:2]
        calls: list[list[str]] = []
        snap = src.assemble(
            "42",
            name="electro house",
            ids=["1", "2", "9"],
            fetch_details=lambda missing: calls.append(list(missing)) or {},
            previous=broken,
            refresh_metadata=False,
        )
        self.assertEqual(calls, [["9"]])
        self.assertEqual(snap.track_ids, ["1", "2", "9"])


if __name__ == "__main__":
    unittest.main()