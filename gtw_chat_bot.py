"""Chat command parsing, voting and walker status replies."""

import logging

from san_andreas.runtime import (
    DIRECTIONS,
    MOVEMENT_COMMANDS,
    NavigationModeError,
    WalkerControl,
)
from san_andreas.voting import CommandVoting

HELP = {
    "walk": "!walk — Start autowalking. Alias: !w.",
    "stop": "!stop — Stop movement without exiting the controller. Alias: !s.",
    "left": "!left [1-100] — Turn left in free mode; choose the next left in sidewalk mode. Alias: !l. Default: 1.",
    "right": "!right [1-100] — Turn right in free mode; choose the next right in sidewalk mode. Alias: !r. Default: 1.",
    "forward": "!forward [1-100] — Move forward that many one-second steps in free mode. Alias: !f. Default: 1.",
    "back": "!back [1-100] — Move backward that many one-second steps in free mode. Alias: !b. Default: 1.",
    "jump": "!jump — High jump to clear buildings. Requires being on foot and on the ground. Alias: !j.",
    "mode": "!mode <sidewalk|free> — Start a 20-second vote for sidewalk navigation or free roaming.",
    "camera": "!camera [first|third] — Start a 20-second vote to toggle the camera or select a perspective.",
    "yes": "!yes — Vote yes on the active camera or mode vote.",
    "no": "!no — Vote no on the active camera or mode vote.",
    "help": "!help [command] — Show commands or details for a command.",
    "more": "!more — Show the remaining commands.",
    "info": "!info — Show live walker status.",
    "location": "!location — Show the current area, game time, weather, and surroundings. Alias: !loc.",
    "mission": "!mission — Show the active mission or the three destinations up for voting.",
    "vote": "!vote <1|2|3> — Vote for the next mission. Your latest vote counts.",
}
ALIASES = {
    "w": "walk",
    "s": "stop",
    "l": "left",
    "r": "right",
    "f": "forward",
    "b": "back",
    "j": "jump",
    "loc": "location",
}
logger = logging.getLogger(__name__)


class GTWBot:
    def __init__(self, control=None):
        self.control = control if control is not None else WalkerControl()
        self.command_voting = CommandVoting(self._execute)

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
        try:
            author = getattr(ctx, "author", None)
            viewer = getattr(author, "name", None) or getattr(author, "id", None)
            viewer = str(viewer) if viewer else ""
            if name == "vote" and len(args) == 1 and args[0] in ("1", "2", "3"):
                reply = self.control.vote(viewer, int(args[0]))
            elif name in ("yes", "no") and not args:
                reply = self.command_voting.cast(viewer, name)
            else:
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
            return "More: !mode • !camera • !yes • !no • !help • !info • !location • !mission • !vote • Type !help <cmd> for details."
        if name not in HELP:
            return
        if name in {"vote", "yes", "no"}:
            return HELP[name]
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

        if name in {"info", "location", "mission"}:
            return self._status_reply(name)

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
            return self.command_voting.start(name, args, viewer, description)

        return self._execute(name, args)

    def _execute(self, name, args):
        settings = self.control.request(name, args)
        if name in {"walk", "stop"}:
            return "Autowalk toggled"
        if name in DIRECTIONS and settings["mode"] == "sidewalk":
            hint = "Use !mode free for manual movement."
            if not settings["walking"]:
                hint = "Use !walk to continue or !mode free for manual movement."
            return f"Sidewalk mode: direction queued for the next junction. {hint}"
        if name == "mode":
            return f"Navigation mode: {settings['mode']}."
        if name == "camera":
            perspective = "first" if settings["first_person"] else "third"
            return f"Camera: {perspective} person."

    def _status_reply(self, name):
        state = self.control.snapshot()
        if name == "mission":
            missions = state.get("missions", {})
            active = missions.get("active")
            if active:
                x, y, z = active["position"]
                return (
                    f"Mission: {active['name']} • Meeting point: {x:.1f}, {y:.1f}, {z:.1f} • "
                    f"Standing: {missions['held_seconds']:g}/{missions['hold_seconds']:g} seconds."
                )
            poll = missions.get("poll")
            if poll:
                return (
                    "Vote for the next mission! "
                    + " • ".join(
                        f"{i}: {d['name']} ({poll['counts'][i - 1]} votes)"
                        for i, d in enumerate(poll["options"], 1)
                    )
                    + " • !vote 1/2/3"
                )
            return missions.get("error") or "Waiting for mission destinations."
        if name == "info":
            settings = state.get("settings", {})
            perspective = "first" if settings.get("first_person") else "third"
            reply = (
                f"Grand Theft Walk — San Andreas • {state.get('walking', 'waiting')} • "
                f"mode: {settings.get('mode', 'unknown')} • camera: {perspective} person."
            )
            mission = state.get("missions", {}).get("active")
            if mission:
                reply += f" • Current mission: Travel to {mission['name']}"
            return reply
        if not state.get("ready"):
            return "Game loading; please wait."
        location = state.get("location")
        if location:
            parts = [", ".join(location["places"])]
            if location.get("indoors"):
                parts.append("indoors")
            elif location.get("road"):
                parts.append(location["road"])
            clock = " ".join(filter(None, (location.get("date"), location.get("time"))))
            if clock:
                parts.append(f"{clock} ({location['period']})")
            parts.extend((location["weather"], f"heading {location['heading']}"))
            return " • ".join(parts)
        position = state["position"]
        return (
            f"San Andreas • X: {position['x']:.1f} • Y: {position['y']:.1f} • "
            f"Z: {position['z']:.1f} • Interior: {state['interior']}."
        )
