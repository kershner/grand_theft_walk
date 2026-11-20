"""San Andreas 1.0 runtime profile. See docs/SOURCES.txt for the verified layouts."""

import hashlib
import logging
import math
import re
import struct
import zlib

from core.controls import bind_action_key
from core.missions import load_destinations
from core.saves import Autosave as SharedAutosave
from games import Capabilities


CAPABILITIES = Capabilities(
    protection=True, peaceful=True, unlock_map=True, walk_binding=True,
    walking_pace=True, first_person=True, respawn=True, jump=True, ledge_drop=True, autosave=True, view=True,
)

logger = logging.getLogger(__name__)

PLAYER_INFO = 0xB7CD98
CHEATS = 0x969130
CITIES_UNLOCKED = 0xB79000 + (181 - 120) * 4
ZONES_REVEALED = 0xBA372C
ZONES_VISITED = 0xBA3730
IPL_POOL = 0x8E3FB0
ENTRY_EXIT_POOL = 0x96A7D8
ENTRY_EXIT_SIZE = 0x3C
PED_POOL = 0xB74490
PED_SIZE = 0x7C4  # CPools stores CPed allocations in CCopPed-sized slots.
RELATIONSHIP_TABLE = 0xC0BBE8
GANG_WARS_ACTIVE = 0x96AB90
CONTROL_ACTIONS = 0xB70198 + 0xB70
WALK_ACTION = 17
WALK_KEY = 1002  # RenderWare rsF2.
# Verified SA 1.0 player-only upward clamp: mov [esi+0x4C], 0.25f.
JUMP_CLAMP = 0x5E91CE
JUMP_CLAMP_ORIGINAL = bytes.fromhex("c7 46 4c 00 00 80 3e")
JUMP_CLAMP_DISABLED = b"\x90" * len(JUMP_CLAMP_ORIGINAL)
CLIMB_VTABLE = 0x87059C
DROP_ACTION = 10  # VEHICLE_ENTER_EXIT also releases the native climb task.
DROP_KEY = 70  # F.
JUMP_SPEED = 0.4603  # Roughly 10% less height than the previous 0.4865-speed setting.
JUMP_ACTION = 12
JUMP_KEY = 32  # Space; F1/F2/F3 also trigger hardcoded replay actions.
SPRINT_ACTION = 13  # Also used by swimming and its native help messages.
ENTITY_POOLS = (
    ("buildings", 0xB74498, 0x38),
    ("dummies", 0xB744A0, 0x38),
    ("objects", 0xB7449C, 0x19C),
)  # Object pool uses CCutsceneObject storage.
CHEAT_IDS = {"health": 61, "oxygen": 62, "no_wanted": 65, "no_hunger": 68, "ammo": 72}

CAMERA_MODULE = "ZMenuSA.asi"
CAMERA_MODULE_SHA256 = (
    "b07eb04be443736ea2039ecb6bd4fab64c0eebc2f0e90205f2638d3466595ee1"
)
FIRST_PERSON = 0x582EF2
ROTATE_PLAYER = 0x582F38
ZONE_COUNT = 0xBA3794
ZONES = 0xBA3798
WEATHER_NAMES = (
    "clear skies",
    "clear skies",
    "smoggy",
    "smoggy",
    "cloudy",
    "clear skies",
    "clear skies",
    "cloudy",
    "rainy",
    "foggy",
    "clear skies",
    "clear skies",
    "cloudy",
    "clear skies",
    "clear skies",
    "cloudy",
    "rainy",
    "clear skies",
    "clear skies",
    "sandstorm",
    "underwater",
    "interior lighting",
    "interior lighting",
)
MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def read_game_text(path):
    data = path.read_bytes()
    if len(data) < 4 or struct.unpack_from("<HH", data) != (4, 8):
        raise ValueError("Unsupported game text format")
    chunks, offset = {}, 4
    while offset + 8 <= len(data):
        tag, size = struct.unpack_from("<4sI", data, offset)
        end = offset + 8 + size
        if end > len(data):
            raise ValueError("Truncated game text table")
        if tag in (b"TKEY", b"TDAT"):
            chunks[tag] = data[offset + 8 : end]
        offset = end
        if len(chunks) == 2:
            break
    if len(chunks) != 2:
        raise ValueError("Missing game text tables")
    keys, text = chunks[b"TKEY"], chunks[b"TDAT"]
    if len(keys) % 8:
        raise ValueError("Invalid game text keys")
    return {
        key: re.sub(
            r"~[^~]*~", "", text[start:].split(b"\0", 1)[0].decode("cp1252")
        ).strip()
        for start, key in struct.iter_unpack("<II", keys)
        if start < len(text)
    }


class Game:
    def __init__(self, memory):
        self.mem = memory
        self.place_names = None

    def location(self, state):
        if self.place_names is None:
            try:
                self.place_names = read_game_text(
                    self.mem.exe.parent / "text" / "american.gxt"
                )
            except (OSError, ValueError) as error:
                logger.warning("Place names unavailable: %s", error)
                self.place_names = {}
        position = state["position"]
        x, y, z = (position[axis] for axis in ("x", "y", "z"))
        matches = []
        count = self.mem.unpack(ZONE_COUNT, "h")[0]
        if 0 < count <= 380:
            data = self.mem.read(ZONES, count * 0x20)
            for offset in range(0, len(data), 0x20):
                x1, y1, z1, x2, y2, z2 = struct.unpack_from("<6h", data, offset + 0x10)
                if x1 <= x <= x2 and y1 <= y <= y2 and z1 <= z <= z2:
                    key = data[offset + 8 : offset + 16].split(b"\0", 1)[0]
                    name = self.place_names.get(zlib.crc32(key.upper()) ^ 0xFFFFFFFF)
                    if name:
                        matches.append((x2 - x1 + y2 - y1, name))
        places = list(dict.fromkeys(name for _, name in sorted(matches)))
        if len(places) > 1 and "San Andreas" in places:
            places.remove("San Andreas")
        minutes, hours, day, month = self.mem.read(0xB70152, 4)
        old = self.mem.unpack(0xC81320, "h")[0]
        new = self.mem.unpack(0xC8131C, "h")[0]
        blend = self.mem.unpack(0xC8130C, "f")[0]
        weather = new if blend >= 0.5 else old
        conditions = (
            WEATHER_NAMES[weather]
            if 0 <= weather < len(WEATHER_NAMES)
            else "unknown weather"
        )
        rain = self.mem.unpack(0xC81324, "f")[0]
        fog = self.mem.unpack(0xC81300, "f")[0]
        if self.mem.unpack(0xC812F4, "f")[0] > 0.1:
            conditions = "sandstorm"
        elif rain > 0.1:
            conditions = "heavy rain" if rain > 0.6 else "rain"
        elif fog > 0.1:
            conditions = "dense fog" if fog > 0.6 else "foggy"
        details = [conditions]
        if self.mem.unpack(0xC812F0, "f")[0] > 0.6:
            details.append("strong wind")
        if self.mem.unpack(0xC81308, "f")[0] > 0.1 and rain <= 0.1:
            details.append("wet roads")
        period = (
            "dawn"
            if 5 <= hours < 7
            else "morning"
            if 7 <= hours < 12
            else "afternoon"
            if 12 <= hours < 17
            else "evening"
            if 17 <= hours < 20
            else "night"
        )
        compass = (
            "north",
            "northwest",
            "west",
            "southwest",
            "south",
            "southeast",
            "east",
            "northeast",
        )
        return {
            "places": places[:3] or ["San Andreas"],
            "time": (
                f"{hours % 12 or 12}:{minutes:02} {'AM' if hours < 12 else 'PM'}"
                if hours < 24 and minutes < 60
                else None
            ),
            "date": f"{MONTHS[month - 1]} {day}"
            if 1 <= month <= 12 and 1 <= day <= 31
            else None,
            "period": period,
            "weather": ", ".join(details),
            "heading": compass[
                int((state["heading"] % math.tau) / math.tau * 8 + 0.5) % 8
            ],
            "indoors": state["interior"] != 0,
        }

    def pool(self, global_address, stride):
        pointer = self.mem.u32(global_address)
        objects, bitmap, size = self.mem.unpack(pointer, "IIi")
        if (
            not 0 < size <= 50000
            or not 0x10000 < objects < 0x80000000
            or not 0x10000 < bitmap < 0x80000000
        ):
            raise RuntimeError(f"Invalid pool at {global_address:#x}")
        return objects, self.mem.read(bitmap, size), size, stride

    def ipls(self):
        objects, bitmap, size, stride = self.pool(IPL_POOL, 0x34)
        raw = self.mem.read(objects, size * stride)
        return [
            {
                "slot": i,
                "name": raw[i * stride + 0x10 : i * stride + 0x20]
                .split(b"\0", 1)[0]
                .decode("ascii", errors="replace"),
                "loaded": bool(raw[i * stride + 0x2D]),
                "dynamic_disabled": bool(raw[i * stride + 0x2F]),
            }
            for i in range(size)
            if not bitmap[i] & 0x80
        ]

    def barrier_entities(self):
        slots = {
            e["slot"]
            for e in self.ipls()
            if e["name"].lower() in {"barriers1", "barriers2"}
        }
        result = []
        if not slots:
            return result
        for kind, global_address, stride in ENTITY_POOLS:
            objects, bitmap, size, stride = self.pool(global_address, stride)
            raw = self.mem.read(objects, size * stride)
            for i in range(size):
                if bitmap[i] & 0x80:
                    continue
                start = i * stride
                if raw[start + 0x2E] in slots:
                    result.append(
                        {
                            "kind": kind,
                            "address": objects + start,
                            "identity": raw[start + 0x20 : start + 0x24].hex(),
                            "ipl": raw[start + 0x2E],
                            "flags": raw[start + 0x1C],
                            "pool_map": self.mem.u32(self.mem.u32(global_address) + 4)
                            + i,
                            "generation": bitmap[i],
                        }
                    )
        return result

    def entry_exits(self):
        pointer = self.mem.u32(ENTRY_EXIT_POOL)
        objects, bitmap, size, stride = self.pool(ENTRY_EXIT_POOL, ENTRY_EXIT_SIZE)
        map_address = self.mem.u32(pointer + 4)
        raw = self.mem.read(objects, size * stride)
        result = []
        for i, generation in enumerate(bitmap):
            if generation & 0x80:
                continue
            block = raw[i * stride : (i + 1) * stride]
            flags = struct.unpack_from("<H", block, 0x30)[0]
            bounds = struct.unpack_from("<4f", block, 8)
            if (
                flags & 0x8000
                or not all(math.isfinite(v) for v in bounds)
                or bounds[2] <= bounds[0]
                or bounds[3] <= bounds[1]
            ):
                continue  # Deleted or nonphysical placeholder entrances.
            address = objects + i * stride
            result.append(
                {
                    "address": address,
                    "flags": flags,
                    "name": block[:8]
                    .split(b"\0", 1)[0]
                    .decode("ascii", errors="replace"),
                    "hours": tuple(block[0x34:0x36]),
                    "guard": (
                        "enex",
                        pointer,
                        objects,
                        map_address,
                        i,
                        generation,
                        block[:0x30].hex(),
                    ),
                }
            )
        return result

    def pedestrians(self):
        pointer = self.mem.u32(PED_POOL)
        objects, bitmap, size, stride = self.pool(PED_POOL, PED_SIZE)
        if size > 2000:
            raise RuntimeError("Invalid pedestrian pool size")
        map_address = self.mem.u32(pointer + 4)
        raw = self.mem.read(objects, size * stride)
        result = []
        for i, generation in enumerate(bitmap):
            if generation & 0x80:
                continue
            block = raw[i * stride : (i + 1) * stride]
            kind = struct.unpack_from("<I", block, 0x598)[0]
            if kind < 4 or kind >= 32:
                continue  # Never modify player ped relationships.
            result.append(
                {
                    "address": objects + i * stride,
                    "guard": (
                        "npc",
                        pointer,
                        objects,
                        map_address,
                        i,
                        generation,
                        block[0x20:0x24].hex(),
                        kind,
                    ),
                }
            )
        return result

    def snapshot(self):
        focus = self.mem.byte(0xB7CD74)
        if focus not in (0, 1):
            raise RuntimeError("Invalid player index.")
        info = PLAYER_INFO + focus * 0x190
        ped = self.mem.u32(info)
        if not 0x10000 < ped < 0x80000000:
            return {"ready": False, "reason": "Load a save or start a game."}
        block = self.mem.read(ped, 0x7A0)
        matrix = struct.unpack_from("<I", block, 0x14)[0]
        position = (
            self.mem.unpack(matrix + 0x30, "3f")
            if matrix
            else struct.unpack_from("<3f", block, 4)
        )
        health, max_health, armor = struct.unpack_from("<3f", block, 0x540)
        heading = struct.unpack_from("<f", block, 0x558)[0]
        camera_x, camera_y, camera_z = self.mem.unpack(0xB6F028 + 0x974 + 0x10, "3f")
        camera_heading = (
            math.atan2(-camera_x, camera_y)
            if math.isfinite(camera_x)
            and math.isfinite(camera_y)
            and camera_x * camera_x + camera_y * camera_y > 0.01
            else None
        )
        camera_pitch = (
            math.atan2(camera_z, math.hypot(camera_x, camera_y))
            if all(math.isfinite(value) for value in (camera_x, camera_y, camera_z))
            and camera_x * camera_x + camera_y * camera_y + camera_z * camera_z > 0.01
            else None
        )
        if not all(
            math.isfinite(v) for v in (*position, health, max_health, heading)
        ) or any(abs(v) > 20000 for v in position):
            raise RuntimeError(
                "Player state failed validation; refusing to use this memory profile."
            )
        data = struct.unpack_from("<I", block, 0x480)[0]
        wanted = self.mem.u32(data) if data else 0
        ped_state, move_state = struct.unpack_from("<II", block, 0x530)
        in_vehicle = bool(block[0x46D] & 1)
        paused = any(self.mem.read(0xB7CB48, 2))
        return {
            "ready": True,
            "pid": self.mem.pid,
            "ped": ped,
            "player_info": info,
            "identity": block[0x20:0x24].hex(),
            "player_data": data,
            "position": dict(zip(("x", "y", "z"), position)),
            "heading": heading,
            "camera_heading": camera_heading,
            "camera_pitch": camera_pitch,
            "standing": bool(block[0x46C] & 1),
            "in_air": bool(block[0x46D] & 2),
            "in_water": bool(block[0x41] & 1 or block[0x43] & 8),
            "vertical_speed": struct.unpack_from("<f", block, 0x4C)[0],
            "health": health,
            "max_health": max_health,
            "armor": armor,
            "interior": block[0x2F],
            "in_vehicle": in_vehicle,
            "vehicle": struct.unpack_from("<I", block, 0x58C)[0]
            if in_vehicle
            else None,
            "ped_state": ped_state,
            "move_state": move_state,
            "wanted_pointer": wanted,
            "wanted_level": self.mem.u32(wanted + 0x2C) if wanted else None,
            "player_state": self.mem.byte(info + 0xDC),
            "paused": paused,
            "cutscene": bool(self.mem.byte(0xB5F851)),
            "controls_disabled": bool(self.mem.unpack(0xB73458 + 0x10E, "H")[0]),
            "foreground": self.mem.foreground(),
            "background_control": getattr(self.mem, "background_control", False),
            "game_time_ms": self.mem.u32(0xB7CB84),
            "cities_unlocked": self.mem.u32(CITIES_UNLOCKED),
            "map_tiles_revealed": self.mem.u32(ZONES_REVEALED),
            "proof_flags": block[0x42],
            "gang_wars_active": bool(self.mem.byte(GANG_WARS_ACTIVE)),
            "cheats": {
                name: bool(self.mem.byte(CHEATS + index))
                for name, index in CHEAT_IDS.items()
            },
        }

    def kill_player(self, state):
        """Set only the current living player's health; native code handles death."""
        if not state.get("ready"):
            return False
        ped, info = state["ped"], state["player_info"]
        if (self.mem.u32(info) != ped
                or self.mem.read(ped + 0x20, 4).hex() != state["identity"]
                or self.mem.byte(info + 0xDC) != 0):
            return False
        health = self.mem.unpack(ped + 0x540, "f")[0]
        if not math.isfinite(health) or health <= 0:
            return False
        self.mem.write(ped + 0x540, struct.pack("<f", 0.0))
        return True

    def climb_task(self, ped):
        """Find a native climb on the active primary-task chain, without calls."""
        intelligence = self.mem.u32(ped + 0x47C)
        if not 0x10000 < intelligence < 0x80000000:
            return None
        tasks = self.mem.unpack(intelligence + 4, "5I")
        task = next((task for task in tasks if task), 0)
        seen = set()
        for _ in range(16):
            if not 0x10000 < task < 0x80000000 or task in seen:
                return None
            seen.add(task)
            vtable = self.mem.u32(task)
            if vtable == CLIMB_VTABLE:
                return task
            if not 0x800000 <= vtable < 0x900000:
                return None
            # All simple tasks share IsSimple(); only complex tasks have +8.
            if self.mem.u32(vtable + 12) == self.mem.u32(CLIMB_VTABLE + 12):
                return None
            task = self.mem.u32(task + 8)
        return None

    def can_drop(self, state):
        if not state.get("ready") or state.get("in_vehicle"):
            return False
        ped = state["ped"]
        return bool(
            self.mem.u32(state["player_info"]) == ped
            and self.mem.read(ped + 0x20, 4).hex() == state["identity"]
            and self.climb_task(ped)
        )

    def pull_up(self, state):
        """Ask a verified hanging task to advance through its native pull-up."""
        if not self.can_drop(state):
            return False
        task = self.climb_task(state['ped'])
        if task is None:
            return False
        # CTaskSimpleClimb: finished +8, invalid +C, anim/position stages +D/+E.
        # GRAB=1 is the hanging stage; do not interfere with an active ascent.
        flags = self.mem.read(task + 8, 8)
        if flags[0] or flags[4] or flags[5:7] != b'\x01\x01':
            return False
        if self.climb_task(state['ped']) != task or not self.can_drop(state):
            return False
        # m_HasToForceClimb follows the same animation progression as GetJump().
        # Native vault/stand-up collision tests remain responsible for clearance.
        self.mem.write(task + 0xB, b'\x01')
        return True

    def boost_jump(self, state):
        """Boost a real takeoff once, preserving horizontal velocity and gravity."""
        if not state.get("ready") or not state.get("in_air") or state.get("standing"):
            return False
        ped = state["ped"]
        if (
            self.mem.u32(state["player_info"]) != ped
            or self.mem.read(ped + 0x20, 4).hex() != state["identity"]
        ):
            return False
        if self.mem.byte(ped + 0x46C) & 1 or not self.mem.byte(ped + 0x46D) & 2:
            return False
        speed = self.mem.unpack(ped + 0x4C, "f")[0]
        if not math.isfinite(speed) or speed <= 0 or speed >= JUMP_SPEED:
            return False
        self.mem.write(ped + 0x4C, struct.pack("<f", JUMP_SPEED))
        return True


class WalkingPace:
    def __init__(self, memory, multiplier):
        if not math.isfinite(multiplier) or not 0.1 <= multiplier <= 1.0:
            raise ValueError("Walking speed multiplier must be between 0.1 and 1.0")
        self.mem = memory
        self.multiplier = multiplier
        self.owned = {}

    def associations(self, state):
        """Yield the current player's live walk associations."""
        if not state.get("ready"):
            return
        mem, ped = self.mem, state["ped"]
        if (
            mem.u32(state["player_info"]) != ped
            or mem.read(ped + 0x20, 4).hex() != state["identity"]
        ):
            return
        clump = mem.u32(ped + 0x18)
        offset = mem.u32(0xB5F878)
        if not clump or not 0 < offset < 0x1000:
            return
        data = mem.u32(clump + offset)
        if not data:
            return
        link = mem.u32(data)
        seen = set()
        while link and link != data and link not in seen:
            if len(seen) >= 64:
                raise RuntimeError("Invalid player animation list")
            seen.add(link)
            address = link - 4  # The intrusive list link follows the vtable.
            block = mem.read(address, 0x30)
            link = struct.unpack_from("<I", block, 4)[0]
            anim_id, flags = struct.unpack_from("<hH", block, 0x2C)
            if anim_id != 0 or not flags & 0x20 or flags & 0x10:
                continue  # WALK only; exclude partial, idle, run, jump, and combat anims.
            speed = block[0x24:0x28]
            value = struct.unpack("<f", speed)[0]
            if not 0 < value <= 10:
                continue
            identity = (
                state["player_info"],
                ped,
                state["identity"],
                clump,
                data,
                address,
                block[
                    0xC:0x18
                ],  # Node count, group, nodes, and hierarchy identify this anim.
            )
            yield identity, address + 0x24, speed

    def tick(self, state, moving):
        moving = moving and self.multiplier != 1.0
        if not moving and not self.owned:
            return
        owned = {}
        for identity, address, current in self.associations(state):
            previous = self.owned.get(identity)
            if moving:
                # Use the original speed, not our last write: never compound the slowdown.
                original = (
                    previous[0] if previous and current == previous[1] else current
                )
                scaled = struct.pack(
                    "<f", struct.unpack("<f", original)[0] * self.multiplier
                )
                if current != scaled:
                    self.mem.write(address, scaled)
                owned[identity] = (original, scaled)
            elif previous and current == previous[1]:
                self.mem.write(address, previous[0])
        # Deleted/replaced associations are discarded without writing to stale memory.
        self.owned = owned


class RuntimeChanges:
    """Restore only the owned bits, and only while entity identities still match."""

    def __init__(self, memory):
        self.mem = memory
        self.entries = {}

    def patch(self, address, after, mask=None, guard=None):
        before = self.mem.read(address, len(after))
        mask = bytes([255] * len(after)) if mask is None else mask
        key = (address, len(after))
        if key in self.entries and self.entries[key]["guard"] != guard:
            del self.entries[
                key
            ]  # Allocation was reused; its old bits are no longer ours.
        if key not in self.entries:
            self.entries[key] = {
                "address": address,
                "before": before,
                "after": after,
                "mask": mask,
                "guard": guard,
            }
        value = bytes(
            (old & (~bits & 255)) | (new & bits)
            for old, new, bits in zip(before, after, mask)
        )
        if before != value:
            self.mem.write(address, value)

    def valid_guard(self, guard):
        if guard is None:
            return True
        if guard[0] == "ped":
            _, info, ped, identity = guard
            return (
                self.mem.u32(info) == ped
                and self.mem.read(ped + 0x20, 4).hex() == identity
            )
        if guard[0] == "enex":
            _, pointer, objects, bitmap, slot, generation, identity = guard
            return (
                self.mem.u32(ENTRY_EXIT_POOL) == pointer
                and self.mem.u32(pointer) == objects
                and self.mem.u32(pointer + 4) == bitmap
                and self.mem.byte(bitmap + slot) == generation
                and self.mem.read(objects + slot * ENTRY_EXIT_SIZE, 0x30).hex()
                == identity
            )
        if guard[0] == "relationships":
            return self.mem.u32(RELATIONSHIP_TABLE) == guard[1]
        if guard[0] == "npc":
            _, pointer, objects, bitmap, slot, generation, identity, kind = guard
            address = objects + slot * PED_SIZE
            return (
                self.mem.u32(PED_POOL) == pointer
                and self.mem.u32(pointer) == objects
                and self.mem.u32(pointer + 4) == bitmap
                and self.mem.byte(bitmap + slot) == generation
                and self.mem.read(address + 0x20, 4).hex() == identity
                and self.mem.u32(address + 0x598) == kind
            )
        _, address, identity, ipl, map_address, generation = guard
        return (
            self.mem.byte(map_address) == generation
            and self.mem.read(address + 0x20, 4).hex() == identity
            and self.mem.byte(address + 0x2E) == ipl
        )

    def restore(self):
        failures = []
        for entry in reversed(list(self.entries.values())):
            try:
                if not self.valid_guard(entry["guard"]):
                    continue
                current = self.mem.read(entry["address"], len(entry["before"]))
                value = bytes(
                    (now & (~mask & 255)) | (old & mask)
                    for now, old, mask in zip(current, entry["before"], entry["mask"])
                )
                self.mem.write(entry["address"], value)
            except OSError as error:
                failures.append(str(error))
        self.entries.clear()
        return failures


def apply_protection(game, changes, state):
    mem = game.mem
    # Water is native gameplay: allow swimming, breath depletion and drowning.
    # Read the live flags too, in case entry happened after the last snapshot.
    if state.get("in_water") or mem.byte(state["ped"] + 0x41) & 1 or mem.byte(state["ped"] + 0x43) & 8:
        changes.restore()
        return
    guard = ("ped", state["player_info"], state["ped"], state["identity"])
    for cheat_id in CHEAT_IDS.values():
        changes.patch(CHEATS + cheat_id, b"\x01")
    # Bullet/fire/collision/melee/invulnerable/explosion proofs; preserve other physical bits.
    changes.patch(state["ped"] + 0x42, b"\xfc", b"\xfc", guard)
    # Prevent critical headshots and disable the explicit player-damage permission.
    changes.patch(state["ped"] + 0x471, b"\x10", b"\x10", guard)
    changes.patch(state["player_data"] + 0x34, b"\0", b"\x10", guard)
    changes.patch(state["player_info"] + 0x14C, b"\x01")  # Does not get tired.
    changes.patch(state["player_info"] + 0x14E, b"\x01")  # Player-info fire immunity.
    # Desired gameplay values, not reversible settings: don't resurrect or restore old health.
    if 0 < state["health"] < max(100.0, state["max_health"]):
        mem.write(
            state["ped"] + 0x540, struct.pack("<f", max(100.0, state["max_health"]))
        )
    if state["wanted_pointer"]:
        wanted = state["wanted_pointer"]
        for offset in (0, 4, 0x2C, 0x30):
            if mem.u32(wanted + offset):
                mem.write(wanted + offset, b"\0" * 4)
    if mem.u32(state["player_info"] + 0x144):
        mem.write(state["player_info"] + 0x144, b"\0" * 4)


def ensure_sprint_key(game, changes):
    """Keep native swim hints from dereferencing an unbound control label."""
    bindings = game.mem.read(CONTROL_ACTIONS, 59 * 32)
    for column in (0, 1):
        key, priority = struct.unpack_from("<II", bindings, SPRINT_ACTION * 32 + column * 8)
        if 0 < key < 1056 and priority:
            return
    used = {
        struct.unpack_from("<I", bindings, action * 32 + column * 8)[0]
        for action in range(59) for column in (0, 1)
    }
    # Choose an unused ordinary key so repairing sprint never unbinds another action.
    key = next((ord(letter) for letter in "RGHBNMYUIOPZXCVQETJKL" if ord(letter) not in used), None)
    if key is None:
        raise RuntimeError("No unused keyboard key available for sprint/swim")
    bind_action_key(game.mem, changes, CONTROL_ACTIONS, 59, SPRINT_ACTION, key)
    logger.info("Restored missing sprint/swim binding to %s", chr(key))


def bind_walk_key(game, changes):
    bind_action_key(game.mem, changes, CONTROL_ACTIONS, 59, WALK_ACTION, WALK_KEY)
    ensure_sprint_key(game, changes)


def bind_jump_key(game, changes):
    current = game.mem.read(JUMP_CLAMP, len(JUMP_CLAMP_ORIGINAL))
    if current not in (JUMP_CLAMP_ORIGINAL, JUMP_CLAMP_DISABLED):
        raise RuntimeError("Unsupported San Andreas upward-speed clamp")
    changes.patch(JUMP_CLAMP, JUMP_CLAMP_DISABLED)
    bind_action_key(game.mem, changes, CONTROL_ACTIONS, 59, JUMP_ACTION, JUMP_KEY)
    bind_action_key(game.mem, changes, CONTROL_ACTIONS, 59, DROP_ACTION, DROP_KEY)
    ensure_sprint_key(game, changes)


def unlock_regions(game, changes, include_barriers=True):
    changes.patch(CITIES_UNLOCKED, struct.pack("<I", 3))
    changes.patch(ZONES_REVEALED, struct.pack("<I", 100))
    changes.patch(ZONES_VISITED, b"\x01" * 100)
    barriers = game.barrier_entities() if include_barriers else []
    for entity in barriers:
        guard = (
            "entity",
            entity["address"],
            entity["identity"],
            entity["ipl"],
            entity["pool_map"],
            entity["generation"],
        )
        # Keep the entity alive for scripts; hide it and disable its collision.
        changes.patch(entity["address"] + 0x1C, b"\0", b"\x81", guard)
    return len(barriers)


def unlock_interiors(game, changes):
    """Open existing normal portals, preserving geometry, links and transition state."""
    entries = game.entry_exits()
    for entry in entries:
        # Set ENABLE_ACCESS; clear DISABLE_ON_FOOT and DISABLE_EXIT only.
        changes.patch(
            entry["address"] + 0x30,
            struct.pack("<H", 0x4000),
            struct.pack("<H", 0x4880),
            entry["guard"],
        )
        changes.patch(entry["address"] + 0x34, bytes((0, 24)), guard=entry["guard"])
    return len(entries)


def apply_peaceful_mode(game, changes):
    """Remove ambient dislike/hate of player types, without changing NPC factions."""
    table = game.mem.u32(RELATIONSHIP_TABLE)
    if not 0x10000 < table < 0x80000000:
        raise RuntimeError("Invalid pedestrian relationship table")
    mask = struct.pack("<I", 0xF)  # PLAYER1, PLAYER2, PLAYER_NETWORK, PLAYER_UNUSED.
    for kind in range(32):
        for offset in (12, 16):  # Dislike, hate; retain respect/like/other targets.
            changes.patch(
                table + kind * 20 + offset, bytes(4), mask, ("relationships", table)
            )
    changes.patch(GANG_WARS_ACTIVE, b"\0")
    pedestrians = game.pedestrians()
    for ped in pedestrians:
        for offset in (12, 16):
            changes.patch(ped["address"] + 0x4E0 + offset, bytes(4), mask, ped["guard"])
    return len(pedestrians)


class FirstPersonCamera:
    def __init__(self, memory):
        self.mem = memory
        self.bound = None
        self.original = None
        self.enabled = None
        self.error = None

    def apply(self, enabled, rotate_player=True):
        try:
            module = self.mem.module(CAMERA_MODULE)
            if module is None:
                raise RuntimeError("First-person mod is not installed or loaded")
            if self.bound != module:
                if (
                    module["size"] <= ROTATE_PLAYER
                    or hashlib.sha256(module["path"].read_bytes()).hexdigest()
                    != CAMERA_MODULE_SHA256
                ):
                    raise RuntimeError("Unsupported first-person mod version")
                current = tuple(
                    self.mem.byte(module["base"] + offset)
                    for offset in (FIRST_PERSON, ROTATE_PLAYER)
                )
                if any(value not in (0, 1) for value in current):
                    raise RuntimeError("Invalid first-person camera flags")
                self.bound, self.original = module, current
            base = module["base"]
            # Forcing the ped to face the camera fights the game's backward input.
            values = (
                ((ROTATE_PLAYER, int(rotate_player)), (FIRST_PERSON, 1))
                if enabled
                else ((FIRST_PERSON, 0), (ROTATE_PLAYER, self.original[1]))
            )
            for offset, value in values:
                if self.mem.byte(base + offset) != value:
                    self.mem.write(base + offset, bytes((value,)))
            self.enabled, self.error = enabled, None
            return True
        except (OSError, RuntimeError, ValueError) as error:
            self.error = str(error)
            return False

    def close(self):
        if self.bound is not None and self.mem.module(CAMERA_MODULE) == self.bound:
            for offset, value in zip((FIRST_PERSON, ROTATE_PLAYER), self.original):
                self.mem.write(self.bound["base"] + offset, bytes((value,)))

    def report(self):
        return {
            "available": self.bound is not None,
            "first_person": self.enabled,
            "error": self.error,
        }


MENU = 0xBA6748
SAVE_PREFIX = 0xC16F18


def valid_save(path):
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return False
    return (
        200000 <= len(data) <= 4 * 1024 * 1024
        and data.startswith(b"BLOCK")
        and (sum(data[:-4]) & 0xFFFFFFFF) == struct.unpack("<I", data[-4:])[0]
    )


class Autosave(SharedAutosave):
    save_prefix = SAVE_PREFIX
    valid_save = staticmethod(valid_save)

    def menu(self):
        return self.mem.byte(MENU + 0x15D) if self.mem.byte(MENU + 0x5C) else None

    def safe(self, state):
        pause_menu = self.menu() == 41 and state.get("paused")
        if not (
            state.get("ready")
            and (not state["paused"] or pause_menu)
            and (not state["controls_disabled"] or pause_menu)
            and state["player_state"] == 0
            and state["health"] > 0
            and not state.get("in_water", False)
            and not state["in_vehicle"]
            and state["interior"] == 0
            and state["ped_state"] in (0, 1)
        ):
            return False
        if (
            (self.menu() is not None and not pause_menu)
            or self.mem.byte(0xB5F851)
            or self.mem.u32(0x96A7CC)
        ):
            return False
        mission = self.mem.u32(0xA476AC)
        return mission < 200000 and not (mission and self.mem.u32(0xA49960 + mission) == 1)
