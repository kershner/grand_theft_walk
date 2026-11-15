"""San Andreas runtime files, control requests, and bounded logging."""

import json
import logging
import threading
import time
import uuid
from pathlib import Path

from concurrent_log_handler import ConcurrentRotatingFileHandler

LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 2
DEFAULT_STATE_DIR = Path(__file__).resolve().parent / "runtime"
MODES = ("sidewalks", "free")
DIRECTIONS = {"left", "right", "forward", "back"}
MOVEMENT_COMMANDS = DIRECTIONS | {"walk", "stop", "jump"}
DEFAULTS = {
    "walking": False,
    "protection": True,
    "unlock_map": True,
    "quit": False,
    "mode": "sidewalks",
    "peaceful": True,
    "autosave": True,
    "first_person": False,
}


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def control_request(direction=None, amount=None):
    request = {"id": uuid.uuid4().hex, "issued": time.time()}
    if direction is not None:
        request["direction"] = direction
    if amount is not None:
        request["amount"] = amount
    return request


def request_expired(request):
    issued = request.get("issued")
    return not isinstance(issued, (int, float)) or not 0 <= time.time() - issued <= 10


def read_controls(path, settings):
    try:
        requested = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return
    for key, default in DEFAULTS.items():
        if isinstance(default, bool) and isinstance(requested.get(key), bool):
            settings[key] = requested[key]
    if requested.get("mode") in MODES:
        settings["mode"] = requested["mode"]
    for key in ("steering", "jump"):
        if isinstance(requested.get(key), dict):
            settings[key] = requested[key]


class WalkerControl:
    """Send chat commands through the walker's existing control file."""

    def __init__(self, state_dir=DEFAULT_STATE_DIR):
        self.state_dir = Path(state_dir)
        self.lock = threading.Lock()

    def snapshot(self):
        try:
            state = json.loads((self.state_dir / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError("Start the San Andreas walker controller first.") from error
        age = time.time() - state.get("updated", 0)
        if state.get("controller") != "running" or not 0 <= age <= 5:
            raise RuntimeError("The San Andreas walker controller is offline. Start it first.")
        return state

    def request(self, name, args):
        with self.lock:
            if not self.snapshot().get("ready"):
                raise RuntimeError("Game loading; please wait.")
            path = self.state_dir / "control.json"
            settings = json.loads(path.read_text(encoding="utf-8"))
            if settings.get("quit"):
                raise RuntimeError("The walker controller is stopping; please wait.")
            if name == "back" and settings.get("mode") != "free":
                raise RuntimeError("Use !mode free before moving backward.")
            if name == "jump":
                settings["jump"] = control_request()
            elif name in MOVEMENT_COMMANDS:
                settings["steering"] = control_request(name)
                if name in DIRECTIONS:
                    settings["steering"]["amount"] = int(args[0]) if args else 1
                if name in {"walk", "stop"}:
                    settings["walking"] = name == "walk"
            elif name == "mode":
                settings["mode"] = args[0]
            elif name == "camera":
                settings["first_person"] = (
                    args[0] == "first" if args else not settings.get("first_person", False)
                )
            atomic_json(path, settings)
            return settings


def configure_logging(log_dir=DEFAULT_STATE_DIR):
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "san_andreas.log"
    root = logging.getLogger()
    if any(isinstance(handler, ConcurrentRotatingFileHandler) for handler in root.handlers):
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
