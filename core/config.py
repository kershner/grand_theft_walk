"""Local stream configuration; paths are relative to parameters.json."""

import json
import math
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from games import ACTIVE_GAMES, ROOT


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


def select_game(params, now=None):
    rotation = params.get("ROTATION", {})
    games = rotation.get("games", list(ACTIVE_GAMES))
    if not isinstance(games, list) or not games or any(not isinstance(g, str) or g not in ACTIVE_GAMES for g in games):
        raise ValueError("ROTATION.games must list available games: san_andreas, vice_city.")
    if len(set(games)) != len(games):
        raise ValueError("ROTATION.games must not contain duplicates.")
    zone = ZoneInfo(rotation.get("timezone", "America/New_York"))
    current = datetime.now(zone) if now is None else now.astimezone(zone)
    anchor = date.fromisoformat(rotation.get("start_date", "2026-11-17"))
    return games[(current.date() - anchor).days % len(games)]


def twitch_settings(params):
    for key in ("CLIENT_ID", "CLIENT_SECRET", "TWITCH_CHANNEL", "TWITCH_BOT_LOGIN"):
        if not isinstance(params.get(key), str) or not params[key].strip():
            raise ValueError(f"parameters.json must include {key}.")
    for key in ("TWITCH_CHANNEL", "TWITCH_BOT_LOGIN"):
        value = params[key]
        if not value.isascii() or not value.replace("_", "").isalnum():
            raise ValueError(f"{key} must be a Twitch login without # or spaces.")
    return params
