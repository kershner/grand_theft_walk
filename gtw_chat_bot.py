"""Shared chat command handler for the local console and a future Twitch adapter."""

import logging

from san_andreas.runtime import DIRECTIONS, MOVEMENT_COMMANDS, WalkerControl

HELP = {
    "walk": "!walk — Start autowalking. Alias: !w.",
    "stop": "!stop — Stop movement without exiting the controller. Alias: !s.",
    "left": "!left [1-100] — Turn left in free mode; choose the next left on sidewalks. Alias: !l. Default: 1.",
    "right": "!right [1-100] — Turn right in free mode; choose the next right on sidewalks. Alias: !r. Default: 1.",
    "forward": "!forward [1-100] — Move forward that many one-second steps in free mode. Alias: !f. Default: 1.",
    "back": "!back [1-100] — Move backward that many one-second steps in free mode. Alias: !b. Default: 1.",
    "jump": "!jump — High jump to clear buildings. Requires being on foot and on the ground. Alias: !j.",
    "mode": "!mode <sidewalks|free> — Choose sidewalk navigation or free roaming.",
    "camera": "!camera [first|third] — Toggle the camera or select a perspective.",
    "help": "!help [command] — Show commands or details for a command.",
    "more": "!more — Show the remaining commands.",
    "info": "!info — Show live walker status.",
    "location": "!location — Show live San Andreas coordinates. Alias: !loc.",
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
            reply = self._reply(name, args)
        except (OSError, ValueError, RuntimeError) as error:
            reply = str(error)
            if name not in MOVEMENT_COMMANDS:
                logger.warning("!%s: %s", name[:100], reply)
        if name in MOVEMENT_COMMANDS:
            if reply:
                logger.warning("!%s: %s", name, reply)
            elif name in {"walk", "stop"}:
                await ctx.send("Autowalk toggled")
            return
        if reply:
            await ctx.send(reply)

    def _reply(self, name, args):
        if name == "help":
            if args:
                target = args[0].lstrip("!")
                target = ALIASES.get(target, target)
                return HELP.get(target, f"Unknown command: !{target}. Type !help for commands.")
            return "Commands: !walk • !stop • !left • !right • !forward • !back • !jump • Type !more for more commands."
        if name == "more":
            return (
                "More: !mode • !camera • !help • !info • !location • Type !help <cmd> for details."
            )
        if name not in HELP:
            return  # Match NMS: unrecognized commands are ignored.
        if name in DIRECTIONS:
            valid = not args or (
                len(args) == 1
                and args[0].isascii()
                and args[0].isdigit()
                and 1 <= int(args[0]) <= 100
            )
        elif name == "mode":
            valid = args in (["sidewalks"], ["free"])
        elif name == "camera":
            valid = args in ([], ["first"], ["third"])
        else:
            valid = not args
        if not valid:
            return HELP[name]

        if name in {"info", "location"}:
            return self._status_reply(name)

        settings = self.control.request(name, args)
        if name == "mode":
            return f"Navigation mode: {settings['mode']}."
        if name == "camera":
            perspective = "first" if settings["first_person"] else "third"
            return f"Camera: {perspective} person."

    def _status_reply(self, name):
        state = self.control.snapshot()
        if name == "info":
            settings = state.get("settings", {})
            perspective = "first" if settings.get("first_person") else "third"
            return (
                f"Grand Theft Walk — San Andreas • {state.get('walking', 'waiting')} • "
                f"mode: {settings.get('mode', 'unknown')} • camera: {perspective} person."
            )
        if not state.get("ready"):
            return "Game loading; please wait."
        position = state["position"]
        return (
            f"San Andreas • X: {position['x']:.1f} • Y: {position['y']:.1f} • "
            f"Z: {position['z']:.1f} • Interior: {state['interior']}."
        )
