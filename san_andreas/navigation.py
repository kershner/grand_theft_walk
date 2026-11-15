"""San Andreas pedestrian path loading, waypoint selection, and steering."""

import math
import struct
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


def angle_delta(target, current):
    return (target - current + math.pi) % (2 * math.pi) - math.pi


def heading_to(origin, target):
    return math.atan2(-(target[0] - origin[0]), target[1] - origin[1])


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class SmoothTurn:
    """Acceleration-limited mouse velocity, with fractional movement retained."""

    def __init__(self, speed=160.0, acceleration=320.0):
        self.limit, self.acceleration = speed, acceleration
        self.reset()

    def reset(self):
        self.velocity = self.fraction = 0.0
        self.last_at = None

    def step(self, desired, now):
        dt = 1 / 120 if self.last_at is None else max(0.0, min(1 / 30, now - self.last_at))
        self.last_at = now
        desired = max(-self.limit, min(self.limit, desired))
        change = max(-self.acceleration * dt, min(self.acceleration * dt, desired - self.velocity))
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
    if count != vehicles + pedestrians or count > 10000 or addresses > 100000 or car_links > 100000:
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
            (struct.unpack_from("<HH", data, links_at + j * 4), bool(data[crossings_at + j] & 1))
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


class SidewalkNavigator:
    def __init__(self, graph):
        self.graph = graph
        self.visits = Counter()
        self.blocked = {}
        self.avoided = {}
        self.reset()

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
        if self.target is not None:
            node = self.graph.nodes[self.target]
            if distance(position, node.position) > 45 or abs(position[2] - node.position[2]) > 4:
                self.target = self.previous = None
        if self.target is None:
            node = self.graph.nearest(position, self.avoided)
            if node is None:
                self.message = "no nearby pedestrian path; switch to free roam"
                return None
            self.target = node.key
            self.message = "joining pedestrian path"
        node = self.graph.nodes[self.target]
        remaining = distance(position, node.position)
        passed = False
        if self.previous is not None:
            start = self.graph.nodes[self.previous].position
            dx, dy = node.position[0] - start[0], node.position[1] - start[1]
            length = math.hypot(dx, dy)
            if length > 0.1:
                beyond = (
                    (position[0] - node.position[0]) * dx + (position[1] - node.position[1]) * dy
                ) >= 0
                lateral = (
                    abs((position[0] - start[0]) * dy - (position[1] - start[1]) * dx) / length
                )
                passed = beyond and lateral <= 3 and remaining <= 6
        if remaining <= 2 or (self.previous is None and remaining <= 4) or passed:
            options = [
                (self.graph.nodes[key], crossing)
                for key, crossing in node.links
                if key in self.graph.nodes
                and (node.key, key) not in self.blocked
                and key not in self.avoided
            ]
            forward = [option for option in options if option[0].key != self.previous]
            if forward:
                options = forward
            if not options:
                self.message = "pedestrian path blocked; waiting to retry"
                return None

            def delta(option):
                return angle_delta(heading_to(node.position, option[0].position), heading)

            if len(options) > 1 and self.turn:
                chosen = (
                    max(options, key=delta)
                    if self.turn == "left"
                    else min(options, key=delta)
                    if self.turn == "right"
                    else min(options, key=lambda option: abs(delta(option)))
                )
                self.turn = None
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
                "following pedestrian crossing" if self.crossing else "following sidewalk"
            )
        return node.position

    def report(self):
        return {
            "target": self.target,
            "previous": self.previous,
            "crossing": self.crossing,
            "next_turn": self.turn,
            "status": self.message,
        }
