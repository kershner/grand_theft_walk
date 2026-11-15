"""Exercise HTTP chat commands through to the walker's file protocol."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from dev_server import create_app


class ChatIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.settings = {
            "walking": False,
            "mode": "sidewalks",
            "first_person": False,
            "protection": True,
            "autosave": True,
            "quit": False,
        }
        self.write("control.json", self.settings)
        self.state = {
            "controller": "running",
            "updated": time.time(),
            "ready": True,
            "walking": "stopped",
            "settings": self.settings,
            "position": {"x": 1, "y": 2, "z": 3},
            "interior": 0,
        }
        self.write("state.json", self.state)
        self.app = create_app(self.root)
        self.client = self.app.test_client()

    def tearDown(self):
        self.folder.cleanup()

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value), encoding="utf-8")

    def control(self):
        return json.loads((self.root / "control.json").read_text())

    def command(self, text):
        response = self.client.get("/cmd/" + text)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ok"])

    def test_walk_steer_stop_reaches_existing_protocol(self):
        self.command("w")
        self.assertTrue(self.control()["walking"])
        self.assertEqual(self.control()["steering"]["direction"], "walk")
        previous = self.control()["steering"]["id"]
        self.command("!left")
        self.assertEqual(self.control()["steering"]["direction"], "left")
        self.assertNotEqual(self.control()["steering"]["id"], previous)
        self.command("!s")
        self.assertFalse(self.control()["walking"])
        self.assertFalse(self.control()["quit"])
        self.assertTrue(self.control()["protection"])
        self.assertEqual(
            self.client.get("/chat").json["messages"],
            ["Autowalk toggled", "Autowalk toggled"],
        )

    def test_camera_and_mode_settings_validate_arguments(self):
        self.command("camera")
        self.assertTrue(self.control()["first_person"])
        self.command("camera third")
        self.assertFalse(self.control()["first_person"])
        self.command("mode free")
        self.assertEqual(self.control()["mode"], "free")
        expected = self.control()
        for command in ("mode fly", "camera upside-down"):
            self.command(command)
            self.assertEqual(self.control(), expected)

    def test_direction_aliases_preserve_autowalk_and_send_amount(self):
        self.command("mode free")
        for walking in (False, True):
            for alias, direction in (
                ("l", "left"),
                ("r", "right"),
                ("f", "forward"),
                ("b", "back"),
            ):
                with self.subTest(walking=walking, direction=direction):
                    settings = self.control()
                    settings["walking"] = walking
                    self.write("control.json", settings)
                    self.command(f"{alias} 100")
                    self.assertEqual(self.control()["walking"], walking)
                    self.assertEqual(self.control()["steering"]["direction"], direction)
                    self.assertEqual(self.control()["steering"]["amount"], 100)
        self.command("f")
        self.assertEqual(self.control()["steering"]["amount"], 1)
        self.assertEqual(self.client.get("/chat").json["messages"], ["Navigation mode: free."])

    def test_back_requires_free_mode(self):
        with self.assertLogs("gtw_chat_bot", level="WARNING") as logs:
            self.command("b 2")
        self.assertEqual(self.control(), self.settings)
        self.assertEqual(self.client.get("/chat").json["messages"], [])
        self.assertIn("mode free", logs.output[0])

    def test_jump_is_silent_and_does_not_change_walking_or_steering(self):
        self.command("l 3")
        steering = self.control()["steering"]
        for command, walking in (("jump", False), ("j", True)):
            settings = self.control()
            settings["walking"] = walking
            self.write("control.json", settings)
            self.command(command)
            self.assertEqual(self.control()["walking"], walking)
            self.assertEqual(self.control()["steering"], steering)
            self.assertIn("id", self.control()["jump"])
            self.assertLess(abs(time.time() - self.control()["jump"]["issued"]), 2)
        self.assertEqual(self.client.get("/chat").json["messages"], [])
        expected = self.control()
        with self.assertLogs("gtw_chat_bot", level="WARNING"):
            self.command("jump 100")
        self.assertEqual(self.control(), expected)

    def test_offline_stale_and_loading_do_not_write(self):
        unavailable_states = [
            {"controller": "stopped"},
            {"updated": time.time() - 30},
            {"ready": False},
        ]
        with self.assertLogs("gtw_chat_bot", level="WARNING") as logs:
            for changes in unavailable_states:
                with self.subTest(changes=changes):
                    self.write("state.json", dict(self.state, **changes))
                    self.command("walk")
                    self.assertEqual(self.control(), self.settings)
        self.assertEqual(self.client.get("/chat").json["messages"], [])
        self.assertIn("offline", logs.output[0])
        self.assertIn("offline", logs.output[1])
        self.assertIn("loading", logs.output[2])

    def test_invalid_movement_is_silent_and_help_remains_available(self):
        with self.assertLogs("gtw_chat_bot", level="WARNING"):
            for command in ("l 0", "r 101", "f nope", "b -1", "f 1.5", "f 2 3", "w nope", "s nope"):
                self.command(command)
                self.assertEqual(self.control(), self.settings)
        self.assertEqual(self.client.get("/chat").json["messages"], [])
        self.command("help f")
        self.assertIn("!forward [1-100]", self.client.get("/chat").json["messages"][0])

    def test_help_location_and_cursor(self):
        self.command("help")
        cursor = self.client.get("/chat").json["cursor"]
        self.command("loc")
        messages = self.client.get(f"/chat?after={cursor}").json["messages"]
        self.assertEqual(len(messages), 1)
        self.assertIn("X: 1.0", messages[0])
        self.assertEqual(self.client.get("/chat?after=invalid").json["cursor"], 2)
        self.assertEqual(self.client.get("/chat?after=-1").json["cursor"], 2)
        self.assertEqual(self.client.get("/chat?after=2").json["messages"], [])

    def test_console_loads_and_handles_empty_or_unknown_commands(self):
        response = self.client.get("/")
        try:
            self.assertIn(b"Grand Theft Walk Control", response.data)
            self.assertEqual(response.status_code, 200)
        finally:
            response.close()
        self.assertEqual(self.client.get("/cmd/%20").status_code, 400)
        self.command("unknown")
        self.assertEqual(self.client.get("/chat").json["messages"], [])


if __name__ == "__main__":
    unittest.main()
