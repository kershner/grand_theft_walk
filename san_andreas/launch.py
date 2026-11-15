"""Launch San Andreas, prepare its save, and start the walking controller."""

import argparse
import ctypes
import hashlib
import json
import logging
import struct
import subprocess
import sys
import time
from pathlib import Path

from controls import Keys, key_down
from game import Game
from runtime import DEFAULT_STATE_DIR, MODES, atomic_json, configure_logging
from saves import MENU, SAVE_PREFIX, valid_save
from walker import run
from winmemory import EXPECTED_EXE, Memory, elevate, game_pids, k32

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
MAIN_MENU, NEW_GAME, LOAD, SAVE_LIST, SAVING, SAVED = 34, 6, 13, 16, 18, 19
SCRIPT_SPACE = 0xA49960
INTRO_SCRIPT = SCRIPT_SPACE + 200000
INTRO_SIZE = 10682
INTRO_SHA256 = "5715d5cd355df00beae42775b1a9531f97d23c5687ab48df95779422bfc1ae6c"


def choose_save(prefix, record=None):
    paths = [Path(f"{prefix}{slot + 1}.b") for slot in range(8)]
    if record is not None:
        slot = record.get("slot")
        if type(slot) is not int or slot not in range(8):
            raise RuntimeError("Invalid walker save record.")
        path = paths[slot]
        if str(path).casefold() != str(record.get("path", "")).casefold():
            raise RuntimeError("The game's save folder changed; check runtime/launch.json.")
        if valid_save(path):
            return slot, path, False
        if path.exists():
            raise RuntimeError(f"The walker save is damaged; refusing to overwrite it: {path}")
        return slot, path, True
    for slot, path in enumerate(paths):
        if valid_save(path):
            return slot, path, False
    for slot, path in enumerate(paths):
        if not path.exists():
            return slot, path, True
    raise RuntimeError("All eight normal save slots are occupied and none passes validation.")


class Startup:
    def __init__(self, memory, timeout):
        self.mem = memory
        self.game = Game(memory)
        self.keys = Keys(focused=memory.foreground)
        self.deadline = time.monotonic() + timeout
        self.last_skip = 0
        self.bootstrap_intro = False
        self.intro_finished = False

    def wait(self, description, predicate):
        logger.info(description)
        background_at = None
        while time.monotonic() < self.deadline:
            if not self.mem.alive():
                raise RuntimeError("San Andreas exited during startup.")
            if key_down("f12"):
                raise RuntimeError("Startup cancelled with F12.")
            now = time.monotonic()
            if not self.mem.foreground():
                if background_at is None:
                    logger.info("Startup paused while the game is in the background.")
                else:
                    self.deadline += now - background_at
                background_at = now
                time.sleep(0.1)
                continue
            if background_at is not None:
                self.deadline += now - background_at
                background_at = None
                logger.info("Resuming startup.")
            result = predicate()
            if result:
                return result
            time.sleep(0.1)
        raise RuntimeError(f"Startup timed out: {description}")

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
        def title_ready():
            if self.menu() == MAIN_MENU:
                return True
            # Startup movies need Enter; CCutsceneMgr is not active yet.
            if time.monotonic() - self.last_skip >= 1.5:
                if self.keys.tap("enter"):
                    self.last_skip = time.monotonic()
            return False

        self.wait("Waiting for the game to reach its title screen...", title_ready)
        prefix = self.mem.read(SAVE_PREFIX, 260).split(b"\0", 1)[0].decode("mbcs")
        if not prefix or not Path(prefix).is_absolute():
            raise RuntimeError("The game did not provide a valid normal save folder.")
        record_path = state_dir / "launch.json"
        record = json.loads(record_path.read_text()) if record_path.exists() else None
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
            stable_at = time.monotonic()

            def stable():
                nonlocal stable_at
                if not self.free_roam():
                    stable_at = time.monotonic()
                return time.monotonic() - stable_at >= 3

            self.wait("Preparing the first normal save...", stable)
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
        atomic_json(
            record_path,
            {
                "slot": slot,
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            },
        )
        return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--game",
        type=Path,
        default=Path("C:/Program Files/Rockstar Games/Grand Theft Auto San Andreas/gta_sa.exe"),
    )
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--mode", choices=MODES, help="Override the remembered navigation mode")
    parser.add_argument(
        "--prepare-only", action="store_true", help="Create/load the save without walking"
    )
    parser.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.state_dir = args.state_dir.resolve()
    configure_logging(args.state_dir)
    mutex = k32.CreateMutexW(None, False, "Local\\GrandTheftWalkLauncher")
    if not mutex or ctypes.get_last_error() == 183:
        if mutex:
            k32.CloseHandle(mutex)
        raise RuntimeError("Grand Theft Walk is already launching or running.")
    try:
        if list(game_pids()):
            raise RuntimeError(
                "Close the existing San Andreas instance before using the full launcher."
            )
        exe = args.game.resolve()
        if hashlib.sha256(exe.read_bytes()).hexdigest() != EXPECTED_EXE:
            raise RuntimeError(
                "Unsupported game executable. Install the verified PS2-style setup first."
            )
        logger.info("Launching Grand Theft Walk... F12 cancels startup.")
        try:
            process = subprocess.Popen([str(exe)], cwd=str(exe.parent))
        except PermissionError:
            if args.elevated:
                raise
            parameters = [str(Path(__file__).resolve()), *sys.argv[1:], "--elevated"]
            elevate(parameters, ROOT)
            return
        deadline = time.monotonic() + 30
        while process.poll() is None and time.monotonic() < deadline:
            if process.pid in list(game_pids()):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("The game failed to start.")
        memory = Memory(writable=True, pid=process.pid)
        try:
            path = Startup(memory, args.timeout).prepare(args.state_dir)
        finally:
            memory.close()
        logger.info("Loaded and ready: %s", path)
        if not args.prepare_only:
            args.walk = True
            args.seconds = None
            run(args)
    finally:
        k32.CloseHandle(mutex)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        logger.exception("Grand Theft Walk stopped: %s", error)
        sys.exit(1)
