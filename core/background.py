"""Private, watchdog-protected game input; never sends desktop input events."""

import configparser
import io
import struct
from pathlib import Path

from core.files import read_json, sha256
from core.winmemory import k32

MODULE = "GTW.BackgroundInput.asi"
WINDOW_CONFIG = "scripts/III.VC.SA.WindowedMode.cfg"
BUTTONS = {"forward": 1, "back": 2, "jump": 4, "drop": 8, "walk": 16}


def configure_window(directory):
    """Keep window geometry local; set streaming requirements before launch."""
    path = Path(directory) / WINDOW_CONFIG
    config = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    config.optionxform = str
    if path.exists():
        config.read(path, encoding="utf-8-sig")
    if not config.has_section("MAIN"):
        config.add_section("MAIN")
    required = {"WindowMode": "2", "PauseOnFocusLoss": "0", "ResumeOnFocus": "0", "ConfineCursor": "1"}
    if all(config.get("MAIN", key, fallback=None) == value for key, value in required.items()):
        return
    for key, value in required.items():
        config.set("MAIN", key, value)
    output = io.StringIO()
    config.write(output)
    path.write_text(output.getvalue(), encoding="utf-8")


class NativeInput:
    def __init__(self, memory):
        self.mem = memory
        manifest = read_json(Path(__file__).parent / "native/background.json")
        module = memory.module(MODULE)
        if not module or sha256(module["path"]) != manifest["sha256"]:
            raise RuntimeError("The background input bridge is missing or changed; run suite repair.")
        self.address = module["base"] + manifest["state_rva"]
        if manifest["state_rva"] + 40 > module["size"]:
            raise RuntimeError("Invalid background input bridge layout")
        protocol, status, game = memory.unpack(self.address, "3I")
        expected = {"san_andreas": 1, "vice_city": 2, "gta_iii": 3}[memory.profile.id]
        if (protocol, status, game) != (1, 1, expected):
            raise RuntimeError("The background input hook did not match this game; refusing desktop input fallback.")
        self.channels = []
        self.x = self.y = 0
        memory.background_control = True

    def keys(self, actions=()):
        channel = NativeKeys(self, actions)
        self.channels.append(channel)
        return channel

    def publish(self):
        bits = 0
        for channel in self.channels:
            for action in channel.held:
                bits |= BUTTONS[action]
        self.mem.write(self.address + 20, struct.pack("<3I", bits, self.x, self.y))
        self.mem.write(self.address + 16, struct.pack("<I", k32.GetTickCount() & 0xFFFFFFFF))
        self.mem.write(self.address + 12, struct.pack("<I", 1))

    def report(self):
        frames, applied = self.mem.unpack(self.address + 32, "2I")
        return {"enabled": True, "input": "native", "frames": frames, "applied_frames": applied}

    def close(self):
        self.mem.background_control = False
        if self.mem.alive():
            self.mem.write(self.address + 12, bytes(20))


class NativeKeys:
    def __init__(self, bridge, actions):
        self.bridge = bridge
        self.bindings = dict(actions) if isinstance(actions, dict) else {name: name for name in actions}
        self.held = set()

    def set(self, desired):
        desired = set(desired)
        if not desired <= self.bindings.keys() or not desired <= BUTTONS.keys():
            raise ValueError("Unknown native input action")
        self.held = desired

    def release(self, actions=None):
        self.held.difference_update(self.held.copy() if actions is None else actions)

    def move_mouse(self, horizontal, vertical):
        self.bridge.x = (self.bridge.x + int(horizontal)) & 0xFFFFFFFF
        self.bridge.y = (self.bridge.y + int(vertical)) & 0xFFFFFFFF

    def turn(self, amount):
        self.move_mouse(amount, 0)

    def look(self, amount):
        self.move_mouse(0, amount)
