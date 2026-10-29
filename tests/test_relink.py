"""relink 的纯逻辑测试（不碰数据库、不联网）。"""

from __future__ import annotations

import contextlib
import shutil
import unittest
import uuid
from dataclasses import dataclass
from pathlib import Path

from rb_sync.rekordbox.relink import (
    build_name_index,
    build_plan,
    normalize_root,
    remap_by_root,
)

TMP_ROOT = Path(__file__).resolve().parent / "_tmp"


@contextlib.contextmanager
def temp_dir():
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TMP_ROOT / f"relink-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@dataclass
class FakeTrack:
    id: str
    title: str
    artist: str
    path: str


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


class TestRoots(unittest.TestCase):
    def test_normalize_root(self):
        self.assertEqual(normalize_root("E:/Music"), "E:/Music/")
        self.assertEqual(normalize_root("E:\\Music\\"), "E:/Music/")
        self.assertEqual(normalize_root(""), "")

    def test_remap_hits(self):
        self.assertEqual(
            remap_by_root("E:/Music/Cloud Music/a.flac", "E:/Music", "F:/Music"),
            "F:/Music/Cloud Music/a.flac",
        )

    def test_remap_is_case_and_separator_insensitive(self):
        self.assertEqual(
            remap_by_root("e:\\Music\\x\\b.wav", "E:/MUSIC", "F:/Music"),
            "F:/Music/x/b.wav",
        )

    def test_remap_miss(self):
        self.assertIsNone(remap_by_root("D:/Other/a.flac", "E:/Music", "F:/Music"))
        self.assertIsNone(remap_by_root("E:/Music/a.flac", "", "F:/Music"))


class TestPlan(unittest.TestCase):
    def test_root_remap_wins(self):
        with temp_dir() as tmp:
            new_root = tmp / "Music"
            target = touch(new_root / "Cloud Music" / "a.flac")
            tracks = [FakeTrack("1", "A", "X", "E:/Music/Cloud Music/a.flac")]
            plan = build_plan(tracks, "E:/Music", str(new_root))
            self.assertEqual(len(plan.matched), 1)
            self.assertEqual(plan.matched[0].strategy, "root_remap")
            self.assertEqual(
                Path(plan.matched[0].new_path).resolve(), target.resolve()
            )

    def test_basename_rescue_same_extension(self):
        with temp_dir() as tmp:
            touch(tmp / "Music" / "temp" / "deep water.wav")
            tracks = [FakeTrack("1", "Deep Water", "Maozon",
                                "E:/Music/Cloud Music/Maozon/Deep Water.wav")]
            plan = build_plan(tracks, "E:/Music", str(tmp / "Music"))
            self.assertEqual(plan.matched[0].strategy, "basename")

    def test_basename_with_extension_change_is_marked(self):
        with temp_dir() as tmp:
            touch(tmp / "Music" / "x" / "song.wav")
            tracks = [FakeTrack("1", "Song", "Y", "E:/Music/Cloud Music/song.flac")]
            plan = build_plan(tracks, "E:/Music", str(tmp / "Music"))
            self.assertEqual(plan.matched[0].strategy, "basename_ext")

    def test_ambiguous_is_not_guessed(self):
        with temp_dir() as tmp:
            touch(tmp / "Music" / "a" / "same.flac")
            touch(tmp / "Music" / "b" / "same.flac")
            tracks = [FakeTrack("1", "Same", "Z", "E:/Music/Cloud Music/same.flac")]
            plan = build_plan(tracks, "E:/Music", str(tmp / "Music"))
            self.assertEqual(plan.matched, [])
            self.assertEqual(len(plan.ambiguous), 1)

    def test_unmatched_when_nothing_found(self):
        with temp_dir() as tmp:
            touch(tmp / "Music" / "other.flac")
            tracks = [FakeTrack("1", "Gone", "Q", "E:/Music/Cloud Music/gone.flac")]
            plan = build_plan(tracks, "E:/Music", str(tmp / "Music"))
            self.assertEqual(plan.matched, [])
            self.assertEqual(len(plan.unmatched), 1)
            self.assertEqual(plan.by_strategy(), {})

    def test_index_ignores_non_audio(self):
        with temp_dir() as tmp:
            touch(tmp / "Music" / "cover.jpg")
            touch(tmp / "Music" / "notes.txt")
            index = build_name_index([tmp / "Music"])
            self.assertEqual(index, {})

    def test_counts_by_strategy(self):
        with temp_dir() as tmp:
            root = tmp / "Music"
            touch(root / "keep" / "a.flac")
            touch(root / "moved" / "b.flac")
            tracks = [
                FakeTrack("1", "A", "X", "E:/Music/keep/a.flac"),
                FakeTrack("2", "B", "Y", "E:/Music/old/b.flac"),
                FakeTrack("3", "C", "Z", "E:/Music/old/c.flac"),
            ]
            plan = build_plan(tracks, "E:/Music", str(root))
            self.assertEqual(plan.by_strategy(), {"root_remap": 1, "basename": 1})
            self.assertEqual(len(plan.unmatched), 1)
            self.assertEqual(plan.to_dict()["counts"]["matched"], 2)


if __name__ == "__main__":
    unittest.main()
