"""Managed launch behavior without starting games, OBS, or a live stream."""

import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.config import load_parameters, select_game, rotation_seconds, twitch_settings
from streaming.obs import OBS
from chat.auth import OAuthTokens, REQUIRED_SCOPES
from chat.oauth import REQUIRED_SCOPES as OAUTH_SCOPES
from streaming import launch as launcher
from chat.twitch import CATEGORIES, STREAM_TAGS, StreamMetadata, TwitchBot


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.params = {"ROTATION": {"start_date": "2026-11-17", "timezone": "America/New_York",
                                    "games": ["san_andreas", "vice_city"]}}

    def test_rotation_interval_defaults_and_fractional_configuration(self):
        self.assertEqual(rotation_seconds({}), 7200)
        self.assertEqual(rotation_seconds({"ROTATION": {"interval_hours": 0.5}}), 1800)
        for value in (0, -1, True, "2", None, float("nan"), float("inf"), 8761):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "interval_hours"):
                rotation_seconds({"ROTATION": {"interval_hours": value}})

    def test_unfinished_game_resumes_and_completed_game_advances(self):
        for elapsed in (0, 2400, 7199):
            self.assertEqual(select_game(self.params, {"game": "vice_city", "elapsed_seconds": elapsed}), "vice_city")
        self.assertEqual(select_game(self.params, {"game": "san_andreas", "elapsed_seconds": 7200}), "vice_city")

    def test_legacy_calendar_settings_do_not_affect_selection(self):
        self.params["ROTATION"].update(start_date="unused", timezone="unused")
        self.assertEqual(select_game(self.params), "san_andreas")

    def test_invalid_and_single_game_rotations(self):
        for games in ([], ["unknown"], ["vice_city", "vice_city"], "vice_city"):
            with self.subTest(games=games), self.assertRaises(ValueError):
                select_game({"ROTATION": {"games": games}})
        self.assertEqual(select_game({"ROTATION": {"games": ["vice_city"]}}), "vice_city")

    def test_three_game_rotation_includes_gta_iii_and_wraps(self):
        self.params["ROTATION"]["games"].append("gta_iii")
        for current, game in (("san_andreas", "vice_city"), ("vice_city", "gta_iii"), ("gta_iii", "san_andreas")):
            self.assertEqual(select_game(self.params, {"game": current, "elapsed_seconds": 7200}), game)

    def test_local_dev_config_does_not_require_credentials_or_a_template(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "parameters.json"
            path.write_text('{"MODE": "dev"}', encoding="utf-8")
            self.assertEqual(load_parameters(path)["MODE"], "dev")


class ReadinessTests(unittest.TestCase):
    def test_old_controller_session_cannot_satisfy_startup(self):
        process = Mock(returncode=None)
        process.poll.return_value = None
        control = Mock()
        control.snapshot.side_effect = [
            {"ready": True, "session": "old"}, {"ready": False, "session": "new"},
            {"ready": True, "session": "new"}]
        with patch.object(launcher.time, "sleep"):
            self.assertEqual(launcher.wait_for_game(process, control, "old", 10)["session"], "new")

    def test_chat_exit_prevents_stream_start(self):
        with tempfile.TemporaryDirectory() as folder:
            child = Mock()
            child.poll.return_value = 1
            with self.assertRaisesRegex(RuntimeError, "exited"):
                launcher.wait_for_chat([child], Path(folder) / "ready.json")


class SessionLifecycleTests(unittest.TestCase):
    def run_mock_session(self, mode, chat_failure=False):
        import core.manager
        import core.processes
        import core.winmemory

        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            root = Path(folder)
            stack.enter_context(patch.object(launcher, "ROOT", root))
            stack.enter_context(patch.object(core.winmemory, "process_mutex"))
            stack.enter_context(patch.object(core.winmemory, "game_pids", return_value=[]))
            stack.enter_context(patch.object(core.manager, "session_command", return_value=(["python", "launch.py"], root)))
            job = stack.enter_context(patch.object(core.processes, "ProcessJob")).return_value
            obs = stack.enter_context(patch("streaming.obs.OBS")).return_value
            auth = stack.enter_context(patch("chat.auth.OAuthTokens")).return_value
            control = stack.enter_context(patch.object(launcher, "WalkerControl")).return_value
            control.state_dir = root
            control.snapshot.side_effect = RuntimeError("offline")
            game, chat = Mock(returncode=0), Mock()
            game.poll.return_value = 0
            spawn = stack.enter_context(patch.object(launcher, "spawn", side_effect=[game, chat]))
            stack.enter_context(patch.object(launcher, "wait_for_game"))
            ready = stack.enter_context(patch.object(launcher, "wait_for_chat"))
            stack.enter_context(patch.object(launcher.time, "sleep"))
            if chat_failure:
                ready.side_effect = RuntimeError("chat failed")
            params = {"CLIENT_ID": "app", "CLIENT_SECRET": "secret", "TWITCH_CHANNEL": "walker", "TWITCH_BOT_LOGIN": "walker"}
            if chat_failure:
                with self.assertRaisesRegex(RuntimeError, "chat failed"):
                    launcher.run_session(params, root / "parameters.json", "vice_city", mode)
            else:
                launcher.run_session(params, root / "parameters.json", "vice_city", mode)
            job.close.assert_called_once()
            self.assertIn("--game", spawn.call_args_list[1].args[1])
            self.assertIn("vice_city", spawn.call_args_list[1].args[1])
            return obs, auth, spawn

    def test_dev_launch_never_accesses_twitch_or_obs(self):
        obs, auth, spawn = self.run_mock_session("dev")
        obs.prepare.assert_not_called()
        auth.ensure_fresh.assert_not_called()
        self.assertIn("chat.dev", str(spawn.call_args_list[1]))

    def test_prod_starts_and_stops_owned_stream(self):
        obs, auth, spawn = self.run_mock_session("prod")
        auth.ensure_fresh.assert_called_once()
        obs.prepare.assert_called_once_with("vice_city")
        obs.start_stream.assert_called_once()
        obs.close.assert_called_once()
        self.assertIn("chat.twitch", str(spawn.call_args_list[1]))

    def test_chat_startup_failure_cleans_up_without_streaming(self):
        obs, _, _ = self.run_mock_session("prod", chat_failure=True)
        obs.start_stream.assert_not_called()
        obs.close.assert_called_once()


class TimedRotationTests(unittest.TestCase):
    def test_deadline_rotates_in_order_and_requests_controller_cleanup(self):
        import core.manager
        import core.processes

        for current, expected, elapsed in (("san_andreas", "vice_city", 0),
                                           ("gta_iii", "san_andreas", 0),
                                           ("gta_iii", "san_andreas", 2400)):
            with self.subTest(current=current), tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
                root = Path(folder)
                (root / "runtime").mkdir()
                (root / "runtime/rotation-progress-dev.json").write_text(json.dumps(
                    {"version": 1, "game": current, "elapsed_seconds": elapsed}))
                stack.enter_context(patch.object(launcher, "ROOT", root))
                command = stack.enter_context(patch.object(core.manager, "session_command", return_value=(["python"], root)))
                job = stack.enter_context(patch.object(core.processes, "ProcessJob")).return_value
                control = stack.enter_context(patch.object(launcher, "WalkerControl")).return_value
                control.state_dir = root
                control.snapshot.return_value = {"session": "new"}
                (root / "state.json").write_text('{"session": "old"}')
                (root / "control.json").write_text('{"walking": true}')
                game, chat = Mock(returncode=None), Mock()
                game.poll.return_value = chat.poll.return_value = None
                spawn = stack.enter_context(patch.object(launcher, "spawn", side_effect=[game, chat]))
                stack.enter_context(patch.object(launcher, "wait_for_game"))
                reports = []
                def ready(*args):
                    cli = spawn.call_args.args[1]
                    reports.append(json.loads(Path(cli[cli.index("--rotation-file") + 1]).read_text()))
                stack.enter_context(patch.object(launcher, "wait_for_chat", side_effect=ready))
                stack.enter_context(patch.object(launcher.time, "monotonic", side_effect=[100, 7299 - elapsed, 7300 - elapsed, 7300 - elapsed]))
                stack.enter_context(patch.object(launcher.time, "time", return_value=1000))
                sleep = stack.enter_context(patch.object(launcher.time, "sleep"))
                self.assertEqual(launcher.run_game_session({}, root / "params.json", current, "dev"), expected)
                self.assertEqual(reports[0]["next_switch_at"], 8200 - elapsed)
                self.assertEqual([call.args[0] for call in command.call_args_list], [current, expected])
                sleep.assert_called_once_with(0.5)
                self.assertTrue(json.loads((root / "control.json").read_text())["quit"])
                game.wait.assert_any_call(timeout=10)
                job.close.assert_called_once()
                self.assertEqual([p.name for p in (root / "runtime").glob("*.json")], ["rotation-progress-dev.json"])
                progress = json.loads((root / "runtime/rotation-progress-dev.json").read_text())
                self.assertEqual(select_game({}, progress), expected)

    def test_rotation_keeps_one_live_obs_stream_and_changes_scene(self):
        with (
            patch("core.winmemory.process_mutex"),
            patch("core.winmemory.game_pids", return_value=[]),
            patch("chat.auth.OAuthTokens"),
            patch("streaming.obs.OBS") as factory,
            patch.object(launcher, "run_game_session", side_effect=["gta_iii", "san_andreas", None]) as run,
        ):
            params = {"CLIENT_ID": "app", "CLIENT_SECRET": "secret", "TWITCH_CHANNEL": "walker", "TWITCH_BOT_LOGIN": "walker"}
            launcher.run_session(params, Path("params.json"), "vice_city", "prod")
        obs = factory.return_value
        obs.prepare.assert_called_once_with("vice_city")
        self.assertEqual([call.args[0] for call in obs.select_game.call_args_list], ["gta_iii", "san_andreas"])
        obs.close.assert_called_once()
        self.assertEqual([call.args[5] for call in run.call_args_list], [None, "vice_city", "gta_iii"])


class OBSOwnershipTests(unittest.TestCase):
    def test_existing_live_stream_is_never_stopped(self):
        client = Mock()
        client.get_stream_status.return_value.output_active = True
        obs = OBS({"settle_seconds": 0})
        with patch("streaming.obs.psutil.process_iter", return_value=[SimpleNamespace(info={"name": "obs64.exe"})]), \
                patch("streaming.obs.obsws_python.ReqClient", return_value=client), \
                patch("streaming.obs.subprocess.Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "already streaming"):
                obs.prepare("vice_city")
            obs.close()
        popen.assert_not_called()
        client.stop_stream.assert_not_called()

    def test_scene_selected_before_stream_and_owned_stream_stops(self):
        client = Mock()
        client.get_stream_status.return_value.output_active = False
        obs = OBS({"scenes": {"vice_city": "VC"}, "settle_seconds": 0})
        with patch("streaming.obs.psutil.process_iter", return_value=[SimpleNamespace(info={"name": "obs64.exe"})]), \
                patch("streaming.obs.obsws_python.ReqClient", return_value=client):
            obs.prepare("vice_city")
        client.set_current_program_scene.assert_called_once_with("VC")
        client.start_stream.assert_not_called()
        client.get_stream_status.return_value.output_active = True
        obs.start_stream()
        obs.close()
        client.stop_stream.assert_called_once()


class TokenTests(unittest.TestCase):
    def test_missing_token_file_explains_authorization_step(self):
        with tempfile.TemporaryDirectory() as folder:
            tokens = OAuthTokens({}, Path(folder) / "missing.json")
            with self.assertRaisesRegex(RuntimeError, "-m chat.oauth"):
                tokens.ensure_fresh()

    def test_refresh_preserves_rotated_token_and_validates_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "oauth_tokens.json"
            path.write_text(json.dumps({"refresh_token": "old", "expires_at": 0}))
            tokens = OAuthTokens({"CLIENT_ID": "app", "CLIENT_SECRET": "secret", "TWITCH_BOT_LOGIN": "walker"}, path)
            refreshed = Mock(status_code=200)
            refreshed.json.return_value = {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}
            validated = Mock(status_code=200)
            validated.json.return_value = {"client_id": "app", "login": "walker", "scopes": ["chat:read", "chat:edit", "channel:manage:broadcast"]}
            with patch("chat.auth.requests.post", return_value=refreshed) as post, \
                    patch("chat.auth.requests.get", return_value=validated) as get:
                tokens.ensure_fresh()
                tokens.ensure_fresh()
            self.assertEqual(json.loads(path.read_text())["refresh_token"], "new-refresh")
            self.assertEqual(post.call_count, 1)
            self.assertEqual(get.call_count, 1)

    def test_wrong_account_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tokens.json"
            path.write_text(json.dumps({"access_token": "access", "expires_at": time.time() + 3600}))
            tokens = OAuthTokens({"CLIENT_ID": "app", "TWITCH_BOT_LOGIN": "walker"}, path)
            response = Mock(status_code=200)
            response.json.return_value = {"client_id": "app", "login": "someone_else", "scopes": ["chat:read", "chat:edit"]}
            with patch("chat.auth.requests.get", return_value=response), self.assertRaisesRegex(RuntimeError, "wrong"):
                tokens.ensure_fresh()


class TwitchTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_twitch_routes_viewer_identity_and_ignores_echo(self):
        bot = Mock()
        bot.params = {"TWITCH_CHANNEL": "walker"}
        bot.chat.process_chat_message = AsyncMock()
        message = SimpleNamespace(echo=False, channel=SimpleNamespace(name="Walker"),
                                  author=SimpleNamespace(name="viewer"), content="!yes")
        await TwitchBot.event_message(bot, message)
        context, content = bot.chat.process_chat_message.call_args.args
        self.assertEqual(content, "!yes")
        self.assertEqual(context.author.name, "viewer")
        message.echo = True
        await TwitchBot.event_message(bot, message)
        self.assertEqual(bot.chat.process_chat_message.call_count, 1)


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.params = {"CLIENT_ID": "app", "CLIENT_SECRET": "secret",
                       "TWITCH_CHANNEL": "walker", "TWITCH_BOT_LOGIN": "walker"}
        self.tokens = Mock()
        self.tokens.ensure_fresh.return_value = {"access_token": "fresh-token"}
        self.state = {"ready": True, "location": {
            "places": ["Red County"], "date": "Jan 29", "time": "5:20 PM",
            "period": "evening", "weather": "cloudy", "heading": "north"},
            "settings": {"mode": "free"}, "missions": {"active": {"name": "Excluded"}}}

    def responses(self, game):
        return [Mock(status_code=200, json=lambda: {"data": [{"id": "broadcaster"}]}),
                Mock(status_code=200, json=lambda: {"data": [{"id": "category", "name": CATEGORIES[game]}]}),
                Mock(status_code=204)]

    def test_category_and_exact_location_title_for_both_games(self):
        for game in CATEGORIES:
            with self.subTest(game=game), patch("chat.twitch.requests.request", side_effect=self.responses(game)) as request:
                metadata = StreamMetadata(self.params, self.tokens, game)
                self.assertTrue(metadata.update(self.state, now=0))
                self.assertEqual(request.call_args_list[1].kwargs["params"], {"name": CATEGORIES[game]})
                self.assertEqual(request.call_args.kwargs["json"], {
                    "game_id": "category", "tags": STREAM_TAGS,
                    "title": "Red County • Jan 29 5:20 PM (evening) • cloudy • heading north"})
                self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer fresh-token")

    def test_only_changes_and_periodic_refresh_write_and_failures_retry(self):
        metadata = StreamMetadata(self.params, self.tokens, "san_andreas")
        with patch("chat.twitch.requests.request", side_effect=self.responses("san_andreas")) as request:
            metadata.update(self.state, now=0)
            self.assertFalse(metadata.update(self.state, now=30))
            self.assertEqual(request.call_count, 3)
            request.side_effect = None
            request.return_value = Mock(status_code=204)
            self.assertTrue(metadata.update(self.state, now=300))
            self.state["location"]["heading"] = "west"
            request.return_value = Mock(status_code=503)
            with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                metadata.update(self.state, now=330)
            request.return_value = Mock(status_code=204)
            self.assertTrue(metadata.update(self.state, now=360))
            self.assertIn("heading west", request.call_args.kwargs["json"]["title"])

    def test_loading_and_empty_location_do_not_touch_twitch(self):
        metadata = StreamMetadata(self.params, self.tokens, "vice_city")
        with patch("chat.twitch.requests.request") as request:
            self.assertFalse(metadata.update({"ready": False, "location": self.state["location"]}))
            self.assertFalse(metadata.update({"ready": True}))
            request.assert_not_called()

    def test_missing_category_never_patches_channel(self):
        metadata = StreamMetadata(self.params, self.tokens, "vice_city")
        responses = self.responses("vice_city")
        responses[1].json = Mock(return_value={"data": []})
        with patch("chat.twitch.requests.request", side_effect=responses) as request:
            with self.assertRaisesRegex(RuntimeError, "category was not found"):
                metadata.update(self.state)
            self.assertEqual(request.call_count, 2)

    def test_broadcaster_identity_and_permission_are_required(self):
        self.assertIs(REQUIRED_SCOPES, OAUTH_SCOPES)
        self.assertIn("channel:manage:broadcast", REQUIRED_SCOPES)
        self.params["TWITCH_BOT_LOGIN"] = "otherbot"
        with self.assertRaisesRegex(ValueError, "broadcaster"):
            twitch_settings(self.params)


if __name__ == "__main__":
    unittest.main()
