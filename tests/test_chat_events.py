"""Transport-independent startup, vote, and mission event delivery."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from chat.bot import ChatBot
from core.files import atomic_json
from core.runtime import WalkerControl


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
    def test_switch_announces_info_and_countdown_and_clamps_at_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            state = {"game": "vice_city", "protocol": 1, "controller": "running",
                     "ready": True, "updated": time.time(), "session": "new", "settings": {}}
            atomic_json(Path(folder) / "state.json", state)
            rotation = Path(folder) / "rotation.json"
            atomic_json(rotation, {"game": "vice_city", "switched_from": "san_andreas", "next_switch_at": 10000})
            bot = ChatBot(WalkerControl(folder, game="vice_city"), rotation_file=rotation)
            self.addCleanup(bot.close)
            with patch("chat.bot.time.time", return_value=5320), patch.object(bot.control, "snapshot", return_value=state):
                messages = bot.poll_events(now=0)
                self.assertIn("Switched to", messages[0])
                self.assertIn("Next game in: 1h18m", messages[2])
                self.assertIn("Next game in: 1h18m", bot._reply("info", []))
                self.assertEqual(bot.poll_events(now=1), [])
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
