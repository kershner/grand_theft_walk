"""Shared normal-save selection and focus-aware startup waits."""

import logging
import time
from pathlib import Path

from gtw.controls import key_down

logger = logging.getLogger(__name__)


def choose_save(prefix, valid_save, record=None):
    paths = [Path(f"{prefix}{slot + 1}.b") for slot in range(8)]
    if record is not None:
        slot = record.get("slot")
        if type(slot) is not int or slot not in range(8):
            raise RuntimeError("Invalid walker save record.")
        path = paths[slot]
        if str(path).casefold() != str(record.get("path", "")).casefold():
            raise RuntimeError("The game's save folder changed; check runtime/launch.json.")
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
    def wait(self, description, predicate):
        logger.info(description)
        background_at = None
        while time.monotonic() < self.deadline:
            if not self.mem.alive():
                raise RuntimeError("The game exited during startup.")
            if key_down("f12"):
                raise RuntimeError("Startup cancelled with F12.")
            now = time.monotonic()
            if not self.mem.foreground():
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
