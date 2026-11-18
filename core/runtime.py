"""Shared runtime files, control requests, and bounded logging."""

import json
import logging
import math
import threading
import time
import uuid
from pathlib import Path

from concurrent_log_handler import ConcurrentRotatingFileHandler

from games import get_game
from core.files import atomic_json, read_json

LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 2
DEFAULT_STATE_DIR = get_game().state_dir
PROTOCOL_VERSION = 1
MODES = ("free", "mission", "sidewalk")
DIRECTIONS = {"left", "right", "forward", "back", "up", "down"}
VIEW_COMMANDS = {"up", "down", "center"}
MOVEMENT_COMMANDS = DIRECTIONS | {"walk", "stop", "jump", "drop", "center"}
COMMAND_CAPABILITIES = {"up": "view", "down": "view", "center": "view",
                        "jump": "jump", "drop": "ledge_drop", "respawn": "respawn", "camera": "first_person"}
DEFAULTS = {
    "walking": False,
    "protection": True,
    "unlock_map": True,
    "quit": False,
    "mode": "mission",
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
    direction = ""
    facing = state.get("camera_heading")
    if facing is None or not math.isfinite(facing):
        facing = state.get("heading")
    if meters > 0 and facing is not None and math.isfinite(facing):
        # Game headings increase counterclockwise from north. Arrows are
        # relative to the view at the time this message is produced.
        bearing = math.atan2(-(target[0] - position["x"]), target[1] - position["y"])
        sector = math.floor(((bearing - facing) % math.tau) / (math.pi / 4) + 0.5) % 8
        direction = ("↑", "↖", "←", "↙", "↓", "↘", "→", "↗")[sector] + " "
    feet = round(meters / 0.3048)
    if feet < 5280:
        return f"{direction}{feet:,} ft away"
    miles = f"{feet / 5280:,.1f}".removesuffix(".0")
    return f"{direction}{miles} mi away"


def control_request(direction=None, amount=None):
    request = {"id": uuid.uuid4().hex, "issued": time.time()}
    if direction is not None:
        request["direction"] = direction
    if amount is not None:
        request["amount"] = amount
    return request


def queue_movement(settings, direction, amount=1):
    if direction in {"jump", "drop"}:
        settings["jump"] = control_request("drop") if direction == "drop" else control_request()
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
    for key in ("steering", "jump", "view", "respawn"):
        if isinstance(requested.get(key), dict):
            settings[key] = requested[key]


class NavigationModeError(RuntimeError):
    """A movement command needs a different navigation mode."""


class UnsupportedCommandError(RuntimeError):
    """The active backend does not implement the requested command."""


def command_supported(state, name):
    capability = COMMAND_CAPABILITIES.get(name)
    if capability is None:
        return True
    capabilities = state.get("capabilities")
    if capabilities is None:
        # Preserve the original San Andreas protocol before capability reports.
        return state.get("game", "san_andreas") == "san_andreas"
    return isinstance(capabilities, dict) and capabilities.get(capability) is True


def require_command(state, name):
    if not command_supported(state, name):
        title = get_game(state.get("game", "san_andreas")).title
        raise UnsupportedCommandError(f"!{name} is not available in {title} yet.")


class WalkerControl:
    """Send chat commands through the walker's existing control file."""

    def __init__(self, state_dir=None, game="san_andreas"):
        self.game = get_game(game)
        self.state_dir = Path(state_dir) if state_dir is not None else self.game.state_dir
        self.lock = threading.Lock()

    def snapshot(self):
        try:
            state = read_json(self.state_dir / "state.json")
        except (OSError, ValueError) as error:
            raise RuntimeError(
                f"Start the {self.game.title} walker controller first."
            ) from error
        # Legacy reports may omit identity/version only for the original default profile.
        if state.get("game", "san_andreas") != self.game.id:
            raise RuntimeError(f"The runtime folder belongs to another game; expected {self.game.title}.")
        protocol = state.get("protocol", 1 if self.game.id == "san_andreas" else None)
        if type(protocol) is not int or protocol != PROTOCOL_VERSION:
            raise RuntimeError("Unsupported walker protocol version.")
        age = time.time() - state.get("updated", 0)
        if state.get("controller") != "running" or not 0 <= age <= 5:
            raise RuntimeError(
                f"The {self.game.title} walker controller is offline. Start it first."
            )
        return state

    def request(self, name, args, expected_session=None):
        with self.lock:
            state = self.snapshot()
            if not chat_ready(state):
                raise RuntimeError("Game loading; please wait.")
            require_command(state, name)
            if expected_session is not None and state.get("session", state.get("pid", "controller")) != expected_session:
                raise RuntimeError("The game session changed; start a new vote.")
            path = self.state_dir / "control.json"
            settings = read_json(path)
            if settings.get("quit"):
                raise RuntimeError("The walker controller is stopping; please wait.")
            if name == "back" and settings.get("mode") != "free":
                raise NavigationModeError(
                    f"Back is unavailable in {settings.get('mode')} mode. Use !mode free for manual movement."
                )
            if name in MOVEMENT_COMMANDS:
                queue_movement(settings, name, int(args[0]) if args else 1)
            elif name == "respawn":
                settings["respawn"] = dict(control_request(), session=state.get("session", state.get("pid", "controller")))
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


def configure_logging(log_dir=DEFAULT_STATE_DIR, log_name="san_andreas"):
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{log_name}.log"
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
