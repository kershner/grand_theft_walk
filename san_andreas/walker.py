"""Live walking controller, navigation modes, hotkeys, and runtime game settings."""

import argparse
import ctypes
import json
import logging
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gtw.controller import Controller
from gtw.navigation import PedestrianGraph
from gtw.runtime import (
    DEFAULT_STATE_DIR, DIRECTIONS, MODES, atomic_json, configure_logging,
    queue_movement, read_json,
)
from gtw.winmemory import Memory, api, elevate, k32, w
from san_andreas import game
from san_andreas.saves import Autosave

logger = logging.getLogger(__name__)
api(k32, "CreateMutexW", [ctypes.c_void_p, w.BOOL, w.LPCWSTR], w.HANDLE)


def request_admin(args):
    if ctypes.windll.shell32.IsUserAnAdmin():
        return False
    if args.elevated:
        raise RuntimeError("Windows did not grant access to the running game.")
    parameters = [
        str(Path(__file__).resolve()),
        "run",
        "--state-dir",
        str(args.state_dir),
        "--elevated",
    ]
    if args.walk:
        parameters.append("--walk")
    if args.seconds:
        parameters += ["--seconds", str(args.seconds)]
    if getattr(args, "mode", None) in MODES:
        parameters += ["--mode", args.mode]
    logger.info(
        "Accept the Windows administrator prompt so Python can control the running game."
    )
    elevate(parameters, Path(__file__).parent)
    return True


def run(args):
    try:
        mem = Memory(writable=True)
    except PermissionError:
        if request_admin(args):
            return
        raise
    mutex = k32.CreateMutexW(None, False, "Local\\SanAndreasWalker-" + str(mem.pid))
    if not mutex or ctypes.get_last_error() == 183:
        mem.close()
        if mutex:
            k32.CloseHandle(mutex)
        raise RuntimeError("A walker controller is already attached to this game.")
    try:
        controller = Controller(
            mem, args, backend=game,
            graph_loader=lambda memory: PedestrianGraph.load(memory.exe.parent),
            autosave_factory=Autosave,
        )
        started = time.monotonic()
        try:
            while mem.alive():
                now = time.monotonic()
                if args.seconds and now - started >= args.seconds:
                    break
                if not controller.tick(now):
                    break
                time.sleep(1 / 120)
        finally:
            controller.close()
    finally:
        k32.CloseHandle(mutex)
        mem.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=(
            "status",
            "run",
            "walk",
            "protect",
            "unlock",
            "peace",
            "autosave",
            "camera",
            "stop",
            "mode",
            "steer",
        ),
        nargs="?",
        default="status",
    )
    parser.add_argument("value", nargs="?")
    parser.add_argument(
        "--amount",
        type=int,
        choices=range(1, 101),
        default=1,
        metavar="1-100",
        help="Amount for steer commands",
    )
    parser.add_argument("--mode", choices=MODES)
    parser.add_argument("--walk", action="store_true")
    parser.add_argument("--seconds", type=float)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.state_dir = args.state_dir.resolve()
    configure_logging(args.state_dir)
    if args.command == "status":
        mem = Memory()
        try:
            state = game.Game(mem).snapshot()
            print(json.dumps(state, indent=2))
        finally:
            mem.close()
    elif args.command == "run":
        run(args)
    else:
        path = args.state_dir / "control.json"
        if not path.exists():
            raise RuntimeError("Start the controller before sending it commands.")
        settings = read_json(path)
        if args.command == "stop":
            settings["quit"] = True
        elif args.command == "mode":
            if args.value not in MODES:
                parser.error("Specify sidewalk or free.")
            settings["mode"] = args.value
        elif args.command == "camera":
            if args.value not in ("first", "third"):
                parser.error("Specify first or third.")
            settings["first_person"] = args.value == "first"
        elif args.command == "steer":
            if args.value not in DIRECTIONS | {"center", "stop"}:
                parser.error(
                    "Specify left, right, forward, back, up, down, center or stop."
                )
            queue_movement(settings, args.value, args.amount)
        else:
            if args.value not in ("on", "off"):
                parser.error("Specify on or off.")
            key = {
                "walk": "walking",
                "protect": "protection",
                "unlock": "unlock_map",
                "peace": "peaceful",
                "autosave": "autosave",
            }[args.command]
            settings[key] = args.value == "on"
        atomic_json(path, settings)
        logger.info("Requested: %s", settings)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Controller stopped")
        sys.exit(1)
