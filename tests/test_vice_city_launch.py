"""Vice City normal checkpoint selection and native startup transitions."""

import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from vice_city.launch import MAIN_MENU, NEW_GAME, MENU, SAVE_SIZE, Startup, choose_save, valid_save


def checkpoint():
    data = bytearray()
    for _ in range(21):
        data += struct.pack("<I", 9600) + bytes(9600)
    struct.pack_into("<I", data, 68, SAVE_SIZE)
    return data + struct.pack("<I", sum(data) & 0xFFFFFFFF)


class SaveTests(unittest.TestCase):
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


class NewGameTests(unittest.TestCase):
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
        with patch("vice_city.launch.time.monotonic", return_value=10):
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
