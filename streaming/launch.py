"""Start OBS, rotating games, and Twitch chat (or the local dev chat server)."""

import argparse
import ctypes
import json
import logging
import math
import subprocess
import sys
import time
import uuid
from pathlib import Path

from core.config import load_parameters, local_path, rotation_seconds, twitch_settings
from core.files import atomic_json, read_json
from core.runtime import WalkerControl, chat_ready, configure_logging
from games import ACTIVE_GAMES, ROOT, get_game
from core.processes import spawn
from core.rotation import RotationProgress

logger = logging.getLogger(__name__)


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


def wait_for_chat(processes, ready_file, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(child.poll() is not None for child in processes):
            raise RuntimeError("Game or chat exited during startup; check its log.")
        if ready_file.exists():
            return
        time.sleep(0.5)
    raise RuntimeError("Chat did not connect within 60 seconds; check configuration and network access.")


def run_session(params, parameters_path, game, mode):
    from core.winmemory import game_pids, process_mutex

    with process_mutex("Local\\GrandTheftWalkManager", "A managed Grand Theft Walk session is already running."):
        if any(list(game_pids(other)) for other in ACTIVE_GAMES):
            raise RuntimeError("Close the existing game before starting a managed session.")
        obs = None
        previous_game = None
        try:
            if mode == "prod":
                from streaming.obs import OBS
                from chat.auth import OAuthTokens

                twitch_settings(params)
                OAuthTokens(params, local_path(parameters_path, params.get("TOKENS_FILE", "oauth_tokens.json"))).ensure_fresh()
                obs = OBS(params.get("OBS", {}))
                obs.prepare(game)
            while True:
                if obs is not None and previous_game is not None:
                    obs.select_game(game)
                next_game = run_game_session(params, parameters_path, game, mode, obs, previous_game)
                if next_game is None:
                    return
                previous_game, game = game, next_game
        finally:
            if obs is not None:
                obs.close()


def run_game_session(params, parameters_path, game, mode, obs=None, switched_from=None):
    from core.manager import session_command
    from core.processes import ProcessJob, stop_processes

    command, directory = session_command(game)
    control = WalkerControl(game=game)
    try:
        previous_session = read_json(control.state_dir / "state.json").get("session")
    except (OSError, ValueError):
        previous_session = None
    job = ProcessJob()
    processes = []
    rotation_file = ROOT / "runtime" / f"rotation-{uuid.uuid4().hex}.json"
    ready_file = ROOT / "runtime" / f"chat-ready-{uuid.uuid4().hex}.json"
    progress = RotationProgress(params, ROOT / "runtime" / f"rotation-progress-{mode}.json")
    try:
        logger.info("Launching %s...", get_game(game).title)
        process = spawn(job, [*command, "--timeout", str(params.get("STARTUP_TIMEOUT", 600))], directory)
        processes.append(process)
        wait_for_game(process, control, previous_session, params.get("STARTUP_TIMEOUT", 600) + 30)
        interval = progress.remaining(game) or rotation_seconds(params)
        games = params.get("ROTATION", {}).get("games", list(ACTIVE_GAMES))
        next_game = games[(games.index(game) + 1) % len(games)] if game in games else games[0]
        rotating = next_game != game
        atomic_json(rotation_file, {"game": game, "switched_from": switched_from,
                                   "next_switch_at": time.time() + interval if rotating else None})
        if mode == "prod":
            chat_command = [sys.executable, "-B", "-m", "chat.twitch",
                            "--parameters", str(parameters_path), "--game", game]
        else:
            dev = params.get("DEV_SERVER", {})
            chat_command = [sys.executable, "-B", "-m", "chat.dev", "--game", game,
                            "--host", dev.get("host", "0.0.0.0"), "--port", str(dev.get("port", 5050))]
        processes.append(spawn(job, [*chat_command, "--ready-file", str(ready_file),
                                     "--rotation-file", str(rotation_file)]))
        wait_for_chat(processes, ready_file)
        if obs is not None and switched_from is None:
            obs.start_stream()
        progress.start(game)
        atomic_json(rotation_file, {"game": game, "switched_from": switched_from,
                                   "next_switch_at": time.time() + interval if rotating else None})
        logger.info("Session ready: %s (%s). F12 exits the game; Ctrl+C stops the session.", game, mode)
        while process.poll() is None:
            progress.checkpoint()
            if processes[1].poll() is not None:
                raise RuntimeError("Chat process exited; stopping the managed session.")
            if rotating and progress.remaining(game) <= 0:
                # Validate the incoming installation before ending this game.
                session_command(next_game)
                logger.info("Rotating from %s to %s.", game, next_game)
                return next_game
            time.sleep(0.5)
        if process.returncode:
            raise RuntimeError(f"Game controller exited with code {process.returncode}.")
    finally:
        # Save before cleanup; time with the launcher stopped never counts.
        # Cleanup must still run if a progress write fails.
        try:
            progress.checkpoint(force=True)
        except OSError:
            logger.exception("Could not save rotation progress")
        # Give the controller a chance to save/restore its state before killing our tree.
        try:
            state = control.snapshot()
            if state.get("session") != previous_session:
                path = control.state_dir / "control.json"
                settings = read_json(path)
                atomic_json(path, dict(settings, quit=True))
                if processes:
                    try:
                        processes[0].wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
        except (OSError, ValueError, RuntimeError):
            pass
        stop_processes(job, processes)
        ready_file.unlink(missing_ok=True)
        rotation_file.unlink(missing_ok=True)


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
    game = args.game or RotationProgress(
        params, ROOT / "runtime" / f"rotation-progress-{mode}.json").select()
    if args.dry_run:
        print(json.dumps({"mode": mode, "game": game, "title": get_game(game).title,
                          "parameters": str(args.parameters)}, indent=2))
        return
    # Elevate the supervisor once so child launchers remain in our owned job tree.
    if not ctypes.windll.shell32.IsUserAnAdmin():
        if args.elevated:
            raise RuntimeError("Administrator access was not granted.")
        from core.winmemory import elevate

        elevate(["-B", "-m", "streaming.launch", "--parameters", str(args.parameters),
                 "--mode", mode, "--game", game, "--elevated"], ROOT)
        return
    configure_logging(ROOT / "runtime", log_name="stream")
    run_session(params, args.parameters, game, mode)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Session stopped.")
    except (ValueError, RuntimeError) as error:
        logger.error("%s", error)
        raise SystemExit(1)
    except Exception as error:
        # Avoid printing HTTP exceptions containing credentials in request URLs.
        logger.error("Startup failed (%s). Check local settings, game setup, and authorization.", type(error).__name__)
        raise SystemExit(1)
