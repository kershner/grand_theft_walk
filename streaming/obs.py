"""OBS startup and stream ownership for a managed session."""

import logging
import subprocess
import time
from pathlib import Path

import obsws_python
import psutil

logger = logging.getLogger(__name__)


class OBS:
    def __init__(self, settings):
        self.settings = settings
        self.client = None
        self.started_stream = False

    def prepare(self, game):
        settings = self.settings
        executable = Path(settings.get("executable", "C:/Program Files/obs-studio/bin/64bit/obs64.exe"))
        running = any((p.info["name"] or "").lower() == executable.name.lower()
                      for p in psutil.process_iter(["name"]))
        local = settings.get("host", "127.0.0.1") in ("localhost", "127.0.0.1", "::1")
        if not running and local:
            if not executable.is_file():
                raise RuntimeError(
                    "OBS executable was not found. Set OBS.executable in parameters.json "
                    "to the full path of obs64.exe."
                )
            command = [str(executable), "--minimize-to-tray", "--disable-missing-files-check", "--disable-updater"]
            for key, flag in (("profile", "--profile"), ("scene_collection", "--collection")):
                if settings.get(key):
                    command.extend([flag, settings[key]])
            subprocess.Popen(command, cwd=executable.parent)
        deadline = time.monotonic() + float(settings.get("connect_timeout", 60))
        while True:
            try:
                self.client = obsws_python.ReqClient(
                    host=settings.get("host", "127.0.0.1"), port=settings.get("port", 4455),
                    password=settings.get("password", ""), timeout=5)
                self.client.get_version()
                break
            except Exception:
                self.disconnect()
                if time.monotonic() >= deadline:
                    raise RuntimeError("OBS WebSocket unavailable; check OBS Tools > WebSocket Server Settings.") from None
                time.sleep(2)
        if self.client.get_stream_status().output_active:
            raise RuntimeError("OBS is already streaming. Stop that stream before starting a managed session.")
        if settings.get("profile"):
            self.client.set_current_profile(settings["profile"])
        if settings.get("scene_collection"):
            self.client.set_current_scene_collection(settings["scene_collection"])
        scene = settings.get("scenes", {}).get(game)
        if scene:
            self.client.set_current_program_scene(scene)
        # OBS attaches its capture hook before the game creates its DirectX context.
        time.sleep(float(settings.get("settle_seconds", 10)))

    def start_stream(self):
        # Set ownership first: a timed-out request may still have started the stream.
        self.started_stream = True
        self.client.start_stream()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.client.get_stream_status().output_active:
                logger.info("OBS stream live.")
                return
            time.sleep(1)
        raise RuntimeError("OBS did not report a live stream within 30 seconds.")

    def disconnect(self):
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception:
                pass
            self.client = None

    def close(self):
        try:
            if self.started_stream and self.client is not None:
                self.client.stop_stream()
        except Exception:
            logger.error("Could not stop the managed OBS stream; check OBS.")
        finally:
            self.disconnect()
