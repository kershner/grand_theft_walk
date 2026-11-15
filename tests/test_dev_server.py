"""Exercise HTTP chat commands through to the walker's file protocol."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from dev_server import create_app


class ChatIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.settings = {
            "walking": False,
            "mode": "sidewalk",
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
        self.voting = self.app.extensions["gtw_bot"].command_voting
        self.timer = Mock()
        self.voting.timer_factory = self.timer

    def tearDown(self):
        self.voting.close()
        self.folder.cleanup()

    def finish_vote(self):
        self.timer.call_args.args[1]()

    def set_mode(self, mode):
        settings = self.control()
        settings["mode"] = mode
        self.write("control.json", settings)

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
            [
                "Autowalk toggled",
                "Sidewalk mode: direction queued for the next junction. Use !mode free for manual movement.",
                "Autowalk toggled",
            ],
        )

    def test_camera_and_mode_settings_validate_arguments(self):
        self.command("camera")
        self.assertFalse(self.control()["first_person"])
        self.finish_vote()
        self.assertTrue(self.control()["first_person"])
        self.command("camera third")
        self.finish_vote()
        self.assertFalse(self.control()["first_person"])
        self.command("mode free")
        self.assertEqual(self.control()["mode"], "sidewalk")
        self.finish_vote()
        self.assertEqual(self.control()["mode"], "free")
        expected = self.control()
        for command in ("mode fly", "camera upside-down"):
            self.command(command)
            self.assertEqual(self.control(), expected)

    def test_direction_aliases_preserve_autowalk_and_send_amount(self):
        self.set_mode("free")
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
        self.assertEqual(self.client.get("/chat").json["messages"], [])

    def test_back_requires_free_mode(self):
        self.command("b 2")
        self.assertEqual(self.control(), self.settings)
        self.assertEqual(
            self.client.get("/chat").json["messages"],
            [
                "Back is unavailable in sidewalk mode. Use !mode free for manual movement."
            ],
        )

    def test_stopped_sidewalk_directions_explain_how_to_move(self):
        for command, direction in (
            ("l 10", "left"),
            ("r 10", "right"),
            ("f 2", "forward"),
        ):
            self.command(command)
            self.assertFalse(self.control()["walking"])
            self.assertEqual(self.control()["steering"]["direction"], direction)
        messages = self.client.get("/chat").json["messages"]
        self.assertEqual(
            messages,
            [
                (
                    "Sidewalk mode: direction queued for the next junction. "
                    "Use !walk to continue or !mode free for manual movement."
                )
            ]
            * 3,
        )

    def test_mode_command_uses_singular_name(self):
        self.command("mode free")
        self.finish_vote()
        self.command("mode sidewalk")
        self.finish_vote()
        self.assertEqual(self.control()["mode"], "sidewalk")
        self.assertEqual(
            self.client.get("/chat").json["messages"][-1], "Navigation mode: sidewalk."
        )

    def test_command_ballots_are_per_browser_and_can_change(self):
        self.command("camera first")
        other = self.app.test_client()
        other.get("/cmd/no")
        self.command("no")
        self.command("yes")
        self.assertEqual(sorted(self.voting.vote.votes.values()), ["no", "yes"])
        self.command("mode free")
        self.assertIn(
            "Vote already in progress.", self.client.get("/chat").json["messages"]
        )
        self.finish_vote()
        self.assertFalse(self.control()["first_person"])
        self.assertTrue(
            any(
                "Vote failed! (1-1)" in message
                for message in self.client.get("/chat").json["messages"]
            )
        )

    def test_mission_ballots_and_announcements_are_independent_of_command_vote(self):
        self.state["missions"] = {
            "active": None,
            "events": [{"id": "e1", "text": "Choose our next mission!"}],
            "poll": {
                "id": "round1",
                "closes": time.time() + 60,
                "counts": [0, 0, 0],
                "options": [
                    {"name": name, "position": [301, 402, 3]}
                    for name in ("Ganton", "Market", "Rodeo")
                ],
            },
        }
        self.write("state.json", self.state)
        self.command("camera")
        self.command("vote 2")
        self.command("vote 3")
        self.command("mission")
        ballots = [
            json.loads(p.read_text())
            for p in (self.root / "mission_votes").glob("*.json")
        ]
        self.assertEqual(len(ballots), 2)
        self.assertEqual(len({b["viewer"] for b in ballots}), 1)
        self.assertTrue(self.voting.vote.active)
        messages = self.client.get("/chat").json["messages"]
        self.assertEqual(messages.count("Choose our next mission!"), 1)
        self.assertEqual(
            self.client.get("/chat").json["messages"].count("Choose our next mission!"),
            1,
        )
        self.assertTrue(
            any("2: Market (1,640 ft away, 0 votes)" in m for m in messages)
        )

    def test_info_includes_current_mission(self):
        self.state["missions"] = {"active": {"name": "Market"}}
        self.write("state.json", self.state)
        self.command("info")
        message = self.client.get("/chat").json["messages"][-1]
        self.assertIn("Current mission: Travel to Market", message)
        self.assertIn("mode: sidewalk", message)
        self.assertIn("camera: third person.", message)

    def test_mission_distance_updates_in_mission_and_info_replies(self):
        self.state["missions"] = {
            "active": {"name": "Market", "position": [301, 402, 10]},
            "held_seconds": 0,
            "hold_seconds": 30,
        }
        for position, expected in (
            ({"x": 1, "y": 2, "z": 3}, "1,640 ft"),
            ({"x": 301, "y": 402, "z": 10}, "0 ft"),
            ({"x": 300, "y": 402, "z": 10}, "3 ft"),
            ({"x": -699, "y": 402, "z": 10}, "3,281 ft"),
            ({"x": 301 - 1609.344, "y": 402, "z": 10}, "1 mi"),
            ({"x": -12199, "y": 402, "z": 10}, "7.8 mi"),
        ):
            self.state["position"] = position
            self.write("state.json", self.state)
            for command in ("mission", "info"):
                self.command(command)
                message = self.client.get("/chat").json["messages"][-1]
                self.assertIn(f" • {expected} away", message)
        self.state["ready"] = False
        self.write("state.json", self.state)
        self.command("mission")
        self.assertIn(
            "Distance unavailable", self.client.get("/chat").json["messages"][-1]
        )

    def test_jump_is_silent_and_does_not_change_walking_or_steering(self):
        self.write("control.json", dict(self.settings, mode="free"))
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
            for command in (
                "l 0",
                "r 101",
                "f nope",
                "b -1",
                "f 1.5",
                "f 2 3",
                "w nope",
                "s nope",
            ):
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

    def test_location_and_loc_report_places_and_live_conditions(self):
        self.state["location"] = {
            "places": ["Las Colinas", "Los Santos"],
            "road": "near a pedestrian crossing",
            "date": "Jul 14",
            "time": "4:32 PM",
            "period": "afternoon",
            "weather": "rain, strong wind",
            "heading": "northwest",
            "indoors": False,
        }
        self.write("state.json", self.state)
        self.command("location")
        self.command("loc")
        expected = (
            "Las Colinas, Los Santos • near a pedestrian crossing • "
            "Jul 14 4:32 PM (afternoon) • rain, strong wind • heading northwest"
        )
        self.assertEqual(
            self.client.get("/chat").json["messages"], [expected, expected]
        )

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
