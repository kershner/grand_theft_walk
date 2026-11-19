"""Persistent coverage and adaptive pedestrian/off-path exploration."""

import heapq
import logging
import math
from collections import Counter
from pathlib import Path

from core.files import atomic_json, read_json
from core.navigation import PedestrianGraph, PedestrianNavigator, angle_delta, closest_point_on_segment, distance, heading_to

logger = logging.getLogger(__name__)


class ExploreNavigator(PedestrianNavigator):
    """Plan graph destinations and learn short off-path steps from actual movement.

    Off-path candidates are exploratory attempts, not collision-tested routes.
    Cells include height so a bridge does not cover the street beneath it.
    Mission routing continues to use the original pedestrian navigator.
    """

    def __init__(self, graph=None, memory_path=None, rng=None):
        super().__init__(graph if graph is not None else PedestrianGraph([]), rng)
        self.exploring = False
        self.memory_path = Path(memory_path) if memory_path is not None else None
        self.coverage = Counter()
        self.failures = Counter()
        self.edge_failures = Counter()
        self.goal = None
        self.goal_at = 0
        self.progress_target = None
        self.best_remaining = math.inf
        self.progress_at = 0
        self.local_target = None
        self.open_goal = None
        self.open_goal_at = 0
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
        self.load_memory()

    @staticmethod
    def coverage_cell(position):
        return (math.floor(position[0] / 12), math.floor(position[1] / 12),
                math.floor(position[2] / 6))

    def load_memory(self):
        if self.memory_path is None:
            return
        try:
            data = read_json(self.memory_path)
            if not isinstance(data, dict) or data.get('version') != 1:
                return
            counters = {}
            for name, width in (('coverage', 3), ('failures', 6), ('visits', 2),
                                ('edge_visits', 4), ('edge_failures', 4)):
                entries = data.get(name, [])
                if not isinstance(entries, list) or len(entries) > 500000:
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
            for name, counter in counters.items():
                setattr(self, name, counter)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as error:
            logger.warning('Exploration memory unavailable: %s', error)

    def save_memory(self, now, force=False):
        if self.memory_path is None or not self.dirty or (not force and now - self.saved_at < 30):
            return
        data = {'version': 1}
        for name in ('coverage', 'failures', 'visits', 'edge_visits', 'edge_failures'):
            counter = getattr(self, name)
            data[name] = [list(key[0] + key[1] if name.startswith('edge_') else key) + [count]
                          for key, count in counter.items()]
        self.saved_at = now
        try:
            atomic_json(self.memory_path, data)
            self.dirty = False
        except OSError as error:
            logger.warning('Could not save exploration memory: %s', error)

    def reset(self):
        super().reset()
        self.goal = self.local_target = self.local_origin = None
        self.open_goal = None
        self.excursion_anchor = None
        self.returning = False
        self.breadcrumbs = []
        self.last_position = self.last_cell = None
        self.progress_target = None
        self.best_remaining = math.inf
        self.travel = self.excursion_travel = 0.0

    def observe(self, position, now):
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
            self.coverage[cell] += 1
            self.last_cell = cell
            self.dirty = True
        if not self.breadcrumbs or distance(position, self.breadcrumbs[-1]) >= 3:
            self.breadcrumbs.append(position)
            self.breadcrumbs = self.breadcrumbs[-100:]
        self.last_position = position
        self.save_memory(now)

    def connection(self, origin, target):
        return self.coverage_cell(origin) + self.coverage_cell(target)

    def avoid_target(self, now):
        if not self.exploring:
            return super().avoid_target(now)
        if self.returning and self.breadcrumbs and self.last_position is not None:
            self.failures[self.connection(self.last_position, self.breadcrumbs[-1])] += 1
            # Observed ground can become blocked too. Try fresh approaches here.
            self.returning = False
            self.excursion_anchor = None
            self.local_target = self.open_goal = None
            self.breadcrumbs = [self.last_position]
            self.travel = 0
        elif self.local_target is not None and self.last_position is not None:
            origin = self.local_origin or self.last_position
            self.failures[self.connection(origin, self.local_target)] += 1
            self.local_target = None
            self.open_goal = None
            # Retrace observed ground before attempting a different heading.
            self.returning = True
        elif self.previous is not None and self.target is not None:
            edge = self.previous, self.target
            self.edge_failures[edge] += 1
            count = self.edge_failures[edge]
            super().avoid_target(now)
            self.blocked[edge] = now + min(3600, 60 * 2 ** min(count, 5))
        else:
            super().avoid_target(now)
        self.goal = None
        self.progress_target = None
        self.best_remaining = math.inf
        self.dirty = True

    def choose_goal(self, start, now):
        """Search reachable directed links, including previously visited bridges."""
        costs = {start: 0.0}
        queue = [(0.0, start)]
        regions = Counter()
        for key, count in self.visits.items():
            if key in self.graph.nodes:
                p = self.graph.nodes[key].position
                regions[(math.floor(p[0] / 100), math.floor(p[1] / 100), math.floor(p[2] / 12))] += count
        while queue:
            cost, key = heapq.heappop(queue)
            if cost != costs[key]:
                continue
            node = self.graph.nodes[key]
            for target, _ in node.links:
                if (target not in self.graph.nodes or self.blocked.get((key, target), 0) > now
                        or self.avoided.get(target, 0) > now):
                    continue
                penalty = min(self.edge_failures[(key, target)], 20) * 50
                candidate = cost + distance(node.position, self.graph.nodes[target].position) + penalty
                if candidate < costs.get(target, math.inf):
                    costs[target] = candidate
                    heapq.heappush(queue, (candidate, target))
        def score(key):
            p = self.graph.nodes[key].position
            region = (math.floor(p[0] / 100), math.floor(p[1] / 100), math.floor(p[2] / 12))
            return (self.visits[key] * 8 + min(regions[region], 100) * .15
                    + costs[key] / 300 + self.coverage[self.coverage_cell(p)] * .2,
                    costs[key], key)
        self.goal = min((key for key in costs if key != start), key=score, default=None)
        self.goal_at = now

    def off_path(self, position, heading, now):
        self.crossing = False
        if self.local_target is not None:
            horizontal = distance(position, self.local_origin)
            rise = abs(position[2] - self.local_origin[2])
            if rise > 2.5 and rise > horizontal * .65:
                self.avoid_target(now)
            elif distance(position, self.local_target) <= 2:
                self.local_target = None
        if self.returning:
            while self.breadcrumbs and distance(position, self.breadcrumbs[-1]) <= 2:
                self.breadcrumbs.pop()
            if self.breadcrumbs:
                self.message = 'exploring: backtracking over observed ground'
                return self.breadcrumbs[-1]
            self.returning = False
        if self.local_target is None:
            if (self.open_goal is None or distance(position, self.open_goal) < 15
                    or now - self.open_goal_at > 120):
                regions = Counter()
                for cell, count in self.coverage.items():
                    regions[(math.floor(cell[0] * 12 / 100),
                             math.floor(cell[1] * 12 / 100), cell[2])] += count
                candidates = []
                for step in range(8):
                    bearing = heading + step * math.tau / 8
                    goal = (position[0] - math.sin(bearing) * 100,
                            position[1] + math.cos(bearing) * 100, position[2])
                    region = (math.floor(goal[0] / 100), math.floor(goal[1] / 100),
                              self.coverage_cell(position)[2])
                    candidates.append((regions[region], abs(angle_delta(bearing, heading)), goal))
                self.open_goal = min(candidates)[2]
                self.open_goal_at = now
            options = []
            near_node = self.graph.nearest(position, radius=12)
            failed_bearings = []
            if near_node is not None:
                failed_bearings = [heading_to(near_node.position, self.graph.nodes[key].position)
                                   for key, _ in near_node.links
                                   if key in self.graph.nodes
                                   and self.edge_failures[(near_node.key, key)]]
            for step in range(8):
                bearing = heading + step * math.tau / 8
                if any(abs(angle_delta(bearing, failed)) < math.pi / 4 for failed in failed_bearings):
                    continue
                target = (position[0] - math.sin(bearing) * 10,
                          position[1] + math.cos(bearing) * 10, position[2])
                failures = self.failures[self.connection(position, target)]
                if failures >= 2:
                    continue
                cell = self.coverage_cell(target)
                novelty = self.coverage[cell]
                nearby = sum(self.coverage[(cell[0] + x, cell[1] + y, cell[2])]
                             for x, y in ((-1, 0), (1, 0), (0, -1), (0, 1)))
                bias = (abs(angle_delta(bearing, heading)) * .15
                        + abs(angle_delta(bearing, heading_to(position, self.open_goal))) * .4)
                if self.turn:
                    desired = heading + {'left': math.pi / 2, 'right': -math.pi / 2,
                                         'forward': 0}[self.turn]
                    bias += abs(angle_delta(bearing, desired)) * 4
                options.append((failures * 20 + novelty * 4 + nearby * .2 + bias,
                                self.rng.random(), target))
            if not options:
                self.message = 'exploring: local approaches blocked; waiting for a new position'
                return None
            self.local_target = min(options)[2]
            self.local_origin = position
            self.turn = None
        self.message = 'exploring open ground'
        return self.local_target

    def waypoint(self, position, heading, interior, now):
        result = self._waypoint(position, heading, interior, now)
        if not self.exploring or result is None or self.returning:
            return result
        remaining = distance(position, result)
        if result != self.progress_target:
            self.progress_target = result
            self.best_remaining, self.progress_at = remaining, now
        elif remaining < self.best_remaining - .5:
            self.best_remaining, self.progress_at = remaining, now
        elif now - self.progress_at > 30:
            # Moving in circles is also a failed approach, even without a stall.
            self.avoid_target(now)
            self.message = 'exploring: no progress toward waypoint; planning another approach'
            return None
        return result

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
            if self.excursion_travel >= 60 or self.returning:
                self.returning = True
            if self.returning and distance(position, self.excursion_anchor) <= 3:
                self.excursion_anchor = None
                self.returning = False
                self.local_target = None
                self.open_goal = None
                self.breadcrumbs = [position]
                self.travel = 0
            else:
                return self.off_path(position, heading, now)
        if nearby is None:
            self.target = self.previous = self.goal = None
            return self.off_path(position, heading, now)
        self.returning = False
        if self.travel >= 250 and (len(nearby.links) == 1 or self.travel >= 400):
            self.excursion_anchor = position
            self.excursion_travel = 0
            self.breadcrumbs = [position]
            self.local_target = self.goal = self.target = self.previous = None
            return self.off_path(position, heading, now)
        self.local_target = None
        reached_goal = (self.goal in self.graph.nodes
                        and distance(position, self.graph.nodes[self.goal].position) <= 3
                        and abs(position[2] - self.graph.nodes[self.goal].position[2]) <= 3)
        if (self.goal is None or self.goal not in self.graph.nodes or now - self.goal_at > 180
                or reached_goal):
            if reached_goal:
                self.visits[self.goal] += 1
            self.choose_goal(nearby.key, now)
        # Reuse directed mission routing without exposing an exploration goal as a mission.
        self.destination = self.goal
        try:
            result = super().waypoint(position, heading, interior, now)
        finally:
            self.destination = None
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
