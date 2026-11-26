"""Load an isolated full-map GTA III checkpoint, then use the shared runtime."""

import logging
import struct
import sys
import time
from pathlib import Path

# Direct script launches must find the project before importing its packages.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


from core.controls import Keys
from core.files import write_state, read_json, read_state, state_exists, sha256
from core.saves import seed_checkpoint
from core.session import StartupWait, choose_save
from games.gta_iii.game import (
    Game,
    MENU,
    RuntimeChanges,
    SAVE_PREFIX,
    initialize_camera_provider,
    menu,
    on_mission,
    unlock_regions,
    valid_save,
)


MAIN_MENU, LOAD, PAUSE = 51, 14, 52
logger = logging.getLogger(__name__)
SEED_SHA256 = "3342ae68b1debfc1297c2a15ef557d34b4814ad0af99ffb38451cacffe71e06d"
SCRIPT_SHA256 = "e4f809e770418842565c86d133edf88f42c85d61a4825d1322cd70c584d17e96"


class Startup(StartupWait):
    def __init__(self, memory, timeout):
        self.mem, self.game = memory, Game(memory)
        self.keys = Keys(focused=memory.foreground)
        self.deadline = time.monotonic() + timeout
        self.last_skip = 0
        self.world = RuntimeChanges(memory)

    def menu(self):
        return menu(self.mem)

    def free_roam(self):
        if self.menu() == PAUSE:
            self.mem.write(0x95CF22, b"\x01")  # Native frontend shutdown request.
            return False
        if self.menu() is not None:
            return False
        state = self.game.snapshot()
        return state if (state.get("ready") and not state["cutscene"] and not on_mission(self.mem)
            and not state["paused"] and not state["controls_disabled"] and not state["in_vehicle"]
            and state["health"] > 0 and state["player_state"] == 0) else False

    def load_save(self, slot):
        if not self.mem.foreground() or self.menu() is None:
            raise RuntimeError("Expected an active foreground menu before loading GTA III.")
        self.mem.write(MENU + 0x55C, struct.pack("<I", slot))
        self.mem.write(MENU + 0x548, struct.pack("<I", LOAD))
        stable_at = stable_clock = None

        def loaded():
            nonlocal stable_at, stable_clock
            state = self.free_roam()
            if not state:
                stable_at = stable_clock = None
                return False
            now = time.monotonic()
            if stable_at is None or state["game_time_ms"] < stable_clock:
                stable_at, stable_clock = now, state["game_time_ms"]
            return state if now - stable_at >= 1 and state["game_time_ms"] - stable_clock >= 500 else False

        return self.wait("Loading the normal GTA III walker checkpoint...", loaded)

    def prepare(self, state_dir):
        try:
            script = self.mem.exe.parent / "data/main.scm"
            if sha256(script) != SCRIPT_SHA256:
                raise RuntimeError("Unsupported GTA III mission script")
            self.wait_for_title(MAIN_MENU)
            initialize_camera_provider(self.mem)
            self.world.patch(0x5F2D58, b"\x01")  # Native 30 FPS frame limiter.
            prefix = self.mem.read(SAVE_PREFIX, 260).split(b"\0", 1)[0].decode("mbcs")
            if not Path(prefix).is_absolute() or Path(prefix).name.casefold() != "gta3sf":
                raise RuntimeError("GTA III did not provide a valid normal save folder")
            record_path = state_dir / "launch"
            record = read_state(record_path) if state_exists(record_path) else None
            # With no owned checkpoint, reserve an empty slot instead of adopting
            # and eventually autosaving over somebody's personal game progress.
            validator = valid_save if record is not None else lambda path: False
            slot, path, create = choose_save(prefix, validator, record)
            if create:
                seed_checkpoint(self.mem.exe.parent / "GTW/walker-seed.b", path, SEED_SHA256, valid_save)
                write_state(record_path, {"slot": slot, "path": str(path),
                    "sha256": sha256(path)})
            state = self.load_save(slot)
            unlock_regions(self.game, self.world)
            self.world.patch(0x5F2D58, b"\x01")
            write_state(record_path, {"slot": slot, "path": str(path),
                "sha256": sha256(path),
                "loaded_identity": state["identity"], "loaded_game_time_ms": state["game_time_ms"],
                "loaded_position": state["position"], "full_map": True})
            return path
        except BaseException:
            if self.mem.alive():
                self.world.restore()
            raise
        finally:
            self.keys.release()


def main(argv=None):
    from core.session import launch
    launch("gta_iii", Startup, argv)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Grand Theft Walk stopped")
        raise SystemExit(1)
