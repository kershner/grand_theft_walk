"""San Andreas startup, checkpoint selection, autosaves, and recovery."""

from core.files import read_state, write_state
from tests.fixtures import StateTestCase
import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.saves import NativeSaveRequest
from games.san_andreas.game import Autosave, MENU, SAVE_PREFIX
from games.san_andreas.launch import INTRO_SCRIPT, Startup, choose_save, valid_save
from tests.fixtures import FakeBridge, MemoryFixture, checkpoint


class NativeSaveTests(StateTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = NativeSaveRequest(Mock(pid=1234))
        self.client.folder = Path(self.temp.name)
        hashes = {}
        for name in ("save_request.exe", "save_bridge.dll"):
            data = name.encode()
            (self.client.folder / name).write_bytes(data)
            hashes[name] = hashlib.sha256(data).hexdigest()
        (self.client.folder / "manifest.json").write_text(
            json.dumps({"format": 1, "files": hashes})
        )

    def test_helper_is_hidden_and_receives_only_game_pid_and_save_slot(self):
        with patch("core.saves.subprocess.Popen") as popen:
            self.client.start(2)
        self.assertEqual(popen.call_args.args[0][1:], ["1234", "2"])
        self.assertTrue(popen.call_args.kwargs["creationflags"])

    def test_modified_bridge_is_rejected_before_starting_process(self):
        (self.client.folder / "save_bridge.dll").write_bytes(b"modified")
        with patch("core.saves.subprocess.Popen") as popen:
            with self.assertRaisesRegex(ValueError, "validation"):
                self.client.start(0)
        popen.assert_not_called()

    def test_pending_request_never_blocks_or_is_terminated_on_close(self):
        process = Mock()
        process.poll.return_value = None
        self.client.process = process
        self.assertIsNone(self.client.poll())
        self.client.close()
        process.communicate.assert_not_called()
        process.terminate.assert_not_called()

    def test_native_completion_and_bad_responses(self):
        for output, code, success in (
            (b'{"status":"saved","duration_ms":9}', 0, True),
            (b'{"status":"deferred"}', 3, True),
            (b"null", 0, False),
            (b"broken", 1, False),
        ):
            with self.subTest(output=output):
                process = Mock(returncode=code)
                process.poll.return_value = code
                process.communicate.return_value = (output, None)
                self.client.process = process
                if success:
                    self.assertIn(self.client.poll()["status"], ("saved", "deferred"))
                else:
                    with self.assertRaises(RuntimeError):
                        self.client.poll()


class AutosaveTests(StateTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.state_dir = root / "runtime"
        self.state_dir.mkdir()
        self.path = root / "GTASAsf1.b"
        self.original = checkpoint()
        self.path.write_bytes(self.original)
        self.record = self.state_dir / "launch"
        write_state(self.record, {"slot": 0, "path": str(self.path)})
        self.mem = MemoryFixture()
        self.mem.foreground = Mock(return_value=True)
        self.mem.write(SAVE_PREFIX, str(root / "GTASAsf").encode("mbcs") + b"\0")
        self.bridge = FakeBridge()
        self.saver = Autosave(self.mem, self.state_dir, bridge=self.bridge)
        self.saver.next_at = 60.0
        self.state = dict(
            ready=True,
            foreground=True,
            paused=False,
            controls_disabled=False,
            player_state=0,
            health=100,
            in_vehicle=False,
            interior=0,
            ped_state=1,
        )

    def page(self, page):
        self.mem.write(MENU + 0x5C, bytes((page is not None,)))
        if page is not None:
            self.mem.write(MENU + 0x15D, bytes((page,)))

    def start_save(self):
        self.assertTrue(self.saver.tick(self.state, 60))
        self.assertEqual(self.saver.phase, "saving")
        self.assertEqual(self.bridge.calls, [0])

    def test_save_runs_after_one_minute_and_keeps_previous_checkpoint(self):
        self.assertFalse(self.saver.tick(self.state, 59.9))
        self.start_save()
        new = checkpoint(12)
        self.path.write_bytes(new)
        self.bridge.result = {"status": "saved", "duration_ms": 8}
        self.saver.tick(self.state, 60.8)
        self.assertEqual(self.saver.count, 1)
        self.assertEqual(self.saver.backup.read_bytes(), self.original)
        self.assertEqual(self.mem.byte(MENU + 0x32), 0)
        record = read_state(self.record)
        self.assertEqual(record["sha256"], hashlib.sha256(new).hexdigest())
        self.assertEqual(self.saver.phase, "idle")
        self.assertAlmostEqual(self.saver.next_at, 120.8)
        self.assertFalse(self.saver.tick(self.state, 120.7))
        self.assertEqual(self.saver.duration_ms, 8)

    def test_unsafe_gameplay_defers_without_opening_menu(self):
        for key, value in (
            ("paused", True),
            ("controls_disabled", True),
            ("in_vehicle", True),
            ("in_water", True),
            ("interior", 5),
            ("health", 0),
        ):
            with self.subTest(key=key):
                state = dict(self.state, **{key: value})
                self.assertFalse(self.saver.tick(state, 60))
        self.assertFalse(self.saver.backup.exists())
        self.assertEqual(self.bridge.calls, [])

    def test_missions_cutscenes_and_door_transitions_defer_save(self):
        for address in (0xB5F851, 0x96A7CC):
            self.mem.write(address, struct.pack("<I", 1))
            self.assertFalse(self.saver.tick(self.state, 60))
            self.mem.write(address, bytes(4))
        self.mem.write(0xA476AC, struct.pack("<I", 8))
        self.mem.write(0xA49960 + 8, struct.pack("<I", 1))
        self.assertFalse(self.saver.tick(self.state, 60))

    def test_background_save_uses_native_bridge_without_changing_game_memory(self):
        before = dict(self.mem.data)
        state = dict(self.state, foreground=False)
        self.assertTrue(self.saver.tick(state, 60))
        self.assertEqual(self.bridge.calls, [0])
        self.saver.tick(state, 90)
        self.bridge.result = {"status": "saved"}
        self.saver.tick(state, 91)
        self.assertEqual(self.saver.count, 1)
        self.assertEqual(self.mem.data, before)

    def test_existing_pause_menu_can_save_without_opening_or_closing_it(self):
        self.page(41)
        state = dict(self.state, foreground=False, paused=True, controls_disabled=True)
        self.assertTrue(self.saver.tick(state, 60))
        self.assertEqual(self.mem.byte(MENU + 0x15D), 41)
        self.assertEqual(self.mem.byte(MENU + 0x32), 0)

    def test_occupied_save_slot_mismatch_is_never_overwritten(self):
        write_state(self.record, {"slot": 1, "path": str(self.path)})
        with self.assertLogs("core.saves", level="WARNING"):
            self.assertFalse(self.saver.tick(self.state, 60))
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.mem.byte(MENU + 0x5F), 0)
        self.assertIsNotNone(self.saver.error)

    def test_invalid_native_save_restores_backup_and_reports_error(self):
        self.start_save()
        self.path.write_bytes(b"incomplete save")
        self.bridge.result = {"status": "saved"}
        with self.assertLogs("core.saves", level="WARNING"):
            self.saver.tick(self.state, 60.8)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.saver.count, 0)
        self.assertIsNotNone(self.saver.error)

    def test_pending_save_preserves_active_file_and_backup(self):
        self.start_save()
        self.path.write_bytes(b"save in progress")
        self.saver.tick(self.state, 90)
        self.assertEqual(self.path.read_bytes(), b"save in progress")
        self.assertEqual(self.saver.backup.read_bytes(), self.original)
        self.assertEqual(self.saver.phase, "saving")

    def test_deferred_native_request_retries_when_safe(self):
        self.start_save()
        self.bridge.result = {"status": "deferred"}
        self.assertFalse(self.saver.tick(self.state, 60.1))
        self.assertEqual(self.saver.count, 0)
        self.assertEqual(self.saver.next_at, 61.1)
        self.assertIsNone(self.saver.error)
        self.assertFalse(self.saver.tick(self.state, 61))
        self.bridge.result = None
        self.assertTrue(self.saver.tick(self.state, 61.1))
        self.assertEqual(self.bridge.calls, [0, 0])

    def test_disabled_autosave_waits_a_full_interval_after_reenable(self):
        self.assertFalse(self.saver.tick(self.state, 60, enabled=False))
        self.assertFalse(self.saver.tick(self.state, 119))
        self.assertTrue(self.saver.tick(self.state, 120))


class IntroTests(StateTestCase):
    def setUp(self):
        self.memory = Mock()
        self.memory.foreground.return_value = True
        self.memory.u32.return_value = 0xA90670
        self.script = bytearray(0xE0)
        self.script[8:16] = b"intro\0\0\0"
        self.script[0xC4] = self.script[0xDC] = 1
        struct.pack_into("<I", self.script, 0x14, INTRO_SCRIPT + 9809)
        self.bytecode = b"verified intro fixture"
        self.memory.read.side_effect = lambda address, size: (
            self.bytecode
            if address == INTRO_SCRIPT
            else bytes(self.script[8:16])
            if address == 0xA90678
            else bytes(self.script)
        )
        self.startup = Startup(self.memory, 10)

    def test_verified_intro_uses_existing_cleanup_call(self):
        with patch("games.san_andreas.launch.INTRO_SHA256", hashlib.sha256(self.bytecode).hexdigest()):
            self.assertTrue(self.startup.finish_intro())
        self.memory.write.assert_called_once_with(0xA90684, struct.pack("<I", INTRO_SCRIPT + 38))
        self.assertFalse(self.startup.finish_intro())

    def test_unknown_intro_is_never_modified(self):
        with self.assertRaisesRegex(RuntimeError, "Unsupported opening script"):
            self.startup.finish_intro()
        self.memory.write.assert_not_called()

    def test_intro_is_not_skipped_before_cinematic_setup_finishes(self):
        struct.pack_into("<I", self.script, 0x14, INTRO_SCRIPT + 2000)
        self.assertFalse(self.startup.finish_intro())
        self.memory.write.assert_not_called()

    def test_intro_is_not_modified_in_background(self):
        self.memory.foreground.return_value = False
        self.assertFalse(self.startup.finish_intro())
        self.memory.write.assert_not_called()


class FocusTests(StateTestCase):
    def test_startup_does_not_act_until_game_is_foreground(self):
        memory = Mock()
        memory.alive.return_value = True
        memory.foreground.side_effect = [False, False, True]
        predicate = Mock(return_value="ready")
        startup = Startup(memory, 10)
        with patch("core.session.time.sleep"), patch("core.session.key_down", return_value=False):
            self.assertEqual(startup.wait("Test startup", predicate), "ready")
        predicate.assert_called_once_with()
        self.assertEqual(memory.foreground.call_count, 3)
        memory.write.assert_not_called()


class SaveSelectionTests(StateTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.prefix = str(Path(self.temp.name) / "GTASAsf")

    def path(self, slot):
        return Path(f"{self.prefix}{slot + 1}.b")

    def save(self, slot):
        self.path(slot).write_bytes(checkpoint())

    def test_first_launch_uses_first_normal_slot(self):
        self.assertEqual(choose_save(self.prefix), (0, self.path(0), True))

    def test_existing_normal_save_is_reused(self):
        self.save(2)
        self.assertEqual(choose_save(self.prefix), (2, self.path(2), False))

    def test_corrupt_occupied_slot_is_preserved(self):
        self.path(0).write_bytes(b"personal data")
        self.assertEqual(choose_save(self.prefix), (1, self.path(1), True))
        self.assertEqual(self.path(0).read_bytes(), b"personal data")

    def test_known_damaged_save_is_not_overwritten(self):
        self.path(0).write_bytes(b"damaged")
        with self.assertRaisesRegex(RuntimeError, "damaged"):
            choose_save(self.prefix, {"slot": 0, "path": str(self.path(0))})

    def test_missing_known_save_is_recreated_in_same_slot(self):
        self.save(0)
        self.assertEqual(
            choose_save(self.prefix, {"slot": 3, "path": str(self.path(3))}),
            (3, self.path(3), True),
        )

    def test_changed_save_folder_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "folder changed"):
            choose_save(self.prefix, {"slot": 0, "path": "elsewhere"})

    def test_full_folder_is_not_overwritten(self):
        for slot in range(8):
            self.path(slot).write_bytes(b"occupied")
        with self.assertRaisesRegex(RuntimeError, "occupied"):
            choose_save(self.prefix)

    def test_checksum_detects_corruption(self):
        self.save(0)
        self.assertTrue(valid_save(self.path(0)))
        data = bytearray(self.path(0).read_bytes())
        data[100] = 1
        self.path(0).write_bytes(data)
        self.assertFalse(valid_save(self.path(0)))
