"""Persist completed running time without counting launcher downtime."""

import logging
import math
import time

from core.config import rotation_seconds, select_game
from core.files import atomic_json, read_json

logger = logging.getLogger(__name__)


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
