"""Restart and failure behavior of the persistent running-time rotation."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.rotation import RotationProgress


class RotationProgressTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "progress.json"
        self.params = {"ROTATION": {"games": ["san_andreas", "vice_city", "gta_iii"]}}

    def load(self):
        return RotationProgress(self.params, self.path)

    def test_restart_resumes_remaining_time_and_excludes_downtime(self):
        progress = self.load()
        with patch("core.rotation.time.monotonic", return_value=100):
            progress.start("gta_iii")
        with patch("core.rotation.time.monotonic", return_value=2500):
            progress.checkpoint(force=True)
        resumed = self.load()
        self.assertEqual(resumed.select(), "gta_iii")
        self.assertEqual(resumed.remaining("gta_iii"), 4800)
        with patch("core.rotation.time.monotonic", return_value=100000):
            resumed.start("gta_iii")
        with patch("core.rotation.time.monotonic", return_value=104799):
            resumed.checkpoint()
        self.assertEqual(self.load().select(), "gta_iii")
        with patch("core.rotation.time.monotonic", return_value=104800):
            resumed.checkpoint()
        self.assertEqual(self.load().select(), "san_andreas")

    def test_failed_launch_does_not_reset_or_add_time(self):
        self.path.write_text(json.dumps({"version": 1, "game": "vice_city", "elapsed_seconds": 2400}))
        progress = self.load()
        progress.checkpoint(force=True)
        self.assertEqual(self.load().remaining("vice_city"), 4800)

    def test_checkpoint_survives_unclean_exit(self):
        progress = self.load()
        with patch("core.rotation.time.monotonic", return_value=100):
            progress.start("vice_city")
        with patch("core.rotation.time.monotonic", return_value=106):
            progress.checkpoint()
        self.assertEqual(self.load().remaining("vice_city"), 7194)

    def test_explicit_game_resumes_same_slot_but_starts_new_game_fresh(self):
        self.path.write_text(json.dumps({"version": 1, "game": "vice_city", "elapsed_seconds": 2400}))
        progress = self.load()
        progress.start("vice_city")
        self.assertEqual(progress.remaining("vice_city"), 4800)
        progress.start("gta_iii")
        self.assertEqual(self.load().select(), "gta_iii")
        self.assertEqual(progress.remaining("gta_iii"), 7200)

    def test_missing_corrupt_and_removed_game_progress_use_first_game(self):
        self.assertEqual(self.load().select(), "san_andreas")
        for data in ("{", "[]", '{"version":1,"game":"vice_city","elapsed_seconds":-1}',
                     '{"version":1,"game":"vice_city","elapsed_seconds":true}',
                     '{"version":1,"game":"vice_city","elapsed_seconds":NaN}'):
            with self.subTest(data=data):
                self.path.write_text(data)
                self.assertEqual(self.load().select(), "san_andreas")
        self.path.write_text('{"version":1,"game":"removed","elapsed_seconds":100}')
        self.assertEqual(self.load().select(), "san_andreas")

    def test_changed_interval_uses_saved_running_time(self):
        self.path.write_text('{"version":1,"game":"vice_city","elapsed_seconds":4000}')
        self.params["ROTATION"]["interval_hours"] = 1
        self.assertEqual(self.load().select(), "gta_iii")
