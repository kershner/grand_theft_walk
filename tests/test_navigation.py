import math
import struct
import unittest
from unittest.mock import patch

from navigation import Node, PedestrianGraph, SidewalkNavigator, SmoothTurn, angle_delta, heading_to, read_area
from walker import Walking


def graph_fixture():
    positions = [(0, 0, 0), (0, -6, 0), (-6, 0, 0), (6, 0, 0), (0, 6, 0)]
    return PedestrianGraph([Node((0, i), position,
                                tuple(((0, j), j == 3) for j in (range(1, 5) if i == 0 else [0])))
                            for i, position in enumerate(positions)])


class GraphTests(unittest.TestCase):
    def test_heading_and_wrap_match_game_convention(self):
        self.assertAlmostEqual(heading_to((0, 0), (1, 0)), -math.pi / 2)
        self.assertAlmostEqual(heading_to((0, 0), (-1, 0)), math.pi / 2)
        self.assertAlmostEqual(angle_delta(-math.pi + .1, math.pi - .1), .2)

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
        self.assertEqual(graph.nearest((.1, .1, 0)).key, (0, 0))


class SmoothTurnTests(unittest.TestCase):
    def test_high_frequency_output_has_no_large_turn_steps(self):
        turn = SmoothTurn()
        turn.last_at = 0.
        amounts = [turn.step(160, i / 120) for i in range(1, 241)]
        self.assertTrue(all(abs(amount) <= 2 for amount in amounts))
        self.assertGreater(sum(amounts), 200)
    def test_acceleration_and_speed_limit_prevent_large_mouse_bursts(self):
        turn = SmoothTurn()
        amounts = [turn.step(10000, i * .05) for i in range(40)]
        self.assertLessEqual(amounts[0], 1)
        self.assertTrue(all(abs(amount) <= 8 for amount in amounts))
        self.assertLessEqual(turn.velocity, 160)

    def test_reversal_brakes_before_turning_other_way(self):
        turn = SmoothTurn()
        for i in range(20):
            turn.step(160, i * .05)
        turn.step(-160, 1.)
        self.assertGreater(turn.velocity, 0)
        for i in range(1, 25):
            turn.step(-160, 1. + i * .05)
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
    def test_camera_feedback_does_not_chase_delayed_character_heading(self):
        self.state["camera_heading"] = 0.
        self.state["heading"] = -math.pi / 2
        self.movement.mode = "sidewalks"
        with patch.object(self.movement.navigator, "waypoint", return_value=(0, 10, 0)):
            self.movement.tick(self.state, True, "sidewalks")
        self.keys.turn.assert_not_called()

    def test_large_turn_does_not_flip_at_angle_wrap(self):
        self.movement.mode = "sidewalks"
        with patch.object(self.movement.navigator, "waypoint", return_value=(0, -10, 0)):
            self.state["camera_heading"] = -.01
            with patch("walker.time.monotonic", return_value=0):
                self.movement.tick(self.state, True, "sidewalks")
            first = self.movement.turn_direction
            self.state["camera_heading"] = .01
            with patch("walker.time.monotonic", return_value=.05):
                self.movement.tick(self.state, True, "sidewalks")
        self.assertEqual(self.movement.turn_direction, first)

    def setUp(self):
        self.keys_patch = patch("walker.Keys")
        self.keys = self.keys_patch.start().return_value
        self.addCleanup(self.keys_patch.stop)
        self.movement = Walking(graph_fixture())
        self.state = dict(ready=True, foreground=True, paused=False, controls_disabled=False,
                          player_state=0, health=100, in_vehicle=False, ped_state=1,
                          position=dict(x=0, y=0, z=0), heading=0, interior=0)

    def test_background_releases_keys_and_never_steers(self):
        self.state["foreground"] = False
        self.movement.tick(self.state, True, "sidewalks")
        self.keys.set.assert_not_called()
        self.keys.turn.assert_not_called()
        self.keys.release.assert_called()
        self.assertFalse(self.movement.steer("left", "free", self.state))

    def test_free_mode_does_not_consult_pedestrian_graph(self):
        with patch.object(self.movement.navigator, "waypoint", side_effect=AssertionError("graph used")):
            self.assertEqual(self.movement.tick(self.state, True, "free"), "walking freely")
        self.keys.set.assert_called_with({"walk", "forward"})

    def test_sidewalk_mode_without_a_path_stops_instead_of_roaming(self):
        self.state["position"] = dict(x=1000, y=1000, z=0)
        self.assertIn("no nearby", self.movement.tick(self.state, True, "sidewalks"))
        self.keys.set.assert_not_called()

    def test_switching_modes_cancels_old_recovery_and_route(self):
        self.movement.tick(self.state, True, "sidewalks")
        self.movement.reverse_until = 99999999
        self.movement.tick(self.state, True, "free")
        self.assertEqual(self.movement.reverse_until, 0)
        self.assertIsNone(self.movement.navigator.target)

    def test_missing_network_only_blocks_sidewalk_mode(self):
        movement = Walking(navigation_error="missing files")
        self.assertIn("unavailable", movement.tick(self.state, True, "sidewalks"))
        self.assertEqual(movement.tick(self.state, True, "free"), "walking freely")

    def test_manual_turn_is_spread_across_ticks_and_cancelled_on_alt_tab(self):
        with patch("walker.time.monotonic", return_value=0):
            self.movement.tick(self.state, True, "free")
        self.assertTrue(self.movement.steer("left", "free", self.state))
        self.keys.turn.assert_not_called()
        for i in range(1, 161):
            self.state["position"] = dict(x=i * .1, y=0, z=0)
            with patch("walker.time.monotonic", return_value=i * .05):
                self.movement.tick(self.state, True, "free")
        amounts = [call.args[0] for call in self.keys.turn.call_args_list]
        self.assertEqual(sum(amounts), -250)
        self.assertTrue(all(abs(amount) <= 8 for amount in amounts))
        self.movement.steer("right", "free", self.state)
        self.state["foreground"] = False
        self.movement.tick(self.state, True, "free")
        self.assertEqual(self.movement.manual_turn, 0)
        self.assertEqual(self.movement.smooth_turn.velocity, 0)


if __name__ == "__main__":
    unittest.main()
