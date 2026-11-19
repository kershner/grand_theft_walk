"""Exercise exploration coverage, routing, memory, and observed-ground recovery."""

import json
import math
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.controller import Walking
from core.exploration import ExploreNavigator
from core.navigation import Node, PedestrianGraph, distance, heading_to
from core.runtime import MODES, read_controls


def network():
    return PedestrianGraph([
        Node((0, 0), (0, 0, 0), (((0, 1), False),)),
        Node((0, 1), (0, 10, 0), (((0, 0), False), ((0, 2), False), ((0, 3), True))),
        Node((0, 2), (-10, 10, 0), (((0, 1), False),)),
        Node((0, 3), (10, 10, 0), (((0, 1), True),)),
        Node((1, 0), (1000, 0, 0), (((1, 1), False),)),
        Node((1, 1), (1000, 10, 0), (((1, 0), False),)),
    ])


class ExplorationTests(unittest.TestCase):
    def navigator(self, graph=None, path=None):
        navigator = ExploreNavigator(graph, memory_path=path, rng=random.Random(3))
        navigator.exploring = True
        return navigator

    def test_goal_planning_reuses_visited_connection_to_reach_unvisited_ground(self):
        navigator = self.navigator(network())
        navigator.visits.update({(0, 0): 20, (0, 1): 20, (0, 3): 20})
        self.assertEqual(navigator.waypoint((0, 0, 0), 0, 0, 0), (0, 10, 0))
        self.assertEqual(navigator.goal, (0, 2))
        self.assertIsNone(navigator.destination)

    def test_simulated_walk_covers_branches_and_returns_after_failed_excursion(self):
        navigator = self.navigator(network())
        position, heading = (0, 0, 0), 0
        reached, excursion, returned, failed = set(), False, False, False
        for now in range(350):
            if navigator.excursion_travel > 80 and not failed:
                navigator.avoid_target(now)
                failed = True
            target = navigator.waypoint(position, heading, 0, now)
            self.assertIsNotNone(target)
            reached.add(position)
            excursion |= navigator.excursion_anchor is not None
            returned |= excursion and navigator.excursion_anchor is None
            self.assertNotIn(navigator.goal, {(1, 0), (1, 1)})
            heading = heading_to(position, target)
            position = target
        self.assertTrue({(0, 0, 0), (0, 10, 0), (-10, 10, 0), (10, 10, 0)} <= reached)
        self.assertTrue(excursion)
        self.assertTrue(failed)
        self.assertTrue(returned)

    def test_successful_excursion_continues_past_old_limit_and_can_recover(self):
        navigator = self.navigator(network())
        position, heading = (0, 0, 0), 0
        navigator.travel = 400
        # More than 100 breadcrumbs exercises the bounded recovery window too.
        for now in range(150):
            target = navigator.waypoint(position, heading, 0, now)
            self.assertIsNotNone(target)
            self.assertFalse(navigator.returning)
            heading = heading_to(position, target)
            position = target
        self.assertGreater(navigator.excursion_travel, 1000)
        self.assertGreater(distance(position, (0, 0, 0)), 100)
        self.assertEqual(len(navigator.breadcrumbs), 100)
        self.assertEqual(navigator.excursion_anchor, navigator.breadcrumbs[0])
        # The next obstacle still retraces the retained route and finishes.
        navigator.avoid_target(150)
        for now in range(150, 260):
            target = navigator.waypoint(position, heading, 0, now)
            self.assertIsNotNone(target)
            if not navigator.returning:
                break
            heading = heading_to(position, target)
            position = target
        self.assertFalse(navigator.returning)
        self.assertIsNone(navigator.excursion_anchor)

    def test_open_goal_outweighs_heavily_visited_forward_cells(self):
        navigator = self.navigator()
        position = (6, 6, 0)
        navigator.open_goal = (6, 106, 0)
        navigator.coverage[(0, 1, 0)] = 100
        navigator.coverage[(0, 2, 0)] = 100
        # Face sideways: the established goal should also outweigh current facing.
        target = navigator.waypoint(position, math.pi / 2, 0, 1)
        self.assertAlmostEqual(target[0], 6)
        self.assertAlmostEqual(target[1], 16)

    def test_open_goal_still_avoids_a_known_failed_step(self):
        navigator = self.navigator()
        position = (6, 6, 0)
        navigator.open_goal = (6, 106, 0)
        forward = (6, 16, 0)
        navigator.failures[navigator.connection(position, forward)] = 1
        target = navigator.waypoint(position, 0, 0, 1)
        self.assertGreater(distance(target, forward), 1)

    def test_long_link_midpoint_stays_on_pedestrian_route(self):
        graph = PedestrianGraph([
            Node((0, 0), (0, 0, 0), (((0, 1), False),)),
            Node((0, 1), (0, 70, 0), (((0, 0), False),)),
        ])
        navigator = self.navigator(graph)
        navigator.waypoint((0, 0, 0), 0, 0, 0)
        self.assertEqual(navigator.waypoint((0, 35, 0), 0, 0, 10), (0, 70, 0))
        self.assertIsNone(navigator.local_target)

    def test_open_ground_works_without_a_graph_and_remembers_actual_cells(self):
        navigator = self.navigator()
        position = (1000, 1000, 0)
        for now in range(80):
            target = navigator.waypoint(position, 0, 0, now)
            self.assertLessEqual(distance(position, target), 10.01)
            position = target
        self.assertGreater(len(navigator.coverage), 45)
        count = sum(navigator.coverage.values())
        for now in range(80, 100):
            navigator.waypoint(position, 0, 0, now)
        self.assertLessEqual(sum(navigator.coverage.values()), count + 1)

    def test_failed_off_path_step_retraces_observed_ground(self):
        navigator = self.navigator()
        navigator.waypoint((0, 0, 0), 0, 0, 0)
        navigator.waypoint((0, 4, 0), 0, 0, 1)
        failed = navigator.local_target
        navigator.avoid_target(2)
        self.assertEqual(navigator.waypoint((0, 4, 0), 0, 0, 3), (0, 0, 0))
        self.assertEqual(sum(navigator.failures.values()), 1)
        self.assertNotEqual(navigator.waypoint((0, 0, 0), 0, 0, 4), failed)

    def test_moving_without_approaching_waypoint_replans(self):
        navigator = self.navigator()
        navigator.waypoint((0, 0, 0), 0, 0, 0)
        self.assertIsNone(navigator.waypoint((1, 0, 0), 0, 0, 31))
        self.assertEqual(sum(navigator.failures.values()), 1)

    def test_steep_height_change_abandons_trial_route(self):
        navigator = self.navigator()
        navigator.waypoint((0, 0, 0), 0, 0, 0)
        navigator.waypoint((0, 4, 5), 0, 0, 1)
        self.assertEqual(sum(navigator.failures.values()), 1)
        self.assertIsNone(navigator.local_target)

    def test_coverage_keeps_overlapping_elevations_separate(self):
        navigator = self.navigator()
        navigator.observe((1, 1, 0), 0)
        navigator.observe((1, 1, 8), 1)
        self.assertEqual(len(navigator.coverage), 2)

    def test_memory_survives_restart_including_blocked_connections(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'exploration.json'
            navigator = self.navigator(network(), path)
            navigator.waypoint((0, 0, 0), 0, 0, 0)
            navigator.previous, navigator.target = (0, 0), (0, 1)
            navigator.avoid_target(1)
            navigator.save_memory(2, force=True)
            resumed = self.navigator(network(), path)
            self.assertEqual(resumed.coverage, navigator.coverage)
            self.assertEqual(resumed.visits, navigator.visits)
            self.assertEqual(resumed.edge_visits, navigator.edge_visits)
            self.assertEqual(resumed.edge_failures, navigator.edge_failures)
            resumed.waypoint((0, 0, 0), 0, 0, 100)
            self.assertGreater(resumed.blocked[((0, 0), (0, 1))], 100)

    def test_corrupt_memory_does_not_prevent_navigation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'exploration.json'
            for content in ('{', '[]', '{"version":1,"coverage":[[0,0,0,-1]]}'):
                path.write_text(content)
                navigator = self.navigator(path=path)
                self.assertIsNotNone(navigator.waypoint((0, 0, 0), 0, 0, 0))

    def test_timed_out_goal_is_not_recorded_as_visited(self):
        navigator = self.navigator(network())
        navigator.waypoint((0, 0, 0), 0, 0, 0)
        goal = navigator.goal
        before = navigator.visits[goal]
        navigator.waypoint((0, 0, 0), 0, 0, 181)
        self.assertEqual(navigator.visits[goal], before)

    def test_teleport_discards_local_route_without_painting_intermediate_cells(self):
        navigator = self.navigator()
        navigator.waypoint((0, 0, 0), 0, 0, 0)
        target = navigator.waypoint((1000, 1000, 0), 0, 0, 1)
        self.assertLessEqual(distance((1000, 1000, 0), target), 10.01)
        self.assertEqual(len(navigator.coverage), 2)
        self.assertEqual(navigator.breadcrumbs, [(1000, 1000, 0)])

    def test_teleport_while_backtracking_discards_old_breadcrumbs(self):
        navigator = self.navigator()
        navigator.waypoint((0, 0, 0), 0, 0, 0)
        navigator.waypoint((0, 4, 0), 0, 0, 1)
        navigator.avoid_target(2)
        target = navigator.waypoint((1000, 1000, 0), 0, 0, 3)
        self.assertLessEqual(distance((1000, 1000, 0), target), 10.01)
        self.assertFalse(navigator.returning)
        self.assertEqual(navigator.breadcrumbs, [(1000, 1000, 0)])

    def test_blocked_backtrack_tries_new_ground_instead_of_repeating_forever(self):
        navigator = self.navigator()
        navigator.waypoint((0, 0, 0), 0, 0, 0)
        navigator.waypoint((0, 4, 0), 0, 0, 1)
        navigator.avoid_target(2)
        navigator.waypoint((0, 4, 0), 0, 0, 3)
        navigator.avoid_target(4)
        self.assertFalse(navigator.returning)
        self.assertIsNotNone(navigator.waypoint((0, 4, 0), 0, 0, 5))

    def test_three_modes_and_saved_setting_migration(self):
        self.assertEqual(MODES, ('free', 'explore', 'mission'))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'control.json'
            path.write_text(json.dumps({'mode': 'sidewalk'}))
            settings = {}
            read_controls(path, settings)
            self.assertEqual(settings['mode'], 'explore')

    def test_off_path_obstacle_uses_backtracking_and_water_pauses(self):
        with patch('core.controller.Keys'):
            movement = Walking()
            movement.set_mode('explore')
            movement.navigator.waypoint((0, 0, 0), 0, 0, 0)
            self.assertIn('backtracking', movement.recover({'x': 0, 'y': 0, 'z': 0}, 5))
            self.assertIsNone(movement.recovery_turn)
            movement.navigator.waypoint((0, 0, 0), 0, 0, 6)
            self.assertIn('water', movement.tick({'in_water': True}, True, 'explore'))
            self.assertIsNone(movement.navigator.local_target)
