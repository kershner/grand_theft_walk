"""Launch San Andreas, prepare its save, and start the walking controller."""

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
from core.files import write_state, read_json, read_state, state_exists
from core.session import StartupWait, choose_save as select_save
from games.san_andreas.game import Game, MENU, SAVE_PREFIX, valid_save


logger = logging.getLogger(__name__)

MAIN_MENU, NEW_GAME, LOAD, SAVE_LIST, SAVING, SAVED = 34, 6, 13, 16, 18, 19
SCRIPT_SPACE = 0xA49960
INTRO_SCRIPT = SCRIPT_SPACE + 200000
INTRO_SIZE = 10682
INTRO_SHA256 = "5715d5cd355df00beae42775b1a9531f97d23c5687ab48df95779422bfc1ae6c"


def choose_save(prefix, record=None):
    return select_save(prefix, valid_save, record)


class Startup(StartupWait):
    def __init__(self, memory, timeout):
        self.mem = memory
        self.game = Game(memory)
        self.keys = Keys(focused=memory.foreground)
        self.deadline = time.monotonic() + timeout
        self.last_skip = 0
        self.bootstrap_intro = False
        self.intro_finished = False

    def menu(self):
        return self.mem.byte(MENU + 0x15D) if self.mem.byte(MENU + 0x5C) else None

    def page(self, page, item=0, slot=None):
        if not self.mem.byte(MENU + 0x5C):
            raise RuntimeError("Expected an active game menu during startup.")
        self.mem.write(MENU + 0x54, struct.pack("<I", item))
        if slot is not None:
            self.mem.write(MENU + 0x15F, bytes([slot]))
        # Publish the action last. The game processes it on its own main thread.
        self.mem.write(MENU + 0x15D, bytes([page]))

    def free_roam(self):
        page = self.menu()
        if page == 41:
            # The game can open Pause automatically after a focus transition.
            self.mem.write(MENU + 0x32, b"\x01")
            return False
        if page is not None:
            return False
        state = self.game.snapshot()
        if not state.get("ready"):
            return False
        if self.mem.byte(0xB5F851):
            # Skip only a running cutscene, never send space during walking.
            self.keys.tap("space")
            return False
        mission_offset = self.mem.u32(0xA476AC)
        if mission_offset >= 200000:
            raise RuntimeError("Invalid mission flag offset.")
        on_mission = mission_offset and self.mem.u32(SCRIPT_SPACE + mission_offset) == 1
        if (
            on_mission
            and self.bootstrap_intro
            and not state["paused"]
            and not state["controls_disabled"]
            and not state["in_vehicle"]
            and state["health"] > 0
            and state["player_state"] == 0
            and state["interior"] == 0
        ):
            self.finish_intro()
        return (
            state
            if not on_mission
            and not state["paused"]
            and not state["controls_disabled"]
            and state["player_state"] == 0
            and state["health"] > 0
            and not state["in_vehicle"]
            and state["interior"] == 0
            else False
        )

    def finish_intro(self):
        """Skip the opening objective through the verified script's own cleanup."""
        if self.intro_finished or not self.mem.foreground():
            return False
        script = self.mem.u32(0xA8B42C)
        seen = set()
        while script and script not in seen and len(seen) < 96:
            seen.add(script)
            data = self.mem.read(script, 0xE0)
            ip = struct.unpack_from("<I", data, 0x14)[0]
            if data[8:16].rstrip(b"\0").lower() == b"intro":
                if not (
                    data[0xC4] and data[0xDC] and INTRO_SCRIPT + 9700 <= ip < INTRO_SCRIPT + 9994
                ):
                    return False
                bytecode = self.mem.read(INTRO_SCRIPT, INTRO_SIZE)
                if hashlib.sha256(bytecode).hexdigest() != INTRO_SHA256:
                    raise RuntimeError("Unsupported opening script; refusing to skip its mission.")
                if self.mem.read(script + 8, 8) != data[8:16] or not self.mem.foreground():
                    return False
                logger.info("Finishing the opening mission for the walker...")
                # Run the script's cleanup; clearing its mission flag alone breaks saves.
                self.mem.write(script + 0x14, struct.pack("<I", INTRO_SCRIPT + 38))
                self.intro_finished = True
                return True
            script = struct.unpack_from("<I", data)[0]
        return False

    def prepare(self, state_dir):
        self.wait_for_title(MAIN_MENU)
        prefix = self.mem.read(SAVE_PREFIX, 260).split(b"\0", 1)[0].decode("mbcs")
        if not prefix or not Path(prefix).is_absolute():
            raise RuntimeError("The game did not provide a valid normal save folder.")
        record_path = state_dir / "launch"
        record = read_state(record_path) if state_exists(record_path) else None
        slot, path, create = choose_save(prefix, record)
        logger.info("Normal save slot %s: %s", slot + 1, path)
        if create:
            self.bootstrap_intro = True

            def start_new_game():
                page = self.menu()
                if page is None:
                    return True
                if page in (MAIN_MENU, 1):
                    self.page(NEW_GAME, item=2)
                elif page == NEW_GAME:
                    if self.mem.u32(MENU + 0x54) != 2:
                        self.mem.write(MENU + 0x54, struct.pack("<I", 2))
                    elif time.monotonic() - self.last_skip >= 1:
                        if self.keys.tap("enter"):
                            self.last_skip = time.monotonic()
                else:
                    raise RuntimeError(f"Unexpected new-game screen: {page}")
                return False

            self.wait("Starting a new game...", start_new_game)
            self.wait("Waiting for the opening sequence to finish...", self.free_roam)
            # Require continuous free roam before serialising script/player state.
            self.wait_for_free_roam()
            self.bootstrap_intro = False
            if path.exists():
                raise RuntimeError(
                    "The selected save slot became occupied; no save was overwritten."
                )
            # Request the game's normal save UI initialization (no manual input).
            self.mem.write(MENU + 0x5F, b"\x01")
            self.wait("Creating the normal save...", lambda: self.menu() == SAVE_LIST)
            if path.exists():
                raise RuntimeError(
                    "The selected save slot became occupied; no save was overwritten."
                )
            self.page(SAVING, slot=slot)

            def saved():
                page = self.menu()
                if page not in (SAVING, SAVED):
                    raise RuntimeError(f"The game's save action failed (screen {page}).")
                return page == SAVED and valid_save(path)

            self.wait("Verifying that the game saved successfully...", saved)
            self.mem.write(MENU + 0x32, b"\x01")
            self.wait("Returning to free roam...", self.free_roam)
            # Reload the new file too, so first launch verifies the recovery path.
            self.mem.write(MENU + 0x33, b"\x01")
            self.wait("Verifying the new save can be loaded...", lambda: self.menu() is not None)
        self.page(LOAD, slot=slot)
        self.wait("Loading the normal walker save...", self.free_roam)
        write_state(
            record_path,
            {
                "slot": slot,
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            },
        )
        return path


def main(argv=None):
    from core.session import launch

    launch("san_andreas", Startup, argv)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Grand Theft Walk stopped")
        raise SystemExit(1)
