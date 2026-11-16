"""Exercise installs, repairs, interrupted writes and restoration on fake games."""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gtw import setup


def digest(data):
    return hashlib.sha256(data).hexdigest()


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "Transfer bundle"
        self.game = self.root / "Original game"
        (self.bundle / "payload").mkdir(parents=True)
        (self.game / "data").mkdir(parents=True)
        (self.game / "data/main.scm").write_bytes(b"owned game data")
        (self.game / "san_andreas.game.exe").write_bytes(b"original executable")
        (self.bundle / "payload/game.exe").write_bytes(b"pinned executable")
        (self.bundle / "payload/options.ini").write_bytes(b"pinned settings")
        self.definition = {
            "component_id": "test-component-1", "title": "Vice City", "install_folder": "Example",
            "executable": "san_andreas.game.exe", "required_files": ["data/main.scm"],
            "files": [
                {"path": "san_andreas.game.exe", "payload": "payload/game.exe", "sha256": digest(b"pinned executable"),
                 "accepted_before": [digest(b"original executable")]},
                {"path": "options.ini", "payload": "payload/options.ini", "sha256": digest(b"pinned settings"),
                 "accepted_before": [None]},
            ],
        }
        self.installer = setup.Installer(self.bundle, self.game, "vice_city", self.definition)

    def record(self):
        return json.loads(self.installer.state_path.read_text())

    def index(self):
        (self.bundle / "suite-manifest.json").write_text(json.dumps({"format": 1, "games": {"vice_city": self.definition}}))
        files = {p.relative_to(self.bundle).as_posix(): setup.sha256(p) for p in self.bundle.rglob("*") if p.is_file() and p.name != "bundle-index.json"}
        (self.bundle / "bundle-index.json").write_text(json.dumps({"format": 1, "files": files}))

    def test_install_check_and_restore_preserve_owned_game_data(self):
        self.assertEqual(self.installer.apply("install")["changed"], 2)
        self.assertTrue(self.installer.check()["ok"])
        self.installer.apply("restore")
        self.assertEqual((self.game / "san_andreas.game.exe").read_bytes(), b"original executable")
        self.assertFalse((self.game / "options.ini").exists())
        self.assertEqual((self.game / "data/main.scm").read_bytes(), b"owned game data")

    def test_repeated_installs_and_repairs_keep_the_first_backup(self):
        self.installer.apply("install")
        original = self.record()
        self.assertEqual(self.installer.apply("install")["changed"], 0)
        for _ in range(2):
            (self.game / "san_andreas.game.exe").write_bytes(b"original executable")
            self.assertFalse(self.installer.check()["ok"])
            with self.assertRaisesRegex(ValueError, "Run Setup.cmd -Action Repair"):
                self.installer.apply("install")
            self.installer.apply("repair")
            self.assertEqual(self.record(), original)
        self.installer.apply("restore")
        self.assertEqual((self.game / "san_andreas.game.exe").read_bytes(), b"original executable")

    def test_repair_saves_changed_settings_in_a_checkpoint(self):
        self.installer.apply("install")
        (self.game / "options.ini").write_bytes(b"my custom settings")
        self.installer.apply("repair")
        self.assertEqual((self.game / "options.ini").read_bytes(), b"pinned settings")
        checkpoints = list((self.game / ".gtw-suite/transactions").glob("*/options.ini"))
        self.assertTrue(any(path.read_bytes() == b"my custom settings" for path in checkpoints))

    def test_first_install_rejects_unknown_files_before_changing_anything(self):
        (self.game / "options.ini").write_bytes(b"unrelated mod")
        with self.assertRaisesRegex(ValueError, "Unrecognized existing file"):
            self.installer.apply("install")
        self.assertEqual((self.game / "san_andreas.game.exe").read_bytes(), b"original executable")
        self.assertFalse(self.installer.state_path.exists())

    def test_damaged_payload_is_rejected_before_any_game_write(self):
        (self.bundle / "payload/options.ini").write_bytes(b"broken download")
        with self.assertRaisesRegex(ValueError, "Payload missing or damaged"):
            self.installer.apply("install")
        self.assertEqual((self.game / "san_andreas.game.exe").read_bytes(), b"original executable")
        self.assertFalse((self.game / "options.ini").exists())

    def test_missing_executable_can_be_repaired_from_existing_record(self):
        self.installer.apply("install")
        (self.game / "san_andreas.game.exe").unlink()
        self.installer.apply("repair")
        self.assertTrue(self.installer.check()["ok"])

    def test_damaged_original_backup_blocks_restore_before_any_write(self):
        self.installer.apply("install")
        entry = self.record()["entries"][0]
        self.installer.backup_path(entry["original"]).write_bytes(b"damaged")
        with self.assertRaisesRegex(ValueError, "Original backup missing or damaged"):
            self.installer.apply("restore")
        self.assertTrue(self.installer.check()["ok"])

    def test_failed_write_rolls_back_all_preceding_writes(self):
        put = setup.put_file
        calls = 0

        def fail_second_write(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated write failure")
            return put(source, target)

        with patch("gtw.setup.put_file", side_effect=fail_second_write), self.assertRaises(OSError):
            self.installer.apply("install")
        self.assertEqual((self.game / "san_andreas.game.exe").read_bytes(), b"original executable")
        self.assertFalse((self.game / "options.ini").exists())
        self.assertFalse(self.installer.pending_path.exists())
        self.assertFalse(self.installer.state_path.exists())

    def interrupt_install(self):
        put = setup.put_file
        calls = 0

        def interrupt_second_write(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt()
            return put(source, target)

        with patch("gtw.setup.put_file", side_effect=interrupt_second_write), self.assertRaises(KeyboardInterrupt):
            self.installer.apply("install")

    def test_repair_recovers_a_process_interruption_then_retries(self):
        self.interrupt_install()
        self.assertTrue(self.installer.pending_path.exists())
        self.assertEqual((self.game / "san_andreas.game.exe").read_bytes(), b"pinned executable")
        self.installer.apply("repair")
        self.assertTrue(self.installer.check()["ok"])
        self.assertFalse(self.installer.pending_path.exists())
        self.installer.apply("restore")
        self.assertEqual((self.game / "san_andreas.game.exe").read_bytes(), b"original executable")

    def test_unknown_post_interruption_edit_is_preserved_before_any_recovery(self):
        self.interrupt_install()
        (self.game / "options.ini").write_bytes(b"new user edit")
        with self.assertRaisesRegex(ValueError, "changed after setup was interrupted"):
            self.installer.apply("repair")
        self.assertEqual((self.game / "options.ini").read_bytes(), b"new user edit")
        self.assertEqual((self.game / "san_andreas.game.exe").read_bytes(), b"pinned executable")
        self.assertTrue(self.installer.pending_path.exists())

    def test_disabled_files_are_backed_up_removed_and_restored(self):
        (self.game / "old-loader.dll").write_bytes(b"known launcher component")
        self.definition["files"].append({"path": "old-loader.dll", "payload": None, "sha256": None,
                                         "accepted_before": [None, digest(b"known launcher component")]})
        installer = setup.Installer(self.bundle, self.game, "vice_city", self.definition)
        installer.apply("install")
        self.assertFalse((self.game / "old-loader.dll").exists())
        self.assertTrue(installer.check()["ok"])
        installer.apply("restore")
        self.assertEqual((self.game / "old-loader.dll").read_bytes(), b"known launcher component")

    def test_restore_is_repeatable(self):
        self.installer.apply("install")
        self.installer.apply("restore")
        self.assertEqual(self.installer.apply("restore")["changed"], 0)

    def test_preconfigured_adoption_restores_its_actual_pre_suite_state(self):
        (self.game / "san_andreas.game.exe").write_bytes(b"pinned executable")
        self.installer.apply("install")
        self.installer.apply("restore")
        self.assertEqual((self.game / "san_andreas.game.exe").read_bytes(), b"pinned executable")

    def test_manifest_paths_cannot_escape_install_or_target_alternate_streams(self):
        for name in ("../outside.exe", "C:/outside.exe", "\\\\server\\share\\outside.exe", "san_andreas.game.exe:stream", "data/../outside.exe", "trailing. "):
            with self.subTest(name=name), self.assertRaises(ValueError):
                setup.under(self.game, name)

    def test_archive_damage_is_detected(self):
        self.index()
        setup.verify_bundle(self.bundle)
        (self.bundle / "payload/game.exe").write_bytes(b"corrupted in transfer")
        with self.assertRaisesRegex(ValueError, "Archive file missing or damaged"):
            setup.verify_bundle(self.bundle)

    def test_custom_path_is_saved_and_reused_even_if_executable_is_deleted(self):
        self.index()
        with patch("gtw.setup.ensure_closed"), contextlib.redirect_stdout(io.StringIO()):
            setup.main(["install", "--bundle", str(self.bundle), "--game", "vice_city", "--vice-city-dir", str(self.game)])
            (self.game / "san_andreas.game.exe").unlink()
            setup.main(["repair", "--bundle", str(self.bundle), "--game", "vice_city"])
        self.assertTrue(self.installer.check()["ok"])


if __name__ == "__main__":
    unittest.main()
