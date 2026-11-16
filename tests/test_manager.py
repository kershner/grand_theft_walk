"""Common launch selection and readiness gates, without starting any game."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from games import ACTIVE_GAMES, GAMES, load_profile
from core.manager import component_status, session_command, start_session


class ManagerTests(unittest.TestCase):
    def test_all_games_use_the_same_profile_filename_and_schema(self):
        for game in ACTIVE_GAMES:
            with self.subTest(game=game):
                self.assertEqual(load_profile(game), GAMES[game])

    def test_profile_identity_cannot_silently_point_at_another_game(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "games/vice_city").mkdir(parents=True)
            (root / "games/vice_city/profile.json").write_text(json.dumps({"format": 1, "id": "san_andreas"}))
            with self.assertRaisesRegex(ValueError, "Invalid game profile"):
                load_profile("vice_city", root)

    def test_wrong_executable_blocks_launch_and_baseline_before_spawning(self):
        for baseline in (False, True):
            with (
                self.subTest(baseline=baseline),
                patch("core.manager.installation_status", return_value={"executable_matches": False}),
                self.assertRaisesRegex(RuntimeError, "pinned classic"),
            ):
                session_command("vice_city", baseline)

    def test_incomplete_controller_cannot_be_launched_as_if_parity_is_ready(self):
        with (
            patch("core.manager.installation_status", return_value={"executable_matches": True, "setup_matches": True, "controller_ready": False}),
            self.assertRaisesRegex(RuntimeError, "controller is not ready"),
        ):
            session_command("vice_city")

    def test_baseline_runs_the_game_directly_without_the_controller(self):
        with patch("core.manager.installation_status", return_value={"executable_matches": True, "setup_matches": True, "controller_ready": False, "executable": "classic.exe"}):
            command, directory = session_command("vice_city", baseline=True)
        self.assertEqual(command, ["classic.exe"])
        self.assertEqual(directory, GAMES["vice_city"].install_dir)

    def test_correct_executable_with_original_loader_cannot_launch(self):
        status = {"executable_matches": True, "setup_matches": False,
                  "mismatches": [{"path": "vorbisFile.dll"}], "controller_ready": False}
        for baseline in (False, True):
            with (
                self.subTest(baseline=baseline),
                patch("core.manager.installation_status", return_value=status),
                self.assertRaisesRegex(RuntimeError, "mod setup is incomplete.*vorbisFile.dll"),
            ):
                session_command("san_andreas", baseline)

    def test_unbounded_baseline_test_is_rejected_before_starting_a_session(self):
        with patch("core.manager.session_command") as select:
            for seconds in (-1, 0, 9, 301):
                with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                    start_session("vice_city", baseline=True, seconds=seconds)
            select.assert_not_called()


class ComponentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = self.root / "Game"
        self.game.mkdir(parents=True)
        self.files = {"gta_sa.exe": b"pinned exe", "vorbisFile.dll": b"ASI loader", "skygfx.ini": b"PS2 preset"}
        for name, data in self.files.items():
            (self.game / name).write_bytes(data)
        files = [{"path": name, "sha256": hashlib.sha256(data).hexdigest()} for name, data in self.files.items()]
        self.manifest = {"format": 1, "games": {"san_andreas": {"files": files}}}
        self.save()

    def save(self):
        (self.root / "suite-manifest.json").write_text(json.dumps(self.manifest))

    def test_all_component_hashes_must_match(self):
        self.assertTrue(component_status("san_andreas", self.game, self.root)["setup_matches"])
        (self.game / "vorbisFile.dll").write_bytes(b"original loader")
        status = component_status("san_andreas", self.game, self.root)
        self.assertFalse(status["setup_matches"])
        self.assertEqual([x["path"] for x in status["mismatches"]], ["vorbisFile.dll"])

    def test_changed_settings_are_detected(self):
        (self.game / "skygfx.ini").write_bytes(b"different preset")
        self.assertFalse(component_status("san_andreas", self.game, self.root)["setup_matches"])

    def test_missing_manifest_does_not_silently_reduce_check_to_executable(self):
        (self.root / "suite-manifest.json").unlink()
        self.assertFalse(component_status("san_andreas", self.game, self.root)["setup_matches"])

    def test_unfinished_repair_blocks_launch_even_if_files_match(self):
        (self.game / ".gtw-suite").mkdir(parents=True)
        (self.game / ".gtw-suite/pending.json").write_text("{}")
        self.assertFalse(component_status("san_andreas", self.game, self.root)["setup_matches"])
