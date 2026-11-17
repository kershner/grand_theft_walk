"""Pinned classic Vice City layout and reversible walking/map changes."""

import hashlib
import math
import struct
from pathlib import Path

from games import Capabilities
from core.saves import Autosave as SharedAutosave
from core.navigation import Node, PedestrianGraph
from core.winmemory import Memory as ProcessMemory
from core.controls import JUMP_SPEED, bind_action_key, can_control

CAPABILITIES = Capabilities(protection=True, unlock_map=True, walk_binding=True, walking_pace=True, first_person=True, respawn=True, jump=True, autosave=True, view=True)
WALK_KEYS = {"forward": "w", "back": "s"}
WALK_SPEED_MULTIPLIER = 1.0
JUMP_ACTION = 11
JUMP_KEY = 32  # Space; avoid the game's hardcoded replay shortcuts.

PLAYER_INFO = 0x94AD28
PED_POOL = 0x97F2AC
PED_SIZE = 0x6D8
CAMERA = 0x7E4688
MENU = 0x869630
PAD = 0x7DBCB0
SCRIPT_SPACE = 0x821280
PATHS = 0x9B6E5C
OBJECT_POOL = 0x94DBE0
OBJECT_SIZE = 0x1A0
CONTROL_ACTIONS = 0x9B5914
BARRIER_MODELS = {590, 2141, 2446, 2447, 3518}
# Main.scm's island bridge/checkpoint closures, with coordinate order normalized.
ISLAND_PATH_REGIONS = (
    (-787.8, -519.4, 10, -657.5, -475.2, 20),
    (-230, -464.5, 10, 165.85, -459.8, 20),
    (-129, 1041.9, 0, -99.8, 1097.4, 30),
    (-258.7, -948.8, 0, -214.6, -920.6, 30),
    (189.8, 230.3, 0, 248, 258.5, 30),
    (-102.3, 84.3, 0, -38, 95.1, 30),
)
COUNTS = 0x537F0
CONNECTIONS = 0x3A980
MAX_NODES = 9650
MAX_CONNECTIONS = 20400
WEATHER = ("clear skies", "cloudy", "rainy", "foggy", "clear skies", "hurricane", "extra colours")


def read_game_text(path):
    data = Path(path).read_bytes()
    if data[:4] != b"TABL" or len(data) < 8:
        raise ValueError("Unsupported Vice City text table")
    size = struct.unpack_from("<I", data, 4)[0]
    main = None
    for at in range(8, 8 + size, 12):
        if at + 12 > len(data):
            raise ValueError("Truncated Vice City table directory")
        if data[at:at + 8].rstrip(b"\0") == b"MAIN":
            main = struct.unpack_from("<I", data, at + 8)[0]
            break
    if main is None or data[main:main + 4] != b"TKEY":
        raise ValueError("Vice City MAIN text table is missing")
    size = struct.unpack_from("<I", data, main + 4)[0]
    text_at = main + 8 + size
    if size % 12 or data[text_at:text_at + 4] != b"TDAT":
        raise ValueError("Invalid Vice City text chunks")
    text_size = struct.unpack_from("<I", data, text_at + 4)[0]
    text = data[text_at + 8:text_at + 8 + text_size]
    if len(text) != text_size:
        raise ValueError("Truncated Vice City text data")
    result = {}
    for at in range(main + 8, main + 8 + size, 12):
        offset = struct.unpack_from("<I", data, at)[0]
        key = data[at + 4:at + 12].rstrip(b"\0").decode("ascii")
        if offset >= len(text) or offset % 2:
            raise ValueError("Invalid Vice City text offset")
        end = offset
        while end + 1 < len(text) and text[end:end + 2] != b"\0\0":
            end += 2
        result[key.upper()] = text[offset:end].decode("utf-16-le")
    return result


def load_zones(game_directory, names):
    zones = []
    for line in (Path(game_directory) / "data/navig.zon").read_text().splitlines():
        fields = [part.strip() for part in line.split(",")]
        if len(fields) != 9:
            continue
        name = names.get(fields[0].upper())
        if name:
            bounds = tuple(float(value) for value in fields[2:8])
            zones.append((name, bounds))
    return zones


SAVE_PREFIX = 0x97509C
SAVE_SIZE = 201729


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


class Autosave(SharedAutosave):
    save_prefix = SAVE_PREFIX
    valid_save = staticmethod(valid_save)

    def menu(self):
        return self.mem.u32(MENU + 0xF8) if self.mem.byte(MENU + 0x38) else None

    def safe(self, state):
        if not state.get("ready"):
            return False
        page = self.menu()
        pause_menu = page == 32 and state.get("paused")
        if not (state["player_state"] == 0 and state["health"] > 0
                and not state.get("in_water", False)
                and not state["in_vehicle"] and state["interior"] == 0
                and state["ped_state"] in (0, 1)
                and state["standing"] and not state["in_air"]
                and (not state["paused"] or pause_menu)
                and (not state["controls_disabled"] or pause_menu)):
            return False
        if ((page is not None and not pause_menu)
                or self.mem.byte(MENU + 0x39) or self.mem.byte(MENU + 0x3C)
                or self.mem.byte(0xA10AB2) or self.mem.byte(0xA10B8D)):
            return False
        mission = self.mem.u32(0x978748)
        return (0 <= mission < 34336 and mission % 4 == 0
                and (not mission or (mission >= 8 and self.mem.u32(SCRIPT_SPACE + mission) != 1)))


class Game:
    def __init__(self, memory, zones=None):
        self.mem = memory
        self.names = read_game_text(memory.exe.parent / "TEXT/american.gxt") if zones is None else {}
        self.zones = load_zones(memory.exe.parent, self.names) if zones is None else zones

    def identity(self, ped):
        mem = self.mem
        pool = mem.u32(PED_POOL)
        if not pool:
            return None
        objects, bitmap, count = mem.unpack(pool, "3I")
        slot, remainder = divmod(ped - objects, PED_SIZE)
        if remainder or not 0 <= slot < min(count, 10000):
            return None
        generation = mem.byte(bitmap + slot)
        if generation & 0x80:
            return None
        return f"{pool:x}:{objects:x}:{slot}:{generation}:{mem.read(ped + 0x5A, 4).hex()}"

    def snapshot(self):
        try:
            return self._snapshot()
        except (OSError, ValueError, struct.error):
            return {"ready": False, "pid": self.mem.pid, "reason": "Waiting for the Vice City world to settle"}

    def _snapshot(self):
        mem = self.mem
        if mem.byte(MENU + 0x39) or mem.byte(MENU + 0x3C):
            return {"ready": False, "pid": mem.pid, "reason": "Vice City is loading"}
        ped = mem.u32(PLAYER_INFO)
        identity = self.identity(ped) if ped else None
        if not identity:
            return {"ready": False, "pid": mem.pid, "reason": "Waiting for the Vice City world"}
        block = mem.read(ped, PED_SIZE)
        position = struct.unpack_from("<3f", block, 0x34)
        health, armor = struct.unpack_from("<2f", block, 0x354)
        if not all(math.isfinite(value) for value in (*position, health, armor)):
            return {"ready": False, "pid": mem.pid, "reason": "Invalid Vice City player state"}
        active = mem.byte(CAMERA + 0x76)
        if active > 2:
            return {"ready": False, "pid": mem.pid, "reason": "Waiting for the Vice City camera"}
        cam = CAMERA + 0x188 + active * 0x1CC
        front = mem.unpack(cam + 0x168, "3f")
        heading_value = struct.unpack_from("<f", block, 0x374)[0]
        vertical_speed = struct.unpack_from("<f", block, 0x78)[0]
        if not all(math.isfinite(value) for value in (*front, heading_value, vertical_speed)):
            return {"ready": False, "pid": mem.pid, "reason": "Invalid Vice City movement state"}
        heading = math.atan2(-front[0], front[1])
        pitch = math.atan2(front[2], math.hypot(front[0], front[1]))
        wanted = struct.unpack_from("<I", block, 0x5F4)[0]
        flags = block[0x14C:0x14E]
        if mem.u32(PLAYER_INFO) != ped or self.identity(ped) != identity:
            return {"ready": False, "pid": mem.pid, "reason": "Vice City player changed"}
        return {
            "ready": True, "pid": mem.pid, "ped": ped, "identity": identity,
            "player_info": PLAYER_INFO,
            "position": dict(zip(("x", "y", "z"), position)),
            "heading": heading_value,
            "camera_heading": heading, "camera_pitch": pitch,
            "standing": bool(flags[0] & 1), "in_air": bool(flags[1] & 8),
            "in_water": bool(block[0x54] & 4 or block[0x156] & 1),
            "drowning": bool(block[0x156] & 1),
            "vertical_speed": vertical_speed,
            "health": health, "armor": armor,
            "max_health": mem.byte(PLAYER_INFO + 0x143),
            "interior": mem.u32(0x978810),
            "in_vehicle": bool(block[0x3AC]),
            "player_state": mem.byte(PLAYER_INFO + 0xCC),
            "ped_state": struct.unpack_from("<I", block, 0x244)[0],
            "move_state": struct.unpack_from("<I", block, 0x24C)[0],
            "wanted_pointer": wanted,
            "wanted_level": mem.u32(wanted + 0x20) if wanted else None,
            "paused": bool(mem.byte(0xA10B36) or mem.byte(0xA10B76)),
            "controls_disabled": bool(mem.unpack(PAD + 0xF0, "H")[0]),
            "foreground": mem.foreground(),
            "game_time_ms": mem.u32(0x974B2C),
            "cutscene": bool(mem.byte(0xA10AB2)),
        }

    def kill_player(self, state):
        """Let the native death/hospital sequence handle the current player."""
        if not state.get("ready"):
            return False
        ped = state["ped"]
        if (self.mem.u32(PLAYER_INFO) != ped
                or self.identity(ped) != state["identity"]
                or self.mem.byte(PLAYER_INFO + 0xCC) != 0):
            return False
        health = self.mem.unpack(ped + 0x354, "f")[0]
        if not math.isfinite(health) or health <= 0:
            return False
        self.mem.write(ped + 0x354, struct.pack("<f", 0.0))
        return True

    def boost_jump(self, state):
        """Tune native takeoff once, preserving horizontal motion and gravity."""
        if not state.get("ready") or not state.get("in_air") or state.get("standing"):
            return False
        ped = state["ped"]
        if self.mem.u32(PLAYER_INFO) != ped or self.identity(ped) != state["identity"]:
            return False
        if self.mem.byte(ped + 0x14C) & 1 or not self.mem.byte(ped + 0x14D) & 8:
            return False
        speed = self.mem.unpack(ped + 0x78, "f")[0]
        if not math.isfinite(speed) or speed <= 0:
            return False
        self.mem.write(ped + 0x78, struct.pack("<f", JUMP_SPEED))
        return True

    def location(self, state):
        point = tuple(state["position"][axis] for axis in ("x", "y", "z"))
        names = [name for name, b in self.zones if all(b[i] <= point[i] <= b[i + 3] for i in range(3))]
        hour, minute = self.mem.byte(0xA10B6B), self.mem.byte(0xA10B92)
        weather = self.mem.unpack(0xA10A2E, "h")[0]
        compass = ("north", "northwest", "west", "southwest", "south", "southeast", "east", "northeast")
        return {
            "places": (names[:1] + ["Vice City"]) if names else ["Vice City"],
            "time": f"{hour % 12 or 12}:{minute:02} {'AM' if hour < 12 else 'PM'}",
            "period": "night" if hour < 6 or hour >= 20 else "morning" if hour < 12 else "afternoon" if hour < 18 else "evening",
            "weather": WEATHER[weather] if 0 <= weather < len(WEATHER) else "weather unavailable",
            "heading": compass[round(state["heading"] / (math.pi / 4)) % 8],
            "indoors": state["interior"] != 0,
        }


class Memory(ProcessMemory):
    def __init__(self, writable=False, pid=None):
        super().__init__(writable=writable, pid=pid, game="vice_city")
        try:
            # Plugin-SDK's classic 1.0 discriminator. This permits inspection,
            # plus the pinned executable hash checked by ProcessMemory.
            if self.read(0x667BF0, 4) != bytes.fromhex("5589e553"):
                raise RuntimeError("Vice City's classic 1.0 runtime signature does not match.")
        except Exception:
            self.close()
            raise

def read_graph(data):
    if len(data) < COUNTS + 12:
        raise ValueError("Truncated Vice City path graph")
    total, cars, pedestrians = struct.unpack_from("<3I", data, COUNTS)
    if total != cars + pedestrians or not 0 < total <= MAX_NODES:
        raise ValueError("Vice City's pedestrian graph is not initialized")
    nodes = []
    for index in range(cars, total):
        at = index * 20
        x, y, z = struct.unpack_from("<3h", data, at + 4)
        first = struct.unpack_from("<H", data, at + 12)[0]
        flags = data[at + 16]
        count = flags & 15
        if first + count > MAX_CONNECTIONS:
            raise ValueError("Invalid Vice City path connection range")
        if flags & 0x60 or data[at + 17] & 1:
            continue  # Disabled/between-level/water nodes cannot be walked.
        links = []
        for link in range(first, first + count):
            address = struct.unpack_from("<H", data, CONNECTIONS + link * 2)[0]
            target = address & 0x3FFF
            if cars <= target < total:
                links.append(((0, target), bool(address & 0x8000)))
        nodes.append(Node((0, index), (x / 8, y / 8, z / 8), tuple(links)))
    graph = PedestrianGraph(nodes)
    if not graph.nodes:
        raise ValueError("No connected Vice City pedestrian paths found")
    return graph


def load_graph(memory):
    return read_graph(memory.read(PATHS, COUNTS + 12))


def main_sidewalk_network(graph):
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


def load_destinations(game_directory, graph, read_text):
    """Offer every distinct meeting point on the main sidewalk network."""
    names = read_text(Path(game_directory) / "TEXT/american.gxt")
    zones = load_zones(game_directory, names)
    network = main_sidewalk_network(graph)
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
                     if all(bounds[i] <= node.position[i] <= bounds[i + 3] for i in range(3))), "Vice City")
        add(name, node)
    return destinations


class RuntimeChanges:
    """Restore owned bits only while their object/path allocation still matches."""

    def __init__(self, memory):
        self.mem = memory
        self.entries = {}

    def valid_guard(self, guard):
        if guard is None:
            return True
        kind, *values = guard
        if kind == "paths":
            return self.mem.unpack(PATHS + COUNTS, "3I") == tuple(values)
        if kind in ("ped", "wanted"):
            ped, identity, *wanted = values
            return (self.mem.u32(PLAYER_INFO) == ped
                    and Game(self.mem, zones=[]).identity(ped) == identity
                    and (not wanted or self.mem.u32(ped + 0x5F4) == wanted[0]))
        pool, objects, bitmap, slot, generation, identity = values
        return (self.mem.u32(OBJECT_POOL) == pool
                and self.mem.unpack(pool, "2I") == (objects, bitmap)
                and self.mem.byte(bitmap + slot) == generation
                and self.mem.read(objects + slot * OBJECT_SIZE + 0x5A, 4) == identity)

    def patch(self, address, after, mask=None, guard=None):
        if not self.valid_guard(guard):
            return
        mask = bytes([255] * len(after)) if mask is None else mask
        current = self.mem.read(address, len(after))
        key = (address, len(after))
        entry = self.entries.get(key)
        if entry is None or entry[3] != guard:
            self.entries[key] = (current, after, mask, guard)
        value = bytes((old & ~bits) | (new & bits) for old, new, bits in zip(current, after, mask))
        if value != current:
            self.mem.write(address, value)

    def restore(self):
        failures = []
        for (address, size), (before, after, mask, guard) in reversed(list(self.entries.items())):
            try:
                if not self.valid_guard(guard):
                    continue
                current = self.mem.read(address, size)
                if any((now & bits) != (ours & bits) for now, ours, bits in zip(current, after, mask)):
                    continue
                value = bytes((now & ~bits) | (old & bits) for now, old, bits in zip(current, before, mask))
                self.mem.write(address, value)
            except OSError as error:
                failures.append(str(error))
        self.entries.clear()
        return failures


def apply_protection(game, changes, state):
    """Protect the current living player without changing NPC behavior."""
    if not state.get("ready"):
        return
    mem, ped = game.mem, state["ped"]
    guard = ("ped", ped, state["identity"])
    if not changes.valid_guard(guard) or mem.byte(PLAYER_INFO + 0xCC) != 0:
        return
    if state.get("in_water") or mem.byte(ped + 0x54) & 4 or mem.byte(ped + 0x156) & 1:
        changes.restore()
        return
    health = mem.unpack(ped + 0x354, "f")[0]
    if not math.isfinite(health) or health <= 0:
        return
    # Explosion, bullet, fire, collision/fall, melee and non-player damage.
    changes.patch(ped + 0x52, b"\x02", b"\x02", guard)
    changes.patch(ped + 0x53, b"\x3e", b"\x3e", guard)
    changes.patch(ped + 0x154, b"\x02", b"\x02", guard)  # No critical hits.
    changes.patch(PLAYER_INFO + 0x140, b"\x01")  # Infinite sprint; fixed player-info storage.
    changes.patch(PLAYER_INFO + 0x142, b"\x01")  # Fireproof.
    maximum = max(100, mem.byte(PLAYER_INFO + 0x143))
    if health < maximum and changes.valid_guard(guard):
        mem.write(ped + 0x354, struct.pack("<f", maximum))
    wanted = mem.u32(ped + 0x5F4)
    if not wanted:
        return
    wanted_guard = ("wanted", ped, state["identity"], wanted)
    # Zero crime multiplier prevents new chaos; clear existing wanted values
    # and queued crimes as gameplay state, without restoring a previous chase.
    changes.patch(wanted + 0x14, struct.pack("<f", 0), guard=wanted_guard)
    for offset in (0, 4, 0x20, 0x24, *(0x28 + index * 0x1C for index in range(16))):
        if not changes.valid_guard(wanted_guard):
            return
        if mem.u32(wanted + offset):
            mem.write(wanted + offset, b"\0" * 4)


def bind_walk_key(game, changes):
    """Bind W/S to their native actions; Vice City has no keyboard walk modifier."""
    data = game.mem.read(CONTROL_ACTIONS, 44 * 32)
    for action, key in ((3, ord("W")), (4, ord("S"))):
        for other in range(3, 7):
            if other == action:
                continue
            for keyboard in (0, 1):
                at = other * 32 + keyboard * 8
                if struct.unpack_from("<I", data, at)[0] == key:
                    changes.patch(CONTROL_ACTIONS + at, struct.pack("<I", 1056))
        changes.patch(CONTROL_ACTIONS + action * 32, struct.pack("<II", key, 1))


def bind_jump_key(game, changes):
    """Temporarily own Space in both keyboard binding columns."""
    bind_action_key(game.mem, changes, CONTROL_ACTIONS, 44, JUMP_ACTION, JUMP_KEY)


class WalkingPace:
    """Cap the native walk/run blend while grounded, without scaling velocity."""

    def __init__(self, memory, multiplier):
        if not math.isfinite(multiplier) or not 0.1 <= multiplier <= 1:
            raise ValueError("Walking speed multiplier must be between 0.1 and 1.0")
        self.mem = memory
        self.limit = multiplier
        self.game = Game(memory)

    def tick(self, state, moving):
        if not moving or not state.get("ready"):
            return
        if (self.mem.u32(PLAYER_INFO) != state["ped"]
                or self.game.identity(state["ped"]) != state["identity"]):
            return
        address = state["ped"] + 0x5FC
        speed = self.mem.unpack(address, "f")[0]
        if self.limit < speed <= 3:
            self.mem.write(address, struct.pack("<f", self.limit))


def island_path_changes(data):
    total, cars, pedestrians = struct.unpack_from("<3I", data, COUNTS)
    if total != cars + pedestrians or not 0 < total <= MAX_NODES:
        raise ValueError("Vice City's path graph is not initialized")
    pending = []
    reopened = set()
    for index in range(total):
        at = index * 20
        if not data[at + 16] & 0x20 or data[at + 17] & 1:
            continue
        point = tuple(value / 8 for value in struct.unpack_from("<3h", data, at + 4))
        if any(all(bounds[i] <= point[i] <= bounds[i + 3] for i in range(3))
               for bounds in ISLAND_PATH_REGIONS):
            pending.append(index)
            reopened.add(index)
    # Match CPathFind::SwitchOffNodeAndNeighbours: propagate along disabled
    # chains until a junction, without opening unrelated closed building paths.
    while pending:
        index = pending.pop()
        at = index * 20
        count = data[at + 16] & 15
        if count >= 3:
            continue
        first = struct.unpack_from("<H", data, at + 12)[0]
        if first + count > MAX_CONNECTIONS:
            raise ValueError("Invalid Vice City bridge path links")
        for link in range(first, first + count):
            target = struct.unpack_from("<H", data, CONNECTIONS + link * 2)[0] & 0x3FFF
            if not 0 <= target < total:
                raise ValueError("Invalid Vice City bridge path target")
            flags = data[target * 20 + 16]
            if (target not in reopened and flags & 0x20 and flags & 15 < 3
                    and not data[target * 20 + 17] & 1):
                reopened.add(target)
                pending.append(target)
    return {index: (0x60 if index in reopened else 0x40)
            for index in range(total)
            if index in reopened or data[index * 20 + 16] & 0x40}


def unlock_regions(game, changes):
    mem = game.mem
    data = mem.read(PATHS, COUNTS + 12)
    counts = struct.unpack_from("<3I", data, COUNTS)
    for index, mask in island_path_changes(data).items():
        changes.patch(PATHS + index * 20 + 16, b"\0", bytes([mask]), ("paths", *counts))
    pool = mem.u32(OBJECT_POOL)
    if not pool:
        return 0
    objects, bitmap, count = mem.unpack(pool, "3I")
    if not 0 < count <= 10000:
        raise ValueError("Invalid Vice City object pool")
    barriers = 0
    for slot, generation in enumerate(mem.read(bitmap, count)):
        if generation & 0x80:
            continue
        address = objects + slot * OBJECT_SIZE
        identity = mem.read(address + 0x5A, 4)
        model = struct.unpack_from("<H", identity, 2)[0]
        if model not in BARRIER_MODELS:
            continue
        guard = ("object", pool, objects, bitmap, slot, generation, identity)
        # Preserve the objects and script handles, just as in San Andreas.
        changes.patch(address + 0x51, b"\0\0", b"\x01\x04", guard)
        barriers += 1
    return barriers


def unlock_interiors(game, changes):
    # Vice City lacks San Andreas's entry/exit portal pool. Interior support is
    # separate from opening the island roadblocks and is not enabled here.
    return 0


CAMERA_MODULE = "FirstPerson.asi"
CAMERA_MODULE_SHA256 = "b63145e7f5c13e893aa4e38a765d58eba3857831f4fd3a45c6f4ea80601806aa"
PED_ZOOM_INDICATOR = 0x7E4688 + 0x164
ACTIVE = 0x1D2EA6


class FirstPersonCamera:
    def __init__(self, memory):
        self.mem = memory
        self.bound = self.original = self.last_written = None
        self.error = None

    def apply(self, enabled, rotate_player=True):
        # This plugin handles character rotation internally.
        try:
            module = self.mem.module(CAMERA_MODULE)
            if module is None:
                raise RuntimeError("First-person mod is not installed or loaded")
            if module != self.bound:
                if (module["size"] <= ACTIVE
                        or hashlib.sha256(module["path"].read_bytes()).hexdigest()
                        != CAMERA_MODULE_SHA256):
                    raise RuntimeError("Unsupported first-person mod version")
                self.bound = self.original = self.last_written = None
                if self.mem.byte(module["base"] + ACTIVE) not in (0, 1):
                    raise RuntimeError("Invalid first-person camera flag")
                self.bound = module
            if not can_control(Game(self.mem, zones=[]).snapshot()):
                return False
            current = self.mem.unpack(PED_ZOOM_INDICATOR, "f")[0]
            if not math.isfinite(current) or current not in (0, 1, 2, 3, 4):
                raise RuntimeError("Invalid native camera selector")
            if self.original is None:
                self.original = current
            third = self.original if self.original in (1, 2) else 2.0
            target = 3.0 if enabled else third
            if current != target:
                self.mem.write(PED_ZOOM_INDICATOR, struct.pack("<f", target))
                self.last_written = target
            self.error = None
            return True
        except (OSError, RuntimeError, ValueError) as error:
            self.error = str(error)
            return False

    def close(self):
        if (self.original is not None and self.last_written is not None
                and self.mem.module(CAMERA_MODULE) == self.bound
                and self.mem.unpack(PED_ZOOM_INDICATOR, "f")[0] == self.last_written):
            self.mem.write(PED_ZOOM_INDICATOR, struct.pack("<f", self.original))

    def report(self):
        available = self.bound is not None and self.mem.module(CAMERA_MODULE) == self.bound
        return {"available": available,
                "first_person": bool(self.mem.byte(self.bound["base"] + ACTIVE)) if available else None,
                "error": self.error}
