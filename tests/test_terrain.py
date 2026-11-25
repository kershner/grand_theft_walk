"""Collision-based routing, async bridge ownership, and SA shore recovery."""

import math
import struct
import tempfile
import unittest
from collections import OrderedDict
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

from core.controller import Walking
from core.exploration import ExploreNavigator
from core.navigation import Node, PedestrianGraph
from core.terrain import HEADER, MAX_RAYS, RAY, NativeTerrain, SwimNavigator, TerrainNavigator, WaterAvoidance
from tests.fixtures import bridge_exports


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

    def test_walking_height_bob_does_not_cancel_a_still_valid_ground_corridor(self):
        world = World()
        nav = TerrainNavigator(world)
        self.assertIsNotNone(self.route(nav, (0, 0, 1), (0, 30, 1)))
        world.pending = True
        for frame in range(20):
            target = nav.route((0, .1 + frame * .01, 1.3), (0, 30, 1), .7 + frame / 60)
            self.assertIsNotNone(target)
        self.assertEqual(nav.plans, 0)
        self.assertIsNone(nav.route((0, .4, 2), (0, 30, 1), 1.1))

    def test_small_steering_arc_keeps_moving_while_refreshing_but_stops_for_a_confirmed_hit(self):
        world = World()
        nav = TerrainNavigator(world)
        self.route(nav, (0, 0, 1), (0, 30, 1))
        count = len(world.calls)
        world.pending = True
        self.assertIsNotNone(nav.route((.35, .1, 1), (0, 30, 1), .2))
        self.assertGreater(len(world.calls), count)  # Drift requests a fresh check immediately.
        self.assertIsNotNone(nav.route((.36, .15, 1), (0, 30, 1), .25))
        world.pending = False
        world.walls.append((-1, .8, 1, 1.2, 3))
        target = nav.route((.36, .15, 1), (0, 30, 1), .3)
        self.assertTrue(target is None or abs(target[0]) >= 1)

    def test_drift_outside_validated_corridor_still_pauses_for_clearance(self):
        world = World()
        nav = TerrainNavigator(world)
        self.route(nav, (0, 0, 1), (0, 30, 1))
        world.pending = True
        self.assertIsNone(nav.route((.6, .1, 1), (0, 30, 1), .2))
        self.assertIsNone(nav.route((.1, .2, 1), (0, 30, 1), 2))

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
            position = (.35 * math.sin(walked / .9), walked, 1 + .2 * math.sin(walked / .14))
            target = nav.route(position, (0, 100, 1), frame / 60)
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

    def test_search_corridor_retains_partial_replies_across_cache_expiration(self):
        class ExpiringWorld(World):
            def __init__(self):
                super().__init__()
                self.queue, self.results = OrderedDict(), {}

            def ray(self, start, end, flags, now):
                key = (start, end, flags)
                if key in self.results and self.results[key][0] > now:
                    return self.results[key][1]
                self.queue[key] = None
                return None

            def pump(self, now):
                if self.queue:
                    key, _ = self.queue.popitem(last=False)
                    self.results[key] = (now + .06, super().ray(*key, now))

        world = ExpiringWorld()
        nav, samples = TerrainNavigator(world), {}
        for frame in range(100):
            now = frame * .05
            world.pump(now)
            result = nav.corridor((0, 0, 0), (0, 2, 0), now, samples=samples)
            if result is not None:
                break
        self.assertIs(result, True)
        self.assertLess(frame, 30)
        world.walls.append((-1, .8, 1, 1.2, 3))
        # Search history must never bypass fresh checks before actual movement.
        for frame in range(100, 150):
            now = frame * .05
            world.pump(now)
            result = nav.corridor((0, 0, 0), (0, 2, 0), now, dynamic=True)
            if result is False:
                break
        self.assertIs(result, False)

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
            for frame in range(10):
                now = frame / 20
                with patch('core.controller.time.monotonic', return_value=now):
                    self.assertIn('sampling', walk.tick(state, True, 'explore'))
            self.assertEqual(walk.recoveries, 0)
            self.assertFalse(walk.navigator.failures)
            walk.keys.set.assert_called_with({'walk'})

    def test_endless_terrain_wait_retries_without_inventing_an_obstacle(self):
        with patch('core.controller.Keys'):
            walk = Walking()
            walk.set_mode('explore')
            walk.jump_supported = True
            walk.navigator.waypoint = Mock(return_value=(0, 10, 0))
            walk.terrain = TerrainNavigator(World())
            walk.terrain.sensor.pending = True
            state = dict(ready=True, foreground=True, paused=False, controls_disabled=False,
                         in_vehicle=False, health=100, player_state=0, ped_state=1,
                         interior=0, heading=0, standing=True, in_air=False,
                         position=dict(x=0, y=0, z=1))
            with patch('core.controller.time.monotonic', return_value=0):
                walk.tick(state, True, 'explore')
            with patch('core.controller.time.monotonic', return_value=1.01):
                self.assertIn('walking freely', walk.tick(state, True, 'explore'))
            self.assertEqual(walk.recoveries, 0)
            self.assertFalse(walk.navigator.failures)
            self.assertIsNone(walk.terrain_wait_since)
            self.assertEqual(walk.jump_budget_used, 0)
            walk.keys.set.assert_called_with({'walk', 'forward'})

    def test_repeated_terrain_timeouts_do_not_spend_jump_budget(self):
        with patch('core.controller.Keys'):
            walk = Walking()
            walk.set_mode('explore')
            walk.jump_supported = True
            walk.navigator.waypoint = Mock(return_value=(0, 10, 0))
            walk.terrain = TerrainNavigator(World())
            walk.terrain.sensor.pending = True
            state = dict(ready=True, foreground=True, paused=False, controls_disabled=False,
                         in_vehicle=False, health=100, player_state=0, ped_state=1,
                         interior=0, heading=0, standing=True, in_air=False,
                         position=dict(x=0, y=0, z=1))
            jumps = 0
            for attempt in range(6):
                state['position']['y'] = attempt * 10
                # Keep making physical progress while sensor replies are pending.
                walk.recovery_jump_setup = None
                with patch('core.controller.time.monotonic', return_value=attempt * 16):
                    walk.tick(state, True, 'explore')
                state['position']['y'] += 9
                with patch('core.controller.time.monotonic', return_value=attempt * 16 + 15):
                    result = walk.tick(state, True, 'explore')
                jumps += 'backing up for jump' in result
            self.assertEqual(walk.recoveries, 0)
            self.assertEqual(jumps, 0)
            self.assertEqual(len(walk.jump_sites), 0)
            self.assertFalse(walk.navigator.failures)

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

    def test_steering_decelerates_during_a_route_wait_instead_of_lurching_on_resume(self):
        with patch('core.controller.Keys'):
            walk = Walking()
            walk.set_mode('explore')
            target = (-10, 10, 0)
            walk.navigator.waypoint = Mock(return_value=target)
            walk.terrain = SimpleNamespace(enabled=True, blocked=False, status='sampling',
                route=Mock(return_value=target), reset=Mock())
            state = dict(ready=True, foreground=True, paused=False, controls_disabled=False,
                in_vehicle=False, health=100, player_state=0, ped_state=1, standing=True,
                in_air=False, interior=0, heading=0, position=dict(x=0, y=0, z=0))
            for frame in range(120):
                with patch('core.controller.time.monotonic', return_value=frame / 120):
                    walk.tick(state, True, 'explore')
            self.assertGreater(abs(walk.smooth_turn.velocity), 70)
            walk.terrain.route.return_value = None
            calls = walk.keys.turn.call_count
            for frame in range(120, 180):
                with patch('core.controller.time.monotonic', return_value=frame / 120):
                    walk.tick(state, True, 'explore')
            self.assertLess(abs(walk.smooth_turn.velocity), 22)
            self.assertEqual(walk.keys.turn.call_count, calls)
            walk.terrain.route.return_value = target
            with patch('core.controller.time.monotonic', return_value=1.5):
                walk.tick(state, True, 'explore')
            self.assertLessEqual(abs(walk.keys.turn.call_args.args[0]), 1)

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
                if temporary:
                    self.assertFalse(walk.navigator.failures)
                    walk.keys.set.assert_called_with({'walk', 'forward'})
                    self.assertEqual(walk.free_walk_until, 10)
                else:
                    self.assertTrue(walk.navigator.failures)
                    walk.keys.set.assert_called_with({'walk'})
                    self.assertEqual(walk.free_walk_until, 0)

    def test_async_twelve_ray_frame_budget_eventually_finds_u_fence_exit(self):
        class BufferedWorld(World):
            def __init__(self):
                super().__init__(walls=[(-3, -2, -2.8, 6, 3), (2.8, -2, 3, 6, 3),
                                       (-3, 5.8, 3, 6, 3)])
                self.results, self.queue = {}, OrderedDict()
                self.last_pump = None

            def ray(self, start, end, flags, now):
                key = (start, end, flags)
                if key in self.results and self.results[key][0] <= now:
                    del self.results[key]
                if key not in self.results and len(self.queue) < MAX_RAYS:
                    self.queue[key] = None
                return self.results[key][1] if key in self.results else None

            def pump(self, now):
                if self.last_pump is not None and now - self.last_pump < .1:
                    return
                self.last_pump = now
                for key in list(self.queue)[:12]:
                    # Match the live sensor's expiring collision cache. Earlier
                    # tests retained every ray forever and hid search starvation.
                    result = super().ray(*key, now)
                    ttl = (1.5 if result else .75) if key[-1] & 1 else (30 if result else 3)
                    self.results[key] = (now + ttl, result)
                    del self.queue[key]

        nav = TerrainNavigator(BufferedWorld())
        result = None
        for frame in range(1200):
            result = nav.route((0, 0, 1), (0, 10, 1), frame / 60)
            if result is not None:
                break
        self.assertIsNotNone(result)
        self.assertLess(result[1], 0)
        self.assertLess(frame, 1200)

    def test_free_stretch_keeps_walking_and_recovers_only_if_physically_stuck(self):
        for stuck in (False, True):
            with self.subTest(stuck=stuck), patch('core.controller.Keys'):
                walk = Walking()
                walk.set_mode('explore')
                walk.jump_supported = True
                walk.navigator.waypoint = Mock(return_value=(0, 20, 0))
                walk.terrain = SimpleNamespace(enabled=True, blocked=True,
                    route=Mock(return_value=None), reset=Mock(), status='blocked')
                state = dict(ready=True, foreground=True, paused=False, controls_disabled=False,
                    in_vehicle=False, health=100, player_state=0, ped_state=1, standing=True,
                    in_air=False, interior=0, heading=0, position=dict(x=0, y=0, z=0))
                for now in (0, 1, 2):
                    state['position']['y'] = now
                    with patch('core.controller.time.monotonic', return_value=now):
                        self.assertIn('walking freely', walk.tick(state, True, 'explore'))
                    walk.keys.set.assert_called_with({'walk', 'forward'})
                walk.terrain.route.assert_called_once()
                if stuck:
                    with patch('core.controller.time.monotonic', return_value=4.1):
                        self.assertIn('backing up for jump', walk.tick(state, True, 'explore'))
                    self.assertEqual(walk.recoveries, 1)
                else:
                    state['position']['y'] = 11
                    with patch('core.controller.time.monotonic', return_value=10.1):
                        self.assertIn('walking freely', walk.tick(state, True, 'explore'))
                    self.assertEqual(walk.terrain.route.call_count, 2)
                    self.assertEqual(walk.recoveries, 0)

    def test_sensor_fallback_keeps_steering_and_retries_without_resetting_the_plan(self):
        with patch('core.controller.Keys'):
            walk = Walking()
            walk.set_mode('explore')
            target = (0, 20, 0)
            walk.terrain = SimpleNamespace(enabled=True, reset=Mock(), status='tested route',
                route=Mock(return_value=target))
            walk.navigator.join_forward_path = Mock(return_value=target)
            walk.navigator.waypoint = Mock(return_value=target)
            state = dict(ready=True, foreground=True, paused=False, controls_disabled=False,
                in_vehicle=False, health=100, player_state=0, ped_state=1, standing=True,
                in_air=False, interior=0, heading=0, position=dict(x=0, y=0, z=0))
            walk.begin_free_walk(state, 0)
            for now in (.5, 1, 1.25):
                state['position']['y'] = now
                with patch('core.controller.time.monotonic', return_value=now):
                    self.assertIn('walking freely', walk.tick(state, True, 'explore'))
                walk.keys.set.assert_called_with({'walk', 'forward'})
            walk.terrain.route.assert_not_called()
            self.assertEqual(walk.navigator.waypoint.call_count, 3)
            state['position']['y'] = 1.5
            with patch('core.controller.time.monotonic', return_value=10.1):
                self.assertEqual(walk.tick(state, True, 'explore'), 'tested route')
            self.assertEqual(walk.free_walk_until, 0)
            self.assertEqual(walk.navigator.waypoint.call_count, 4)
            self.assertEqual(walk.recoveries, 0)


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
    def test_rebuilt_bridge_uses_relocated_terrain_export(self):
        mem = Memory()
        bridge_exports(mem, 0, {"GTWTerrain": 0x1000})
        mem.write(0x1000, HEADER.pack(1, 1, 1, 0, 0, 0, 0))
        with patch('core.terrain.read_json', return_value={'terrain_rva': 64, 'sha256': 'pinned'}), \
                patch('core.terrain.sha256', return_value='rebuilt'):
            sensor = NativeTerrain(mem)
        self.assertTrue(sensor.enabled)
        self.assertEqual(sensor.address, 0x1000)

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


class WaterAvoidanceTests(unittest.TestCase):
    def test_missed_probe_above_mapped_water_does_not_reverse_on_dry_land(self):
        guard = self.guard(World(lambda x, y: None), '6, -100, -100, 100, 100')
        self.assertIs(guard.danger((0, 0, 11.65), 0, 0), False)
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

    def test_pending_shoreline_refresh_keeps_recent_checked_approach_moving(self):
        world = World(lambda x, y: 2)
        guard = self.guard(world)
        self.assertIs(guard.danger((0, 15, 3), 0, 0), False)
        world.pending = True
        self.assertIs(guard.danger((0, 15.8, 3), 0, .8), False)
        # Pending results cannot extend their own distance or time allowance.
        self.assertIsNone(guard.danger((0, 16.1, 3), 0, .9))
        self.assertIsNone(guard.danger((0, 15.8, 3), 0, 1.5))
        self.assertIsNone(guard.danger((0, 15.8, 3), .3, 1))

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


class RecoveryTimingTests(unittest.TestCase):
    def test_blocked_corridor_turns_and_moves_even_when_sensor_keeps_rejecting_routes(self):
        from games.gta_iii import game as iii
        from games.san_andreas import game as sa
        from games.vice_city import game as vc

        for backend in (iii, sa, vc):
            for jumping in (True, False):
                with self.subTest(game=backend.__name__, jumping=jumping), patch('core.controller.Keys'):
                    multiplier = getattr(backend, 'TURN_MULTIPLIER', 1)
                    walk = Walking(turn_multiplier=multiplier)
                    walk.set_mode('explore')
                    walk.jump_supported = jumping
                    walk.terrain = SimpleNamespace(enabled=True, blocked=True, temporary=False,
                        route=Mock(return_value=None), reset=Mock(), status='blocked')
                    state = self.state()
                    with patch('core.controller.time.monotonic', return_value=0):
                        status = walk.tick(state, True, 'explore')
                    if jumping:
                        self.assertIn('backing up for jump', status)
                        walk.keys.set.assert_called_with({'walk', 'back'})
                        for now in (1.6, 2.):
                            with patch('core.controller.time.monotonic', return_value=now):
                                walk.tick(state, True, 'explore')
                        walk.jump_feedback(walk.recovery_jump_id, 'failed')
                        with patch('core.controller.time.monotonic', return_value=2.1):
                            status = walk.tick(state, True, 'explore')
                    self.assertIn('steering around obstacle', status)
                    self.assertIsNotNone(walk.recovery_target)
                    self.assertFalse(walk.navigator.returning)

                    def rotate(amount):
                        state['heading'] -= math.radians(amount * .4 / multiplier)

                    walk.keys.turn.side_effect = rotate
                    for frame in range(1, 601):
                        with patch('core.controller.time.monotonic', return_value=2.1 + frame / 120):
                            walk.tick(state, True, 'explore')
                        if 'forward' in walk.keys.set.call_args.args[0]:
                            state['position']['x'] -= math.sin(state['heading']) / 60
                            state['position']['y'] += math.cos(state['heading']) / 60
                        if math.hypot(state['position']['x'], state['position']['y']) >= 1:
                            break
                    self.assertLess(frame, 600)
                    walk.keys.turn.assert_called()
                    self.assertEqual(walk.terrain.route.call_count, 1)
                    self.assertLessEqual(walk.recoveries, 2)
                    landing = walk.recovery_target
                    state['position'] = dict(zip(('x', 'y', 'z'), landing))
                    walk.terrain.route.side_effect = lambda position, target, now: target
                    with patch('core.controller.time.monotonic', return_value=8):
                        walk.tick(state, True, 'explore')
                    self.assertIsNone(walk.recovery_target)
                    self.assertEqual(walk.navigator.local_origin, landing)

    def test_circling_survives_planner_resets_and_paused_waypoint_checks(self):
        navigator = ExploreNavigator()
        navigator.exploring = True
        for frame in range(121):
            now = frame / 4
            angle = now * math.tau / 20
            position = (4 * math.sin(angle), 4 * math.cos(angle), 0)
            navigator.reset()  # Terrain/free-walk/shoreline transitions.
            navigator.watchdogs_paused_until = now + 1
            if navigator.observe_motion(position, angle, now):
                break
        self.assertEqual(navigator.loop_escapes, 1)
        self.assertTrue(navigator.route_abandoned)
        self.assertFalse(navigator.returning)
        self.assertTrue(navigator.obstacle_regions)

    def test_motion_watchdog_allows_forward_progress_turning_pauses_and_teleports(self):
        for kind in ('straight', 'stationary', 'pause', 'teleport'):
            with self.subTest(kind=kind):
                navigator = ExploreNavigator()
                for frame in range(161):
                    now = frame / 4
                    position = (0, now if kind == 'straight' else 0, 0)
                    if kind == 'pause' and frame >= 80:
                        now += 100
                    if kind == 'teleport' and frame >= 80:
                        position = (1000, 1000, 0)
                    self.assertFalse(navigator.observe_motion(position, 0, now))
                self.assertEqual(navigator.loop_escapes, 0)

    def test_graph_continues_forward_before_seeking_unused_ground_behind(self):
        graph = PedestrianGraph([
            Node((0, i), (0, y, 0), tuple(((0, j), False) for j in (i - 1, i + 1)
                 if 0 <= j < 4)) for i, y in enumerate((-20, 0, 20, 40))])
        navigator = ExploreNavigator(graph, open_terrain_chance=0)
        navigator.exploring = True
        navigator.edge_visits[((0, 1), (0, 2))] = 5
        self.assertEqual(navigator.waypoint((0, 0, 0), 0, 0, 0), (0, 20, 0))

    def test_joining_a_sidewalk_does_not_chase_the_nearest_node_behind(self):
        graph = PedestrianGraph([
            Node((0, 0), (0, -5, 0), (((0, 1), False),)),
            Node((0, 1), (0, 15, 0), (((0, 0), False),))])
        navigator = ExploreNavigator(graph, open_terrain_chance=0)
        navigator.exploring = True
        self.assertEqual(navigator.waypoint((0, 0, 0), 0, 0, 0), (0, 15, 0))

    def state(self, **updates):
        state = dict(ready=True, foreground=True, paused=False, controls_disabled=False,
                     in_vehicle=False, health=100, player_state=0, ped_state=1,
                     interior=0, heading=0, standing=True, in_air=False, vertical_speed=0,
                     position=dict(x=0, y=0, z=0))
        state.update(updates)
        return state

    def test_exploration_keeps_using_collision_tested_paths_when_aligned(self):
        with patch('core.controller.Keys'):
            walk = Walking()
            walk.jump_supported = True
            walk.terrain = SimpleNamespace(enabled=True,
                route=Mock(side_effect=lambda position, target, now: target), reset=Mock(), status='safe route')
            walk.navigator.waypoint = Mock(side_effect=lambda position, *args:
                                           (position[0], position[1] + 20, position[2]))
            for now in range(121):
                state = self.state(position=dict(x=0, y=now, z=0))
                with patch('core.controller.time.monotonic', return_value=now):
                    result = walk.tick(state, True, 'explore')
                    self.assertIn('safe route', result)
                walk.keys.set.assert_called_with({'walk', 'forward'})
            self.assertEqual(walk.navigator.waypoint.call_count, 121)
            self.assertEqual(walk.terrain.route.call_count, 121)
            walk.keys.turn.assert_not_called()
            self.assertEqual(walk.recoveries, 0)
            with patch('core.controller.time.monotonic', return_value=122.1):
                self.assertIn('backing up for jump', walk.tick(state, True, 'explore'))
            self.assertEqual(walk.recoveries, 1)

    def test_turn_requests_resume_collision_tested_path_following(self):
        with patch('core.controller.Keys'):
            walk = Walking()
            walk.set_mode('explore')
            state = self.state()
            walk.navigator.waypoint = Mock(return_value=(-10, 0, 0))
            self.assertTrue(walk.steer('left', 'explore', state))
            with patch('core.controller.time.monotonic', return_value=0):
                self.assertIn('aligning', walk.tick(state, True, 'explore'))
            walk.navigator.waypoint.assert_called_once()
            state['heading'] = math.pi / 2
            with patch('core.controller.time.monotonic', return_value=1):
                walk.tick(state, True, 'explore')
            calls = walk.navigator.waypoint.call_count
            state['position']['x'] = -1
            with patch('core.controller.time.monotonic', return_value=2):
                walk.tick(state, True, 'explore')
            self.assertEqual(walk.navigator.waypoint.call_count, calls + 1)

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

    def test_long_jump_resumes_at_landing_for_sidewalk_and_open_ground(self):
        for graph in (None, PedestrianGraph([
                Node((0, i), (0, y, 1), tuple(((0, j), False) for j in (i - 1, i + 1)
                     if 0 <= j < 4)) for i, y in enumerate((0, 10, 20, 80))])):
            with self.subTest(graph=graph is not None), patch('core.controller.Keys'):
                walk = Walking(graph)
                walk.set_mode('explore')
                walk.navigator.open_terrain_chance = 0
                walk.terrain = TerrainNavigator(World())
                for now, state in (
                        (0, self.state(position=dict(x=0, y=0, z=1))),
                        (1, self.state(position=dict(x=3, y=15, z=8), in_air=True, standing=False)),
                        (2, self.state(position=dict(x=8, y=65, z=1))),
                        (2.7, self.state(position=dict(x=8, y=65, z=1))),
                        (6, self.state(position=dict(x=8, y=68, z=1)))):
                    with patch('core.controller.time.monotonic', return_value=now):
                        walk.tick(state, True, 'explore')
                self.assertGreater(walk.last_waypoint[1], 68)
                self.assertGreater(walk.terrain.path[0][1], 68)
                self.assertGreaterEqual(walk.navigator.travel, 68)
                self.assertFalse(walk.navigator.returning)
                self.assertEqual(walk.recoveries, 0)
                self.assertFalse(walk.navigator.failures)
                self.assertIsNone(walk.jump_route_origin)

    def test_slow_alignment_does_not_become_an_invisible_obstacle(self):
        with patch('core.controller.Keys'):
            walk = Walking()
            walk.set_mode('explore')
            walk.navigator.course = 0
            for frame in range(240):
                with patch('core.controller.time.monotonic', return_value=frame / 20):
                    walk.tick(self.state(heading=math.pi), True, 'explore')
            self.assertTrue(walk.aligning)
            self.assertEqual(walk.recoveries, 0)
            self.assertFalse(walk.navigator.failures)


if __name__ == '__main__':
    unittest.main()
