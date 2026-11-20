"""Common launch selection and readiness gates, without starting any game."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.manager import component_status, inspect_install, session_command, start_session
from core.runtime import WalkerControl
from games import ACTIVE_GAMES, GAMES, get_game, load_profile


class ManagerTests(unittest.TestCase):
    def test_direct_entry_points_bootstrap_imports_before_parsing_arguments(self):
        root = Path(__file__).resolve().parents[1]
        scripts = [root / 'games' / game / 'launch.py' for game in ACTIVE_GAMES]
        scripts.append(root / 'build' / 'build_suite.py')
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        for script in scripts:
            with self.subTest(script=script.relative_to(root)):
                result = subprocess.run(
                    [sys.executable, str(script), '--help'], cwd=script.parent,
                    env=environment, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('usage:', result.stdout.lower())

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


class GameProfileTests(unittest.TestCase):
    def test_runtime_directories_are_isolated_under_the_games_folder(self):
        folders = {game.state_dir for game in GAMES.values()}
        self.assertEqual(len(folders), len(GAMES))
        self.assertEqual(get_game().state_dir.name, "runtime")
        self.assertEqual(get_game().state_dir.parent.name, "san_andreas")
        for game in GAMES.values():
            self.assertEqual(game.source_dir.parent.name, "games")
            self.assertEqual(game.state_dir, game.source_dir / "runtime")
            self.assertEqual(WalkerControl(game=game.id).state_dir, game.state_dir)

    def test_unknown_game_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown game"):
            WalkerControl(game="unknown")

    def test_synthetic_game_is_only_registered_inside_its_test_fixture(self):
        from tests.fixtures import legacy_game_profile

        self.assertNotIn("synthetic_legacy", GAMES)
        with legacy_game_profile():
            self.assertEqual(get_game("synthetic_legacy").title, "GTA III")
        self.assertNotIn("synthetic_legacy", GAMES)

    def test_only_san_andreas_accepts_legacy_untagged_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = {"updated": time.time(), "controller": "running", "ready": True}
            path.write_text(json.dumps(state), encoding="utf-8")
            self.assertTrue(WalkerControl(folder).snapshot()["ready"])
            for game in ("vice_city",):
                with self.subTest(game=game), self.assertRaisesRegex(RuntimeError, "another game"):
                    WalkerControl(folder, game=game).snapshot()

    def test_new_backends_require_protocol_and_all_reject_unknown_versions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            for game in GAMES:
                for protocol in (None, 2, True):
                    state = {"game": game, "updated": time.time(), "controller": "running"}
                    if protocol is not None:
                        state["protocol"] = protocol
                    path.write_text(json.dumps(state), encoding="utf-8")
                    if protocol is None and game == "san_andreas":
                        WalkerControl(folder, game=game).snapshot()
                    else:
                        with (
                            self.subTest(game=game, protocol=protocol),
                            self.assertRaisesRegex(RuntimeError, "protocol"),
                        ):
                            WalkerControl(folder, game=game).snapshot()


class InstallInspectionTests(unittest.TestCase):
    def test_inventory_fingerprints_without_changing_files_or_archiving_tokens(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = {
                "gta-vc.exe": b"unverified executable",
                "version.txt": "1.0.0.15".encode("utf-16"),
                "skygfx.ini": b"buildingPipe=PS2\n",
                "othermod.ini": b"token=private\n",
            }
            for name, data in files.items():
                (root / name).write_bytes(data)
            report = inspect_install("vice_city", root)
            self.assertFalse(report["verified_executable"])
            self.assertEqual(report["reported_version"], "1.0.0.15")
            self.assertEqual(report["files"]["gta-vc.exe"]["sha256"],
                             hashlib.sha256(files["gta-vc.exe"]).hexdigest())
            self.assertEqual(report["configuration"], {"skygfx.ini": "buildingPipe=PS2\n"})
            self.assertNotIn("private", json.dumps(report))
            self.assertEqual({path.name: path.read_bytes() for path in root.iterdir()}, files)

    def test_missing_installation_is_reported_without_creating_it(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "missing"
            with self.assertRaisesRegex(FileNotFoundError, "Game executable not found"):
                inspect_install("vice_city", missing)
            self.assertFalse(missing.exists())
