"""Vice City autosave scheduling, safe transitions, backups, and recovery."""

import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from core.saves import NativeSaveRequest
from tests.san_andreas.helpers import MemoryFixture
from tests.san_andreas.test_saves import FakeBridge
from tests.test_vice_city_launch import checkpoint
from games.vice_city.game import Autosave, MENU, SAVE_PREFIX, SCRIPT_SPACE, valid_save


class AutosaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.state_dir = root / "runtime"
        self.state_dir.mkdir()
        self.path = root / "GTAVCsf1.b"
        self.original = checkpoint()
        self.path.write_bytes(self.original)
        self.record = self.state_dir / "launch.json"
        self.record.write_text(json.dumps({"slot": 0, "path": str(self.path)}))
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
        self.assertEqual(json.loads(self.record.read_text())["sha256"], hashlib.sha256(new).hexdigest())

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
        self.record.write_text(json.dumps({"slot": 1, "path": str(self.path)}))
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
