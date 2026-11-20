"""Collision-based routing, async bridge ownership, and SA shore recovery."""

import math
import struct
import unittest
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.controller import Walking
from core.navigation import Node, PedestrianGraph
from core.terrain import HEADER, MAX_RAYS, RAY, NativeTerrain, SwimNavigator, TerrainNavigator


class World:
    """Deterministic ground and box collisions, independent of the planner."""

    enabled = True
    checked = 0
    reason = 'test world'
    mem = SimpleNamespace(profile=SimpleNamespace(id='san_andreas'))

    def __init__(self, ground=None, walls=()):
        self.height = ground or (lambda x, y: 0.)
        self.walls = list(walls)
        self.pending = False
        self.calls = []

    def pump(self, now):
        pass

    def ray(self, start, end, flags, now):
        self.calls.append((start, end, flags))
        if self.pending:
            return None
        self.checked += 1
        if start[:2] == end[:2]:
            height = self.height(*start[:2])
            if height is None or not end[2] <= height <= start[2]:
                return False
            return ((start[0], start[1], height), (0, 0, 1), height < -.15)
        for x1, y1, x2, y2, z in self.walls:
            lo, hi = 0., 1.
            for a, b, minimum, maximum in zip(start[:2], end[:2], (x1, y1), (x2, y2)):
                delta = b - a
                if abs(delta) < 1e-8:
                    if not minimum <= a <= maximum:
                        lo, hi = 1., 0.
                        break
                else:
                    enter, leave = sorted(((minimum - a) / delta, (maximum - a) / delta))
                    lo, hi = max(lo, enter), min(hi, leave)
            if lo <= hi and min(start[2], end[2]) <= z:
                return (start, (1, 0, 0), False)
        return False


class TerrainTests(unittest.TestCase):
    def route(self, nav, start, goal):
        result = None
        for step in range(3):
            result = nav.route(start, goal, step * .1)
            if result is not None:
                return result
        return result

    def test_finds_route_out_of_u_shaped_fence_even_when_first_step_is_away(self):
        world = World(walls=[(-3, -2, -2.8, 6, 3), (2.8, -2, 3, 6, 3),
                             (-3, 5.8, 3, 6, 3)])
        nav = TerrainNavigator(world)
        start, goal = (0, 0, 1), (0, 10, 1)
        first = self.route(nav, start, goal)
        self.assertIsNotNone(first)
        self.assertLess(first[1], 0)
        self.assertTrue(any(abs(p[0]) >= 4 for p in nav.path))
        for a, b in zip([(0, 0, 0)] + nav.path, nav.path):
            self.assertTrue(nav.corridor(a, b, 0))

    def test_clear_walk_keeps_long_corridors_and_never_runs_astar(self):
        nav = TerrainNavigator(World())
        for frame in range(160):
            position = (0, frame * .1, 1)
            target = nav.route(position, (0, 60, 1), frame * .1)
            self.assertIsNotNone(target)
            self.assertGreater(target[1] - position[1], 3)
        self.assertEqual(nav.plans, 0)

    def test_background_refresh_does_not_pause_an_existing_valid_corridor(self):
        world = World()
        nav = TerrainNavigator(world)
        self.assertIsNotNone(self.route(nav, (0, 0, 1), (0, 30, 1)))
        world.pending = True
        self.assertIsNotNone(nav.route((0, .1, 1), (0, 30, 1), .7))
        self.assertIsNotNone(nav.route((0, .2, 1), (0, 30, 1), .8))
        self.assertEqual(nav.plans, 0)

    def test_delayed_checks_allow_a_long_uninterrupted_walk(self):
        class DelayedGround(World):
            def __init__(self):
                super().__init__()
                self.ready = {}

            def ray(self, start, end, flags, now):
                due = self.ready.setdefault((start, end, flags), now + .05)
                return super().ray(start, end, flags, now) if now >= due else None

        nav = TerrainNavigator(DelayedGround())
        walked, interrupted, moving = 0., 0, False
        for frame in range(1800):
            target = nav.route((0, walked, 1), (0, 100, 1), frame / 60)
            if target is None:
                interrupted += int(moving)
            else:
                walked += .02
                moving = True
        self.assertGreater(walked, 35)
        self.assertEqual(interrupted, 0)
        self.assertEqual(nav.plans, 0)

    def test_unknown_ground_pauses_instead_of_becoming_a_clear_path(self):
        world = World()
        world.pending = True
        nav = TerrainNavigator(world)
        self.assertIsNone(nav.route((0, 0, 1), (0, 10, 1), 0))
        self.assertFalse(nav.blocked)
        world.pending = False
        self.assertIsNotNone(self.route(nav, (0, 0, 1), (0, 10, 1)))

    def test_missing_support_water_steep_grade_and_narrow_gap_are_rejected(self):
        for height in (lambda x, y: None if .5 < y < 1.5 else 0,
                       lambda x, y: -1 if .5 < y < 1.5 else 0):
            nav = TerrainNavigator(World(height))
            self.assertFalse(nav.corridor((0, 0, 0), (0, 2, 0), 0))
        self.assertFalse(TerrainNavigator(World()).corridor((0, 0, 0), (0, 2, 3), 0))
        world = World(walls=[(-2, .5, -.2, 1.5, 3), (.2, .5, 2, 1.5, 3)])
        self.assertFalse(TerrainNavigator(world).corridor((0, 0, 0), (0, 2, 0), 0))

    def test_bridge_probe_stays_on_its_current_surface_and_tracks_gentle_slope(self):
        nav = TerrainNavigator(World(lambda x, y: 10 if x >= 0 else 0))
        self.assertFalse(nav.ground(-1, 0, 10, 0))
        self.assertEqual(nav.ground(1, 0, 10, 0)[2], 10)
        slope = TerrainNavigator(World(lambda x, y: y * .25))
        self.assertIsNotNone(self.route(slope, (0, 0, 1), (0, 10, 3.5)))
        self.assertGreater(slope.path[-1][2], 1)

    def test_new_obstruction_invalidates_the_next_step(self):
        world = World()
        nav = TerrainNavigator(world)
        self.assertIsNotNone(self.route(nav, (0, 0, 1), (0, 10, 1)))
        world.walls.append((-1, .8, 1, 1.2, 3))
        result = nav.route((0, 0, 1), (0, 10, 1), 1)
        if result is None:
            result = nav.route((0, 0, 1), (0, 10, 1), 1.1)
        self.assertTrue(result is None or abs(result[0]) >= 1)

    def test_validated_corridor_allows_small_forward_steps_without_requerying(self):
        world = World()
        nav = TerrainNavigator(world)
        self.route(nav, (0, 0, 1), (0, 10, 1))
        count = len(world.calls)
        self.assertIsNotNone(nav.route((0, .1, 1), (0, 10, 1), .2))
        self.assertEqual(len(world.calls), count)

    def test_walking_waits_without_spending_stuck_budget_or_recording_a_failure(self):
        with patch('core.controller.Keys'):
            walk = Walking()
            walk.set_mode('explore')
            walk.terrain = TerrainNavigator(World())
            walk.terrain.sensor.pending = True
            state = dict(ready=True, foreground=True, paused=False, controls_disabled=False,
                         in_vehicle=False, health=100, player_state=0, ped_state=1,
                         interior=0, heading=0, standing=True, in_air=False,
                         position=dict(x=0, y=0, z=1))
            for now in range(25):
                with patch('core.controller.time.monotonic', return_value=now):
                    self.assertIn('sampling', walk.tick(state, True, 'explore'))
            self.assertEqual(walk.recoveries, 0)
            self.assertFalse(walk.navigator.failures)
            walk.keys.set.assert_called_with({'walk'})

    def test_repeated_query_waits_preserve_accumulated_actual_walking_stalls(self):
        with patch('core.controller.Keys'):
            walk = Walking()
            walk.set_mode('explore')
            walk.navigator.waypoint = Mock(return_value=(0, 10, 0))
            walk.terrain = SimpleNamespace(enabled=True, blocked=False, status='sampling',
                                           route=Mock(), reset=Mock())
            state = dict(ready=True, foreground=True, paused=False, controls_disabled=False,
                         in_vehicle=False, health=100, player_state=0, ped_state=1,
                         interior=0, heading=0, standing=True, in_air=False,
                         position=dict(x=0, y=0, z=0))
            for now, waiting in ((0, False), (.6, True), (1, False), (1.6, True),
                                 (2, False), (2.6, True), (3, False), (3.3, False)):
                walk.terrain.route.return_value = None if waiting else (0, 10, 0)
                with patch('core.controller.time.monotonic', return_value=now):
                    walk.tick(state, True, 'explore')
            self.assertEqual(walk.recoveries, 1)

    def test_temporary_obstruction_replans_without_permanent_failure_memory(self):
        for temporary in (True, False):
            with self.subTest(temporary=temporary), patch('core.controller.Keys'):
                walk = Walking()
                walk.set_mode('explore')
                walk.terrain = SimpleNamespace(enabled=True, blocked=True, temporary=temporary,
                                               route=Mock(return_value=None), reset=Mock())
                state = dict(ready=True, foreground=True, paused=False, controls_disabled=False,
                             in_vehicle=False, health=100, player_state=0, ped_state=1,
                             interior=0, heading=0, standing=True, in_air=False,
                             position=dict(x=0, y=0, z=0))
                with patch('core.controller.time.monotonic', return_value=0):
                    walk.tick(state, True, 'explore')
                self.assertEqual(sum(walk.navigator.failures.values()), int(not temporary))

    def test_async_twelve_ray_frame_budget_eventually_finds_u_fence_exit(self):
        class BufferedWorld(World):
            def __init__(self):
                super().__init__(walls=[(-3, -2, -2.8, 6, 3), (2.8, -2, 3, 6, 3),
                                       (-3, 5.8, 3, 6, 3)])
                self.results, self.queue = {}, OrderedDict()
                self.last_pump = None

            def ray(self, start, end, flags, now):
                key = (start, end, flags)
                if key not in self.results and len(self.queue) < MAX_RAYS:
                    self.queue[key] = None
                return self.results.get(key)

            def pump(self, now):
                if self.last_pump == now:
                    return
                self.last_pump = now
                for key in list(self.queue)[:12]:
                    self.results[key] = super().ray(*key, now)
                    del self.queue[key]

        nav = TerrainNavigator(BufferedWorld())
        result = None
        for frame in range(1200):
            result = nav.route((0, 0, 1), (0, 10, 1), frame / 60)
            if result is not None:
                break
        self.assertIsNotNone(result)
        self.assertLess(result[1], 0)
        self.assertLess(frame, 600)


class Memory:
    profile = SimpleNamespace(id='san_andreas')

    def __init__(self):
        self.data = bytearray(8192)
        self.write(64, HEADER.pack(1, 1, 1, 0, 0, 0, 0))

    def read(self, address, size):
        return bytes(self.data[address:address + size])

    def write(self, address, data):
        self.data[address:address + len(data)] = data

    def unpack(self, address, fmt):
        return struct.unpack('<' + fmt, self.read(address, struct.calcsize('<' + fmt)))

    def u32(self, address):
        return self.unpack(address, 'I')[0]

    def module(self, name):
        return {'base': 0, 'size': len(self.data), 'path': 'pinned.asi'}


class MailboxTests(unittest.TestCase):
    def sensor(self):
        mem = Memory()
        with patch('core.terrain.read_json', return_value={'terrain_rva': 64, 'sha256': 'pinned'}), \
                patch('core.terrain.sha256', return_value='pinned'):
            return NativeTerrain(mem), mem

    def test_bounded_batch_is_not_consumed_until_matching_completion(self):
        sensor, mem = self.sensor()
        for i in range(100):
            self.assertIsNone(sensor.ray((i, 0, 1), (i, 0, -1), 2, 0))
        self.assertEqual(len(sensor.pending), MAX_RAYS)
        sensor.pump(0)
        sequence = sensor.sequence
        self.assertEqual(mem.u32(64 + 20), MAX_RAYS)
        sensor.pump(.1)
        self.assertFalse(sensor.cache)
        self.assertEqual(sensor.sequence, sequence)
        mem.write(64 + HEADER.size, RAY.pack(0, 0, 1, 0, 0, -1, 2, 1,
                                            0, 0, 0, 0, 0, 1, 0))
        mem.write(64 + 16, struct.pack('<I', sequence))
        sensor.pump(.2)
        self.assertEqual(sensor.ray((0, 0, 1), (0, 0, -1), 2, .3),
                         ((0., 0., 0.), (0., 0., 1.), False))

    def test_stalled_bridge_falls_back_without_overwriting_inflight_request(self):
        sensor, mem = self.sensor()
        sensor.ray((0, 0, 1), (0, 0, -1), 2, 0)
        sensor.pump(0)
        sequence = mem.u32(64 + 12)
        for now in (.5, 1, 1.5, 2.1):
            sensor.pump(now)
        self.assertFalse(sensor.enabled)
        self.assertEqual(mem.u32(64 + 12), sequence)

    def test_gameplay_pause_does_not_permanently_disable_sensing(self):
        sensor, mem = self.sensor()
        sensor.ray((0, 0, 1), (0, 0, -1), 2, 0)
        sensor.pump(0)
        sensor.pump(120)
        self.assertTrue(sensor.enabled)

    def test_wrong_protocol_or_busy_mailbox_is_not_adopted(self):
        sensor, mem = self.sensor()
        for header in ((2, 1, 1, 0, 0, 0, 0), (1, 1, 1, 2, 1, 0, 0)):
            mem.write(64, HEADER.pack(*header))
            with patch('core.terrain.read_json', return_value={'terrain_rva': 64, 'sha256': 'pinned'}), \
                    patch('core.terrain.sha256', return_value='pinned'):
                self.assertFalse(NativeTerrain(mem).enabled)


class SwimTests(unittest.TestCase):
    def state(self, **updates):
        return dict(position=dict(x=0, y=0, z=0), standing=True, in_air=False,
                    heading=0, vertical_speed=0, **updates)

    def test_two_minutes_then_commits_to_observed_land_and_requires_stable_exit(self):
        world = World(lambda x, y: 0 if y < 0 else -1)
        nav = SwimNavigator(world, PedestrianGraph([]))
        dry = self.state()
        dry['position']['y'] = -10
        self.assertFalse(nav.observe(dry, 0, True))
        wet = self.state(in_water=True)
        for i in range(481):
            self.assertTrue(nav.observe(wet, i * .25, True))
        self.assertEqual(nav.elapsed, 120)
        target = nav.waypoint((0, 0, 0), 120)
        self.assertLess(target[1], 0)
        self.assertEqual(nav.goal, (0, -10, 0))
        self.assertEqual(nav.shore_goal((1, 0, 0), 121), nav.goal)
        self.assertTrue(nav.observe(dry, 122, True))
        self.assertTrue(nav.observe(wet, 122.1, True))
        self.assertTrue(nav.observe(dry, 123, True))
        self.assertFalse(nav.observe(dry, 124.1, True))
        self.assertEqual(nav.elapsed, 0)

    def test_pause_does_not_spend_swim_budget_and_new_coast_can_be_found(self):
        world = World(lambda x, y: .5 if x >= 15 else -1)
        nav = SwimNavigator(world, PedestrianGraph([]))
        state = self.state(in_water=True)
        nav.observe(state, 0, True)
        nav.observe(state, .2, True)
        nav.observe(state, 1, False)
        nav.observe(state, 500, True)
        self.assertEqual(nav.elapsed, .2)
        nav.elapsed = 120
        self.assertIsNotNone(nav.waypoint((0, 0, 0), 501))
        self.assertGreaterEqual(nav.goal[0], 15)

    def test_nearby_water_is_not_a_shore_target_and_stalled_goal_is_reconsidered(self):
        nav = SwimNavigator(World(lambda x, y: -1), PedestrianGraph([]))
        nav.elapsed = 120
        nav.waypoint((0, 0, 0), 0)
        self.assertIsNone(nav.goal)
        nav.goal, nav.progress_at = (0, 10, 0), 0
        nav.best_remaining = 10
        self.assertIsNone(nav.shore_goal((0, 0, 0), 21))
        self.assertIn((0, 10, 0), nav.rejected)

    def test_swimming_bypasses_walking_jump_and_journey_stall_detectors(self):
        with patch('core.controller.Keys'):
            walk = Walking()
            walk.set_mode('explore')
            walk.swim = SwimNavigator(World(), walk.navigator.graph)
            state = dict(self.state(in_water=True), ready=True, foreground=True,
                         paused=False, controls_disabled=False, in_vehicle=False,
                         health=100, player_state=0, ped_state=50, interior=0)
            for now in range(20):
                with patch('core.controller.time.monotonic', return_value=now):
                    self.assertIn('swimming', walk.tick(state, True, 'explore'))
            self.assertEqual(walk.recoveries, 0)
            self.assertFalse(walk.navigator.failures)

    def test_open_ocean_uses_distant_mapped_land_without_waiting_for_unloaded_geometry(self):
        graph = PedestrianGraph([
            Node((0, 0), (1000, 0, 5), (((0, 1), False),)),
            Node((0, 1), (1010, 0, 5), (((0, 0), False),))])
        world = World(lambda x, y: None)
        nav = SwimNavigator(world, graph)
        nav.elapsed = 120
        target = nav.waypoint((0, 0, -1), 0)
        self.assertIsNotNone(target)
        self.assertGreater(target[0], 0)
        self.assertEqual(nav.goal, (1000, 0, 5))
        self.assertFalse(any(start[:2] == end[:2] for start, end, _ in world.calls))

    def test_async_swim_checks_survive_wave_bobbing_and_keep_the_turn_target(self):
        class DelayedOcean(World):
            def __init__(self):
                super().__init__(lambda x, y: None)
                self.requests = {}

            def ray(self, start, end, flags, now):
                key = (start, end, flags)
                ready = self.requests.setdefault(key, now + .05)
                return False if now >= ready else None

        graph = PedestrianGraph([
            Node((0, 0), (1000, 0, 5), (((0, 1), False),)),
            Node((0, 1), (1010, 0, 5), (((0, 0), False),))])
        world = DelayedOcean()
        nav = SwimNavigator(world, graph)
        nav.elapsed = 120
        accepted = []
        for frame in range(60):
            target = nav.waypoint((0, 0, math.sin(frame) - .5), frame / 60)
            if target:
                accepted.append(target)
        self.assertGreater(len(accepted), 50)
        self.assertEqual(len(set(accepted)), 1)
        self.assertLessEqual(len(world.requests), 20)

    def test_mapped_shore_selection_prefers_a_low_approach_over_a_nearby_cliff(self):
        graph = PedestrianGraph([
            Node((0, 0), (300, 0, 60), (((0, 1), False),)),
            Node((0, 1), (310, 0, 60), (((0, 0), False),)),
            Node((1, 0), (500, 0, 5), (((1, 1), False),)),
            Node((1, 1), (510, 0, 5), (((1, 0), False),))])
        nav = SwimNavigator(World(lambda x, y: None), graph)
        nav.elapsed = 120
        nav.waypoint((0, 0, -1), 0)
        self.assertEqual(nav.goal, (500, 0, 5))


class RecoveryTimingTests(unittest.TestCase):
    def state(self, **updates):
        state = dict(ready=True, foreground=True, paused=False, controls_disabled=False,
                     in_vehicle=False, health=100, player_state=0, ped_state=1,
                     interior=0, heading=0, standing=True, in_air=False, vertical_speed=0,
                     position=dict(x=0, y=0, z=0))
        state.update(updates)
        return state

    def test_transient_ground_flag_during_approach_does_not_abort_jump(self):
        with patch('core.controller.Keys'):
            walk = Walking()
            walk.set_mode('explore')
            walk.jump_supported = True
            walk.recover(self.state()['position'], 0, self.state())
            for now, state in ((1.5, self.state()), (1.8, self.state(standing=False)),
                               (2.2, self.state(standing=False)), (2.3, self.state())):
                with patch('core.controller.time.monotonic', return_value=now):
                    status = walk.tick(state, True, 'explore')
            self.assertIn('jumping', status)
            self.assertIsNotNone(walk.recovery_jump_request)
            self.assertEqual(walk.recovery_attempts, 1)
            self.assertFalse(walk.navigator.failures)

    def test_ground_flag_flicker_and_vertical_motion_cannot_end_landing_early(self):
        with patch('core.controller.Keys'):
            walk = Walking()
            walk.set_mode('explore')
            walk.jump_feedback('jump', 'accepted')
            samples = ((0, self.state(in_air=True, standing=False)),
                       (1, self.state()), (1.2, self.state(standing=False)),
                       (2, self.state(vertical_speed=-.1)), (3, self.state()),
                       (3.7, self.state()), (6.5, self.state()))
            for now, state in samples:
                with patch('core.controller.time.monotonic', return_value=now):
                    self.assertIn('recover', walk.tick(state, True, 'explore'))
            self.assertEqual(walk.recoveries, 0)
            self.assertFalse(walk.navigator.failures)
            self.assertGreater(walk.jump_grace_until, 6.5)


if __name__ == '__main__':
    unittest.main()
