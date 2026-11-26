"""Start OBS, rotating games, and Twitch chat (or the local dev chat server)."""

import argparse
import ctypes
import json
import logging
import math
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import obsws_python
import psutil

from core.config import BroadcastSchedule, load_parameters, select_game, twitch_settings
from core.files import StateStore, write_state, read_state, state_exists, delete_state
from core.runtime import WalkerControl, chat_ready, configure_logging
from games import ACTIVE_GAMES, ROOT, get_game


logger = logging.getLogger(__name__)


def show_console():
    kernel = ctypes.windll.kernel32
    kernel.GetConsoleWindow.restype = ctypes.c_void_p
    window = kernel.GetConsoleWindow()
    if not window and kernel.AllocConsole():
        sys.stdin = open('CONIN$', 'r', encoding='utf-8')
        sys.stdout = sys.stderr = open('CONOUT$', 'w', encoding='utf-8', buffering=1)
        for handler in logging.getLogger().handlers:
            if type(handler) is logging.StreamHandler:
                handler.setStream(sys.stderr)
        window = kernel.GetConsoleWindow()
    if window:
        kernel.SetConsoleTitleW('Grand Theft Walk')
        ctypes.windll.user32.ShowWindow(ctypes.c_void_p(window), 9)
        ctypes.windll.user32.SetForegroundWindow(ctypes.c_void_p(window))


def wait_for_game(process, control, previous_session, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Game launcher exited during startup (code {process.returncode}).")
        try:
            state = control.snapshot()
            if chat_ready(state) and state.get("session") != previous_session:
                return state
        except RuntimeError:
            pass
        time.sleep(0.5)
    raise RuntimeError("Game did not become ready before STARTUP_TIMEOUT.")


def wait_for_chat(processes, ready_record, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(child.poll() is not None for child in processes):
            raise RuntimeError("Game or chat exited during startup; check its log.")
        if state_exists(ready_record):
            return
        time.sleep(0.5)
    raise RuntimeError("Chat did not connect within 60 seconds; check configuration and network access.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parameters", type=Path, default=ROOT / "parameters.json")
    parser.add_argument("--mode", choices=("dev", "prod", "twitch"))
    parser.add_argument("--game", choices=ACTIVE_GAMES, help="Choose the first game in the rotation")
    parser.add_argument("--dry-run", action="store_true", help="Show selection without starting apps or accessing Twitch")
    parser.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.parameters = args.parameters.resolve()
    params = load_parameters(args.parameters)
    mode = args.mode or params.get("MODE", "dev")
    mode = "prod" if mode == "twitch" else mode
    if mode not in ("dev", "prod"):
        raise ValueError("MODE must be dev or prod.")
    timeout = params.get("STARTUP_TIMEOUT", 600)
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 10 <= timeout <= 600:
        raise ValueError("STARTUP_TIMEOUT must be between 10 and 600 seconds.")
    game = args.game or select_game(params)
    if args.game is None:
        try:
            game = read_state(ROOT / 'runtime' / f'schedule-{mode}')['game']
        except FileNotFoundError:
            pass
    if args.dry_run:
        print(json.dumps({"mode": mode, "game": game, "title": get_game(game).title,
                          "parameters": str(args.parameters)}, indent=2))
        return
    from core.winmemory import process_mutex
    with process_mutex('Local\\GrandTheftWalkManager', '', existing_ok=True) as available:
        if not available:
            write_state(ROOT / 'runtime/launch-request', {'mode': mode, 'game': args.game})
            print('Grand Theft Walk is already running; showing its console and requesting startup.')
            return
    if mode == 'prod':
        from chat.oauth import OAuthTokens, authorize

        twitch_settings(params)
        try:
            StateStore().read(OAuthTokens(params).key)
        except FileNotFoundError:
            print('Sign in to Twitch once to finish setting up the fresh database.')
            authorize(params)
    # Elevate once to control games; their processes survive supervisor shutdown.
    if not ctypes.windll.shell32.IsUserAnAdmin():
        if args.elevated:
            raise RuntimeError("Administrator access was not granted.")
        from core.winmemory import elevate

        command = ["-B", "-m", "streaming.launch", "--parameters", str(args.parameters),
                   "--mode", mode, "--elevated"]
        if args.game is not None:
            command += ['--game', game]
        elevate(command, ROOT)
        return
    show_console()
    configure_logging(ROOT / "runtime", log_name="stream")
    run_session(params, args.parameters, args.game, mode)


class ManagedGames:
    """Reuse running games and controllers; never put a game in a kill-on-close job."""

    def __init__(self, params):
        self.params = params
        self.active = None

    def prepare(self, game):
        from core.manager import session_command
        from core.winmemory import game_pids

        control = WalkerControl(game=game)
        try:
            state = control.snapshot()
            if not chat_ready(state):
                raise RuntimeError('Controller is loading')
        except RuntimeError:
            if list(game_pids(game)):
                command = [sys.executable, '-B', '-m', 'core.session', '--game', game, 'run', '--walk']
                directory = ROOT
            else:
                command, directory = session_command(game)
                command = [*command, '--allow-other-games', '--timeout', str(self.params.get('STARTUP_TIMEOUT', 600))]
            # These processes intentionally survive supervisor shutdown.
            child = subprocess.Popen(command, cwd=directory)
            state = wait_for_game(child, control, None, self.params.get('STARTUP_TIMEOUT', 600) + 30)
        self.active = control
        self.walking(False)
        self.focus(state.get('pid'))
        return state

    @staticmethod
    def focus(pid):
        if not pid:
            raise RuntimeError('The controller did not report a game process.')
        user32 = ctypes.windll.user32
        windows = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

        @callback_type
        def visit(window, _):
            owner = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(ctypes.c_void_p(window), ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(ctypes.c_void_p(window)):
                windows.append(window)
            return True

        user32.EnumWindows(visit, 0)
        if not windows:
            raise RuntimeError('No visible game window was found.')
        user32.ShowWindow(ctypes.c_void_p(windows[0]), 9)
        if not user32.SetForegroundWindow(ctypes.c_void_p(windows[0])):
            raise RuntimeError('Windows could not activate the scheduled game window.')

    def walking(self, enabled):
        if self.active is not None:
            path = self.active.state_dir / 'control'
            settings = read_state(path)
            write_state(path, dict(settings, walking=enabled))

    def close(self):
        """Close the scheduled game once its broadcast has ended."""
        if self.active is None:
            return
        control = self.active
        pid = control.snapshot()['pid']
        path = control.state_dir / 'control'
        write_state(path, dict(read_state(path), walking=False, quit=True))
        # Let the controller release inputs and save progress before closing GTA.
        time.sleep(1)
        try:
            process = psutil.Process(pid)
            if Path(process.exe()) != control.game.install_dir / control.game.executable:
                raise RuntimeError('The scheduled game process has changed.')
            process.terminate()
            process.wait(timeout=10)
        except psutil.NoSuchProcess:
            pass
        self.active = None


def run_session(params, parameters_path, game, mode='prod'):
    """Keep status/chat alive through breaks and sync Twitch away from the timing loop."""
    from chat.oauth import OAuthTokens
    from chat.twitch import CATEGORIES, StreamMetadata, TwitchSchedule, offline_title
    from core.winmemory import process_mutex

    with process_mutex('Local\\GrandTheftWalkManager', 'A managed session is already running.'):
        try:
            previous = read_state(ROOT / 'runtime' / f'schedule-{mode}')
            state = WalkerControl(game=previous['game']).snapshot()
            resume_live = (mode == 'prod' and previous['phase'] == 'live' and previous.get('obs_managed')
                           and previous['deadline'] > time.time() and game in (None, previous['game']) and chat_ready(state))
        except (OSError, ValueError, KeyError, RuntimeError):
            resume_live = False
        schedule = BroadcastSchedule(params, mode, game, resume_live=resume_live)
        games = ManagedGames(params)
        obs, sync, metadata = None, None, None
        services = []
        worker = ThreadPoolExecutor(max_workers=2, thread_name_prefix='twitch-sync')
        future, published, retry_at = None, None, 0
        live_started = None
        ending_update = None
        service_retry_at = {}

        def publish_offline(game, earliest, *, ending=False):
            start = schedule.fit(earliest)
            target = dict(schedule.data, game=game, next_start_at=start, ending=ending,
                          phase='overnight' if start >= schedule.window(time.time())[1] else 'break')
            target.pop('transition', None)
            schedule.data['transition'] = target
            schedule.save()
            return worker.submit(metadata.update, {}, schedule=target) if metadata is not None else None

        try:
            if mode == 'prod':
                twitch_settings(params)
                tokens = OAuthTokens(params)
                tokens.ensure_fresh()
                sync = TwitchSchedule(params, tokens) if params.get('SCHEDULE', {}).get('twitch_calendar', False) else None
                obs = OBS(dict(params.get('OBS', {}), scheduled=True,
                               recover_owned=schedule.data.get('obs_managed', False)))
                obs.prepare(schedule.data['game'])
                if resume_live and obs.started_stream:
                    games.active = WalkerControl(game=schedule.data['game'])
                    live_started = time.monotonic() - max(0, time.time() - schedule.data['started_at'])
                elif obs.started_stream:
                    obs.stop_stream()
                    schedule.data['obs_managed'] = False
                    schedule.interrupt(time.time(), 'Retrying the interrupted broadcast in full', wait=False)
                elif resume_live:
                    schedule.interrupt(time.time(), 'Retrying the interrupted broadcast in full', wait=False)
                metadata = StreamMetadata(params, tokens, schedule.data['game'], schedule.path)
                schedule.data['twitch_sync_key'] = sync.key if sync else None
                schedule.save()
            dev = params.get('DEV_SERVER', {})
            ui_ready = ROOT / 'runtime' / f'schedule-ui-ready-{mode}'
            delete_state(ui_ready)
            services.append(subprocess.Popen([sys.executable, '-B', '-m', 'chat.dev', '--managed',
                '--game', schedule.data['game'], '--schedule-record', str(schedule.path),
                '--host', dev.get('host', '127.0.0.1'), '--port', str(dev.get('port', 5050)),
                '--ready-record', str(ui_ready)], cwd=ROOT))
            wait_for_chat(services, ui_ready)
            if mode == 'prod':
                chat_ready_record = ROOT / 'runtime' / 'schedule-chat-ready'
                delete_state(chat_ready_record)
                services.append(subprocess.Popen([sys.executable, '-B', '-m', 'chat.twitch',
                    '--parameters', str(parameters_path), '--game', schedule.data['game'],
                    '--schedule-record', str(schedule.path), '--ready-record', str(chat_ready_record)], cwd=ROOT))
                wait_for_chat(services, chat_ready_record)
            logger.info('%s', get_game(schedule.data['game']).title if schedule.data['phase'] == 'live' else offline_title(schedule.data))
            last_poll = time.monotonic()
            while True:
                now, monotonic = time.time(), time.monotonic()
                try:
                    request = read_state(ROOT / 'runtime/launch-request')
                except FileNotFoundError:
                    request = None
                if request is not None:
                    delete_state(ROOT / 'runtime/launch-request')
                    show_console()
                    if request.get('mode') == mode:
                        schedule.request_start(now, request.get('game'))
                        logger.info('Launch requested: %s.', schedule.data['phase'])
                    else:
                        logger.warning('This session is running in %s mode; stop it before changing modes.', mode)
                if future is not None and future.done():
                    try:
                        published = future.result()
                        retry_at = now + 300
                    except Exception as error:
                        published = None
                        retry_at = now + 60
                        # Store a safe message; HTTP exception text can contain credentials.
                        message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                        logger.error('Twitch schedule sync failed: %s. Retrying in 60 seconds.', message)
                        key = sync.key
                        try:
                            status = StateStore().read(key)
                        except FileNotFoundError:
                            status = {'segments': {}}
                        StateStore().write(key, dict(status, error=message))
                    future = None
                for index, service in enumerate(services):
                    if service.poll() is not None and now >= service_retry_at.get(index, 0):
                        if schedule.data['phase'] == 'live':
                            publish_offline(schedule.data['game'], now + schedule.BREAK_SECONDS)
                            if obs is not None:
                                obs.stop_stream()
                            games.walking(False)
                            schedule.data['obs_managed'] = False
                            schedule.interrupt(now)
                        logger.error('Restarting a disconnected chat/status service.')
                        # Service readiness is checked again before the next broadcast.
                        ready_index = service.args.index('--ready-record') + 1
                        delete_state(Path(service.args[ready_index]))
                        services[index] = subprocess.Popen(service.args, cwd=ROOT)
                        service_retry_at[index] = now + 30
                if sync is not None and future is None:
                    plan = schedule.upcoming(now)
                    signature = (schedule.data['phase'], schedule.data['slot_id'], schedule.data['deadline'])
                    if now >= retry_at or (published is not None and signature != published):
                        def publish(plan=plan, signature=signature):
                            sync.sync(plan)
                            return signature
                        future = worker.submit(publish)
                try:
                    if schedule.data['phase'] == 'live':
                        if monotonic - last_poll > 5:
                            raise RuntimeError('PC sleep or a long interruption was detected')
                        state = games.active.snapshot()
                        if not chat_ready(state):
                            raise RuntimeError('The scheduled game is no longer ready')
                        if obs is not None:
                            status = obs.client.get_stream_status()
                            if not status.output_active or getattr(status, 'output_reconnecting', False):
                                raise RuntimeError('Broadcast connection was interrupted')
                        cutoff = schedule.window(schedule.data['started_at'])[1]
                        if min(cutoff - now, schedule.LIVE_SECONDS - (monotonic - live_started)) <= 5:
                            if not schedule.data.get('ending'):
                                schedule.data['ending'] = True
                                ending_update = publish_offline(schedule.data['next_game'],
                                    schedule.data['deadline'] + schedule.BREAK_SECONDS, ending=True)
                        if now >= cutoff or monotonic - live_started >= schedule.LIVE_SECONDS:
                            if ending_update is not None and ending_update.done():
                                try:
                                    ending_update.result()
                                except Exception:
                                    logger.error('Twitch title update failed; chat will retry while offline.')
                            if obs is not None:
                                obs.stop_stream()
                            schedule.data['obs_managed'] = False
                            games.close()
                            if monotonic - live_started >= schedule.LIVE_SECONDS:
                                schedule.finish(time.time())
                            else:
                                schedule.interrupt(time.time(), 'Midnight cutoff; retrying a full session tomorrow')
                    else:
                        schedule.refresh(now)
                        services_ready = all(state_exists(Path(service.args[service.args.index('--ready-record') + 1]))
                                             for service in services)
                        if now >= schedule.data['next_start_at'] and services_ready:
                            schedule.preparing(now)
                            state = games.prepare(schedule.data['game'])
                            if obs is not None:
                                obs.select_game(schedule.data['game'])
                                metadata.game = schedule.data['game']
                                metadata.category = CATEGORIES[metadata.game]
                                metadata.game_id = metadata.last_title = None
                                live_rotation = dict(schedule.data, phase='live', deadline=time.time() + schedule.LIVE_SECONDS)
                                live_rotation.pop('transition', None)
                                metadata.update(state, schedule=live_rotation)
                            if schedule.fit(time.time()) <= time.time():
                                if obs is not None:
                                    schedule.data['obs_managed'] = True
                                    schedule.save()
                                    obs.start_stream()
                                # OBS startup itself can use the last seconds of the window.
                                if schedule.start(time.time()):
                                    live_started = time.monotonic()
                                    games.walking(True)
                                elif obs is not None:
                                    obs.stop_stream()
                                    schedule.data['obs_managed'] = False
                                    schedule.save()
                            else:
                                schedule.refresh(time.time())
                except Exception as error:
                    publish_offline(schedule.data['game'], time.time() + schedule.BREAK_SECONDS)
                    if obs is not None:
                        obs.stop_stream()
                    schedule.data['obs_managed'] = False
                    games.walking(False)
                    reason = str(error) if isinstance(error, (RuntimeError, ValueError)) else type(error).__name__
                    logger.error('Session interrupted: %s. A full retry follows the break.', reason)
                    schedule.interrupt(time.time(), reason)
                if time.time() - schedule.data.get('updated', 0) >= 10:
                    schedule.save()
                last_poll = time.monotonic()
                time.sleep(.5)
        finally:
            try:
                if schedule.data['phase'] in ('live', 'preparing'):
                    publish_offline(schedule.data['game'], time.time() + schedule.BREAK_SECONDS)
                if obs is not None:
                    obs.close()
                    schedule.data['obs_managed'] = obs.started_stream
                    schedule.save()
            finally:
                try:
                    games.walking(False)
                    if schedule.data['phase'] in ('live', 'preparing'):
                        schedule.interrupt(time.time())
                finally:
                    # Only our chat/status Python processes are stopped. Games stay alive.
                    for service in services:
                        if service.poll() is None:
                            service.terminate()
                            service.wait(timeout=10)
                    worker.shutdown(wait=False, cancel_futures=True)


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
            if settings.get('scheduled'):
                if not settings.get('recover_owned'):
                    raise RuntimeError('OBS is already broadcasting. Start scheduling after the existing broadcast ends.')
                self.started_stream = True
            logger.info("Reusing the live OBS stream; keeping its profile and scene collection.")
        else:
            if settings.get("profile"):
                self.client.set_current_profile(settings["profile"])
            if settings.get("scene_collection"):
                self.client.set_current_scene_collection(settings["scene_collection"])
        self.select_game(game)

    def select_game(self, game):
        scene = self.settings.get("scenes", {}).get(game)
        if scene:
            self.client.set_current_program_scene(scene)
        # Attach capture before the next game creates its DirectX context.
        time.sleep(float(self.settings.get("settle_seconds", 10)))

    def start_stream(self):
        # OBS may already be live, including a stream started during game startup.
        if self.client.get_stream_status().output_active:
            logger.info("OBS stream already live; continuing without restarting it.")
            return
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

    def stop_stream(self):
        if not self.started_stream:
            return
        self.client.stop_stream()
        deadline = time.monotonic() + 30
        while self.client.get_stream_status().output_active:
            if time.monotonic() >= deadline:
                raise RuntimeError('OBS did not confirm that broadcasting stopped.')
            time.sleep(.5)
        self.started_stream = False

    def close(self):
        try:
            if self.started_stream and self.client is not None:
                self.stop_stream()
        except Exception:
            logger.error("Could not stop the managed OBS stream; check OBS.")
        finally:
            self.disconnect()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Session stopped.")
    except Exception as error:
        show_console()
        if isinstance(error, (ValueError, RuntimeError)):
            logger.error('%s', error)
        else:
            # HTTP exceptions can contain credentials in request URLs.
            logger.error('Startup failed (%s). Check the stream log.', type(error).__name__)
        if sys.stdin is not None and sys.stdin.isatty():
            input('Press Enter to close this window...')
        raise SystemExit(1)
