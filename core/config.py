"""Stream configuration and persistent running-time rotation progress."""

import json
import logging
import math
import time
from pathlib import Path

from core.files import atomic_json, read_json
from games import ACTIVE_GAMES, ROOT


logger = logging.getLogger(__name__)


def load_parameters(path=ROOT / "parameters.json"):
    path = Path(path).resolve()
    try:
        params = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as error:
        raise ValueError('Create a local parameters.json first; {"MODE": "dev"} is enough for development.') from error
    if not isinstance(params, dict):
        raise ValueError("parameters.json must contain an object.")
    for section in ("ROTATION", "DEV_SERVER", "OBS"):
        if not isinstance(params.get(section, {}), dict):
            raise ValueError(f"{section} must contain an object.")
    obs = params.get("OBS", {})
    for key, default, minimum, maximum in (("connect_timeout", 60, 1, 600), ("settle_seconds", 10, 0, 120)):
        value = obs.get(key, default)
        if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"OBS.{key} must be between {minimum} and {maximum} seconds.")
    for section, default in (("OBS", 4455), ("DEV_SERVER", 5050)):
        value = params.get(section, {}).get("port", default)
        if type(value) is not int or not 1 <= value <= 65535:
            raise ValueError(f"{section}.port must be between 1 and 65535.")
    if not isinstance(obs.get("scenes", {}), dict):
        raise ValueError("OBS.scenes must map game IDs to scene names.")
    select_game(params)  # Validate rotation even when a manual game is selected.
    return params


def local_path(params_path, value):
    path = Path(value)
    return path if path.is_absolute() else Path(params_path).resolve().parent / path


def rotation_seconds(params):
    hours = params.get("ROTATION", {}).get("interval_hours", 2)
    if type(hours) not in (int, float) or not math.isfinite(hours) or not 0 < hours <= 8760:
        raise ValueError("ROTATION.interval_hours must be greater than 0 and at most 8760.")
    return hours * 3600


def select_game(params, progress=None):
    rotation_seconds(params)
    rotation = params.get("ROTATION", {})
    games = rotation.get("games", list(ACTIVE_GAMES))
    if not isinstance(games, list) or not games or any(not isinstance(g, str) or g not in ACTIVE_GAMES for g in games):
        raise ValueError(f"ROTATION.games must list available games: {', '.join(ACTIVE_GAMES)}.")
    if len(set(games)) != len(games):
        raise ValueError("ROTATION.games must not contain duplicates.")
    if progress and progress.get("game") in games:
        game = progress["game"]
        if progress["elapsed_seconds"] >= rotation_seconds(params):
            return games[(games.index(game) + 1) % len(games)]
        return game
    return games[0]


def twitch_settings(params):
    for key in ("CLIENT_ID", "CLIENT_SECRET", "TWITCH_CHANNEL", "TWITCH_BOT_LOGIN"):
        if not isinstance(params.get(key), str) or not params[key].strip():
            raise ValueError(f"parameters.json must include {key}.")
    for key in ("TWITCH_CHANNEL", "TWITCH_BOT_LOGIN"):
        value = params[key]
        if not value.isascii() or not value.replace("_", "").isalnum():
            raise ValueError(f"{key} must be a Twitch login without # or spaces.")
    if params["TWITCH_CHANNEL"].lower() != params["TWITCH_BOT_LOGIN"].lower():
        raise ValueError("TWITCH_BOT_LOGIN must match TWITCH_CHANNEL to manage stream metadata. Authorize as the broadcaster.")
    return params


class RotationProgress:
    def __init__(self, params, path):
        self.params, self.path = params, path
        self.interval = rotation_seconds(params)
        self.data = {}
        self.started_at = None
        self.saved_at = None
        try:
            data = read_json(path)
            elapsed = data.get("elapsed_seconds") if isinstance(data, dict) else None
            if (not isinstance(data, dict) or data.get("version") != 1
                    or not isinstance(data.get("game"), str)
                    or type(elapsed) not in (int, float)
                    or not math.isfinite(elapsed) or elapsed < 0):
                raise ValueError("Invalid rotation progress")
            self.data = data
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            logger.warning("Rotation progress unavailable: %s", error)

    def select(self):
        return select_game(self.params, self.data)

    def remaining(self, game):
        elapsed = self.data.get("elapsed_seconds", 0) if self.data.get("game") == game else 0
        return max(0, self.interval - elapsed)

    def start(self, game):
        # Explicitly choosing another game starts its slot. Choosing the same
        # unfinished game resumes it; a completed slot starts fresh.
        elapsed = self.interval - self.remaining(game)
        self.data = {"version": 1, "game": game,
                     "elapsed_seconds": elapsed if elapsed < self.interval else 0}
        self.started_at = self.saved_at = time.monotonic()
        self.initial_elapsed = self.data["elapsed_seconds"]
        atomic_json(self.path, self.data)

    def checkpoint(self, force=False):
        if self.started_at is None:
            return
        now = time.monotonic()
        was_complete = self.data["elapsed_seconds"] >= self.interval
        self.data["elapsed_seconds"] = min(
            self.interval, self.initial_elapsed + max(0, now - self.started_at))
        if force or now - self.saved_at >= 5 or (not was_complete and self.data["elapsed_seconds"] >= self.interval):
            atomic_json(self.path, self.data)
            self.saved_at = now
