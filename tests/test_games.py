"""Identity, legacy compatibility, isolation, and read-only install inspection."""

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from gtw.games import GAMES, get_game
from gtw.inspect_install import inspect_install
from gtw.runtime import WalkerControl


class GameProfileTests(unittest.TestCase):
    def test_default_directories_are_distinct_and_san_andreas_is_unchanged(self):
        folders = {game.state_dir for game in GAMES.values()}
        self.assertEqual(len(folders), 3)
        self.assertEqual(get_game().state_dir.name, "runtime")
        self.assertEqual(get_game().state_dir.parent.name, "san_andreas")
        for game in GAMES.values():
            self.assertEqual(WalkerControl(game=game.id).state_dir, game.state_dir)

    def test_unknown_game_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown game"):
            WalkerControl(game="unknown")

    def test_only_san_andreas_accepts_legacy_untagged_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = {"updated": time.time(), "controller": "running", "ready": True}
            path.write_text(json.dumps(state), encoding="utf-8")
            self.assertTrue(WalkerControl(folder).snapshot()["ready"])
            for game in ("vice_city", "gta_iii"):
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
