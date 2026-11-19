

import math
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.classic import main_pedestrian_network
from core.controller import Walking
from core.files import sha256
from core.navigation import Node, PedestrianGraph, angle_delta
from core.saves import seed_checkpoint
from games.gta_iii import game as iii
from games.gta_iii.game import TURN_MULTIPLIER
from tests.fixtures import MemoryFixture, classic_checkpoint as checkpoint


class PlayerTests(unittest.TestCase):
    def setUp(self):
        self.mem = MemoryFixture()
        self.mem.pid, self.mem.foreground = 123, lambda: True
        self.game = iii.Game(self.mem, zones=[])
        self.ped, self.pool, self.bitmap = 0x200000, 0x100000, 0x300000
        self.mem.write(iii.PLAYER_INFO, struct.pack("<I", self.ped))
        self.mem.write(iii.PED_POOL, struct.pack("<I", self.pool))
        self.mem.write(self.pool, struct.pack("<3I", self.ped, self.bitmap, 1))
        self.mem.write(self.bitmap, b"\x01")
        self.mem.write(self.ped + 0x2C0, struct.pack("<f", 80))
        self.mem.write(self.ped + 0x154, b"\x01")

    def test_snapshot_uses_iii_layout_and_rejects_recycled_player(self):
        self.mem.write(self.ped + 0x34, struct.pack("<3f", 10, 20, 30))
        state = self.game.snapshot()
        self.assertTrue(state["ready"])
        self.assertEqual(state["position"], {"x": 10, "y": 20, "z": 30})
        self.assertEqual(state["health"], 80)
        self.mem.write(self.bitmap, b"\x02")
        before = dict(self.mem.data)
        self.assertFalse(self.game.kill_player(state))
        self.assertEqual(before, self.mem.data)

    def test_loading_invalid_camera_and_nan_are_not_controllable(self):
        for address, value in ((iii.MENU + 0x114, b"\x01"),
                               (iii.CAMERA + 0x76, b"\x03"),
                               (self.ped + 0x34, struct.pack("<f", math.nan))):
            original = self.mem.read(address, len(value))
            self.mem.write(address, value)
            self.assertFalse(self.game.snapshot()["ready"])
            self.mem.write(address, original)

    def test_protection_restores_owned_bits_but_not_old_health_or_wanted(self):
        state = self.game.snapshot()
        changes = iii.RuntimeChanges(self.mem)
        iii.apply_protection(self.game, changes, state)
        self.assertEqual(self.mem.unpack(self.ped + 0x2C0, "f")[0], 100)
        self.assertEqual(self.mem.byte(self.ped + 0x53) & 0x1F, 0x1F)
        self.assertEqual(changes.restore(), [])
        self.assertEqual(self.mem.byte(self.ped + 0x53) & 0x1F, 0)
        self.assertEqual(self.mem.unpack(self.ped + 0x2C0, "f")[0], 100)

    def test_guard_does_not_restore_into_reused_pool_slot(self):
        changes = iii.RuntimeChanges(self.mem)
        state = self.game.snapshot()
        iii.apply_protection(self.game, changes, state)
        self.mem.write(self.bitmap, b"\x02")
        self.mem.write(self.ped + 0x53, b"\xff")
        changes.restore()
        self.assertEqual(self.mem.byte(self.ped + 0x53), 255)

    def test_jump_changes_only_vertical_velocity_and_checks_identity(self):
        self.mem.write(self.ped + 0x154, b"\x00\x08")
        self.mem.write(self.ped + 0x78, struct.pack("<3f", 0.1, 0.2, 0.3))
        state = self.game.snapshot()
        before = self.mem.read(self.ped + 0x78, 8)
        self.assertTrue(self.game.boost_jump(state))
        self.assertEqual(self.mem.read(self.ped + 0x78, 8), before)
        self.assertAlmostEqual(self.mem.unpack(self.ped + 0x80, "f")[0], iii.JUMP_SPEED)
        self.mem.write(self.bitmap, b"\x02")
        self.assertFalse(self.game.boost_jump(state))

    def test_native_story_progress_is_required_for_full_map(self):
        changes = iii.RuntimeChanges(self.mem)
        with self.assertRaisesRegex(ValueError, "full-map"):
            iii.unlock_regions(self.game, changes)
        for address in (iii.INDUSTRIAL_PASSED, iii.COMMERCIAL_PASSED):
            self.mem.write(address, struct.pack("<I", 1))
        before = dict(self.mem.data)
        self.assertEqual(iii.unlock_regions(self.game, changes), 0)
        self.assertEqual(self.mem.data, before)

    def test_hud_is_hidden_without_changing_frontend_and_restored_on_exit(self):
        self.mem.write(iii.HUD_VISIBLE, b"\x01")
        self.mem.write(iii.MENU + 0x111, b"\x01")
        changes = iii.RuntimeChanges(self.mem)
        iii.hide_hud(self.game, changes)
        iii.hide_hud(self.game, changes)
        self.assertEqual(self.mem.byte(iii.HUD_VISIBLE), 0)
        self.assertEqual(self.mem.byte(iii.MENU + 0x111), 1)
        self.assertEqual(changes.restore(), [])
        self.assertEqual(self.mem.byte(iii.HUD_VISIBLE), 1)

    def test_autosave_rejects_water_missions_and_loading(self):
        save = iii.Autosave(self.mem, Path("unused"), bridge=Mock())
        state = self.game.snapshot()
        self.assertTrue(save.safe(state))
        for change in ({"in_water": True}, {"standing": False}, {"in_vehicle": True}, {"cutscene": True}):
            self.assertFalse(save.safe(state | change))
        self.mem.write(iii.MISSION_FLAG, struct.pack("<I", 8))
        self.mem.write(iii.SCRIPT_SPACE + 8, struct.pack("<I", 1))
        self.assertFalse(save.safe(state))


class PathTests(unittest.TestCase):
    def graph_data(self):
        data = bytearray(iii.COUNTS + 12)
        struct.pack_into("<3I", data, iii.COUNTS, 3, 1, 2)
        for i, target in ((1, 2), (2, 1)):
            struct.pack_into("<3f", data, i * 32, i * 5, 10, 2)
            struct.pack_into("<HBB", data, i * 32 + 24, i, 1, 0)
            struct.pack_into("<H", data, iii.CONNECTIONS + i * 2, target)
        data[0x3E39A + 1] = 1
        return data

    def test_float_nodes_direct_links_and_crossings(self):
        graph = iii.read_graph(self.graph_data())
        self.assertEqual(graph.nodes[(0, 1)].position, (5, 10, 2))
        self.assertEqual(graph.nodes[(0, 1)].links, (((0, 2), True),))
        self.assertEqual(main_pedestrian_network(graph), {(0, 1), (0, 2)})

    def test_corrupt_graph_cannot_drive_navigation(self):
        data = self.graph_data()
        struct.pack_into("<H", data, 32 + 24, 65535)
        with self.assertRaisesRegex(ValueError, "links"):
            iii.read_graph(data)
        with self.assertRaisesRegex(ValueError, "Truncated"):
            iii.read_graph(data[:20])

    def test_native_long_hill_link_survives_iii_limits(self):
        data = self.graph_data()
        struct.pack_into("<3f", data, 32, 0, 0, 0)
        struct.pack_into("<3f", data, 64, 100, 0, 20)
        graph = iii.read_graph(data)
        self.assertIn((0, 2), [key for key, _ in graph.nodes[(0, 1)].links])

    def test_destinations_cover_reachable_networks_on_different_islands(self):
        from unittest.mock import patch
        graph = PedestrianGraph([
            Node((0, 1), (0, 0, 0), (((0, 2), False),)),
            Node((0, 2), (10, 0, 0), (((0, 1), False),)),
            Node((0, 3), (1000, 0, 0), (((0, 4), False),)),
            Node((0, 4), (1010, 0, 0), (((0, 3), False),)),
        ])
        zones = [("Portland", (-10, -10, -10, 20, 10, 10)),
                 ("Shoreside Vale", (990, -10, -10, 1020, 10, 10))]
        with patch.object(iii, "load_zones", return_value=zones):
            destinations = iii.load_destinations(Path("unused"), graph, Mock(return_value={}))
        self.assertEqual(len(destinations), 4)
        self.assertTrue(any(d["name"] == "Shoreside Vale" for d in destinations))


class SeedTests(unittest.TestCase):
    def test_seed_reserves_empty_slot_and_never_replaces_personal_saves(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "seed.b", Path(folder) / "GTA3sf1.b"
            source.write_bytes(checkpoint())
            seed_checkpoint(source, target, sha256(source), iii.valid_save)
            self.assertTrue(iii.valid_save(target))
            target.write_bytes(b"personal save")
            with self.assertRaises(FileExistsError):
                seed_checkpoint(source, target, sha256(source), iii.valid_save)
            self.assertEqual(target.read_bytes(), b"personal save")

    def test_damaged_seed_is_rejected_before_slot_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "seed.b", Path(folder) / "slot.b"
            source.write_bytes(b"damaged")
            with self.assertRaisesRegex(ValueError, "verification"):
                seed_checkpoint(source, target, sha256(source), iii.valid_save)
            self.assertFalse(target.exists())


class CameraBindingTests(unittest.TestCase):
    def test_null_references_bind_once_and_unknown_references_block_all_writes(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            module_path = Path(folder) / "FirstPersonIII.asi"
            module_path.write_bytes(b"pinned test provider")
            mem = MemoryFixture()
            base = 0x5000000
            mem.module = lambda name: {"base": base, "size": 0x356000, "path": module_path}
            with patch.object(iii, "CAMERA_MODULE_SHA256", sha256(module_path)):
                mem.write(base + 0x355944, struct.pack("<I", 123))
                before = dict(mem.data)
                with self.assertRaisesRegex(RuntimeError, "Unexpected"):
                    iii.initialize_camera_provider(mem)
                self.assertEqual(before, mem.data)
                mem.write(base + 0x355944, b"\0" * 4)
                iii.initialize_camera_provider(mem)
                self.assertEqual(mem.u32(base + 0x355940), iii.CAMERA)
                before = dict(mem.data)
                iii.initialize_camera_provider(mem)
                self.assertEqual(before, mem.data)

    def test_unpinned_module_is_rejected_before_memory_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "unknown.asi"
            path.write_bytes(b"unknown")
            mem = MemoryFixture()
            mem.module = lambda name: {"base": 0x5000000, "size": 0x356000, "path": path}
            with self.assertRaisesRegex(RuntimeError, "missing or changed"):
                iii.initialize_camera_provider(mem)
            self.assertEqual(mem.data, {})


class TurningTests(unittest.TestCase):
    def setUp(self):
        with patch("core.controller.Keys"):
            self.walking = Walking(bindings={"forward": "w", "back": "s"},
                                   turn_multiplier=TURN_MULTIPLIER)
        self.keys = self.walking.keys
        self.state = dict(ready=True, foreground=False, background_control=True,
                          paused=False, controls_disabled=False, in_vehicle=False,
                          health=100, player_state=0, ped_state=0, interior=0,
                          position=dict(x=0, y=0, z=0), heading=0, camera_heading=0)
        self.keys.turn.side_effect = self.rotate

    def rotate(self, amount):
        self.state["camera_heading"] -= math.radians(amount * 0.1)
        self.state["heading"] = self.state["camera_heading"]

    def tick(self, now, enabled=False, mode="free"):
        with patch("core.controller.time.monotonic", return_value=now):
            return self.walking.tick(self.state, enabled, mode)

    def test_numbered_turns_finish_smoothly_in_both_directions_while_stopped(self):
        for direction, sign in (("left", 1), ("right", -1)):
            with self.subTest(direction=direction):
                self.walking.stop()
                self.keys.reset_mock()
                self.state["camera_heading"] = 0
                self.assertTrue(self.walking.steer(direction, "free", self.state, 25))
                for step in range(720):
                    self.tick(step / 120)
                self.assertAlmostEqual(self.state["camera_heading"], sign * math.radians(100))
                self.assertEqual(self.walking.manual_turn, 0)
                self.assertLessEqual(max(abs(c.args[0]) for c in self.keys.turn.call_args_list), 8)
                self.keys.set.assert_not_called()

    def test_explore_and_mission_align_with_a_right_angle_within_six_seconds(self):
        for mode in ("explore", "mission"):
            with self.subTest(mode=mode):
                self.walking.stop()
                self.walking.navigator = Mock()
                self.walking.navigator.waypoint.return_value = (-10, 0, 0)
                self.walking.navigator.message = "following pedestrian path"
                self.state["heading"] = self.state["camera_heading"] = 0
                for step in range(720):
                    self.tick(step / 120, True, mode)
                    if abs(angle_delta(math.pi / 2, self.state["camera_heading"])) <= 0.04:
                        break
                else:
                    self.fail("GTA III took more than six seconds to align with the path")
                self.assertEqual(self.walking.recoveries, 0)
                self.keys.set.assert_called_with({"forward"})

    def test_obstacle_recovery_uses_the_same_turn_response(self):
        self.walking.set_mode("free")
        self.walking.recover(self.state["position"], 0)
        for step in range(720):
            self.tick(step / 120, True)
            if self.walking.recovery_turn == 0:
                break
        self.assertEqual(self.walking.recovery_turn, 0)
        self.assertAlmostEqual(self.state["camera_heading"], -math.radians(80))
