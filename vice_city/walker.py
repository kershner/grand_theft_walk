"""Vice City entry point for the shared walking controller."""

import argparse
import ctypes
import json
import logging
import math
import time
from pathlib import Path

from gtw.controller import Controller
from gtw.games import get_game
from gtw.runtime import MODES, configure_logging
from gtw.winmemory import api, k32, w
from vice_city import game

logger = logging.getLogger(__name__)
api(k32, "CreateMutexW", [ctypes.c_void_p, w.BOOL, w.LPCWSTR], w.HANDLE)


def run(args, pid=None):
    memory = game.Memory(writable=True, pid=pid)
    mutex = k32.CreateMutexW(None, False, "Local\\ViceCityWalker-" + str(memory.pid))
    if not mutex or ctypes.get_last_error() == 183:
        memory.close()
        if mutex:
            k32.CloseHandle(mutex)
        raise RuntimeError("A walker controller is already attached to Vice City.")
    try:
        controller = Controller(memory, args, backend=game, graph_loader=game.load_graph,
                                autosave_factory=game.Autosave)
        started = time.monotonic()
        try:
            while memory.alive():
                now = time.monotonic()
                if args.seconds is not None and now - started >= args.seconds:
                    break
                if not controller.tick(now):
                    break
                time.sleep(1 / 120)
        finally:
            controller.close()
    finally:
        k32.CloseHandle(mutex)
        memory.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "run"), nargs="?", default="status")
    parser.add_argument("--state-dir", type=Path, default=get_game("vice_city").state_dir)
    parser.add_argument("--walk", action="store_true")
    parser.add_argument("--mode", choices=MODES)
    parser.add_argument("--seconds", type=float, default=60)
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or not 1 <= args.seconds <= 300:
        parser.error("Development session must last between 1 and 300 seconds.")
    args.state_dir = args.state_dir.resolve()
    configure_logging(args.state_dir, "vice_city")
    if args.command == "run":
        run(args)
    else:
        memory = game.Memory()
        try:
            print(json.dumps(game.Game(memory).snapshot(), indent=2))
        finally:
            memory.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Vice City controller stopped")
        raise SystemExit(1)
