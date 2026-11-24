"""Pinned GTA III 1.1 layout; behavior runs through the shared controller."""

import math
import struct
import time
from pathlib import Path

from core.classic import (
    load_destinations as classic_destinations,
    load_zones as classic_zones,
    player_identity,
    read_text_table,
    valid_save,
)
from core.controls import JUMP_SPEED, bind_action_key, can_control
from core.files import sha256
from core.navigation import Node, PedestrianGraph
from core.saves import Autosave as SharedAutosave
from core.winmemory import Memory as ProcessMemory, RuntimeChanges as SharedChanges
from games import Capabilities


CAPABILITIES = Capabilities(protection=True, peaceful=True, unlock_map=True, walk_binding=True,
    walking_pace=True, first_person=True, respawn=True, jump=True, autosave=True, view=True, hide_hud=True)
WALK_KEYS = {"forward": "w", "back": "s"}
# FirstPersonIII applies native mouse X in 0.1-degree increments. Keep the
# shared turn limit near 24 degrees/second, with matching view nudges.
TURN_MULTIPLIER = 3
LOOK_MULTIPLIER = -3
MISSION_DEFAULTS = {"radius": 100.0, "height_tolerance": 15.0}
JUMP_ACTION, JUMP_KEY = 11, 32
PLAYER_INFO, PED_POOL, PED_SIZE = 0x9414A8, 0x8F2D14, 0x5F0
CAMERA, MENU, PAD = 0x6FACF8, 0x8F5A8C, 0x6F0360
SCRIPT_SPACE, MISSION_FLAG = 0x74B248, 0x8F1C18
SAVE_PREFIX = 0x8E2870
CONTROL_ACTIONS = 0x8F4458 + 0xF08
PATHS, COUNTS, CONNECTIONS = 0x8F690C, 0x45BD8, 0x3434A
MAX_NODES, MAX_CONNECTIONS = 4930, 10260
INDUSTRIAL_PASSED, COMMERCIAL_PASSED = 0x8E2A18, 0x8F43E8
WEATHER = ("clear skies", "cloudy", "rainy", "foggy")


def read_game_text(path):
    return read_text_table(Path(path).read_bytes())

def load_zones(game_directory, names):
    return classic_zones(Path(game_directory) / "data/gta3.zon", names)


def load_destinations(directory, graph, read_text):
    # III's native pedestrian network has separate island/neighborhood groups.
    # Keep their named destinations; shared mission voting filters each poll by
    # reachability from the actual player instead of assuming one citywide SCC.
    return classic_destinations(directory, graph, read_text, load_zones, "Liberty City", network=set(graph.nodes))


def menu(memory):
    return memory.u32(MENU + 0x548) if memory.byte(MENU + 0x111) else None


def on_mission(memory):
    offset = memory.u32(MISSION_FLAG)
    if offset >= 163840 or offset % 4 or (offset and offset < 8):
        raise ValueError("Invalid GTA III mission flag offset")
    return bool(offset and memory.u32(SCRIPT_SPACE + offset) == 1)


class Memory(ProcessMemory):
    def __init__(self, writable=False, pid=None):
        super().__init__(writable=writable, pid=pid, game="gta_iii")


class Game:
    def __init__(self, memory, zones=None):
        self.mem = memory
        self.names = read_game_text(memory.exe.parent / "TEXT/american.gxt") if zones is None else {}
        self.zones = load_zones(memory.exe.parent, self.names) if zones is None else zones

    def identity(self, ped):
        return player_identity(self.mem, ped, PED_POOL, PED_SIZE)

    def snapshot(self):
        try:
            return self._snapshot()
        except (OSError, ValueError, struct.error):
            return {"ready": False, "pid": self.mem.pid, "reason": "Waiting for the GTA III world to settle"}

    def _snapshot(self):
        mem = self.mem
        if mem.byte(MENU + 0x114) or mem.byte(MENU + 0x454):
            return {"ready": False, "pid": mem.pid, "reason": "GTA III is loading"}
        ped = mem.u32(PLAYER_INFO)
        identity = self.identity(ped) if ped else None
        if not identity:
            return {"ready": False, "pid": mem.pid, "reason": "Waiting for the GTA III player"}
        block = mem.read(ped, PED_SIZE)
        position = struct.unpack_from("<3f", block, 0x34)
        health, armor = struct.unpack_from("<2f", block, 0x2C0)
        heading = struct.unpack_from("<f", block, 0x2DC)[0]
        vertical = struct.unpack_from("<f", block, 0x80)[0]
        active = mem.byte(CAMERA + 0x76)
        if active > 2:
            raise ValueError("Invalid active camera")
        front = mem.unpack(CAMERA + 0x1A4 + active * 0x1A4 + 0x140, "3f")
        if not all(math.isfinite(v) for v in (*position, health, armor, heading, vertical, *front)):
            raise ValueError("Nonfinite player state")
        if mem.u32(PLAYER_INFO) != ped or self.identity(ped) != identity:
            raise ValueError("Player allocation changed")
        wanted = struct.unpack_from("<I", block, 0x53C)[0]
        water = bool(block[0x54] & 8 or block[0x122] & 8)
        return {
            "ready": True, "pid": mem.pid, "ped": ped, "identity": identity,
            "player_info": PLAYER_INFO, "position": dict(zip(("x", "y", "z"), position)),
            "heading": heading, "camera_heading": math.atan2(-front[0], front[1]),
            "camera_pitch": math.atan2(front[2], math.hypot(front[0], front[1])),
            "standing": bool(block[0x154] & 1), "in_air": bool(block[0x155] & 8),
            "in_water": water, "drowning": water, "vertical_speed": vertical,
            "health": health, "armor": armor, "max_health": 100, "interior": 0,
            "in_vehicle": bool(block[0x314]), "player_state": mem.byte(PLAYER_INFO + 0xD8),
            "ped_state": struct.unpack_from("<I", block, 0x224)[0],
            "move_state": struct.unpack_from("<I", block, 0x22C)[0],
            "wanted_pointer": wanted, "wanted_level": mem.u32(wanted + 0x18) if wanted else None,
            "paused": bool(mem.byte(0x95CF34) or mem.byte(0x95CF69)),
            "controls_disabled": bool(mem.byte(PAD + 0xDF)), "foreground": mem.foreground(),
            "background_control": getattr(mem, "background_control", False),
            "game_time_ms": mem.u32(0x885AF8), "cutscene": bool(mem.byte(0x95CEAD)),
        }

    def current(self, state):
        return bool(state.get("ready") and self.mem.u32(PLAYER_INFO) == state["ped"]
                    and self.identity(state["ped"]) == state["identity"])

    def kill_player(self, state):
        if not self.current(state) or self.mem.byte(PLAYER_INFO + 0xD8):
            return False
        health = self.mem.unpack(state["ped"] + 0x2C0, "f")[0]
        if not math.isfinite(health) or health <= 0:
            return False
        self.mem.write(state["ped"] + 0x2C0, struct.pack("<f", 0))
        return True

    def boost_jump(self, state):
        if not self.current(state) or state.get("standing") or not state.get("in_air"):
            return False
        ped = state["ped"]
        speed = self.mem.unpack(ped + 0x80, "f")[0]
        if (self.mem.byte(ped + 0x154) & 1 or not self.mem.byte(ped + 0x155) & 8
                or not math.isfinite(speed) or speed <= 0):
            return False
        self.mem.write(ped + 0x80, struct.pack("<f", JUMP_SPEED))
        return True

    def location(self, state):
        point = tuple(state["position"][a] for a in ("x", "y", "z"))
        names = [name for name, b in self.zones if all(b[i] <= point[i] <= b[i + 3] for i in range(3))]
        hour, minute = self.mem.byte(0x95CF5F), self.mem.byte(0x95CF80)
        weather = self.mem.unpack(0x95CE28, "h")[0]
        compass = ("north", "northwest", "west", "southwest", "south", "southeast", "east", "northeast")
        return {"places": names[:1] + ["Liberty City"],
            "time": f"{hour % 12 or 12}:{minute:02} {'AM' if hour < 12 else 'PM'}",
            "period": "night" if hour < 6 or hour >= 20 else "morning" if hour < 12 else "afternoon" if hour < 18 else "evening",
            "weather": WEATHER[weather] if 0 <= weather < len(WEATHER) else "weather unavailable",
            "heading": compass[round(state["heading"] / (math.pi / 4)) % 8], "indoors": False}


class RuntimeChanges(SharedChanges):
    def valid_guard(self, guard):
        if guard is None:
            return True
        kind, ped, identity, *wanted = guard
        return (kind in ("ped", "wanted") and self.mem.u32(PLAYER_INFO) == ped
            and player_identity(self.mem, ped, PED_POOL, PED_SIZE) == identity
            and (not wanted or self.mem.u32(ped + 0x53C) == wanted[0]))


def apply_protection(game, changes, state):
    if not game.current(state) or state.get("in_water") or game.mem.byte(PLAYER_INFO + 0xD8):
        return
    ped, mem = state["ped"], game.mem
    health = mem.unpack(ped + 0x2C0, "f")[0]
    if not math.isfinite(health) or health <= 0:
        return
    guard = ("ped", ped, state["identity"])
    changes.patch(ped + 0x52, b"\x02", b"\x02", guard)
    changes.patch(ped + 0x53, b"\x1f", b"\x1f", guard)
    changes.patch(ped + 0x15C, b"\x02", b"\x02", guard)
    changes.patch(PLAYER_INFO + 0x114, b"\x01")
    if health < 100:
        mem.write(ped + 0x2C0, struct.pack("<f", 100))
    wanted = mem.u32(ped + 0x53C)
    if wanted:
        guard = ("wanted", ped, state["identity"], wanted)
        changes.patch(wanted + 0xC, struct.pack("<f", 0), guard=guard)
        for offset in (0, 0x18, *(0x1C + i * 0x1C for i in range(16))):
            if changes.valid_guard(guard) and mem.u32(wanted + offset):
                mem.write(wanted + offset, b"\0" * 4)


def apply_peaceful_mode(game, changes):
    """Let native AI abandon attacks on the player, including existing pursuits."""
    state = game.snapshot()
    if not game.current(state):
        return 0
    wanted = state["wanted_pointer"]
    if not wanted:
        return 0
    # CWanted::m_bIgnoredByEveryone, not merely zero wanted stars. Native
    # kill-character AI checks this bit and idles even for hostile gang peds.
    # Keep other wanted flags and guard against respawn/reused allocations.
    guard = ("wanted", state["ped"], state["identity"], wanted)
    changes.patch(wanted + 0x16, b"\x02", b"\x02", guard)
    return 0  # This player-level switch does not rewrite individual NPCs.


def bind_walk_key(game, changes):
    for action, key in ((3, ord("W")), (4, ord("S"))):
        bind_action_key(game.mem, changes, CONTROL_ACTIONS, 41, action, key)


def bind_jump_key(game, changes):
    bind_action_key(game.mem, changes, CONTROL_ACTIONS, 41, JUMP_ACTION, JUMP_KEY)


class WalkingPace:
    def __init__(self, memory, multiplier):
        if not math.isfinite(multiplier) or not 0.1 <= multiplier <= 1:
            raise ValueError("Walking speed multiplier must be between 0.1 and 1.0")
        self.mem, self.limit = memory, multiplier
        self.game = Game(memory)

    def tick(self, state, moving):
        if moving and self.game.current(state):
            address = state["ped"] + 0x544
            speed = self.mem.unpack(address, "f")[0]
            if self.limit < speed <= 3:
                self.mem.write(address, struct.pack("<f", self.limit))


def read_graph(data):
    if len(data) < COUNTS + 12:
        raise ValueError("Truncated GTA III path graph")
    total, cars, peds = struct.unpack_from("<3I", data, COUNTS)
    if total != cars + peds or not 0 < total <= MAX_NODES:
        raise ValueError("GTA III pedestrian graph is not initialized")
    nodes = []
    for index in range(cars, total):
        at = index * 32
        point = struct.unpack_from("<3f", data, at)
        first, count, flags = struct.unpack_from("<HBB", data, at + 24)
        if not all(math.isfinite(v) for v in point) or first + count > MAX_CONNECTIONS:
            raise ValueError("Invalid GTA III pedestrian links")
        if flags & 0x18:
            continue
        links = []
        for link in range(first, first + count):
            target = struct.unpack_from("<H", data, CONNECTIONS + link * 2)[0]
            if cars <= target < total:
                crossing = bool(data[0x3E39A + link] & 1)
                links.append(((0, target), crossing))
        nodes.append(Node((0, index), point, tuple(links)))
    # III uses longer native links on bridges and steep Shoreside footpaths.
    graph = PedestrianGraph(nodes, max_link_distance=125, max_link_height=40)
    if not graph.nodes:
        raise ValueError("No connected GTA III pedestrian paths found")
    return graph


def load_graph(memory):
    return read_graph(memory.read(PATHS, COUNTS + 12))


def unlock_regions(game, changes):
    # Native story progress opens Callahan Bridge, Shoreside's lift bridge and
    # the Porter Tunnel through the original script. Never fake mission flags
    # or remove bridge geometry underneath the pedestrian graph.
    if not game.mem.u32(INDUSTRIAL_PASSED) or not game.mem.u32(COMMERCIAL_PASSED):
        raise ValueError("GTA III needs the full-map walker checkpoint; run its normal launcher first.")
    return 0


def unlock_interiors(game, changes):
    return 0


HUD_VISIBLE = 0x95CF41


def hide_hud(game, changes):
    # CHud::Draw gates health, armour, weapon, clock, money, stars and radar on
    # this flag. It does not gate the frontend, so pause/load menus keep working.
    changes.patch(HUD_VISIBLE, b"\0")


class Autosave(SharedAutosave):
    save_prefix = SAVE_PREFIX
    valid_save = staticmethod(valid_save)

    def safe(self, state):
        page = menu(self.mem)
        pause = page == 52 and state.get("paused")
        return bool(state.get("ready") and state["player_state"] == 0 and state["health"] > 0
            and not state.get("in_water") and not state["in_vehicle"] and state["ped_state"] in (0, 1)
            and state["standing"] and not state["in_air"] and (page is None or pause)
            and (not state["paused"] or pause) and (not state["controls_disabled"] or pause)
            and not self.mem.byte(MENU + 0x114) and not self.mem.byte(MENU + 0x454)
            and not state.get("cutscene") and not on_mission(self.mem))


CAMERA_MODULE = "FirstPersonIII.asi"
CAMERA_MODULE_SHA256 = "95e4297221206e8be734f9defa074522fd441ce25df57cfd9e519835d5039327"
CAMERA_ACTIVE = 0x334445
PED_ZOOM_INDICATOR = CAMERA + 0x160


def initialize_camera_provider(memory):
    """Bind the shipped provider's two null static references before world load.

    The shipped provider leaves these references null with the pinned 1.1
    executable and loader; bind only null values at the title screen.
    The original constructor constants are verified against the pinned binary.
    """
    module = memory.module(CAMERA_MODULE)
    if (module is None or module["size"] <= 0x355948
            or sha256(module["path"]) != CAMERA_MODULE_SHA256):
        raise RuntimeError("GTA III's pinned first person provider is missing or changed")
    bindings = {0x355940: CAMERA, 0x355944: 0x8F1C38}
    current = {offset: memory.u32(module["base"] + offset) for offset in bindings}
    if any(value not in (0, bindings[offset]) for offset, value in current.items()):
        raise RuntimeError("Unexpected GTA III camera provider reference")
    for offset, address in bindings.items():
        if not current[offset]:
            memory.write(module["base"] + offset, struct.pack("<I", address))


class FirstPersonCamera:
    """Use the provider's native 4 -> 1 camera cycle, including its cleanup."""
    def __init__(self, memory):
        self.mem = memory
        self.bound = self.original = None
        self.phase = None
        self.error = None

    def apply(self, enabled, rotate_player=True):
        try:
            module = self.mem.module(CAMERA_MODULE)
            if module is None:
                raise RuntimeError("GTA III first person mod is not loaded")
            if module != self.bound:
                if (module["size"] <= CAMERA_ACTIVE or
                        sha256(module["path"]) != CAMERA_MODULE_SHA256):
                    raise RuntimeError("Unsupported GTA III first person mod")
                self.bound = module
                self.original = self.phase = None
            if not can_control(Game(self.mem, zones=[]).snapshot()):
                return False
            active = self.mem.byte(module["base"] + CAMERA_ACTIVE)
            if active not in (0, 1):
                raise RuntimeError("Invalid first person state")
            current = self.mem.unpack(PED_ZOOM_INDICATOR, "f")[0]
            if current not in (0, 1, 2, 3, 4):
                raise RuntimeError("Invalid pedestrian camera selector")
            if self.original is None:
                self.original = current
            if bool(active) != enabled:
                target = 1.0 if enabled and self.phase == "armed" else 4.0
                self.mem.write(PED_ZOOM_INDICATOR, struct.pack("<f", target))
                self.phase = "armed" if target == 4 else None
            else:
                self.phase = None
            self.error = None
            return True
        except (OSError, ValueError, RuntimeError) as error:
            self.error = str(error)
            return False

    def close(self):
        if self.bound is None or self.mem.module(CAMERA_MODULE) != self.bound:
            return
        if self.mem.byte(self.bound["base"] + CAMERA_ACTIVE):
            self.mem.write(PED_ZOOM_INDICATOR, struct.pack("<f", 4))
            deadline = time.monotonic() + 1
            while self.mem.alive() and time.monotonic() < deadline:
                if not self.mem.byte(self.bound["base"] + CAMERA_ACTIVE):
                    break
                time.sleep(0.02)
        if self.original is not None and not self.mem.byte(self.bound["base"] + CAMERA_ACTIVE):
            self.mem.write(PED_ZOOM_INDICATOR, struct.pack("<f", self.original))

    def report(self):
        available = self.bound is not None and self.mem.module(CAMERA_MODULE) == self.bound
        return {"available": available, "first_person": bool(self.mem.byte(self.bound["base"] + CAMERA_ACTIVE)) if available else None,
                "error": self.error}
