"""Twitch transport for the same ChatBot used by the local development server."""

import argparse
import asyncio
import logging
import time
from contextlib import suppress
from pathlib import Path

import requests
from twitchio.ext import commands

from chat.bot import ChatBot, location_text
from chat.oauth import OAuthTokens
from core.config import load_parameters, local_path, twitch_settings
from core.files import atomic_json
from core.runtime import WalkerControl, chat_ready, configure_logging
from games import ACTIVE_GAMES, ROOT, get_game


logger = logging.getLogger(__name__)


CATEGORIES = {
    "san_andreas": "Grand Theft Auto: San Andreas",
    "vice_city": "Grand Theft Auto: Vice City",
    "gta_iii": "Grand Theft Auto III",
}
STREAM_TAGS = [
    "Exploration", "Automation", "Chill", "Cozy", "Comfy",
    "Interactive", "PC", "Programming", "Casual", "Python",
]
POLL_INTERVAL = 30
REFRESH_INTERVAL = 300


class StreamMetadata:
    def __init__(self, params, tokens, game):
        self.params, self.tokens = params, tokens
        self.category = CATEGORIES[game]
        self.broadcaster_id = None
        self.game_id = None
        self.last_title = None
        self.next_refresh = 0

    def request(self, method, endpoint, **kwargs):
        access = self.tokens.ensure_fresh()["access_token"]
        response = requests.request(
            method, f"https://api.twitch.tv/helix/{endpoint}",
            headers={"Client-ID": self.params["CLIENT_ID"],
                     "Authorization": f"Bearer {access}"}, timeout=20, **kwargs,
        )
        expected = 204 if method == "PATCH" else 200
        if response.status_code != expected:
            # Never log HTTP response bodies or request URLs containing credentials.
            raise RuntimeError(f"Twitch metadata {endpoint} failed (HTTP {response.status_code}).")
        return response

    def update(self, state, now=None):
        if not chat_ready(state):
            return False
        title = location_text(state)[:140]
        if not title:
            return False
        now = time.monotonic() if now is None else now
        if title == self.last_title and now < self.next_refresh:
            return False
        if self.broadcaster_id is None:
            users = self.request("GET", "users", params={"login": self.params["TWITCH_CHANNEL"]}).json()["data"]
            if not users:
                raise RuntimeError("Twitch broadcaster was not found.")
            self.broadcaster_id = users[0]["id"]
        if self.game_id is None:
            games = self.request("GET", "games", params={"name": self.category}).json()["data"]
            matches = [game for game in games if game["name"] == self.category]
            if not matches:
                raise RuntimeError(f"Twitch category was not found: {self.category}.")
            self.game_id = matches[0]["id"]
        self.request("PATCH", "channels", params={"broadcaster_id": self.broadcaster_id},
                     json={"game_id": self.game_id, "title": title, "tags": STREAM_TAGS})
        self.last_title = title
        self.next_refresh = now + REFRESH_INTERVAL
        return True


class TwitchBot(commands.Bot):
    def __init__(self, params, tokens, game, ready_file=None, rotation_file=None):
        self.params, self.tokens = params, tokens
        self.access = tokens.ensure_fresh()["access_token"]
        super().__init__(token=self.access, prefix="!", initial_channels=[params["TWITCH_CHANNEL"]])
        self.chat = ChatBot(WalkerControl(game=game), rotation_file=rotation_file)
        self.metadata = StreamMetadata(params, tokens, game)
        self.next_metadata_poll = 0
        self.runtime_task = None
        self.send_lock = asyncio.Lock()
        self.ready_file = ready_file

    async def say(self, text):
        async with self.send_lock:
            channel = self.get_channel(self.params["TWITCH_CHANNEL"])
            if channel is None:
                raise RuntimeError("Twitch chat is disconnected.")
            # Stay within Twitch's message size and normal-account rate limits.
            for offset in range(0, len(text), 450):
                await channel.send(text[offset:offset + 450])
                await asyncio.sleep(1.6)

    async def event_ready(self):
        if self.get_channel(self.params["TWITCH_CHANNEL"]) is None:
            raise RuntimeError("Could not join the configured Twitch channel.")
        logger.info("Connected to Twitch as %s", self.nick)
        if self.runtime_task is None:
            # Apply category/title/tags before signaling OBS that it can go live.
            state = self.chat.ready_state()
            if state is None or not await asyncio.to_thread(self.metadata.update, state):
                raise RuntimeError("Game metadata is not ready for streaming.")
            self.next_metadata_poll = asyncio.get_running_loop().time() + POLL_INTERVAL
        if self.ready_file is not None:
            atomic_json(self.ready_file, {"ready": True})
        if self.runtime_task is None:
            self.runtime_task = asyncio.create_task(self.poll_runtime())

    async def event_message(self, message):
        if message.echo or message.channel.name.lower() != self.params["TWITCH_CHANNEL"].lower():
            return
        bot = self

        class Context:
            author = message.author

            async def send(self, text):
                await bot.say(text)

        await self.chat.process_chat_message(Context(), message.content)

    async def poll_runtime(self):
        while True:
            try:
                tokens = await asyncio.to_thread(self.tokens.ensure_fresh)
                if tokens["access_token"] != self.access:
                    # TwitchIO 2.10 reconnects using the connection token.
                    self.access = tokens["access_token"]
                    self._connection._token = self.access
                    self._http.token = self.access
                for text in self.chat.poll_events():
                    await self.say(text)
            except (OSError, ValueError, RuntimeError, requests.RequestException):
                # Do not include HTTP exceptions/URLs, which can contain credentials.
                logger.error("Twitch runtime update failed; retrying shortly.")
                await asyncio.sleep(5)
            now = asyncio.get_running_loop().time()
            if now >= self.next_metadata_poll:
                self.next_metadata_poll = now + POLL_INTERVAL
                try:
                    state = self.chat.ready_state()
                    if state is not None:
                        await asyncio.to_thread(self.metadata.update, state)
                except (OSError, ValueError, KeyError, RuntimeError, requests.RequestException):
                    logger.error("Twitch metadata update failed; retrying in 30 seconds.")
            await asyncio.sleep(1)

    async def event_error(self, error, *args, **kwargs):
        logger.error("Twitch event failed (%s).", type(error).__name__)

    async def close(self):
        if self.runtime_task is not None:
            self.runtime_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.runtime_task
        self.chat.close()
        await super().close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parameters", type=Path, default=ROOT / "parameters.json")
    parser.add_argument("--game", choices=ACTIVE_GAMES, required=True)
    parser.add_argument("--ready-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--rotation-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    configure_logging(get_game(args.game).state_dir, log_name="twitch")
    # TwitchIO 2.10 can log the raw access token on failed IRC login.
    logging.getLogger("twitchio").setLevel(logging.CRITICAL)
    params = twitch_settings(load_parameters(args.parameters))
    tokens = OAuthTokens(params, local_path(args.parameters, params.get("TOKENS_FILE", "oauth_tokens.json")))
    TwitchBot(params, tokens, args.game, args.ready_file, args.rotation_file).run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.error("Twitch bot stopped. Check configuration, authorization, and network access.")
        raise SystemExit(1)
