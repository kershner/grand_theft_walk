"""Tests for interior access, faction neutrality, and runtime-state restoration."""

import hashlib
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from game import (
    CONTROL_ACTIONS,
    ENTRY_EXIT_POOL,
    ENTRY_EXIT_SIZE,
    FIRST_PERSON,
    GANG_WARS_ACTIVE,
    PED_POOL,
    PED_SIZE,
    RELATIONSHIP_TABLE,
    ROTATE_PLAYER,
    WALK_ACTION,
    WALK_KEY,
    FirstPersonCamera,
    Game,
    RuntimeChanges,
    apply_peaceful_mode,
    bind_walk_key,
    unlock_interiors,
)

from .helpers import MemoryFixture


class WalkBindingTests(unittest.TestCase):
    def setUp(self):
        self.mem = MemoryFixture()
        self.walk = CONTROL_ACTIONS + WALK_ACTION * 0x20
        self.mem.write(self.walk, struct.pack("<II", 1051, 1))
        self.game, self.changes = Game(self.mem), RuntimeChanges(self.mem)

    def test_rebinds_walk_without_modifiers_and_restores_existing_controls(self):
        other_primary = CONTROL_ACTIONS + 3 * 0x20
        other_secondary = CONTROL_ACTIONS + 5 * 0x20 + 8
        self.mem.write(other_primary, struct.pack("<II", WALK_KEY, 2))
        self.mem.write(other_secondary, struct.pack("<II", WALK_KEY, 3))
        before = self.mem.read(CONTROL_ACTIONS, 59 * 0x20)
        bind_walk_key(self.game, self.changes)
        self.assertEqual(self.mem.u32(self.walk), WALK_KEY)
        self.assertEqual(self.mem.u32(other_primary), 1056)
        self.assertEqual(self.mem.u32(other_secondary), 1056)
        self.assertEqual(self.mem.u32(other_primary + 4), 2)
        bind_walk_key(self.game, self.changes)
        self.assertEqual(self.changes.restore(), [])
        self.assertEqual(self.mem.read(CONTROL_ACTIONS, 59 * 0x20), before)

    def test_assigns_priority_for_a_previously_unbound_walk_key(self):
        self.mem.write(self.walk, struct.pack("<II", 1056, 0))
        bind_walk_key(self.game, self.changes)
        self.assertEqual(self.mem.unpack(self.walk, "II"), (WALK_KEY, 1))
        self.changes.restore()
        self.assertEqual(self.mem.unpack(self.walk, "II"), (1056, 0))


class InteriorTests(unittest.TestCase):
    def setUp(self):
        self.mem = MemoryFixture()
        self.objects, self.bitmap, self.pointer = 0x20000, 0x30000, 0x40000
        self.mem.write(ENTRY_EXIT_POOL, struct.pack("<I", self.pointer))
        self.mem.write(self.pointer, struct.pack("<IIi", self.objects, self.bitmap, 3))
        for i in range(3):
            block = bytearray(ENTRY_EXIT_SIZE)
            block[:8] = b"HOUSE\0\0\0"
            struct.pack_into("<4f", block, 8, 10, 20, 12, 22)
            struct.pack_into("<H", block, 0x30, 0x1980)
            block[0x34:0x36] = bytes((20, 6))
            self.mem.write(self.objects + i * ENTRY_EXIT_SIZE, block)
        self.mem.write(self.bitmap, bytes((1, 0x80, 1)))
        self.mem.write(self.objects + 2 * ENTRY_EXIT_SIZE + 0x30, struct.pack("<H", 0x8000))
        self.game, self.changes = Game(self.mem), RuntimeChanges(self.mem)

    def test_unlocks_only_live_physical_portals_and_preserves_other_flags(self):
        before = self.mem.read(self.objects, ENTRY_EXIT_SIZE * 3)
        self.assertEqual(unlock_interiors(self.game, self.changes), 1)
        self.assertEqual(self.mem.unpack(self.objects + 0x30, "H")[0], 0x5100)
        self.assertEqual(self.mem.read(self.objects + 0x34, 2), bytes((0, 24)))
        self.assertEqual(self.mem.read(self.objects, 0x30), before[:0x30])
        self.assertEqual(
            self.mem.read(self.objects + 0x36, ENTRY_EXIT_SIZE * 3 - 0x36), before[0x36:]
        )

    def test_restore_retains_game_transition_flags(self):
        unlock_interiors(self.game, self.changes)
        flags = self.mem.unpack(self.objects + 0x30, "H")[0] | 0x2000
        self.mem.write(self.objects + 0x30, struct.pack("<H", flags))
        self.assertEqual(self.changes.restore(), [])
        self.assertEqual(self.mem.unpack(self.objects + 0x30, "H")[0], 0x3980)
        self.assertEqual(self.mem.read(self.objects + 0x34, 2), bytes((20, 6)))

    def test_repeated_unlock_still_restores_original_hours(self):
        unlock_interiors(self.game, self.changes)
        unlock_interiors(self.game, self.changes)
        self.changes.restore()
        self.assertEqual(self.mem.read(self.objects + 0x34, 2), bytes((20, 6)))

    def test_restore_skips_recycled_allocation(self):
        unlock_interiors(self.game, self.changes)
        self.mem.write(self.bitmap, b"\x02")
        self.changes.restore()
        self.assertEqual(self.mem.read(self.objects + 0x34, 2), bytes((0, 24)))

    def test_restore_skips_replaced_pool(self):
        unlock_interiors(self.game, self.changes)
        self.mem.write(ENTRY_EXIT_POOL, struct.pack("<I", self.pointer + 0x100))
        self.changes.restore()
        self.assertEqual(self.mem.read(self.objects + 0x34, 2), bytes((0, 24)))

    def test_zero_size_placeholder_is_not_enabled(self):
        self.mem.write(self.objects + 8, bytes(16))
        self.assertEqual(unlock_interiors(self.game, self.changes), 0)


class PeacefulTests(unittest.TestCase):
    def setUp(self):
        self.mem = MemoryFixture()
        self.table, self.pointer, self.objects, self.bitmap = 0x50000, 0x60000, 0x70000, 0x80000
        self.mem.write(RELATIONSHIP_TABLE, struct.pack("<I", self.table))
        self.mem.write(PED_POOL, struct.pack("<I", self.pointer))
        self.mem.write(self.pointer, struct.pack("<IIi", self.objects, self.bitmap, 3))
        self.mem.write(self.bitmap, bytes((1, 1, 0x80)))
        for kind in range(32):
            self.mem.write(
                self.table + kind * 20, struct.pack("<5I", 1, 2, 4, 0x123FFFFF, 0x432FFFFF)
            )
        self.mem.write(GANG_WARS_ACTIVE, b"\x01")
        for i, kind in enumerate((7, 0, 8)):
            self.mem.write(self.objects + i * PED_SIZE + 0x598, struct.pack("<I", kind))
            self.mem.write(
                self.objects + i * PED_SIZE + 0x4E0,
                struct.pack("<5I", 1, 2, 4, 0x123FFFFF, 0x432FFFFF),
            )
        self.game, self.changes = Game(self.mem), RuntimeChanges(self.mem)

    def test_removes_only_player_hostility_from_templates_and_live_npcs(self):
        player_before = self.mem.read(self.objects + PED_SIZE, PED_SIZE)
        free_before = self.mem.read(self.objects + PED_SIZE * 2, PED_SIZE)
        self.assertEqual(apply_peaceful_mode(self.game, self.changes), 1)
        expected = (1, 2, 4, 0x123FFFF0, 0x432FFFF0)
        for kind in range(32):
            self.assertEqual(self.mem.unpack(self.table + kind * 20, "5I"), expected)
        self.assertEqual(self.mem.unpack(self.objects + 0x4E0, "5I"), expected)
        self.assertEqual(self.mem.read(self.objects + PED_SIZE, PED_SIZE), player_before)
        self.assertEqual(self.mem.read(self.objects + PED_SIZE * 2, PED_SIZE), free_before)
        self.assertEqual(self.mem.byte(GANG_WARS_ACTIVE), 0)

    def test_restore_preserves_other_relationship_changes(self):
        apply_peaceful_mode(self.game, self.changes)
        self.mem.write(self.table + 16, struct.pack("<I", 0x832FFFF0))
        self.assertEqual(self.changes.restore(), [])
        self.assertEqual(self.mem.u32(self.table + 16), 0x832FFFFF)
        self.assertEqual(self.mem.byte(GANG_WARS_ACTIVE), 1)
        self.assertEqual(self.mem.u32(self.objects + 0x4F0), 0x432FFFFF)

    def test_recycled_npc_is_not_restored(self):
        apply_peaceful_mode(self.game, self.changes)
        self.mem.write(self.bitmap, b"\x02")
        self.changes.restore()
        self.assertEqual(self.mem.u32(self.objects + 0x4F0), 0x432FFFF0)

    def test_replaced_relationship_table_is_not_restored(self):
        apply_peaceful_mode(self.game, self.changes)
        self.mem.write(RELATIONSHIP_TABLE, struct.pack("<I", self.table + 0x1000))
        self.changes.restore()
        self.assertEqual(self.mem.u32(self.table + 16), 0x432FFFF0)


if __name__ == "__main__":
    unittest.main()


class CameraTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "ZMenuSA.asi"
        data = b"verified camera fixture"
        path.write_bytes(data)
        self.hash_patch = patch("game.CAMERA_MODULE_SHA256", hashlib.sha256(data).hexdigest())
        self.hash_patch.start()
        self.addCleanup(self.hash_patch.stop)
        self.module = {"base": 0x10000000, "size": 0x600000, "path": path}
        self.mem = MemoryFixture()
        self.mem.module = Mock(return_value=self.module)
        self.camera = FirstPersonCamera(self.mem)

    def flag(self, offset):
        return self.mem.byte(self.module["base"] + offset)

    def test_toggle_and_shutdown_restore_original_camera_flags(self):
        self.assertTrue(self.camera.apply(True))
        self.assertEqual((self.flag(FIRST_PERSON), self.flag(ROTATE_PLAYER)), (1, 1))
        self.assertTrue(self.camera.apply(False))
        self.assertEqual((self.flag(FIRST_PERSON), self.flag(ROTATE_PLAYER)), (0, 0))
        self.camera.apply(True)
        self.camera.close()
        self.assertEqual((self.flag(FIRST_PERSON), self.flag(ROTATE_PLAYER)), (0, 0))

    def test_unknown_version_cannot_write_camera_memory(self):
        self.module["path"].write_bytes(b"unsupported version")
        self.assertFalse(self.camera.apply(True))
        self.assertIn("Unsupported", self.camera.error)
        self.assertFalse(self.mem.data)

    def test_backward_movement_keeps_first_person_without_forcing_player_rotation(self):
        self.camera.apply(True)
        self.assertTrue(self.camera.apply(True, rotate_player=False))
        self.assertEqual((self.flag(FIRST_PERSON), self.flag(ROTATE_PLAYER)), (1, 0))
        self.assertTrue(self.camera.apply(True))
        self.assertEqual((self.flag(FIRST_PERSON), self.flag(ROTATE_PLAYER)), (1, 1))

    def test_third_person_and_shutdown_restore_original_rotation_after_backing_up(self):
        self.mem.write(self.module["base"] + ROTATE_PLAYER, b"\x01")
        self.camera.apply(True, rotate_player=False)
        self.camera.apply(False, rotate_player=False)
        self.assertEqual((self.flag(FIRST_PERSON), self.flag(ROTATE_PLAYER)), (0, 1))
        self.camera.apply(True, rotate_player=False)
        self.camera.close()
        self.assertEqual((self.flag(FIRST_PERSON), self.flag(ROTATE_PLAYER)), (0, 1))

    def test_missing_mod_reports_unavailable(self):
        self.mem.module.return_value = None
        self.assertFalse(self.camera.apply(True))
        self.assertFalse(self.camera.report()["available"])
        self.assertFalse(self.mem.data)

    def test_invalid_or_out_of_bounds_flags_are_rejected(self):
        self.mem.write(self.module["base"] + FIRST_PERSON, b"\x02")
        self.assertFalse(self.camera.apply(True))
        self.assertEqual(self.flag(FIRST_PERSON), 2)
        self.module["size"] = 100
        self.assertFalse(self.camera.apply(True))
        self.assertEqual(self.flag(ROTATE_PLAYER), 0)

    def test_unloaded_module_is_not_restored(self):
        self.camera.apply(True)
        before = dict(self.mem.data)
        self.mem.module.return_value = None
        self.camera.close()
        self.assertEqual(self.mem.data, before)
