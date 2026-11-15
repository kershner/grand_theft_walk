"""San Andreas 1.0 runtime profile. See docs/SOURCES.txt for the verified layouts."""
import math
import struct

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
ENTITY_POOLS = (("buildings", 0xB74498, 0x38), ("dummies", 0xB744A0, 0x38),
                ("objects", 0xB7449C, 0x19C))  # Object pool uses CCutsceneObject storage.
CHEAT_IDS = {"health": 61, "oxygen": 62, "no_wanted": 65, "no_hunger": 68, "ammo": 72}


class Game:
    def __init__(self, memory):
        self.mem = memory

    def pool(self, global_address, stride):
        pointer = self.mem.u32(global_address)
        objects, bitmap, size = self.mem.unpack(pointer, "IIi")
        if not 0 < size <= 50000 or not 0x10000 < objects < 0x80000000 or not 0x10000 < bitmap < 0x80000000:
            raise RuntimeError(f"Invalid pool at {global_address:#x}")
        return objects, self.mem.read(bitmap, size), size, stride

    def ipls(self):
        objects, bitmap, size, stride = self.pool(IPL_POOL, 0x34)
        raw = self.mem.read(objects, size * stride)
        return [{"slot": i, "name": raw[i*stride+0x10:i*stride+0x20].split(b"\0",1)[0].decode("ascii",errors="replace"),
                 "loaded": bool(raw[i*stride+0x2D]), "dynamic_disabled": bool(raw[i*stride+0x2F])}
                for i in range(size) if not bitmap[i] & 0x80]

    def barrier_entities(self):
        slots = {e["slot"] for e in self.ipls() if e["name"].lower() in {"barriers1", "barriers2"}}
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
                    result.append({"kind": kind, "address": objects + start,
                                   "identity": raw[start+0x20:start+0x24].hex(),
                                   "ipl": raw[start+0x2E], "flags": raw[start+0x1C],
                                   "pool_map": self.mem.u32(self.mem.u32(global_address)+4)+i,
                                   "generation": bitmap[i]})
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
            block = raw[i * stride:(i + 1) * stride]
            flags = struct.unpack_from("<H", block, 0x30)[0]
            bounds = struct.unpack_from("<4f", block, 8)
            if (flags & 0x8000 or not all(math.isfinite(v) for v in bounds)
                    or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]):
                continue  # Deleted or nonphysical placeholder entrances.
            address = objects + i * stride
            result.append({"address": address, "flags": flags,
                           "name": block[:8].split(b"\0", 1)[0].decode("ascii", errors="replace"),
                           "hours": tuple(block[0x34:0x36]),
                           "guard": ("enex", pointer, objects, map_address, i,
                                     generation, block[:0x30].hex())})
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
            block = raw[i * stride:(i + 1) * stride]
            kind = struct.unpack_from("<I", block, 0x598)[0]
            if kind < 4 or kind >= 32:
                continue  # Never modify player ped relationships.
            result.append({"address": objects + i * stride,
                           "guard": ("npc", pointer, objects, map_address, i,
                                     generation, block[0x20:0x24].hex(), kind)})
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
        position = self.mem.unpack(matrix+0x30, "3f") if matrix else struct.unpack_from("<3f", block, 4)
        health, max_health, armor = struct.unpack_from("<3f", block, 0x540)
        heading = struct.unpack_from("<f", block, 0x558)[0]
        camera_x, camera_y, _ = self.mem.unpack(0xB6F028 + 0x974 + 0x10, "3f")
        camera_heading = (math.atan2(-camera_x, camera_y)
                          if math.isfinite(camera_x) and math.isfinite(camera_y)
                          and camera_x * camera_x + camera_y * camera_y > .01 else None)
        if not all(math.isfinite(v) for v in (*position, health, max_health, heading)) or any(abs(v) > 20000 for v in position):
            raise RuntimeError("Player state failed validation; refusing to use this memory profile.")
        data = struct.unpack_from("<I", block, 0x480)[0]
        wanted = self.mem.u32(data) if data else 0
        ped_state, move_state = struct.unpack_from("<II", block, 0x530)
        in_vehicle = bool(block[0x46D] & 1)
        paused = any(self.mem.read(0xB7CB48, 2))
        return {
            "ready": True, "pid": self.mem.pid, "ped": ped, "player_info": info,
            "identity": block[0x20:0x24].hex(), "player_data": data,
            "position": dict(zip(("x", "y", "z"), position)),
            "heading": heading, "camera_heading": camera_heading,
            "health": health, "max_health": max_health, "armor": armor,
            "interior": block[0x2F], "in_vehicle": in_vehicle,
            "vehicle": struct.unpack_from("<I", block, 0x58C)[0] if in_vehicle else None,
            "ped_state": ped_state, "move_state": move_state,
            "wanted_pointer": wanted, "wanted_level": self.mem.u32(wanted+0x2C) if wanted else None,
            "player_state": self.mem.byte(info+0xDC), "paused": paused,
            "controls_disabled": bool(self.mem.unpack(0xB73458+0x10E, "H")[0]),
            "foreground": self.mem.foreground(), "game_time_ms": self.mem.u32(0xB7CB84),
            "cities_unlocked": self.mem.u32(CITIES_UNLOCKED),
            "map_tiles_revealed": self.mem.u32(ZONES_REVEALED),
            "proof_flags": block[0x42],
            "gang_wars_active": bool(self.mem.byte(GANG_WARS_ACTIVE)),
            "cheats": {name: bool(self.mem.byte(CHEATS+index)) for name,index in CHEAT_IDS.items()},
        }


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
            del self.entries[key]  # Allocation was reused; its old bits are no longer ours.
        if key not in self.entries:
            self.entries[key] = {"address": address, "before": before, "after": after, "mask": mask, "guard": guard}
        value = bytes((old & (~bits & 255)) | (new & bits) for old,new,bits in zip(before,after,mask))
        if before != value:
            self.mem.write(address, value)

    def valid_guard(self, guard):
        if guard is None:
            return True
        if guard[0] == "ped":
            _, info, ped, identity = guard
            return self.mem.u32(info) == ped and self.mem.read(ped+0x20,4).hex() == identity
        if guard[0] == "enex":
            _, pointer, objects, bitmap, slot, generation, identity = guard
            return (self.mem.u32(ENTRY_EXIT_POOL) == pointer
                    and self.mem.u32(pointer) == objects
                    and self.mem.u32(pointer + 4) == bitmap
                    and self.mem.byte(bitmap + slot) == generation
                    and self.mem.read(objects + slot * ENTRY_EXIT_SIZE, 0x30).hex() == identity)
        if guard[0] == "relationships":
            return self.mem.u32(RELATIONSHIP_TABLE) == guard[1]
        if guard[0] == "npc":
            _, pointer, objects, bitmap, slot, generation, identity, kind = guard
            address = objects + slot * PED_SIZE
            return (self.mem.u32(PED_POOL) == pointer
                    and self.mem.u32(pointer) == objects and self.mem.u32(pointer + 4) == bitmap
                    and self.mem.byte(bitmap + slot) == generation
                    and self.mem.read(address + 0x20, 4).hex() == identity
                    and self.mem.u32(address + 0x598) == kind)
        _, address, identity, ipl, map_address, generation = guard
        return (self.mem.byte(map_address) == generation and
                self.mem.read(address+0x20,4).hex() == identity and self.mem.byte(address+0x2E) == ipl)

    def restore(self):
        failures = []
        for entry in reversed(list(self.entries.values())):
            try:
                if not self.valid_guard(entry["guard"]):
                    continue
                current = self.mem.read(entry["address"], len(entry["before"]))
                value = bytes((now & (~mask & 255)) | (old & mask)
                              for now,old,mask in zip(current, entry["before"], entry["mask"]))
                self.mem.write(entry["address"], value)
            except OSError as error:
                failures.append(str(error))
        self.entries.clear()
        return failures


def apply_protection(game, changes, state):
    mem = game.mem
    guard = ("ped", state["player_info"], state["ped"], state["identity"])
    for name in CHEAT_IDS:
        changes.patch(CHEATS+CHEAT_IDS[name], b"\x01")
    # Bullet/fire/collision/melee/invulnerable/explosion proofs; preserve other physical bits.
    changes.patch(state["ped"]+0x42, b"\xFC", b"\xFC", guard)
    # Prevent critical headshots and disable the explicit player-damage permission.
    changes.patch(state["ped"]+0x471, b"\x10", b"\x10", guard)
    changes.patch(state["player_data"]+0x34, b"\0", b"\x10", guard)
    changes.patch(state["player_info"]+0x14C, b"\x01")  # Does not get tired.
    changes.patch(state["player_info"]+0x14E, b"\x01")  # Player-info fire immunity.
    # Desired gameplay values, not reversible settings: don't resurrect or restore old health.
    if 0 < state["health"] < max(100., state["max_health"]):
        mem.write(state["ped"]+0x540, struct.pack("<f", max(100., state["max_health"])))
    if state["wanted_pointer"]:
        wanted = state["wanted_pointer"]
        for offset in (0, 4, 0x2C, 0x30):
            if mem.u32(wanted+offset):
                mem.write(wanted+offset, b"\0"*4)
    if mem.u32(state["player_info"]+0x144):
        mem.write(state["player_info"]+0x144, b"\0"*4)


def bind_walk_key(game, changes):
    keys = game.mem.read(CONTROL_ACTIONS, 59 * 0x20)
    walk_offset = WALK_ACTION * 0x20
    if struct.unpack_from("<I", keys, walk_offset)[0] > 1056:
        raise RuntimeError("Invalid walk-control binding")
    for action in range(59):
        if action == WALK_ACTION:
            continue
        for keyboard_type in (0, 1):
            offset = action * 0x20 + keyboard_type * 8
            if struct.unpack_from("<I", keys, offset)[0] == WALK_KEY:
                changes.patch(CONTROL_ACTIONS + offset, struct.pack("<I", 1056))
    changes.patch(CONTROL_ACTIONS + walk_offset, struct.pack("<I", WALK_KEY))
    if not struct.unpack_from("<I", keys, walk_offset + 4)[0]:
        changes.patch(CONTROL_ACTIONS + walk_offset + 4, struct.pack("<I", 1))


def unlock_regions(game, changes, include_barriers=True):
    changes.patch(CITIES_UNLOCKED, struct.pack("<I", 3))
    changes.patch(ZONES_REVEALED, struct.pack("<I", 100))
    changes.patch(ZONES_VISITED, b"\x01"*100)
    barriers = game.barrier_entities() if include_barriers else []
    for entity in barriers:
        guard = ("entity", entity["address"], entity["identity"], entity["ipl"],
                 entity["pool_map"], entity["generation"])
        # Keep the entity alive for scripts; hide it and disable its collision.
        changes.patch(entity["address"]+0x1C, b"\0", b"\x81", guard)
    return len(barriers)


def unlock_interiors(game, changes):
    """Open existing normal portals, preserving geometry, links and transition state."""
    entries = game.entry_exits()
    for entry in entries:
        # Set ENABLE_ACCESS; clear DISABLE_ON_FOOT and DISABLE_EXIT only.
        changes.patch(entry["address"] + 0x30, struct.pack("<H", 0x4000),
                      struct.pack("<H", 0x4880), entry["guard"])
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
            changes.patch(table + kind * 20 + offset, bytes(4), mask, ("relationships", table))
    changes.patch(GANG_WARS_ACTIVE, b"\0")
    pedestrians = game.pedestrians()
    for ped in pedestrians:
        for offset in (12, 16):
            changes.patch(ped["address"] + 0x4E0 + offset, bytes(4), mask, ped["guard"])
    return len(pedestrians)
