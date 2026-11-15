"""San Andreas runtime files, control requests, and bounded logging."""

import json
import logging
import math
import threading
import time
import uuid
from pathlib import Path

from concurrent_log_handler import ConcurrentRotatingFileHandler

LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 2
DEFAULT_STATE_DIR = Path(__file__).resolve().parent / "runtime"
MODES = ("sidewalk", "free")
DIRECTIONS = {"left", "right", "forward", "back", "up", "down"}
VIEW_COMMANDS = {"up", "down", "center"}
MOVEMENT_COMMANDS = DIRECTIONS | {"walk", "stop", "jump", "center"}
DEFAULTS = {
    "walking": False,
    "protection": True,
    "unlock_map": True,
    "quit": False,
    "mode": "sidewalk",
    "peaceful": True,
    "autosave": True,
    "first_person": False,
}


def chat_ready(state):
    return bool(state.get("ready") and state.get("startup_ready", True))


def mission_distance(state, mission):
    position = state.get("position")
    target = mission.get("position")
    if not state.get("ready") or not position or not target:
        return "Distance unavailable"
    meters = math.hypot(position["x"] - target[0], position["y"] - target[1])
    feet = round(meters / 0.3048)
    if feet < 5280:
        return f"{feet:,} ft away"
    miles = f"{feet / 5280:,.1f}".removesuffix(".0")
    return f"{miles} mi away"


def _retry_file_access(operation, *args, **kwargs):
    # Windows can briefly deny access while another process replaces the file.
    for attempt in range(6):
        try:
            return operation(*args, **kwargs)
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.005 * 2**attempt)


def read_json(path):
    return json.loads(_retry_file_access(path.read_text, encoding="utf-8"))


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _retry_file_access(
            temporary.write_text, json.dumps(data, indent=2), encoding="utf-8"
        )
        _retry_file_access(temporary.replace, path)
    finally:
        _retry_file_access(temporary.unlink, missing_ok=True)


def control_request(direction=None, amount=None):
    request = {"id": uuid.uuid4().hex, "issued": time.time()}
    if direction is not None:
        request["direction"] = direction
    if amount is not None:
        request["amount"] = amount
    return request


def queue_movement(settings, direction, amount=1):
    if direction == "jump":
        settings["jump"] = control_request()
        return
    field = "view" if direction in VIEW_COMMANDS else "steering"
    request = control_request(direction)
    if direction in DIRECTIONS or direction == "center":
        request["amount"] = amount
    settings[field] = request
    if direction in {"walk", "stop"}:
        settings["walking"] = direction == "walk"


def request_expired(request):
    issued = request.get("issued")
    return not isinstance(issued, (int, float)) or not 0 <= time.time() - issued <= 10


def read_controls(path, settings):
    try:
        requested = read_json(path)
    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
        return
    for key, default in DEFAULTS.items():
        if isinstance(default, bool) and isinstance(requested.get(key), bool):
            settings[key] = requested[key]
    mode = requested.get("mode")
    if mode in MODES:
        settings["mode"] = mode
    for key in ("steering", "jump", "view"):
        if isinstance(requested.get(key), dict):
            settings[key] = requested[key]


class NavigationModeError(RuntimeError):
    """A movement command needs a different navigation mode."""


class WalkerControl:
    """Send chat commands through the walker's existing control file."""

    def __init__(self, state_dir=DEFAULT_STATE_DIR):
        self.state_dir = Path(state_dir)
        self.lock = threading.Lock()

    def snapshot(self):
        try:
            state = read_json(self.state_dir / "state.json")
        except (OSError, ValueError) as error:
            raise RuntimeError(
                "Start the San Andreas walker controller first."
            ) from error
        age = time.time() - state.get("updated", 0)
        if state.get("controller") != "running" or not 0 <= age <= 5:
            raise RuntimeError(
                "The San Andreas walker controller is offline. Start it first."
            )
        return state

    def request(self, name, args):
        with self.lock:
            if not chat_ready(self.snapshot()):
                raise RuntimeError("Game loading; please wait.")
            path = self.state_dir / "control.json"
            settings = read_json(path)
            if settings.get("quit"):
                raise RuntimeError("The walker controller is stopping; please wait.")
            if name == "back" and settings.get("mode") != "free":
                raise NavigationModeError(
                    "Back is unavailable in sidewalk mode. Use !mode free for manual movement."
                )
            if name in MOVEMENT_COMMANDS:
                queue_movement(settings, name, int(args[0]) if args else 1)
            elif name == "mode":
                settings["mode"] = args[0]
            elif name == "camera":
                settings["first_person"] = (
                    args[0] == "first"
                    if args
                    else not settings.get("first_person", False)
                )
            atomic_json(path, settings)
            return settings

    def vote(self, viewer, choice):
        if not isinstance(viewer, str) or not viewer.strip():
            raise ValueError("Could not identify your viewer account.")
        state = self.snapshot()
        if not chat_ready(state):
            raise RuntimeError("Game loading; please wait.")
        poll = state.get("missions", {}).get("poll")
        if not poll or time.time() >= poll["closes"]:
            return "No mission vote is open. Use !info to see the current mission."
        if type(choice) is not int or not 1 <= choice <= 3:
            raise ValueError("Vote with !vote 1, !vote 2, or !vote 3.")
        atomic_json(
            self.state_dir / "mission_votes" / f"{uuid.uuid4().hex}.json",
            {
                "poll": poll["id"],
                "viewer": viewer.strip().casefold(),
                "choice": choice,
                "issued": time.time(),
            },
        )
        return f"Vote submitted: {choice}. Your latest vote counts."


def configure_logging(log_dir=DEFAULT_STATE_DIR):
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "san_andreas.log"
    root = logging.getLogger()
    if any(
        isinstance(handler, ConcurrentRotatingFileHandler) for handler in root.handlers
    ):
        return path

    file_handler = ConcurrentRotatingFileHandler(
        str(path),
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUPS,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(process)d] %(name)s: %(message)s",
        )
    )
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(console)
    # Successful HTTP polls are noise; warnings and errors still reach the log.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.captureWarnings(True)
    return path
