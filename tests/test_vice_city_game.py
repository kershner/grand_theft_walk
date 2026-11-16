"""Reversible map opening, native movement, and allocation reuse guards."""

import struct
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.san_andreas.helpers import MemoryFixture
from gtw.controls import can_control
from gtw.navigation import Node, PedestrianGraph
from vice_city.game import (
    BARRIER_MODELS, CONNECTIONS, CONTROL_ACTIONS, COUNTS, Game, OBJECT_POOL,
    OBJECT_SIZE, PATHS, PLAYER_INFO, RuntimeChanges, WalkingPace,
    bind_walk_key, island_path_changes, load_destinations, read_graph, unlock_regions,
)


class WorldTests(unittest.TestCase):
    def setUp(self):
        self.mem = MemoryFixture()
        self.game = Game(self.mem, zones=[])
        self.changes = RuntimeChanges(self.mem)
        self.pool, self.objects, self.bitmap = 0x100000, 0x200000, 0x300000
        self.mem.write(OBJECT_POOL, struct.pack("<I", self.pool))
        self.mem.write(self.pool, struct.pack("<3I", self.objects, self.bitmap, 2))
        self.mem.write(self.bitmap, b"\x01\x02")
        for slot, model in enumerate((3518, 999)):
            self.mem.write(self.objects + slot * OBJECT_SIZE + 0x5A, struct.pack("<HH", 7, model))
            self.mem.write(self.objects + slot * OBJECT_SIZE + 0x51, b"\xff\xff")
        self.mem.write(PATHS + COUNTS, struct.pack("<3I", 2, 0, 2))
        self.mem.write(PATHS + 16, b"\x41")
        self.mem.write(PATHS + 20 + 16, b"\x21")

    def test_opening_preserves_objects_and_unrelated_flags_and_restores(self):
        self.assertEqual(unlock_regions(self.game, self.changes), 1)
        self.assertEqual(self.mem.read(self.objects + 0x51, 2), b"\xfe\xfb")
        self.assertEqual(self.mem.read(self.objects + OBJECT_SIZE + 0x51, 2), b"\xff\xff")
        self.assertEqual(self.mem.byte(PATHS + 16), 1)
        self.assertEqual(self.mem.byte(PATHS + 36), 0x21)
        self.assertEqual(self.changes.restore(), [])
        self.assertEqual(self.mem.read(self.objects + 0x51, 2), b"\xff\xff")
        self.assertEqual(self.mem.byte(PATHS + 16), 0x41)

    def test_reused_object_slot_does_not_receive_old_flags(self):
        unlock_regions(self.game, self.changes)
        self.mem.write(self.bitmap, b"\x03")
        self.changes.restore()
        self.assertEqual(self.mem.read(self.objects + 0x51, 2), b"\xfe\xfb")

    def test_changed_owned_bits_are_not_overwritten_during_restore(self):
        unlock_regions(self.game, self.changes)
        self.mem.write(self.objects + 0x51, b"\xff\xfb")
        self.changes.restore()
        self.assertEqual(self.mem.read(self.objects + 0x51, 2), b"\xff\xfb")

    def test_invalid_graph_blocks_unlocking_before_any_object_changes(self):
        self.mem.write(PATHS + COUNTS, struct.pack("<3I", 2, 0, 1))
        with self.assertRaisesRegex(ValueError, "not initialized"):
            unlock_regions(self.game, self.changes)
        self.assertEqual(self.mem.read(self.objects + 0x51, 2), b"\xff\xff")

    def test_movement_bindings_preserve_contextual_vehicle_bindings(self):
        self.mem.write(CONTROL_ACTIONS + 16 * 32, struct.pack("<II", ord("W"), 1))
        bind_walk_key(self.game, self.changes)
        self.assertEqual(self.mem.unpack(CONTROL_ACTIONS + 3 * 32, "2I"), (ord("W"), 1))
        self.assertEqual(self.mem.unpack(CONTROL_ACTIONS + 4 * 32, "2I"), (ord("S"), 1))
        self.assertEqual(self.mem.unpack(CONTROL_ACTIONS + 16 * 32, "2I"), (ord("W"), 1))

    def test_pace_caps_native_blend_without_compounding_or_touching_stale_player(self):
        pace = WalkingPace.__new__(WalkingPace)
        pace.mem, pace.limit, pace.game = self.mem, 0.75, self.game
        ped = 0x400000
        self.mem.write(PLAYER_INFO, struct.pack("<I", ped))
        self.game.identity = lambda value: "current"
        self.mem.write(ped + 0x5FC, struct.pack("<f", 2))
        state = {"ready": True, "ped": ped, "identity": "current"}
        pace.tick(state, True)
        pace.tick(state, True)
        self.assertEqual(self.mem.unpack(ped + 0x5FC, "f")[0], 0.75)
        self.mem.write(ped + 0x5FC, struct.pack("<f", 2))
        pace.tick(state, False)
        self.assertEqual(self.mem.unpack(ped + 0x5FC, "f")[0], 2)
        pace.tick(dict(state, identity="old"), True)
        self.assertEqual(self.mem.unpack(ped + 0x5FC, "f")[0], 2)

    def test_loading_memory_races_report_waiting_instead_of_crashing_the_controller(self):
        self.mem.pid = 123
        with patch.object(self.mem, "byte", side_effect=OSError("allocation changing")):
            self.assertFalse(self.game.snapshot()["ready"])

    def test_cutscenes_prevent_keyboard_control(self):
        self.assertFalse(can_control({"ready": True, "foreground": True, "paused": False,
                                      "controls_disabled": False, "in_vehicle": False,
                                      "cutscene": True, "health": 100, "player_state": 0}))

    def test_destinations_prefer_main_network_over_an_isolated_zone_centre(self):
        graph = PedestrianGraph([
            Node((0, 0), (10, 10, 0), (((0, 1), False),)),
            Node((0, 1), (20, 20, 0), (((0, 0), False), ((0, 2), False))),
            Node((0, 2), (30, 30, 0), (((0, 1), False),)),
            Node((0, 3), (49, 49, 0), (((0, 4), False),)),
            Node((0, 4), (50, 50, 0), (((0, 3), False),)),
        ])
        with patch("vice_city.game.load_zones", return_value=[("Area", (0, 0, -10, 100, 100, 10))]):
            destinations = load_destinations(Path("game"), graph, Mock(return_value={}))
        self.assertEqual(destinations[0]["node"], [0, 2])

    def test_island_opening_follows_closed_bridge_chains_and_preserves_other_closures(self):
        data = bytearray(COUNTS + 12)
        struct.pack_into("<3I", data, COUNTS, 3, 0, 3)
        for index, point in enumerate(((-240, -935, 10), (-260, -935, 10), (500, 500, 10))):
            at = index * 20
            struct.pack_into("<3h", data, at + 4, *(v * 8 for v in point))
            struct.pack_into("<H", data, at + 12, index)
            data[at + 16] = 0x21
        struct.pack_into("<3H", data, CONNECTIONS, 1, 0, 2)
        self.assertEqual(island_path_changes(data), {0: 0x60, 1: 0x60})
        data[20 + 17] = 1  # Never open water paths to pedestrians.
        self.assertEqual(island_path_changes(data), {0: 0x60})
