"""Reversible map opening, native movement, and allocation reuse guards."""

import math
import struct
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.san_andreas.helpers import MemoryFixture
from gtw.controls import HighJump, can_control
from gtw.navigation import Node, PedestrianGraph
from vice_city.game import (
    BARRIER_MODELS, CONNECTIONS, CONTROL_ACTIONS, COUNTS, Game, OBJECT_POOL,
    OBJECT_SIZE, PATHS, PED_POOL, PLAYER_INFO, JUMP_ACTION, JUMP_KEY, JUMP_SPEED,
    CAPABILITIES, RuntimeChanges, WalkingPace,
    apply_protection, bind_jump_key, bind_walk_key, island_path_changes, load_destinations, read_graph, unlock_regions,
)


class JumpTests(unittest.TestCase):
    def setUp(self):
        self.mem = MemoryFixture()
        self.mem.foreground = lambda: True
        self.game = Game(self.mem, zones=[])
        self.ped, self.pool, self.bitmap = 0x200000, 0x100000, 0x300000
        self.mem.write(PLAYER_INFO, struct.pack("<I", self.ped))
        self.mem.write(PED_POOL, struct.pack("<I", self.pool))
        self.mem.write(self.pool, struct.pack("<3I", self.ped, self.bitmap, 1))
        self.mem.write(self.bitmap, b"\x01")
        self.mem.write(self.ped + 0x14C, b"\x00\x08")
        self.mem.write(self.ped + 0x70, struct.pack("<3f", 0.1, 0.2, 0.12))
        self.state = {
            "ready": True, "ped": self.ped, "identity": self.game.identity(self.ped),
            "in_air": True, "standing": False, "vertical_speed": 0.12,
            "foreground": True, "paused": False, "controls_disabled": False,
            "in_vehicle": False, "health": 100, "player_state": 0,
        }

    def test_boost_only_changes_vertical_velocity(self):
        before = dict(self.mem.data)
        self.assertTrue(self.game.boost_jump(self.state))
        self.assertAlmostEqual(self.mem.unpack(self.ped + 0x78, "f")[0], JUMP_SPEED)
        self.assertEqual(self.mem.data.keys(), before.keys())
        for address, value in before.items():
            if not self.ped + 0x78 <= address < self.ped + 0x7C:
                self.assertEqual(self.mem.data[address], value)

    def test_invalid_or_nonascending_takeoffs_are_rejected_without_writes(self):
        for speed in (0, -0.1, math.nan, math.inf):
            with self.subTest(speed=speed):
                self.mem.write(self.ped + 0x78, struct.pack("<f", speed))
                before = dict(self.mem.data)
                self.assertFalse(self.game.boost_jump(self.state))
                self.assertEqual(self.mem.data, before)
        self.mem.write(self.ped + 0x78, struct.pack("<f", 0.12))
        for flags in (b"\x01\x08", b"\x00\x00"):
            self.mem.write(self.ped + 0x14C, flags)
            before = dict(self.mem.data)
            self.assertFalse(self.game.boost_jump(self.state))
            self.assertEqual(self.mem.data, before)

    def test_fast_native_takeoff_is_reduced_to_the_configured_jump_speed(self):
        self.mem.write(self.ped + 0x78, struct.pack("<f", 0.6))
        self.assertTrue(self.game.boost_jump(self.state))
        self.assertAlmostEqual(self.mem.unpack(self.ped + 0x78, "f")[0], JUMP_SPEED)

    def test_stale_snapshot_and_reused_player_slot_are_rejected(self):
        for update in ({"ready": False}, {"standing": True}, {"in_air": False}, {"identity": "old"}):
            self.assertFalse(self.game.boost_jump(dict(self.state, **update)))
        self.mem.write(self.bitmap, b"\x02")
        before = dict(self.mem.data)
        self.assertFalse(self.game.boost_jump(self.state))
        self.assertEqual(self.mem.data, before)
        self.mem.write(self.bitmap, b"\x01")
        self.mem.write(PLAYER_INFO, struct.pack("<I", self.ped + 4))
        self.assertFalse(self.game.boost_jump(self.state))

    def test_binding_removes_conflicts_and_restores_all_owned_changes(self):
        self.mem.write(CONTROL_ACTIONS + 12 * 32, struct.pack("<II", JUMP_KEY, 1))
        self.mem.write(CONTROL_ACTIONS + 19 * 32 + 8, struct.pack("<II", JUMP_KEY, 2))
        before = self.mem.read(CONTROL_ACTIONS, 44 * 32)
        changes = RuntimeChanges(self.mem)
        bind_jump_key(self.game, changes)
        bind_jump_key(self.game, changes)
        self.assertEqual(self.mem.unpack(CONTROL_ACTIONS + JUMP_ACTION * 32, "2I"), (32, 1))
        self.assertEqual(self.mem.u32(CONTROL_ACTIONS + 12 * 32), 1056)
        self.assertEqual(self.mem.u32(CONTROL_ACTIONS + 19 * 32 + 8), 1056)
        self.assertEqual(changes.restore(), [])
        self.assertEqual(self.mem.read(CONTROL_ACTIONS, 44 * 32), before)

    def test_shared_jump_drives_vice_city_takeoff_and_boosts_once(self):
        self.assertTrue(CAPABILITIES.jump)
        changes = RuntimeChanges(self.mem)
        with patch("gtw.controls.Keys") as keys, patch.object(self.game, "boost_jump", wraps=self.game.boost_jump) as boost:
            jump = HighJump(self.game, changes, binder=bind_jump_key)
            request = {"id": "vice-jump", "issued": time.time()}
            jump.tick(request, dict(self.state, standing=True, in_air=False, vertical_speed=0), 10)
            keys.return_value.set.assert_called_once_with({"jump"})
            boost.assert_not_called()
            jump.tick(request, self.state, 10.2)
            jump.tick(request, self.state, 10.3)
            boost.assert_called_once_with(self.state)
            keys.return_value.release.assert_called()
            self.assertIsNone(jump.player)


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.mem = MemoryFixture()
        self.game = Game(self.mem, zones=[])
        self.ped, self.pool, self.bitmap, self.wanted = 0x200000, 0x100000, 0x300000, 0x400000
        self.mem.write(PLAYER_INFO, struct.pack("<I", self.ped))
        self.mem.write(PED_POOL, struct.pack("<I", self.pool))
        self.mem.write(self.pool, struct.pack("<3I", self.ped, self.bitmap, 1))
        self.mem.write(self.bitmap, b"\x01")
        self.mem.write(self.ped + 0x52, b"\x81\xc1")
        self.mem.write(self.ped + 0x154, b"\x80")
        self.mem.write(self.ped + 0x156, b"\xff")
        self.mem.write(self.ped + 0x354, struct.pack("<2f", 25, 50))
        self.mem.write(PLAYER_INFO + 0x143, b"\x96")
        self.mem.write(self.ped + 0x5F4, struct.pack("<I", self.wanted))
        self.mem.write(self.wanted + 0x14, struct.pack("<f", 1))
        for offset in (0, 4, 0x20, 0x24, 0x28, 0x28 + 15 * 0x1C):
            self.mem.write(self.wanted + offset, struct.pack("<I", 5))
        self.state = {"ready": True, "ped": self.ped, "identity": self.game.identity(self.ped)}
        self.changes = RuntimeChanges(self.mem)

    def test_protection_preserves_other_flags_heals_and_clears_wanted(self):
        self.assertTrue(CAPABILITIES.protection)
        self.assertFalse(CAPABILITIES.peaceful)
        apply_protection(self.game, self.changes, self.state)
        self.assertEqual(self.mem.read(self.ped + 0x52, 2), b"\x83\xff")
        self.assertEqual(self.mem.byte(self.ped + 0x154), 0x82)
        self.assertEqual(self.mem.byte(self.ped + 0x156), 0xFD)
        self.assertEqual(self.mem.unpack(self.ped + 0x354, "2f"), (150, 50))
        self.assertEqual(self.mem.byte(PLAYER_INFO + 0x140), 1)
        self.assertEqual(self.mem.byte(PLAYER_INFO + 0x142), 1)
        for offset in (0, 4, 0x14, 0x20, 0x24, 0x28, 0x28 + 15 * 0x1C):
            self.assertEqual(self.mem.u32(self.wanted + offset), 0)

    def test_shutdown_restores_settings_but_keeps_health_and_cleared_chase(self):
        apply_protection(self.game, self.changes, self.state)
        apply_protection(self.game, self.changes, self.state)
        self.assertEqual(self.changes.restore(), [])
        self.assertEqual(self.mem.read(self.ped + 0x52, 2), b"\x81\xc1")
        self.assertEqual(self.mem.byte(self.ped + 0x154), 0x80)
        self.assertEqual(self.mem.byte(self.ped + 0x156), 0xFF)
        self.assertEqual(self.mem.byte(PLAYER_INFO + 0x140), 0)
        self.assertEqual(self.mem.byte(PLAYER_INFO + 0x142), 0)
        self.assertEqual(self.mem.unpack(self.wanted + 0x14, "f")[0], 1)
        self.assertEqual(self.mem.unpack(self.ped + 0x354, "f")[0], 150)
        self.assertEqual(self.mem.u32(self.wanted + 0x20), 0)

    def test_dead_loading_or_stale_players_are_untouched(self):
        for update in ({"ready": False}, {"identity": "old"}):
            before = dict(self.mem.data)
            apply_protection(self.game, self.changes, dict(self.state, **update))
            self.assertEqual(self.mem.data, before)
        for health in (0, -1, math.nan, math.inf):
            self.mem.write(self.ped + 0x354, struct.pack("<f", health))
            before = dict(self.mem.data)
            apply_protection(self.game, self.changes, self.state)
            self.assertEqual(self.mem.data, before)
        self.mem.write(self.ped + 0x354, struct.pack("<f", 25))
        self.mem.write(PLAYER_INFO + 0xCC, b"\x01")
        before = dict(self.mem.data)
        apply_protection(self.game, self.changes, self.state)
        self.assertEqual(self.mem.data, before)

    def test_restore_skips_reused_ped_and_wanted_allocations(self):
        apply_protection(self.game, self.changes, self.state)
        self.mem.write(self.bitmap, b"\x02")
        self.mem.write(self.ped + 0x52, b"\x04")
        self.mem.write(self.wanted + 0x14, struct.pack("<f", 2))
        self.changes.restore()
        self.assertEqual(self.mem.byte(self.ped + 0x52), 4)
        self.assertEqual(self.mem.unpack(self.wanted + 0x14, "f")[0], 2)
        self.assertEqual(self.mem.byte(PLAYER_INFO + 0x140), 0)

    def test_changed_wanted_pointer_does_not_restore_old_allocation(self):
        apply_protection(self.game, self.changes, self.state)
        self.mem.write(self.ped + 0x5F4, struct.pack("<I", self.wanted + 0x1000))
        self.changes.restore()
        self.assertEqual(self.mem.unpack(self.wanted + 0x14, "f")[0], 0)


class LocationTests(unittest.TestCase):
    def test_sunny_weather_modes_are_clear_skies_even_at_night(self):
        mem = MemoryFixture()
        game = Game(mem, zones=[])
        state = {"position": {"x": 0, "y": 0, "z": 0}, "heading": 0, "interior": 0}
        for hour in (0, 12, 23):
            mem.write(0xA10B6B, bytes([hour]))
            for weather in (0, 4):
                mem.write(0xA10A2E, struct.pack("<h", weather))
                location = game.location(state)
                self.assertEqual(location["weather"], "clear skies")
                self.assertEqual(location["period"], "night" if hour in (0, 23) else "afternoon")


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
