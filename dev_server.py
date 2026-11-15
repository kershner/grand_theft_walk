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

from gtw_chat_bot import GTWBot
from san_andreas.runtime import DEFAULT_STATE_DIR, WalkerControl, configure_logging

logger = logging.getLogger(__name__)


def create_app(state_dir=DEFAULT_STATE_DIR):
    app = Flask(__name__)
    messages = []
    messages_lock = threading.Lock()
    bot = GTWBot(control=WalkerControl(state_dir))
    app.extensions["gtw_bot"] = bot
    seen_events = set()

    @app.before_request
    def identify_viewer():
        g.viewer = request.cookies.get("gtw_viewer") or uuid.uuid4().hex

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
            with messages_lock:
                messages.append(text)

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
            events = bot.control.snapshot().get("missions", {}).get("events", [])
        except RuntimeError:
            events = []
        try:
            after = max(0, int(request.args.get("after", 0)))
        except ValueError:
            after = 0
        with messages_lock:
            messages.extend(bot.command_voting.drain_events())
            for event in events:
                if event["id"] not in seen_events:
                    messages.append(event["text"])
                    seen_events.add(event["id"])
            return jsonify({"messages": messages[after:], "cursor": len(messages)})

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5050)
    args = parser.parse_args()
    log_path = configure_logging(args.state_dir)
    app = create_app(args.state_dir.resolve())
    ip = socket.gethostbyname(socket.gethostname())
    logger.info(
        f"\n  Grand Theft Walk Dev Server running\n"
        f"  Local:   http://localhost:{args.port}\n"
        f"  Phone:   http://{ip}:{args.port}\n",
    )
    logger.info("Log: %s", log_path)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Dev server stopped")
        raise SystemExit(1)
