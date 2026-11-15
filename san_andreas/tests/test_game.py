"""Tests for interior access, faction neutrality, and runtime-state restoration."""
import struct
import unittest

from game import (ENTRY_EXIT_POOL, ENTRY_EXIT_SIZE, PED_POOL, PED_SIZE, RELATIONSHIP_TABLE,
                  GANG_WARS_ACTIVE, Game, RuntimeChanges, unlock_interiors, apply_peaceful_mode)
from tests.helpers import MemoryFixture


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
        self.assertEqual(self.mem.read(self.objects + 0x36, ENTRY_EXIT_SIZE * 3 - 0x36), before[0x36:])

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
            self.mem.write(self.table + kind * 20, struct.pack("<5I", 1, 2, 4, 0x123FFFFF, 0x432FFFFF))
        self.mem.write(GANG_WARS_ACTIVE, b"\x01")
        for i, kind in enumerate((7, 0, 8)):
            self.mem.write(self.objects + i * PED_SIZE + 0x598, struct.pack("<I", kind))
            self.mem.write(self.objects + i * PED_SIZE + 0x4E0,
                           struct.pack("<5I", 1, 2, 4, 0x123FFFFF, 0x432FFFFF))
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
