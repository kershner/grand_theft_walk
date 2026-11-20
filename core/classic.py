"""Save validation and named sidewalk destinations shared by classic III/VC."""

import math
import struct
from pathlib import Path


SAVE_SIZE = 201729


def read_text_table(data, start=0):
    """Decode the UTF-16 TKEY/TDAT chunks used by III and Vice City."""
    if start < 0 or start + 8 > len(data) or data[start:start + 4] != b"TKEY":
        raise ValueError("Missing classic game text table")
    size = struct.unpack_from("<I", data, start + 4)[0]
    end = start + 8 + size
    if size % 12 or end + 8 > len(data) or data[end:end + 4] != b"TDAT":
        raise ValueError("Invalid classic game text chunks")
    text_size = struct.unpack_from("<I", data, end + 4)[0]
    text = data[end + 8:end + 8 + text_size]
    if len(text) != text_size:
        raise ValueError("Truncated classic game text data")
    result = {}
    for at in range(start + 8, end, 12):
        offset = struct.unpack_from("<I", data, at)[0]
        if offset >= len(text) or offset % 2:
            raise ValueError("Invalid classic game text offset")
        stop = offset
        while stop + 1 < len(text) and text[stop:stop + 2] != b"\0\0":
            stop += 2
        key = data[at + 4:at + 12].rstrip(b"\0").decode("ascii")
        result[key.upper()] = text[offset:stop].decode("utf-16-le")
    return result


def load_zones(path, names):
    zones = []
    for line in Path(path).read_text().splitlines():
        fields = [part.strip() for part in line.split(",")]
        if len(fields) == 9 and (name := names.get(fields[0].upper())):
            zones.append((name, tuple(float(v) for v in fields[2:8])))
    return zones


def player_identity(memory, ped, pool_address, ped_size):
    pool = memory.u32(pool_address)
    if not pool:
        return None
    objects, bitmap, count = memory.unpack(pool, "3I")
    slot, remainder = divmod(ped - objects, ped_size)
    if remainder or not 0 <= slot < min(count, 10000):
        return None
    generation = memory.byte(bitmap + slot)
    if generation & 0x80:
        return None
    return f"{pool:x}:{objects:x}:{slot}:{generation}:{memory.read(ped + 0x5A, 4).hex()}"


def valid_save(path):
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return False
    # PC saves have size-prefixed, four-byte-aligned blocks and a byte checksum.
    # Unlike San Andreas these do not have a BLOCK marker.
    if not 200000 <= len(data) <= 4 * 1024 * 1024:
        return False
    if struct.unpack_from("<I", data, 68)[0] != SAVE_SIZE:
        return False
    end = len(data) - 4
    at = 0
    blocks = 0
    while at < end:
        if at + 4 > end:
            return False
        size = struct.unpack_from("<I", data, at)[0]
        if not 0 < size <= 0x10000:
            return False
        at += 4 + ((size + 3) & ~3)
        blocks += 1
        if at > end or blocks > 40:
            return False
    return (at == end and blocks >= 20
            and sum(data[:end]) & 0xFFFFFFFF == struct.unpack_from("<I", data, end)[0])


def main_pedestrian_network(graph):
    """Find the largest mutually reachable network, respecting one-way links."""
    reverse = {key: [] for key in graph.nodes}
    for key, node in graph.nodes.items():
        for target, _ in node.links:
            if target in reverse:
                reverse[target].append(key)
    # Iterative Kosaraju avoids recursion limits on the several-thousand-node map.
    visited, order = set(), []
    for start in sorted(graph.nodes):
        pending = [(start, False)]
        while pending:
            key, finished = pending.pop()
            if finished:
                order.append(key)
            elif key not in visited:
                visited.add(key)
                pending.append((key, True))
                pending.extend((target, False) for target, _ in graph.nodes[key].links
                               if target in graph.nodes and target not in visited)
    visited, largest = set(), set()
    for start in reversed(order):
        if start in visited:
            continue
        component, pending = set(), [start]
        while pending:
            key = pending.pop()
            if key in visited:
                continue
            visited.add(key)
            component.add(key)
            pending.extend(reverse[key])
        if len(component) > len(largest):
            largest = component
    return largest


def load_destinations(game_directory, graph, read_text, load_zones, default_name, *, network=None):
    """Offer every distinct meeting point on the main sidewalk network."""
    names = read_text(Path(game_directory) / "TEXT/american.gxt")
    zones = load_zones(game_directory, names)
    network = main_pedestrian_network(graph) if network is None else network
    nodes = [graph.nodes[key] for key in sorted(network)]
    destinations, used = [], set()
    counts = {}

    def add(name, node):
        if node.position in used:
            return
        used.add(node.position)
        counts[name] = counts.get(name, 0) + 1
        label = name if counts[name] == 1 else f"{name}: meeting point {counts[name]}"
        destinations.append({"name": label, "node": list(node.key), "position": list(node.position)})

    # Keep the original named-area meeting points for existing sessions.
    for name, bounds in zones:
        candidates = [node for node in nodes
                 if all(bounds[i] <= node.position[i] <= bounds[i + 3] for i in range(3))]
        if not candidates:
            continue
        center = ((bounds[0] + bounds[3]) / 2, (bounds[1] + bounds[4]) / 2)
        add(name, min(candidates, key=lambda candidate: math.dist(candidate.position[:2], center)))
    # Prefer small subareas (e.g. Junk Yard) over their enclosing neighborhood.
    zones = sorted(zones, key=lambda zone: ((zone[1][3] - zone[1][0]) * (zone[1][4] - zone[1][1]), zone[0]))
    for node in nodes:
        name = next((name for name, bounds in zones
                     if all(bounds[i] <= node.position[i] <= bounds[i + 3] for i in range(3))), default_name)
        add(name, node)
    return destinations
