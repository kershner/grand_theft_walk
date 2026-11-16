"""Chat command parsing, voting and walker status replies."""

import logging
import threading
import time

from gtw.games import get_game
from gtw.runtime import (
    DIRECTIONS,
    MOVEMENT_COMMANDS,
    NavigationModeError,
    WalkerControl,
    chat_ready,
    mission_distance,
)
from gtw.voting import CommandVoting

HELP = {
    "walk": "!walk — Start autowalking. Alias: !w.",
    "stop": "!stop — Stop movement without exiting the controller. Alias: !s.",
    "left": "!left [1-100] — Turn left in free mode; choose the next left in sidewalk mode. Alias: !l. Default: 1.",
    "right": "!right [1-100] — Turn right in free mode; choose the next right in sidewalk mode. Alias: !r. Default: 1.",
    "up": "!up [1-100] — Look up in either navigation mode. Alias: !u. Default: 1.",
    "down": "!down [1-100] — Look down in either navigation mode. Alias: !d. Default: 1.",
    "center": "!center — Level the view without changing direction or navigation mode.",
    "forward": "!forward [1-100] — Move forward that many one-second steps in free mode. Alias: !f. Default: 1.",
    "back": "!back [1-100] — Move backward that many one-second steps in free mode. Alias: !b. Default: 1.",
    "jump": "!jump — High jump to clear buildings. Requires being on foot and on the ground. Alias: !j.",
    "mode": "!mode <sidewalk|free> — Start a 20-second vote for sidewalk navigation or free roaming.",
    "camera": "!camera [first|third] — Start a 20-second vote to toggle the camera or select a perspective.",
    "yes": "!yes — Vote yes on the active camera or mode vote.",
    "no": "!no — Vote no on the active camera or mode vote.",
    "help": "!help [command] — Show commands or details for a command.",
    "more": "!more — Show the remaining commands.",
    "info": "!info — Show the current area, game time, weather, navigation mode, camera, and mission.",
    "vote": "!vote <1|2|3> — Vote for the next mission. Your latest vote counts.",
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


class GTWBot:
    def __init__(self, control=None):
        self.control = control if control is not None else WalkerControl()
        self.command_voting = CommandVoting(self._execute)
        self.session = None
        self.announced_session = None
        self.next_announcement_at = None
        self.announcement_index = 0
        self.runtime_lock = threading.Lock()
        self.runtime_stop = threading.Event()
        self.runtime_thread = None

    def ready_state(self):
        try:
            state = self.control.snapshot()
        except RuntimeError:
            return None
        return state if chat_ready(state) else None

    def poll_announcements(self, now=None):
        now = time.monotonic() if now is None else now
        with self.runtime_lock:
            state = self.ready_state()
            if state is None:
                return []
            session = state.get("session", state.get("pid", "controller"))
            if self.session != session:
                self.session = session
                self.announcement_index = 0
                self.next_announcement_at = now + ANNOUNCEMENT_INTERVAL
                return [
                    "Grand Theft Walk is online!",
                    self._reply("help", []),
                    self._info_reply(state),
                ]
            if now < self.next_announcement_at:
                return []
            reply = (
                self._reply("help", [])
                if self.announcement_index == 0
                else self._info_reply(state)
            )
            self.announcement_index = (self.announcement_index + 1) % 2
            missed = int((now - self.next_announcement_at) // ANNOUNCEMENT_INTERVAL) + 1
            self.next_announcement_at += missed * ANNOUNCEMENT_INTERVAL
            return [reply]

    def start_runtime(self, send):
        if self.runtime_thread is not None:
            return

        def run():
            while not self.runtime_stop.is_set():
                try:
                    for message in self.poll_announcements():
                        send(message)
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
            reply = self._reply(name, args, viewer)
        except NavigationModeError as error:
            reply = str(error)
        except (OSError, ValueError, RuntimeError) as error:
            reply = str(error)
            logger.warning("!%s: %s", name[:100], reply)
            if name in MOVEMENT_COMMANDS:
                return
        if reply:
            await ctx.send(reply)

    def _reply(self, name, args, viewer=""):
        if name == "help":
            if args:
                target = args[0].lstrip("!")
                target = ALIASES.get(target, target)
                return HELP.get(
                    target, f"Unknown command: !{target}. Type !help for commands."
                )
            return "Commands: !walk • !stop • !left • !right • !forward • !back • !jump • Type !more for more commands."
        if name == "more":
            return "More: !up • !down • !center • !mode • !camera • !help • !info • Type !help <cmd> for details."
        if name not in HELP:
            return
        if name == "vote":
            if len(args) == 1 and args[0] in ("1", "2", "3"):
                return self.control.vote(viewer, int(args[0]))
            return HELP[name]
        if name in {"yes", "no"}:
            return HELP[name] if args else self.command_voting.cast(viewer, name)
        if name in DIRECTIONS:
            valid = not args or (
                len(args) == 1
                and args[0].isascii()
                and args[0].isdigit()
                and 1 <= int(args[0]) <= 100
            )
        elif name == "mode":
            valid = args in (["sidewalk"], ["free"])
        elif name == "camera":
            valid = args in ([], ["first"], ["third"])
        else:
            valid = not args
        if not valid:
            if name in MOVEMENT_COMMANDS:
                raise ValueError(HELP[name])
            return HELP[name]

        if name == "info":
            return self._info_reply()

        if name in {"camera", "mode"}:
            state = self.control.snapshot()
            if not state.get("ready"):
                raise RuntimeError("Game loading; please wait.")
            if name == "mode":
                description = f"Navigation mode: {args[0]}."
            elif args:
                description = f"Camera: {args[0]} person."
            else:
                description = "Toggle camera perspective."
            session = state.get("session", state.get("pid", "controller"))
            return self.command_voting.start(
                name, args, viewer, description,
                execute=lambda command, values: self._execute_for_session(
                    session, command, values
                ),
            )

        return self._execute(name, args)

    def _execute_for_session(self, session, name, args):
        state = self.control.snapshot()
        if state.get("session", state.get("pid", "controller")) != session:
            raise RuntimeError("Game session changed; start a new vote.")
        return self._execute(name, args)

    def _execute(self, name, args):
        settings = self.control.request(name, args)
        if name in {"walk", "stop"}:
            return "Autowalk toggled"
        if name in {"left", "right", "forward"} and settings["mode"] == "sidewalk":
            hint = "Use !mode free for manual movement."
            if not settings["walking"]:
                hint = "Use !walk to continue or !mode free for manual movement."
            return f"Sidewalk mode: direction queued for the next junction. {hint}"
        if name == "mode":
            return f"Navigation mode: {settings['mode']}."
        if name == "camera":
            perspective = "first" if settings["first_person"] else "third"
            return f"Camera: {perspective} person."

    def _info_reply(self, state=None):
        state = self.control.snapshot() if state is None else state
        if not chat_ready(state):
            return "Game loading; please wait."
        location = state.get("location") or {}
        game = getattr(self.control, "game", get_game())
        parts = [", ".join(location.get("places") or [game.title])]
        clock = " ".join(filter(None, (location.get("date"), location.get("time"))))
        if clock:
            period = location.get("period")
            parts.append(f"{clock} ({period})" if period else clock)
        if location.get("weather"):
            parts.append(location["weather"])
        if location.get("heading"):
            parts.append(f"heading {location['heading']}")
        settings = state.get("settings", {})
        perspective = "first" if settings.get("first_person") else "third"
        parts.extend(
            (
                f"mode: {settings.get('mode', 'unknown')}",
                f"camera: {perspective} person",
            )
        )
        mission = state.get("missions", {}).get("active")
        if mission:
            parts.extend(
                (
                    f"Current mission: Travel to {mission['name']}",
                    mission_distance(state, mission),
                )
            )
        return " • ".join(parts)
