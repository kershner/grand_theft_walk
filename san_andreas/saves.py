"""San Andreas save validation and safe-gameplay policy for shared autosave."""

import struct

from gtw.saves import Autosave as SharedAutosave, NativeSaveRequest

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
