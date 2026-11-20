"""Bounded, asynchronous collision sensing and local walking routes.

Only the native game thread executes engine functions. Python publishes batches
and plans over collision-tested connections; unknown space is never a clear ray.
"""

import heapq
import logging
import math
import struct
from collections import OrderedDict, deque
from pathlib import Path

from core.files import read_json, sha256
from core.navigation import angle_delta, closest_point_on_segment, heading_to
from core.winmemory import k32


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
            if not module or rva is None:
                return
            if sha256(module['path']) != manifest['sha256']:
                self.reason = 'terrain bridge update pending; using movement feedback'
                return
            if not 0 <= rva <= module['size'] - HEADER.size - MAX_RAYS * RAY.size:
                raise ValueError('Invalid terrain mailbox bounds')
            address = module['base'] + rva
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
        except (OSError, ValueError, TypeError, KeyError) as error:
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
    """Lazy A* on height-specific ground samples, with body-width edge tests."""

    def __init__(self, sensor):
        self.sensor = sensor
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

    @property
    def enabled(self):
        return self.sensor.enabled

    def ground(self, x, y, reference, now):
        reference = round(reference * 2) / 2
        hit = self.sensor.ray((x, y, reference + 1.25), (x, y, reference - 2.0), 2, now)
        if hit is None or hit is False:
            return hit
        point, normal, wet = hit
        if wet or normal[2] < .82:  # Approximately 35 degrees maximum.
            return False
        return point

    def corridor(self, start, end, now, *, dynamic=False):
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
        samples = max(2, math.ceil(length))
        for index in range(1, samples):
            middle = tuple(a + (b - a) * index / samples for a, b in zip(start, end))
            support = self.ground(*middle, now)
            if support is None:
                pending = True
            elif support is False or abs(support[2] - middle[2]) > .6:
                return False
        dx, dy = (end[0] - start[0]) / length, (end[1] - start[1]) / length
        for side in (-.35, 0, .35):
            for height in (.45, 1.05, 1.75):
                a = (start[0] - dy * side, start[1] + dx * side, start[2] + height)
                b = (end[0] - dy * side, end[1] + dx * side, end[2] + height)
                hit = self.sensor.ray(a, b, int(dynamic), now)
                if hit is None:
                    pending = True
                elif hit is not False:
                    if dynamic:
                        self.dynamic_blocks[edge] = now + 3
                    return False
        return None if pending else True

    def search(self, start, target, now):
        """Unknown edges request samples and defer; never become traversable."""
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
                ground = self.ground(start[0] + gx * STEP, start[1] + gy * STEP, point[2], now)
                if ground is None:
                    waiting = True
                    continue
                if ground is False:
                    continue
                neighbour = (gx, gy, round(ground[2], 1))
                candidate = cost + math.dist(point, ground)
                if candidate >= costs.get(neighbour, math.inf):
                    continue
                clear = self.corridor(point, ground, now)
                if clear is None:
                    waiting = True
                    continue
                if not clear:
                    continue
                costs[neighbour], parents[neighbour], positions[neighbour] = candidate, key, ground
                heapq.heappush(queue, (candidate + math.dist(ground[:2], target[:2]), candidate, neighbour))
        if waiting or queue:
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

    def route(self, position, target, now):
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
            covered = (previous and previous[1] == self.path[0] and now < previous[2]
                       and math.dist(feet, closest_point_on_segment(feet, previous[0], previous[1])) < .25)
            refreshed = not covered or now >= self.refresh_at
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
        if not self.path:
            if now < self.search_at:
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


class SwimNavigator:
    """SA-only swim budget, remembered dry ground, and committed shore recovery."""

    def __init__(self, sensor, graph, limit=120.):
        self.sensor, self.graph, self.limit = sensor, graph, limit
        self.dry_ground = deque(maxlen=64)
        self.elapsed = 0.
        self.observed_at = None
        self.active = False
        self.dry_since = None
        self.entry_heading = 0.
        self.goal = None
        self.candidates = []
        self.scan_origin = None
        self.best_remaining = math.inf
        self.progress_at = 0.
        self.rejected = {}
        self.status = 'on land'
        self.surface = None
        self.swim_anchor = None
        self.swim_step = None
        self.swim_clear_until = 0.
        self.mapped_land = []

    def observe(self, state, now, usable):
        if not usable:
            self.observed_at = None
            return False
        point = tuple(state['position'][axis] for axis in ('x', 'y', 'z'))
        wet = state.get('in_water', False)
        if wet:
            if not self.active:
                self.entry_heading = state['heading']
                self.elapsed, self.goal, self.candidates = 0., None, []
                self.active = True
                self.scan_origin = None
                self.rejected.clear()
                self.surface = round(point[2] + 1.)
                self.swim_anchor = self.swim_step = None
                self.swim_clear_until = 0.
            if self.observed_at is not None:
                delta = now - self.observed_at
                self.elapsed += delta if 0 <= delta <= 1 else 0
            self.dry_since = None
        elif self.active:
            grounded = (state.get('standing', False) and not state.get('in_air', False)
                        and abs(state.get('vertical_speed', 0)) < .03)
            if not grounded:
                self.dry_since = None
            elif self.dry_since is None:
                self.dry_since = now
            elif now - self.dry_since >= 1.0:
                self.active = False
                self.elapsed, self.goal = 0., None
                self.status = 'on land'
        if not self.active and state.get('standing', False) and not state.get('in_air', False):
            if not self.dry_ground or math.dist(point, self.dry_ground[-1]) > 4:
                self.dry_ground.append(point)
        self.observed_at = now
        return self.active

    def shore_goal(self, position, now):
        if self.goal is not None:
            remaining = math.dist(position[:2], self.goal[:2])
            if remaining < self.best_remaining - 2:
                self.best_remaining, self.progress_at = remaining, now
            if remaining > 2 and now - self.progress_at < 20:
                return self.goal
            # Dry contact, not arrival at an XY coordinate, finishes swimming.
            self.rejected[self.goal] = now + 30
            self.goal = None
        if (self.scan_origin is None or math.dist(position[:2], self.scan_origin[:2]) > 12
                or not self.candidates):
            self.scan_origin = position
            candidates = list(reversed(self.dry_ground))
            if self.graph:
                # A nearby cliff-top path is a poor shore target. Prefer a low
                # approach, but do not require distant geometry to be streamed.
                candidates += [node.position for node in sorted(self.graph.nodes.values(),
                                key=lambda node: math.dist(position[:2], node.position[:2])
                                + max(0., node.position[2] - position[2]) * 8)[:12]]
            self.mapped_land = sorted(set(candidates), key=lambda p:
                                     math.dist(position[:2], p[:2]) + max(0., p[2] - position[2]) * 8)
            coarse = next((p for p in self.mapped_land if self.rejected.get(p, 0) <= now), None)
            if coarse is not None and math.dist(position[:2], coarse[:2]) > 64:
                self.goal = coarse
                self.best_remaining = math.dist(position[:2], coarse[:2])
                self.progress_at = now
                return coarse
            for radius in (16, 32, 64):
                for step in range(12):
                    bearing = step * math.tau / 12
                    candidates.append((position[0] + math.cos(bearing) * radius,
                                       position[1] + math.sin(bearing) * radius, position[2]))
            self.candidates = sorted(set(candidates), key=lambda p: math.dist(position[:2], p[:2]))
        for candidate in self.candidates:
            if self.rejected.get(candidate, 0) > now:
                continue
            if self.sensor.enabled:
                surface = self.surface if self.surface is not None else round(position[2] + 1)
                hit = self.sensor.ray((candidate[0], candidate[1], surface + 6),
                                      (candidate[0], candidate[1], surface - 2.5), 2, now)
                if hit is None or hit is False:
                    continue
                point, normal, wet = hit
                if wet or normal[2] < .82 or point[2] > position[2] + 2:
                    continue
                candidate = point
            # Without native sensing, use observed land or the pedestrian graph;
            # arbitrary radial probes have no evidence of being land.
            elif candidate not in self.dry_ground and not (self.graph and any(
                    candidate == node.position for node in self.graph.nodes.values())):
                continue
            self.goal = candidate
            self.best_remaining = math.dist(position[:2], candidate[:2])
            self.progress_at = now
            return candidate
        return None

    def waypoint(self, position, now):
        if self.elapsed < self.limit:
            self.status = 'swimming; shore recovery starts after two minutes'
            return None
        self.sensor.pump(now)
        goal = self.shore_goal(position, now)
        if goal is None:
            # With no known coast yet, reverse the entry course while looking.
            goal = (position[0] + math.sin(self.entry_heading) * 30,
                    position[1] - math.cos(self.entry_heading) * 30, position[2])
        direction = math.atan2(goal[1] - position[1], goal[0] - position[0])
        if (self.swim_anchor is None
                or math.dist(position[:2], self.swim_anchor[:2]) > 3):
            surface = self.surface if self.surface is not None else round(position[2] + 1)
            self.swim_anchor = (position[0], position[1], surface)
            self.swim_step = None
        if self.swim_step is not None and now < self.swim_clear_until:
            self.status = 'swimming toward land'
            return self.swim_step
        origin = self.swim_anchor
        direction = math.atan2(goal[1] - origin[1], goal[0] - origin[0])
        for offset in (0, .4, -.4, .8, -.8, 1.4, -1.4, 2.2, -2.2, math.pi):
            angle = direction + offset
            # Fixed segment endpoints let async results survive water bobbing
            # and turning; checking a new floating-point ray every frame starves.
            if offset == 0 and self.swim_step is not None:
                target = self.swim_step
                angle = math.atan2(target[1] - origin[1], target[0] - origin[0])
            else:
                target = (origin[0] + math.cos(angle) * 5,
                          origin[1] + math.sin(angle) * 5, origin[2])
            if self.sensor.enabled:
                clear = True
                for side in (-.35, .35):
                    start = (origin[0] - math.sin(angle) * side,
                             origin[1] + math.cos(angle) * side, origin[2] + .4)
                    end = (target[0] - math.sin(angle) * side,
                           target[1] + math.cos(angle) * side, target[2] + .4)
                    if self.sensor.ray(start, end, 1, now) is not False:
                        clear = False
                if not clear:
                    continue
            self.status = 'swimming toward land'
            self.swim_step, self.swim_clear_until = target, now + .6
            self.sensor.pump(now)
            return target
        self.sensor.pump(now)
        self.status = 'checking a clear swimming route to land'
        return False

    def report(self):
        return {'active': self.active, 'seconds': round(self.elapsed, 1),
                'return_after': self.limit, 'returning': self.active and self.elapsed >= self.limit,
                'shore_target': self.goal, 'status': self.status}
