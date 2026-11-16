"""Hash-pinned FirstPerson.asi integration using the native camera selector."""

import hashlib
import math
import struct

from gtw.controls import can_control

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
        from vice_city.game import Game

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
