"""Exercise HTTP chat commands through to the walker's file protocol."""

import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from chat.bot import ANNOUNCEMENT_INTERVAL, ChatBot
from chat.dev import create_app
from core.files import atomic_json
from core.runtime import UnsupportedCommandError, WalkerControl
from games import Capabilities, get_game


class ChatFixture(unittest.TestCase):
    game = "san_andreas"

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.settings = {
            "walking": False,
            "mode": "explore",
            "first_person": False,
            "protection": True,
            "autosave": True,
            "quit": False,
        }
        self.write("control.json", self.settings)
        self.state = {
            "game": self.game,
            "protocol": 1,
            "controller": "running",
            "updated": time.time(),
            "ready": True,
            "startup_ready": True,
            "session": "session-a",
            "walking": "stopped",
            "settings": self.settings,
            "position": {"x": 1, "y": 2, "z": 3},
            "interior": 0,
            "capabilities": {name: True for name in Capabilities().report()},
        }
        self.write("state.json", self.state)
        self.app = create_app(self.root, start_runtime=False, game=self.game)
        self.client = self.app.test_client()
        self.voting = self.app.extensions["gtw_bot"].command_voting
        self.timer = Mock()
        self.voting.timer_factory = self.timer

    def tearDown(self):
        self.app.extensions["gtw_bot"].close()
        self.folder.cleanup()

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


class ChatProtocolContract:
    """Game identity, capability discovery, and session isolation for each adapter."""

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
                "Explore mode: direction queued for the next junction. Use !mode free for manual movement.",
                "Autowalk toggled",
            ],
        )

    def test_respawn_vote_rechecks_session_and_capability_when_it_passes(self):
        for changed in ({"session": "replacement"}, {"capabilities": {}}):
            with self.subTest(changed=changed):
                self.write("state.json", self.state)
                before = self.control()
                self.command("respawn")
                self.write("state.json", dict(self.state, **changed))
                self.timer.call_args.args[1]()
                self.assertEqual(self.control(), before)
                self.assertTrue(any("Could not apply voted command" in message for message in self.client.get("/chat").json["messages"]))

    def test_other_game_state_cannot_receive_commands_or_votes(self):
        self.state["game"] = "vice_city" if self.game == "san_andreas" else "san_andreas"
        self.write("state.json", self.state)
        for command in ("walk", "camera first", "vote 1"):
            self.command(command)
        self.assertEqual(self.control(), self.settings)
        self.assertFalse(self.voting.vote.active)
        self.assertFalse((self.root / "mission_votes").exists())

    def test_help_lists_only_available_commands_and_updates_with_capabilities(self):
        self.state["capabilities"].update(jump=False, first_person=False, view=False)
        self.write("state.json", self.state)
        self.command("help")
        self.command("more")
        self.command("help yes")
        messages = self.client.get("/chat").json["messages"]
        self.assertNotIn("!jump", messages[0])
        for command in ("!camera", "!up", "!down", "!center"):
            self.assertNotIn(command, messages[1])
        self.assertNotIn("camera", messages[2])
        self.assertIn("Vote yes", messages[2])
        self.state["capabilities"]["view"] = True
        self.write("state.json", self.state)
        self.command("more")
        self.command("help u")
        messages = self.client.get("/chat").json["messages"]
        for command in ("!up", "!down", "!center"):
            self.assertIn(command, messages[-2])
        self.assertIn("Look up", messages[-1])

    def test_missing_capabilities_keep_only_original_san_andreas_compatibility(self):
        self.state.pop("capabilities")
        self.write("state.json", self.state)
        control = self.app.extensions["gtw_bot"].control
        if self.game == "san_andreas":
            control.request("up", [])
            self.assertEqual(self.control()["view"]["direction"], "up")
        else:
            with self.assertRaises(UnsupportedCommandError):
                control.request("up", [])
            self.assertEqual(self.control(), self.settings)
        self.state["capabilities"] = {"view": "true"}
        self.write("state.json", self.state)
        with self.assertRaises(UnsupportedCommandError):
            control.request("up", [])


class ChatIntegrationTests(ChatProtocolContract, ChatFixture):
    """Shared HTTP behavior; run once against the default game."""

    def test_camera_and_mode_settings_validate_arguments(self):
        self.command("camera")
        self.assertTrue(self.control()["first_person"])
        self.command("camera third")
        self.assertFalse(self.control()["first_person"])
        self.command("mode free")
        self.assertEqual(self.control()["mode"], "free")
        self.timer.assert_not_called()
        expected = self.control()
        for command in ("mode fly", "camera upside-down"):
            self.command(command)
            self.assertEqual(self.control(), expected)

    def test_mission_mode_accepts_commands_and_queues_turns(self):
        self.command("mode mission")
        self.assertEqual(self.control()["mode"], "mission")
        self.command("walk")
        self.command("left")
        self.assertEqual(self.control()["steering"]["direction"], "left")
        self.assertEqual(
            self.client.get("/chat").json["messages"][-1],
            "Mission mode: direction queued for the next junction. Use !mode free for manual movement.",
        )
        expected = self.control()
        self.command("back")
        self.assertEqual(self.control(), expected)
        self.assertEqual(
            self.client.get("/chat").json["messages"][-1],
            "Back is unavailable in mission mode. Use !mode free for manual movement.",
        )
        self.timer.assert_not_called()

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
                "Back is unavailable in explore mode. Use !mode free for manual movement."
            ],
        )

    def test_view_commands_work_in_all_modes_without_replacing_steering(self):
        for mode in ("free", "mission", "explore"):
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
        with self.assertLogs("chat.bot", level="WARNING"):
            for command in ("up 0", "down 101", "center 5", "up nope"):
                self.command(command)
                self.assertEqual(self.control(), expected)

    def test_stopped_explore_directions_explain_how_to_move(self):
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
                    "Explore mode: direction queued for the next junction. "
                    "Use !walk to continue or !mode free for manual movement."
                )
            ]
            * 3,
        )

    def test_mode_command_uses_singular_name(self):
        self.command("mode free")
        self.command("mode explore")
        self.assertEqual(self.control()["mode"], "explore")
        self.assertEqual(
            self.client.get("/chat").json["messages"][-1], "Navigation mode: explore."
        )

    def test_camera_and_mode_apply_immediately_without_ballots(self):
        self.command("camera first")
        self.assertTrue(self.control()["first_person"])
        other = self.app.test_client()
        other.get("/cmd/no")
        self.command("no")
        self.command("yes")
        self.assertTrue(self.control()["first_person"])
        self.command("mode free")
        self.assertEqual(self.control()["mode"], "free")
        other.get("/cmd/camera third")
        self.assertFalse(self.control()["first_person"])
        self.assertFalse(self.voting.vote.active)
        self.timer.assert_not_called()
        self.assertEqual(self.client.get("/chat").json["messages"], [
            "Camera: first person.", "Navigation mode: free.", "Camera: third person.",
        ])

    def test_mission_ballots_and_announcements_still_work_with_immediate_camera(self):
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
        self.assertTrue(self.control()["first_person"])
        self.assertFalse(self.voting.vote.active)
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
        self.assertIn("mode: explore", message)
        self.assertNotIn("camera:", message)
        self.assertTrue(message.endswith("Type !help for commands"))

    def test_mission_distance_updates_in_info_replies(self):
        self.state["camera_heading"] = 0
        self.state["missions"] = {
            "active": {"name": "Market", "position": [301, 402, 10]},
            "held_seconds": 0,
            "hold_seconds": 30,
        }
        for position, expected in (
            ({"x": 1, "y": 2, "z": 3}, "↗ 1,640 ft"),
            ({"x": 301, "y": 402, "z": 10}, "0 ft"),
            ({"x": 300, "y": 402, "z": 10}, "→ 3 ft"),
            ({"x": -699, "y": 402, "z": 10}, "→ 3,281 ft"),
            ({"x": 301 - 1609.344, "y": 402, "z": 10}, "→ 1 mi"),
            ({"x": -12199, "y": 402, "z": 10}, "→ 7.8 mi"),
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
        with self.assertLogs("chat.bot", level="WARNING"):
            self.command("jump 100")
        self.assertEqual(self.control(), expected)

    def test_drop_queues_a_silent_request_and_rejects_unsupported_games(self):
        self.write("control.json", dict(self.settings, walking=True))
        self.command("drop")
        request = self.control()["jump"]
        self.assertEqual(request["direction"], "drop")
        self.assertTrue(self.control()["walking"])
        self.assertEqual(self.client.get("/chat").json["messages"], [])
        before = self.control()
        with self.assertLogs("chat.bot", level="WARNING"):
            self.command("drop 2")
        self.assertEqual(self.control(), before)
        self.state["capabilities"]["ledge_drop"] = False
        self.write("state.json", self.state)
        self.command("drop")
        self.assertEqual(self.control(), before)
        self.assertIn("not available", self.client.get("/chat").json["messages"][-1].lower())

    def test_unsupported_respawn_does_not_open_a_vote_or_write_controls(self):
        self.state["capabilities"]["respawn"] = False
        self.write("state.json", self.state)
        before = self.control()
        self.command("respawn")
        self.assertFalse(self.voting.vote.active)
        self.assertEqual(self.control(), before)
        self.timer.assert_not_called()
        self.assertIn("not available", self.client.get("/chat").json["messages"][-1])

    def test_respawn_vote_waits_sixty_seconds_then_queues_once(self):
        before = self.control()
        self.command("respawn")
        self.assertEqual(self.control(), before)
        self.assertEqual(self.timer.call_args.args[0], 60)
        self.assertIn("60 seconds", self.client.get("/chat").json["messages"][-1])
        halfway = self.timer.call_args_list[-2]
        self.assertEqual(halfway.args[0], 30)
        with patch("core.missions.time.monotonic", return_value=self.voting.closes_at-30):
            halfway.args[1]()
        reminder = self.client.get("/chat").json["messages"][-1]
        self.assertIn("Vote halfway!", reminder)
        self.assertIn("30 seconds remaining", reminder)
        self.assertEqual(self.control(), before)
        other = self.app.test_client()
        other.get("/cmd/no")
        other.get("/cmd/yes")
        self.command("respawn")
        self.assertIn("Vote already in progress", self.client.get("/chat").json["messages"][-1])
        callback = self.timer.call_args.args[1]
        callback()
        request = self.control()["respawn"]
        self.assertEqual(request["session"], self.state["session"])
        self.assertLess(abs(time.time() - request["issued"]), 2)
        callback()
        self.assertEqual(self.control()["respawn"], request)
        self.assertTrue(any("Vote passed! (2-0)" in message for message in self.client.get("/chat").json["messages"]))

    def test_respawn_tie_fails_and_invalid_arguments_never_start_votes(self):
        before = self.control()
        self.command("respawn now")
        self.assertFalse(self.voting.vote.active)
        self.command("respawn")
        other = self.app.test_client()
        other.get("/cmd/no")
        self.timer.call_args.args[1]()
        self.assertEqual(self.control(), before)
        self.assertTrue(any("Vote failed! (1-1)" in message for message in self.client.get("/chat").json["messages"]))

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
        app = create_app(self.root, start_runtime=False, game=self.game)
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

    def test_server_start_delivers_pending_arrival_for_active_mission_once(self):
        arrival = {"id": "arrival", "text": "Mission location reached!  Stay inside the area for 30 seconds."}
        self.state["missions"] = {
            "active": {"name": "Airport", "arrival_event_id": "arrival"},
            "events": [
                {"id": "old-complete", "text": "Mission complete: old destination!"},
                arrival,
            ],
        }
        self.write("state.json", self.state)
        app = create_app(self.root, start_runtime=False, game=self.game)
        self.addCleanup(app.extensions["gtw_bot"].close)
        client = app.test_client()
        self.assertEqual(client.get("/chat").json["messages"], [arrival["text"]])
        self.assertEqual(client.get("/chat?after=1").json["messages"], [])

    def test_live_mission_events_wait_until_startup_messages_are_delivered(self):
        with patch("chat.dev.ChatBot.start_runtime") as start:
            app = create_app(self.root, game=self.game)
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
        with self.assertLogs("chat.bot", level="WARNING"):
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
            messages[0], f"Game: {get_game(self.game).title} • mode: explore • Type !help for commands"
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
            f"Game: {get_game(self.game).title} • "
            "Las Colinas, Los Santos • Jul 14 4:32 PM • rain, strong wind • "
            "heading northwest • mode: explore • "
            "Current mission: Travel to Market • 1.2 mi away • Type !help for commands"
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
        self.command("unknown-mode")
        self.assertEqual(self.client.get("/chat").json["messages"], [])
        self.command("more")
        self.assertEqual(
            self.client.get("/chat").json["messages"][-1],
            "More: !up • !down • !center • !free • !explore • !mission • !camera • !help • !info • Type !help <cmd> for details.",
        )
        self.command("help mission")
        self.assertEqual(
            self.client.get("/chat").json["messages"][-1],
            "!mission • Navigate toward the current mission.",
        )

    def test_direct_mode_commands_preserve_walk_setting(self):
        for walking in (False, True):
            settings = self.control()
            settings['walking'] = walking
            self.write('control.json', settings)
            for mode in ('free', 'explore', 'mission'):
                self.command(mode)
                self.assertEqual(self.control()['mode'], mode)
                self.assertEqual(self.control()['walking'], walking)
                self.assertEqual(self.client.get('/chat').json['messages'][-1],
                                 f'Navigation mode: {mode}.')

    def test_navigation_endpoint_exposes_map_without_control_mutations(self):
        self.state['navigation'] = {'exploration': {'visited_cells': 7}}
        self.write('state.json', self.state)
        before = self.control()
        response = self.client.get('/navigation')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['navigation']['exploration']['visited_cells'], 7)
        self.assertEqual(response.json['mode'], 'explore')
        self.assertEqual(self.control(), before)

    def test_camera_command_has_no_delayed_effect_in_replacement_session(self):
        self.command("camera first")
        self.assertTrue(self.control()["first_person"])
        self.state["session"] = "replacement-session"
        self.write("state.json", self.state)
        self.command("camera third")
        self.assertFalse(self.control()["first_person"])
        self.timer.assert_not_called()
        self.assertEqual(self.client.get("/chat").json["messages"], [
            "Camera: first person.", "Camera: third person.",
        ])

    def test_partial_backend_rejects_jump_and_camera_before_writing_or_starting_votes(self):
        self.state["capabilities"].update(jump=False, first_person=False)
        self.write("state.json", self.state)
        for command in ("jump", "j", "camera", "camera first", "help jump", "help j", "help camera"):
            self.command(command)
            self.assertEqual(self.control(), self.settings)
        messages = self.client.get("/chat").json["messages"]
        self.assertEqual(len(messages), 7)
        self.assertTrue(all("not available" in message for message in messages))
        self.assertFalse(self.voting.vote.active)
        self.timer.assert_not_called()

    def test_unsupported_view_commands_and_aliases_receive_explicit_replies(self):
        self.state["capabilities"]["view"] = False
        self.write("state.json", self.state)
        for command in ("up", "u 20", "down", "d", "center", "help u"):
            self.command(command)
            self.assertEqual(self.control(), self.settings)
        messages = self.client.get("/chat").json["messages"]
        self.assertEqual(len(messages), 6)
        self.assertTrue(all("not available" in message for message in messages))

    def test_direct_control_requests_cannot_bypass_capability_checks(self):
        self.state["capabilities"].update(jump=False, first_person=False, view=False)
        self.write("state.json", self.state)
        control = self.app.extensions["gtw_bot"].control
        for command in ("up", "down", "center", "jump", "camera"):
            with self.assertRaises(UnsupportedCommandError):
                control.request(command, [])
            self.assertEqual(self.control(), self.settings)

    def test_camera_command_rechecks_capability_on_each_request(self):
        self.command("camera first")
        self.assertTrue(self.control()["first_person"])
        expected = self.control()
        self.state["capabilities"]["first_person"] = False
        self.write("state.json", self.state)
        self.command("camera third")
        self.assertEqual(self.control(), expected)
        messages = self.client.get("/chat").json["messages"]
        self.assertTrue(any("!camera is not available" in message
                            for message in messages))


class ViceCityChatIntegrationTests(ChatProtocolContract, ChatFixture):
    """Backend-sensitive interaction contract with simulated Vice City controller reports."""

    game = "vice_city"


class GTAIIIChatIntegrationTests(ChatProtocolContract, ChatFixture):
    """Backend-sensitive interaction contract with simulated GTA III controller reports."""

    game = "gta_iii"


class ChatEventTests(unittest.TestCase):
    def test_startup_precedes_pending_arrival_and_old_events_are_not_replayed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = {
                "game": "vice_city", "protocol": 1, "controller": "running",
                "ready": True, "updated": time.time(), "session": "session-a",
                "settings": {},
                "missions": {
                    "active": {"name": "Airport", "position": [0, 0, 0], "arrival_event_id": "arrival"},
                    "events": [{"id": "old", "text": "Old completion"},
                               {"id": "arrival", "text": "Destination reached"}],
                },
            }
            atomic_json(path, state)
            bot = ChatBot(WalkerControl(folder, game="vice_city"))
            self.addCleanup(bot.close)
            messages = bot.poll_events(now=0)
            self.assertEqual(messages[0], "Grand Theft Walk is online!")
            self.assertEqual(messages[-1], "Destination reached")
            self.assertNotIn("Old completion", messages)
            self.assertEqual(bot.poll_events(now=1), [])
            state["missions"]["events"].append({"id": "next", "text": "Choose the next mission"})
            atomic_json(path, state)
            self.assertEqual(bot.poll_events(now=2), ["Choose the next mission"])
            self.assertEqual(bot.poll_events(now=3), [])

    def test_new_session_announces_before_its_mission_events(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = {"game": "san_andreas", "controller": "running", "ready": True,
                     "updated": time.time(), "session": "first", "missions": {"events": []}}
            atomic_json(path, state)
            bot = ChatBot(WalkerControl(folder))
            self.addCleanup(bot.close)
            bot.poll_events(now=0)
            state.update(session="second", missions={"events": [{"id": "new", "text": "New mission"}]})
            atomic_json(path, state)
            messages = bot.poll_events(now=1)
            self.assertEqual(messages[0], "Grand Theft Walk is online!")
            self.assertEqual(messages[-1], "New mission")


class RotationChatTests(unittest.TestCase):
    def test_switch_warning_precedes_rotation_once_and_info_countdown_clamps_at_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            state = {"game": "vice_city", "protocol": 1, "controller": "running",
                     "ready": True, "updated": time.time(), "session": "new", "settings": {}}
            atomic_json(Path(folder) / "state.json", state)
            rotation = Path(folder) / "rotation.json"
            atomic_json(rotation, {"game": "vice_city", "switched_from": "san_andreas",
                                   "next_game": "gta_iii", "next_switch_at": 10000,
                                   "rotation_id": "round-a"})
            bot = ChatBot(WalkerControl(folder, game="vice_city"), rotation_file=rotation)
            self.addCleanup(bot.close)
            with patch("chat.bot.time.time", return_value=5320), patch.object(bot.control, "snapshot", return_value=state):
                messages = bot.poll_events(now=0)
                self.assertEqual(messages[0], "Grand Theft Walk is online!")
                self.assertFalse(any('Switched to' in message for message in messages))
                self.assertIn("Next game in: 1h18m", messages[2])
                self.assertIn("Next game in: 1h18m", bot._reply("info", []))
                self.assertEqual(bot.poll_events(now=1), [])
            with patch.object(bot.control, "snapshot", return_value=state):
                with patch("chat.bot.time.time", return_value=9399):
                    self.assertEqual(bot.poll_events(now=2), [])
                with patch("chat.bot.time.time", return_value=9400):
                    self.assertEqual(bot.poll_events(now=3), ["Switching to GTA III in 10 minutes."])
                    self.assertEqual(bot.poll_events(now=4), [])
                atomic_json(rotation, {"game": "vice_city", "next_game": "gta_iii",
                                       "next_switch_at": 10001, "rotation_id": "round-a"})
                with patch("chat.bot.time.time", return_value=9401):
                    self.assertEqual(bot.poll_events(now=5), [])
                with patch("chat.bot.time.time", return_value=10001):
                    self.assertEqual(bot.poll_events(now=5), [])
            with patch("chat.bot.time.time", return_value=10001):
                self.assertIn("Next game in: 0h00m", bot._info_reply(state))

    def test_single_game_and_unmanaged_chat_omit_countdown(self):
        with tempfile.TemporaryDirectory() as folder:
            state = {"ready": True, "settings": {}}
            rotation = Path(folder) / "rotation.json"
            atomic_json(rotation, {"game": "vice_city", "next_switch_at": None})
            bot = ChatBot(WalkerControl(folder, game="vice_city"), rotation_file=rotation)
            self.addCleanup(bot.close)
            self.assertNotIn("Next game in:", bot._info_reply(state))
            rotation.unlink()
            self.assertNotIn("Next game in:", bot._info_reply(state))


class WaterIdleVoteTests(ChatFixture):
    def setUp(self):
        super().setUp()
        self.bot = self.app.extensions['gtw_bot']
        self.state.update(in_water=True, standing=False, in_air=False, health=100)
        self.write('state.json', self.state)

    def poll(self, now):
        with patch('chat.bot.time.monotonic', return_value=now):
            return self.bot.poll_events(include_announcements=False, now=now)

    def advance(self, first, last):
        return [message for now in range(first, last + 1) for message in self.poll(now)]

    def test_idle_swim_opens_with_one_yes_and_does_not_repeat(self):
        self.assertEqual(self.advance(0, 119), [])
        messages = self.poll(120)
        self.assertIn('Two minutes swimming', messages[0])
        self.assertIn('60 seconds', messages[0])
        self.assertEqual(self.voting.vote.votes, {'automatic:respawn': 'yes'})
        self.timer.call_args.args[1]()
        self.assertIn('respawn', self.control())
        self.assertIn('Vote passed! (1-0)', self.advance(121, 400)[0])
        self.assertFalse(self.voting.vote.active)
        self.assertEqual(self.timer.call_count, 2)

    def test_valid_command_resets_inactivity_without_resetting_swim_duration(self):
        self.advance(0, 100)
        with patch('chat.bot.time.monotonic', return_value=100):
            self.command('left')
        self.assertEqual(self.advance(101, 219), [])
        self.assertIn('Vote started!', self.poll(220)[0])

    def test_chatter_unknown_and_invalid_commands_do_not_reset_the_timer(self):
        self.advance(0, 100)
        asyncio.run(self.bot.process_chat_message(None, 'ordinary chat'))
        self.command('notacommand')
        self.command('left 101')
        self.assertIsNone(self.bot.last_game_command)
        self.assertIn('Vote started!', self.advance(101, 120)[0])

    def test_automatic_vote_cancels_if_land_is_reached_before_it_passes(self):
        self.advance(0, 120)
        with patch('chat.bot.time.monotonic', return_value=121):
            self.voting.cast('viewer', 'yes')
        self.state['in_water'] = False
        self.write('state.json', self.state)
        self.timer.call_args.args[1]()
        self.assertNotIn('respawn', self.control())
        self.assertIn('back on land', self.voting.drain_events()[-1])

    def test_paused_time_does_not_spend_the_swimming_budget(self):
        self.advance(0, 100)
        self.state['paused'] = True
        self.write('state.json', self.state)
        self.assertEqual(self.advance(101, 220), [])
        self.state['paused'] = False
        self.write('state.json', self.state)
        self.assertEqual(self.advance(221, 239), [])
        self.assertIn('Vote started!', self.poll(240)[0])

    def test_other_games_do_not_start_automatic_water_votes(self):
        self.state['game'] = 'vice_city'
        self.bot.control.game = get_game('vice_city')
        self.write('state.json', self.state)
        self.assertEqual(self.advance(0, 130), [])
        self.assertFalse(self.voting.vote.active)


if __name__ == "__main__":
    unittest.main()
