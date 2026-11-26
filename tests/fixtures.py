"""Shared in-memory games, native-save bridges, and checkpoint fixtures."""

import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.classic import SAVE_SIZE
from games import GAMES, GameProfile


class StateTestCase(unittest.TestCase):
    def run(self, result=None):
        with tempfile.TemporaryDirectory() as folder:
            with patch('core.files.DATABASE', Path(folder) / 'state.sqlite3'):
                return super().run(result)


def legacy_game_profile():
    return patch.dict(GAMES, {"synthetic_legacy": GameProfile("synthetic_legacy", "GTA III", "gta3.exe", "Grand Theft Auto III")})


def checkpoint(marker=0):
    data = b"BLOCK" + bytes((marker,)) + bytes(202742)
    return data + struct.pack("<I", sum(data))


def bridge_exports(memory, base, symbols):
    """Lay out a small loaded PE32 image with movable exported data."""
    memory.write(base, b"MZ")
    memory.write(base + 0x3C, struct.pack("<I", 0x80))
    memory.write(base + 0x80, b"PE\0\0\x4c\x01")
    memory.write(base + 0x98, struct.pack("<H", 0x10B))
    memory.write(base + 0xF8, struct.pack("<II", 0x200, 0x200))
    memory.write(base + 0x214, struct.pack("<5I", len(symbols), len(symbols), 0x240, 0x260, 0x280))
    for i, (name, target) in enumerate(symbols.items()):
        memory.write(base + 0x240 + i * 4, struct.pack("<I", target))
        memory.write(base + 0x260 + i * 4, struct.pack("<I", 0x300 + i * 64))
        memory.write(base + 0x280 + i * 2, struct.pack("<H", i))
        memory.write(base + 0x300 + i * 64, name.encode("ascii") + b"\0")


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
