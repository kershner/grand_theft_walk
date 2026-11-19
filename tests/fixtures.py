"""Shared in-memory games, native-save bridges, and checkpoint fixtures."""

import struct
from unittest.mock import patch

from core.classic import SAVE_SIZE
from games import GAMES, GameProfile


def legacy_game_profile():
    return patch.dict(GAMES, {"synthetic_legacy": GameProfile("synthetic_legacy", "GTA III", "gta3.exe", "Grand Theft Auto III")})


def checkpoint(marker=0):
    data = b"BLOCK" + bytes((marker,)) + bytes(202742)
    return data + struct.pack("<I", sum(data))


class MemoryFixture:
    def __init__(self):
        self.data = {}

    def read(self, address, length):
        return bytes(self.data.get(address + i, 0) for i in range(length))

    def write(self, address, data):
        self.data.update((address + i, value) for i, value in enumerate(data))

    def unpack(self, address, fmt):
        return struct.unpack("<" + fmt, self.read(address, struct.calcsize("<" + fmt)))

    def u32(self, address):
        return self.unpack(address, "I")[0]

    def byte(self, address):
        return self.read(address, 1)[0]


class FakeBridge:
    def __init__(self):
        self.busy = False
        self.result = None
        self.calls = []

    def start(self, slot):
        self.calls.append(slot)
        self.busy = True

    def poll(self):
        if self.result is not None:
            self.busy = False
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self):
        pass


def classic_checkpoint():
    data = bytearray()
    for _ in range(21):
        data += struct.pack("<I", 9600) + bytes(9600)
    struct.pack_into("<I", data, 68, SAVE_SIZE)
    return data + struct.pack("<I", sum(data) & 0xFFFFFFFF)
