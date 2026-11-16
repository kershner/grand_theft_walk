"""Focus-aware keyboard input, held-key tracking, and relative mouse movement."""

import ctypes as c
import logging
import time
from ctypes import wintypes as w

import keyboard

from gtw.navigation import SmoothTurn
from gtw.runtime import request_expired

logger = logging.getLogger(__name__)
MOUSE_UNITS_PER_STEP = 10


def can_control(state):
    return (
        state.get("ready")
        and state["foreground"]
        and not state["paused"]
        and not state["controls_disabled"]
        and not state["in_vehicle"]
        and not state.get("cutscene", False)
        and state["health"] > 0
        and state["player_state"] == 0
    )


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
    def __init__(self, focused=None):
        self.keys = Keys(focused=focused)
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
            self.keys.look(pixels)


class HighJump:
    def __init__(self, game, bindings, binder):
        self.game = game
        self.bindings = bindings
        self.binder = binder
        self.keys = Keys({"jump": "space"}, focused=game.mem.foreground)
        self.last_request = None
        self.player = None
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
                if (
                    state["in_air"]
                    and not state["standing"]
                    and state["vertical_speed"] > 0
                ):
                    if self.game.boost_jump(state):
                        logger.info("High jump boosted")
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
        if self.player is not None or not state["standing"] or state["in_air"]:
            logger.debug("Jump ignored: already jumping or airborne")
            return
        self.binder(self.game, self.bindings)
        self.keys.set({"jump"})
        self.player = (state["ped"], state["identity"])
        self.release_at = now + 0.12
        self.deadline = now + 2.0
        logger.debug("Jump requested; waiting for native takeoff")
