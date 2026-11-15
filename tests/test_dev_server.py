"""Exercise HTTP chat commands through to the walker's file protocol."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dev_server import create_app
from gtw_chat_bot import ANNOUNCEMENT_INTERVAL


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
            "startup_ready": True,
            "session": "session-a",
            "walking": "stopped",
            "settings": self.settings,
            "position": {"x": 1, "y": 2, "z": 3},
            "interior": 0,
        }
        self.write("state.json", self.state)
        self.app = create_app(self.root, start_runtime=False)
        self.client = self.app.test_client()
        self.voting = self.app.extensions["gtw_bot"].command_voting
        self.timer = Mock()
        self.voting.timer_factory = self.timer

    def tearDown(self):
        self.app.extensions["gtw_bot"].close()
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

    def test_view_commands_work_in_both_modes_without_replacing_steering(self):
        for mode in ("free", "sidewalk"):
            for walking in (False, True):
                settings = dict(
                    self.settings,
                    mode=mode,
                    walking=walking,
                    steering={"id": "queued-turn", "direction": "left"},
                )
                self.write("control.json", settings)
                for command, direction, amount in (
                    ("up", "up", 1),
                    ("u 100", "up", 100),
                    ("down 10", "down", 10),
                    ("d", "down", 1),
                    ("center", "center", 1),
                ):
                    self.command(command)
                    updated = self.control()
                    self.assertEqual(updated["steering"], settings["steering"])
                    self.assertEqual(updated["view"]["direction"], direction)
                    self.assertEqual(updated["view"]["amount"], amount)
                    self.assertEqual(updated["walking"], walking)
                    self.assertEqual(updated["mode"], mode)
        self.assertEqual(self.client.get("/chat").json["messages"], [])
        expected = self.control()
        with self.assertLogs("gtw_chat_bot", level="WARNING"):
            for command in ("up 0", "down 101", "center 5", "up nope"):
                self.command(command)
                self.assertEqual(self.control(), expected)

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
        self.assertIn("Vote submitted: 3. Your latest vote counts.", messages)

    def test_info_includes_current_mission(self):
        self.state["missions"] = {"active": {"name": "Market"}}
        self.write("state.json", self.state)
        self.command("info")
        message = self.client.get("/chat").json["messages"][-1]
        self.assertIn("Current mission: Travel to Market", message)
        self.assertIn("mode: sidewalk", message)
        self.assertIn("camera: third person •", message)

    def test_mission_distance_updates_in_info_replies(self):
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
            self.command("info")
            message = self.client.get("/chat").json["messages"][-1]
            self.assertIn(f" • {expected} away", message)
        self.state["ready"] = False
        self.write("state.json", self.state)
        self.command("info")
        self.assertIn(
            "Game loading; please wait.", self.client.get("/chat").json["messages"][-1]
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
            {"startup_ready": False},
        ]
        for changes in unavailable_states:
            with self.subTest(changes=changes):
                self.write("state.json", dict(self.state, **changes))
                self.command("walk")
                self.assertEqual(self.control(), self.settings)
        self.assertEqual(
            self.client.get("/chat").json["messages"],
            ["Game loading; please wait."] * len(unavailable_states),
        )

    def test_startup_locks_help_info_votes_and_input_until_ready(self):
        self.state["startup_ready"] = False
        self.write("state.json", self.state)
        bot = self.app.extensions["gtw_bot"]
        for command in (
            "help",
            "more",
            "info",
            "camera",
            "mode free",
            "yes",
            "no",
            "vote 1",
            "up",
            "center",
        ):
            self.command(command)
        self.assertEqual(self.control(), self.settings)
        self.assertFalse(self.voting.vote.active)
        self.assertFalse((self.root / "mission_votes").exists())
        self.assertEqual(bot.poll_announcements(0), [])
        self.assertEqual(
            self.client.get("/chat").json["messages"],
            ["Game loading; please wait."] * 10,
        )
        self.state["startup_ready"] = True
        self.write("state.json", self.state)
        startup = bot.poll_announcements(10)
        self.assertEqual(
            startup,
            ["Grand Theft Walk is online!", bot._reply("help", []), bot._info_reply()],
        )
        self.assertEqual(bot.poll_announcements(11), [])
        self.command("walk")
        self.assertTrue(self.control()["walking"])

    def test_announcements_rotate_from_startup_and_skip_missed_intervals(self):
        bot = self.app.extensions["gtw_bot"]
        self.assertEqual(len(bot.poll_announcements(100)), 3)
        self.command("help")
        self.command("info")
        self.assertEqual(bot.poll_announcements(100 + ANNOUNCEMENT_INTERVAL - 1), [])
        self.assertEqual(
            bot.poll_announcements(100 + ANNOUNCEMENT_INTERVAL),
            [bot._reply("help", [])],
        )
        self.state["location"] = {"places": ["Market", "Los Santos"]}
        self.write("state.json", self.state)
        self.assertEqual(
            bot.poll_announcements(100 + ANNOUNCEMENT_INTERVAL * 2), [bot._info_reply()]
        )
        self.assertEqual(
            len(bot.poll_announcements(100 + ANNOUNCEMENT_INTERVAL * 10)), 1
        )
        self.assertEqual(bot.poll_announcements(101 + ANNOUNCEMENT_INTERVAL * 10), [])

    def test_announcements_restart_for_new_controller_but_not_temporary_loading(self):
        bot = self.app.extensions["gtw_bot"]
        bot.poll_announcements(0)
        self.state["ready"] = False
        self.write("state.json", self.state)
        self.assertEqual(bot.poll_announcements(1), [])
        self.state["ready"] = True
        self.write("state.json", self.state)
        self.assertEqual(bot.poll_announcements(2), [])
        self.state["session"] = "session-b"
        self.write("state.json", self.state)
        self.assertEqual(len(bot.poll_announcements(3)), 3)
        self.assertEqual(bot.next_announcement_at, 3 + ANNOUNCEMENT_INTERVAL)

    def test_announcement_worker_posts_without_http_polling_and_stops_cleanly(self):
        bot = self.app.extensions["gtw_bot"]
        messages = []
        announced = threading.Event()

        def send(message):
            messages.append(message)
            if len(messages) == 3:
                announced.set()

        bot.start_runtime(send)
        worker = bot.runtime_thread
        bot.start_runtime(send)
        self.assertIs(bot.runtime_thread, worker)
        self.assertTrue(announced.wait(timeout=2))
        bot.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(
            messages,
            ["Grand Theft Walk is online!", bot._reply("help", []), bot._info_reply()],
        )

    def test_dev_server_ignores_existing_mission_events_but_delivers_new_ones_once(
        self,
    ):
        old = {"id": "previous-event", "text": "Mission complete: old destination!"}
        self.state["missions"] = {"events": [old]}
        self.write("state.json", self.state)
        app = create_app(self.root, start_runtime=False)
        self.addCleanup(app.extensions["gtw_bot"].close)
        client = app.test_client()
        self.assertEqual(client.get("/chat").json["messages"], [])
        self.state["missions"]["events"].append(
            {"id": "new-event", "text": "Choose our next mission!"}
        )
        self.write("state.json", self.state)
        self.assertEqual(
            client.get("/chat").json["messages"], ["Choose our next mission!"]
        )
        self.assertEqual(
            client.get("/chat").json["messages"], ["Choose our next mission!"]
        )

    def test_live_mission_events_wait_until_startup_messages_are_delivered(self):
        with patch("dev_server.GTWBot.start_runtime") as start:
            app = create_app(self.root)
        bot = app.extensions["gtw_bot"]
        self.addCleanup(bot.close)
        client = app.test_client()
        self.state["missions"] = {
            "events": [{"id": "fresh-vote", "text": "Choose our next mission!"}]
        }
        self.write("state.json", self.state)
        self.assertEqual(client.get("/chat").json["messages"], [])
        startup = bot.poll_announcements(0)
        send = start.call_args.args[0]
        for message in startup:
            send(message)
        bot.announced_session = bot.session
        messages = client.get("/chat").json["messages"]
        self.assertEqual(messages, startup + ["Choose our next mission!"])
        self.assertEqual(messages[0], "Grand Theft Walk is online!")

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

    def test_help_info_and_cursor(self):
        self.command("help")
        cursor = self.client.get("/chat").json["cursor"]
        self.command("info")
        messages = self.client.get(f"/chat?after={cursor}").json["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(
            messages[0], "San Andreas • mode: sidewalk • camera: third person"
        )
        self.assertEqual(self.client.get("/chat?after=invalid").json["cursor"], 2)
        self.assertEqual(self.client.get("/chat?after=-1").json["cursor"], 2)
        self.assertEqual(self.client.get("/chat?after=2").json["messages"], [])

    def test_info_combines_location_settings_and_mission_and_replaces_location_commands(
        self,
    ):
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
        self.state["missions"] = {
            "active": {"name": "Market", "position": [1932, 2, 3]}
        }
        self.write("state.json", self.state)
        self.command("location")
        self.command("loc")
        self.command("info")
        expected = (
            "Las Colinas, Los Santos • Jul 14 4:32 PM (afternoon) • rain, strong wind • "
            "heading northwest • mode: sidewalk • camera: third person • "
            "Current mission: Travel to Market • 1.2 mi away"
        )
        self.assertEqual(self.client.get("/chat").json["messages"], [expected])

    def test_console_loads_and_handles_empty_or_unknown_commands(self):
        response = self.client.get("/")
        try:
            self.assertIn(b"Grand Theft Walk Control", response.data)
            self.assertEqual(response.status_code, 200)
        finally:
            response.close()
        self.assertEqual(self.client.get("/cmd/%20").status_code, 400)
        self.command("unknown")
        self.command("mission")
        self.assertEqual(self.client.get("/chat").json["messages"], [])
        self.command("more")
        self.assertEqual(
            self.client.get("/chat").json["messages"][-1],
            "More: !up • !down • !center • !mode • !camera • !help • !info • Type !help <cmd> for details.",
        )
        self.command("help mission")
        self.assertEqual(
            self.client.get("/chat").json["messages"][-1],
            "Unknown command: !mission. Type !help for commands.",
        )


if __name__ == "__main__":
    unittest.main()
