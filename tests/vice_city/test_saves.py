"""Vice City startup, checkpoint selection, autosaves, and recovery."""

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
from games.vice_city.game import Autosave, MENU, SAVE_PREFIX, SCRIPT_SPACE, valid_save
from games.vice_city.launch import MAIN_MENU, NEW_GAME, Startup, choose_save
from tests.fixtures import FakeBridge, MemoryFixture, classic_checkpoint as checkpoint


class AutosaveTests(StateTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.state_dir = root / "runtime"
        self.state_dir.mkdir()
        self.path = root / "GTAVCsf1.b"
        self.original = checkpoint()
        self.path.write_bytes(self.original)
        self.record = self.state_dir / "launch"
        write_state(self.record, {"slot": 0, "path": str(self.path)})
        self.mem = MemoryFixture()
        self.mem.write(SAVE_PREFIX, str(root / "GTAVCsf").encode("mbcs") + b"\0")
        self.bridge = FakeBridge()
        self.saver = Autosave(self.mem, self.state_dir, bridge=self.bridge)
        self.saver.next_at = 60
        self.state = dict(ready=True, foreground=True, paused=False, controls_disabled=False,
                          player_state=0, health=100, in_vehicle=False, interior=0,
                          ped_state=1, standing=True, in_air=False)

    def test_periodic_save_validates_new_checkpoint_and_retains_previous_backup(self):
        self.assertFalse(self.saver.tick(self.state, 59.9))
        self.assertTrue(self.saver.tick(self.state, 60))
        self.assertEqual(self.bridge.calls, [0])
        new = bytearray(self.original)
        new[100] = 12
        struct.pack_into("<I", new, len(new) - 4, sum(new[:-4]) & 0xFFFFFFFF)
        self.path.write_bytes(new)
        self.bridge.result = {"status": "saved", "duration_ms": 9}
        self.assertFalse(self.saver.tick(self.state, 60.2))
        self.assertTrue(valid_save(self.path))
        self.assertEqual(self.saver.backup.read_bytes(), self.original)
        self.assertEqual(self.saver.count, 1)
        self.assertEqual(self.saver.duration_ms, 9)
        self.assertAlmostEqual(self.saver.next_at, 120.2)
        self.assertEqual(read_state(self.record)["sha256"], hashlib.sha256(new).hexdigest())

    def test_unsafe_gameplay_never_starts_a_save(self):
        for key, value in (("ready", False), ("health", 0), ("in_vehicle", True),
                           ("interior", 1), ("paused", True), ("controls_disabled", True),
                           ("player_state", 1), ("ped_state", 24), ("standing", False), ("in_air", True),
                           ("in_water", True)):
            with self.subTest(key=key):
                self.assertFalse(self.saver.tick(dict(self.state, **{key: value}), 60))
        self.assertEqual(self.bridge.calls, [])
        self.assertFalse(self.saver.backup.exists())

    def test_loading_cutscenes_intro_and_missions_defer(self):
        for address in (MENU + 0x39, MENU + 0x3C, 0xA10AB2, 0xA10B8D):
            self.mem.write(address, b"\x01")
            self.assertFalse(self.saver.tick(self.state, 60))
            self.mem.write(address, b"\0")
        self.mem.write(0x978748, struct.pack("<I", 1252))
        self.mem.write(SCRIPT_SPACE + 1252, struct.pack("<I", 1))
        self.assertFalse(self.saver.tick(self.state, 60))
        for offset in (1, 7, 34336, 0xFFFFFFFF):
            self.mem.write(0x978748, struct.pack("<I", offset))
            self.assertFalse(self.saver.tick(self.state, 60))
        self.assertEqual(self.bridge.calls, [])

    def test_existing_pause_menu_saves_in_background_without_menu_writes(self):
        self.mem.write(MENU + 0x38, b"\x01")
        self.mem.write(MENU + 0xF8, struct.pack("<I", 32))
        before = dict(self.mem.data)
        state = dict(self.state, paused=True, controls_disabled=True, foreground=False)
        self.assertTrue(self.saver.tick(state, 60))
        self.assertEqual(self.mem.data, before)
        self.assertEqual(self.bridge.calls, [0])

    def test_other_menus_never_save(self):
        self.mem.write(MENU + 0x38, b"\x01")
        for page in (7, 12, 15, 17, 18, 29):
            self.mem.write(MENU + 0xF8, struct.pack("<I", page))
            self.assertFalse(self.saver.tick(self.state, 60))
        self.assertEqual(self.bridge.calls, [])

    def test_invalid_native_result_restores_previous_checkpoint(self):
        self.assertTrue(self.saver.tick(self.state, 60))
        self.path.write_bytes(b"partial")
        self.bridge.result = {"status": "saved"}
        with self.assertLogs("core.saves", level="WARNING"):
            self.saver.tick(self.state, 60.1)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertTrue(valid_save(self.saver.backup))
        self.assertEqual(self.saver.count, 0)
        self.assertIsNotNone(self.saver.error)

    def test_partial_save_is_not_restored_while_native_callback_is_still_running(self):
        self.saver.tick(self.state, 60)
        self.path.write_bytes(b"in progress")
        self.assertTrue(self.saver.tick(self.state, 61))
        self.assertEqual(self.path.read_bytes(), b"in progress")
        self.assertEqual(self.saver.backup.read_bytes(), self.original)

    def test_slot_mismatch_preserves_files_and_never_dispatches(self):
        write_state(self.record, {"slot": 1, "path": str(self.path)})
        with self.assertLogs("core.saves", level="WARNING"):
            self.assertFalse(self.saver.tick(self.state, 60))
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.bridge.calls, [])

    def test_native_deferral_retries_without_marking_a_save_completed(self):
        self.saver.tick(self.state, 60)
        self.bridge.result = {"status": "deferred"}
        self.assertFalse(self.saver.tick(self.state, 60.1))
        self.assertEqual(self.saver.count, 0)
        self.assertAlmostEqual(self.saver.next_at, 61.1)
        self.assertFalse(self.saver.tick(self.state, 61))
        self.bridge.result = None
        self.assertTrue(self.saver.tick(self.state, 61.1))

    def test_native_helper_rejects_invalid_slots_before_starting(self):
        request = NativeSaveRequest(Mock(pid=123))
        for slot in (-1, 8, True, "0"):
            with self.subTest(slot=slot), self.assertRaisesRegex(ValueError, "slot"):
                request.start(slot)


class SaveTests(StateTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.prefix = str(Path(self.temp.name) / "GTAVCsf")

    def path(self, slot=0):
        return Path(f"{self.prefix}{slot + 1}.b")

    def test_missing_checkpoint_uses_empty_slot_and_preserves_occupied_files(self):
        self.path().write_bytes(b"personal data")
        self.assertEqual(choose_save(self.prefix), (1, self.path(1), True))
        self.assertEqual(self.path().read_bytes(), b"personal data")

    def test_existing_valid_checkpoint_is_selected_and_reused(self):
        self.path(2).write_bytes(checkpoint())
        self.assertTrue(valid_save(self.path(2)))
        self.assertEqual(choose_save(self.prefix), (2, self.path(2), False))

    def test_recorded_missing_checkpoint_is_recreated_in_its_original_slot(self):
        self.assertEqual(choose_save(self.prefix, {"slot": 3, "path": str(self.path(3))}),
                         (3, self.path(3), True))

    def test_recorded_damaged_checkpoint_is_never_overwritten(self):
        self.path().write_bytes(b"damaged")
        with self.assertRaisesRegex(RuntimeError, "damaged"):
            choose_save(self.prefix, {"slot": 0, "path": str(self.path())})

    def test_all_occupied_slots_block_creation(self):
        for slot in range(8):
            self.path(slot).write_bytes(b"occupied")
        with self.assertRaisesRegex(RuntimeError, "occupied"):
            choose_save(self.prefix)

    def test_checksum_block_boundaries_and_game_marker_are_required(self):
        original = checkpoint()
        for kind in ("checksum", "size", "marker", "truncated"):
            data = bytearray(original)
            if kind == "checksum":
                data[100] = 1
            elif kind == "size":
                struct.pack_into("<I", data, 0, 0xFFFFFFFF)
            elif kind == "marker":
                struct.pack_into("<I", data, 68, 0)
            else:
                data = data[:-20]
            self.path().write_bytes(data)
            self.assertFalse(valid_save(self.path()), kind)


class NewGameTests(StateTestCase):
    def setUp(self):
        self.startup = Startup.__new__(Startup)
        self.startup.mem = Mock()
        self.startup.keys = Mock()
        self.startup.last_skip = 0

    def test_title_screen_selects_yes_in_new_game_confirmation(self):
        self.startup.menu = Mock(return_value=MAIN_MENU)
        self.startup.page = Mock()
        self.assertFalse(self.startup.start_new_game())
        self.startup.page.assert_called_once_with(NEW_GAME, item=2)
        self.startup.keys.tap.assert_not_called()

    def test_confirmation_reselects_yes_before_sending_enter(self):
        self.startup.menu = Mock(return_value=NEW_GAME)
        self.startup.mem.u32.return_value = 0
        self.assertFalse(self.startup.start_new_game())
        self.startup.mem.write.assert_called_once_with(MENU + 0x30, struct.pack("<I", 2))
        self.startup.keys.tap.assert_not_called()
        self.startup.mem.u32.return_value = 2
        with patch("games.vice_city.launch.time.monotonic", return_value=10):
            self.assertFalse(self.startup.start_new_game())
        self.startup.keys.tap.assert_called_once_with("enter")

    def test_world_transition_ends_new_game_menu_actions(self):
        self.startup.menu = Mock(return_value=None)
        self.assertTrue(self.startup.start_new_game())
        self.startup.keys.tap.assert_not_called()

    def test_unexpected_menu_aborts_instead_of_sending_keys(self):
        self.startup.menu = Mock(return_value=23)
        with self.assertRaisesRegex(RuntimeError, "Unexpected"):
            self.startup.start_new_game()
        self.startup.keys.tap.assert_not_called()

    def test_page_requires_an_active_foreground_menu(self):
        self.startup.mem.foreground.return_value = False
        with self.assertRaisesRegex(RuntimeError, "foreground"):
            self.startup.page(NEW_GAME, 2)
        self.startup.mem.write.assert_not_called()
