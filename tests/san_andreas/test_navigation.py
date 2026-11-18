"""Tests for pedestrian paths, steering, and walking recovery."""

import json
import math
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.navigation import (
    Node,
    PedestrianGraph,
    SidewalkNavigator,
    SmoothTurn,
    angle_delta,
    heading_to,
    read_area,
)
from core.runtime import DEFAULTS, control_request
from core.controller import Controller, Walking
from games.san_andreas.game import CAPABILITIES


def graph_fixture():
    positions = [(0, 0, 0), (0, -6, 0), (-6, 0, 0), (6, 0, 0), (0, 6, 0)]
    return PedestrianGraph(
        [
            Node(
                (0, i),
                position,
                tuple(((0, j), j == 3) for j in (range(1, 5) if i == 0 else [0])),
            )
            for i, position in enumerate(positions)
        ]
    )


class GraphTests(unittest.TestCase):
    def test_heading_and_wrap_match_game_convention(self):
        self.assertAlmostEqual(heading_to((0, 0), (1, 0)), -math.pi / 2)
        self.assertAlmostEqual(heading_to((0, 0), (-1, 0)), math.pi / 2)
        self.assertAlmostEqual(angle_delta(-math.pi + 0.1, math.pi - 0.1), 0.2)

    def test_parser_excludes_vehicle_nodes_and_retains_crossing_links(self):
        count, links = 3, 2
        allocated = links + 192
        nodes_at = 20
        links_at = nodes_at + count * 28
        crossing_at = links_at + allocated * 4 + links * 2 + allocated
        data = bytearray(crossing_at + allocated)
        struct.pack_into("<5I", data, 0, count, 1, 2, 0, links)
        for i in range(count):
            at = nodes_at + i * 28
            struct.pack_into("<3h", data, at + 8, i * 16, 0, 0)
            struct.pack_into("<3H", data, at + 16, max(0, i - 1), 4, i)
            data[at + 24] = 1
        struct.pack_into("<4H", data, links_at, 4, 2, 4, 1)
        data[crossing_at] = 1
        nodes = read_area(data, 4)
        self.assertEqual([node.key for node in nodes], [(4, 1), (4, 2)])
        self.assertEqual(nodes[0].position, (2, 0, 0))
        self.assertEqual(nodes[0].links, (((4, 2), True),))
        with self.assertRaisesRegex(ValueError, "Truncated"):
            read_area(data[:-1], 4)
        data[nodes_at + 28 + 24] |= 0x80
        self.assertEqual(len(read_area(data, 4)), 1)

    def test_nearest_rejects_wrong_floor_and_remote_nodes(self):
        graph = graph_fixture()
        self.assertIsNone(graph.nearest((0, 0, 10)))
        self.assertIsNone(graph.nearest((1000, 1000, 0)))
        self.assertEqual(graph.nearest((0.1, 0.1, 0)).key, (0, 0))


class SmoothTurnTests(unittest.TestCase):
    def test_high_frequency_output_has_no_large_turn_steps(self):
        turn = SmoothTurn()
        turn.last_at = 0.0
        amounts = [turn.step(160, i / 120) for i in range(1, 241)]
        self.assertTrue(all(abs(amount) <= 2 for amount in amounts))
        self.assertGreater(sum(amounts), 200)

    def test_acceleration_and_speed_limit_prevent_large_mouse_bursts(self):
        turn = SmoothTurn()
        amounts = [turn.step(10000, i * 0.05) for i in range(40)]
        self.assertLessEqual(amounts[0], 1)
        self.assertTrue(all(abs(amount) <= 8 for amount in amounts))
        self.assertLessEqual(turn.velocity, 160)

    def test_reversal_brakes_before_turning_other_way(self):
        turn = SmoothTurn()
        for i in range(20):
            turn.step(160, i * 0.05)
        turn.step(-160, 1.0)
        self.assertGreater(turn.velocity, 0)
        for i in range(1, 25):
            turn.step(-160, 1.0 + i * 0.05)
        self.assertLess(turn.velocity, 0)

    def test_long_frame_does_not_dump_accumulated_movement(self):
        turn = SmoothTurn()
        turn.step(160, 0)
        self.assertLessEqual(abs(turn.step(160, 1000)), 6)

    def test_reset_clears_velocity_and_fractional_movement(self):
        turn = SmoothTurn()
        turn.step(160, 0)
        turn.reset()
        self.assertEqual(turn.step(0, 10), 0)
        self.assertEqual(turn.velocity, 0)


class NavigatorTests(unittest.TestCase):
    def test_valid_slopes_do_not_rejoin_the_previous_waypoint(self):
        for rise in (-6, 6):
            with self.subTest(rise=rise):
                graph = PedestrianGraph(
                    [
                        Node((0, 0), (0, 0, 0), (((0, 1), False),)),
                        Node((0, 1), (20, 0, rise), (((0, 0), False),)),
                    ]
                )
                navigator = SidewalkNavigator(graph)
                navigator.set_destination((0, 1))
                for step, x in enumerate((0, 2, 5, 10, 15, 18, 20)):
                    self.assertEqual(
                        navigator.waypoint(
                            (x, 0, rise * x / 20), -math.pi / 2, 0, step
                        ),
                        (20, 0, rise),
                    )
                    self.assertEqual(navigator.previous, (0, 0))
                    self.assertEqual(navigator.target, (0, 1))

    def test_long_valid_edges_do_not_rejoin_the_previous_waypoint(self):
        graph = PedestrianGraph(
            [
                Node((0, 0), (0, 0, 0), (((0, 1), False),)),
                Node((0, 1), (70, 0, 0), (((0, 0), False),)),
            ]
        )
        navigator = SidewalkNavigator(graph)
        navigator.set_destination((0, 1))
        for step, x in enumerate((0, 2, 5, 10, 20, 40, 60, 70)):
            self.assertEqual(
                navigator.waypoint((x, 0, 0), -math.pi / 2, 0, step), (70, 0, 0)
            )
            self.assertEqual(navigator.previous, (0, 0))

    def test_leaving_the_segment_floor_still_discards_the_route(self):
        self.navigator.waypoint((0, 0, 0), 0, 0, 0)
        self.assertIsNone(self.navigator.waypoint((0, 2, 20), 0, 0, 1))
        self.assertIsNone(self.navigator.previous)
        self.assertIn("no nearby", self.navigator.message)

    def test_restart_near_a_node_joins_forward_link_instead_of_chasing_backwards(self):
        self.navigator.reset()
        self.assertEqual(self.navigator.waypoint((0, 2.5, 0), 0, 0, 0), (0, 6, 0))

    def test_missed_waypoint_is_advanced_after_passing_its_segment(self):
        self.assertEqual(self.navigator.waypoint((1.8, 1.6, 0), 0, 0, 0), (0, 6, 0))
        self.assertEqual(self.navigator.previous, (0, 0))

    def test_far_lateral_position_does_not_skip_a_waypoint(self):
        self.assertEqual(self.navigator.waypoint((5, 1, 0), 0, 0, 0), (0, 0, 0))

    def setUp(self):
        self.navigator = SidewalkNavigator(graph_fixture())
        self.navigator.target, self.navigator.previous = (0, 0), (0, 1)

    def test_continues_forward_without_reversing_at_junction(self):
        self.assertEqual(self.navigator.waypoint((0, 0, 0), 0, 0, 0), (0, 6, 0))
        self.assertEqual(self.navigator.previous, (0, 0))

    def test_left_and_right_are_consumed_at_next_junction(self):
        self.navigator.turn = "left"
        self.assertEqual(self.navigator.waypoint((0, 0, 0), 0, 0, 0), (-6, 0, 0))
        self.assertIsNone(self.navigator.turn)
        self.setUp()
        self.navigator.turn = "right"
        self.assertEqual(self.navigator.waypoint((0, 0, 0), 0, 0, 0), (6, 0, 0))
        self.assertTrue(self.navigator.crossing)

    def test_reverses_on_a_real_dead_end(self):
        self.navigator.target, self.navigator.previous = (0, 4), (0, 0)
        self.assertEqual(self.navigator.waypoint((0, 6, 0), 0, 0, 0), (0, 0, 0))

    def test_stops_indoors_and_when_far_from_network(self):
        self.assertIsNone(self.navigator.waypoint((0, 0, 0), 0, 5, 0))
        self.assertIn("indoors", self.navigator.message)
        self.assertIsNone(self.navigator.waypoint((1000, 1000, 0), 0, 0, 0))

    def test_blocked_edge_is_not_immediately_retried(self):
        self.navigator.target, self.navigator.previous = (0, 4), (0, 0)
        self.navigator.avoid_target(0)
        self.navigator.target = (0, 0)
        self.assertNotEqual(self.navigator.waypoint((0, 0, 0), 0, 0, 1), (0, 6, 0))
        self.assertIn(((0, 0), (0, 4)), self.navigator.blocked)
        self.navigator.waypoint((0, 0, 0), 0, 0, 40)
        self.assertFalse(self.navigator.blocked)


class MovementTests(unittest.TestCase):
    def test_unreachable_mission_does_not_release_forward_in_sidewalk_mode(self):
        graph = PedestrianGraph(list(graph_fixture().nodes.values()) + [
            Node((1, 0), (1000, 0, 0), (((1, 1), False),)),
            Node((1, 1), (1000, 10, 0), (((1, 0), False),)),
        ])
        self.movement.navigator = SidewalkNavigator(graph)
        self.movement.navigator.set_destination((1, 1))
        self.state["heading"] = self.state["camera_heading"] = 0
        status = self.movement.tick(self.state, True, "sidewalk")
        self.assertIn("mission unreachable", status)
        self.keys.set.assert_called_with({"walk", "forward"})

    def test_large_turn_aligns_without_walking_in_a_circle(self):
        self.movement.mode = "sidewalk"
        with patch.object(self.movement.navigator, "waypoint", return_value=(0, -3, 0)):
            self.state["camera_heading"] = 0
            for now in (0, 1, 3, 5):
                with patch("core.controller.time.monotonic", return_value=now):
                    self.assertIn(
                        "aligning", self.movement.tick(self.state, True, "sidewalk")
                    )
                self.keys.set.assert_called_with({"walk"})
            self.assertEqual(self.movement.recoveries, 0)
            self.state["camera_heading"] = math.pi - 0.1
            with patch("core.controller.time.monotonic", return_value=6):
                self.movement.tick(self.state, True, "sidewalk")
            self.keys.set.assert_called_with({"walk", "forward"})

    def test_motion_without_goal_progress_does_not_reselect_route(self):
        self.movement.mode = "sidewalk"
        with patch.object(self.movement.navigator, "waypoint", return_value=(0, 10, 0)):
            for now, x in ((0, 0), (3, 1), (6, -1), (9, 0)):
                self.state["position"] = {"x": x, "y": 0, "z": 0}
                self.state["camera_heading"] = heading_to((x, 0), (0, 10))
                with patch("core.controller.time.monotonic", return_value=now):
                    status = self.movement.tick(self.state, True, "sidewalk")
        self.assertNotIn("recovery", status)
        self.assertEqual(self.movement.recoveries, 0)

    def test_stuck_repeatedly_turns_right_20_then_walks_in_both_modes(self):
        for mode in ("free", "sidewalk"):
            with self.subTest(mode=mode):
                self.movement = Walking(graph_fixture())
                self.keys.reset_mock()
                with patch("core.controller.time.monotonic", return_value=0):
                    self.movement.tick(self.state, True, mode)
                for attempt in range(2):
                    start = 4.1 if attempt == 0 else end + 4.1
                    with patch("core.controller.time.monotonic", return_value=start):
                        self.assertIn("turning right 20", self.movement.tick(self.state, True, mode))
                    self.keys.turn.reset_mock()
                    with patch.object(self.movement.navigator, "waypoint", side_effect=AssertionError("route interrupted recovery")):
                        for step in range(1, 601):
                            end = start + step / 120
                            with patch("core.controller.time.monotonic", return_value=end):
                                self.movement.tick(self.state, True, mode)
                            if self.movement.recovery_turn == 0:
                                break
                        else:
                            self.fail("recovery turn never finished")
                        turns = [call.args[0] for call in self.keys.turn.call_args_list]
                        self.assertEqual(sum(turns), 200)
                        self.assertTrue(all(turn > 0 for turn in turns))
                        with patch("core.controller.time.monotonic", return_value=end + 0.01):
                            self.movement.tick(self.state, True, mode)
                        self.keys.set.assert_called_with({"walk", "forward"})
                    self.assertEqual(self.movement.recoveries, attempt + 1)
                self.assertFalse(self.movement.navigator.blocked)
                self.assertFalse(self.movement.navigator.avoided)
                self.assertNotIn(unittest.mock.call({"walk", "back"}), self.keys.set.call_args_list)
                self.state["position"] = {"x": 1, "y": 0, "z": 0}
                with patch("core.controller.time.monotonic", return_value=end + 0.02):
                    self.movement.tick(self.state, True, mode)
                self.assertIsNone(self.movement.recovery_turn)
                self.state["position"] = {"x": 0, "y": 0, "z": 0}

    def test_stop_pause_and_focus_loss_cancel_recovery(self):
        for cancel in ("stop", "paused", "foreground"):
            with self.subTest(cancel=cancel):
                self.movement = Walking(graph_fixture())
                state = dict(self.state)
                with patch("core.controller.time.monotonic", return_value=0):
                    self.movement.tick(state, True, "free")
                with patch("core.controller.time.monotonic", return_value=5):
                    self.movement.tick(state, True, "free")
                self.assertIsNotNone(self.movement.recovery_turn)
                if cancel != "stop":
                    state[cancel] = cancel == "paused"
                self.keys.reset_mock()
                self.movement.tick(state, cancel != "stop", "free")
                self.assertIsNone(self.movement.recovery_turn)
                self.keys.turn.assert_not_called()
                self.keys.set.assert_not_called()
                self.keys.release.assert_called()

    def test_camera_feedback_does_not_chase_delayed_character_heading(self):
        self.state["camera_heading"] = 0.0
        self.state["heading"] = -math.pi / 2
        self.movement.mode = "sidewalk"
        with patch.object(self.movement.navigator, "waypoint", return_value=(0, 10, 0)):
            self.movement.tick(self.state, True, "sidewalk")
        self.keys.turn.assert_not_called()

    def test_large_turn_does_not_flip_at_angle_wrap(self):
        self.movement.mode = "sidewalk"
        with patch.object(
            self.movement.navigator, "waypoint", return_value=(0, -10, 0)
        ):
            self.state["camera_heading"] = -0.01
            with patch("core.controller.time.monotonic", return_value=0):
                self.movement.tick(self.state, True, "sidewalk")
            first = self.movement.turn_direction
            self.state["camera_heading"] = 0.01
            with patch("core.controller.time.monotonic", return_value=0.05):
                self.movement.tick(self.state, True, "sidewalk")
        self.assertEqual(self.movement.turn_direction, first)

    def setUp(self):
        self.keys_patch = patch("core.controller.Keys")
        self.keys = self.keys_patch.start().return_value
        self.addCleanup(self.keys_patch.stop)
        self.movement = Walking(graph_fixture())
        self.state = {
            "ready": True,
            "foreground": True,
            "paused": False,
            "controls_disabled": False,
            "player_state": 0,
            "health": 100,
            "in_vehicle": False,
            "ped_state": 1,
            "position": {"x": 0, "y": 0, "z": 0},
            "heading": 0,
            "interior": 0,
        }

    def test_background_releases_keys_and_never_steers(self):
        self.state["foreground"] = False
        self.movement.tick(self.state, True, "sidewalk")
        self.keys.set.assert_not_called()
        self.keys.turn.assert_not_called()
        self.keys.release.assert_called()
        self.assertFalse(self.movement.steer("left", "free", self.state))

    def test_free_mode_does_not_consult_pedestrian_graph(self):
        with patch.object(
            self.movement.navigator,
            "waypoint",
            side_effect=AssertionError("graph used"),
        ):
            self.assertEqual(
                self.movement.tick(self.state, True, "free"), "walking freely"
            )
        self.keys.set.assert_called_with({"walk", "forward"})

    def test_sidewalk_mode_without_a_path_stops_instead_of_roaming(self):
        self.state["position"] = {"x": 1000, "y": 1000, "z": 0}
        self.assertIn("no nearby", self.movement.tick(self.state, True, "sidewalk"))
        self.keys.set.assert_not_called()

    def test_switching_modes_cancels_old_recovery_and_route(self):
        self.movement.tick(self.state, True, "sidewalk")
        self.movement.recovery_turn = 200
        self.movement.tick(self.state, True, "free")
        self.assertIsNone(self.movement.recovery_turn)
        self.assertIsNone(self.movement.navigator.target)

    def test_missing_network_only_blocks_sidewalk_mode(self):
        movement = Walking(navigation_error="missing files")
        self.assertIn("unavailable", movement.tick(self.state, True, "sidewalk"))
        self.assertEqual(movement.tick(self.state, True, "free"), "walking freely")

    def test_manual_turn_is_spread_across_ticks_and_cancelled_on_alt_tab(self):
        with patch("core.controller.time.monotonic", return_value=0):
            self.movement.tick(self.state, True, "free")
        self.assertTrue(self.movement.steer("left", "free", self.state, 25))
        self.keys.turn.assert_not_called()
        for i in range(1, 161):
            self.state["position"] = {"x": i * 0.1, "y": 0, "z": 0}
            with patch("core.controller.time.monotonic", return_value=i * 0.05):
                self.movement.tick(self.state, True, "free")
        amounts = [call.args[0] for call in self.keys.turn.call_args_list]
        self.assertEqual(sum(amounts), -250)
        self.assertTrue(all(abs(amount) <= 8 for amount in amounts))
        self.movement.steer("right", "free", self.state)
        self.state["foreground"] = False
        self.movement.tick(self.state, True, "free")
        self.assertEqual(self.movement.manual_turn, 0)
        self.assertEqual(self.movement.smooth_turn.velocity, 0)

    def test_numbered_turns_work_while_stopped_without_pressing_forward(self):
        for direction, sign in (("left", -1), ("right", 1)):
            with self.subTest(direction=direction):
                self.movement.stop()
                self.keys.reset_mock()
                self.assertTrue(self.movement.steer(direction, "free", self.state, 10))
                for i in range(600):
                    with patch("core.controller.time.monotonic", return_value=i / 120):
                        self.movement.tick(self.state, False, "free")
                amounts = [call.args[0] for call in self.keys.turn.call_args_list]
                self.assertEqual(sum(amounts), sign * 100)
                self.keys.set.assert_not_called()
                self.assertEqual(self.movement.manual_turn, 0)

    def test_forward_and_back_are_finite_while_stopped(self):
        for direction in ("forward", "back"):
            with self.subTest(direction=direction):
                self.movement.stop()
                with patch("core.controller.time.monotonic", return_value=10):
                    self.assertTrue(
                        self.movement.steer(direction, "free", self.state, 3)
                    )
                for now in (10, 11, 12.9):
                    with patch("core.controller.time.monotonic", return_value=now):
                        self.assertEqual(
                            self.movement.tick(self.state, False),
                            f"stepping {direction}",
                        )
                    self.keys.set.assert_called_with({"walk", direction})
                self.keys.reset_mock()
                with patch("core.controller.time.monotonic", return_value=13):
                    self.assertEqual(self.movement.tick(self.state, False), "stopped")
                self.keys.set.assert_not_called()
                self.keys.release.assert_called()

    def test_manual_back_resumes_existing_autowalk_when_finished(self):
        with patch("core.controller.time.monotonic", return_value=10):
            self.movement.steer("back", "free", self.state, 2)
            self.movement.tick(self.state, True)
        self.keys.set.assert_called_with({"walk", "back"})
        with patch("core.controller.time.monotonic", return_value=12):
            self.assertEqual(self.movement.tick(self.state, True), "walking freely")
        self.keys.set.assert_called_with({"walk", "forward"})

    def test_stop_and_focus_loss_cancel_finite_steps(self):
        for cancel in ("stop", "background"):
            with self.subTest(cancel=cancel):
                self.state["foreground"] = True
                self.movement.steer("forward", "free", self.state, 100)
                if cancel == "stop":
                    self.movement.stop()
                else:
                    self.state["foreground"] = False
                self.keys.reset_mock()
                self.movement.tick(self.state, False)
                self.assertIsNone(self.movement.manual_direction)
                self.keys.set.assert_not_called()
                self.keys.release.assert_called()

    def test_invalid_amount_never_queues_movement(self):
        for amount in (0, 101, -1, 1.5, "10", True):
            with self.subTest(amount=amount):
                self.assertFalse(
                    self.movement.steer("forward", "free", self.state, amount)
                )
                self.assertIsNone(self.movement.manual_direction)

    def test_backing_up_only_covers_manual_steps(self):
        with patch("core.controller.time.monotonic", return_value=10):
            self.movement.steer("back", "free", self.state, 2)
        self.assertTrue(self.movement.backing_up(10))
        self.assertTrue(self.movement.backing_up(11.9))
        self.assertFalse(self.movement.backing_up(12))
        self.movement.stop()
        self.assertFalse(self.movement.backing_up(10))
        self.movement.recovery_turn = 200
        self.assertFalse(self.movement.backing_up(14))


class ControllerTests(unittest.TestCase):
    def test_initialized_controller_ticks_and_publishes_reports_at_the_expected_interval(self):
        with tempfile.TemporaryDirectory() as folder:
            args = SimpleNamespace(state_dir=Path(folder), walk=True, mode="free")
            memory, backend, autosave = Mock(), Mock(), Mock()
            backend.CAPABILITIES = CAPABILITIES
            backend.WALK_KEYS = None
            memory.profile.id = "san_andreas"
            state = {
                "ready": True, "foreground": True, "paused": False,
                "controls_disabled": False, "in_vehicle": False,
                "health": 100, "player_state": 0, "ped_state": 1,
                "standing": True, "in_air": False, "interior": 0,
                "position": {"x": 0, "y": 0, "z": 0}, "heading": 0,
            }
            backend.Game.return_value.snapshot.return_value = state
            backend.Game.return_value.location.return_value = {}
            backend.FirstPersonCamera.return_value.report.return_value = {}
            backend.FirstPersonCamera.return_value.error = None
            for name in ("apply_peaceful_mode", "unlock_regions", "unlock_interiors"):
                getattr(backend, name).return_value = 0
            autosave.return_value.tick.return_value = False
            autosave.return_value.report.return_value = {}
            with (
                patch("core.controller.Keys") as keys,
                patch("core.controller.HighJump"),
                patch("core.controller.ViewControl"),
                patch("core.controller.Hotkeys") as hotkeys,
            ):
                keys.return_value.held = {"forward", "walk"}
                hotkeys.return_value.pressed.return_value = []
                controller = Controller(memory, args, backend, lambda mem: None, autosave)
                with patch.object(controller, "write_report", wraps=controller.write_report) as report:
                    self.assertTrue(controller.tick(10))
                    report.assert_called_once_with("walking freely")
                    self.assertTrue(controller.tick(10.5))
                    self.assertEqual(report.call_count, 1)
                    self.assertTrue(controller.tick(11))
                    self.assertEqual(report.call_count, 2)
            published = json.loads((args.state_dir / "state.json").read_text())
            self.assertEqual(published["controller"], "running")
            self.assertEqual(published["game"], "san_andreas")

    def test_controller_uses_the_game_adapter_and_explicit_factories(self):
        with tempfile.TemporaryDirectory() as folder:
            args = SimpleNamespace(state_dir=Path(folder), walk=True, mode="free")
            memory, backend, autosave = Mock(), Mock(), Mock()
            backend.CAPABILITIES = CAPABILITIES
            backend.WALK_KEYS = None
            backend.load_destinations.return_value = []
            graph = graph_fixture()
            loader = Mock(return_value=graph)
            with (
                patch("core.controller.Keys"),
                patch("core.controller.HighJump") as jump,
                patch("core.controller.ViewControl"),
                patch("core.controller.Hotkeys"),
            ):
                controller = Controller(memory, args, backend, loader, autosave)
            loader.assert_called_once_with(memory)
            backend.Game.assert_called_once_with(memory)
            autosave.assert_called_once_with(memory, args.state_dir)
            backend.load_destinations.assert_called_once_with(memory.exe.parent, graph, backend.read_game_text)
            jump.assert_called_once_with(controller.game, controller.bindings, binder=backend.bind_jump_key)
            self.assertIs(controller.movement.navigator.graph, graph)
            self.assertEqual(controller.settings["mode"], "free")
            self.assertTrue(controller.settings["walking"])

    def setUp(self):
        self.controller = Controller.__new__(Controller)
        self.controller.settings = dict(DEFAULTS)
        self.controller.movement = Mock()
        self.controller.jump = Mock()
        self.controller.view = Mock()
        self.controller.last_steering = None

    def test_steering_waits_until_accepted_and_runs_only_once(self):
        request = control_request("left", 10)
        self.controller.settings["steering"] = request
        self.controller.movement.steer.side_effect = [False, True]
        self.controller.handle_steering({})
        self.assertIsNone(self.controller.last_steering)
        self.controller.handle_steering({})
        self.controller.handle_steering({})
        self.assertEqual(self.controller.last_steering, request["id"])
        self.assertEqual(self.controller.movement.steer.call_count, 2)

    def test_stop_cancels_movement_and_pending_jump(self):
        jump = control_request()
        self.controller.settings.update(
            walking=True, jump=jump, steering=control_request("stop")
        )
        self.controller.handle_steering({})
        self.assertFalse(self.controller.settings["walking"])
        self.controller.movement.stop.assert_called_once()
        self.controller.jump.cancel.assert_called_once()
        self.controller.view.cancel.assert_called_once()
        self.assertEqual(self.controller.jump.last_request, jump["id"])

    def test_cleanup_restores_game_even_if_releasing_jump_key_fails(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.controller.state_dir = Path(folder.name)
        self.controller.mem = Mock()
        self.controller.autosave = Mock()
        self.controller.autosave.report.return_value = {}
        self.controller.camera = Mock()
        self.controller.pace = Mock()
        self.controller.game = Mock()
        self.controller.report = {}
        for name in ("bindings", "protection", "world", "peaceful"):
            changes = Mock()
            changes.restore.return_value = []
            setattr(self.controller, name, changes)
        self.controller.jump.cancel.side_effect = RuntimeError("key release failed")
        with self.assertRaisesRegex(RuntimeError, "key release failed"):
            self.controller.close()
        self.controller.movement.stop.assert_called_once()
        self.controller.camera.close.assert_called_once()
        self.controller.bindings.restore.assert_called_once()
        report = json.loads((self.controller.state_dir / "state.json").read_text())
        self.assertEqual(report["controller"], "stopped")


if __name__ == "__main__":
    unittest.main()
