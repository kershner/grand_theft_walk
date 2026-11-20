"""Tests for pedestrian paths, steering, and walking recovery."""

import json
import math
import random
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.controller import Controller, Walking
from core.navigation import (
    Node,
    PedestrianGraph,
    PedestrianNavigator,
    SmoothTurn,
    angle_delta,
    heading_to,
    read_area,
)
from core.runtime import DEFAULTS, control_request
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
                navigator = PedestrianNavigator(graph)
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
        navigator = PedestrianNavigator(graph)
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
        self.navigator = PedestrianNavigator(graph_fixture(), rng=random.Random(0))
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

    def test_exploration_prefers_less_visited_paths_even_after_long_walks(self):
        for visits in (0, 1000):
            with self.subTest(visits=visits):
                self.setUp()
                self.navigator.visits.update({(0, 2): visits, (0, 3): visits + 1,
                                              (0, 4): visits + 1})
                self.assertEqual(
                    self.navigator.waypoint((0, 0, 0), 0, 0, 0), (-6, 0, 0)
                )

    def test_equally_explored_junctions_allow_random_turns(self):
        targets = set()
        for seed in range(30):
            navigator = PedestrianNavigator(graph_fixture(), rng=random.Random(seed))
            navigator.target, navigator.previous = (0, 0), (0, 1)
            navigator.waypoint((0, 0, 0), 0, 0, 0)
            targets.add(navigator.target)
        self.assertEqual(targets, {(0, 2), (0, 3), (0, 4)})

    def test_endless_stroll_explores_all_branches_and_keeps_walking(self):
        graph = graph_fixture()
        navigator = PedestrianNavigator(graph, rng=random.Random(9))
        position, heading = (0, 0, 0), 0
        previous = None
        reached = set()
        for now in range(500):
            waypoint = navigator.waypoint(position, heading, 0, now)
            self.assertIsNotNone(waypoint)
            reached.add(navigator.target)
            if navigator.previous == (0, 0):
                self.assertNotEqual(navigator.target, previous)
            previous = navigator.previous
            heading = heading_to(position, waypoint)
            position = waypoint
        self.assertEqual(reached, set(graph.nodes))
        self.assertIsNone(navigator.destination)

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
    def test_airtime_and_landing_do_not_spend_the_route_stall_budget(self):
        movement = Walking()
        movement.set_mode('explore')
        nav = movement.navigator
        nav.waypoint((0, 0, 0), 0, 0, 0)
        nav.journey_goal, nav.journey_remaining = nav.open_goal, 100
        nav.journey_stalled, nav.journey_observed_at = 19, 0
        state = dict(self.state, standing=False, in_air=True, ped_state=41)
        for now in range(1, 16):
            with patch('core.controller.time.monotonic', return_value=now):
                self.assertIn('jumping', movement.tick(state, True, 'explore'))
        state.update(in_air=False, standing=True)
        with patch('core.controller.time.monotonic', return_value=16):
            self.assertIn('recovering from landing', movement.tick(state, True, 'explore'))
        state['ped_state'] = 1
        for now in (17, 18.4, 19):
            with patch('core.controller.time.monotonic', return_value=now):
                movement.tick(state, True, 'explore')
        self.assertEqual(movement.recoveries, 0)
        self.assertEqual(nav.journey_stalled, 19)
        self.assertFalse(nav.failures)
        self.keys.set.assert_called_with({'walk', 'forward'})

    def test_manual_jump_gets_takeoff_and_landing_time_then_stuck_checks_resume(self):
        movement = Walking()
        movement.set_mode('explore')
        with patch('core.controller.time.monotonic', return_value=0):
            movement.tick(self.state, True, 'explore')
        movement.jump_feedback('viewer-jump', 'accepted')
        with patch('core.controller.time.monotonic', return_value=5):
            self.assertIn('jumping', movement.tick(self.state, True, 'explore'))
        airborne = dict(self.state, in_air=True, standing=False, ped_state=41)
        with patch('core.controller.time.monotonic', return_value=6):
            movement.tick(airborne, True, 'explore')
        for now in (7, 9):
            with patch('core.controller.time.monotonic', return_value=now):
                movement.tick(self.state, True, 'explore')
            self.assertEqual(movement.recoveries, 0)
        with patch('core.controller.time.monotonic', return_value=11):
            movement.tick(self.state, True, 'explore')
        self.assertEqual(movement.recoveries, 0)
        with patch('core.controller.time.monotonic', return_value=14.1):
            movement.tick(self.state, True, 'explore')
        self.assertEqual(movement.recoveries, 1)

    def test_jump_protection_does_not_reset_the_obstacle_retry_budget(self):
        movement = Walking()
        movement.set_mode('free')
        movement.recover(self.state['position'], 0, self.state)
        attempts, target = movement.recovery_attempts, movement.recovery_target
        for now in (9, 10, 11):
            state = dict(self.state, in_air=True, standing=False, ped_state=41)
            with patch('core.controller.time.monotonic', return_value=now):
                movement.tick(state, True)
        with patch('core.controller.time.monotonic', return_value=12):
            movement.tick(self.state, True)
        self.assertEqual(movement.recovery_attempts, attempts)
        self.assertEqual(movement.recovery_target, target)
        self.assertEqual(movement.recovery_progress_at, 12)
        with patch('core.controller.time.monotonic', return_value=12.7):
            movement.tick(self.state, True)
        self.assertEqual(movement.recovery_progress_at, 15.7)

    def test_all_local_directions_blocked_still_produces_a_walking_route(self):
        movement = Walking()
        movement.set_mode('explore')
        nav = movement.navigator
        nav.recent_ground.extend(((0, -6, 0), (0, 0, 0)))
        for bearing in (0, math.pi / 2, math.pi, -math.pi / 2):
            nav.failed_approaches.append(((0, 0, 0), bearing, 120))
        target = nav.waypoint((0, 0, 0), 0, 0, 0)
        self.assertEqual(target, (0, -6, 0))
        self.assertIn('walking to find a route', nav.message)
        state = dict(self.state, camera_heading=math.pi)
        with patch('core.controller.time.monotonic', return_value=.1):
            movement.tick(state, True, 'explore')
        self.keys.set.assert_called_with({'walk', 'forward'})
        next_target = nav.waypoint(target, math.pi, 0, 1)
        self.assertLess(next_target[1], target[1])

    def test_missing_mission_path_keeps_walking_and_retries_the_planner(self):
        movement = Walking()
        movement.set_mode('mission')
        with patch.object(movement.navigator, 'waypoint', return_value=None) as route:
            with patch('core.controller.time.monotonic', return_value=0):
                movement.tick(self.state, True, 'mission')
            target = movement.recovery_target
            state = dict(self.state, camera_heading=heading_to((0, 0), target))
            with patch('core.controller.time.monotonic', return_value=.1):
                movement.tick(state, True, 'mission')
            self.keys.set.assert_called_with({'walk', 'forward'})
            state['position'] = dict(zip(('x', 'y', 'z'), target))
            with patch('core.controller.time.monotonic', return_value=1):
                movement.tick(state, True, 'mission')
            self.assertEqual(route.call_count, 2)

    def test_detour_does_not_wait_when_all_directions_are_rejected(self):
        movement = Walking()
        movement.set_mode('free')
        with patch.object(movement.navigator, 'approach_blocked', return_value=True):
            movement.recover(self.state['position'], 0, self.state)
        self.assertIsNotNone(movement.recovery_target)
        state = dict(self.state, camera_heading=heading_to((0, 0), movement.recovery_target))
        with patch('core.controller.time.monotonic', return_value=.1):
            movement.tick(state, True)
        self.keys.set.assert_called_with({'walk', 'forward'})

    def test_unreachable_mission_does_not_release_forward_in_mission_mode(self):
        graph = PedestrianGraph(list(graph_fixture().nodes.values()) + [
            Node((1, 0), (1000, 0, 0), (((1, 1), False),)),
            Node((1, 1), (1000, 10, 0), (((1, 0), False),)),
        ])
        self.movement.navigator = PedestrianNavigator(graph)
        self.movement.navigator.set_destination((1, 1))
        self.state["heading"] = self.state["camera_heading"] = 0
        status = self.movement.tick(self.state, True, "mission")
        self.assertIn("mission unreachable", status)
        self.keys.set.assert_called_with({"walk", "forward"})

    def test_large_turn_aligns_without_walking_in_a_circle(self):
        self.movement.mode = "explore"
        with patch.object(self.movement.navigator, "waypoint", return_value=(0, -3, 0)):
            self.state["camera_heading"] = 0
            for now in (0, 1, 3, 5):
                with patch("core.controller.time.monotonic", return_value=now):
                    self.assertIn(
                        "aligning", self.movement.tick(self.state, True, "explore")
                    )
                self.keys.set.assert_called_with({"walk"})
            self.assertEqual(self.movement.recoveries, 0)
            self.state["camera_heading"] = math.pi - 0.1
            with patch("core.controller.time.monotonic", return_value=6):
                self.movement.tick(self.state, True, "explore")
            self.keys.set.assert_called_with({"walk", "forward"})

    def test_motion_without_goal_progress_does_not_reselect_route(self):
        self.movement.mode = "explore"
        with patch.object(self.movement.navigator, "waypoint", return_value=(0, 10, 0)):
            for now, x in ((0, 0), (3, 1), (6, -1), (9, 0)):
                self.state["position"] = {"x": x, "y": 0, "z": 0}
                self.state["camera_heading"] = heading_to((x, 0), (0, 10))
                with patch("core.controller.time.monotonic", return_value=now):
                    status = self.movement.tick(self.state, True, "explore")
        self.assertNotIn("recovery", status)
        self.assertEqual(self.movement.recoveries, 0)

    def test_stuck_walker_steers_toward_a_detour_instead_of_a_fixed_turn(self):
        for mode in ("free", "mission"):
            with self.subTest(mode=mode):
                movement = Walking(graph_fixture())
                movement.navigator.waypoint = Mock(return_value=(0, 10, 0))
                with patch("core.controller.time.monotonic", return_value=0):
                    movement.tick(self.state, True, mode)
                with patch("core.controller.time.monotonic", return_value=4.1):
                    self.assertIn("steering around obstacle", movement.tick(self.state, True, mode))
                target = movement.recovery_target
                self.assertIsNotNone(target)
                self.assertGreater(abs(target[0]), 2)
                facing = heading_to((0, 0), target)
                state = dict(self.state, camera_heading=facing)
                with patch("core.controller.time.monotonic", return_value=4.2):
                    self.assertIn("walking around obstacle", movement.tick(state, True, mode))
                self.keys.set.assert_called_with({"walk", "forward"})
                self.assertEqual(movement.recovery_target, target)


    def test_stop_pause_and_focus_loss_cancel_recovery(self):
        for cancel in ("stop", "paused", "foreground"):
            with self.subTest(cancel=cancel):
                self.movement = Walking(graph_fixture())
                state = dict(self.state)
                with patch("core.controller.time.monotonic", return_value=0):
                    self.movement.tick(state, True, "free")
                with patch("core.controller.time.monotonic", return_value=5):
                    self.movement.tick(state, True, "free")
                self.assertIsNotNone(self.movement.recovery_target)
                if cancel != "stop":
                    state[cancel] = cancel == "paused"
                self.keys.reset_mock()
                self.movement.tick(state, cancel != "stop", "free")
                self.assertIsNone(self.movement.recovery_target)
                self.keys.turn.assert_not_called()
                self.keys.set.assert_not_called()
                self.keys.release.assert_called()
                self.assertIsNone(self.movement.recovery_anchor)
                self.assertEqual(self.movement.recovery_attempts, 0)

    def test_recovery_jumps_first_then_returns_to_feedback_steering(self):
        for mode in ('free', 'mission'):
            with self.subTest(mode=mode):
                movement = Walking(graph_fixture())
                movement.set_mode(mode)
                movement.jump_supported = True
                self.assertIn('backing up', movement.recover(self.state['position'], 0))
                with patch('core.controller.time.monotonic', return_value=1.5):
                    self.assertIn('approaching jump', movement.tick(self.state, True, mode))
                with patch('core.controller.time.monotonic', return_value=1.75):
                    self.assertIn('jumping', movement.tick(self.state, True, mode))
                airborne = dict(self.state, in_air=True, standing=False, ped_state=41)
                with patch('core.controller.time.monotonic', return_value=1.85):
                    self.assertIn('jumping', movement.tick(airborne, True, mode))
                self.assertEqual(movement.recovery_attempts, 1)
                with patch('core.controller.time.monotonic', return_value=2):
                    movement.tick(self.state, True, mode)
                self.assertIsNotNone(movement.recovery_jump_started)
                for now in (2.7, 5.8):
                    with patch('core.controller.time.monotonic', return_value=now):
                        movement.tick(self.state, True, mode)
                self.assertIsNone(movement.recovery_jump_started)
                self.assertIn('steering around obstacle', movement.recover(self.state['position'], 6, self.state))
                self.assertIsNotNone(movement.recovery_target)
                self.assertIsNone(movement.recovery_jump_request)


    def test_jump_retreat_distance_starts_short_forward_approach(self):
        movement = self.movement
        movement.set_mode('free')
        movement.jump_supported = True
        movement.recover(self.state['position'], 1)
        self.assertTrue(movement.backing_up(1))
        with patch('core.controller.time.monotonic', return_value=1.5):
            self.assertIn('backing up', movement.tick(self.state, True))
        state = dict(self.state, position={'x': -1.5, 'y': 0, 'z': 0})
        with patch('core.controller.time.monotonic', return_value=1.6):
            self.assertIn('approaching jump', movement.tick(state, True))
        self.assertFalse(movement.backing_up(1.6))
        self.assertIsNone(movement.recovery_jump_request)
        state['position'] = {'x': -1, 'y': 0, 'z': 0}
        with patch('core.controller.time.monotonic', return_value=1.8):
            self.assertIn('jumping', movement.tick(state, True))
        self.assertIsNotNone(movement.recovery_jump_request)

    def test_jump_preparation_cancels_on_stop_focus_loss_pause_and_mode_change(self):
        for phase in ('back', 'forward'):
            for cancel in ('stop', 'foreground', 'paused', 'mode'):
                with self.subTest(phase=phase, cancel=cancel):
                    movement = Walking(graph_fixture())
                    movement.set_mode('free')
                    movement.jump_supported = True
                    movement.recover(self.state['position'], 1)
                    if phase == 'forward':
                        with patch('core.controller.time.monotonic', return_value=2.5):
                            movement.tick(self.state, True)
                    state = dict(self.state)
                    if cancel in ('foreground', 'paused'):
                        state[cancel] = cancel == 'paused'
                    with patch('core.controller.time.monotonic', return_value=2.6):
                        movement.tick(state, cancel != 'stop', 'explore' if cancel == 'mode' else 'free')
                    self.assertIsNone(movement.recovery_jump_setup)
                    self.assertIsNone(movement.recovery_jump_request)
                    self.assertIsNone(movement.recovery_jump_started)

    def test_detour_rejoins_the_route_without_dropping_the_mission(self):
        movement = self.movement
        movement.set_mode('mission')
        movement.navigator.set_destination((0, 3))
        movement.last_waypoint = (0, 10, 0)
        movement.recover(self.state['position'], 0, self.state)
        target = movement.recovery_target
        state = dict(self.state, position=dict(zip(('x', 'y', 'z'), target)))
        with patch.object(movement.navigator, 'waypoint', return_value=(0, 10, 0)) as route:
            with patch('core.controller.time.monotonic', return_value=2):
                movement.tick(state, True, 'mission')
        route.assert_called_once()
        self.assertIsNone(movement.recovery_target)
        self.assertEqual(movement.navigator.destination, (0, 3))

    def test_detour_can_choose_either_side_and_avoids_the_failed_bearing(self):
        targets = []
        for side in (-1, 1):
            movement = Walking()
            movement.set_mode('free')
            movement.navigator.failed_approaches.append(((0, 0, 0), side * math.pi / 2, 120))
            movement.recover(self.state['position'], 0, self.state)
            target = movement.recovery_target
            self.assertIsNotNone(target)
            self.assertFalse(movement.navigator.approach_blocked((0, 0, 0), target, 0))
            targets.append(target[0])
        self.assertLess(targets[0] * targets[1], 0)

    def test_repeated_detour_failure_does_not_retry_the_same_corridor(self):
        movement = Walking()
        movement.set_mode('free')
        movement.recover(self.state['position'], 0, self.state)
        first = movement.recovery_target
        movement.recover(self.state['position'], 4, self.state)
        self.assertNotEqual(movement.recovery_target, first)
        self.assertTrue(movement.navigator.approach_blocked((0, 0, 0), first, 5))

    def test_detour_watchdog_detects_motion_without_getting_closer(self):
        movement = Walking()
        movement.set_mode('free')
        movement.recover(self.state['position'], 0, self.state)
        first = movement.recovery_target
        for now in range(1, 10):
            state = dict(self.state, position=dict(x=0, y=(-1) ** now, z=0),
                         camera_heading=heading_to((0, (-1) ** now), first))
            with patch('core.controller.time.monotonic', return_value=now):
                movement.tick(state, True, 'free')
        self.assertGreater(movement.recoveries, 1)
        self.assertNotEqual(movement.recovery_target, first)

    def test_camera_feedback_does_not_chase_delayed_character_heading(self):
        self.state["camera_heading"] = 0.0
        self.state["heading"] = -math.pi / 2
        self.movement.mode = "explore"
        with patch.object(self.movement.navigator, "waypoint", return_value=(0, 10, 0)):
            self.movement.tick(self.state, True, "explore")
        self.keys.turn.assert_not_called()

    def test_large_turn_does_not_flip_at_angle_wrap(self):
        self.movement.mode = "explore"
        with patch.object(
            self.movement.navigator, "waypoint", return_value=(0, -10, 0)
        ):
            self.state["camera_heading"] = -0.01
            with patch("core.controller.time.monotonic", return_value=0):
                self.movement.tick(self.state, True, "explore")
            first = self.movement.turn_direction
            self.state["camera_heading"] = 0.01
            with patch("core.controller.time.monotonic", return_value=0.05):
                self.movement.tick(self.state, True, "explore")
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
        self.movement.tick(self.state, True, "explore")
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

    def test_explore_mode_without_a_path_explores_open_ground(self):
        self.state["position"] = {"x": 1000, "y": 1000, "z": 0}
        self.assertIn("exploring open ground", self.movement.tick(self.state, True, "explore"))
        self.keys.set.assert_called_with({"walk", "forward"})

    def test_switching_modes_cancels_old_recovery_and_route(self):
        self.movement.tick(self.state, True, "explore")
        self.movement.recovery_target = (6, 0, 0)
        self.movement.tick(self.state, True, "free")
        self.assertIsNone(self.movement.recovery_target)
        self.assertIsNone(self.movement.navigator.target)

    def test_switching_from_mission_to_explore_discards_goal_and_keeps_exploring(self):
        self.movement.set_mode("mission")
        navigator = self.movement.navigator
        navigator.open_terrain_chance = 1
        navigator.set_destination((0, 4))
        navigator.visits[(0, 3)] = 20
        self.movement.tick(self.state, True, "mission")
        self.state["position"] = {"x": 0, "y": 6, "z": 0}
        self.movement.tick(self.state, True, "explore")
        self.assertIsNone(navigator.destination)
        self.assertIsNotNone(navigator.local_target)
        self.assertNotIn("mission", navigator.message)
        self.assertEqual(navigator.visits[(0, 3)], 20)

    def test_mission_steering_queues_junction_preferences(self):
        for direction in ("left", "right", "forward"):
            self.assertTrue(self.movement.steer(direction, "mission", self.state))
            self.assertEqual(self.movement.navigator.turn, direction)
        self.assertFalse(self.movement.steer("back", "mission", self.state))
        self.assertIsNone(self.movement.manual_direction)

    def test_missing_network_keeps_every_mode_moving(self):
        movement = Walking(navigation_error="missing files")
        self.assertIn("exploring open ground", movement.tick(self.state, True, "explore"))
        self.assertIn("obstacle recovery", movement.tick(self.state, True, "mission"))
        self.assertEqual(movement.tick(self.state, True, "free"), "walking freely")

    def test_controllable_water_does_not_pause_exploration(self):
        movement = Walking()
        state = dict(self.state, in_water=True)
        with patch('core.controller.time.monotonic', return_value=0):
            status = movement.tick(state, True, 'explore')
        self.assertNotIn('paused', status)
        self.keys.set.assert_called_with({'walk', 'forward'})

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
        self.movement.recovery_target = (6, 0, 0)
        self.assertFalse(self.movement.backing_up(14))


class ControllerTests(unittest.TestCase):
    def test_only_mission_mode_targets_and_holds_at_the_active_mission(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = self.controller
            controller.control_path = Path(folder) / "control.json"
            controller.settings["walking"] = True
            controller.game = Mock()
            controller.game.snapshot.return_value = {
                "ready": True, "foreground": True, "paused": False,
                "controls_disabled": False, "in_vehicle": False,
                "health": 100, "player_state": 0,
            }
            controller.startup_ready = False
            controller.respawn = controller.autosave = controller.pace = None
            controller.jump = controller.view = None
            controller.missions = Mock()
            controller.missions.data = {"active": {"node": [0, 4]}}
            controller.missions.tick.return_value = True
            controller.movement.tick.return_value = "following pedestrian path"
            controller.limit_settings = Mock()
            controller.handle_steering = controller.handle_hotkeys = Mock()
            controller.update_game = Mock()
            controller.write_report = Mock()
            controller.last_message = None
            controller.saved_at = 0
            for mode in ("mission", "explore", "free", "mission"):
                with self.subTest(mode=mode):
                    controller.settings["mode"] = mode
                    controller.movement.reset_mock()
                    self.assertTrue(controller.tick(10))
                    controller.movement.navigator.set_destination.assert_called_once_with(
                        (0, 4) if mode == "mission" else None
                    )
                    if mode == "mission":
                        controller.movement.stop.assert_called_once()
                        controller.movement.tick.assert_not_called()
                    else:
                        controller.movement.tick.assert_called_once_with(
                            controller.game.snapshot.return_value, True, mode
                        )

    def test_f5_cycles_all_three_modes(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = self.controller
            controller.control_path = Path(folder) / "control.json"
            controller.hotkeys = Mock()
            controller.hotkeys.pressed.return_value = ["f5"]
            controller.settings["mode"] = "free"
            for expected in ("explore", "mission", "free"):
                controller.handle_hotkeys({"ready": True, "foreground": True, "paused": False})
                self.assertEqual(controller.settings["mode"], expected)
                saved = json.loads(controller.control_path.read_text())
                self.assertEqual(saved["mode"], expected)

    def test_initialized_controller_ticks_and_publishes_reports_at_the_expected_interval(self):
        with tempfile.TemporaryDirectory() as folder:
            args = SimpleNamespace(state_dir=Path(folder), walk=True, mode="free")
            memory, backend, autosave = Mock(), Mock(), Mock()
            backend.CAPABILITIES = CAPABILITIES
            backend.WALK_KEYS = None
            backend.MISSION_DEFAULTS = None
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
                before = (args.state_dir / "state.json").read_bytes()
                with (
                    patch("core.controller.atomic_json", side_effect=PermissionError("reader holds state.json")),
                    self.assertLogs("core.controller", level="WARNING") as warnings,
                ):
                    self.assertTrue(controller.tick(12))
                    self.assertTrue(controller.tick(13))
                self.assertEqual(len(warnings.output), 1)
                self.assertTrue(controller.report_write_blocked)
                self.assertEqual((args.state_dir / "state.json").read_bytes(), before)
                with self.assertLogs("core.controller", level="INFO") as recovery:
                    self.assertTrue(controller.tick(14))
                self.assertFalse(controller.report_write_blocked)
                self.assertTrue(any("publication resumed" in message for message in recovery.output))
                request = control_request()
                controller.movement.recovery_jump_request = request
                controller.jump.last_request = request['id']
                self.assertTrue(controller.tick(15))
                self.assertEqual(controller.jump.tick.call_args.args[0], request)
                self.assertIsNone(controller.movement.recovery_jump_request)
            published = json.loads((args.state_dir / "state.json").read_text())
            self.assertEqual(published["controller"], "running")
            self.assertEqual(published["game"], "san_andreas")

    def test_controller_uses_the_game_adapter_and_explicit_factories(self):
        with tempfile.TemporaryDirectory() as folder:
            args = SimpleNamespace(state_dir=Path(folder), walk=True, mode="free")
            memory, backend, autosave = Mock(), Mock(), Mock()
            backend.CAPABILITIES = CAPABILITIES
            backend.WALK_KEYS = None
            backend.MISSION_DEFAULTS = None
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
        self.controller.input = None
        self.controller.display = None
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
