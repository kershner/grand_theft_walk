"""Persistent coverage and adaptive pedestrian/off-path exploration."""

import heapq
import json
import logging
import math
import sqlite3
from collections import Counter, deque
from contextlib import closing
from pathlib import Path

from core.files import read_json
from core.navigation import (
    PedestrianGraph,
    PedestrianNavigator,
    RECENT_TRAIL_POINTS,
    RecentTrail,
    angle_delta,
    closest_point_on_segment,
    distance,
    heading_to,
)


logger = logging.getLogger(__name__)
JOURNEY_RECONSIDER_DISTANCE = 1500.0
JOURNEY_STALL_SECONDS = 20.0
JOURNEY_PROGRESS_DISTANCE = 5.0
MEMORY_COUNTERS = ('coverage', 'failures', 'ground_visits', 'visits', 'edge_visits', 'edge_failures')


class ExploreNavigator(PedestrianNavigator):
    """Plan graph destinations and learn short off-path steps from actual movement.

    Off-path candidates are exploratory attempts, not collision-tested routes.
    Cells include height so a bridge does not cover the street beneath it.
    Mission routing continues to use the original pedestrian navigator.
    """

    def __init__(self, graph=None, memory_path=None, rng=None, *, open_terrain_chance=.65):
        super().__init__(graph if graph is not None else PedestrianGraph([]), rng)
        self.open_terrain_chance = open_terrain_chance
        self.exploring = False
        self.memory_path = Path(memory_path) if memory_path is not None else None
        for name in MEMORY_COUNTERS:
            setattr(self, name, MemoryCounter())
        self.store = ExplorationStore(self.memory_path.with_suffix('.sqlite3')) if self.memory_path else None
        self.recent_edges = deque(maxlen=64)
        self.recent_ground = RecentTrail()
        self.failed_approaches = deque(maxlen=32)
        self.obstacle_regions = deque(maxlen=32)
        self.route_abandoned = False
        self.observation_time = 0.0
        self.goal_steps = {}
        self.goal_edge = None
        self.goal = None
        self.progress_target = None
        self.best_remaining = math.inf
        self.progress_at = 0
        self.watchdogs_paused_until = float('-inf')
        self.journey_goal = None
        self.journey_remaining = math.inf
        self.journey_stalled = 0.0
        self.journey_observed_at = None
        self.local_target = None
        self.open_goal = None
        self.course = None
        self.escape_course = None
        self.detour_heading = None
        self.detour_origin = None
        self.local_origin = None
        self.breadcrumbs = []
        self.last_position = None
        self.last_cell = None
        self.travel = self.excursion_travel = 0.0
        self.excursion_anchor = None
        self.returning = False
        self.saved_at = float('-inf')
        self.dirty = False
        self.memory_seeded = False
        self.motion_history = deque(maxlen=160)
        self.loop_escapes = 0
        self.load_memory()

    def observe_motion(self, position, heading, now):
        """Catch circling even when terrain, turns or replanning pause goal checks."""
        history = self.motion_history
        if history and (now - history[-1][0] > 2 or math.dist(position, history[-1][1]) > 20):
            history.clear()
        if not history or now - history[-1][0] >= .25:
            history.append((now, position))
        while history and now - history[0][0] > 30:
            history.popleft()
        points = list(history)
        if len(points) < 2 or now - points[0][0] < 20:
            return False
        travelled = sum(distance(a[1], b[1]) for a, b in zip(points, points[1:]))
        displacement = distance(position, points[0][1])
        if travelled < 18 or displacement > 6:
            return False
        goal = (position[0] - math.sin(heading) * 10,
                position[1] + math.cos(heading) * 10, position[2])
        self.observe(position, now)
        self.avoid_target(now, retreat=False)
        self.abandon_journey(position, goal, now)
        self.commit_fresh_course(position, (self.course, self.course + math.pi), now)
        history.clear()
        self.loop_escapes += 1
        logger.info('Circling detected: %.1fm walked, %.1fm displacement; choosing a new course',
                    travelled, displacement)
        return True

    @staticmethod
    def coverage_cell(position):
        return (math.floor(position[0] / 12), math.floor(position[1] / 12),
                math.floor(position[2] / 6))

    def load_memory(self):
        if self.memory_path is None:
            return
        try:
            data = self.store.load()
            migrate = data is None
            if migrate:
                data = read_json(self.memory_path)
            if not isinstance(data, dict) or data.get('version') != 1:
                return
            counters = {}
            for name, width in (('coverage', 3), ('failures', 6), ('ground_visits', 6), ('visits', 2),
                                ('edge_visits', 4), ('edge_failures', 4)):
                entries = data.get(name, [])
                if not isinstance(entries, list):
                    raise ValueError('Invalid exploration memory size')
                counter = Counter()
                for entry in entries:
                    if (not isinstance(entry, list) or len(entry) != width + 1
                            or any(type(v) is not int for v in entry)
                            or not 0 < entry[-1] <= 1000000000):
                        raise ValueError('Invalid exploration memory entry')
                    key = tuple(entry[:-1])
                    if name.startswith('edge_'):
                        key = (key[:2], key[2:])
                        if any(k not in self.graph.nodes for k in key):
                            continue
                    elif name == 'visits' and key not in self.graph.nodes:
                        continue
                    counter[key] = entry[-1]
                counters[name] = counter
            recent = data.get('recent_edges', [])
            if (not isinstance(recent, list) or len(recent) > 64
                    or any(not isinstance(edge, list) or len(edge) != 4
                           or any(type(v) is not int for v in edge) for edge in recent)):
                raise ValueError('Invalid recent exploration route')
            ground = data.get('recent_ground', [])
            if (not isinstance(ground, list) or len(ground) > RECENT_TRAIL_POINTS
                    or any(not isinstance(point, list) or len(point) != 3
                           or any(type(v) not in (int, float) or not -1e7 <= v <= 1e7
                                  for v in point) for point in ground)):
                raise ValueError('Invalid recent exploration ground')
            for name, counter in counters.items():
                setattr(self, name, MemoryCounter(counter))
            self.recent_edges = deque(
                ((tuple(edge[:2]), tuple(edge[2:])) for edge in recent
                 if tuple(edge[:2]) in self.graph.nodes and tuple(edge[2:]) in self.graph.nodes),
                maxlen=64)
            self.recent_ground = RecentTrail(ground)
            if migrate:
                for name in MEMORY_COUNTERS:
                    counter = getattr(self, name)
                    counter.pending.update(counter)
                self.store.save({name: getattr(self, name) for name in MEMORY_COUNTERS},
                                self.recent_edges, self.recent_ground)
                logger.info('Imported exploration history into %s; original JSON retained', self.store.path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, sqlite3.Error) as error:
            logger.warning('Exploration memory unavailable: %s', error)

    def resume_after_free_walk(self, position, heading, now):
        """Find a route here, rather than retracing the start of a free stretch."""
        self.observe(position, now)
        travel = self.travel
        self.reset()
        self.travel = travel
        self.last_position, self.last_cell = position, self.coverage_cell(position)
        self.breadcrumbs = [position]
        self.course = heading
        self.join_forward_path(position, heading, now)

    def resume_after_jump(self, origin, position, heading, now):
        """Resume from actual landing ground, without retracing airborne travel."""
        moved = distance(origin, position)
        course = heading_to(origin, position) if moved > 2 else heading
        # Landing is observed ground; the airborne segment is not a safe
        # walking connection or a breadcrumb trail to follow backward.
        self.last_position = self.last_cell = None
        self.observe(position, now)
        self.travel += moved
        self.breadcrumbs = [position]
        self.excursion_anchor = position
        self.excursion_travel = 0.0
        self.returning = False
        self.target = self.previous = self.goal = self.goal_edge = None
        self.goal_steps = {}
        self.local_target = self.local_origin = None
        self.detour_heading = self.detour_origin = None
        self.course = course
        if (self.open_goal is not None and (distance(position, self.open_goal) < 15
                or abs(angle_delta(heading_to(position, self.open_goal), course)) > math.pi / 2)):
            self.open_goal = None
        self.progress_target = None
        self.best_remaining, self.progress_at = math.inf, now
        self.recovery_requested = False
        self.journey_goal = self.journey_observed_at = None
        self.journey_stalled = 0.0

    def save_memory(self, now, force=False):
        counters = {name: getattr(self, name) for name in MEMORY_COUNTERS}
        changed = (self.dirty or self.recent_ground.pending or self.recent_ground.cleared
                   or any(counter.pending for counter in counters.values()))
        if self.store is None or not changed or (not force and now - self.saved_at < 30):
            return
        self.saved_at = now
        try:
            self.store.save(counters, self.recent_edges, self.recent_ground)
            self.dirty = False
        except (OSError, sqlite3.Error) as error:
            logger.warning('Could not save exploration memory: %s', error)

    def reset(self):
        super().reset()
        self.goal = self.local_target = self.local_origin = None
        self.open_goal = None
        self.course = None
        self.escape_course = None
        self.detour_heading = None
        self.detour_origin = None
        self.excursion_anchor = None
        self.returning = False
        self.breadcrumbs = []
        self.last_position = self.last_cell = None
        self.progress_target = None
        self.best_remaining = math.inf
        self.travel = self.excursion_travel = 0.0
        self.journey_goal = None
        self.journey_stalled = 0.0
        self.journey_observed_at = None
        self.goal_steps = {}
        self.goal_edge = None
        self.next_departure_at = 0.0
        self.recovery_requested = False

    def observe(self, position, now):
        self.observation_time = now
        if self.last_position is not None:
            moved = distance(position, self.last_position)
            if moved > 50 or abs(position[2] - self.last_position[2]) > 15:
                # Respawn/teleport: do not invent a traversable connection.
                self.reset()
            elif moved <= 20:
                self.travel += moved
                self.excursion_travel += moved if self.excursion_anchor is not None else 0
        cell = self.coverage_cell(position)
        if cell != self.last_cell:
            if self.last_cell is not None:
                self.ground_visits[self.last_cell + cell] += 1
                self.ground_visits[cell + self.last_cell] += 1
            self.coverage[cell] += 1
            self.last_cell = cell
            self.dirty = True
        if not self.breadcrumbs or distance(position, self.breadcrumbs[-1]) >= 3:
            self.breadcrumbs.append(position)
            self.breadcrumbs = self.breadcrumbs[-100:]
            if self.excursion_anchor is not None and not self.returning:
                # Long successful excursions can outgrow the recovery window.
                # Return only over retained, observed ground after a failure.
                self.excursion_anchor = self.breadcrumbs[0]
        if not self.recent_ground or distance(position, self.recent_ground[-1]) >= 3:
            self.recent_ground.append(position)
            self.dirty = True
        self.last_position = position
        self.save_memory(now)

    def connection(self, origin, target):
        return self.coverage_cell(origin) + self.coverage_cell(target)

    def repeats_ground(self, origin, target):
        """Check the entire short approach, including intermediate cell borders."""
        if self.recent_ground.near(target):
            return True
        previous = self.coverage_cell(origin)
        steps = max(1, math.ceil(distance(origin, target) / 2))
        for step in range(1, steps + 1):
            point = tuple(a + (b - a) * step / steps for a, b in zip(origin, target))
            cell = self.coverage_cell(point)
            if cell != previous and self.ground_visits[previous + cell]:
                return True
            previous = cell
        return False

    def ground_usage(self, origin, target):
        """Count historic crossings along a proposed corridor, including old walks."""
        previous = self.coverage_cell(origin)
        known = crossings = frequency = 0
        steps = max(1, math.ceil(distance(origin, target) / 2))
        for step in range(1, steps + 1):
            point = tuple(a + (b - a) * step / steps for a, b in zip(origin, target))
            cell = self.coverage_cell(point)
            if cell != previous:
                count = self.ground_visits[previous + cell]
                crossings += 1
                known += bool(count)
                frequency += count
            previous = cell
        return known, crossings, frequency

    def try_open_departure(self, node, position, heading):
        """Commit to a long journey instead of rerolling the choice at each node."""
        if self.travel < self.next_departure_at:
            return None
        self.next_departure_at = self.travel + JOURNEY_RECONSIDER_DISTANCE
        if not self.open_terrain_chance or self.turn or self.rng.random() >= self.open_terrain_chance:
            return None
        paths = [heading_to(node.position, self.graph.nodes[key].position)
                 for key, _ in node.links if key in self.graph.nodes]
        if any(abs(angle_delta(path, heading)) <= math.pi / 4 for path in paths):
            return None  # Keep a usable path that already carries us forward.
        options = []
        for step in range(8):
            bearing = heading + step * math.tau / 8
            if any(abs(angle_delta(bearing, path)) < math.pi / 6 for path in paths):
                continue
            target = (position[0] - math.sin(bearing) * 10,
                      position[1] + math.cos(bearing) * 10, position[2])
            if self.failures[self.connection(position, target)] or self.repeats_ground(position, target):
                continue
            options.append((self.coverage[self.coverage_cell(target)],
                            abs(angle_delta(bearing, heading)), self.rng.random(), target))
        if not options:
            return None
        target = min(options)[3]
        bearing = heading_to(position, target)
        if self.previous is not None and self.target == node.key:
            self.recent_edges.append((self.previous, node.key))
        self.goal = self.target = self.previous = None
        self.goal_edge = None
        self.goal_steps = {}
        self.local_origin = self.excursion_anchor = position
        self.breadcrumbs = [position]
        self.excursion_travel = 0
        self.local_target = target
        self.open_goal = (position[0] - math.sin(bearing) * 100,
                          position[1] + math.cos(bearing) * 100, position[2])
        self.crossing = False
        self.dirty = True
        self.message = 'exploring fresh ground beyond pedestrian paths'
        return target

    def approach_blocked(self, position, target, now):
        # Keep a wall blocked across cell borders and small recovery movements.
        return any(now < expiry and distance(position, origin) < 35
                   and abs(position[2] - origin[2]) <= 12
                   and abs(angle_delta(heading_to(position, target), bearing)) < math.pi / 3
                   for origin, bearing, expiry in self.obstacle_regions) or any(
                   now < expiry and distance(position, origin) < 12
                   and abs(position[2] - origin[2]) <= 3
                   and abs(angle_delta(heading_to(position, target), bearing)) < math.pi / 3
                   for origin, bearing, expiry in self.failed_approaches)

    def avoid_target(self, now, *, retreat=True):
        if not self.exploring:
            return super().avoid_target(now)
        self.route_abandoned = False
        old_goal = self.open_goal
        # An actual failed approach permits a new travel decision after recovery.
        self.next_departure_at = 0.0
        self.detour_heading = self.detour_origin = None
        self.recovery_requested = False
        if self.returning and self.breadcrumbs and self.last_position is not None:
            self.failures[self.connection(self.last_position, self.breadcrumbs[-1])] += 1
            self.failed_approaches.append((self.last_position,
                                          heading_to(self.last_position, self.breadcrumbs[-1]), now + 120))
            # Observed ground can become blocked too. Try fresh approaches here.
            self.returning = False
            self.excursion_anchor = None
            self.local_target = self.open_goal = None
            self.breadcrumbs = [self.last_position]
            self.travel = 0
        elif self.local_target is not None and self.last_position is not None:
            origin = self.local_origin or self.last_position
            self.failures[self.connection(origin, self.local_target)] += 1
            self.failed_approaches.append((self.last_position,
                                          heading_to(self.last_position, self.local_target), now + 120))
            self.local_target = None
            # Retreat a few observed steps, rather than undoing a long walk
            # because its latest ten-metre approach hit an obstacle.
            self.excursion_anchor = self.last_position
            retreat_distance = 0.0
            for point in reversed(self.breadcrumbs):
                retreat_distance += distance(self.excursion_anchor, point)
                if retreat_distance > 12:
                    break
                self.excursion_anchor = point
            self.returning = retreat
            if not retreat:
                self.excursion_anchor = None
        elif self.previous is not None and self.target is not None:
            edge = self.previous, self.target
            self.edge_failures[edge] += 1
            count = self.edge_failures[edge]
            super().avoid_target(now)
            self.blocked[edge] = now + min(3600, 60 * 2 ** min(count, 5))
        else:
            super().avoid_target(now)
        self.goal = None
        self.goal_edge = None
        self.goal_steps = {}
        self.progress_target = None
        self.best_remaining = math.inf
        self.dirty = True
        if not retreat and old_goal is not None and self.last_position is not None:
            nearby_failures = sum(now < expiry and distance(self.last_position, origin) < 35
                                  and abs(self.last_position[2] - origin[2]) <= 12
                                  for origin, _, expiry in self.failed_approaches)
            if nearby_failures >= 2:
                self.abandon_journey(self.last_position, old_goal, now)

    def abandon_journey(self, position, goal, now):
        bearing = heading_to(position, goal)
        self.obstacle_regions.append((position, bearing, now + 300))
        self.local_target = self.local_origin = self.open_goal = None
        self.excursion_anchor = None
        self.returning = False
        self.target = self.previous = None
        self.goal = self.goal_edge = None
        self.goal_steps = {}
        self.course = bearing + self.rng.choice((-1, 1)) * math.pi / 2
        self.detour_heading = self.detour_origin = None
        self.progress_target = None
        self.recovery_requested = False
        self.journey_goal = None
        self.journey_observed_at = None
        self.route_abandoned = True

    def commit_fresh_course(self, position, headings, now):
        """Pick less-used ground at a course change, then keep that decision."""
        def score(heading):
            target = (position[0] - math.sin(heading) * 36,
                      position[1] + math.cos(heading) * 36, position[2])
            known, crossings, frequency = self.ground_usage(position, target)
            coverage = sum(self.coverage[self.coverage_cell(tuple(
                a + (b - a) * step / 3 for a, b in zip(position, target)))]
                for step in (1, 2, 3))
            return (self.approach_blocked(position, target, now),
                    self.repeats_ground(position, target), known / max(1, crossings),
                    frequency / max(1, crossings), coverage)

        self.course = min(headings, key=score)
        self.target = self.previous = self.goal = self.goal_edge = None
        self.goal_steps = {}
        self.local_origin = position
        self.local_target = (position[0] - math.sin(self.course) * 10,
                             position[1] + math.cos(self.course) * 10, position[2])
        self.open_goal = (position[0] - math.sin(self.course) * 100,
                          position[1] + math.cos(self.course) * 100, position[2])

    def choose_goal(self, start, now, heading=None):
        """Continue ahead first, then seek unused connections along that route."""
        ahead = set()
        if heading is not None:
            node = self.graph.nodes[start]
            ahead = {key for key, _ in node.links
                     if key in self.graph.nodes
                     and self.blocked.get((start, key), 0) <= now
                     and self.avoided.get(key, 0) <= now
                     and not self.approach_blocked(node.position, self.graph.nodes[key].position, now)
                     and abs(angle_delta(heading_to(node.position, self.graph.nodes[key].position),
                                         heading)) <= math.pi / 2 + .01}
        costs = {start: (0.0, 0.0)}
        parents = {}
        queue = [(costs[start], start)]
        frontier = None
        traversed = {frozenset(edge) for edge in self.recent_edges}
        if self.previous is not None and self.target is not None:
            traversed.add(frozenset((self.previous, self.target)))
        while queue:
            cost, key = heapq.heappop(queue)
            if cost != costs[key]:
                continue
            if frontier is not None and cost >= frontier[:2]:
                break  # No remaining route can improve the best unused link.
            node = self.graph.nodes[key]
            for target, _ in node.links:
                if key == start and ahead and target not in ahead:
                    continue
                if (target not in self.graph.nodes or self.blocked.get((key, target), 0) > now
                        or self.avoided.get(target, 0) > now):
                    continue
                length = max(.01, distance(node.position, self.graph.nodes[target].position))
                used = bool(self.edge_visits[(key, target)] or self.edge_visits[(target, key)]
                            or frozenset((key, target)) in traversed)
                endpoint = self.graph.nodes[target].position
                if (not used and self.coverage[self.coverage_cell(node.position)]
                        and self.coverage[self.coverage_cell(endpoint)]):
                    known, crossings, _ = self.ground_usage(node.position, endpoint)
                    used = crossings > 0 and known == crossings
                if not used:
                    candidate_frontier = (cost[0], cost[1] + length
                                          + min(self.edge_failures[(key, target)], 20) * 50,
                                          self.visits[target], key, target)
                    if frontier is None or candidate_frontier < frontier:
                        frontier = candidate_frontier
                # First minimize walking over old links, then the total route
                # length. Long fresh routes beat short familiar loops.
                candidate = (cost[0] + (length if used else 0),
                             cost[1] + length + min(self.edge_failures[(key, target)], 20) * 50)
                if candidate < costs.get(target, (math.inf, math.inf)):
                    costs[target] = candidate
                    parents[target] = key
                    heapq.heappush(queue, (candidate, target))
        self.goal_steps = {}
        self.goal = self.goal_edge = None
        if frontier is None:
            return  # Try new terrain instead of circulating in a completed area.
        _, _, _, origin, target = frontier
        self.goal, self.goal_edge = target, (origin, target)
        self.goal_steps[origin] = target
        key = origin
        while key in parents:
            parent = parents[key]
            self.goal_steps[parent] = key
            key = parent

    def link_options(self, node):
        options = super().link_options(node)
        options = [option for option in options if not self.approach_blocked(
            node.position, option[0].position, self.observation_time)]
        if not self.exploring:
            return options
        if self.previous is not None and self.previous != node.key:
            self.recent_edges.append((self.previous, node.key))
            self.dirty = True
        if self.turn:
            # A viewer's junction choice takes precedence over the planned route.
            self.goal = None
            return options
        next_key = self.goal_steps.get(node.key)
        return [option for option in options if option[0].key == next_key]

    def join_forward_path(self, position, direction, now):
        node = self.graph.nearest(position, self.avoided, radius=8)
        if node is None or abs(position[2] - node.position[2]) > 3 or self.turn:
            return None
        options = []
        for key, crossing in node.links:
            target = self.graph.nodes.get(key)
            if (target is None or self.blocked.get((node.key, key), 0) > now
                    or self.avoided.get(key, 0) > now or distance(position, target.position) <= 2
                    or abs(position[2] - target.position[2]) > 3):
                continue
            error = abs(angle_delta(heading_to(position, target.position), direction))
            if error <= math.pi / 4 and not self.approach_blocked(position, target.position, now):
                options.append((error, distance(position, target.position), key, crossing))
        if not options:
            return None
        _, _, key, self.crossing = min(options)
        self.previous, self.target = node.key, key
        self.goal = self.goal_edge = None
        self.goal_steps = {}
        self.local_target = self.excursion_anchor = None
        self.open_goal = None
        self.returning = False
        self.detour_heading = self.detour_origin = None
        self.message = 'exploring: joining a pedestrian path ahead'
        return self.graph.nodes[key].position

    def escape_waypoint(self, position, heading, now):
        """Keep searching when learned avoidance rejects every local corridor.

        Failure memory ranks these attempts instead of vetoing all movement.
        Previously traversed ground is preferable to another blind probe.
        """
        candidates = []
        for point in reversed(self.recent_ground):
            if 3 <= distance(position, point) <= 12 and abs(position[2] - point[2]) <= 3:
                candidates.append((point, True))
        for step in range(16):
            bearing = heading + step * math.tau / 16
            candidates.append(((position[0] - math.sin(bearing) * 6,
                                position[1] + math.cos(bearing) * 6, position[2]), False))

        def score(candidate):
            target, observed = candidate
            bearing = heading_to(position, target)
            recent_failures = sum(now < expiry and distance(position, origin) < radius
                                  and abs(position[2] - origin[2]) <= height
                                  and abs(angle_delta(bearing, failed)) < math.pi / 3
                                  for regions, radius, height in ((self.failed_approaches, 12, 3),
                                                                  (self.obstacle_regions, 35, 12))
                                  for origin, failed, expiry in regions)
            commitment = (abs(angle_delta(bearing, self.escape_course))
                          if self.escape_course is not None else 0)
            return (recent_failures, round(commitment, 6), not observed,
                    self.failures[self.connection(position, target)],
                    abs(angle_delta(bearing, heading)))

        target = min(candidates, key=score)[0]
        self.escape_course = heading_to(position, target)
        return target

    def off_path(self, position, heading, now):
        self.crossing = False
        if self.local_target is not None:
            horizontal = distance(position, self.local_origin)
            rise = abs(position[2] - self.local_origin[2])
            if (now > self.watchdogs_paused_until and rise > 2.5
                    and rise > horizontal * .65):
                self.recovery_requested = True
                self.message = 'exploring: steep approach; trying obstacle recovery'
                return None
            elif (distance(position, self.local_target) <= 2
                  or (abs(position[2] - self.local_target[2]) <= 3
                      and distance(position, self.local_origin) >= distance(self.local_target, self.local_origin)
                      and distance(self.local_target, closest_point_on_segment(
                          self.local_target, self.local_origin, position)) <= 2)):
                # A jump can carry us past a short waypoint between observations.
                # Consume it instead of turning back toward the obstacle.
                self.local_target = None
        if self.returning:
            while self.breadcrumbs and distance(position, self.breadcrumbs[-1]) <= 2:
                self.breadcrumbs.pop()
            if self.breadcrumbs:
                self.message = 'exploring: backtracking over observed ground'
                return self.breadcrumbs[-1]
            self.returning = False
        if self.local_target is None:
            goal_finished = self.open_goal is not None and distance(position, self.open_goal) < 15
            direction = (heading_to(position, self.open_goal) if self.open_goal is not None and not goal_finished
                         else self.course if self.course is not None else heading)
            if self.detour_origin is not None and distance(position, self.detour_origin) >= 16:
                self.detour_heading = self.detour_origin = None
            if self.detour_heading is not None:
                direction = self.detour_heading
            joined = self.join_forward_path(position, direction, now)
            if joined is not None:
                return joined
            if self.open_goal is None or goal_finished:
                self.commit_fresh_course(position, (direction, direction + math.pi / 4,
                                                   direction - math.pi / 4), now)
                direction = self.course
            options = []
            near_node = self.graph.nearest(position, radius=12)
            failed_bearings = []
            if near_node is not None:
                failed_bearings = [heading_to(near_node.position, self.graph.nodes[key].position)
                                   for key, _ in near_node.links
                                   if key in self.graph.nodes
                                   and self.edge_failures[(near_node.key, key)]]
            for step in range(16):
                bearing = direction + step * math.tau / 16
                if any(abs(angle_delta(bearing, failed)) < math.pi / 4 for failed in failed_bearings):
                    continue
                target = (position[0] - math.sin(bearing) * 10,
                          position[1] + math.cos(bearing) * 10, position[2])
                if self.approach_blocked(position, target, now):
                    continue
                failures = self.failures[self.connection(position, target)]
                if failures:
                    continue
                cell = self.coverage_cell(target)
                # Coverage breaks ties between useful approaches; it should not
                # pull a successful walk sideways or backward at cell borders.
                novelty = min(self.coverage[cell], 3)
                nearby = sum(min(self.coverage[(cell[0] + x, cell[1] + y, cell[2])], 3)
                             for x, y in ((-1, 0), (1, 0), (0, -1), (0, 1)))
                bias = (abs(angle_delta(bearing, heading)) * .5
                        + abs(angle_delta(bearing, heading_to(position, self.open_goal))) * 3)
                # Do not immediately reuse the ground behind us when the open
                # goal changes or a short obstacle retreat finishes.
                retraces = self.repeats_ground(position, target)
                _, _, reuse = self.ground_usage(position, target)
                if self.turn:
                    desired = heading + {'left': math.pi / 2, 'right': -math.pi / 2,
                                         'forward': 0}[self.turn]
                    bias += abs(angle_delta(bearing, desired)) * 4
                # Direction comes before coverage. Old but usable ground is
                # preferable to a needless turn toward an unexplored cell.
                deviation = abs(angle_delta(bearing, direction))
                if self.turn:
                    deviation = abs(angle_delta(bearing, desired))
                options.append((round(deviation, 6), retraces, reuse + novelty * .5 + nearby * .05 + bias,
                                self.rng.random(), target))
            if not options:
                self.local_target = self.escape_waypoint(position, heading, now)
                self.open_goal = None
            else:
                self.local_target = min(options)[4]
                self.escape_course = None
            if self.detour_heading is None and abs(angle_delta(
                    heading_to(position, self.local_target), direction)) >= math.pi / 4 - .01:
                self.detour_heading = heading_to(position, self.local_target)
                self.detour_origin = position
            self.local_origin = position
            self.turn = None
        self.message = ('exploring: walking to find a route' if self.escape_course is not None
                        else 'exploring open ground')
        return self.local_target

    def waypoint(self, position, heading, interior, now):
        if self.course is None:
            self.course = (heading_to(self.recent_ground[-2], self.recent_ground[-1])
                           if len(self.recent_ground) >= 2 else heading)
        result = self._waypoint(position, heading, interior, now)
        if result is not None and not self.returning and distance(position, result) > 2:
            self.course = heading_to(position, result)
        if (not self.exploring or result is None or self.returning
                or now <= self.watchdogs_paused_until):
            return result
        remaining = distance(position, result)
        if result != self.progress_target:
            self.progress_target = result
            self.best_remaining, self.progress_at = remaining, now
        elif remaining < self.best_remaining - .5:
            self.best_remaining, self.progress_at = remaining, now
        elif now - self.progress_at > 10:
            # Moving in circles is also a failed approach, even without a stall.
            self.recovery_requested = True
            self.message = 'exploring: no progress toward waypoint; trying obstacle recovery'
            return None
        return result

    def journey_stalled_out(self, position, now):
        """Keep a journey budget across short waypoints, jumps and replanning."""
        if now <= self.watchdogs_paused_until:
            self.journey_observed_at = None
            return False
        goal = self.open_goal
        if goal is None or self.returning:
            self.journey_goal = None
            self.journey_observed_at = None
            return False
        remaining = distance(position, goal)
        if goal != self.journey_goal:
            self.journey_goal = goal
            self.journey_remaining = remaining
            self.journey_stalled = 0.0
        elif remaining <= self.journey_remaining - JOURNEY_PROGRESS_DISTANCE:
            self.journey_remaining = remaining
            self.journey_stalled = 0.0
        elif self.journey_observed_at is not None:
            elapsed = now - self.journey_observed_at
            # Pauses and loading gaps are not failed walking time.
            if 0 <= elapsed <= 1:
                self.journey_stalled += elapsed
        self.journey_observed_at = now
        if self.journey_stalled < JOURNEY_STALL_SECONDS:
            return False
        self.avoid_target(now, retreat=False)
        self.abandon_journey(position, goal, now)
        self.message = 'exploring: no journey progress; route abandoned; choosing a new course'
        return True

    def _waypoint(self, position, heading, interior, now):
        if not self.exploring:
            return super().waypoint(position, heading, interior, now)
        if interior:
            self.reset()
            self.message = 'exploration is outdoors; switch to free roam indoors'
            return None
        if not self.memory_seeded:
            for edge, count in self.edge_failures.items():
                self.blocked[edge] = now + min(3600, 60 * 2 ** min(count, 5))
            self.memory_seeded = True
        # Do not add recovery backtracking to the breadcrumb stack.
        crumbs = list(self.breadcrumbs) if self.returning else None
        self.observe(position, now)
        if self.journey_stalled_out(position, now):
            return None
        if crumbs is not None and self.returning:
            self.breadcrumbs = crumbs
        nearby = self.graph.nearest(position, self.avoided, radius=18)
        if nearby is None and self.target in self.graph.nodes and self.previous in self.graph.nodes:
            start = self.graph.nodes[self.previous].position
            end = self.graph.nodes[self.target].position
            point = closest_point_on_segment(position, start, end)
            if distance(position, point) <= 8 and abs(position[2] - point[2]) <= 3:
                nearby = self.graph.nodes[self.target]
        if self.excursion_anchor is not None:
            # Successful movement has no distance budget. Obstacles, steep
            # steps and the progress watchdog still trigger observed recovery.
            if self.returning and distance(position, self.excursion_anchor) <= 3:
                self.excursion_anchor = None
                self.returning = False
                self.local_target = None
                self.breadcrumbs = [position]
                self.travel = 0
            else:
                return self.off_path(position, heading, now)
        # A nearby sidewalk is not a reason to interrupt a successful open walk.
        if self.local_target is not None:
            return self.off_path(position, heading, now)
        if nearby is None:
            self.target = self.previous = self.goal = None
            return self.off_path(position, heading, now)
        if (self.target is None and distance(position, nearby.position) > 4
                and abs(angle_delta(heading_to(position, nearby.position), heading)) > math.pi / 2):
            # A nearby node behind us is not a reason to turn around to join it.
            joined = self.join_forward_path(position, heading, now)
            return joined if joined is not None else self.off_path(position, heading, now)
        node = self.graph.nodes.get(self.target, nearby)
        if self.target is None or distance(position, node.position) <= 2:
            departure = self.try_open_departure(node, position, heading)
            if departure is not None:
                return departure
        self.returning = False
        self.local_target = None
        reached_goal = (self.goal in self.graph.nodes
                        and (self.goal_edge is None or self.goal_edge == (self.previous, self.target))
                        and distance(position, self.graph.nodes[self.goal].position) <= 3
                        and abs(position[2] - self.graph.nodes[self.goal].position[2]) <= 3)
        if self.goal is None or self.goal not in self.graph.nodes or reached_goal:
            if reached_goal:
                self.visits[self.goal] += 1
            start = self.target if self.target in self.graph.nodes else nearby.key
            self.choose_goal(start, now, heading)
        # The committed edge sequence can include a cycle-closing connector
        # whose endpoint is a node already crossed on the way there.
        # Follow that sequence rather than letting mission routing stop early.
        self.destination = None
        result = super().waypoint(position, heading, interior, now)
        if result is None:
            self.goal = self.target = self.previous = None
            self.excursion_anchor = position
            self.excursion_travel = 0
            self.breadcrumbs = [position]
            return self.off_path(position, heading, now)
        else:
            self.message = ('exploring a pedestrian crossing' if self.crossing
                            else 'exploring pedestrian paths')
        self.dirty = True
        return result

    def report(self):
        result = super().report()
        result['exploration'] = {
            'goal': self.goal,
            'goal_position': self.graph.nodes[self.goal].position if self.goal in self.graph.nodes else self.open_goal,
            'waypoint': self.local_target,
            'visited_cells': len(self.coverage),
            'visited_nodes': sum(1 for count in self.visits.values() if count),
            'total_nodes': len(self.graph.nodes),
            'blocked_connections': len(self.failures) + len(self.edge_failures),
            'off_path': self.local_target is not None or self.returning,
            'returning': self.returning,
            'recent_route': self.breadcrumbs[-30:],
            'covered_cells': list(reversed(self.coverage))[:300],
            'failed_steps': list(reversed(self.failures))[:100],
            'failed_edges': [(self.graph.nodes[a].position, self.graph.nodes[b].position)
                             for a, b in list(reversed(self.edge_failures))[:100]],
        }
        return result


class MemoryCounter(Counter):
    """Track modified keys so a checkpoint does not rewrite the entire map."""

    def __init__(self, values=None):
        self.pending = set()
        super().__init__()
        self.update(values or {})
        self.pending.clear()

    def __setitem__(self, key, value):
        value = max(0, min(int(value), 1000000000))
        if value != self.get(key, 0):
            self.pending.add(key)
        super().__setitem__(key, value)

    def __delitem__(self, key):
        if key in self:
            self.pending.add(key)
        super().__delitem__(key)

    def update(self, values=None, **kwargs):
        if values is not None:
            values = values if hasattr(values, 'items') else Counter(values)
            for key, count in values.items():
                self[key] += count
        if kwargs:
            self.update(kwargs)

    def clear(self):
        self.pending.update(self)
        super().clear()


class ExplorationStore:
    def __init__(self, path):
        self.path = path

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.path, timeout=.1)

    def load(self):
        with closing(self.connect()) as db, db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise ValueError('Unsupported exploration database version')
            db.execute('CREATE TABLE IF NOT EXISTS counters ('
                       'name TEXT NOT NULL, key TEXT NOT NULL, value INTEGER NOT NULL, '
                       'PRIMARY KEY (name, key)) WITHOUT ROWID')
            db.execute('CREATE TABLE IF NOT EXISTS metadata ('
                       'name TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID')
            db.execute('CREATE TABLE IF NOT EXISTS trail ('
                       'id INTEGER PRIMARY KEY AUTOINCREMENT, x REAL, y REAL, z REAL)')
            db.execute('PRAGMA user_version = 1')
            metadata = dict(db.execute('SELECT name, value FROM metadata'))
            if 'version' not in metadata:
                return None
            result = {name: json.loads(value) for name, value in metadata.items()}
            if result['version'] != 1:
                raise sqlite3.DatabaseError('Unsupported exploration metadata version')
            if result.get('trail_version') not in (None, 1):
                raise sqlite3.DatabaseError('Unsupported exploration trail version')
            if result.get('trail_version') == 1:
                result['recent_ground'] = [list(row) for row in db.execute(
                    'SELECT x,y,z FROM (SELECT id,x,y,z FROM trail ORDER BY id DESC LIMIT ?) '
                    'ORDER BY id', (RECENT_TRAIL_POINTS,))]
            for name, key, count in db.execute('SELECT name, key, value FROM counters'):
                result.setdefault(name, []).append(json.loads(key) + [count])
            return result

    def save(self, counters, recent_edges, recent_ground):
        batches = {name: set(counter.pending) for name, counter in counters.items()}
        rows, removed = [], []
        for name, keys in batches.items():
            counter = counters[name]
            for key in keys:
                flat = key[0] + key[1] if name.startswith('edge_') else key
                encoded = json.dumps(flat, separators=(',', ':'))
                if counter[key] > 0:
                    rows.append((name, encoded, counter[key]))
                else:
                    removed.append((name, encoded))
        metadata = {'version': 1,
                    'recent_edges': [list(a + b) for a, b in recent_edges], 'trail_version': 1}
        pending = list(recent_ground.pending)
        # The rollback journal exists only for this transaction. No growing
        # append-only history or WAL is left behind between checkpoints.
        with closing(self.connect()) as db, db:
            if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise sqlite3.DatabaseError('Existing exploration database version left unchanged')
            version = db.execute("SELECT value FROM metadata WHERE name='version'").fetchone()
            if version is not None and version[0] != '1':
                raise sqlite3.DatabaseError('Existing exploration metadata version left unchanged')
            seeded = db.execute("SELECT value FROM metadata WHERE name='trail_version'").fetchone()
            if seeded is not None and seeded[0] != '1':
                raise sqlite3.DatabaseError('Unsupported exploration trail version')
            if seeded is None or recent_ground.cleared:
                db.execute('DELETE FROM trail')
                pending = list(recent_ground)
            db.executemany('INSERT INTO trail(x,y,z) VALUES (?,?,?)', pending)
            db.execute('DELETE FROM trail WHERE id <= '
                       '(SELECT COALESCE(MAX(id),0) FROM trail) - ?', (RECENT_TRAIL_POINTS,))
            db.execute("DELETE FROM metadata WHERE name='recent_ground'")
            db.executemany('INSERT INTO counters VALUES (?, ?, ?) '
                           'ON CONFLICT(name, key) DO UPDATE SET value=excluded.value', rows)
            db.executemany('DELETE FROM counters WHERE name=? AND key=?', removed)
            db.executemany('INSERT INTO metadata VALUES (?, ?) '
                           'ON CONFLICT(name) DO UPDATE SET value=excluded.value',
                           [(name, json.dumps(value, separators=(',', ':')))
                            for name, value in metadata.items()])
        # Keep pending updates after any failed/rolled-back transaction.
        for name, keys in batches.items():
            counters[name].pending.difference_update(keys)
        recent_ground.pending.clear()
        recent_ground.cleared = False
