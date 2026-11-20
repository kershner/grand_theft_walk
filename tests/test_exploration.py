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
    def navigator(self, graph=None, path=None, *, open_terrain_chance=0):
        # Graph-routing regressions isolate path following from voluntary departures.
        navigator = ExploreNavigator(graph, memory_path=path, rng=random.Random(3),
                                     open_terrain_chance=open_terrain_chance)
        navigator.exploring = True
        return navigator

    def test_open_terrain_is_often_chosen_with_a_usable_sidewalk_available(self):
        departures = 0
        for seed in range(100):
            navigator = ExploreNavigator(network(), rng=random.Random(seed))
            navigator.exploring = True
            target = navigator.waypoint((0, 0, 0), 0, 0, 0)
            if navigator.local_target is not None:
                departures += 1
                self.assertGreater(abs(target[0]), 1)
                self.assertIsNone(navigator.goal)
                self.assertEqual(navigator.waypoint(target, heading_to((0, 0, 0), target), 0, 10),
                                 navigator.local_target)
                self.assertFalse(navigator.returning)
        self.assertGreaterEqual(departures, 55)
        self.assertLessEqual(departures, 80)

    def test_voluntary_open_departures_wait_for_nodes_and_skip_failed_or_repeated_ground(self):
        graph = network()
        navigator = self.navigator(graph, open_terrain_chance=1)
        navigator.previous, navigator.target, navigator.goal = (0, 0), (0, 1), (0, 1)
        self.assertEqual(navigator.waypoint((0, 5, 0), 0, 0, 1), (0, 10, 0))
        # At the junction a fresh departure is now allowed.
        target = navigator.waypoint((0, 10, 0), 0, 0, 2)
        self.assertIsNotNone(navigator.local_target)
        self.assertEqual(target, navigator.local_target)
        for reason in ('failed', 'repeated', 'viewer_turn'):
            with self.subTest(reason=reason):
                navigator = self.navigator(graph, open_terrain_chance=1)
                if reason == 'viewer_turn':
                    navigator.turn = 'left'
                for step in range(8):
                    bearing = step * math.tau / 8
                    target = (-math.sin(bearing) * 10, math.cos(bearing) * 10, 0)
                    if reason == 'failed':
                        navigator.failures[navigator.connection((0, 0, 0), target)] = 1
                    elif reason == 'repeated':
                        navigator.recent_ground.extend([(0, 0, 0), target, (0, 0, 0)])
                self.assertIsNotNone(navigator.waypoint((0, 0, 0), 0, 0, 0))
                self.assertIsNone(navigator.local_target)

    def test_path_choice_lasts_for_a_long_journey_and_waits_for_the_next_node(self):
        graph = PedestrianGraph([
            Node((0, i), (0, i * 20, 0),
                 tuple(((0, j), False) for j in (i - 1, i + 1) if 0 <= j < 100))
            for i in range(100)
        ])
        navigator = self.navigator(graph)
        navigator.waypoint((0, 0, 0), 0, 0, 0)
        # Every subsequent choice would prefer open ground. The initial path
        # decision must still survive many nodes and a long pause.
        navigator.open_terrain_chance = 1
        for step in range(1, 759):
            target = navigator.waypoint((0, step * 2, 0), 0, 0, 10000 + step)
            self.assertIsNotNone(target)
            self.assertIsNone(navigator.local_target)
            self.assertGreater(target[1], step * 2)
        # 1,500 metres elapses mid-link: wait until reaching the normal
        # two-metre arrival radius around the node at 1,520 metres.
        target = navigator.waypoint((0, 1518, 0), 0, 0, 10759)
        self.assertEqual(target, navigator.local_target)
        self.assertIsNotNone(navigator.local_target)

    def test_obstacle_unlocks_a_new_choice_without_waiting_for_the_distance_limit(self):
        navigator = self.navigator(network())
        navigator.waypoint((0, 0, 0), 0, 0, 0)
        navigator.open_terrain_chance = 1
        self.assertGreater(navigator.next_departure_at, navigator.travel)
        navigator.avoid_target(1)
        self.assertEqual(navigator.next_departure_at, 0)
        self.assertIsNotNone(navigator.waypoint((0, 0, 0), 0, 0, 2))
        self.assertIsNotNone(navigator.local_target)

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
            if not excursion:
                self.assertNotIn(navigator.goal, {(1, 0), (1, 1)})
            if failed and returned:
                break
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
        # The next obstacle makes a short retreat and resumes exploration.
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
        position = (6, 6, 0)
        forward = (6, 16, 0)
        for memory in ('failure', 'old_route'):
            with self.subTest(memory=memory):
                navigator = self.navigator()
                navigator.open_goal = (6, 106, 0)
                if memory == 'failure':
                    navigator.failures[navigator.connection(position, forward)] = 1
                else:
                    # This route predates the recent breadcrumb window.
                    navigator.ground_visits[navigator.connection(position, forward)] = 1
                    navigator.ground_visits[navigator.connection(forward, position)] = 1
                target = navigator.waypoint(position, 0, 0, 1)
                self.assertGreater(distance(target, forward), 1)
                if memory == 'old_route':
                    self.assertNotEqual(navigator.connection(position, target),
                                        navigator.connection(position, forward))

    def test_long_link_midpoint_stays_on_pedestrian_route(self):
        graph = PedestrianGraph([
            Node((0, 0), (0, 0, 0), (((0, 1), False),)),
            Node((0, 1), (0, 70, 0), (((0, 0), False),)),
        ])
        navigator = self.navigator(graph)
        navigator.waypoint((0, 0, 0), 0, 0, 0)
        self.assertEqual(navigator.waypoint((0, 35, 0), 0, 0, 10), (0, 70, 0))
        self.assertIsNone(navigator.local_target)

    def test_usable_paths_continue_beyond_old_distance_and_time_limits(self):
        for spacing, steps, seconds in ((20, 280, 1), (70, 30, 20)):
            with self.subTest(spacing=spacing):
                graph = PedestrianGraph([
                    Node((0, i), (0, i * spacing, 0),
                         tuple(((0, j), False) for j in (i - 1, i + 1) if 0 <= j < 35))
                    for i in range(35)
                ])
                navigator = self.navigator(graph)
                for step in range(steps):
                    position = (0, step * 2, 0)
                    target = navigator.waypoint(position, 0, 0, step * seconds)
                    self.assertIsNotNone(target)
                    self.assertGreater(target[1], position[1],
                                       f'{step=}, {position=}, {target=}, {navigator.open_goal=}')
                    self.assertIsNone(navigator.local_target)
                    self.assertIsNone(navigator.excursion_anchor)

    def test_goal_routing_avoids_immediate_reversal_and_recent_corridors(self):
        navigator = self.navigator(network())
        navigator.previous, navigator.target, navigator.goal = (0, 0), (0, 1), (0, 1)
        navigator.visits.update({(0, 1): 50, (0, 2): 50, (0, 3): 50})
        # The node behind us is less visited, but both branches are still usable.
        self.assertNotEqual(navigator.waypoint((0, 10, 0), 0, 0, 1), (0, 0, 0))

        graph = PedestrianGraph([
            Node((0, 0), (0, 0, 0), (((0, 1), False), ((0, 2), True))),
            Node((0, 1), (0, 10, 0), (((0, 0), False), ((0, 3), False))),
            Node((0, 2), (10, 0, 0), (((0, 0), True), ((0, 3), False))),
            Node((0, 3), (10, 10, 0), (((0, 1), False), ((0, 2), False))),
        ])
        for edge in (((0, 0), (0, 1)), ((0, 1), (0, 0))):
            with self.subTest(recent_edge=edge):
                navigator = self.navigator(graph)
                navigator.visits.update({(0, 0): 10, (0, 1): 1, (0, 2): 1})
                navigator.recent_edges.append(edge)
                self.assertEqual(navigator.waypoint((0, 0, 0), 0, 0, 0), (10, 0, 0))
                self.assertEqual(navigator.goal, (0, 3))
                self.assertTrue(navigator.crossing)
        navigator = self.navigator(graph)
        navigator.visits.update({(0, 0): 10, (0, 2): 100, (0, 3): 100})
        navigator.edge_visits[((0, 0), (0, 1))] = 1
        # Prefer an unused link even when its endpoint has more historic visits.
        self.assertEqual(navigator.waypoint((0, 0, 0), 0, 0, 0), (10, 0, 0))

    def test_open_ground_preserves_progress_and_only_retreats_a_short_distance(self):
        navigator = self.navigator()
        position, heading = (0, 0, 0), 0
        for step in range(50):
            target = navigator.waypoint(position, heading, 0, step * 20)
            self.assertGreater(target[1], position[1])
            heading = heading_to(position, target)
            position = target
        # A single blocked step must not unwind the last 500 metres.
        navigator.waypoint(position, heading, 0, 1000)
        navigator.avoid_target(1000)
        retreat_from = position
        for step in range(10):
            target = navigator.waypoint(position, heading, 0, 1001 + step)
            self.assertIsNotNone(target)
            if not navigator.returning:
                break
            self.assertLessEqual(distance(retreat_from, target), 12)
            heading = heading_to(position, target)
            position = target
        self.assertFalse(navigator.returning)
        # Facing backwards after retreat must not send us down the old trail.
        self.assertGreaterEqual(target[1], position[1])

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

    def test_open_terrain_keeps_going_on_slopes_and_past_nearby_sidewalks(self):
        sidewalk = PedestrianGraph([
            Node((0, 0), (0, 60, 0), (((0, 1), False),)),
            Node((0, 1), (0, 70, 0), (((0, 0), False),)),
        ])
        for graph, slope in ((None, 0), (None, .2), (sidewalk, 0)):
            with self.subTest(sidewalk=graph is not None, slope=slope):
                navigator = self.navigator(graph)
                position = (0, 0, 0)
                previous_step = None
                previous_cell, traversed = None, set()
                for step in range(600):
                    # Facing may briefly differ from actual travel, including
                    # when a new open-ground goal is due.
                    heading = math.pi if step and step % 45 == 0 else math.sin(step) * .2
                    target = navigator.waypoint(position, heading, 0, step * 2)
                    self.assertIsNotNone(target)
                    if previous_step is not None:
                        direction = (target[0] - position[0], target[1] - position[1])
                        self.assertGreaterEqual(sum(a * b for a, b in zip(direction, previous_step)), -1e-6)
                    self.assertFalse(navigator.returning)
                    self.assertIsNone(navigator.goal)
                    self.assertIsNotNone(navigator.local_target)
                    cell = navigator.coverage_cell(position)
                    if previous_cell is not None and cell != previous_cell:
                        edge = frozenset((previous_cell, cell))
                        self.assertFalse(edge in traversed, f'Repeated terrain link at step {step}: {edge}')
                        traversed.add(edge)
                    previous_cell = cell
                    length = distance(position, target)
                    x = position[0] + (target[0] - position[0]) * 2 / length
                    y = position[1] + (target[1] - position[1]) * 2 / length
                    previous_step = (x - position[0], y - position[1])
                    position = (x, y, slope * y)
                self.assertGreater(distance(position, (0, 0, 0)), 800)

    def test_open_terrain_detours_around_a_wall_and_remembers_the_walk_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'exploration.json'
            navigator = self.navigator(path=path)
            position, heading, obstacles = (0, 0, 0), 0, 0
            for now in range(250):
                target = navigator.waypoint(position, heading, 0, now)
                self.assertIsNotNone(target)
                length = distance(position, target)
                following = tuple(a + (b - a) * min(2 / length, 1)
                                  for a, b in zip(position, target))
                # A finite wall across the forward route, not a fake waypoint
                # failure: the walker must find and actually traverse a detour.
                if min(position[1], following[1]) <= 32 and max(position[1], following[1]) >= 30 and abs(following[0]) < 10:
                    navigator.avoid_target(now)
                    obstacles += 1
                    continue
                heading = heading_to(position, target)
                position = following
                if position[1] > 100:
                    break
            self.assertGreater(obstacles, 0)
            self.assertGreater(position[1], 100)
            self.assertTrue(navigator.ground_visits)
            navigator.save_memory(251, force=True)
            resumed = self.navigator(path=path)
            self.assertEqual(resumed.recent_ground, navigator.recent_ground)
            self.assertEqual(resumed.failures, navigator.failures)
            self.assertEqual(resumed.ground_visits, navigator.ground_visits)
            # The restored trail outweighs a backwards-facing spawn camera.
            target = resumed.waypoint(position, math.pi, 0, 252)
            self.assertGreater(target[1], position[1])

    def test_failed_off_path_step_retraces_observed_ground(self):
        navigator = self.navigator()
        navigator.waypoint((0, 0, 0), 0, 0, 0)
        navigator.waypoint((0, 4, 0), 0, 0, 1)
        failed = navigator.local_target
        navigator.avoid_target(2)
        self.assertEqual(navigator.waypoint((0, 4, 0), 0, 0, 3), (0, 0, 0))
        self.assertEqual(sum(navigator.failures.values()), 1)
        self.assertNotEqual(navigator.waypoint((0, 0, 0), 0, 0, 4), failed)

    def test_moving_without_approaching_waypoint_requests_recovery_first(self):
        navigator = self.navigator()
        navigator.waypoint((0, 0, 0), 0, 0, 0)
        self.assertIsNone(navigator.waypoint((1, 0, 0), 0, 0, 31))
        self.assertEqual(sum(navigator.failures.values()), 0)
        self.assertTrue(navigator.recovery_requested)

    def test_steep_height_change_requests_recovery_before_abandoning_route(self):
        navigator = self.navigator()
        navigator.waypoint((0, 0, 0), 0, 0, 0)
        self.assertIsNone(navigator.waypoint((0, 4, 5), 0, 0, 1))
        self.assertEqual(sum(navigator.failures.values()), 0)
        self.assertTrue(navigator.recovery_requested)
        self.assertIsNotNone(navigator.local_target)

    def test_exploration_watchdogs_use_right_turn_recovery_without_replacing_the_goal(self):
        for position, now in (((0, 4, 5), 1), ((1, 0, 0), 31)):
            with self.subTest(position=position), patch('core.controller.Keys'):
                movement = Walking()
                state = dict(ready=True, foreground=True, paused=False,
                             controls_disabled=False, in_vehicle=False, health=100,
                             player_state=0, ped_state=1, interior=0, heading=0,
                             position=dict(x=0, y=0, z=0))
                with patch('core.controller.time.monotonic', return_value=0):
                    movement.tick(state, True, 'explore')
                goal, target = movement.navigator.open_goal, movement.navigator.local_target
                state['position'] = dict(zip(('x', 'y', 'z'), position))
                with patch('core.controller.time.monotonic', return_value=now):
                    self.assertIn('turning right 20', movement.tick(state, True, 'explore'))
                self.assertEqual(movement.navigator.open_goal, goal)
                self.assertEqual(movement.navigator.local_target, target)
                self.assertFalse(movement.navigator.returning)
                self.assertFalse(movement.navigator.recovery_requested)

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
            navigator.waypoint((0, 10, 0), 0, 0, .5)
            self.assertTrue(navigator.recent_edges)
            navigator.previous, navigator.target = (0, 0), (0, 1)
            navigator.avoid_target(1)
            navigator.save_memory(2, force=True)
            resumed = self.navigator(network(), path)
            self.assertEqual(resumed.coverage, navigator.coverage)
            self.assertEqual(resumed.visits, navigator.visits)
            self.assertEqual(resumed.edge_visits, navigator.edge_visits)
            self.assertEqual(resumed.edge_failures, navigator.edge_failures)
            self.assertEqual(resumed.recent_edges, navigator.recent_edges)
            self.assertEqual(resumed.recent_ground, navigator.recent_ground)
            self.assertEqual(resumed.ground_visits, navigator.ground_visits)
            resumed.waypoint((0, 0, 0), 0, 0, 100)
            self.assertGreater(resumed.blocked[((0, 0), (0, 1))], 100)

    def test_corrupt_memory_does_not_prevent_navigation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'exploration.json'
            for content in ('{', '[]', '{"version":1,"coverage":[[0,0,0,-1]]}',
                            '{"version":1,"recent_ground":[[0,0,NaN]]}',
                            '{"version":1,"recent_edges":[[0,1]]}'):
                path.write_text(content)
                navigator = self.navigator(path=path)
                self.assertIsNotNone(navigator.waypoint((0, 0, 0), 0, 0, 0))

    def test_stationary_goal_is_not_recorded_as_visited(self):
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

    def test_off_path_obstacle_turns_right_before_backtracking_and_water_pauses(self):
        with patch('core.controller.Keys'):
            movement = Walking()
            movement.set_mode('explore')
            movement.navigator.waypoint((0, 0, 0), 0, 0, 0)
            goal, target = movement.navigator.open_goal, movement.navigator.local_target
            for attempt, turn in ((1, 20), (2, 20), (3, 50), (4, 50)):
                status = movement.recover({'x': 0, 'y': 0, 'z': 0}, attempt)
                self.assertIn(f'turning right {turn}', status)
                self.assertEqual(movement.navigator.open_goal, goal)
                self.assertEqual(movement.navigator.local_target, target)
                self.assertFalse(movement.navigator.returning)
            self.assertIn('backtracking', movement.recover({'x': 0, 'y': 0, 'z': 0}, 5))
            self.assertIsNone(movement.recovery_turn)
            movement.navigator.waypoint((0, 0, 0), 0, 0, 6)
            self.assertIn('water', movement.tick({'in_water': True}, True, 'explore'))
            self.assertIsNone(movement.navigator.local_target)
