"""Tests for focus-aware keyboard and mouse input."""

import struct
import time
import unittest
from unittest.mock import Mock, call, patch

from core.controls import HighJump, Hotkeys, Keys, ViewControl
from games.san_andreas.game import (
    CONTROL_ACTIONS,
    JUMP_ACTION,
    JUMP_CLAMP,
    JUMP_CLAMP_ORIGINAL,
    JUMP_CLAMP_DISABLED,
    CLIMB_VTABLE,
    DROP_ACTION,
    DROP_KEY,
    JUMP_KEY,
    JUMP_SPEED,
    SPRINT_ACTION,
    Game,
    RuntimeChanges,
    bind_jump_key,
    bind_walk_key,
)
from core.runtime import control_request

from tests.fixtures import MemoryFixture


class InputTests(unittest.TestCase):
    def test_vertical_mouse_input_preserves_the_horizontal_axis_and_checks_focus(self):
        with patch("core.controls._user32.SendInput", return_value=1) as send:
            self.keys.look(-12)
            event = send.call_args.args[1]._obj
            self.assertEqual((event.data.mouse.dx, event.data.mouse.dy), (0, -12))
            self.focused.return_value = False
            self.keys.look(12)
            self.assertEqual(send.call_count, 1)

    def setUp(self):
        self.backend = patch("core.controls.keyboard")
        self.keyboard = self.backend.start()
        self.addCleanup(self.backend.stop)
        self.focused = Mock(return_value=True)
        self.keys = Keys({"forward": "w", "back": "s", "walk": "f2"}, self.focused)

    def test_walk_holds_no_modifier_and_repeated_ticks_do_not_repeat_keydown(self):
        self.keys.set({"walk", "forward"})
        self.keys.set({"walk", "forward"})
        self.assertEqual(self.keyboard.press.call_args_list, [call("f2"), call("w")])
        self.keys.release()
        self.assertCountEqual(
            self.keyboard.release.call_args_list, [call("f2"), call("w")]
        )
        self.assertFalse(self.keys.held)

    def test_losing_focus_releases_owned_keys_without_pressing_new_ones(self):
        self.keys.set({"walk", "forward"})
        self.keyboard.reset_mock()
        self.focused.return_value = False
        self.keys.set({"walk", "back"})
        self.keyboard.press.assert_not_called()
        self.assertEqual(self.keyboard.release.call_count, 2)
        self.assertFalse(self.keys.held)
        with patch("core.controls._user32.SendInput") as mouse:
            self.keys.turn(5)
        mouse.assert_not_called()
        self.assertFalse(self.keys.tap("enter"))
        self.keyboard.press.assert_not_called()

    def test_tap_releases_key_even_when_waiting_raises(self):
        with (
            patch("core.controls.time.sleep", side_effect=RuntimeError("interrupted")),
            self.assertRaises(RuntimeError),
        ):
            self.keys.tap("enter")
        self.keyboard.press.assert_called_once_with("enter")
        self.keyboard.release.assert_called_once_with("enter")

    def test_release_attempts_all_owned_keys_if_one_fails(self):
        self.keys.set({"walk", "forward"})
        self.keyboard.release.side_effect = [RuntimeError("failed"), None]
        with self.assertRaises(RuntimeError):
            self.keys.release()
        self.assertEqual(self.keyboard.release.call_count, 2)

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


class ViewControlTests(unittest.TestCase):
    def setUp(self):
        keys_patch = patch("core.controls.Keys")
        self.keys = keys_patch.start().return_value
        self.addCleanup(keys_patch.stop)
        self.view = ViewControl()
        self.state = {
            "ready": True,
            "foreground": True,
            "paused": False,
            "controls_disabled": False,
            "in_vehicle": False,
            "health": 100,
            "player_state": 0,
            "camera_pitch": 0.0,
        }

    def test_numbered_looks_are_finite_and_do_not_move_the_walker(self):
        for direction, sign in (("up", -1), ("down", 1)):
            self.view.cancel()
            self.keys.reset_mock()
            request = control_request(direction, 10)
            for step in range(600):
                self.view.tick(request, self.state, step / 120)
            pixels = [call.args[0] for call in self.keys.look.call_args_list]
            self.assertEqual(sum(pixels), sign * 100)
            self.assertTrue(all(abs(pixel) <= 2 for pixel in pixels))
            self.assertEqual(self.view.pending, 0)
            self.keys.turn.assert_not_called()
            self.keys.set.assert_not_called()

    def test_center_uses_live_pitch_and_replaces_pending_looks(self):
        for pitch in (-0.7, 0.7):
            self.view.tick(control_request("up", 100), self.state, 0)
            self.state["camera_pitch"] = pitch
            self.keys.look.side_effect = lambda pixels: self.state.update(
                camera_pitch=self.state["camera_pitch"] - pixels * 0.005
            )
            request = control_request("center")
            for step in range(960):
                self.view.tick(request, self.state, step / 120)
            self.assertLessEqual(abs(self.state["camera_pitch"]), 0.02)
            self.assertFalse(self.view.centering)
            self.assertEqual(self.view.pending, 0)

    def test_focus_loss_and_saving_cancel_pending_looks(self):
        for saving in (False, True):
            self.state["foreground"] = True
            request = control_request("up", 100)
            self.view.tick(request, self.state, 0)
            self.keys.reset_mock()
            self.state["foreground"] = saving
            self.view.tick(request, self.state, 1, saving=saving)
            self.assertEqual(self.view.pending, 0)
            self.keys.look.assert_not_called()
            self.state["foreground"] = True
            self.view.tick(request, self.state, 2)
            self.keys.look.assert_not_called()

    def test_center_stops_when_pitch_is_missing_or_does_not_respond(self):
        for pitch in (None, 0.7):
            self.state["camera_pitch"] = pitch
            request = control_request("center")
            self.view.tick(request, self.state, 0)
            self.keys.reset_mock()
            self.view.tick(request, self.state, 8)
            self.assertFalse(self.view.centering)
            self.keys.look.assert_not_called()


class JumpBoostTests(unittest.TestCase):
    def setUp(self):
        self.mem = MemoryFixture()
        self.game = Game(self.mem)
        self.ped = 0x20000
        self.state = {
            "ready": True,
            "ped": self.ped,
            "player_info": 0x10000,
            "identity": "01020304",
            "standing": False,
            "in_air": True,
        }
        self.mem.write(0x10000, struct.pack("<I", self.ped))
        self.mem.write(self.ped + 0x20, bytes.fromhex(self.state["identity"]))
        self.mem.write(self.ped + 0x46D, b"\x02")
        self.mem.write(JUMP_CLAMP, JUMP_CLAMP_ORIGINAL)
        self.mem.write(self.ped + 0x44, struct.pack("<3f", 0.1, 0.2, 0.12))
        self.mem.write(self.ped + 0x98, struct.pack("<f", 0.005714286))

    def test_boost_changes_only_vertical_velocity(self):
        before = dict(self.mem.data)
        self.assertTrue(self.game.boost_jump(self.state))
        self.assertEqual(self.mem.data.keys(), before.keys())
        self.assertAlmostEqual(self.mem.unpack(self.ped + 0x4C, "f")[0], JUMP_SPEED)
        for address, value in before.items():
            if not self.ped + 0x4C <= address < self.ped + 0x50:
                self.assertEqual(self.mem.data[address], value)

    def test_grounded_falling_and_already_boosted_players_are_not_boosted(self):
        for speed in (0.0, -0.1, JUMP_SPEED + 0.1, float("nan")):
            with self.subTest(speed=speed):
                self.mem.write(self.ped + 0x4C, struct.pack("<f", speed))
                before = dict(self.mem.data)
                self.assertFalse(self.game.boost_jump(self.state))
                self.assertEqual(self.mem.data, before)
        self.mem.write(self.ped + 0x4C, struct.pack("<f", 0.12))
        self.mem.write(self.ped + 0x46C, b"\x01")
        self.assertFalse(self.game.boost_jump(self.state))

    def test_recycled_player_identity_cannot_receive_boost(self):
        self.mem.write(self.ped + 0x20, b"\x00" * 4)
        before = dict(self.mem.data)
        self.assertFalse(self.game.boost_jump(self.state))
        self.assertEqual(self.mem.data, before)

    def test_jump_binding_is_restored_on_shutdown(self):
        # Jump owns Space, but sprint/swim must retain a printable working key.
        other_action = CONTROL_ACTIONS + 13 * 0x20
        self.mem.write(other_action, struct.pack("<II", 32, 1))
        before = self.mem.read(CONTROL_ACTIONS, 59 * 0x20)
        changes = RuntimeChanges(self.mem)
        bind_jump_key(self.game, changes)
        self.assertEqual(self.mem.u32(CONTROL_ACTIONS + JUMP_ACTION * 0x20), JUMP_KEY)
        self.assertEqual(self.mem.u32(other_action), ord("R"))
        self.assertEqual(self.mem.read(JUMP_CLAMP, 7), JUMP_CLAMP_DISABLED)
        self.assertEqual(self.mem.u32(CONTROL_ACTIONS + DROP_ACTION * 0x20), DROP_KEY)
        changes.restore()
        self.assertEqual(self.mem.read(JUMP_CLAMP, 7), JUMP_CLAMP_ORIGINAL)
        self.assertEqual(self.mem.read(CONTROL_ACTIONS, 59 * 0x20), before)

    def test_jump_preserves_existing_sprint_binding(self):
        address = CONTROL_ACTIONS + SPRINT_ACTION * 32
        self.mem.write(address, struct.pack("<4I", ord("G"), 1, 1056, 0))
        original = self.mem.read(address, 32)
        bind_jump_key(self.game, RuntimeChanges(self.mem))
        self.assertEqual(self.mem.read(address, 32), original)

    def test_sprint_fallback_does_not_steal_another_actions_key(self):
        sprint = CONTROL_ACTIONS + SPRINT_ACTION * 32
        other = CONTROL_ACTIONS + 5 * 32
        self.mem.write(sprint, struct.pack("<4I", 32, 1, 1056, 0))
        self.mem.write(other, struct.pack("<2I", ord("R"), 1))
        changes = RuntimeChanges(self.mem)
        bind_jump_key(self.game, changes)
        self.assertEqual(self.mem.u32(sprint), ord("G"))
        self.assertEqual(self.mem.u32(other), ord("R"))
        before = dict(self.mem.data)
        bind_jump_key(self.game, changes)
        self.assertEqual(self.mem.data, before)

    def test_normal_walk_binding_repairs_previously_unbound_sprint_and_restores_it(self):
        sprint = CONTROL_ACTIONS + SPRINT_ACTION * 32
        self.mem.write(sprint, struct.pack("<4I", 1056, 1, 1056, 0))
        before = self.mem.read(CONTROL_ACTIONS, 59 * 32)
        changes = RuntimeChanges(self.mem)
        bind_walk_key(self.game, changes)
        self.assertEqual(self.mem.u32(sprint), ord("R"))
        self.assertEqual(self.mem.u32(sprint + 4), 1)
        changes.restore()
        self.assertEqual(self.mem.read(CONTROL_ACTIONS, 59 * 32), before)


    def test_unrecognized_clamp_is_rejected_before_any_binding_writes(self):
        self.mem.write(JUMP_CLAMP, b"invalid")
        before = dict(self.mem.data)
        with self.assertRaisesRegex(RuntimeError, "upward-speed clamp"):
            bind_jump_key(self.game, RuntimeChanges(self.mem))
        self.assertEqual(self.mem.data, before)

    def test_drop_requires_a_live_matching_player_and_active_climb(self):
        intelligence, root_task, climb = 0x30000, 0x40000, 0x50000
        complex_vtable = 0x860000
        self.mem.write(self.ped + 0x47C, struct.pack("<I", intelligence))
        self.mem.write(intelligence + 4, struct.pack("<5I", root_task, 0, 0, 0, 0))
        self.mem.write(root_task, struct.pack("<3I", complex_vtable, 0, climb))
        self.mem.write(complex_vtable + 12, struct.pack("<I", 0x610000))
        self.mem.write(CLIMB_VTABLE + 12, struct.pack("<I", 0x620000))
        self.mem.write(climb, struct.pack("<I", CLIMB_VTABLE))
        self.assertTrue(self.game.can_drop(self.state))
        self.assertFalse(self.game.can_drop(dict(self.state, in_vehicle=True)))
        self.assertFalse(self.game.can_drop(dict(self.state, identity="stale")))
        # A simple task has no child pointer, even if +8 resembles one.
        self.mem.write(root_task, struct.pack("<I", 0x861000))
        self.mem.write(0x861000 + 12, struct.pack("<I", 0x620000))
        self.assertFalse(self.game.can_drop(self.state))
        # Cyclic complex-task chains cannot hang the controller.
        self.mem.write(root_task, struct.pack("<3I", complex_vtable, 0, root_task))
        self.assertFalse(self.game.can_drop(self.state))


class HighJumpTests(unittest.TestCase):
    def setUp(self):
        self.keys_patch = patch("core.controls.Keys")
        self.key_factory = self.keys_patch.start()
        self.keys = self.key_factory.return_value
        self.addCleanup(self.keys_patch.stop)
        self.bind = Mock()
        self.game = Mock()
        self.jump = HighJump(self.game, Mock(), binder=self.bind)
        self.state = {
            "ready": True,
            "foreground": True,
            "paused": False,
            "controls_disabled": False,
            "in_vehicle": False,
            "health": 100,
            "player_state": 0,
            "standing": True,
            "in_air": False,
            "ped": 0x20000,
            "identity": "01020304",
            "vertical_speed": 0.0,
        }
        self.request = {"id": "jump-1", "issued": time.time()}

    def test_jump_uses_space_instead_of_hardcoded_replay_keys(self):
        self.key_factory.assert_called_once_with(
            {"jump": "space", "drop": "f"},
            focused=self.game.mem.foreground,
        )
        self.assertEqual(JUMP_KEY, 32)

    def test_normal_jump_is_boosted_once_after_takeoff(self):
        self.jump.tick(self.request, self.state, 10)
        self.keys.set.assert_called_once_with({"jump"})
        self.game.boost_jump.assert_not_called()
        self.jump.tick(self.request, self.state, 10.15)
        self.keys.release.assert_called()
        self.state.update(standing=False, in_air=True, vertical_speed=0.12)
        self.jump.tick(self.request, self.state, 10.2)
        self.jump.tick(self.request, self.state, 10.3)
        self.game.boost_jump.assert_called_once_with(self.state)
        self.assertIsNone(self.jump.player)

    def test_drop_releases_native_ledge_without_applying_jump_boost(self):
        self.state.update(standing=False, in_air=True, vertical_speed=0)
        self.game.can_drop.return_value = True
        request = dict(self.request, direction="drop")
        self.jump.tick(request, self.state, 10)
        self.keys.set.assert_called_once_with({"drop"})
        self.jump.tick(request, self.state, 10.2)
        self.assertIsNone(self.jump.player)
        self.game.boost_jump.assert_not_called()

    def test_drop_does_not_send_vehicle_entry_input_outside_a_climb(self):
        self.game.can_drop.return_value = False
        self.jump.tick(dict(self.request, direction="drop"), self.state, 10)
        self.keys.set.assert_not_called()
        self.bind.assert_not_called()

    def test_background_and_saving_do_not_send_drop_input(self):
        self.game.can_drop.return_value = True
        self.state['foreground'] = False
        request = dict(self.request, direction="drop")
        self.jump.tick(request, self.state, 10)
        self.keys.set.assert_not_called()
        self.state['foreground'] = True
        self.jump.tick(request, self.state, 10.1, saving=True)
        self.keys.set.assert_not_called()
        self.jump.tick(request, self.state, 10.2)
        self.keys.set.assert_called_once_with({"drop"})

    def test_airborne_requests_do_not_stack_boosts(self):
        self.state.update(standing=False, in_air=True, vertical_speed=0.3)
        self.jump.tick(self.request, self.state, 10)
        self.keys.set.assert_not_called()
        self.game.boost_jump.assert_not_called()

    def test_background_request_waits_for_focus(self):
        self.state["foreground"] = False
        self.jump.tick(self.request, self.state, 10)
        self.keys.set.assert_not_called()
        self.state["foreground"] = True
        self.jump.tick(self.request, self.state, 11)
        self.keys.set.assert_called_once_with({"jump"})

    def test_focus_loss_or_saving_cancels_pending_takeoff(self):
        for saving in (False, True):
            with self.subTest(saving=saving):
                self.jump.last_request = None
                self.state["foreground"] = True
                self.jump.tick(self.request, self.state, 10)
                self.state["foreground"] = saving
                self.jump.tick(self.request, self.state, 10.1, saving=saving)
                self.assertIsNone(self.jump.player)
                self.keys.release.assert_called()
        self.game.boost_jump.assert_not_called()

    def test_expired_request_and_failed_takeoff_never_boost(self):
        expired = dict(self.request, id="old", issued=time.time() - 30)
        self.jump.tick(expired, self.state, 10)
        self.keys.set.assert_not_called()
        self.jump.tick(self.request, self.state, 10)
        with self.assertLogs("core.controls", level="WARNING"):
            self.jump.tick(self.request, self.state, 12)
        self.assertIsNone(self.jump.player)
        self.game.boost_jump.assert_not_called()
