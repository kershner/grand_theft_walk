"""Exercise installs, repairs, interrupted writes and restoration on fake games."""

import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from build import build_suite
from build.build_suite import validate_graphics_capacity
from build.setup import setup


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
        (self.game / "games.san_andreas.game.exe").write_bytes(b"original executable")
        (self.bundle / "payload/game.exe").write_bytes(b"pinned executable")
        (self.bundle / "payload/options.ini").write_bytes(b"pinned settings")
        self.definition = {
            "component_id": "test-component-1", "title": "Vice City", "install_folder": "Example",
            "executable": "games.san_andreas.game.exe", "required_files": ["data/main.scm"],
            "files": [
                {"path": "games.san_andreas.game.exe", "payload": "payload/game.exe", "sha256": digest(b"pinned executable"),
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
        self.assertEqual((self.game / "games.san_andreas.game.exe").read_bytes(), b"original executable")
        self.assertFalse((self.game / "options.ini").exists())
        self.assertEqual((self.game / "data/main.scm").read_bytes(), b"owned game data")

    def test_repeated_installs_and_repairs_keep_the_first_backup(self):
        self.installer.apply("install")
        original = self.record()
        self.assertEqual(self.installer.apply("install")["changed"], 0)
        for _ in range(2):
            (self.game / "games.san_andreas.game.exe").write_bytes(b"original executable")
            self.assertFalse(self.installer.check()["ok"])
            with self.assertRaisesRegex(ValueError, r"Run build\\setup\\Setup.cmd -Action Repair"):
                self.installer.apply("install")
            self.installer.apply("repair")
            self.assertEqual(self.record(), original)
        self.installer.apply("restore")
        self.assertEqual((self.game / "games.san_andreas.game.exe").read_bytes(), b"original executable")

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
        self.assertEqual((self.game / "games.san_andreas.game.exe").read_bytes(), b"original executable")
        self.assertFalse(self.installer.state_path.exists())

    def test_damaged_payload_is_rejected_before_any_game_write(self):
        (self.bundle / "payload/options.ini").write_bytes(b"broken download")
        with self.assertRaisesRegex(ValueError, "Payload missing or damaged"):
            self.installer.apply("install")
        self.assertEqual((self.game / "games.san_andreas.game.exe").read_bytes(), b"original executable")
        self.assertFalse((self.game / "options.ini").exists())

    def test_missing_executable_can_be_repaired_from_existing_record(self):
        self.installer.apply("install")
        (self.game / "games.san_andreas.game.exe").unlink()
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

        with patch("build.setup.setup.put_file", side_effect=fail_second_write), self.assertRaises(OSError):
            self.installer.apply("install")
        self.assertEqual((self.game / "games.san_andreas.game.exe").read_bytes(), b"original executable")
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

        with patch("build.setup.setup.put_file", side_effect=interrupt_second_write), self.assertRaises(KeyboardInterrupt):
            self.installer.apply("install")

    def test_repair_recovers_a_process_interruption_then_retries(self):
        self.interrupt_install()
        self.assertTrue(self.installer.pending_path.exists())
        self.assertEqual((self.game / "games.san_andreas.game.exe").read_bytes(), b"pinned executable")
        self.installer.apply("repair")
        self.assertTrue(self.installer.check()["ok"])
        self.assertFalse(self.installer.pending_path.exists())
        self.installer.apply("restore")
        self.assertEqual((self.game / "games.san_andreas.game.exe").read_bytes(), b"original executable")

    def test_unknown_post_interruption_edit_is_preserved_before_any_recovery(self):
        self.interrupt_install()
        (self.game / "options.ini").write_bytes(b"new user edit")
        with self.assertRaisesRegex(ValueError, "changed after setup was interrupted"):
            self.installer.apply("repair")
        self.assertEqual((self.game / "options.ini").read_bytes(), b"new user edit")
        self.assertEqual((self.game / "games.san_andreas.game.exe").read_bytes(), b"pinned executable")
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
        (self.game / "games.san_andreas.game.exe").write_bytes(b"pinned executable")
        self.installer.apply("install")
        self.installer.apply("restore")
        self.assertEqual((self.game / "games.san_andreas.game.exe").read_bytes(), b"pinned executable")

    def test_manifest_paths_cannot_escape_install_or_target_alternate_streams(self):
        for name in ("../outside.exe", "C:/outside.exe", "\\\\server\\share\\outside.exe", "games.san_andreas.game.exe:stream", "data/../outside.exe", "trailing. "):
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
        with patch("build.setup.setup.ensure_closed"), contextlib.redirect_stdout(io.StringIO()):
            setup.main(["install", "--bundle", str(self.bundle), "--game", "vice_city", "--vice-city-dir", str(self.game)])
            (self.game / "games.san_andreas.game.exe").unlink()
            setup.main(["repair", "--bundle", str(self.bundle), "--game", "vice_city"])
        self.assertTrue(self.installer.check()["ok"])

    def test_executable_path_installs_and_records_the_game_folder(self):
        self.index()
        with patch("build.setup.setup.ensure_closed"), contextlib.redirect_stdout(io.StringIO()):
            setup.main(["install", "--bundle", str(self.bundle), "--game", "vice_city",
                        "--vice-city-dir", str(self.game / self.definition["executable"])])
        self.assertTrue(self.installer.check()["ok"])
        locations = json.loads((self.bundle / "runtime/installations.json").read_text())
        self.assertEqual(locations["vice_city"], str(self.game.absolute()))
        self.installer.apply("restore")
        self.assertEqual((self.game / self.definition["executable"]).read_bytes(), b"original executable")

    def test_executable_path_rejects_launcher_or_wrong_game(self):
        launcher = self.game / "Launcher.exe"
        launcher.write_bytes(b"launcher")
        with self.assertRaisesRegex(ValueError, "Select games.san_andreas.game.exe"):
            setup.game_directory(launcher, self.definition["executable"])
        with self.assertRaisesRegex(ValueError, "Game executable not found"):
            setup.game_directory(self.game / "missing.exe", self.definition["executable"])

    def upgraded_installer(self):
        definition = copy.deepcopy(self.definition)
        definition["component_id"] = "test-component-graphics"
        definition["previous_versions"] = [{"component_id": self.definition["component_id"],
            "files": {e["path"]: e["sha256"] for e in self.definition["files"]}}]
        (self.bundle / "payload/graphics.asi").write_bytes(b"graphics plugin")
        definition["files"].append({"path": "scripts/graphics.asi", "payload": "payload/graphics.asi",
            "sha256": digest(b"graphics plugin"), "accepted_before": [None]})
        return setup.Installer(self.bundle, self.game, "vice_city", definition)

    def test_graphics_upgrade_retains_original_backups_and_restores_everything(self):
        self.installer.apply("install")
        original = self.record()["entries"]
        upgraded = self.upgraded_installer()
        self.assertEqual(upgraded.apply("install")["changed"], 1)
        self.assertTrue(upgraded.check()["ok"])
        self.assertEqual(self.record()["entries"][:2], original)
        self.assertEqual(upgraded.apply("repair")["changed"], 0)
        upgraded.apply("restore")
        self.assertEqual((self.game / "games.san_andreas.game.exe").read_bytes(), b"original executable")
        self.assertFalse((self.game / "scripts/graphics.asi").exists())
        self.assertFalse((self.game / "options.ini").exists())

    def test_upgrade_rejects_unrelated_existing_graphics_file_before_writing(self):
        self.installer.apply("install")
        upgraded = self.upgraded_installer()
        target = self.game / "scripts/graphics.asi"
        target.parent.mkdir()
        target.write_bytes(b"another mod")
        before = self.record()
        with self.assertRaisesRegex(ValueError, "Unrecognized existing file"):
            upgraded.apply("repair")
        self.assertEqual(self.record(), before)
        self.assertEqual(target.read_bytes(), b"another mod")

    def test_unpinned_predecessor_cannot_be_adopted(self):
        self.installer.apply("install")
        upgraded = self.upgraded_installer()
        upgraded.definition["previous_versions"] = []
        with self.assertRaisesRegex(ValueError, "another setup"):
            upgraded.apply("repair")

    def test_interrupted_upgrade_recovers_with_original_backup_record(self):
        self.installer.apply("install")
        original = self.record()
        upgraded = self.upgraded_installer()
        put = setup.put_file
        def interrupted(source, target):
            if target.name == "graphics.asi":
                raise KeyboardInterrupt()
            return put(source, target)
        with patch("build.setup.setup.put_file", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            upgraded.apply("install")
        self.assertTrue(upgraded.pending_path.exists())
        upgraded.recover()
        self.assertEqual(self.record(), original)
        upgraded.apply("repair")
        self.assertTrue(upgraded.check()["ok"])

    def test_new_bundle_can_restore_a_predecessor_without_first_upgrading(self):
        self.installer.apply("install")
        upgraded = self.upgraded_installer()
        upgraded.apply("restore")
        self.assertEqual((self.game / "games.san_andreas.game.exe").read_bytes(), b"original executable")
        upgraded.apply("install")
        self.assertTrue(upgraded.check()["ok"])


class StagingCleanupTests(unittest.TestCase):
    def test_cleanup_preserves_build_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'build/setup/setup.py'
            source.parent.mkdir(parents=True)
            source.write_text('installer source')
            stage = root / 'build/staging/suite-test'
            stage.mkdir(parents=True)
            (stage / 'temporary.txt').write_text('temporary output')
            with patch.object(build_suite, 'ROOT', root):
                build_suite.cleanup_stage(stage)
                with self.assertRaisesRegex(ValueError, 'unexpected build path'):
                    build_suite.cleanup_stage(source.parent)
            self.assertEqual(source.read_text(), 'installer source')
            self.assertFalse(stage.exists())


class RetiredComponentTests(unittest.TestCase):
    def test_retired_component_has_no_payload_and_only_accepts_known_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'games/vice_city/installer'
            source.mkdir(parents=True)
            name = 'scripts/GTWLightOcclusion.asi'
            expected = '1' * 64
            base = {'files': [], 'disable': [name], 'disabled_hashes': {name: expected}}
            (source / 'manifest.json').write_text(json.dumps(base))
            (source / 'first-person.json').write_text('{"files": []}')
            (root / 'games/vice_city/profile.json').write_text(json.dumps({
                'title': 'Vice City', 'executable': 'gta-vc.exe',
                'install_folder': 'Grand Theft Auto Vice City', 'controller_ready': True}))
            with patch.object(build_suite, 'ROOT', root):
                before = set(root.rglob('*'))
                described = build_suite.add_component(None, 'vice_city')
                self.assertEqual(set(root.rglob('*')), before)
                definition = build_suite.add_component(root / 'stage', 'vice_city')
                self.assertEqual(described, definition)
                self.assertEqual(definition['files'], [{
                    'path': name, 'payload': None, 'sha256': None, 'accepted_before': [None, expected]}])
                self.assertFalse((root / 'stage/games/vice_city/installer/payload').exists())
                base.pop('disabled_hashes')
                (source / 'manifest.json').write_text(json.dumps(base))
                with self.assertRaisesRegex(ValueError, 'Missing known hash'):
                    build_suite.add_component(root / 'stage', 'vice_city')


class GraphicsCapacityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.payload = Path(temporary.name)
        (self.payload / "scripts").mkdir()
        self.cfg = self.payload / "scripts/SALodLights.cfg"
        self.ini = self.payload / "scripts/III.VC.SA.LimitAdjuster.ini"
        self.cfg.write_text("[DISTANCE]\nViewDistance=3000\nDrawDistance=5\nDetailDistance=4\n")

    def test_each_native_scenery_limit_is_rejected_at_maximum_distance(self):
        capacities = {"Buildings": 32767, "Dummys": 32767, "PtrNodeSingle": 200000}
        for key in capacities:
            with self.subTest(key=key):
                limits = dict(capacities, **{key: 0})
                self.ini.write_text("[SALIMITS]\n" + "\n".join(f"{k}={v}" for k, v in limits.items()))
                with self.assertRaisesRegex(ValueError, key):
                    validate_graphics_capacity("san_andreas", self.payload)

    def test_verified_and_unlimited_scenery_capacity_are_accepted(self):
        for values in ((32767, 32767, 200000), ("unlimited",) * 3):
            self.ini.write_text("[SALIMITS]\n" + "\n".join(
                f"{key}={value}" for key, value in zip(("Buildings", "Dummys", "PtrNodeSingle"), values)))
            validate_graphics_capacity("san_andreas", self.payload)

    def test_missing_adjuster_configuration_is_rejected_for_maximum_distance(self):
        with self.assertRaisesRegex(ValueError, "limit adjuster configuration"):
            validate_graphics_capacity("san_andreas", self.payload)

    def test_modest_preset_and_other_games_do_not_require_sa_scenery_pools(self):
        validate_graphics_capacity("vice_city", self.payload)
        self.cfg.write_text("[DISTANCE]\nViewDistance=0\nDrawDistance=1.5\nDetailDistance=1.5\n")
        validate_graphics_capacity("san_andreas", self.payload)


if __name__ == "__main__":
    unittest.main()
