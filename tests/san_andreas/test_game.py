"""Tests for interior access, faction neutrality, and runtime-state restoration."""

import hashlib
import math
import struct
import tempfile
import unittest
import zlib
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
    ZONE_COUNT,
    ZONES,
    FirstPersonCamera,
    Game,
    RuntimeChanges,
    WalkingPace,
    apply_peaceful_mode,
    bind_walk_key,
    read_game_text,
    unlock_interiors,
)

from .helpers import MemoryFixture


class WalkingPaceTests(unittest.TestCase):
    def setUp(self):
        self.mem = MemoryFixture()
        self.pace = WalkingPace(self.mem, 0.75)
        self.state = {
            "ready": True, "player_info": 0x10000, "ped": 0x20000,
            "identity": "01020304",
        }
        self.mem.write(0x10000, struct.pack("<I", 0x20000))
        self.mem.write(0x20020, bytes.fromhex(self.state["identity"]))
        self.mem.write(0x20018, struct.pack("<I", 0x30000))
        self.mem.write(0xB5F878, struct.pack("<I", 0x40))
        self.mem.write(0x30040, struct.pack("<I", 0x40000))
        self.mem.write(0x40000, struct.pack("<I", 0x50004))
        self.mem.write(0x5000C, struct.pack("<HHII", 32, 0, 0x60000, 0x70000))
        self.mem.write(0x50024, struct.pack("<f", 1.2))
        self.mem.write(0x5002C, struct.pack("<hH", 0, 0x23))

    def speed(self):
        return self.mem.unpack(0x50024, "f")[0]

    def test_slowdown_does_not_compound_and_stopping_restores_original(self):
        for _ in range(10):
            self.pace.tick(self.state, True)
            self.assertAlmostEqual(self.speed(), 0.9)
        self.pace.tick(self.state, False)
        self.assertAlmostEqual(self.speed(), 1.2)
        self.assertFalse(self.pace.owned)

    def test_game_speed_updates_become_the_new_baseline(self):
        self.pace.tick(self.state, True)
        self.mem.write(0x50024, struct.pack("<f", 1.0))
        self.pace.tick(self.state, True)
        self.assertAlmostEqual(self.speed(), 0.75)
        self.pace.tick(self.state, False)
        self.assertAlmostEqual(self.speed(), 1.0)

    def test_deleted_or_recycled_associations_are_not_restored(self):
        for address in (0x40000, 0x50014, 0x20020):
            with self.subTest(address=address):
                self.setUp()
                self.pace.tick(self.state, True)
                self.mem.write(address, b"\0" * 4)
                before = dict(self.mem.data)
                self.pace.tick(self.state, False)
                self.assertEqual(self.mem.data, before)

    def test_other_animations_and_native_speed_are_untouched(self):
        for anim, flags in ((1, 0x23), (2, 0x23), (3, 0x3), (0, 0x33)):
            with self.subTest(anim=anim, flags=flags):
                self.mem.write(0x5002C, struct.pack("<hH", anim, flags))
                before = dict(self.mem.data)
                self.pace.tick(self.state, True)
                self.assertEqual(self.mem.data, before)
        self.mem.write(0x5002C, struct.pack("<hH", 0, 0x23))
        WalkingPace(self.mem, 1.0).tick(self.state, True)
        self.assertAlmostEqual(self.speed(), 1.2)

    def test_unloaded_player_and_external_speed_changes_are_untouched(self):
        self.pace.tick(self.state, True)
        self.mem.write(0x50024, struct.pack("<f", 1.1))
        self.pace.tick(self.state, False)
        self.assertAlmostEqual(self.speed(), 1.1)
        self.pace.tick({"ready": False}, False)

    def test_invalid_multiplier_is_rejected(self):
        for value in (0, -1, 1.1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                WalkingPace(self.mem, value)


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.mem = MemoryFixture()
        self.game = Game(self.mem)
        self.game.place_names = {}
        zones = []
        for key, name, bounds in (
            (b"SAN_AND", "San Andreas", (-3000, -3000, -500, 3000, 3000, 500)),
            (b"LA", "Los Santos", (0, -3000, -500, 3000, 0, 500)),
            (b"CHC", "Las Colinas", (1900, -1100, 0, 2200, -900, 200)),
        ):
            self.game.place_names[zlib.crc32(key) ^ 0xFFFFFFFF] = name
            zones.append(struct.pack("<8s8s6hhBB", key, key, *bounds, 0, 0, 1))
        self.mem.write(ZONE_COUNT, struct.pack("<h", len(zones)))
        self.mem.write(ZONES, b"".join(zones))
        self.mem.write(0xB70152, bytes((32, 16, 14, 7)))
        self.state = {"position": {"x": 2017, "y": -978, "z": 37}, "heading": 0.0, "interior": 0}

    def test_neighborhood_city_clock_and_compass_come_from_game_state(self):
        location = self.game.location(self.state)
        self.assertEqual(location["places"], ["Las Colinas", "Los Santos"])
        self.assertEqual(location["time"], "4:32 PM")
        self.assertEqual(location["date"], "Jul 14")
        self.assertEqual(location["period"], "afternoon")
        self.assertEqual(location["heading"], "north")
        self.assertFalse(location["indoors"])
        for hour, minute, expected in ((0, 0, "12:00 AM"), (12, 0, "12:00 PM"),
                                       (9, 5, "9:05 AM"), (23, 59, "11:59 PM")):
            self.mem.write(0xB70152, bytes((minute, hour, 14, 7)))
            self.assertEqual(self.game.location(self.state)["time"], expected)

    def test_effective_rain_wind_and_wet_roads_are_reported(self):
        self.mem.write(0xC81324, struct.pack("<f", 0.8))
        self.mem.write(0xC812F0, struct.pack("<f", 0.8))
        self.mem.write(0xC81308, struct.pack("<f", 1.0))
        self.assertEqual(self.game.location(self.state)["weather"], "heavy rain, strong wind")
        self.mem.write(0xC81324, struct.pack("<f", 0.0))
        self.assertEqual(
            self.game.location(self.state)["weather"], "clear skies, strong wind, wet roads"
        )

    def test_fog_sandstorms_and_night_are_distinguished(self):
        self.mem.write(0xC81300, struct.pack("<f", 0.9))
        self.assertEqual(self.game.location(self.state)["weather"], "dense fog")
        self.mem.write(0xC812F4, struct.pack("<f", 0.5))
        self.mem.write(0xB70153, bytes((23,)))
        location = self.game.location(self.state)
        self.assertEqual(location["weather"], "sandstorm")
        self.assertEqual(location["period"], "night")

    def test_interiors_and_missing_zone_data_do_not_invent_places(self):
        self.mem.write(ZONE_COUNT, struct.pack("<h", 0))
        self.state["interior"] = 5
        location = self.game.location(self.state)
        self.assertEqual(location["places"], ["San Andreas"])
        self.assertTrue(location["indoors"])

    def test_game_text_tables_decode_real_labels_and_reject_truncation(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "american.gxt"
        key = zlib.crc32(b"CHC") ^ 0xFFFFFFFF
        keys = struct.pack("<II", 0, key)
        text = b"~b~Las Colinas~s~\0"
        data = struct.pack("<HH", 4, 8)
        for tag, chunk in ((b"TABL", b"unused"), (b"TKEY", keys), (b"TDAT", text)):
            data += struct.pack("<4sI", tag, len(chunk)) + chunk
        path.write_bytes(data)
        self.assertEqual(read_game_text(path)[key], "Las Colinas")
        path.write_bytes(data[:-1])
        with self.assertRaisesRegex(ValueError, "Truncated"):
            read_game_text(path)


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
    def test_snapshot_reports_pitch_from_camera_forward_vector(self):
        memory = MemoryFixture()
        memory.pid = 1
        memory.foreground = lambda: True
        memory.write(0xB7CD98, struct.pack("<I", 0x20000))
        game = Game(memory)
        for pitch in (-0.7, 0, 0.7):
            memory.write(
                0xB6F028 + 0x974 + 0x10,
                struct.pack("<3f", 0, math.cos(pitch), math.sin(pitch)),
            )
            self.assertAlmostEqual(game.snapshot()["camera_pitch"], pitch)

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
