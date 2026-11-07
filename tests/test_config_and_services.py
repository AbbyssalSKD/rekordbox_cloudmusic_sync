"""配置层与服务层的离线测试（不联网、不需要 pyrekordbox）。"""

from __future__ import annotations

import contextlib
import csv
import os
import shutil
import unittest
import uuid
from pathlib import Path

from rb_sync import services
from rb_sync.config import (
    Settings,
    load_cookie,
    load_settings,
    save_settings,
    update_settings,
)
from rb_sync.sources import SourceError

TMP_ROOT = Path(__file__).resolve().parent / "_tmp"


@contextlib.contextmanager
def temp_dir():
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TMP_ROOT / f"cfg-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class TestSettings(unittest.TestCase):
    def test_roundtrip(self):
        original = Settings(
            db_path="F:\\PIONEER\\Master\\master.db",
            music_roots=["F:\\Music", "E:\\Music"],
            previous_roots=["E:/Music"],
            netease_cookie="MUSIC_U=abcdefghijklmnop",
        )
        restored = Settings.from_dict(original.to_dict())
        self.assertEqual(restored.db_path, original.db_path)
        self.assertEqual(restored.music_roots, original.music_roots)
        self.assertEqual(restored.previous_roots, original.previous_roots)

    def test_masking_hides_cookie(self):
        settings = Settings(netease_cookie="MUSIC_U=dXNlcjpwYXNz")
        masked = settings.to_dict(mask_secrets=True)
        self.assertNotIn("dXNlcjpwYXNz", masked["netease_cookie"])
        self.assertTrue(masked["netease_cookie"])
        raw = settings.to_dict(mask_secrets=False)
        self.assertEqual(raw["netease_cookie"], "MUSIC_U=dXNlcjpwYXNz")

    def test_save_and_load(self):
        with temp_dir() as tmp:
            path = tmp / "config.local.json"
            save_settings(Settings(db_path="Z:\\x.db", music_roots=["Z:\\Music"]), path)
            loaded = load_settings(path)
            self.assertEqual(loaded.db_path, "Z:\\x.db")
            self.assertEqual(loaded.music_roots, ["Z:\\Music"])

    def test_update_is_partial(self):
        with temp_dir() as tmp:
            path = tmp / "config.local.json"
            save_settings(Settings(netease_cookie="keepme", db_path="old.db"), path)
            update_settings({"rekordbox": {"db_path": "new.db"}}, path)
            loaded = load_settings(path)
            self.assertEqual(loaded.db_path, "new.db")
            self.assertEqual(loaded.netease_cookie, "keepme")  # 没传的字段不能被清空

    def test_missing_file_gives_defaults(self):
        with temp_dir() as tmp:
            settings = load_settings(tmp / "nope.json")
            self.assertEqual(settings.db_path, "")
            self.assertEqual(settings.web_host, "127.0.0.1")

    def test_corrupt_file_gives_defaults(self):
        with temp_dir() as tmp:
            path = tmp / "config.local.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(load_settings(path).db_path, "")

    def test_legacy_flat_keys(self):
        with temp_dir() as tmp:
            path = tmp / "config.local.json"
            path.write_text('{"db_path": "L.db", "music_roots": ["L"]}', encoding="utf-8")
            loaded = load_settings(path)
            self.assertEqual(loaded.db_path, "L.db")
            self.assertEqual(loaded.music_roots, ["L"])

    def test_cookie_precedence(self):
        self.assertEqual(load_cookie("netease", "explicit"), "explicit")
        os.environ["QQ_COOKIE"] = "from-env"
        try:
            self.assertEqual(load_cookie("qq"), "from-env")
        finally:
            os.environ.pop("QQ_COOKIE", None)


class TestServices(unittest.TestCase):
    def _csv(self, path: Path) -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["title", "artist", "duration_ms"])
            writer.writerow(["Track A", "Artist", "180000"])
            writer.writerow(["Track B", "Artist", "190000"])

    def test_run_plan_with_local_csv(self):
        with temp_dir() as tmp:
            csv_path = tmp / "set.csv"
            self._csv(csv_path)
            state = tmp / "state"
            result = services.run_plan([str(csv_path)], state_dir=state)
            self.assertTrue(result.ok, result.errors)
            self.assertEqual(len(result.playlists), 1)
            self.assertEqual(result.playlists[0]["track_count"], 2)
            self.assertTrue(result.selection_path.is_file())

            # 第二次跑，应当报"无变化"
            again = services.run_plan([str(csv_path)], state_dir=state)
            self.assertTrue(again.report[0]["unchanged"], again.report[0])

    def test_append_merges_selection(self):
        with temp_dir() as tmp:
            first = tmp / "a.csv"
            second = tmp / "b.csv"
            self._csv(first)
            self._csv(second)
            state = tmp / "state"
            services.run_plan([str(first)], state_dir=state)
            result = services.run_plan([str(second)], state_dir=state, append=True)
            self.assertEqual(result.selection_count, 2)
            from rb_sync.state import StateStore

            selection = StateStore(state).load_selection()
            assert selection is not None
            self.assertEqual(len(selection.playlists), 2)

    def test_replace_reports_dropped(self):
        with temp_dir() as tmp:
            first = tmp / "a.csv"
            second = tmp / "b.csv"
            self._csv(first)
            self._csv(second)
            state = tmp / "state"
            services.run_plan([str(first)], state_dir=state)
            result = services.run_plan([str(second)], state_dir=state)
            self.assertEqual(result.dropped_previous, 1)

    def test_bare_number_is_rejected(self):
        with self.assertRaises(SourceError) as ctx:
            services.run_plan(["123456"], state_dir="state")
        self.assertIn("歧义", str(ctx.exception))

    def test_empty_input_rejected(self):
        with self.assertRaises(SourceError):
            services.run_plan([], state_dir="state")

    def test_name_only_for_single(self):
        with temp_dir() as tmp:
            a = tmp / "a.csv"
            b = tmp / "b.csv"
            self._csv(a)
            self._csv(b)
            with self.assertRaises(SourceError):
                services.run_plan([str(a), str(b)], state_dir=tmp / "s", name="x")


if __name__ == "__main__":
    unittest.main()
