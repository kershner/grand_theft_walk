"""Tests for launch focus handling, intro completion, and save-slot selection."""

import hashlib
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from launch import INTRO_SCRIPT, Startup, choose_save, valid_save

from .helpers import checkpoint


class IntroTests(unittest.TestCase):
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
        with patch("launch.INTRO_SHA256", hashlib.sha256(self.bytecode).hexdigest()):
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


class FocusTests(unittest.TestCase):
    def test_startup_does_not_act_until_game_is_foreground(self):
        memory = Mock()
        memory.alive.return_value = True
        memory.foreground.side_effect = [False, False, True]
        predicate = Mock(return_value="ready")
        startup = Startup(memory, 10)
        with patch("launch.time.sleep"), patch("launch.key_down", return_value=False):
            self.assertEqual(startup.wait("Test startup", predicate), "ready")
        predicate.assert_called_once_with()
        self.assertEqual(memory.foreground.call_count, 3)
        memory.write.assert_not_called()


class SaveSelectionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
