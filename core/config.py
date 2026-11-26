"""Configuration and the persistent whole-broadcast schedule."""

import json
import math
import time
import uuid
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from core.files import StateStore, write_state, read_state
from games import ACTIVE_GAMES, ROOT


def load_parameters(path=ROOT / "parameters.json", reimport=False):
    path = Path(path).resolve()
    config_key = 'configuration:' + str(path).casefold()
    try:
        params = StateStore().read(config_key) if not reimport else None
    except FileNotFoundError:
        params = None
    if params is None:
        try:
            params = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError as error:
            raise ValueError('Create a local parameters.json first; {"MODE": "dev"} is enough for development.') from error
    if not isinstance(params, dict):
        raise ValueError("parameters.json must contain an object.")
    for section in ("ROTATION", "DEV_SERVER", "OBS", 'SCHEDULE'):
        if not isinstance(params.get(section, {}), dict):
            raise ValueError(f"{section} must contain an object.")
    obs = params.get("OBS", {})
    for key, default, minimum, maximum in (("connect_timeout", 60, 1, 600), ("settle_seconds", 10, 0, 120)):
        value = obs.get(key, default)
        if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"OBS.{key} must be between {minimum} and {maximum} seconds.")
    for section, default in (("OBS", 4455), ("DEV_SERVER", 5050)):
        value = params.get(section, {}).get("port", default)
        if type(value) is not int or not 1 <= value <= 65535:
            raise ValueError(f"{section}.port must be between 1 and 65535.")
    if not isinstance(obs.get("scenes", {}), dict):
        raise ValueError("OBS.scenes must map game IDs to scene names.")
    select_game(params)
    if not isinstance(params.get('SCHEDULE', {}).get('twitch_calendar', False), bool):
        raise ValueError('SCHEDULE.twitch_calendar must be true or false.')
    StateStore().write(config_key, params)
    return params


def select_game(params):
    rotation = params.get("ROTATION", {})
    games = rotation.get("games", list(ACTIVE_GAMES))
    if not isinstance(games, list) or not games or any(not isinstance(g, str) or g not in ACTIVE_GAMES for g in games):
        raise ValueError(f"ROTATION.games must list available games: {', '.join(ACTIVE_GAMES)}.")
    if len(set(games)) != len(games):
        raise ValueError("ROTATION.games must not contain duplicates.")
    return games[0]


def twitch_settings(params):
    for key in ("CLIENT_ID", "CLIENT_SECRET", "TWITCH_CHANNEL", "TWITCH_BOT_LOGIN"):
        if not isinstance(params.get(key), str) or not params[key].strip():
            raise ValueError(f"parameters.json must include {key}.")
    for key in ("TWITCH_CHANNEL", "TWITCH_BOT_LOGIN"):
        value = params[key]
        if not value.isascii() or not value.replace("_", "").isalnum():
            raise ValueError(f"{key} must be a Twitch login without # or spaces.")
    if params["TWITCH_CHANNEL"].lower() != params["TWITCH_BOT_LOGIN"].lower():
        raise ValueError("TWITCH_BOT_LOGIN must match TWITCH_CHANNEL to manage stream metadata. Authorize as the broadcaster.")
    return params


class BroadcastSchedule:
    """A queue of whole broadcasts; downtime shifts it, midnight limits it."""

    LIVE_SECONDS = BREAK_SECONDS = 7200
    ZONE = ZoneInfo('America/New_York')

    def __init__(self, params, mode='prod', game=None, now=None, path=None, *, resume_live=False):
        self.path = Path(path) if path else ROOT / 'runtime' / f'schedule-{mode}'
        self.games = params.get('ROTATION', {}).get('games', list(ACTIVE_GAMES))
        select_game(params)
        now = time.time() if now is None else now
        try:
            self.data = read_state(self.path)
            if (self.data.get('game') not in self.games
                    or self.data.get('phase') not in ('live', 'break', 'overnight', 'preparing')
                    or type(self.data.get('deadline')) not in (int, float)
                    or not math.isfinite(self.data['deadline'])):
                raise ValueError('Invalid broadcast schedule')
        except FileNotFoundError:
            self.data = {'game': game or self.games[0], 'phase': 'break',
                         'deadline': now, 'slot_id': uuid.uuid4().hex}
        if game is not None and game != self.data['game']:
            self.data.update(game=game, slot_id=uuid.uuid4().hex)
        # No partially completed broadcasts survive an app restart.
        if resume_live and self.data['phase'] == 'live':
            self.save()
        elif self.data['phase'] in ('live', 'preparing'):
            self.interrupt(now, 'Retrying the interrupted broadcast in full', wait=False)
        else:
            if self.data.get('reason'):
                self.data['deadline'] = now
            self.refresh(now)

    @classmethod
    def window(cls, now):
        local = datetime.fromtimestamp(now, cls.ZONE)
        opening = datetime.combine(local.date(), clock_time(9), cls.ZONE)
        cutoff = datetime.combine(local.date() + timedelta(days=1), clock_time(), cls.ZONE)
        return opening.timestamp(), cutoff.timestamp()

    @classmethod
    def fit(cls, earliest):
        opening, cutoff = cls.window(earliest)
        candidate = max(earliest, opening)
        if candidate + cls.LIVE_SECONDS > cutoff:
            tomorrow = datetime.fromtimestamp(earliest, cls.ZONE).date() + timedelta(days=1)
            return datetime.combine(tomorrow, clock_time(9), cls.ZONE).timestamp()
        return candidate

    def save(self):
        self.data['updated'] = time.time()
        write_state(self.path, self.data)

    def refresh(self, now):
        if self.data['phase'] == 'live':
            return
        self.data.pop('ending', None)
        self.data.pop('transition', None)
        earliest = max(now, self.data['deadline'])
        start = self.fit(earliest)
        opening, cutoff = self.window(earliest)
        self.data.update(phase='overnight' if start >= cutoff or earliest < opening else 'break',
                         next_start_at=start, next_game=self.data['game'])
        self.save()

    def preparing(self, now):
        self.data.update(phase='preparing', deadline=now, next_start_at=self.fit(now))
        self.save()

    def start(self, now):
        if self.fit(now) > now:
            self.refresh(now)
            return False
        self.data.update(phase='live', started_at=now, deadline=now + self.LIVE_SECONDS,
                         next_game=self.games[(self.games.index(self.data['game']) + 1) % len(self.games)],
                         next_start_at=self.fit(now + self.LIVE_SECONDS + self.BREAK_SECONDS))
        self.data.pop('reason', None)
        self.data.pop('ending', None)
        self.data.pop('transition', None)
        self.save()
        return True

    def finish(self, now):
        self.data.update(game=self.data['next_game'], phase='break',
                         deadline=now + self.BREAK_SECONDS, slot_id=uuid.uuid4().hex)
        self.refresh(now)

    def interrupt(self, now, reason='Broadcast interrupted; retrying a full session', *, wait=True):
        self.data.update(phase='break', deadline=now + (self.BREAK_SECONDS if wait else 0),
                         reason=reason, slot_id=uuid.uuid4().hex)
        self.refresh(now)

    def request_start(self, now, game=None):
        if self.data['phase'] == 'live':
            return
        if game is not None and game in self.games:
            self.data['game'] = game
        self.interrupt(now, 'Starting the requested broadcast', wait=False)

    def upcoming(self, now, count=8):
        game = self.data['next_game'] if self.data['phase'] == 'live' else self.data['game']
        start = self.fit(max(now, self.data['next_start_at']))
        result = []
        for offset in range(count):
            result.append({'key': f"{self.data['slot_id']}:{offset}", 'game': game, 'start': start})
            game = self.games[(self.games.index(game) + 1) % len(self.games)]
            start = self.fit(start + self.LIVE_SECONDS + self.BREAK_SECONDS)
        return result


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Import editable settings into the application SQLite database.')
    parser.add_argument('--import-file', type=Path, required=True)
    arguments = parser.parse_args()
    load_parameters(arguments.import_file, reimport=True)
    print('Settings imported into SQLite.')
