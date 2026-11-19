"""Background controls must remain private and retain gameplay safety gates."""
import configparser
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.background import NativeInput, WINDOW_CONFIG, configure_window
from core.controls import can_control
from core.controller import Controller
from tests.fixtures import MemoryFixture


class BackgroundTests(unittest.TestCase):
    def test_background_permission_does_not_bypass_pause_death_or_cutscenes(self):
        state = dict(ready=True, foreground=False, background_control=True,
                     paused=False, controls_disabled=False, in_vehicle=False,
                     health=100, player_state=0)
        self.assertTrue(can_control(state))
        for key, value in (("paused", True), ("controls_disabled", True),
                           ("in_vehicle", True), ("cutscene", True),
                           ("health", 0), ("player_state", 1), ("background_control", False)):
            with self.subTest(key=key):
                self.assertFalse(can_control(dict(state, **{key: value})))

    def test_config_keeps_geometry_and_is_not_rewritten_on_every_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / WINDOW_CONFIG
            path.parent.mkdir()
            path.write_text("[MAIN]\nWindowMode=0\n[WINDOW]\nWidth=1600\nPositionX=2300\n")
            configure_window(folder)
            config = configparser.ConfigParser()
            config.read(path)
            self.assertEqual(config.getint("MAIN", "WindowMode"), 2)
            self.assertFalse(config.getboolean("MAIN", "PauseOnFocusLoss"))
            self.assertEqual(config.getint("WINDOW", "PositionX"), 2300)
            with patch.object(Path, "write_text") as write:
                configure_window(folder)
            write.assert_not_called()

    def bridge(self):
        memory = MemoryFixture()
        memory.profile = SimpleNamespace(id="gta_iii")
        memory.module = lambda name: {"base": 0x10000000, "size": 0x10000, "path": Path("pinned.asi")}
        memory.alive = lambda: True
        memory.write(0x10002000, struct.pack("<3I", 1, 1, 3))
        with patch("core.background.read_json", return_value={"sha256": "pinned", "state_rva": 0x2000}), patch("core.background.sha256", return_value="pinned"):
            return NativeInput(memory), memory

    def test_channels_share_private_input_without_keyboard_or_mouse_events(self):
        bridge, memory = self.bridge()
        walk = bridge.keys({"forward": "w", "back": "s"})
        jump = bridge.keys({"jump": "space"})
        with patch("core.controls.keyboard.press") as press, patch("core.controls._user32.SendInput") as mouse:
            walk.set({"forward"})
            jump.set({"jump"})
            walk.turn(-10)
            walk.look(15)
            bridge.publish()
            self.assertEqual(memory.unpack(bridge.address + 20, "3I"), (5, 0xFFFFFFF6, 15))
            jump.release()
            bridge.publish()
            self.assertEqual(memory.u32(bridge.address + 20), 1)
            self.assertTrue(memory.background_control)
            bridge.close()
            self.assertFalse(memory.background_control)
            self.assertEqual(memory.read(bridge.address + 12, 20), bytes(20))
            press.assert_not_called()
            mouse.assert_not_called()

    def test_background_hotkeys_do_not_handle_another_applications_function_keys(self):
        controller = Controller.__new__(Controller)
        controller.hotkeys = Mock()
        controller.hotkeys.pressed.return_value = ["f9", "f12"]
        controller.settings = {"quit": False}
        controller.stop = Mock()
        with patch("core.controller.atomic_json") as write:
            controller.handle_hotkeys({"foreground": False})
        self.assertFalse(controller.settings["quit"])
        controller.stop.assert_not_called()
        write.assert_not_called()

    def test_uninstalled_native_hook_never_falls_back_to_desktop_input(self):
        bridge, memory = self.bridge()
        memory.write(bridge.address + 4, struct.pack("<I", 2))
        with patch("core.background.read_json", return_value={"sha256": "pinned", "state_rva": 0x2000}), patch("core.background.sha256", return_value="pinned"):
            with self.assertRaisesRegex(RuntimeError, "refusing desktop input"):
                NativeInput(memory)
