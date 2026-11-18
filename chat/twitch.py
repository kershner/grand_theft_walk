"""Twitch transport for the same ChatBot used by the local development server."""

import argparse
import asyncio
import logging
from contextlib import suppress
from pathlib import Path

import requests
from twitchio.ext import commands

from chat.bot import ChatBot
from core.config import load_parameters, local_path, twitch_settings
from core.files import atomic_json
from core.runtime import WalkerControl, configure_logging
from chat.auth import OAuthTokens
from games import ACTIVE_GAMES, ROOT, get_game

logger = logging.getLogger(__name__)


class TwitchBot(commands.Bot):
    def __init__(self, params, tokens, game, ready_file=None):
        self.params, self.tokens = params, tokens
        self.access = tokens.ensure_fresh()["access_token"]
        super().__init__(token=self.access, prefix="!", initial_channels=[params["TWITCH_CHANNEL"]])
        self.chat = ChatBot(WalkerControl(game=game))
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
    args = parser.parse_args()
    configure_logging(get_game(args.game).state_dir, log_name="twitch")
    # TwitchIO 2.10 can log the raw access token on failed IRC login.
    logging.getLogger("twitchio").setLevel(logging.CRITICAL)
    params = twitch_settings(load_parameters(args.parameters))
    tokens = OAuthTokens(params, local_path(args.parameters, params.get("TOKENS_FILE", "oauth_tokens.json")))
    TwitchBot(params, tokens, args.game, args.ready_file).run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.error("Twitch bot stopped. Check configuration, authorization, and network access.")
        raise SystemExit(1)
