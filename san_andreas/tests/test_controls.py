import unittest
from unittest.mock import Mock, call, patch

from controls import Keys, Hotkeys, key_down


class InputTests(unittest.TestCase):
    def setUp(self):
        self.backend = patch("controls.keyboard")
        self.keyboard = self.backend.start()
        self.addCleanup(self.backend.stop)
        self.focused = Mock(return_value=True)
        self.keys = Keys({"forward": "w", "back": "s", "walk": "f2"}, self.focused)

    def test_walk_holds_no_modifier_and_repeated_ticks_do_not_repeat_keydown(self):
        self.keys.set({"walk", "forward"})
        self.keys.set({"walk", "forward"})
        self.assertEqual(self.keyboard.press.call_args_list, [call("f2"), call("w")])
        self.keys.release()
        self.assertCountEqual(self.keyboard.release.call_args_list, [call("f2"), call("w")])
        self.assertFalse(self.keys.held)

    def test_losing_focus_releases_owned_keys_without_pressing_new_ones(self):
        self.keys.set({"walk", "forward"})
        self.keyboard.reset_mock()
        self.focused.return_value = False
        self.keys.set({"walk", "back"})
        self.keyboard.press.assert_not_called()
        self.assertEqual(self.keyboard.release.call_count, 2)
        self.assertFalse(self.keys.held)
        with patch("controls._user32.SendInput") as mouse:
            self.keys.turn(5)
        mouse.assert_not_called()

    def test_tap_releases_key_even_when_waiting_raises(self):
        with patch("controls.time.sleep", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                self.keys.tap("enter")
        self.keyboard.press.assert_called_once_with("enter")
        self.keyboard.release.assert_called_once_with("enter")

    def test_release_attempts_all_owned_keys_if_one_fails(self):
        self.keys.set({"walk", "forward"})
        self.keyboard.release.side_effect = [RuntimeError("failed"), None]
        with self.assertRaises(RuntimeError):
            self.keys.release()
        self.assertEqual(self.keyboard.release.call_count, 2)

    def test_hotkey_detection_uses_keyboard_library(self):
        self.keyboard.is_pressed.return_value = True
        self.assertTrue(key_down("f11"))
        self.keyboard.is_pressed.assert_called_once_with("f11")

    def test_held_hotkeys_fire_once_and_rearm_after_release(self):
        hotkeys = Hotkeys(("f11", "f12"))
        self.keyboard.is_pressed.side_effect = lambda key: key == "f11"
        self.assertEqual(hotkeys.pressed(), ["f11"])
        self.assertEqual(hotkeys.pressed(), [])
        self.keyboard.is_pressed.side_effect = lambda key: False
        self.assertEqual(hotkeys.pressed(), [])
        self.keyboard.is_pressed.side_effect = lambda key: key == "f11"
        self.assertEqual(hotkeys.pressed(), ["f11"])

    def test_changing_movement_releases_only_keys_no_longer_needed(self):
        self.keys.set({"walk", "forward"})
        self.keyboard.reset_mock()
        self.keys.set({"walk", "back"})
        self.keyboard.release.assert_called_once_with("w")
        self.keyboard.press.assert_called_once_with("s")
        self.assertEqual(self.keys.held, {"walk", "back"})
