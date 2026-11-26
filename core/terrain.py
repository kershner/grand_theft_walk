"""Bounded, asynchronous collision sensing and local walking routes.

Only the native game thread executes engine functions. Python publishes batches
and plans over collision-tested connections; unknown space is never a clear ray.
"""

import heapq
import logging
import math
import struct
from collections import OrderedDict
from pathlib import Path

from core.files import read_json, sha256
from core.navigation import angle_delta, closest_point_on_segment, heading_to
from core.winmemory import k32, module_export


logger = logging.getLogger(__name__)
HEADER = struct.Struct('<7I')
RAY = struct.Struct('<6fII6fI')
MAX_RAYS = 64
STEP = 2.0
RADIUS = 9  # 36 by 36 metres; both map size and search work are bounded.


class NativeTerrain:
    """Single-writer mailbox, short-lived dynamic rays, bounded static cache."""

    def __init__(self, memory):
        self.mem = memory
        self.address = None
        self.reason = 'terrain bridge unavailable; using movement feedback'
        self.pending = OrderedDict()
        self.cache = OrderedDict()
        self.flight = []
        self.sequence = 0
        self.sent_at = 0.0
        self.pumped_at = None
        self.checked = 0
        try:
            manifest = read_json(Path(__file__).parent / 'native/background.json')
            module = memory.module('GTW.BackgroundInput.asi') if hasattr(memory, 'module') else None
            rva = manifest.get('terrain_rva')
            if not module:
                return
            length = HEADER.size + MAX_RAYS * RAY.size
            if sha256(module['path']) == manifest['sha256']:
                if rva is None:
                    return
                if not 0 <= rva <= module['size'] - length:
                    raise ValueError('Invalid terrain mailbox bounds')
                address = module['base'] + rva
            else:
                address = module_export(memory, module, 'GTWTerrain', length)
            protocol, game, ready, request, complete, _, _ = memory.unpack(address, '7I')
            expected = {'san_andreas': 1, 'vice_city': 2, 'gta_iii': 3}[memory.profile.id]
            if (protocol, game, ready) != (1, expected, 1):
                raise ValueError('Terrain hook did not match this game')
            # Never adopt a still-running request from an earlier controller.
            if request != complete:
                self.reason = 'terrain mailbox busy; using movement feedback'
                return
            self.sequence = request
            self.address = address
            self.reason = 'collision sensing ready'
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
            self.reason = f'terrain sensing unavailable: {error}'
            logger.warning(self.reason)

    @property
    def enabled(self):
        return self.address is not None

    def ray(self, start, end, flags, now):
        key = (*tuple(round(v, 2) for v in start), *tuple(round(v, 2) for v in end), flags)
        entry = self.cache.get(key)
        if entry is not None and entry[0] > now:
            self.cache.move_to_end(key)
            return entry[1]
        if len(self.pending) < MAX_RAYS:
            self.pending[key] = (start, end, flags)
        return None

    def pump(self, now):
        if not self.enabled:
            return
        if self.flight and self.pumped_at is not None and now - self.pumped_at > 1:
            # A menu, stopped walker, or suspended controller is not a stalled
            # engine query. Give the game thread a fresh response window.
            self.sent_at = now
        self.pumped_at = now
        try:
            if self.flight:
                self.mem.write(self.address + 24, struct.pack('<I', k32.GetTickCount() & 0xFFFFFFFF))
                complete = self.mem.u32(self.address + 16)
                if complete != self.sequence:
                    if now - self.sent_at > 2:
                        # Do not overwrite an in-flight native batch or trust stale output.
                        self.address = None
                        self.reason = 'terrain bridge timed out; using movement feedback'
                    return
                data = self.mem.read(self.address + HEADER.size, len(self.flight) * RAY.size)
                if self.mem.u32(self.address + 16) != self.sequence:
                    raise ValueError('Terrain completion changed while reading')
                for key, values in zip(self.flight, RAY.iter_unpack(data)):
                    hit, point, normal, wet = values[7], values[8:11], values[11:14], values[14]
                    if hit not in (0, 1) or wet not in (0, 1) or not all(
                            math.isfinite(v) for v in (*point, *normal)):
                        raise ValueError('Invalid native collision result')
                    result = (point, normal, bool(wet)) if hit else False
                    lifetime = (1.5 if hit else .75) if key[-1] & 1 else (30 if hit else 3)
                    self.cache[key] = (now + lifetime, result)
                    self.pending.pop(key, None)
                    self.checked += 1
                self.flight = []
                while len(self.cache) > 8192:
                    self.cache.popitem(last=False)
            if self.pending:
                self.flight = list(self.pending)[:MAX_RAYS]
                data = b''.join(RAY.pack(*self.pending[key][0], *self.pending[key][1],
                                        self.pending[key][2], 0, *([0.] * 6), 0)
                                for key in self.flight)
                self.mem.write(self.address + HEADER.size, data)
                self.mem.write(self.address + 20, struct.pack('<2I', len(self.flight),
                               k32.GetTickCount() & 0xFFFFFFFF))
                self.sequence = (self.sequence + 1) & 0x7FFFFFFF or 1
                self.mem.write(self.address + 12, struct.pack('<I', self.sequence))
                self.sent_at = now
        except (OSError, ValueError) as error:
            self.address = None
            self.reason = f'terrain sensing failed: {error}; using movement feedback'
            logger.warning(self.reason)


class TerrainNavigator:
    """Direct surface routes, with bounded A* only when a corridor is blocked."""

    def __init__(self, sensor, *, can_swim=False):
        self.sensor, self.can_swim = sensor, can_swim
        self.in_water = False
        self.reset()

    def reset(self):
        self.anchor = None
        self.goal = None
        self.path = []
        self.feet_offset = None
        self.status = 'waiting for terrain samples'
        self.blocked = False
        self.temporary = False
        self.nodes = 0
        self.dynamic_blocks = {}
        self.validated = None
        self.refresh_at = 0.
        self.search_at = 0.
        self.plans = getattr(self, 'plans', 0)
        self.proposal = None
        self.direct = True
        self.refresh_probe = None
        self.last_position = None
        self.search_context = None
        self.search_samples = {}

    @property
    def enabled(self):
        return self.sensor.enabled

    def sample_ray(self, start, end, flags, now, samples=None):
        key = ('ray', start, end, flags)
        if samples is not None and key in samples:
            return samples[key]
        result = self.sensor.ray(start, end, flags, now)
        if samples is not None and result is not None:
            samples[key] = result
        return result

    def ground(self, x, y, reference, now, samples=None):
        reference = round(reference * 2) / 2
        hit = self.sample_ray((x, y, reference + 1.25), (x, y, reference - 2.0), 2, now, samples)
        if hit is None or hit is False:
            return hit
        point, normal, wet = hit
        if (wet and not self.can_swim) or (not wet and normal[2] < .82):
            return False
        return point

    def corridor(self, start, end, now, *, dynamic=False, samples=None):
        if len(self.dynamic_blocks) > 512:
            self.dynamic_blocks = {key: expiry for key, expiry in self.dynamic_blocks.items() if expiry > now}
        edge = tuple(sorted(tuple(round(v, 1) for v in point) for point in (start, end)))
        if self.dynamic_blocks.get(edge, 0) > now:
            return False
        length = math.dist(start[:2], end[:2])
        if length < .01:
            return True
        if abs(end[2] - start[2]) > max(.45, length * .65):
            return False
        pending = False
        # Test support midway as well as at endpoints: a clear ray across a hole
        # does not make that hole walkable. Stay near this surface, not a roof.
        steps = max(2, math.ceil(length))
        for index in range(1, steps):
            middle = tuple(a + (b - a) * index / steps for a, b in zip(start, end))
            support = self.ground(*middle, now, samples=samples)
            if support is None:
                pending = True
            elif support is False or abs(support[2] - middle[2]) > .6:
                return False
        dx, dy = (end[0] - start[0]) / length, (end[1] - start[1]) / length
        for side in (-.35, 0, .35):
            for height in (.45, 1.05, 1.75):
                a = (start[0] - dy * side, start[1] + dx * side, start[2] + height)
                b = (end[0] - dy * side, end[1] + dx * side, end[2] + height)
                hit = self.sample_ray(a, b, int(dynamic), now, samples)
                if hit is None:
                    pending = True
                elif hit is not False:
                    if dynamic:
                        self.dynamic_blocks[edge] = now + 3
                    return False
        return None if pending else True

    def search(self, start, target, now):
        """Unknown edges request samples and defer; never become traversable."""
        if (self.search_context is None or self.search_context[1] != target
                or math.dist(start, self.search_context[0]) > .5):
            self.search_context = (start, target)
            self.search_samples = {}
        # A bounded search spans asynchronous batches. Retain completed static
        # samples for this attempt so early clear rays cannot expire faster
        # than the frontier advances. Actual walking still checks dynamic rays.
        start = self.search_context[0]

        def sample(kind, *points):
            key = (kind, *points)
            if key not in self.search_samples:
                result = (self.ground(*points, now) if kind == 'ground'
                          else self.corridor(*points, now, samples=self.search_samples))
                if result is not None:
                    self.search_samples[key] = result
                return result
            return self.search_samples[key]

        origin = (0, 0, round(start[2], 1))
        positions = {origin: start}
        costs, parents = {origin: 0.}, {}
        queue = [(math.dist(start[:2], target[:2]), 0., origin)]
        waiting = False
        best = origin
        self.nodes = 0
        while queue and self.nodes < 500:
            _, cost, key = heapq.heappop(queue)
            if cost != costs[key]:
                continue
            point = positions[key]
            self.nodes += 1
            if math.dist(point[:2], target[:2]) < 1.5:
                path = [point]
                while key in parents:
                    key = parents[key]
                    path.append(positions[key])
                return list(reversed(path))[1:]
            if math.dist(point[:2], target[:2]) < math.dist(positions[best][:2], target[:2]):
                best = key
            for ox, oy in ((0, 1), (1, 0), (0, -1), (-1, 0),
                           (1, 1), (-1, 1), (1, -1), (-1, -1)):
                gx, gy = key[0] + ox, key[1] + oy
                if abs(gx) > RADIUS or abs(gy) > RADIUS:
                    continue
                ground = sample('ground', start[0] + gx * STEP, start[1] + gy * STEP, point[2])
                if ground is None:
                    waiting = True
                    continue
                if ground is False:
                    continue
                neighbour = (gx, gy, round(ground[2], 1))
                candidate = cost + math.dist(point, ground)
                if candidate >= costs.get(neighbour, math.inf):
                    continue
                clear = sample('corridor', point, ground)
                if clear is None:
                    waiting = True
                    continue
                if not clear:
                    continue
                costs[neighbour], parents[neighbour], positions[neighbour] = candidate, key, ground
                heapq.heappush(queue, (candidate + math.dist(ground[:2], target[:2]), candidate, neighbour))
        if waiting:
            return None
        return []

    def direct_segment(self, feet, target, now):
        """Commit a long clear course; A* is only needed when this is blocked."""
        if self.proposal is None:
            length = math.dist(feet[:2], target[:2])
            scale = min(1., 8 / max(length, .01))
            end = (feet[0] + (target[0] - feet[0]) * scale,
                   feet[1] + (target[1] - feet[1]) * scale, feet[2])
            self.proposal = (feet, end)
        origin, end = self.proposal
        ground = self.ground(*end, now)
        clear = ground if ground is None or ground is False else self.corridor(origin, ground, now, dynamic=True)
        if clear is True:
            self.path = [ground]
            self.validated = (origin, ground, now + 1.5)
            self.refresh_at, self.refresh_probe = now + .6, None
            self.proposal = None
        elif clear is False:
            self.proposal = None
            self.direct = False
        return clear

    def route(self, position, target, now, *, in_water=False):
        if self.in_water != in_water:
            self.reset()
            self.in_water = in_water
        self.sensor.pump(now)
        if not self.enabled:
            return target
        self.blocked = False
        self.temporary = False
        teleported = (self.last_position is not None and
                      (math.dist(position[:2], self.last_position[:2]) > 20
                       or abs(position[2] - self.last_position[2]) > 8))
        self.last_position = position
        # A new action, teleport, surface change, or destination starts a new map.
        if self.anchor is None or teleported:
            self.anchor, self.goal, self.path = position, target, []
            self.feet_offset = None
            self.validated = None
            self.search_at = 0.
            self.proposal, self.refresh_probe, self.direct = None, None, True
        elif self.goal != target:
            # Graph waypoints change routinely. Ground calibration belongs to
            # the current surface, not to each individual pedestrian node.
            forward = (self.path and abs(angle_delta(heading_to(position, target),
                       heading_to(position, self.path[-1]))) < .4)
            self.goal = target
            if not forward:
                self.path, self.validated = [], None
                self.proposal, self.refresh_probe = None, None
            self.direct = True
            self.search_at = 0.
        if self.feet_offset is None:
            ground = self.ground(*position, now)
            if ground is None:
                self.sensor.pump(now)
                self.status = 'sampling ground beneath walker'
                return None
            if ground is False:
                self.blocked = True
                self.status = 'no walkable ground beneath walker'
                return None
            self.feet_offset = position[2] - ground[2]
        feet = (position[0], position[1], position[2] - self.feet_offset)
        while self.path and math.dist(feet[:2], self.path[0][:2]) < .7:
            self.path.pop(0)
        extending = (self.direct and len(self.path) <= 1 and (not self.path or
                     (math.dist(feet[:2], self.path[0][:2]) < 4 and
                      math.dist(feet[:2], target[:2]) > math.dist(feet[:2], self.path[0][:2]) + 2)))
        if self.proposal is not None or extending:
            clear = self.direct_segment(feet, target, now)
            if clear is None and not self.path:
                self.status = 'checking the next continuous walking corridor'
                self.sensor.pump(now)
                return None
        if self.path:
            previous = self.validated
            nearest = (closest_point_on_segment(feet, previous[0], previous[1])
                       if previous else feet)
            lateral = math.dist(feet[:2], nearest[:2])
            covered = (previous and previous[1] == self.path[0] and now < previous[2]
                       # Small steering arcs should not pulse forward off/on
                       # while a fresh corridor query is still in flight.
                       and lateral < .5
                       and abs(feet[2] - nearest[2]) < .6)
            refreshed = not covered or now >= self.refresh_at or lateral >= .25
            if refreshed:
                if self.refresh_probe is None or self.refresh_probe[1] != self.path[0]:
                    self.refresh_probe = (feet, self.path[0])
                clear = self.corridor(*self.refresh_probe, now, dynamic=True)
            else:
                clear = True
            if clear is None and covered:
                # The old corridor is still valid while its replacement is
                # checked. Query latency must not release forward every cycle.
                self.sensor.pump(now)
                point = previous[1]
                self.status = 'following collision-tested terrain route'
                return (point[0], point[1], point[2] + self.feet_offset)
            if clear:
                if refreshed:
                    self.validated = (*self.refresh_probe, now + 1.5)
                    self.refresh_probe = None
                    self.refresh_at = now + .6
                self.sensor.pump(now)
                point = self.path[0]
                self.status = 'following collision-tested terrain route'
                return (point[0], point[1], point[2] + self.feet_offset)
            if clear is False:
                self.path = []
                self.validated = None
                self.refresh_probe = None
                self.direct = False
                self.proposal = None
            elif clear is None:
                self.status = 'refreshing terrain clearance'
        if not self.path:
            if now < self.search_at:
                self.status = 'checking terrain and obstacle clearance'
                return None
            self.search_at = now + .1
            self.plans += 1
            # Keep distant pedestrian destinations in reach of a bounded local map.
            length = math.dist(feet[:2], target[:2])
            scale = min(1., 14 / max(length, .01))
            local = (feet[0] + (target[0] - feet[0]) * scale,
                     feet[1] + (target[1] - feet[1]) * scale, feet[2])
            self.path = self.search(feet, local, now)
            if self.path == []:
                self.blocked = True
                self.temporary = any(expiry > now for expiry in self.dynamic_blocks.values())
                self.status = 'no safe local route; choosing another approach'
            elif self.path:
                # The search caches static geometry for longer. The next actual
                # step must also pass a fresh, short-lived vehicle check.
                self.status = 'checking moving obstacles along terrain route'
            else:
                self.path = []
                self.status = 'checking terrain and obstacle clearance'
        if self.path:
            self.corridor(feet, self.path[0], now, dynamic=True)
        self.sensor.pump(now)
        return None

    def report(self):
        return {'enabled': self.enabled, 'status': self.status if self.enabled else self.sensor.reason,
                'checked_rays': self.sensor.checked, 'search_nodes': self.nodes,
                'route_searches': self.plans,
                'route': self.path, 'water_checks': self.sensor.mem.profile.id != 'gta_iii'}


class WaterAvoidance:
    """A few shoreline probes, independent of route finding or ground normals."""

    def __init__(self, sensor):
        self.sensor = sensor
        self.areas = []
        self.probe = None
        self.turn_target = None
        try:
            path = Path(sensor.mem.exe).parent / 'data/water.dat'
            for line in path.read_text(encoding='ascii').splitlines():
                fields = line.split(';')[0].strip().replace(',', ' ').split()
                if not fields or fields[0] in ('*', 'processed'):
                    continue
                values = tuple(map(float, fields))
                if len(values) == 5:  # III/VC: level, rectangle bounds.
                    z, x1, y1, x2, y2 = values
                    points = ((x1, y1), (x2, y1), (x2, y2), (x1, y2))
                elif len(values) in (22, 29):  # SA: three/four seven-value vertices.
                    vertices = [values[i:i + 3] for i in range(0, len(values) - 1, 7)]
                    z = max(p[2] for p in vertices)
                    cx = sum(p[0] for p in vertices) / len(vertices)
                    cy = sum(p[1] for p in vertices) / len(vertices)
                    points = tuple((p[0], p[1]) for p in sorted(vertices,
                        key=lambda p: math.atan2(p[1] - cy, p[0] - cx)))
                else:
                    continue
                if not all(math.isfinite(v) for p in points for v in p) or not math.isfinite(z):
                    raise ValueError('Invalid water boundary')
                self.areas.append((z, points))
        except (OSError, ValueError, TypeError) as error:
            self.areas = []
            logger.warning('Water boundaries unavailable: %s', error)

    def level(self, x, y):
        levels = []
        for z, polygon in self.areas:
            inside = False
            for a, b in zip(polygon, polygon[1:] + polygon[:1]):
                if (a[1] > y) != (b[1] > y) and x < (
                        (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]):
                    inside = not inside
            if inside:
                levels.append(z)
        return max(levels) if levels else None

    def danger(self, position, heading, now):
        if (self.probe is None or math.dist(position, self.probe[0]) > .75
                or abs(angle_delta(heading, self.probe[1])) > .25):
            self.probe = (position, heading)
        origin, bearing = self.probe
        pending = False
        for length in (2., 4., 6., 8., 10.):
            for side in (-.4, 0., .4):
                x = origin[0] - math.sin(bearing) * length + math.cos(bearing) * side
                y = origin[1] + math.cos(bearing) * length + math.sin(bearing) * side
                level = self.level(x, y)
                if level is None:
                    continue
                # The map extends under dry ground and bridges. Require actual
                # support above the surface, including beside the centre line.
                if not self.sensor.enabled:
                    pending = True
                    continue
                hit = self.sensor.ray((x, y, origin[2] + 1), (x, y, origin[2] - 14), 2, now)
                if hit is None:
                    pending = True
                elif hit is False or hit[2] or hit[0][2] <= level + .35:
                    return True
        return None if pending else False

    def tick(self, walk, state, now):
        """Check ahead without pausing; turn in place only for confirmed danger."""
        if (not self.areas or state.get('interior', 0) or state.get('in_water')
                or state.get('in_air') or not state.get('standing', True)):
            return None
        self.sensor.pump(now)
        position = tuple(state['position'][axis] for axis in ('x', 'y', 'z'))
        facing = state.get('camera_heading')
        facing = state['heading'] if facing is None else facing
        if self.turn_target is not None:
            error = angle_delta(self.turn_target, facing)
            if abs(error) > .2:
                walk.suspend_progress(state['position'], now)
                walk.keys.set(walk.walk_modifier)
                walk.keys.turn(walk.smooth_turn.step(-error * 140 * walk.turn_multiplier, now))
                return 'exploring: turning away from water'
            self.turn_target = None
            self.probe = None
            walk.smooth_turn.reset()
        danger = self.danger(position, facing, now)
        self.sensor.pump(now)
        if danger is not True:
            return None
        walk.suspend_progress(state['position'], now)
        walk.keys.set(walk.walk_modifier)
        walk.navigator.observe(position, now)
        walk.navigator.avoid_target(now, retreat=False)
        walk.navigator.failed_approaches.append((position, facing, now + 120))
        walk.navigator.commit_fresh_course(position, (facing + math.pi / 2, facing - math.pi / 2), now)
        walk.recovery_target = None
        walk.recovery_jump_setup = walk.recovery_jump_request = None
        walk.recovery_jump_started = walk.recovery_jump_id = None
        walk.jump_takeoff_until = walk.jump_grace_until = 0.
        walk.free_walk_until = 0
        target = walk.navigator.local_target
        self.turn_target = heading_to(position, target) if target is not None else facing + math.pi
        if walk.terrain:
            walk.terrain.reset()
        walk.keys.set(walk.walk_modifier)
        return 'exploring: choosing less-used ground away from water'
