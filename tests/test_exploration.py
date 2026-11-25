"""Exercise exploration coverage, routing, memory, and observed-ground recovery."""

import json
import math
import random
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from core.controller import Walking
from core.controls import HighJump
from core.exploration import JOURNEY_STALL_SECONDS, ExploreNavigator
from core.navigation import Node, PedestrianGraph, angle_delta, distance, heading_to
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

    def test_unused_distant_connections_beat_familiar_nearby_loops(self):
        nodes = [Node((0, i), (0, i * 20, 0),
                      tuple(((0, j), False) for j in (i - 1, i + 1) if 0 <= j < 81))
                 for i in range(81)]
        nodes[0] = Node((0, 0), (0, 0, 0), nodes[0].links + (((1, 0), False),))
        nodes.extend([Node((1, 0), (-10, 0, 0), (((0, 0), False), ((1, 1), False))),
                      Node((1, 1), (-10, 10, 0), (((1, 0), False),))])
        graph = PedestrianGraph(nodes)
        for reverse_history in (False, True):
            with self.subTest(reverse_history=reverse_history):
                navigator = self.navigator(graph)
                for node in graph.nodes.values():
                    for target, _ in node.links:
                        if frozenset((node.key, target)) != frozenset(((0, 79), (0, 80))):
                            edge = (target, node.key) if reverse_history else (node.key, target)
                            navigator.edge_visits[edge] = 1
                # High historical popularity cannot outweigh an unused link.
                navigator.visits[(0, 80)] = 1000000
                self.assertEqual(navigator.waypoint((0, 0, 0), 0, 0, 0), (0, 20, 0))
                self.assertEqual(navigator.goal_edge, ((0, 79), (0, 80)))

    def test_graph_covers_unused_physical_connections_before_repeating_and_closes_cycles(self):
        positions = [(0, 0, 0), (-10, 0, 0), (-10, 10, 0), (10, 0, 0), (10, 10, 0)]
        edges = ((0, 1), (1, 2), (2, 0), (0, 3), (3, 4), (4, 0))
        graph = PedestrianGraph([
            Node((0, i), p, tuple(((0, b if a == i else a), False)
                                 for a, b in edges if i in (a, b)))
            for i, p in enumerate(positions)
        ])
        navigator = self.navigator(graph)
        position, heading, seen = positions[0], 0, set()
        for now in range(len(edges)):
            target = navigator.waypoint(position, heading, 0, now)
            self.assertIsNone(navigator.local_target)
            edge = frozenset((position, target))
            self.assertNotIn(edge, seen)
            seen.add(edge)
            heading, position = heading_to(position, target), target
        self.assertEqual(len(seen), len(edges))

        # The only fresh connector closes a directed cycle back to our start.
        graph = PedestrianGraph([
            Node((0, 0), positions[0], (((0, 1), False),)),
            Node((0, 1), positions[1], (((0, 2), False),)),
            Node((0, 2), positions[2], (((0, 0), False),)),
        ])
        navigator = self.navigator(graph)
        navigator.edge_visits.update({((0, 0), (0, 1)): 1, ((0, 1), (0, 2)): 1})
        position = positions[0]
        for now, expected in enumerate((positions[1], positions[2], positions[0])):
            self.assertEqual(navigator.waypoint(position, 0, 0, now), expected)
            self.assertEqual(navigator.goal_edge, ((0, 2), (0, 0)))
            position = expected

    def test_sqlite_migrates_history_once_and_saves_only_modified_counters(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'exploration.json'
            original = json.dumps({'version': 1, 'coverage': [[i, 0, 0, 1] for i in range(1000)],
                                   'edge_visits': [[0, 0, 0, 1, 7]]})
            path.write_text(original)
            navigator = self.navigator(network(), path)
            self.assertEqual(path.read_text(), original)
            self.assertEqual(navigator.edge_visits[((0, 0), (0, 1))], 7)
            statements = []
            connect = navigator.store.connect

            def traced_connect():
                db = connect()
                db.set_trace_callback(statements.append)
                return db

            navigator.coverage[(42, 0, 0)] += 1
            with patch.object(navigator.store, 'connect', side_effect=traced_connect):
                navigator.save_memory(30, force=True)
            inserts = [s for s in statements if s.startswith('INSERT INTO counters')]
            self.assertEqual(len(inserts), 1)
            self.assertFalse(navigator.coverage.pending)
            # A stale legacy writer must never replace the migrated database.
            path.write_text('{"version":1,"coverage":[]}')
            resumed = self.navigator(network(), path)
            self.assertEqual(len(resumed.coverage), 1000)
            self.assertEqual(resumed.coverage[(42, 0, 0)], 2)
            self.assertEqual(resumed.edge_visits[((0, 0), (0, 1))], 7)
            with closing(sqlite3.connect(path.with_suffix('.sqlite3'))) as db, db:
                self.assertEqual(db.execute('SELECT count(*) FROM counters').fetchone()[0], 1001)
                db.execute('PRAGMA user_version=2')
            with self.assertLogs('core.exploration', level='WARNING'):
                future = self.navigator(network(), path)
                future.observe((0, 0, 0), 0)
            with closing(sqlite3.connect(path.with_suffix('.sqlite3'))) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)
                self.assertEqual(db.execute('SELECT count(*) FROM counters').fetchone()[0], 1001)

    def test_sqlite_locked_and_failed_checkpoints_preserve_history_and_retry(self):
        for failure in ('locked', 'rollback'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'exploration.json'
                navigator = self.navigator(path=path)
                navigator.observe((0, 0, 0), 0)
                navigator.coverage[(10, 0, 0)] = 2
                navigator.failures[(0, 0, 0, 1, 0, 0)] = 1
                with closing(sqlite3.connect(path.with_suffix('.sqlite3'))) as db, db:
                    if failure == 'locked':
                        db.execute('BEGIN EXCLUSIVE')
                    else:
                        db.execute("CREATE TRIGGER reject_checkpoint BEFORE INSERT ON counters "
                                   "WHEN new.name='failures' BEGIN SELECT RAISE(ABORT, 'interrupted batch'); END")
                    with self.assertLogs('core.exploration', level='WARNING'):
                        navigator.save_memory(30, force=True)
                    self.assertTrue(navigator.coverage.pending)
                    self.assertEqual(db.execute('SELECT count(*) FROM counters').fetchone()[0], 1)
                    if failure == 'locked':
                        db.rollback()
                    else:
                        db.execute('DROP TRIGGER reject_checkpoint')
                navigator.save_memory(31, force=True)
                resumed = self.navigator(path=path)
                self.assertEqual(resumed.coverage[(10, 0, 0)], 2)
                self.assertEqual(resumed.failures[(0, 0, 0, 1, 0, 0)], 1)
                self.assertFalse(navigator.coverage.pending)

    def test_forward_sidewalk_is_kept_instead_of_randomly_leaving_it(self):
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
        self.assertEqual(departures, 0)

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
        self.assertGreater(target[1], 1518)
        self.assertIsNone(navigator.local_target)

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
        navigator.edge_visits.update({((0, 0), (0, 1)): 20, ((0, 1), (0, 3)): 20})
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

    def test_journey_watchdog_survives_short_waypoints_and_recovery_replans(self):
        navigator = self.navigator()
        navigator.open_goal = (0, 100, 0)
        for now in range(int(JOURNEY_STALL_SECONDS) + 1):
            position = (3 * math.sin(now), 3 * math.cos(now), 0)
            navigator.last_position = navigator.local_origin = position
            navigator.local_target = (position[0], position[1] + 10, 0)
            if now == 5:
                navigator.avoid_target(now, retreat=False)
            abandoned = navigator.journey_stalled_out(position, now)
            self.assertEqual(abandoned, now == JOURNEY_STALL_SECONDS)
        self.assertIsNone(navigator.open_goal)
        self.assertIsNone(navigator.local_target)
        self.assertFalse(navigator.recovery_requested)
        self.assertFalse(navigator.returning)
        self.assertTrue(navigator.approach_blocked(position, (0, 100, 0), 46))
        replacement = navigator.waypoint(position, 0, 0, 46)
        self.assertIsNotNone(replacement)
        self.assertIsNotNone(navigator.open_goal)
        self.assertNotEqual(navigator.open_goal, (0, 100, 0))
        self.assertFalse(navigator.approach_blocked(position, replacement, 46))

    def test_journey_watchdog_measures_best_distance_instead_of_total_movement(self):
        navigator = self.navigator()
        navigator.open_goal = (0, 100, 0)
        for now in range(int(JOURNEY_STALL_SECONDS) + 1):
            # Repeated tiny improvements and reversals cannot refill the budget.
            position = (0, (now % 5) * .2, 0)
            self.assertEqual(navigator.journey_stalled_out(position, now), now == JOURNEY_STALL_SECONDS)

    def test_stalled_journey_replans_without_another_jump_in_controller(self):
        with patch('core.controller.Keys'):
            movement = Walking()
        movement.set_mode('explore')
        movement.jump_supported = True
        navigator = movement.navigator
        navigator.open_goal = (0, 100, 0)
        navigator.local_origin = (0, 0, 0)
        navigator.local_target = (0, 10, 0)
        for now in range(int(JOURNEY_STALL_SECONDS)):
            self.assertFalse(navigator.journey_stalled_out((0, 0, 0), now))
        state = dict(ready=True, foreground=True, paused=False, controls_disabled=False,
                     in_vehicle=False, health=100, player_state=0, ped_state=1,
                     interior=0, heading=0, position=dict(x=0, y=0, z=0))
        with patch('core.controller.time.monotonic', return_value=JOURNEY_STALL_SECONDS):
            self.assertNotIn('stopped', movement.tick(state, True, 'explore'))
        self.assertIsNone(movement.recovery_jump_setup)
        self.assertEqual(movement.recoveries, 0)
        with patch('core.controller.time.monotonic', return_value=JOURNEY_STALL_SECONDS + .1):
            movement.tick(state, True, 'explore')
        self.assertIsNotNone(navigator.local_target)
        self.assertNotEqual(navigator.open_goal, (0, 100, 0))

    def test_journey_watchdog_allows_a_successful_long_walk(self):
        navigator = self.navigator()
        navigator.open_goal = (0, 1000, 0)
        for now in range(300):
            self.assertFalse(navigator.journey_stalled_out((0, now * .5, 0), now))
        self.assertEqual(navigator.open_goal, (0, 1000, 0))

    def test_journey_watchdog_excludes_pause_gaps_and_resets_for_new_goal(self):
        navigator = self.navigator()
        navigator.open_goal = (0, 100, 0)
        for now in range(11):
            self.assertFalse(navigator.journey_stalled_out((0, 0, 0), now))
        self.assertFalse(navigator.journey_stalled_out((0, 0, 0), 1000))
        self.assertEqual(navigator.journey_stalled, 10)
        navigator.open_goal = (100, 0, 0)
        for now in range(1001, 1021):
            self.assertFalse(navigator.journey_stalled_out((0, 0, 0), now))
        self.assertTrue(navigator.journey_stalled_out((0, 0, 0), 1021))

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
                if memory == 'failure':
                    self.assertGreater(distance(target, forward), 1)
                else:
                    self.assertEqual(target, forward)

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
                self.assertEqual(navigator.goal_edge, ((0, 0), (0, 2)))
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
                    self.assertTrue(navigator.local_target is not None or navigator.target in navigator.graph.nodes)
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

    def test_open_terrain_leaves_blocked_region_and_remembers_walk_after_restart(self):
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
                # failure: the walker must leave the blocked region on new ground.
                if min(position[1], following[1]) <= 32 and max(position[1], following[1]) >= 30 and abs(following[0]) < 10:
                    navigator.avoid_target(now, retreat=False)
                    obstacles += 1
                    continue
                heading = heading_to(position, target)
                position = following
                if distance(position, (0, 0, 0)) > 100:
                    break
            self.assertGreater(obstacles, 0)
            self.assertGreater(distance(position, (0, 0, 0)), 100)
            self.assertTrue(navigator.ground_visits)
            navigator.save_memory(251, force=True)
            resumed = self.navigator(path=path)
            self.assertEqual(resumed.recent_ground, navigator.recent_ground)
            self.assertEqual(resumed.failures, navigator.failures)
            self.assertEqual(resumed.ground_visits, navigator.ground_visits)
            # The restored trail outweighs a backwards-facing spawn camera.
            target = resumed.waypoint(position, math.pi, 0, 252)
            self.assertLess(abs(angle_delta(heading_to(position, target), heading)), math.pi / 2)

    def test_new_journeys_prefer_unused_ground_and_keep_turns_gradual(self):
        navigator = self.navigator()
        for cell_y in range(150):
            navigator.coverage[(0, cell_y, 0)] = 100
        position, heading = (0, 0, 0), 0
        for now in range(100):
            target = navigator.waypoint(position, heading, 0, now)
            self.assertLessEqual(abs(angle_delta(heading_to(position, target), heading)), math.pi / 4 + .01)
            if now == 0:
                self.assertGreater(abs(target[0]), 5)
            heading, position = heading_to(position, target), target
        self.assertGreater(distance(position, (0, 0, 0)), 100)

    def test_open_walk_joins_forward_path_without_turning_back_to_nearest_node(self):
        graph = PedestrianGraph([
            Node((0, 0), (2, 8, 0), (((0, 1), False),)),
            Node((0, 1), (2, 28, 0), (((0, 0), False),)),
        ])
        navigator = self.navigator(graph)
        navigator.local_origin = (0, 0, 0)
        navigator.local_target = (0, 10, 0)
        navigator.open_goal = (0, 100, 0)
        target = navigator.waypoint((0, 10, 0), 0, 0, 1)
        self.assertEqual(target, (2, 28, 0))
        self.assertIsNone(navigator.local_target)
        self.assertEqual(navigator.target, (0, 1))
        self.assertIn('joining a pedestrian path ahead', navigator.message)

    def test_sideways_or_different_height_paths_do_not_interrupt_forward_travel(self):
        for endpoint in ((20, 10, 0), (0, -10, 0), (0, 30, 10)):
            with self.subTest(endpoint=endpoint):
                graph = PedestrianGraph([
                    Node((0, 0), (2, 8, 0), (((0, 1), False),)),
                    Node((0, 1), endpoint, (((0, 0), False),)),
                ])
                navigator = self.navigator(graph)
                navigator.local_origin = (0, 0, 0)
                navigator.local_target = (0, 10, 0)
                navigator.open_goal = (0, 100, 0)
                target = navigator.waypoint((0, 10, 0), 0, 0, 1)
                self.assertAlmostEqual(target[0], 0)
                self.assertGreater(target[1], 10)
                self.assertIsNotNone(navigator.local_target)

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
        self.assertIsNone(navigator.waypoint((1, 0, 0), 0, 0, 11))
        self.assertEqual(sum(navigator.failures.values()), 0)
        self.assertTrue(navigator.recovery_requested)

    def test_stalled_exploration_changes_direction_after_two_seconds(self):
        with patch('core.controller.Keys'):
            movement = Walking()
        state = dict(ready=True, foreground=True, paused=False,
                     controls_disabled=False, in_vehicle=False, health=100,
                     player_state=0, ped_state=1, interior=0, heading=0,
                     position=dict(x=0, y=0, z=0))
        with patch('core.controller.time.monotonic', return_value=0):
            movement.tick(state, True, 'explore')
        target = movement.navigator.local_target
        with patch('core.controller.time.monotonic', return_value=2.1):
            self.assertIn('steering around obstacle', movement.tick(state, True, 'explore'))
        with patch('core.controller.time.monotonic', return_value=2.2):
            movement.tick(state, True, 'explore')
        replacement = movement.recovery_target
        self.assertIsNotNone(replacement)
        self.assertGreaterEqual(abs(angle_delta(heading_to((0, 0, 0), replacement),
                                               heading_to((0, 0, 0), target))), math.pi / 3)
        self.assertIsNone(movement.recovery_jump_setup)

    def test_failed_direction_survives_cell_border_movement_and_expires(self):
        navigator = self.navigator()
        origin = (11.9, 0, 0)
        navigator.waypoint(origin, 0, 0, 0)
        target = navigator.local_target
        navigator.avoid_target(1, retreat=False)
        shifted = (12.1, 0, 0)
        self.assertNotEqual(navigator.coverage_cell(origin), navigator.coverage_cell(shifted))
        self.assertTrue(navigator.approach_blocked(shifted, target, 2))
        replacement = navigator.waypoint(shifted, 0, 0, 2)
        self.assertIsNotNone(replacement)
        self.assertFalse(navigator.approach_blocked(shifted, replacement, 2))
        self.assertFalse(navigator.approach_blocked(shifted, target, 122))
        self.assertFalse(navigator.approach_blocked((12.1, 0, 10), target, 2))

    def test_exploration_backs_and_jumps_first_then_replans_and_can_jump_again(self):
        with patch('core.controller.Keys'):
            movement = Walking()
        movement.jump_supported = True
        state = dict(ready=True, foreground=True, paused=False,
                     controls_disabled=False, in_vehicle=False, health=100,
                     player_state=0, ped_state=1, interior=0, heading=0,
                     standing=True, in_air=False, position=dict(x=0, y=0, z=0))

        def tick(now):
            with patch('core.controller.time.monotonic', return_value=now):
                return movement.tick(state, True, 'explore')

        tick(0)
        target = movement.navigator.local_target
        self.assertIn('backing up for jump', tick(2.1))
        movement.keys.set.assert_called_with({'walk', 'back'})
        self.assertEqual(movement.navigator.local_target, target)
        self.assertEqual(sum(movement.navigator.failures.values()), 0)
        state['position']['y'] = -1.5
        self.assertIn('approaching jump', tick(3.6))
        self.assertIn('jumping', tick(3.85))
        self.assertIsNotNone(movement.recovery_jump_request)
        state.update(in_air=True, standing=False, ped_state=41)
        self.assertIn('jumping', tick(4))
        self.assertEqual(movement.recovery_attempts, 1)
        state.update(in_air=False, standing=True, ped_state=1)
        state['position']['y'] = 1
        tick(5)
        self.assertEqual(movement.recovery_attempts, 1)
        self.assertIsNotNone(movement.recovery_jump_started)
        tick(5.7)
        tick(8.8)
        self.assertIsNone(movement.recovery_jump_started)
        self.assertNotIn('steering around obstacle', tick(10.5))
        self.assertIn('steering around obstacle', tick(11.0))
        self.assertIsNone(movement.navigator.local_target)
        self.assertEqual(sum(movement.navigator.failures.values()), 1)
        tick(11.1)
        self.assertNotEqual(movement.navigator.local_target, target)
        self.assertIn('backing up for jump', movement.recover(state['position'], 12))
        self.assertEqual(movement.recovery_attempts, 3)

    def recovery_session(self):
        for target in ('core.controller.Keys', 'core.controls.Keys'):
            keys_patch = patch(target)
            keys_patch.start()
            self.addCleanup(keys_patch.stop)
        movement = Walking()
        movement.jump_supported = True
        jump = HighJump(Mock(), Mock(), binder=Mock())
        state = dict(ready=True, foreground=True, paused=False,
                     controls_disabled=False, in_vehicle=False, health=100,
                     player_state=0, ped_state=1, interior=0, heading=0,
                     standing=True, in_air=False, vertical_speed=0,
                     ped=100, identity='player', position=dict(x=0, y=0, z=0))

        def tick(now):
            request = movement.recovery_jump_request
            jump.tick(request, state, now)
            movement.jump_feedback(jump.last_request, jump.result)
            if request and jump.last_request == request['id']:
                movement.recovery_jump_request = None
            with patch('core.controller.time.monotonic', return_value=now):
                return movement.tick(state, True, 'explore')

        tick(0)
        self.assertIn('backing up', tick(2.1))
        state['position']['y'] = -1.5
        self.assertIn('approaching jump', tick(3.6))
        self.assertIn('jumping', tick(3.85))
        return movement, jump, state, tick

    def test_rejected_recovery_jump_waits_for_existing_airtime_and_landing(self):
        movement, jump, state, tick = self.recovery_session()
        state.update(in_air=True, standing=False)
        self.assertIn('jumping', tick(3.86))
        self.assertEqual(jump.result, 'rejected')
        state.update(in_air=False, standing=True)
        self.assertIn('recovering from landing', tick(4))
        tick(4.7)
        self.assertIn('exploring', tick(7.8))
        self.assertIsNone(movement.recovery_jump_started)
        self.assertIsNone(movement.recovery_jump_request)
        self.assertEqual(movement.recovery_attempts, 1)
        self.assertEqual(movement.jump_budget_used, 1)
        tick(7.9)
        self.assertIsNone(movement.recovery_jump_started)

    def test_twenty_foot_turnarounds_do_not_refill_jump_budget(self):
        with patch('core.controller.Keys'):
            movement = Walking()
        movement.set_mode('explore')
        movement.jump_supported = True
        jumps = 0
        for now in range(30):
            # Reproduce walking roughly twenty feet and turning back, including
            # the interruptions that previously restarted the first attempt.
            y = 6 if now % 2 else 0
            movement.stop()
            movement.navigator.waypoint((0, y, 0), 0, 0, now)
            status = movement.recover(dict(x=0, y=y, z=0), now)
            jumps += 'backing up for jump' in status
        self.assertEqual(jumps, 2)
        # Even a longer detour must remember the old obstacle on returning.
        movement.stop()
        movement.recover(dict(x=0, y=30, z=0), 31)
        movement.stop()
        self.assertNotIn('backing up for jump', movement.recover(dict(x=0, y=0, z=0), 32))

    def test_passing_short_waypoint_does_not_turn_back_after_a_jump(self):
        navigator = self.navigator()
        target = navigator.waypoint((0, 0, 0), 0, 0, 0)
        self.assertEqual(target, (0, 10, 0))
        following = navigator.waypoint((0, 16, 0), 0, 0, 2)
        self.assertIsNotNone(following)
        self.assertGreater(following[1], 16)
        self.assertNotEqual(following, target)

    def test_failed_takeoff_replans_without_waiting_ten_seconds(self):
        movement, jump, state, tick = self.recovery_session()
        tick(3.86)
        self.assertEqual(jump.result, 'accepted')
        with self.assertLogs('core.controls', level='WARNING'):
            self.assertIn('steering around obstacle', tick(5.87))
        self.assertEqual(jump.result, 'failed')
        self.assertIsNone(movement.recovery_jump_started)
        self.assertEqual(movement.recovery_attempts, 2)

    def test_landing_animation_preserves_history_and_local_jump_budget_is_bounded(self):
        movement, jump, state, tick = self.recovery_session()
        tick(3.86)
        state.update(in_air=True, standing=False, ped_state=41, vertical_speed=.2)
        self.assertIn('jumping', tick(4))
        self.assertEqual(jump.result, 'airborne')
        state.update(in_air=False, standing=True, vertical_speed=0)
        self.assertIn('recovering from landing', tick(5))
        self.assertEqual(movement.recovery_attempts, 1)
        self.assertIsNotNone(movement.recovery_anchor)
        state['ped_state'] = 1
        tick(6)
        tick(6.7)
        self.assertNotIn('steering around obstacle', tick(8.1))
        tick(9.8)
        self.assertIn('steering around obstacle', tick(12.0))
        tick(12.1)
        self.assertIn('backing up', movement.recover(state['position'], 12.2, state))
        self.assertEqual(movement.jump_budget_used, 2)
        # The same interruptions that used to reset the loop cannot refill it.
        for now in range(13, 23):
            movement.stop()
            movement.navigator.waypoint((0, -1.5, 0), 0, 0, now)
            status = movement.recover(state['position'], now, state)
            self.assertNotIn('backing up for jump', status)
            self.assertIsNone(movement.recovery_jump_setup)
        # An actual escape starts a fresh obstacle episode.
        state['position']['y'] = 20
        movement.stop()
        self.assertIn('backing up', movement.recover(state['position'], 24, state))
        self.assertEqual(movement.jump_budget_used, 1)

    def test_recovery_does_not_schedule_jump_without_ground_contact(self):
        for standing, airborne in ((False, True), (False, False), (True, True)):
            with self.subTest(standing=standing, airborne=airborne), patch('core.controller.Keys'):
                movement = Walking()
                movement.set_mode('explore')
                movement.jump_supported = True
                movement.navigator.waypoint((0, 0, 0), 0, 0, 0)
                status = movement.recover(dict(x=0, y=0, z=0), 2,
                                          dict(standing=standing, in_air=airborne))
                self.assertIn('steering around obstacle', status)
                self.assertIsNone(movement.recovery_jump_setup)
                self.assertEqual(movement.jump_budget_used, 0)

    def test_blocked_sidewalk_replans_immediately_without_abandoning_exploration(self):
        with patch('core.controller.Keys'):
            movement = Walking(network())
        movement.set_mode('explore')
        navigator = movement.navigator
        navigator.open_terrain_chance = 0
        navigator.waypoint((0, 0, 0), 0, 0, 0)
        navigator.previous, navigator.target = (0, 0), (0, 1)
        navigator.goal = (0, 1)
        status = movement.recover(dict(x=0, y=0, z=0), 3)
        self.assertIn('steering around obstacle', status)
        self.assertIsNone(navigator.target)
        self.assertIsNone(navigator.goal)
        self.assertIn(((0, 0), (0, 1)), navigator.blocked)
        self.assertEqual(navigator.edge_failures[((0, 0), (0, 1))], 1)
        self.assertIsNotNone(movement.recovery_target)
        self.assertTrue(navigator.exploring)

    def test_steep_height_change_requests_recovery_before_abandoning_route(self):
        navigator = self.navigator()
        navigator.waypoint((0, 0, 0), 0, 0, 0)
        self.assertIsNone(navigator.waypoint((0, 4, 5), 0, 0, 1))
        self.assertEqual(sum(navigator.failures.values()), 0)
        self.assertTrue(navigator.recovery_requested)
        self.assertIsNotNone(navigator.local_target)

    def test_exploration_watchdogs_commit_to_detour_without_automatic_backtracking(self):
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
                    self.assertIn('steering around obstacle', movement.tick(state, True, 'explore'))
                self.assertEqual(movement.navigator.open_goal, goal)
                self.assertIsNone(movement.navigator.local_target)
                self.assertEqual(sum(movement.navigator.failures.values()), 1)
                self.assertIsNotNone(movement.recovery_target)
                self.assertIsNone(movement.recovery_jump_setup)
                self.assertFalse(movement.navigator.returning)
                self.assertFalse(movement.navigator.recovery_requested)
                with patch('core.controller.time.monotonic', return_value=now + .1):
                    movement.tick(state, True, 'explore')
                self.assertIsNotNone(movement.recovery_target)
                self.assertNotEqual(movement.recovery_target, target)

    def test_coverage_keeps_overlapping_elevations_separate(self):
        navigator = self.navigator()
        navigator.observe((1, 1, 0), 0)
        navigator.observe((1, 1, 8), 1)
        self.assertEqual(len(navigator.coverage), 2)

    def test_mountain_failures_abandon_goal_and_remain_blocked_after_moving_away(self):
        navigator = self.navigator()
        navigator.waypoint((0, 0, 0), 0, 0, 0)
        original_goal = navigator.open_goal
        navigator.avoid_target(1, retreat=False)
        navigator.waypoint((0, -20, 0), 0, 0, 2)
        navigator.avoid_target(3, retreat=False)
        self.assertTrue(navigator.route_abandoned)
        self.assertIsNone(navigator.open_goal)
        target = navigator.waypoint((0, -20, 0), 0, 0, 4)
        self.assertNotEqual(navigator.open_goal, original_goal)
        self.assertFalse(navigator.approach_blocked((0, -20, 0), target, 4))
        self.assertTrue(navigator.approach_blocked((0, 0, 0), original_goal, 250))
        self.assertFalse(navigator.approach_blocked((0, 0, 0), original_goal, 304))

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
            for index, content in enumerate(('{', '[]', '{"version":1,"coverage":[[0,0,0,-1]]}',
                            '{"version":1,"recent_ground":[[0,0,NaN]]}',
                            '{"version":1,"recent_edges":[[0,1]]}')):
                path = Path(folder) / f'exploration-{index}.json'
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

    def test_repeated_off_path_failures_abandon_course_without_automatic_backtracking(self):
        with patch('core.controller.Keys'):
            movement = Walking()
            movement.set_mode('explore')
            movement.jump_supported = False
            movement.navigator.waypoint((0, 0, 0), 0, 0, 0)
            goal, target = movement.navigator.open_goal, movement.navigator.local_target
            for attempt in (1, 2):
                status = movement.recover({'x': 0, 'y': 0, 'z': 0}, attempt)
                self.assertIn('steering around obstacle', status)
                self.assertIsNone(movement.recovery_jump_setup)
                self.assertIsNotNone(movement.recovery_target)
                if attempt == 1:
                    self.assertEqual(movement.navigator.open_goal, goal)
                else:
                    self.assertIsNone(movement.navigator.open_goal)
                self.assertIsNone(movement.navigator.local_target)
                self.assertFalse(movement.navigator.returning)
                movement.navigator.waypoint((0, 0, 0), 0, 0, attempt + .1)
                self.assertNotEqual(movement.navigator.local_target, target)
            self.assertIn('steering around obstacle', movement.recover({'x': 0, 'y': 0, 'z': 0}, 3))
            self.assertIsNotNone(movement.recovery_target)
            self.assertIsNone(movement.navigator.local_target)
            self.assertIsNone(movement.navigator.open_goal)
            self.assertEqual(sum(movement.navigator.failures.values()), 3)
            self.assertFalse(movement.navigator.returning)
            movement.navigator.waypoint((0, 0, 0), 0, 0, 7)
            self.assertNotEqual(movement.navigator.local_target, target)
