import hashlib
import json
import logging
import shutil
import struct
import subprocess
import time
from pathlib import Path

from runtime import atomic_json, read_json

logger = logging.getLogger(__name__)

MENU = 0xBA6748
SAVE_PREFIX = 0xC16F18


def valid_save(path):
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return False
    return (
        200000 <= len(data) <= 4 * 1024 * 1024
        and data.startswith(b"BLOCK")
        and (sum(data[:-4]) & 0xFFFFFFFF) == struct.unpack("<I", data[-4:])[0]
    )


class NativeSaveRequest:
    def __init__(self, memory):
        self.memory = memory
        self.process = None
        self.folder = Path(__file__).resolve().parent / "native"

    @property
    def busy(self):
        return self.process is not None and self.process.poll() is None

    def start(self, slot):
        if self.busy:
            raise RuntimeError("A native save is already in progress")
        manifest = read_json(self.folder / "manifest.json")
        if manifest.get("format") != 1:
            raise ValueError("Invalid native save bridge manifest")
        for name in ("save_request.exe", "save_bridge.dll"):
            if (
                hashlib.sha256((self.folder / name).read_bytes()).hexdigest()
                != manifest["files"][name]
            ):
                raise ValueError("Native save bridge failed validation")
        self.process = subprocess.Popen(
            [str(self.folder / "save_request.exe"), str(self.memory.pid), str(slot)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def poll(self):
        if self.busy:
            return None
        if self.process is None:
            raise RuntimeError("No native save request")
        output = self.process.communicate()[0].decode("utf-8", errors="replace").strip()
        code = self.process.returncode
        self.process = None
        try:
            result = json.loads(output)
        except ValueError:
            raise RuntimeError(f"Native save helper exited with code {code}: {output}")
        if not isinstance(result, dict):
            raise RuntimeError("Invalid native save helper response")
        if result.get("status") == "deferred" and code == 3:
            return result
        if code != 0 or result.get("status") != "saved":
            raise RuntimeError(result.get("error", "Native save failed"))
        return result

    def close(self):
        # Never terminate a helper while its game-thread callback may be saving.
        if self.process is not None and not self.busy:
            self.process.communicate()
            self.process = None


class Autosave:
    def __init__(self, memory, state_dir, interval=60, bridge=None):
        self.mem, self.state_dir = memory, Path(state_dir)
        self.interval = interval
        self.next_at = time.monotonic() + interval
        self.phase = "idle"
        self.error = None
        self.last_saved = None
        self.count = 0
        self.slot = self.path = None
        self.backup = self.state_dir / "autosave-backup.b"
        self.bridge = bridge if bridge is not None else NativeSaveRequest(memory)
        self.duration_ms = None

    def menu(self):
        return self.mem.byte(MENU + 0x15D) if self.mem.byte(MENU + 0x5C) else None

    def safe(self, state):
        pause_menu = self.menu() == 41 and state.get("paused")
        if not (
            state.get("ready")
            and (not state["paused"] or pause_menu)
            and (not state["controls_disabled"] or pause_menu)
            and state["player_state"] == 0
            and state["health"] > 0
            and not state["in_vehicle"]
            and state["interior"] == 0
            and state["ped_state"] in (0, 1)
        ):
            return False
        if (
            (self.menu() is not None and not pause_menu)
            or self.mem.byte(0xB5F851)
            or self.mem.u32(0x96A7CC)
        ):
            return False
        mission = self.mem.u32(0xA476AC)
        return mission < 200000 and not (mission and self.mem.u32(0xA49960 + mission) == 1)

    def target(self):
        record = read_json(self.state_dir / "launch.json")
        slot = record.get("slot")
        if type(slot) is not int or slot not in range(8):
            raise ValueError("Invalid walker save slot")
        prefix = self.mem.read(SAVE_PREFIX, 260).split(b"\0", 1)[0].decode("mbcs")
        if not prefix or not Path(prefix).is_absolute():
            raise ValueError("Invalid game save directory")
        path = Path(f"{prefix}{slot + 1}.b")
        if str(path).casefold() != str(record.get("path", "")).casefold():
            raise ValueError("Game save directory does not match the walker save")
        if not valid_save(path):
            raise ValueError("Current walker save is missing or damaged")
        return slot, path

    def fail(self, error, now):
        self.error = str(error)
        if self.phase == "saving" and not self.bridge.busy and self.path is not None:
            try:
                if not valid_save(self.path) and valid_save(self.backup):
                    recovery = self.path.with_suffix(".gtw-recover.tmp")
                    shutil.copyfile(self.backup, recovery)
                    recovery.replace(self.path)
            except OSError as recovery_error:
                self.error += f"; restore failed: {recovery_error}"
        logger.warning("Autosave failed: %s. Previous checkpoint backup: %s", error, self.backup)
        self.phase = "idle"
        self.next_at = now + self.interval

    def tick(self, state, now, enabled=True):
        if self.phase == "idle":
            if not enabled:
                self.next_at = now + self.interval
                return False
            if now < self.next_at or not self.safe(state):
                return False
            try:
                self.slot, self.path = self.target()
                self.state_dir.mkdir(parents=True, exist_ok=True)
                temporary = self.backup.with_suffix(".tmp")
                shutil.copyfile(self.path, temporary)
                if not valid_save(temporary):
                    raise ValueError("Checkpoint backup failed validation")
                temporary.replace(self.backup)
                self.bridge.start(self.slot)
            except (OSError, ValueError, TypeError, RuntimeError, KeyError) as error:
                self.fail(error, now)
                return False
            self.phase = "saving"
            self.error = None
            return True
        try:
            result = self.bridge.poll()
            if result is not None:
                if result.get("status") == "deferred":
                    self.phase, self.next_at = "idle", now + 1
                else:
                    if not valid_save(self.path):
                        raise ValueError("The game's new save failed checksum validation")
                    record = {
                        "slot": self.slot,
                        "path": str(self.path),
                        "sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
                        "last_autosave": time.time(),
                    }
                    atomic_json(self.state_dir / "launch.json", record)
                    self.last_saved = record["last_autosave"]
                    self.count += 1
                    self.duration_ms = result.get("duration_ms")
                    logger.info("Autosaved walker checkpoint.")
                    self.phase, self.next_at = "idle", now + self.interval
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            self.fail(error, now)
        return self.phase == "saving"

    def close(self):
        self.bridge.close()

    def report(self):
        return {
            "phase": self.phase,
            "interval_seconds": self.interval,
            "last_saved": self.last_saved,
            "count": self.count,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }
