"""Focus-aware keyboard input, held-key tracking, and relative mouse movement."""
import ctypes as c
from ctypes import wintypes as w
import time

import keyboard


class MouseInput(c.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", c.c_size_t)]


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

    def tap(self, key, duration=.08):
        if not self.focused():
            return False
        keyboard.press(key)
        try:
            time.sleep(duration)
        finally:
            keyboard.release(key)
        return True

    def turn(self, amount):
        if not amount or not self.focused():
            return
        event = Input(type=0, data=InputData(mouse=MouseInput(int(amount), 0, 0, 1, 0, 0)))
        if _user32.SendInput(1, c.byref(event), c.sizeof(Input)) != 1:
            raise RuntimeError("Windows rejected mouse input")

    def release(self, actions=None):
        failure = None
        selected = self.held if actions is None else self.held & set(actions)
        for action in sorted(selected):
            try:
                keyboard.release(self.bindings[action])
                self.held.remove(action)
            except Exception as error:
                failure = failure or error
        if failure is not None:
            raise failure
