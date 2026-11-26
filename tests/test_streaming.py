"""Managed launch behavior without starting games, OBS, or a live stream."""

from tests.fixtures import StateTestCase
import json
import tempfile
import time
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from chat.oauth import OAuthTokens, save_tokens, REQUIRED_SCOPES as OAUTH_SCOPES, REQUIRED_SCOPES
from chat.bot import ChatBot
from chat.twitch import CATEGORIES, STREAM_TAGS, StreamMetadata, TwitchBot
from core.config import load_parameters, select_game, twitch_settings
from core.files import StateStore, write_state, read_state
from games import get_game
from streaming import launch as launcher
from streaming.launch import OBS


class RotationTests(StateTestCase):


    def test_invalid_and_single_game_rotations(self):
        for games in ([], ["unknown"], ["vice_city", "vice_city"], "vice_city"):
            with self.subTest(games=games), self.assertRaises(ValueError):
                select_game({"ROTATION": {"games": games}})
        self.assertEqual(select_game({"ROTATION": {"games": ["vice_city"]}}), "vice_city")


    def test_local_dev_config_does_not_require_credentials_or_a_template(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "parameters.json"
            path.write_text('{"MODE": "dev"}', encoding="utf-8")
            self.assertEqual(load_parameters(path)["MODE"], "dev")


class ReadinessTests(StateTestCase):
    def test_scheduled_game_closes_after_controller_saves(self):
        game = get_game('san_andreas')
        control = Mock(game=game, state_dir=game.state_dir)
        control.snapshot.return_value = {'pid': 123}
        write_state(game.state_dir / 'control', {'walking': True})
        games = launcher.ManagedGames({})
        games.active = control
        process = Mock()
        process.exe.return_value = str(game.install_dir / game.executable)
        def saved(_):
            self.assertEqual(read_state(game.state_dir / 'control'), {'walking': False, 'quit': True})
            process.terminate.assert_not_called()
        with patch.object(launcher.time, 'sleep', side_effect=saved), \
             patch.object(launcher.psutil, 'Process', return_value=process) as find:
            games.close()
        find.assert_called_once_with(123)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=10)
        self.assertIsNone(games.active)

    def test_first_production_launch_authorizes_once_before_starting_session(self):
        params = {'CLIENT_ID':'app', 'CLIENT_SECRET':'secret', 'TWITCH_CHANNEL':'walker', 'TWITCH_BOT_LOGIN':'walker'}
        def grant(_):
            save_tokens(params, {'access_token':'authorized'})
        with patch.object(launcher, 'load_parameters', return_value=params), \
             patch('chat.oauth.authorize', side_effect=grant) as authorize, \
             patch.object(launcher.ctypes.windll.shell32, 'IsUserAnAdmin', return_value=True), \
             patch('core.winmemory.process_mutex', return_value=nullcontext(True)), \
             patch.object(launcher, 'show_console'), \
             patch.object(launcher, 'configure_logging'), patch.object(launcher, 'run_session') as run:
            launcher.main(['--mode','prod','--dry-run'])
            authorize.assert_not_called()
            launcher.main(['--mode','prod'])
            launcher.main(['--mode','prod'])
        authorize.assert_called_once_with(params)
        self.assertEqual(run.call_count, 2)

    def test_repeated_launch_requests_existing_session_without_elevation_or_duplicates(self):
        with patch.object(launcher,'load_parameters',return_value={'MODE':'dev'}), \
             patch('core.winmemory.process_mutex',return_value=nullcontext(False)), \
             patch.object(launcher,'run_session') as run, patch('core.winmemory.elevate') as elevate:
            launcher.main(['--mode','dev'])
        request=read_state(launcher.ROOT/'runtime/launch-request')
        self.assertEqual(request,{'mode':'dev','game':None})
        run.assert_not_called()
        elevate.assert_not_called()

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


class OBSOwnershipTests(StateTestCase):
    def test_existing_live_stream_is_never_stopped(self):
        client = Mock()
        client.get_stream_status.return_value.output_active = True
        obs = OBS({"profile": "Managed", "scene_collection": "Games",
                   "scenes": {"vice_city": "VC"}, "settle_seconds": 0})
        with patch("streaming.launch.psutil.process_iter", return_value=[SimpleNamespace(info={"name": "obs64.exe"})]), \
                patch("streaming.launch.obsws_python.ReqClient", return_value=client), \
                patch("streaming.launch.subprocess.Popen") as popen:
            obs.prepare("vice_city")
            obs.start_stream()
            obs.close()
        popen.assert_not_called()
        client.set_current_profile.assert_not_called()
        client.set_current_scene_collection.assert_not_called()
        client.set_current_program_scene.assert_called_once_with("VC")
        client.start_stream.assert_not_called()
        client.stop_stream.assert_not_called()
        client.disconnect.assert_called_once()
        self.assertFalse(obs.started_stream)

    def test_scene_selected_before_stream_and_owned_stream_stops(self):
        client = Mock()
        client.get_stream_status.return_value.output_active = False
        obs = OBS({"scenes": {"vice_city": "VC"}, "settle_seconds": 0})
        with patch("streaming.launch.psutil.process_iter", return_value=[SimpleNamespace(info={"name": "obs64.exe"})]), \
                patch("streaming.launch.obsws_python.ReqClient", return_value=client):
            obs.prepare("vice_city")
        client.set_current_program_scene.assert_called_once_with("VC")
        client.start_stream.assert_not_called()
        client.get_stream_status.side_effect = [SimpleNamespace(output_active=False), SimpleNamespace(output_active=True)]
        obs.start_stream()
        obs.close()
        client.start_stream.assert_called_once()
        client.stop_stream.assert_called_once()

    def test_stream_started_during_game_startup_is_reused(self):
        client = Mock()
        client.get_stream_status.return_value.output_active = False
        obs = OBS({"profile": "Managed", "scene_collection": "Games", "settle_seconds": 0})
        with patch("streaming.launch.psutil.process_iter", return_value=[SimpleNamespace(info={"name": "obs64.exe"})]), \
                patch("streaming.launch.obsws_python.ReqClient", return_value=client):
            obs.prepare("vice_city")
        client.set_current_profile.assert_called_once_with("Managed")
        client.set_current_scene_collection.assert_called_once_with("Games")
        client.get_stream_status.return_value.output_active = True
        obs.start_stream()
        obs.close()
        client.start_stream.assert_not_called()
        client.stop_stream.assert_not_called()

    def test_existing_stream_survives_scene_selection_failure(self):
        client = Mock()
        client.get_stream_status.return_value.output_active = True
        client.set_current_program_scene.side_effect = RuntimeError("Missing scene")
        obs = OBS({"scenes": {"vice_city": "VC"}, "settle_seconds": 0})
        with patch("streaming.launch.psutil.process_iter", return_value=[SimpleNamespace(info={"name": "obs64.exe"})]), \
                patch("streaming.launch.obsws_python.ReqClient", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "Missing scene"):
                obs.prepare("vice_city")
            obs.close()
        client.start_stream.assert_not_called()
        client.stop_stream.assert_not_called()
        client.disconnect.assert_called_once()


class TokenTests(StateTestCase):

    def test_missing_token_file_explains_authorization_step(self):
        with tempfile.TemporaryDirectory() as folder:
            tokens = OAuthTokens({})
            with self.assertRaisesRegex(RuntimeError, "-m chat.oauth"):
                tokens.ensure_fresh()

    def test_refresh_preserves_rotated_token_and_validates_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "oauth_tokens.json"
            tokens = OAuthTokens({"CLIENT_ID": "app", "CLIENT_SECRET": "secret", "TWITCH_BOT_LOGIN": "walker"})
            save_tokens(tokens.params, {"refresh_token":"old", "expires_at":0})
            refreshed = Mock(status_code=200)
            refreshed.json.return_value = {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}
            validated = Mock(status_code=200)
            validated.json.return_value = {"client_id": "app", "login": "walker", "scopes": ["chat:read", "chat:edit", "channel:manage:broadcast"]}
            with patch("chat.oauth.requests.post", return_value=refreshed) as post, \
                    patch("chat.oauth.requests.get", return_value=validated) as get:
                tokens.ensure_fresh()
                tokens.ensure_fresh()
            self.assertEqual(StateStore().read(tokens.key)["refresh_token"], "new-refresh")
            self.assertEqual(post.call_count, 1)
            self.assertEqual(get.call_count, 1)

    def test_wrong_account_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tokens.json"
            tokens = OAuthTokens({"CLIENT_ID": "app", "TWITCH_BOT_LOGIN": "walker"})
            save_tokens(tokens.params, {"access_token":"access", "expires_at":time.time()+3600})
            response = Mock(status_code=200)
            response.json.return_value = {"client_id": "app", "login": "someone_else", "scopes": ["chat:read", "chat:edit"]}
            with patch("chat.oauth.requests.get", return_value=response), self.assertRaisesRegex(RuntimeError, "wrong"):
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


class MetadataTests(StateTestCase):
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
                    "title": "Red County • Jan 29 5:20 PM • cloudy • heading north"})
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

    def test_title_ends_with_info_countdown_and_updates_when_minute_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rotation"
            write_state(path, {"game": "gta_iii", "next_game": "san_andreas",
                               "phase":"live", "deadline":10000})
            metadata = StreamMetadata(self.params, self.tokens, "gta_iii", path)
            with patch("chat.twitch.requests.request", side_effect=self.responses("gta_iii")) as request:
                with patch("chat.bot.time.time", return_value=5320):
                    self.assertTrue(metadata.update(self.state, now=0))
                self.assertTrue(metadata.last_title.endswith(" • Stream ends in 1h18m"))
                request.side_effect = None
                request.return_value = Mock(status_code=204)
                with patch("chat.bot.time.time", return_value=5380):
                    self.assertTrue(metadata.update(self.state, now=30))
                self.assertTrue(metadata.last_title.endswith(" • Stream ends in 1h17m"))
                self.state["location"]["places"] = ["A" * 200]
                with patch("chat.bot.time.time", return_value=10001):
                    metadata.update(self.state, now=60)
                self.assertEqual(len(metadata.last_title), 140)
                self.assertTrue(metadata.last_title.endswith(" • Stream ends in 0m"))

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


class ScheduledBroadcastTests(StateTestCase):
    def setUp(self):
        from core.config import BroadcastSchedule
        self.Schedule = BroadcastSchedule
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('core.files.DATABASE', self.root / 'runtime/app.sqlite3'))
        self.stack.enter_context(patch('core.config.ROOT', self.root))
        self.path = self.root / 'runtime/schedule-prod.json'
        self.now = self.at('2026-11-25T09:00:00')

    def at(self, value):
        from datetime import datetime
        return datetime.fromisoformat(value).replace(tzinfo=self.Schedule.ZONE).timestamp()

    def schedule(self, now=None):
        now = self.now if now is None else now
        with patch('core.config.time.time', return_value=now):
            return self.Schedule({}, now=now, path=self.path)

    def test_normal_day_has_four_full_sessions_and_two_hour_breaks(self):
        schedule = self.schedule()
        plan = schedule.upcoming(self.now)
        self.assertEqual([item['start'] for item in plan[:5]], [self.at(value) for value in (
            '2026-11-25T09:00:00','2026-11-25T13:00:00','2026-11-25T17:00:00',
            '2026-11-25T21:00:00','2026-11-26T09:00:00')])
        self.assertEqual([item['game'] for item in plan[:4]], ['san_andreas','vice_city','gta_iii','san_andreas'])
        self.assertTrue(schedule.start(self.now))
        schedule.finish(self.now + 7200)
        self.assertEqual(schedule.data['game'], 'vice_city')
        self.assertEqual(schedule.data['next_start_at'], self.now + 14400)

    def test_delay_moves_queue_and_too_late_game_waits_until_morning(self):
        schedule = self.schedule()
        delayed = self.at('2026-11-25T09:45:00')
        self.assertTrue(schedule.start(delayed))
        schedule.finish(delayed + 7200)
        self.assertEqual(schedule.data['next_start_at'], self.at('2026-11-25T13:45:00'))
        self.assertEqual(schedule.fit(self.at('2026-11-25T22:00:00')), self.at('2026-11-25T22:00:00'))
        self.assertEqual(schedule.fit(self.at('2026-11-25T22:00:01')), self.at('2026-11-26T09:00:00'))
        self.assertFalse(schedule.start(self.at('2026-11-25T22:01:00')))
        self.assertEqual(schedule.data['phase'], 'overnight')

    def test_uninterrupted_live_stream_retains_its_original_end_time(self):
        schedule = self.schedule()
        schedule.start(self.now)
        with patch('core.config.time.time', return_value=self.now + 900):
            restored = self.Schedule({}, now=self.now + 900, path=self.path, resume_live=True)
        self.assertEqual(restored.data['phase'], 'live')
        self.assertEqual(restored.data['started_at'], self.now)
        self.assertEqual(restored.data['deadline'], self.now + 7200)

    def test_explicit_launch_wakes_idle_scheduler_but_keeps_live_broadcast(self):
        schedule=self.schedule()
        schedule.start(self.now)
        schedule.finish(self.now+7200)
        schedule.request_start(self.now+7260)
        self.assertEqual(schedule.data['next_start_at'],self.now+7260)
        schedule.start(self.now+7260)
        deadline=schedule.data['deadline']
        schedule.request_start(self.now+7320)
        self.assertEqual(schedule.data['phase'],'live')
        self.assertEqual(schedule.data['deadline'],deadline)

    def test_restart_during_break_keeps_remaining_time(self):
        schedule = self.schedule()
        schedule.start(self.now)
        schedule.finish(self.now + 7200)
        restored = self.schedule(self.now + 9000)
        self.assertEqual(restored.data['next_start_at'], self.now + 14400)
        self.assertEqual(restored.data['game'], 'vice_city')

    def test_relaunch_retries_interrupted_game_immediately_in_full(self):
        schedule = self.schedule()
        schedule.start(self.now)
        with patch('core.config.time.time', return_value=self.now + 1800):
            schedule.save()  # Last confirmed activity before Python died.
        restored = self.schedule(self.now + 2400)
        self.assertEqual(restored.data['game'], 'san_andreas')
        self.assertEqual(restored.data['next_start_at'], self.now + 2400)
        restored.interrupt(self.now + 2400)
        restored = self.schedule(self.now + 2401)
        self.assertEqual(restored.data['next_start_at'], self.now + 2401)
        self.assertTrue(restored.start(self.now + 2401))
        self.assertEqual(restored.data['deadline'] - restored.data['started_at'], 7200)

    def test_overnight_restart_does_not_add_another_break(self):
        schedule = self.schedule()
        started = self.at('2026-11-25T21:00:00')
        schedule.start(started)
        with patch('core.config.time.time', return_value=started + 1800):
            schedule.save()
        morning = self.at('2026-11-26T09:00:00')
        restored = self.schedule(morning)
        self.assertEqual(restored.data['next_start_at'], morning)
        self.assertTrue(restored.start(morning))
        self.assertEqual(restored.data['game'], 'san_andreas')


    def test_exploration_games_share_sqlite_without_mixing_history(self):
        from core.exploration import ExploreNavigator
        paths = [self.root / f'games/{game}/runtime/exploration' for game in ('vice_city','gta_iii')]
        for index, path in enumerate(paths):
            navigator = ExploreNavigator(memory_path=path)
            navigator.observe((index * 120, 0, 0), 0)
            navigator.save_memory(0, force=True)
        for index, path in enumerate(paths):
            restored = ExploreNavigator(memory_path=path)
            self.assertEqual(dict(restored.coverage), {(index * 10, 0, 0):1})
            self.assertFalse(path.exists())


    def test_offline_metadata_uses_same_wording_without_ready_game(self):
        from chat.twitch import offline_title
        schedule = self.schedule()
        schedule.start(self.now)
        schedule.finish(self.at('2026-11-25T23:00:00'))
        with patch('chat.twitch.time.time', return_value=self.at('2026-11-25T23:00:00')):
            closing = dict(schedule.data, ending=True)
            self.assertEqual(offline_title(closing), f'Back tomorrow at 9:00 AM ET {chr(183)} {get_game("vice_city").title}')
            metadata = StreamMetadata({'CLIENT_ID':'app','TWITCH_CHANNEL':'walker'}, Mock(), 'san_andreas', self.path)
            metadata.broadcaster_id, metadata.game_id = 'user','category'
            metadata.request = Mock()
            self.assertFalse(metadata.update({}))
            metadata.request.assert_not_called()
            self.assertTrue(metadata.update({}, schedule=closing))
            self.assertIn('Back tomorrow', metadata.request.call_args.kwargs['json']['title'])
        from chat.dev import create_app
        app = create_app(start_runtime=False, schedule_record=self.path, managed=True)
        self.addCleanup(app.extensions['gtw_bot'].close)
        response = app.test_client().get('/schedule')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['phase'],'overnight')

    def test_twitch_sync_is_idempotent_preserves_manual_entries_and_recovers_lost_post(self):
        from chat.twitch import TwitchSchedule
        service = TwitchSchedule({'CLIENT_ID':'app','TWITCH_CHANNEL':'walker'}, Mock())
        service.tokens.ensure_fresh.return_value = {'scopes':['channel:manage:schedule']}
        remote = {'manual':{'id':'manual','start_time':service.stamp(self.now+99999), 'title':'Manual'}}
        calls=[]
        lose_response=[True]
        def request(method, endpoint, **kwargs):
            calls.append((method,endpoint,kwargs))
            if endpoint=='users':
                data={'data':[{'id':'user','broadcaster_type':'affiliate'}]}
            elif endpoint=='games':
                data={'data':[{'id':'category'}]}
            elif endpoint=='schedule':
                data={'data':{'segments':list(remote.values())}}
            elif method=='POST':
                body=kwargs['json']; identifier='managed-'+str(len(remote))
                remote[identifier]=dict(body,id=identifier)
                if lose_response.pop() if lose_response else False:
                    raise RuntimeError('Lost response')
                data={'data':{'segments':[remote[identifier]]}}
            elif method=='DELETE':
                del remote[kwargs['params']['id']]; data={}
            return Mock(status_code=200,json=Mock(return_value=data))
        service.request=request
        plan=[{'key':'slot:0','game':'vice_city','start':self.now+3600}]
        with self.assertRaisesRegex(RuntimeError,'Lost'):
            service.sync(plan,now=self.now)
        service.sync(plan,now=self.now)
        service.sync(plan,now=self.now)
        self.assertEqual(sum(method=='POST' for method,_,_ in calls),1)
        plan[0]['start']+=1200
        service.sync(plan,now=self.now)
        self.assertIn('manual',remote)
        self.assertEqual(len(remote),2)
        self.assertEqual(sum(method=='POST' for method,_,_ in calls),2)
