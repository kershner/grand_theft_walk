"""Game startup, controller sessions, and the development controller CLI."""

import argparse
import ctypes
import importlib
import json
import logging
import math
import subprocess
import sys
import time
from pathlib import Path

from core.controller import Controller
from core.controls import configure_window, key_down
from core.files import write_state, read_state, state_exists
from core.manager import component_status, validate_components
from core.navigation import PedestrianGraph
from core.runtime import DIRECTIONS, MODES, configure_logging, queue_movement
from core.winmemory import Memory, elevate, game_pids, process_mutex
from games import ACTIVE_GAMES, ROOT, get_game


logger = logging.getLogger(__name__)


def choose_save(prefix, valid_save, record=None):
    paths = [Path(f"{prefix}{slot + 1}.b") for slot in range(8)]
    if record is not None:
        slot = record.get("slot")
        if type(slot) is not int or slot not in range(8):
            raise RuntimeError("Invalid walker save record.")
        path = paths[slot]
        if str(path).casefold() != str(record.get("path", "")).casefold():
            raise RuntimeError("The game's save folder changed; check runtime/launch.")
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


class StartupWait:
    def wait_for_title(self, main_menu):
        def title_ready():
            if self.menu() == main_menu:
                return True
            if time.monotonic() - self.last_skip >= 1.5 and self.keys.tap("enter"):
                self.last_skip = time.monotonic()
            return False

        self.wait("Waiting for the game to reach its title screen...", title_ready)

    def wait_for_free_roam(self):
        stable_at = time.monotonic()

        def stable():
            nonlocal stable_at
            if not self.free_roam():
                stable_at = time.monotonic()
            return time.monotonic() - stable_at >= 3

        self.wait("Preparing the first normal save...", stable)

    def wait(self, description, predicate):
        logger.info(description)
        background_at = None
        while time.monotonic() < self.deadline:
            if not self.mem.alive():
                raise RuntimeError("The game exited during startup.")
            foreground = self.mem.foreground()
            if foreground and key_down("f12"):
                raise RuntimeError("Startup cancelled with F12.")
            now = time.monotonic()
            if not foreground:
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


def backend_for(game):
    return importlib.import_module(f"games.{get_game(game).id}.game")


def open_memory(game, **kwargs):
    factory = Memory if game == "san_andreas" else backend_for(game).Memory
    return factory(**kwargs)


def request_admin(args, game):
    if ctypes.windll.shell32.IsUserAnAdmin():
        return False
    if getattr(args, "elevated", False):
        raise RuntimeError("Windows did not grant access to the running game.")
    parameters = ["-m", "core.session", "--game", game, "run",
                  "--state-dir", str(args.state_dir), "--elevated"]
    if args.walk:
        parameters.append("--walk")
    if args.seconds is not None:
        parameters += ["--seconds", str(args.seconds)]
    if args.mode in MODES:
        parameters += ["--mode", args.mode]
    logger.info("Accept the Windows administrator prompt to control the running game.")
    elevate(parameters, ROOT)
    return True


def run(args, game, pid=None):
    args.background = True
    backend = backend_for(game)
    try:
        memory = open_memory(game, writable=True, pid=pid)
    except PermissionError:
        if request_admin(args, game):
            return
        raise
    try:
        name = get_game(game).id
        with process_mutex(f"Local\\{name}Walker-{memory.pid}",
                           "A walker controller is already attached to this game."):
            graph_loader = (lambda mem: PedestrianGraph.load(mem.exe.parent)) if game == "san_andreas" else backend.load_graph
            controller = Controller(memory, args, backend=backend, graph_loader=graph_loader,
                                    autosave_factory=backend.Autosave)
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
        memory.close()


def launch(game, startup_type, argv=None):
    profile = get_game(game)
    parser = argparse.ArgumentParser(description=f"Launch {profile.title} and start walking.")
    parser.add_argument("--game", type=Path, default=profile.install_dir / profile.executable)
    parser.add_argument("--state-dir", type=Path, default=profile.state_dir)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--mode", choices=MODES)
    parser.add_argument("--seconds", type=float, help="End the controller after a development test")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument('--allow-other-games', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or not 10 <= args.timeout <= 600:
        parser.error("Startup timeout must be between 10 and 600 seconds.")
    validate_seconds(parser, args.seconds)
    args.state_dir = args.state_dir.resolve()
    configure_logging(args.state_dir, game)
    launch_mutex = f'Local\\GrandTheftWalkLauncher-{game}' if args.allow_other_games else 'Local\\GrandTheftWalkLauncher'
    with process_mutex(launch_mutex, "Grand Theft Walk is already launching or running."):
        checked_games = (game,) if args.allow_other_games else ACTIVE_GAMES
        if any(list(game_pids(other)) for other in checked_games):
            raise RuntimeError("Close the existing game before using the full launcher.")
        exe = args.game.resolve()
        from core.files import sha256

        if sha256(exe) != profile.verified_executable:
            raise RuntimeError(f"Unsupported {profile.title} executable. Run suite setup first.")
        validate_components(component_status(game, exe.parent), profile.title)
        logger.info("Launching %s... F12 cancels startup.", profile.title)
        try:
            configure_window(exe.parent)
            process = subprocess.Popen([str(exe)], cwd=str(exe.parent))
        except PermissionError:
            if args.elevated:
                raise
            arguments = sys.argv[1:] if argv is None else argv
            elevate([str(profile.source_dir / "launch.py"), *arguments, "--elevated"], ROOT)
            return
        deadline = time.monotonic() + 30
        while process.poll() is None and time.monotonic() < deadline:
            if process.pid in list(game_pids(game)):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(f"{profile.title} failed to start.")
        memory = open_memory(game, writable=True, pid=process.pid)
        startup = None
        try:
            startup = startup_type(memory, args.timeout)
            path = startup.prepare(args.state_dir)
            logger.info("Loaded and ready: %s", path)
            if not args.prepare_only:
                args.walk = True
                run(args, game, pid=process.pid)
        finally:
            try:
                world = getattr(startup, "world", None)
                if world is not None and memory.alive():
                    world.restore()
            finally:
                try:
                    if startup is not None:
                        startup.keys.release()
                finally:
                    memory.close()


def validate_seconds(parser, seconds):
    if seconds is not None and (not math.isfinite(seconds) or not 1 <= seconds <= 300):
        parser.error("Controller test duration must be between 1 and 300 seconds.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", choices=ACTIVE_GAMES, default="san_andreas")
    parser.add_argument("command", choices=("status", "run", "walk", "protect", "unlock", "peace",
                                           "autosave", "camera", "stop", "mode", "steer"),
                        nargs="?", default="status")
    parser.add_argument("value", nargs="?")
    parser.add_argument("--amount", type=int, choices=range(1, 101), default=1, metavar="1-100")
    parser.add_argument("--mode", choices=MODES)
    parser.add_argument("--walk", action="store_true")
    parser.add_argument("--seconds", type=float)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.game == "vice_city" and args.seconds is None:
        args.seconds = 60
    validate_seconds(parser, args.seconds)
    args.state_dir = (args.state_dir or get_game(args.game).state_dir).resolve()
    configure_logging(args.state_dir, args.game)
    if args.command == "run":
        run(args, args.game)
    elif args.command == "status":
        memory = open_memory(args.game)
        try:
            print(json.dumps(backend_for(args.game).Game(memory).snapshot(), indent=2))
        finally:
            memory.close()
    else:
        update_control(parser, args)


def update_control(parser, args):
    path = args.state_dir / "control"
    if not state_exists(path):
        raise RuntimeError("Start the controller before sending it commands.")
    settings = read_state(path)
    if args.command == "stop":
        settings["quit"] = True
    elif args.command == "mode":
        if args.value not in MODES:
            parser.error("Specify free, explore or mission.")
        settings["mode"] = args.value
    elif args.command == "camera":
        if args.value not in ("first", "third"):
            parser.error("Specify first or third.")
        settings["first_person"] = args.value == "first"
    elif args.command == "steer":
        if args.value not in DIRECTIONS | {"center", "stop"}:
            parser.error("Specify left, right, forward, back, up, down, center or stop.")
        queue_movement(settings, args.value, args.amount)
    else:
        if args.value not in ("on", "off"):
            parser.error("Specify on or off.")
        field = {"walk": "walking", "protect": "protection", "unlock": "unlock_map",
                 "peace": "peaceful", "autosave": "autosave"}[args.command]
        settings[field] = args.value == "on"
    write_state(path, settings)
    logger.info("Requested: %s", settings)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Controller stopped")
        raise SystemExit(1)
