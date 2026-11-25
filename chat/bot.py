"""Chat command parsing, voting and walker status replies."""

import logging
import math
import threading
import time
from pathlib import Path

from core.files import atomic_json, read_json
from core.missions import CommandVoting
from core.runtime import (
    DIRECTIONS,
    MODES,
    MOVEMENT_COMMANDS,
    NavigationModeError,
    UnsupportedCommandError,
    WalkerControl,
    chat_ready,
    command_supported,
    mission_distance,
    require_command,
)
from games import get_game


HELP = {
    "walk": "!walk • Start autowalking. Alias: !w.",
    "stop": "!stop • Stop movement without exiting the controller. Alias: !s.",
    "left": "!left [1-100] • Turn left in free mode; choose the next left in mission or explore mode. Alias: !l. Default: 1.",
    "right": "!right [1-100] • Turn right in free mode; choose the next right in mission or explore mode. Alias: !r. Default: 1.",
    "up": "!up [1-100] • Look up in any navigation mode. Alias: !u. Default: 1.",
    "down": "!down [1-100] • Look down in any navigation mode. Alias: !d. Default: 1.",
    "center": "!center • Level the view without changing direction or navigation mode.",
    "forward": "!forward [1-100] • Move forward that many one-second steps in free mode; choose straight ahead at the next junction in mission or explore mode. Alias: !f. Default: 1.",
    "back": "!back [1-100] • Move backward that many one-second steps in free mode. Alias: !b. Default: 1.",
    "drop": "!drop • Let go of a ledge in San Andreas. Only works while climbing or hanging.",
    "jump": "!jump • High jump to clear buildings. Requires being on foot and on the ground. Alias: !j.",
    "respawn": "!respawn • Start a 60-second vote to kill the player and respawn normally. Vote !yes or !no; ties fail.",
    "yes": "!yes • Vote yes on the active command vote.",
    "no": "!no • Vote no on the active command vote.",
    "mode": "!mode <free|explore|mission> • Choose manual roaming, city and wilderness exploration, or mission navigation.",
    "free": "!free • Switch to manual roaming.",
    "explore": "!explore • Explore pedestrian paths and open ground, remembering covered areas.",
    "mission": "!mission • Navigate toward the current mission.",
    "camera": "!camera [first|third]: Toggle the camera or select a perspective.",
    "help": "!help [command] • Show commands or details for a command.",
    "more": "!more • Show the remaining commands.",
    "info": "!info • Show the current area, game time, weather, navigation mode, mission, and next game countdown.",
    "vote": "!vote <1|2|3> • Vote for the next mission. Your latest vote counts.",
}
ALIASES = {
    "w": "walk",
    "s": "stop",
    "l": "left",
    "r": "right",
    "u": "up",
    "d": "down",
    "f": "forward",
    "b": "back",
    "j": "jump",
}
logger = logging.getLogger(__name__)
ANNOUNCEMENT_INTERVAL = 20 * 60
ROTATION_WARNING_SECONDS = 10 * 60
WATER_IDLE_VOTE_SECONDS = 120


def location_text(state):
    location = state.get("location") or {}
    parts = []
    if location.get("places"):
        parts.append(", ".join(location["places"]))
    clock = " ".join(filter(None, (location.get("date"), location.get("time"))))
    if clock:
        parts.append(clock)
    if location.get("weather"):
        parts.append(location["weather"])
    if location.get("heading"):
        parts.append(f"heading {location['heading']}")
    return " • ".join(parts)


def read_rotation(path, game):
    if path is None:
        return {}
    try:
        rotation = read_json(path)
    except (OSError, ValueError):
        return {}
    return rotation if isinstance(rotation, dict) and rotation.get("game") == game else {}


def rotation_countdown(rotation):
    deadline = rotation.get("next_switch_at")
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        return ""
    minutes = max(0, math.ceil((deadline - time.time()) / 60))
    hours, minutes = divmod(minutes, 60)
    duration = f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"
    return f"Next game in: {duration}"


class ChatBot:
    def __init__(self, control=None, rotation_file=None):
        self.rotation_file = Path(rotation_file) if rotation_file is not None else None
        self.control = control if control is not None else WalkerControl()
        self.command_voting = CommandVoting(self._execute)
        self.session = None
        self.announced_session = None
        self.next_announcement_at = None
        self.announcement_index = 0
        self.warned_rotation = None
        self.runtime_lock = threading.Lock()
        self.runtime_stop = threading.Event()
        self.runtime_thread = None
        self.event_lock = threading.Lock()
        self.seen_events = set()
        self.last_game_command = None
        self.water_session = None
        self.water_started = self.water_observed = self.water_dry_since = None
        self.water_seconds = 0.
        self.water_vote_sent = False
        state_dir = getattr(self.control, "state_dir", None)
        if isinstance(state_dir, (str, Path)):
            try:
                previous = read_json(Path(state_dir) / "state.json")
            except (OSError, ValueError):
                previous = {}
            missions = previous.get("missions", {})
            pending = (missions.get("active") or {}).get("arrival_event_id")
            self.seen_events = {event["id"] for event in missions.get("events", []) if event["id"] != pending}

    def ready_state(self):
        try:
            state = self.control.snapshot()
        except RuntimeError:
            return None
        return state if chat_ready(state) else None

    def poll_announcements(self, now=None):
        now = time.monotonic() if now is None else now
        with self.runtime_lock:
            warnings = self._rotation_warning()
            state = self.ready_state()
            if state is None:
                return warnings
            session = state.get("session", state.get("pid", "controller"))
            if self.session != session:
                self.session = session
                self.announcement_index = 0
                self.next_announcement_at = now + ANNOUNCEMENT_INTERVAL
                return [
                    "Grand Theft Walk is online!",
                    self._reply("help", []),
                    self._info_reply(state),
                    *warnings,
                ]
            if now < self.next_announcement_at:
                return warnings
            reply = (
                self._reply("help", [])
                if self.announcement_index == 0
                else self._info_reply(state)
            )
            self.announcement_index = (self.announcement_index + 1) % 2
            missed = int((now - self.next_announcement_at) // ANNOUNCEMENT_INTERVAL) + 1
            self.next_announcement_at += missed * ANNOUNCEMENT_INTERVAL
            return [*warnings, reply]

    def poll_events(self, *, include_announcements=True, wait_for_announcement=False, now=None):
        """Collect announcements, vote results, and new mission events for either transport."""
        with self.event_lock:
            messages = self.poll_announcements(now) if include_announcements else []
            messages.extend(self.command_voting.drain_events())
            state = self.ready_state()
            if state is None:
                self.water_observed = None
                return messages
            session = state.get("session", state.get("pid", "controller"))
            if wait_for_announcement and self.announced_session != session:
                return messages
            messages.extend(self._water_vote(state, time.monotonic() if now is None else now))
            events = state.get("missions", {}).get("events", [])
            for event in events:
                if event["id"] not in self.seen_events:
                    messages.append(event["text"])
                    self.seen_events.add(event["id"])
            self.seen_events.intersection_update(event["id"] for event in events)
            return messages

    def start_runtime(self, send):
        if self.runtime_thread is not None:
            return

        def run():
            while not self.runtime_stop.is_set():
                try:
                    for message in self.poll_events():
                        send(message)
                        self.acknowledge_rotation(message)
                    self.announced_session = self.session
                except (OSError, ValueError, RuntimeError):
                    logger.exception("Scheduled chat announcement failed")
                self.runtime_stop.wait(0.5)

        self.runtime_thread = threading.Thread(
            target=run, name="chat-announcements", daemon=True
        )
        self.runtime_thread.start()

    def close(self):
        self.runtime_stop.set()
        if self.runtime_thread is not None:
            self.runtime_thread.join(timeout=2)
        self.command_voting.close()

    async def process_chat_message(self, ctx, content):
        content = (content or "").strip()
        if not content.startswith("!"):
            return
        parts = content[1:].split()
        if not parts:
            return
        name = parts[0].lower()
        name = ALIASES.get(name, name)
        args = [arg.lower() for arg in parts[1:]]
        logger.debug("Chat command: !%s %s", name[:100], " ".join(args)[:200])
        if self.ready_state() is None:
            await ctx.send("Game loading; please wait.")
            return
        try:
            author = getattr(ctx, "author", None)
            viewer = getattr(author, "name", None) or getattr(author, "id", None)
            viewer = str(viewer) if viewer else ""
            reply = self._reply(name, args, viewer, activity=True)
        except (NavigationModeError, UnsupportedCommandError) as error:
            reply = str(error)
        except (OSError, ValueError, RuntimeError) as error:
            reply = str(error)
            logger.warning("!%s: %s", name[:100], reply)
            if name in MOVEMENT_COMMANDS:
                return
        if reply:
            await ctx.send(reply)

    def _note_command(self):
        with self.event_lock:
            self.last_game_command = time.monotonic()

    def _water_vote(self, state, now):
        session = state.get('session', state.get('pid', 'controller'))
        if session != self.water_session:
            self.water_session = session
            self.water_started = self.water_observed = self.water_dry_since = None
            self.water_seconds, self.water_vote_sent = 0., False
        game = state.get('game', getattr(getattr(self.control, 'game', None), 'id', None))
        if game != 'san_andreas' or not command_supported(state, 'respawn'):
            self.water_observed = None
            return []
        if not state.get('in_water'):
            if state.get('standing') and not state.get('in_air'):
                if self.water_dry_since is None:
                    self.water_dry_since = now
                elif now - self.water_dry_since >= 1:
                    self.water_started = None
                    self.water_seconds, self.water_vote_sent = 0., False
            self.water_observed = None
            return []
        self.water_dry_since = None
        previous, self.water_observed = self.water_observed, now
        if state.get('paused') or state.get('health', 100) <= 0 or state.get('in_vehicle'):
            return []
        if self.water_started is None:
            self.water_started = now
        if previous is not None and 0 <= now - previous <= 5:
            self.water_seconds += now - previous
        last_interaction = max(self.water_started, self.last_game_command or self.water_started)
        if (self.water_vote_sent or self.water_seconds < WATER_IDLE_VOTE_SECONDS
                or now - last_interaction < WATER_IDLE_VOTE_SECONDS):
            return []
        if self.command_voting.vote.active:
            if self.command_voting.vote.command == 'respawn':
                self.water_vote_sent = True
            return []
        self.water_vote_sent = True
        return ['Two minutes swimming without a game command. ' + self._respawn_vote(None, state, automatic=True)]

    def _respawn_vote(self, viewer, state, *, automatic=False):
        session = state.get('session', state.get('pid', 'controller'))

        def apply_respawn(command, arguments):
            if automatic:
                current = self.control.snapshot()
                if not current.get('in_water'):
                    return 'Automatic respawn cancelled: player is back on land.'
            self.control.request(command, arguments, expected_session=session)
            return 'Respawn requested.'

        starter = 'automatic:respawn' if automatic else viewer
        return self.command_voting.start('respawn', [], starter, 'Respawn the player',
                                         execute=apply_respawn, duration=60)

    def _reply(self, name, args, viewer="", *, activity=False):
        if name in MODES and not args:
            return self._reply("mode", [name], viewer, activity=activity)
        if name in MODES:
            return HELP[name]
        if name not in HELP:
            return
        state = self.control.snapshot()
        if name == "help":
            if args:
                target = args[0].lstrip("!")
                target = ALIASES.get(target, target)
                require_command(state, target)
                if activity and target in HELP:
                    self._note_command()
                return HELP.get(
                    target, f"Unknown command: !{target}. Type !help for commands."
                )
            commands = ("walk", "stop", "left", "right", "forward", "back", "jump", "drop", "respawn")
            if activity:
                self._note_command()
            available = " • ".join(f"!{command}" for command in commands if command_supported(state, command))
            return f"Commands: {available} • !more"
        if name == "more":
            if activity:
                self._note_command()
            commands = ("up", "down", "center", "free", "explore", "mission", "camera", "help", "info")
            available = " • ".join(f"!{command}" for command in commands if command_supported(state, command))
            return f"More: {available} • Type !help <cmd> for details."
        require_command(state, name)
        if name == "vote":
            if len(args) == 1 and args[0] in ("1", "2", "3"):
                if activity:
                    self._note_command()
                return self.control.vote(viewer, int(args[0]))
            return HELP[name]
        if name in DIRECTIONS:
            valid = not args or (
                len(args) == 1
                and args[0].isascii()
                and args[0].isdigit()
                and 1 <= int(args[0]) <= 100
            )
        elif name == "mode":
            valid = len(args) == 1 and args[0] in MODES
        elif name == "camera":
            valid = args in ([], ["first"], ["third"])
        else:
            valid = not args
        if not valid:
            if name in MOVEMENT_COMMANDS:
                raise ValueError(HELP[name])
            return HELP[name]

        if activity:
            self._note_command()

        if name == "info":
            return self._info_reply()

        if name in {"yes", "no"}:
            return self.command_voting.cast(viewer, name)
        if name == "respawn":
            return self._respawn_vote(viewer, state)
        return self._execute(name, args)

    def _execute(self, name, args):
        settings = self.control.request(name, args)
        if name in {"walk", "stop"}:
            return "Autowalk toggled"
        if name in {"left", "right", "forward"} and settings["mode"] in ("mission", "explore"):
            hint = "Use !mode free for manual movement."
            if not settings["walking"]:
                hint = "Use !walk to continue or !mode free for manual movement."
            return f"{settings['mode'].capitalize()} mode: direction queued for the next junction. {hint}"
        if name == "mode":
            return f"Navigation mode: {settings['mode']}."
        if name == "camera":
            perspective = "first" if settings["first_person"] else "third"
            return f"Camera: {perspective} person."

    def _rotation(self):
        game = getattr(self.control, "game", None)
        return read_rotation(self.rotation_file, game.id) if game is not None else {}

    def acknowledge_rotation(self, message):
        """Acknowledge only after the transport successfully sends the message."""
        rotation = self._rotation()
        if rotation.get("status") != "switching" or rotation.get("switch_announced"):
            return
        try:
            expected = f"Switching to {get_game(rotation.get('next_game')).title}..."
        except ValueError:
            return
        if message == expected:
            atomic_json(self.rotation_file, dict(rotation, switch_announced=True))

    def _rotation_warning(self):
        rotation = self._rotation()
        deadline, next_game = rotation.get("next_switch_at"), rotation.get("next_game")
        if type(deadline) not in (int, float) or not math.isfinite(deadline) or not next_game:
            return []
        try:
            title = get_game(next_game).title
        except ValueError:
            return []
        if rotation.get("status") == "switching":
            return [] if rotation.get("switch_announced") else [f"Switching to {title}..."]
        remaining = deadline - time.time()
        identity = (next_game, rotation.get('rotation_id', deadline))
        if not ROTATION_WARNING_SECONDS - 60 < remaining <= ROTATION_WARNING_SECONDS or self.warned_rotation == identity:
            return []
        self.warned_rotation = identity
        return [f"Switching to {title} in 10 minutes."]

    def _info_reply(self, state=None):
        state = self.control.snapshot() if state is None else state
        if not chat_ready(state):
            return "Game loading; please wait."
        game = getattr(self.control, "game", get_game())
        parts = [f"Game: {game.title}"]
        location = location_text(state)
        if location:
            parts.append(location)
        settings = state.get("settings", {})
        parts.append(f"mode: {settings.get('mode', 'unknown')}")
        mission = state.get("missions", {}).get("active")
        if mission:
            parts.extend(
                (
                    f"Current mission: Travel to {mission['name']}",
                    mission_distance(state, mission),
                )
            )
        countdown = rotation_countdown(self._rotation())
        if countdown:
            parts.append(countdown)
        parts.append("Type !help for commands")
        return " • ".join(parts)
