"""Shoreline safety independent of routing, fallback, and recovery."""

import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.controller import Walking
from core.exploration import ExploreNavigator
from core.navigation import Node, PedestrianGraph
from core.terrain import WaterAvoidance
from tests.test_terrain import World


class WaterAvoidanceTests(unittest.TestCase):
    def test_missing_support_above_water_rejects_high_drops(self):
        guard = self.guard(World(lambda x, y: None), '6, -100, -100, 100, 100')
        self.assertIs(guard.danger((0, 0, 11.65), 0, 0), True)
        # Confirmed submerged support still vetoes movement from a high ledge.
        guard.sensor.height = lambda x, y: 5
        self.assertIs(guard.danger((0, 0, 11.65), 0, 1), True)

    def guard(self, world, contents='0, -100, 10, 100, 100'):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data').mkdir()
            (root / 'data/water.dat').write_text(contents)
            world.mem = SimpleNamespace(exe=root / 'gta3.exe')
            return WaterAvoidance(world)

    def test_shoreline_is_rejected_before_arriving_but_safe_ground_and_bridges_are_allowed(self):
        world = World(lambda x, y: 2 if y < 10 else None)
        guard = self.guard(world)
        self.assertIs(guard.danger((0, 6, 3), 0, 0), True)
        self.assertIs(guard.danger((0, 6, 3), math.pi, 1), False)
        # Water rectangles extend beneath land and bridges; map overlap alone
        # must not resurrect the old false-obstacle problem.
        for ground in (2, 10):
            world.height = lambda x, y: ground
            guard.probe = None
            self.assertIs(guard.danger((0, 15, ground + 1), 0, 2), False)

    def test_submerged_support_is_rejected_and_unknown_samples_wait(self):
        world = World(lambda x, y: 0)
        guard = self.guard(world)
        world.pending = True
        self.assertIsNone(guard.danger((0, 8, 1), 0, 0))
        world.pending = False
        self.assertIs(guard.danger((0, 8, 1), 0, 1), True)

    def test_san_andreas_water_vertices_exclude_points_outside_triangle(self):
        guard = self.guard(World(), 'processed\n0 0 0 0 0 0 0  10 0 0 0 0 0 0  0 10 0 0 0 0 0  1')
        self.assertEqual(guard.level(2, 2), 0)
        self.assertIsNone(guard.level(8, 8))

    def test_pending_shoreline_checks_remain_unknown_as_walker_moves(self):
        world = World(lambda x, y: 2)
        guard = self.guard(world)
        self.assertIs(guard.danger((0, 15, 3), 0, 0), False)
        world.pending = True
        for y, heading, now in ((15.8, 0, .8), (16.1, 0, .9), (15.8, .3, 2)):
            self.assertIsNone(guard.danger((0, y, 3), heading, now))

    def test_confirmed_water_overrides_recent_safe_shoreline_probe(self):
        world = World(lambda x, y: 2)
        guard = self.guard(world)
        self.assertIs(guard.danger((0, 15, 3), 0, 0), False)
        world.height = lambda x, y: None
        self.assertIs(guard.danger((0, 15.8, 3), 0, .8), True)
        world.pending = True
        self.assertIsNone(guard.danger((0, 15.8, 3), 0, .9))

    def test_free_walk_rejoins_a_path_ahead_instead_of_returning_to_old_waypoint(self):
        graph = PedestrianGraph([Node((0, i), (0, y, 0),
            tuple(((0, j), False) for j in (i - 1, i + 1) if 0 <= j < 4))
            for i, y in enumerate((0, 10, 30, 50))])
        navigator = ExploreNavigator(graph, open_terrain_chance=0)
        navigator.exploring = True
        navigator.target, navigator.previous = (0, 1), (0, 0)
        navigator.observe((0, 0, 0), 0)
        navigator.observe((0, 20, 0), 10)
        navigator.resume_after_free_walk((0, 28, 0), 0, 30)
        target = navigator.waypoint((0, 28, 0), 0, 0, 30)
        self.assertGreater(target[1], 28)
        self.assertEqual(navigator.travel, 28)
        self.assertFalse(navigator.returning)

    def test_fallback_water_check_commits_to_less_used_ground_without_forced_retreat(self):
        for recent in (True, False):
            with self.subTest(recent=recent), patch('core.controller.Keys'):
                world = World(lambda x, y: 2 if y < 10 else None)
                guard = self.guard(world)
                graph = PedestrianGraph([
                    Node((0, 0), (0, 6, 3), (((0, 1), False),)),
                    Node((0, 1), (0, -4, 3), (((0, 0), False),))])
                walk = Walking(graph, turn_multiplier=4)
                walk.set_mode('explore')
                walk.free_walk_until = 30
                walk.water_guard = guard
                for step, x in enumerate(range(-36, 1, 3)):
                    walk.navigator.observe((x, 6, 3), step)
                if not recent:
                    walk.navigator.recent_ground.clear()  # Saved counters still guide us.
                state = dict(ready=True, foreground=True, paused=False, controls_disabled=False,
                    in_vehicle=False, health=100, player_state=0, ped_state=1, standing=True,
                    in_air=False, in_water=False, interior=0, heading=0,
                    camera_heading=0, position=dict(x=0., y=6., z=3.))
                with patch('core.controller.time.monotonic', return_value=20):
                    self.assertIn('less-used ground', walk.tick(state, True, 'explore'))
                target = walk.navigator.local_target
                self.assertGreater(target[0], 9)
                self.assertAlmostEqual(target[1], 6)
                self.assertGreater(walk.navigator.open_goal[0], 90)
                # A nearby familiar sidewalk behind us cannot override the decision.
                self.assertEqual(walk.navigator.waypoint((0, 6, 3), math.pi, 0, 20.1), target)
                self.assertFalse(walk.navigator.returning)
                self.assertEqual(walk.recoveries, 0)
                self.assertIsNone(walk.recovery_jump_request)



class ContinuousWaterGuardTests(unittest.TestCase):
    guard = WaterAvoidanceTests.guard

    def state(self, **changes):
        state = dict(ready=True, foreground=True, paused=False, controls_disabled=False,
            in_vehicle=False, health=100, player_state=0, ped_state=1, standing=True,
            in_air=False, in_water=False, interior=0, heading=0,
            camera_heading=0, position=dict(x=0., y=2., z=3.))
        state.update(changes)
        return state

    def test_detects_ocean_eight_metres_ahead_and_beside_centre(self):
        world = World(lambda x, y: None if x > .2 and y >= 10 else 2)
        guard = self.guard(world)
        self.assertIs(guard.danger((0, 2, 3), 0, 0), True)

    def test_regular_routes_fallback_and_jump_recovery_all_respect_water(self):
        for free, recovery in ((0, False), (30, False), (0, True)):
            with self.subTest(free=free, recovery=recovery), patch('core.controller.Keys'):
                walk = Walking()
                walk.set_mode('explore')
                walk.free_walk_until = free
                walk.water_guard = self.guard(World(lambda x, y: 2 if y < 10 else None))
                if recovery:
                    walk.recovery_jump_setup = ('forward', 0, self.state()['position'])
                with patch('core.controller.time.monotonic', return_value=20):
                    self.assertIn('away from water', walk.tick(self.state(), True, 'explore'))
                walk.keys.set.assert_called_with(walk.walk_modifier)
                self.assertIsNone(walk.recovery_jump_setup)
                target = walk.water_guard.turn_target
                with patch('core.controller.time.monotonic', return_value=20.1):
                    self.assertIn('turning away', walk.tick(self.state(), True, 'explore'))
                self.assertEqual(walk.water_guard.turn_target, target)
                walk.keys.set.assert_called_with(walk.walk_modifier)
                walk.keys.turn.assert_called()

    def test_pending_checks_do_not_interrupt_forward_movement(self):
        with patch('core.controller.Keys'):
            walk = Walking()
            walk.set_mode('explore')
            walk.free_walk_until = 30
            world = World(lambda x, y: 2)
            world.pending = True
            walk.water_guard = self.guard(world)
            with patch('core.controller.time.monotonic', return_value=20):
                walk.tick(self.state(), True, 'explore')
            walk.keys.set.assert_called_with(walk.walk_modifier | {'forward'})
            self.assertIsNone(walk.water_guard.turn_target)

    def test_manual_swimming_air_and_interiors_are_not_redirected(self):
        with patch('core.controller.Keys'):
            walk = Walking()
            guard = self.guard(World(lambda x, y: None))
            for change in ({'in_water': True}, {'in_air': True}, {'interior': 1}):
                self.assertIsNone(guard.tick(walk, self.state(**change), 0))
            self.assertFalse(guard.sensor.calls)
            walk.water_guard = guard
            with patch('core.controller.time.monotonic', return_value=20):
                walk.tick(self.state(), True, 'free')
            self.assertFalse(guard.sensor.calls)
