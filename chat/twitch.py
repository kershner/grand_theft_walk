"""Twitch transport for the same ChatBot used by the local development server."""

import argparse
import asyncio
import logging
import time
from datetime import datetime, timezone
from contextlib import suppress
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from twitchio.ext import commands

from chat.bot import ChatBot, location_text, read_schedule, schedule_countdown
from chat.oauth import OAuthTokens
from core.config import load_parameters, twitch_settings
from core.files import StateStore, write_state
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


def offline_title(schedule, now=None):
    now = time.time() if now is None else now
    game = schedule.get('next_game') if schedule.get('phase') == 'live' else schedule.get('game')
    title = get_game(game).title
    zone = ZoneInfo('America/New_York')
    start = datetime.fromtimestamp(schedule['next_start_at'], zone)
    today = datetime.fromtimestamp(now, zone).date()
    clock = start.strftime('%I:%M %p').lstrip('0')
    if schedule.get('ending') and start.date() > today:
        return f'Back tomorrow at {clock} ET · {title}'
    return f'Taking a break · {title} goes live at {clock} ET'


class StreamMetadata:
    def __init__(self, params, tokens, game, schedule_record=None):
        self.params, self.tokens = params, tokens
        self.game, self.schedule_record = game, schedule_record
        self.category = CATEGORIES[game]
        self.broadcaster_id = None
        self.game_id = None
        self.last_title = None
        self.next_refresh = 0

    def request(self, method, endpoint, allow_status=(), **kwargs):
        access = self.tokens.ensure_fresh()["access_token"]
        response = requests.request(
            method, f"https://api.twitch.tv/helix/{endpoint}",
            headers={"Client-ID": self.params["CLIENT_ID"],
                     "Authorization": f"Bearer {access}"}, timeout=20, **kwargs,
        )
        expected = 204 if method == 'DELETE' or (method == 'PATCH' and endpoint == 'channels') else 200
        if response.status_code != expected and response.status_code not in allow_status:
            # Never log HTTP response bodies or request URLs containing credentials.
            raise RuntimeError(f"Twitch metadata {endpoint} failed (HTTP {response.status_code}).")
        return response

    def update(self, state, now=None, schedule=None):
        explicit = schedule is not None
        schedule = read_schedule(self.schedule_record) if schedule is None else schedule
        if schedule.get('phase') == 'overnight' and not explicit and not schedule.get('transition'):
            return False  # Keep the title already published when the last stream ended.
        schedule = schedule.get('transition', schedule)
        if schedule.get('phase') and (schedule['phase'] != 'live' or schedule.get('ending')):
            title = offline_title(schedule)
        else:
            if not chat_ready(state):
                return False
            title = location_text(state)
            if not title:
                return False
            countdown = schedule_countdown(schedule)
            suffix = f" • {countdown}" if countdown else ""
            title = title[:140 - len(suffix)].rstrip() + suffix
        title = title[:140]
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


class TwitchSchedule(StreamMetadata):
    """Reconcile only our saved segment IDs, including uncertain POST results."""

    def __init__(self, params, tokens):
        super().__init__(params, tokens, 'san_andreas')
        self.key = 'twitch-schedule:' + params['TWITCH_CHANNEL'].casefold()
        self.categories = {}

    def sync(self, broadcasts, now=None):
        now = time.time() if now is None else now
        authorization = self.tokens.ensure_fresh()
        scopes = authorization.get('scopes', authorization.get('scope', []))
        if isinstance(scopes, str):
            scopes = scopes.split()
        if 'channel:manage:schedule' not in scopes:
            raise RuntimeError('Twitch schedule permission is missing; run venv\\Scripts\\python.exe -m chat.oauth to authorize it.')
        store = StateStore()
        try:
            saved = store.read(self.key)
        except FileNotFoundError:
            saved = {'segments': {}}
        if self.broadcaster_id is None:
            users = self.request('GET', 'users', params={'login': self.params['TWITCH_CHANNEL']}).json()['data']
            if not users:
                raise RuntimeError('Twitch broadcaster was not found.')
            if users[0].get('broadcaster_type') not in ('affiliate', 'partner'):
                raise RuntimeError('Twitch requires Affiliate or Partner status for individual schedule entries.')
            self.broadcaster_id = users[0]['id']
        query = {'broadcaster_id': self.broadcaster_id}
        existing, cursor = [], None
        while True:
            response = self.request('GET', 'schedule', params=dict(query, first=25, **({'after': cursor} if cursor else {})),
                                    allow_status=(404,))
            if response.status_code == 404:
                break
            page = response.json()
            existing.extend(page.get('data', {}).get('segments') or [])
            cursor = page.get('pagination', {}).get('cursor')
            if not cursor:
                break
        actual = {entry['id']: entry for entry in existing}
        desired = {}
        for broadcast in broadcasts:
            start = int(broadcast['start'])
            if start <= now + 30:
                continue  # Twitch rejects creating or moving segments into the past.
            desired[broadcast['key']] = dict(broadcast, start=start,
                title=f"Grand Theft Walk · {get_game(broadcast['game']).title}")
        segments = saved['segments']
        # A previous POST may have succeeded even if its response was lost.
        for item in segments.values():
            if not item.get('id'):
                stamp = self.stamp(item['start'])
                match = next((row for row in existing if row['start_time'] == stamp
                              and row['title'] == item['title'] and not row.get('is_recurring')), None)
                if match:
                    item['id'] = match['id']
                    store.write(self.key, saved)
        # Remove obsolete future entries first, so moved slots cannot overlap.
        for key, item in list(segments.items()):
            wanted = desired.get(key)
            unchanged = wanted and all(item.get(field) == wanted[field] for field in ('game', 'start', 'title'))
            active = item['start'] <= now < item['start'] + 7200
            if unchanged or active:
                continue
            if item.get('id'):
                self.request('DELETE', 'schedule/segment', params=dict(query, id=item['id']), allow_status=(404,))
            del segments[key]
            store.write(self.key, saved)
        for key, item in desired.items():
            previous = segments.get(key)
            if previous and previous.get('id') in actual:
                continue
            game = item['game']
            if game not in self.categories:
                matches = self.request('GET', 'games', params={'name': CATEGORIES[game]}).json()['data']
                if not matches:
                    raise RuntimeError(f'Twitch category unavailable: {CATEGORIES[game]}.')
                self.categories[game] = matches[0]['id']
            segments[key] = dict(item)  # Persist intent before an uncertain network write.
            store.write(self.key, saved)
            response = self.request('POST', 'schedule/segment', params=query,
                json={'start_time': self.stamp(item['start']), 'timezone': 'America/New_York',
                      'duration': 120, 'is_recurring': False, 'category_id': self.categories[game],
                      'title': item['title']})
            segments[key]['id'] = response.json()['data']['segments'][0]['id']
            store.write(self.key, saved)
        saved.update(last_synced=now, error=None)
        store.write(self.key, saved)

    @staticmethod
    def stamp(value):
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


class TwitchBot(commands.Bot):
    def __init__(self, params, tokens, game, ready_record=None, schedule_record=None):
        self.params, self.tokens = params, tokens
        self.access = tokens.ensure_fresh()["access_token"]
        super().__init__(token=self.access, prefix="!", initial_channels=[params["TWITCH_CHANNEL"]])
        self.chat = ChatBot(WalkerControl(game=game), schedule_record=schedule_record)
        self.metadata = StreamMetadata(params, tokens, game, schedule_record)
        self.next_metadata_poll = 0
        self.runtime_task = None
        self.send_lock = asyncio.Lock()
        self.ready_record = ready_record
        self.last_schedule_phase = None

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
        schedule = read_schedule(self.chat.schedule_record)
        if self.runtime_task is None and not schedule.get('phase'):
            # Apply category/title/tags before signaling OBS that it can go live.
            state = self.chat.ready_state()
            if state is None or not await asyncio.to_thread(self.metadata.update, state):
                raise RuntimeError("Game metadata is not ready for streaming.")
            self.next_metadata_poll = asyncio.get_running_loop().time() + POLL_INTERVAL
        if self.ready_record is not None:
            write_state(self.ready_record, {"ready": True})
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
                schedule = read_schedule(self.chat.schedule_record)
                phase = (schedule.get('phase'), schedule.get('ending', False), schedule.get('game'),
                         (schedule.get('transition') or {}).get('phase'))
                if phase != self.last_schedule_phase:
                    self.next_metadata_poll = 0
                    self.last_schedule_phase = phase
                game = schedule.get('game', self.chat.control.game.id)
                if game != self.chat.control.game.id:
                    self.chat.close()
                    self.chat = ChatBot(WalkerControl(game=game), schedule_record=self.chat.schedule_record)
                    self.metadata = StreamMetadata(self.params, self.tokens, game, self.chat.schedule_record)
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
                    if state is not None or schedule.get('phase'):
                        await asyncio.to_thread(self.metadata.update, state or {})
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
    parser.add_argument("--ready-record", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--schedule-record", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    configure_logging(get_game(args.game).state_dir, log_name="twitch")
    # TwitchIO 2.10 can log the raw access token on failed IRC login.
    logging.getLogger("twitchio").setLevel(logging.CRITICAL)
    params = twitch_settings(load_parameters(args.parameters))
    tokens = OAuthTokens(params)
    TwitchBot(params, tokens, args.game, args.ready_record, args.schedule_record).run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.error("Twitch bot stopped. Check configuration, authorization, and network access.")
        raise SystemExit(1)
