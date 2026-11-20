"""Keyboard/native input, view and jump commands, and native respawn recovery."""

import configparser
import ctypes as c
import io
import logging
import struct
import time
from ctypes import wintypes as w
from pathlib import Path

import keyboard

from core.files import read_json, sha256
from core.navigation import SmoothTurn
from core.runtime import request_expired
from core.winmemory import k32


logger = logging.getLogger(__name__)
MOUSE_UNITS_PER_STEP = 10
JUMP_SPEED = 2.0  # Default takeoff speed; San Andreas calibrates for its own physics.


def can_control(state):
    return (
        state.get("ready")
        and (state["foreground"] or state.get("background_control", False))
        and not state["paused"]
        and not state["controls_disabled"]
        and not state["in_vehicle"]
        and not state.get("cutscene", False)
        and not state.get("drowning", False)
        and state["health"] > 0
        and state["player_state"] == 0
    )


def bind_action_key(memory, changes, address, count, action, key):
    """Own a native keyboard binding and clear conflicts in both keyboard columns."""
    data = memory.read(address, count * 32)
    if struct.unpack_from("<I", data, action * 32)[0] > 1056:
        raise RuntimeError("Invalid control binding")
    for other in range(count):
        if other == action:
            continue
        for column in (0, 1):
            offset = other * 32 + column * 8
            if struct.unpack_from("<I", data, offset)[0] == key:
                changes.patch(address + offset, struct.pack("<I", 1056))
    changes.patch(address + action * 32, struct.pack("<I", key))
    if not struct.unpack_from("<I", data, action * 32 + 4)[0]:
        changes.patch(address + action * 32 + 4, struct.pack("<I", 1))


class MouseInput(c.Structure):
    _fields_ = [
        ("dx", w.LONG),
        ("dy", w.LONG),
        ("mouseData", w.DWORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", c.c_size_t),
    ]


class InputData(c.Union):
    _fields_ = [("mouse", MouseInput)]


class Input(c.Structure):
    _fields_ = [("type", w.DWORD), ("data", InputData)]


_user32 = c.WinDLL("user32", use_last_error=True)
_user32.SendInput.argtypes = [w.UINT, c.POINTER(Input), c.c_int]
_user32.SendInput.restype = w.UINT


def key_down(key):
    return keyboard.is_pressed(key)


class Hotkeys:
    def __init__(self, names):
        self.names = tuple(names)
        self.down = set()

    def pressed(self):
        down = {key for key in self.names if key_down(key)}
        pressed = down - self.down
        self.down = down
        return [key for key in self.names if key in pressed]


class Keys:
    def __init__(self, bindings=None, focused=None):
        self.bindings = bindings or {}
        self.focused = focused if focused is not None else lambda: True
        self.held = set()

    def set(self, desired):
        desired = set(desired)
        if not desired <= self.bindings.keys():
            raise ValueError("Unknown input action")
        if not self.focused():
            self.release()
            return
        self.release(self.held - desired)
        for action in sorted(desired - self.held, key=lambda name: name != "walk"):
            if not self.focused():
                self.release()
                return
            keyboard.press(self.bindings[action])
            self.held.add(action)

    def tap(self, key, duration=0.08):
        if not self.focused():
            return False
        keyboard.press(key)
        try:
            time.sleep(duration)
        finally:
            keyboard.release(key)
        return True

    def turn(self, amount):
        self.move_mouse(amount, 0)

    def look(self, amount):
        self.move_mouse(0, amount)

    def move_mouse(self, horizontal, vertical):
        if not (horizontal or vertical) or not self.focused():
            return
        event = Input(
            type=0,
            data=InputData(
                mouse=MouseInput(int(horizontal), int(vertical), 0, 1, 0, 0)
            ),
        )
        if _user32.SendInput(1, c.byref(event), c.sizeof(Input)) != 1:
            raise RuntimeError("Windows rejected mouse input")

    def release(self, actions=None):
        failure = None
        selected = self.held if actions is None else self.held & set(actions)
        for action in sorted(selected):
            try:
                keyboard.release(self.bindings[action])
                self.held.remove(action)
            except Exception as error:  # noqa: BLE001 - Release every key before reraising the first failure.
                failure = failure or error
        if failure is not None:
            raise failure


class ViewControl:
    def __init__(self, focused=None, look_multiplier=1):
        self.keys = Keys(focused=focused)
        self.look_multiplier = look_multiplier
        self.turn = SmoothTurn()
        self.last_request = None
        self.cancel()

    def cancel(self):
        self.pending = 0
        self.centering = False
        self.center_deadline = 0.0
        self.turn.reset()

    def accept_request(self, request, now):
        if not isinstance(request, dict) or request.get("id") == self.last_request:
            return
        self.last_request = request.get("id")
        if request_expired(request):
            return
        direction = request.get("direction")
        amount = request.get("amount", 1)
        if direction == "center":
            self.cancel()
            self.centering = True
            self.center_deadline = now + 8
        elif direction in ("up", "down") and type(amount) is int and 1 <= amount <= 100:
            self.centering = False
            self.pending += (
                (-1 if direction == "up" else 1) * amount * MOUSE_UNITS_PER_STEP
            )

    def tick(self, request, state, now, saving=False):
        if not can_control(state) or saving:
            self.cancel()
            return
        self.accept_request(request, now)
        if self.centering:
            pitch = state.get("camera_pitch")
            if pitch is None or abs(pitch) <= 0.02 or now >= self.center_deadline:
                self.cancel()
                return
            pixels = self.turn.step(pitch * 140, now)
        else:
            pixels, self.pending = self.turn.nudge(self.pending, now)
        if pixels:
            self.keys.look(pixels * self.look_multiplier)


class HighJump:
    def __init__(self, game, bindings, binder):
        self.game = game
        self.bindings = bindings
        self.binder = binder
        self.keys = Keys({"jump": "space", "drop": "f"}, focused=game.mem.foreground)
        self.last_request = None
        self.player = None
        self.action = "jump"
        self.release_at = 0.0
        self.deadline = 0.0

    def cancel(self):
        self.keys.release()
        self.player = None

    def tick(self, request, state, now, saving=False):
        usable = can_control(state) and not saving
        if self.player is not None:
            if not usable or self.player != (state["ped"], state["identity"]):
                self.cancel()
            else:
                if now >= self.release_at:
                    self.keys.release()
                if self.action == "drop":
                    if now >= self.release_at:
                        self.cancel()
                elif (
                    state["in_air"]
                    and not state["standing"]
                    and state["vertical_speed"] > 0
                ):
                    if self.game.boost_jump(state):
                        logger.info("Jump takeoff adjusted")
                    self.cancel()
                elif now >= self.deadline:
                    logger.warning("Jump did not take off; boost cancelled")
                    self.cancel()

        if not isinstance(request, dict) or request.get("id") == self.last_request:
            return
        if request_expired(request):
            self.last_request = request.get("id")
            return
        if not usable:
            return  # Allow time to return focus to the game.
        self.last_request = request.get("id")
        action = "drop" if request.get("direction") == "drop" else "jump"
        if action == "drop":
            self.cancel()
            can_drop = getattr(self.game, "can_drop", None)
            if can_drop is None or not can_drop(state):
                logger.debug("Drop ignored: no active ledge climb")
                return
        elif self.player is not None or not state["standing"] or state["in_air"]:
            logger.debug("Jump ignored: already jumping or airborne")
            return
        self.binder(self.game, self.bindings)
        self.action = action
        self.keys.set({action})
        self.player = (state["ped"], state["identity"])
        self.release_at = now + 0.12
        self.deadline = now + 2.0
        logger.debug("%s requested", action.capitalize())


logger = logging.getLogger(__name__)


class Respawn:
    def __init__(self, game, protection, session):
        self.game = game
        self.protection = protection
        self.session = session
        self.last_request = None
        self.active = False
        self.seen_dead = False
        self.started_at = 0.0

    def requested(self, request):
        return bool(
            isinstance(request, dict) and request.get("id")
            and request["id"] != self.last_request
            and request.get("session") == self.session
            and not request_expired(request)
        )

    def tick(self, request, state, now, saving=False):
        if self.active:
            living = bool(state.get("ready") and state.get("health", 0) > 0
                          and state.get("player_state") == 0)
            self.seen_dead = self.seen_dead or not living
            if self.seen_dead and living:
                self.active = False
                logger.info("Player respawned; normal controls resumed")
            elif now - self.started_at >= 30:
                self.active = False
                logger.warning("Native respawn is taking longer than expected; controls resumed")
            return self.active
        if not self.requested(request):
            return False
        if not state.get("ready"):
            return False
        if state.get("health", 0) <= 0 or state.get("player_state") != 0:
            self.last_request = request["id"]
            return False  # An existing native death already satisfies this request.
        if saving or not (state.get("foreground") or state.get("background_control")) or state.get("paused") or state.get("cutscene"):
            return False
        self.last_request = request["id"]
        if self.protection:
            failures = self.protection.restore()
            if failures:
                logger.error("Respawn cancelled: could not suspend protection: %s", failures)
                return False
        if not self.game.kill_player(state):
            logger.warning("Respawn cancelled: the current player changed")
            return False
        self.active, self.seen_dead, self.started_at = True, False, now
        logger.info("Voted respawn: player health cleared; waiting for native respawn")
        return True


MODULE = "GTW.BackgroundInput.asi"


WINDOW_CONFIG = "scripts/III.VC.SA.WindowedMode.cfg"


BUTTONS = {"forward": 1, "back": 2, "jump": 4, "drop": 8, "walk": 16}


def configure_window(directory):
    """Keep window geometry local; set streaming requirements before launch."""
    path = Path(directory) / WINDOW_CONFIG
    config = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    config.optionxform = str
    if path.exists():
        config.read(path, encoding="utf-8-sig")
    if not config.has_section("MAIN"):
        config.add_section("MAIN")
    required = {"WindowMode": "2", "PauseOnFocusLoss": "0", "ResumeOnFocus": "0", "ConfineCursor": "1"}
    if all(config.get("MAIN", key, fallback=None) == value for key, value in required.items()):
        return
    for key, value in required.items():
        config.set("MAIN", key, value)
    output = io.StringIO()
    config.write(output)
    path.write_text(output.getvalue(), encoding="utf-8")


class NativeInput:
    def __init__(self, memory):
        self.mem = memory
        manifest = read_json(Path(__file__).parent / "native/background.json")
        module = memory.module(MODULE)
        if not module or sha256(module["path"]) != manifest["sha256"]:
            raise RuntimeError("The background input bridge is missing or changed; run suite repair.")
        self.address = module["base"] + manifest["state_rva"]
        if manifest["state_rva"] + 40 > module["size"]:
            raise RuntimeError("Invalid background input bridge layout")
        protocol, status, game = memory.unpack(self.address, "3I")
        expected = {"san_andreas": 1, "vice_city": 2, "gta_iii": 3}[memory.profile.id]
        if (protocol, status, game) != (1, 1, expected):
            raise RuntimeError("The background input hook did not match this game; refusing desktop input fallback.")
        self.channels = []
        self.x = self.y = 0
        memory.background_control = True

    def keys(self, actions=()):
        channel = NativeKeys(self, actions)
        self.channels.append(channel)
        return channel

    def publish(self):
        bits = 0
        for channel in self.channels:
            for action in channel.held:
                bits |= BUTTONS[action]
        self.mem.write(self.address + 20, struct.pack("<3I", bits, self.x, self.y))
        self.mem.write(self.address + 16, struct.pack("<I", k32.GetTickCount() & 0xFFFFFFFF))
        self.mem.write(self.address + 12, struct.pack("<I", 1))

    def report(self):
        frames, applied = self.mem.unpack(self.address + 32, "2I")
        return {"enabled": True, "input": "native", "frames": frames, "applied_frames": applied}

    def close(self):
        self.mem.background_control = False
        if self.mem.alive():
            self.mem.write(self.address + 12, bytes(20))


class NativeKeys:
    def __init__(self, bridge, actions):
        self.bridge = bridge
        self.bindings = dict(actions) if isinstance(actions, dict) else {name: name for name in actions}
        self.held = set()

    def set(self, desired):
        desired = set(desired)
        if not desired <= self.bindings.keys() or not desired <= BUTTONS.keys():
            raise ValueError("Unknown native input action")
        self.held = desired

    def release(self, actions=None):
        self.held.difference_update(self.held.copy() if actions is None else actions)

    def move_mouse(self, horizontal, vertical):
        self.bridge.x = (self.bridge.x + int(horizontal)) & 0xFFFFFFFF
        self.bridge.y = (self.bridge.y + int(vertical)) & 0xFFFFFFFF

    def turn(self, amount):
        self.move_mouse(amount, 0)

    def look(self, amount):
        self.move_mouse(0, amount)
