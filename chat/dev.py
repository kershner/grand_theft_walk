"""Local chat console for Grand Theft Walk."""

import argparse
import asyncio
import logging
import socket
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, g, jsonify, request, send_from_directory

from chat.bot import ChatBot, read_schedule, schedule_countdown
from core.files import StateStore, write_state
from core.runtime import WalkerControl, configure_logging
from games import GAMES, get_game


logger = logging.getLogger(__name__)


def create_app(state_dir=None, start_runtime=True, game="san_andreas", schedule_record=None, managed=False):
    profile = get_game(game)
    state_dir = Path(state_dir) if state_dir is not None else profile.state_dir
    app = Flask(__name__)
    messages = []
    messages_lock = threading.Lock()
    bot = ChatBot(control=WalkerControl(state_dir, game=game), schedule_record=schedule_record)
    app.extensions["gtw_bot"] = bot

    def append_message(text):
        with messages_lock:
            messages.append(text)

    @app.before_request
    def identify_viewer():
        g.viewer = request.cookies.get("gtw_viewer") or uuid.uuid4().hex
        if managed:
            schedule = read_schedule(schedule_record)
            if schedule.get('game') and schedule['game'] != bot.control.game.id:
                bot.control = WalkerControl(game=schedule['game'])

    @app.route('/schedule')
    def schedule_status():
        schedule = read_schedule(schedule_record)
        sync = {}
        if schedule.get('twitch_sync_key'):
            try:
                saved = StateStore().read(schedule['twitch_sync_key'])
                sync = {key: saved.get(key) for key in ('last_synced', 'error')}
            except FileNotFoundError:
                pass
        return jsonify({'phase': schedule.get('phase'), 'game': schedule.get('game'),
                        'countdown': schedule_countdown(schedule),
                        'deadline': schedule.get('deadline') if schedule.get('phase') == 'live' else schedule.get('next_start_at'),
                        'updated': schedule.get('updated'), 'reason': schedule.get('reason'),
                        'twitch': sync})

    @app.after_request
    def remember_viewer(response):
        response.set_cookie(
            "gtw_viewer", g.viewer, httponly=True, samesite="Lax", max_age=31536000
        )
        return response

    class DevContext:
        def __init__(self, viewer):
            self.author = SimpleNamespace(id=viewer)

        async def send(self, text):
            append_message(text)

    @app.route("/")
    def index():
        return send_from_directory(
            Path(__file__).resolve().parent / "templates", "dev_control.html"
        )

    @app.route("/cmd/<path:raw>")
    def run_command(raw):
        content = raw.strip()
        if not content:
            return jsonify({"ok": False, "error": "empty input"}), 400
        if not content.startswith("!"):
            content = "!" + content
        try:
            asyncio.run(bot.process_chat_message(DevContext(g.viewer), content))
            return jsonify({"ok": True})
        except Exception as error:
            logger.exception("Chat command failed")
            return jsonify({"ok": False, "error": str(error)}), 500

    @app.route("/chat")
    def chat():
        try:
            after = max(0, int(request.args.get("after", 0)))
        except ValueError:
            after = 0
        with messages_lock:
            messages.extend(bot.poll_events(
                include_announcements=False, wait_for_announcement=start_runtime,
            ))
            return jsonify({"messages": messages[after:], "cursor": len(messages)})

    @app.route("/navigation")
    def navigation():
        try:
            state = bot.control.snapshot()
        except (OSError, ValueError, RuntimeError) as error:
            return jsonify({"error": str(error)}), 503
        return jsonify({"position": state.get("position"),
                        "mode": state.get("settings", {}).get("mode"),
                        "navigation": state.get("navigation", {})})

    if start_runtime:
        bot.start_runtime(append_message)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", choices=GAMES, default="san_andreas")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--ready-record", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--schedule-record", type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--managed', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    profile = get_game(args.game)
    state_dir = (args.state_dir or profile.state_dir).resolve()
    log_path = configure_logging(state_dir, log_name=profile.id)
    app = create_app(state_dir, game=profile.id, schedule_record=args.schedule_record,
                     managed=args.managed, start_runtime=not args.managed)
    ip = socket.gethostbyname(socket.gethostname())
    logger.info(
        f"\n  Grand Theft Walk Control Server running\n"
        f"  Local:   http://localhost:{args.port}\n"
        f"  Phone:   http://{ip}:{args.port}\n",
    )
    logger.info("Log: %s", log_path)
    try:
        if args.ready_record is None:
            app.run(host=args.host, port=args.port, debug=False)
        else:
            from werkzeug.serving import make_server

            with make_server(args.host, args.port, app, threaded=True) as server:
                write_state(args.ready_record, {"ready": True})
                server.serve_forever()
    finally:
        app.extensions["gtw_bot"].close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Dev server stopped")
        raise SystemExit(1)
