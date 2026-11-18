"""Pedestrian path loading, waypoint selection, and steering."""

import heapq
import logging
import math
import struct
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


def angle_delta(target, current):
    return (target - current + math.pi) % (2 * math.pi) - math.pi


def heading_to(origin, target):
    return math.atan2(-(target[0] - origin[0]), target[1] - origin[1])


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def closest_point_on_segment(position, start, end):
    """Project onto the horizontal segment and interpolate its height."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        return end
    progress = (
        (position[0] - start[0]) * dx + (position[1] - start[1]) * dy
    ) / length_squared
    progress = max(0.0, min(1.0, progress))
    return tuple(a + progress * (b - a) for a, b in zip(start, end))


class SmoothTurn:
    """Acceleration-limited mouse velocity, with fractional movement retained."""

    def __init__(self, speed=160.0, acceleration=320.0):
        self.limit, self.acceleration = speed, acceleration
        self.reset()

    def reset(self):
        self.velocity = self.fraction = 0.0
        self.last_at = None

    def nudge(self, remaining, now):
        """Return a smoothed mouse delta and the unconsumed requested amount."""
        amount = self.step(remaining * 2.0, now)
        if amount * remaining > 0:
            amount = int(math.copysign(min(abs(amount), abs(remaining)), amount))
            remaining -= amount
        elif not remaining:
            amount = 0
            self.reset()
        return amount, remaining

    def step(self, desired, now):
        dt = (
            1 / 120
            if self.last_at is None
            else max(0.0, min(1 / 30, now - self.last_at))
        )
        self.last_at = now
        desired = max(-self.limit, min(self.limit, desired))
        change = max(
            -self.acceleration * dt,
            min(self.acceleration * dt, desired - self.velocity),
        )
        old = self.velocity
        self.velocity += change
        self.fraction += (old + self.velocity) * 0.5 * dt
        amount = math.trunc(self.fraction + math.copysign(1e-9, self.fraction))
        self.fraction -= amount
        if abs(self.fraction) < 1e-8:
            self.fraction = 0.0
        return amount


@dataclass(frozen=True)
class Node:
    key: tuple
    position: tuple
    links: tuple  # ((area, node), road_crossing)


def read_area(data, area):
    if len(data) < 20:
        raise ValueError(f"Truncated path header in area {area}")
    count, vehicles, pedestrians, car_links, addresses = struct.unpack_from("<5I", data)
    if (
        count != vehicles + pedestrians
        or count > 10000
        or addresses > 100000
        or car_links > 100000
    ):
        raise ValueError(f"Invalid path counts in area {area}")
    links_at = 20 + count * 28 + car_links * 14
    allocated = addresses + 192 if addresses else 0
    crossings_at = links_at + allocated * 4 + addresses * 2 + allocated
    if len(data) < crossings_at + allocated:
        raise ValueError(f"Truncated path tables in area {area}")
    nodes = []
    for i in range(vehicles, count):
        at = 20 + i * 28
        x, y, z = struct.unpack_from("<3h", data, at + 8)
        base, node_area, node_id = struct.unpack_from("<3H", data, at + 16)
        flags = data[at + 24]
        number = flags & 15
        if node_area != area or node_id != i or base + number > addresses:
            raise ValueError(f"Invalid pedestrian node in area {area}")
        if flags & 0xA0:  # Switched off or water, never vehicle nodes.
            continue
        links = tuple(
            (
                struct.unpack_from("<HH", data, links_at + j * 4),
                bool(data[crossings_at + j] & 1),
            )
            for j in range(base, base + number)
        )
        nodes.append(Node((area, i), (x / 8, y / 8, z / 8), links))
    return nodes


class PedestrianGraph:
    def __init__(self, nodes):
        original = {node.key: node for node in nodes}
        self.nodes = {}
        self.cells = defaultdict(list)
        for key, node in original.items():
            links = tuple(
                (target, crossing)
                for target, crossing in node.links
                if target in original
                and target != key
                and distance(node.position, original[target].position) <= 75
                and abs(node.position[2] - original[target].position[2]) <= 8
            )
            if links:
                self.nodes[key] = Node(key, node.position, links)
        for node in self.nodes.values():
            self.cells[self.cell(node.position)].append(node.key)

    @staticmethod
    def cell(position):
        return math.floor(position[0] / 50), math.floor(position[1] / 50)

    @classmethod
    def load(cls, game_directory):
        folder = Path(game_directory) / "data" / "paths"
        nodes = []
        for area in range(64):
            nodes.extend(read_area((folder / f"NODES{area}.DAT").read_bytes(), area))
        graph = cls(nodes)
        if not graph.nodes:
            raise ValueError("No connected pedestrian paths found")
        return graph

    def nearest(self, position, excluded=(), radius=25):
        cx, cy = self.cell(position)
        candidates = (
            self.nodes[key]
            for x in range(cx - 1, cx + 2)
            for y in range(cy - 1, cy + 2)
            for key in self.cells.get((x, y), ())
            if key not in excluded
        )
        return min(
            (
                node
                for node in candidates
                if abs(node.position[2] - position[2]) <= 3
                and distance(node.position, position) <= radius
            ),
            key=lambda node: distance(node.position, position),
            default=None,
        )

    def distances_to(self, destination, blocked=(), avoided=()):
        """Reverse Dijkstra: exact routes on directed pedestrian links."""
        reverse = defaultdict(list)
        for key, node in self.nodes.items():
            for target, _ in node.links:
                if (
                    target in self.nodes
                    and (key, target) not in blocked
                    and target not in avoided
                ):
                    reverse[target].append(
                        (key, distance(node.position, self.nodes[target].position))
                    )
        costs = {destination: 0.0}
        queue = [(0.0, destination)]
        while queue:
            cost, key = heapq.heappop(queue)
            if cost != costs[key]:
                continue
            for previous, length in reverse[key]:
                candidate = cost + length
                if candidate < costs.get(previous, math.inf):
                    costs[previous] = candidate
                    heapq.heappush(queue, (candidate, previous))
        return costs


class SidewalkNavigator:
    def __init__(self, graph):
        self.graph = graph
        self.visits = Counter()
        self.blocked = {}
        self.avoided = {}
        self.destination = None
        self.destination_costs = {}
        self.route_costs = {}
        self.route_signature = None
        self.reset()

    def set_destination(self, key):
        self.destination = key

    def update_route(self):
        signature = (self.destination, frozenset(self.blocked), frozenset(self.avoided))
        if signature != self.route_signature:
            if self.route_signature is None or self.destination != self.route_signature[0]:
                self.destination_costs = (
                    self.graph.distances_to(self.destination)
                    if self.destination in self.graph.nodes
                    else {}
                )
            self.route_costs = (
                self.graph.distances_to(self.destination, self.blocked, self.avoided)
                if self.destination in self.graph.nodes and (self.blocked or self.avoided)
                else self.destination_costs
            )
            self.route_signature = signature

    def reset(self):
        self.target = self.previous = None
        self.turn = None
        self.crossing = False
        self.message = "joining pedestrian path"

    def avoid_target(self, now):
        if self.target is not None:
            if self.previous is not None:
                self.blocked[(self.previous, self.target)] = now + 30
                self.blocked[(self.target, self.previous)] = now + 30
            self.avoided[self.target] = now + 15
        self.target = self.previous = None

    def waypoint(self, position, heading, interior, now):
        if interior:
            self.reset()
            self.message = "no sidewalk route indoors; switch to free roam"
            return None
        self.avoided = {k: t for k, t in self.avoided.items() if t > now}
        self.blocked = {k: t for k, t in self.blocked.items() if t > now}
        self.update_route()
        if self.target is not None:
            node = self.graph.nodes[self.target]
            reference = node.position
            if self.previous is not None:
                start = self.graph.nodes[self.previous].position
                reference = closest_point_on_segment(position, start, node.position)
            offset = distance(position, reference)
            height_error = abs(position[2] - reference[2])
            if offset > 45 or height_error > 4:
                logger.debug(
                    "Rejoining path: edge %s -> %s, offset %.1f, height error %.1f",
                    self.previous,
                    self.target,
                    offset,
                    height_error,
                )
                self.target = self.previous = None
        if self.target is None:
            node = self.graph.nearest(position, self.avoided)
            if node is None:
                self.message = "no nearby pedestrian path; switch to free roam"
                return None
            self.target = node.key
            self.message = "joining pedestrian path"
        node = self.graph.nodes[self.target]
        # Respawns and free roaming can leave a saved mission on a disconnected
        # sidewalk network. Keep exploring there instead of waiting forever.
        mission_unreachable = self.destination is not None and node.key not in self.destination_costs
        remaining = distance(position, node.position)
        passed = False
        if self.previous is not None:
            start = self.graph.nodes[self.previous].position
            dx, dy = node.position[0] - start[0], node.position[1] - start[1]
            length = math.hypot(dx, dy)
            if length > 0.1:
                beyond = (
                    (position[0] - node.position[0]) * dx
                    + (position[1] - node.position[1]) * dy
                ) >= 0
                lateral = (
                    abs((position[0] - start[0]) * dy - (position[1] - start[1]) * dx)
                    / length
                )
                passed = beyond and lateral <= 3 and remaining <= 6
        if remaining <= 2 or (self.previous is None and remaining <= 4) or passed:
            if node.key == self.destination:
                self.message = "at mission destination"
                return node.position
            options = [
                (self.graph.nodes[key], crossing)
                for key, crossing in node.links
                if key in self.graph.nodes
                and (node.key, key) not in self.blocked
                and key not in self.avoided
            ]
            forward = [option for option in options if option[0].key != self.previous]
            if forward and (self.destination is None or mission_unreachable or self.turn):
                options = forward
            if not options:
                self.message = "pedestrian path blocked; waiting to retry"
                return None

            def delta(option):
                return angle_delta(
                    heading_to(node.position, option[0].position), heading
                )

            if len(options) > 1 and self.turn:
                if self.turn == "left":
                    chosen = max(options, key=delta)
                elif self.turn == "right":
                    chosen = min(options, key=delta)
                else:
                    chosen = min(options, key=lambda option: abs(delta(option)))
                self.turn = None
            elif self.destination is not None and not mission_unreachable:
                reachable = [
                    option for option in options if option[0].key in self.route_costs
                ]
                if not reachable:
                    self.message = "no pedestrian route to mission; waiting to retry"
                    return None
                chosen = min(
                    reachable,
                    key=lambda option: (
                        distance(node.position, option[0].position)
                        + self.route_costs[option[0].key]
                    ),
                )
            else:
                chosen = min(
                    options,
                    key=lambda option: (
                        abs(delta(option)) + min(self.visits[option[0].key], 8) * 0.8
                    ),
                )
            self.visits[node.key] += 1
            self.previous, self.target = node.key, chosen[0].key
            self.crossing = chosen[1]
            node = chosen[0]
            self.message = (
                "following pedestrian crossing"
                if self.crossing
                else "following sidewalk"
            )
            if mission_unreachable:
                self.message += "; mission unreachable from this path"
        return node.position

    def report(self):
        return {
            "target": self.target,
            "previous": self.previous,
            "crossing": self.crossing,
            "next_turn": self.turn,
            "status": self.message,
            "destination": self.destination,
        }
