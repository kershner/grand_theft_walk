"""Launch Vice City, create/load a normal checkpoint, unlock the map, then walk."""

import hashlib
import logging
import struct
import sys
import time
from pathlib import Path

# Direct script launches must find the project before importing its packages.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


from core.controls import Keys
from core.files import atomic_json, read_json
from core.session import StartupWait, choose_save as select_save
from games.vice_city.game import Game, MENU, RuntimeChanges, SAVE_PREFIX, unlock_regions, valid_save


logger = logging.getLogger(__name__)
MAIN_MENU, NEW_GAME, LOAD, SAVE_LIST, SAVING, SAVED, PAUSE = 29, 7, 12, 15, 17, 18, 32
SCRIPT_SHA256 = "0fb3fd45b6a6dc21870cb48d9abe983510284b2b09b0c3d0eeac51d5dff338c7"


def choose_save(prefix, record=None):
    return select_save(prefix, valid_save, record)


class Startup(StartupWait):
    def __init__(self, memory, timeout):
        self.mem = memory
        self.game = Game(memory)
        self.keys = Keys(focused=memory.foreground)
        self.deadline = time.monotonic() + timeout
        self.last_skip = 0
        self.world = RuntimeChanges(memory)

    def menu(self):
        return self.mem.u32(MENU + 0xF8) if self.mem.byte(MENU + 0x38) else None

    def page(self, page, item=0, slot=None):
        if not self.mem.foreground() or self.menu() is None:
            raise RuntimeError("Expected an active foreground menu during startup.")
        self.mem.write(MENU + 0x30, struct.pack("<I", item))
        if slot is not None:
            self.mem.write(MENU + 0x100, struct.pack("<I", slot))
        self.mem.write(MENU + 0xF8, struct.pack("<I", page))

    def free_roam(self):
        page = self.menu()
        if page == PAUSE:
            self.mem.write(MENU + 0x11, b"\x01")
            return False
        if page is not None:
            return False
        state = self.game.snapshot()
        if not state.get("ready"):
            return False
        if state["cutscene"]:
            if time.monotonic() - self.last_skip >= 1.5 and self.keys.tap("enter"):
                self.last_skip = time.monotonic()
            return False
        mission = self.mem.u32(0x978748)
        if mission >= 34336:
            raise RuntimeError("Invalid Vice City mission flag offset")
        on_mission = mission and self.mem.u32(0x821280 + mission) == 1
        return state if (not on_mission and not state["paused"]
                         and not state["controls_disabled"] and not state["in_vehicle"]
                         and state["health"] > 0 and state["player_state"] == 0
                         and state["interior"] == 0) else False

    def start_new_game(self):
        page = self.menu()
        if page is None:
            return True
        if page in (MAIN_MENU, 1):
            self.page(NEW_GAME, item=2)
        elif page == NEW_GAME:
            # Entry 0 is the question, 1 is No, and 2 is Yes in the pinned menu.
            if self.mem.u32(MENU + 0x30) != 2:
                self.mem.write(MENU + 0x30, struct.pack("<I", 2))
            elif time.monotonic() - self.last_skip >= 1:
                if self.keys.tap("enter"):
                    self.last_skip = time.monotonic()
        else:
            raise RuntimeError(f"Unexpected new-game screen: {page}")
        return False

    def load_save(self, slot):
        self.page(LOAD, slot=slot)
        stable_at = None
        stable_clock = None

        def loaded():
            nonlocal stable_at, stable_clock
            state = self.free_roam()
            if (not state or self.mem.byte(MENU + 0x39)
                    or self.mem.byte(MENU + 0x3C)):
                stable_at = stable_clock = None
                return False
            now = time.monotonic()
            if stable_at is None or state["game_time_ms"] < stable_clock:
                stable_at, stable_clock = now, state["game_time_ms"]
            return state if (now - stable_at >= 1
                             and state["game_time_ms"] - stable_clock >= 500) else False

        return self.wait("Loading the normal walker save...", loaded)

    def prepare(self, state_dir):
        script = self.mem.exe.parent / "data/main.scm"
        if hashlib.sha256(script.read_bytes()).hexdigest() != SCRIPT_SHA256:
            raise RuntimeError("Unsupported Vice City opening script; refusing to prepare a save.")

        try:
            self.wait_for_title(MAIN_MENU)
            # The pinned Widescreen Fix uses index 1 for 30 FPS. Keep the native
            # physics cap on even when a previous user settings file disabled it.
            self.world.patch(MENU + 0x25, b"\x01")
            prefix = self.mem.read(SAVE_PREFIX, 260).split(b"\0", 1)[0].decode("mbcs")
            if not prefix or not Path(prefix).is_absolute() or Path(prefix).name.casefold() != "gtavcsf":
                raise RuntimeError("Vice City did not provide a valid normal save folder.")
            record_path = state_dir / "launch.json"
            record = read_json(record_path) if record_path.exists() else None
            slot, path, create = choose_save(prefix, record)
            logger.info("Normal save slot %s: %s", slot + 1, path)
            if create:
                self.wait("Starting a new Vice City game...", self.start_new_game)
                self.wait("Waiting for the opening sequence's own cleanup...", self.free_roam)
                self.wait_for_free_roam()
                barriers = unlock_regions(self.game, self.world)
                logger.info("Opened %s island roadblocks and gates.", barriers)
                if path.exists():
                    raise RuntimeError("The selected save slot became occupied; no save was overwritten.")
                self.mem.write(MENU + 0x3B, b"\x01")
                self.wait("Opening the game's normal save menu...", lambda: self.menu() == SAVE_LIST)
                if path.exists():
                    raise RuntimeError("The selected save slot became occupied; no save was overwritten.")
                self.page(SAVING, slot=slot)

                def saved():
                    page = self.menu()
                    if page not in (SAVING, SAVED):
                        raise RuntimeError(f"Vice City's save action failed (screen {page}).")
                    return page == SAVED and valid_save(path)

                self.wait("Verifying the new normal save...", saved)
                failures = self.world.restore()
                if failures:
                    raise RuntimeError("Could not restore startup changes: " + "; ".join(failures))
                self.mem.write(MENU + 0x11, b"\x01")
                self.wait("Returning to free roam...", self.free_roam)
                self.mem.write(MENU + 0x12, b"\x01")
                self.wait("Preparing to reload the new save...", lambda: self.menu() is not None)
            state = self.load_save(slot)
            self.world.patch(MENU + 0x25, b"\x01")
            # A loaded checkpoint must also have full map access before navigation.
            barriers = unlock_regions(self.game, self.world)
            logger.info("Opened %s island roadblocks and gates after load.", barriers)
            atomic_json(record_path, {
                "slot": slot, "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "loaded_identity": state["identity"], "loaded_game_time_ms": state["game_time_ms"],
                "loaded_position": state["position"],
                "opened_barriers": barriers,
            })
            return path
        except BaseException:
            if self.mem.alive():
                self.world.restore()
            raise
        finally:
            self.keys.release()


def main(argv=None):
    from core.session import launch

    launch("vice_city", Startup, argv)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Grand Theft Walk stopped")
        raise SystemExit(1)
