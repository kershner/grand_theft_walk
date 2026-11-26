"""Tests for focus-aware keyboard and mouse input."""

from tests.fixtures import StateTestCase
import configparser
import math
import struct
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from core.controller import Controller, Walking
from core.controls import (
    HighJump,
    Hotkeys,
    Keys,
    NativeInput,
    Respawn,
    ViewControl,
    WINDOW_CONFIG,
    can_control,
    configure_window,
)
from core.files import write_state
from tests.fixtures import bridge_exports
from core.navigation import angle_delta, heading_to
from core.runtime import control_request
from games import Capabilities
from games.gta_iii import game as iii
from games.san_andreas import game as sa
from games.san_andreas.game import (
    CLIMB_VTABLE,
    CONTROL_ACTIONS,
    DROP_ACTION,
    DROP_KEY,
    Game,
    JUMP_ACTION,
    JUMP_CLAMP,
    JUMP_CLAMP_DISABLED,
    JUMP_CLAMP_ORIGINAL,
    JUMP_KEY,
    JUMP_SPEED,
    RuntimeChanges,
    SPRINT_ACTION,
    apply_protection,
    bind_jump_key,
    bind_walk_key,
)
from games.vice_city import game as vc
from tests.fixtures import MemoryFixture


class InputTests(StateTestCase):
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


class ViewControlTests(StateTestCase):
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


class JumpBoostTests(StateTestCase):
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

    def test_pull_up_requests_only_a_verified_hanging_task_and_preserves_other_flags(self):
        climb = 0x50000
        self.game.climb_task = Mock(return_value=climb)
        flags = bytes.fromhex('0000000000010100')
        self.mem.write(climb + 8, flags)
        self.assertTrue(self.game.pull_up(self.state))
        self.assertEqual(self.mem.read(climb + 8, 8), bytes.fromhex('0000000100010100'))
        self.assertFalse(self.game.pull_up(dict(self.state, identity='stale')))
        for invalid in ('0100000000010100', '0000000001010100',
                        '0000000000020100', '0000000000010200'):
            self.mem.write(climb + 8, bytes.fromhex(invalid))
            self.assertFalse(self.game.pull_up(self.state))
            self.assertEqual(self.mem.read(climb + 8, 8), bytes.fromhex(invalid))

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


class HighJumpTests(StateTestCase):
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
        self.assertEqual(self.jump.result, 'accepted')
        self.keys.set.assert_called_once_with({"jump"})
        self.game.boost_jump.assert_not_called()
        self.jump.tick(self.request, self.state, 10.15)
        self.keys.release.assert_called()
        self.state.update(standing=False, in_air=True, vertical_speed=0.12)
        self.jump.tick(self.request, self.state, 10.2)
        self.jump.tick(self.request, self.state, 10.3)
        self.game.boost_jump.assert_called_once_with(self.state)
        self.assertEqual(self.jump.result, 'airborne')
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
        self.assertEqual(self.jump.result, 'rejected')
        self.keys.set.assert_not_called()
        self.game.boost_jump.assert_not_called()

    def test_stationary_ledge_hang_gets_pull_up_input_without_high_jump_boost(self):
        self.game.can_drop.return_value = True
        self.state.update(standing=False, in_air=True, vertical_speed=0,
                          position=dict(x=0, y=0, z=10))
        self.jump.tick(None, self.state, 0, auto_climb=True)
        self.keys.set.assert_not_called()
        self.jump.tick(None, self.state, .61, auto_climb=True)
        self.keys.set.assert_called_once_with({'jump'})
        self.assertEqual(self.jump.result, 'climbing')
        self.state['vertical_speed'] = .1
        self.jump.tick(None, self.state, .8, auto_climb=True)
        self.jump.tick(None, self.state, 1, auto_climb=True)
        self.keys.release.assert_called()
        self.game.boost_jump.assert_not_called()
        self.game.pull_up.assert_not_called()

    def test_persistent_hang_uses_bounded_native_pull_up_retries(self):
        self.game.can_drop.return_value = self.game.pull_up.return_value = True
        self.state.update(standing=False, in_air=True, vertical_speed=0)
        for now in (0, .61, 1, 2.2, 3.8, 6, 10):
            self.jump.tick(None, self.state, now, auto_climb=True)
        self.keys.set.assert_called_once_with({'jump'})
        self.assertEqual(self.game.pull_up.call_count, 2)
        self.game.boost_jump.assert_not_called()
        self.assertEqual(self.jump.climb_attempts, 3)

    def test_normal_airtime_and_progressing_climbs_do_not_trigger_pull_up(self):
        self.game.can_drop.return_value = False
        self.state.update(standing=False, in_air=True, vertical_speed=0)
        for now in (0, 1, 2):
            self.jump.tick(None, self.state, now, auto_climb=True)
        self.game.can_drop.return_value = True
        for now in (3, 3.5, 4, 4.5):
            self.state['position'] = dict(x=0, y=0, z=now)
            self.jump.tick(None, self.state, now, auto_climb=True)
        self.keys.set.assert_not_called()
        self.game.pull_up.assert_not_called()

    def test_auto_climb_respects_pause_saving_and_manual_drop(self):
        self.game.can_drop.return_value = True
        self.state.update(standing=False, in_air=True, vertical_speed=0)
        self.jump.tick(None, self.state, 0, auto_climb=True)
        self.jump.tick(None, self.state, 1, saving=True, auto_climb=True)
        self.jump.tick(None, self.state, 2, auto_climb=False)
        self.state['paused'] = True
        self.jump.tick(None, self.state, 3, auto_climb=True)
        self.keys.set.assert_not_called()
        self.state['paused'] = False
        self.jump.tick(None, self.state, 4, auto_climb=True)
        self.jump.tick(dict(self.request, direction='drop'), self.state, 5, auto_climb=True)
        self.keys.set.assert_called_once_with({'drop'})
        self.game.pull_up.assert_not_called()

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
        self.assertEqual(self.jump.result, 'failed')
        self.assertIsNone(self.jump.player)
        self.game.boost_jump.assert_not_called()


class BackgroundTests(StateTestCase):
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
        with patch("core.controls.read_json", return_value={"sha256": "pinned", "state_rva": 0x2000}), patch("core.controls.sha256", return_value="pinned"):
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

    def test_rebuilt_bridge_uses_exports_and_checks_protocol_before_writing(self):
        _, memory = self.bridge()
        module = memory.module("GTW.BackgroundInput.asi")
        address = module["base"] + 0x3000
        bridge_exports(memory, module["base"], {"GTWInput": 0x3000})
        memory.write(address, struct.pack("<3I", 1, 1, 3))
        with patch("core.controls.read_json", return_value={"sha256": "pinned", "state_rva": 0x2000}), \
                patch("core.controls.sha256", return_value="rebuilt"):
            bridge = NativeInput(memory)
            self.assertEqual(bridge.address, address)
            memory.write(address, struct.pack("<I", 2))
            with patch.object(memory, "write") as write:
                with self.assertRaisesRegex(RuntimeError, "refusing desktop input"):
                    NativeInput(memory)
                write.assert_not_called()

    def test_background_hotkeys_do_not_handle_another_applications_function_keys(self):
        controller = Controller.__new__(Controller)
        controller.hotkeys = Mock()
        controller.hotkeys.pressed.return_value = ["f9", "f12"]
        controller.settings = {"quit": False}
        controller.stop = Mock()
        with patch("core.controller.write_state") as write:
            controller.handle_hotkeys({"foreground": False})
        self.assertFalse(controller.settings["quit"])
        controller.stop.assert_not_called()
        write.assert_not_called()

    def test_uninstalled_native_hook_never_falls_back_to_desktop_input(self):
        bridge, memory = self.bridge()
        memory.write(bridge.address + 4, struct.pack("<I", 2))
        with patch("core.controls.read_json", return_value={"sha256": "pinned", "state_rva": 0x2000}), patch("core.controls.sha256", return_value="pinned"):
            with self.assertRaisesRegex(RuntimeError, "refusing desktop input"):
                NativeInput(memory)


class RespawnTests(StateTestCase):
    def setUp(self):
        self.game, self.protection = Mock(), Mock()
        self.protection.restore.return_value = []
        self.game.kill_player.return_value = True
        self.respawn = Respawn(self.game, self.protection, 'session')
        self.request = dict(control_request(), session='session')
        self.state = dict(ready=True, health=100, player_state=0,
                          foreground=True, paused=False, cutscene=False)

    def test_restores_protection_then_kills_once_and_waits_for_native_respawn(self):
        calls = Mock()
        calls.attach_mock(self.protection, 'protection')
        calls.attach_mock(self.game, 'game')
        self.assertTrue(self.respawn.tick(self.request, self.state, 10))
        self.assertEqual([str(call).split('(')[0] for call in calls.mock_calls],
                         ['call.protection.restore', 'call.game.kill_player'])
        self.assertTrue(self.respawn.tick(self.request, dict(self.state, health=0), 11))
        self.assertTrue(self.respawn.tick(self.request, {'ready': False}, 12))
        self.assertFalse(self.respawn.tick(self.request, self.state, 15))
        self.assertFalse(self.respawn.tick(self.request, self.state, 16))
        self.game.kill_player.assert_called_once()

    def test_waits_for_focus_pause_cutscene_and_saving_without_consuming_vote(self):
        for update in ({'foreground': False}, {'paused': True}, {'cutscene': True}, {'ready': False}):
            self.assertFalse(self.respawn.tick(self.request, dict(self.state, **update), 10))
        self.assertFalse(self.respawn.tick(self.request, self.state, 10, saving=True))
        self.game.kill_player.assert_not_called()
        self.assertTrue(self.respawn.tick(self.request, self.state, 11))

    def test_rejects_expired_other_session_and_already_dead_requests(self):
        for request in (dict(self.request, session='old'), dict(self.request, issued=time.time()-11)):
            self.assertFalse(self.respawn.tick(request, self.state, 10))
        self.assertFalse(self.respawn.tick(self.request, dict(self.state, health=0), 10))
        self.assertFalse(self.respawn.tick(self.request, self.state, 12))
        self.game.kill_player.assert_not_called()

    def test_does_not_kill_if_protection_could_not_be_restored(self):
        self.protection.restore.return_value = ['failed']
        with self.assertLogs('core.controls', level='ERROR'):
            self.assertFalse(self.respawn.tick(self.request, self.state, 10))
        self.game.kill_player.assert_not_called()

    def test_slow_or_failed_death_does_not_leave_controls_suspended_forever(self):
        self.assertTrue(self.respawn.tick(self.request, self.state, 10))
        with self.assertLogs('core.controls', level='WARNING'):
            self.assertFalse(self.respawn.tick(self.request, self.state, 41))
        self.assertFalse(self.respawn.tick(self.request, self.state, 42))
        self.game.kill_player.assert_called_once()


class BackendRespawnTests(StateTestCase):
    def test_backends_only_clear_current_living_players_health(self):
        for backend, health_offset, state_offset in ((sa, 0x540, 0xDC), (vc, 0x354, 0xCC)):
            with self.subTest(game=backend.__name__):
                mem = MemoryFixture()
                game = backend.Game(mem) if backend is sa else backend.Game(mem, zones=[])
                ped = 0x200000
                mem.write(backend.PLAYER_INFO, struct.pack('<I', ped))
                mem.write(ped+health_offset, struct.pack('<f', 100))
                if backend is sa:
                    mem.write(ped+0x20, bytes.fromhex('01020304'))
                    identity = '01020304'
                else:
                    pool, bitmap = 0x100000, 0x300000
                    mem.write(vc.PED_POOL, struct.pack('<I', pool))
                    mem.write(pool, struct.pack('<3I', ped, bitmap, 1))
                    mem.write(bitmap, b'\x01')
                    identity = game.identity(ped)
                state = dict(ready=True, ped=ped, player_info=backend.PLAYER_INFO, identity=identity)
                for invalid in (dict(state, ready=False), dict(state, identity='stale')):
                    before = dict(mem.data)
                    self.assertFalse(game.kill_player(invalid))
                    self.assertEqual(mem.data, before)
                mem.write(backend.PLAYER_INFO+state_offset, b'\x01')
                self.assertFalse(game.kill_player(state))
                mem.write(backend.PLAYER_INFO+state_offset, b'\x00')
                before = dict(mem.data)
                self.assertTrue(game.kill_player(state))
                self.assertEqual(mem.unpack(ped+health_offset, 'f')[0], 0)
                for address, value in before.items():
                    if not ped+health_offset <= address < ped+health_offset+4:
                        self.assertEqual(mem.data[address], value)
                self.assertFalse(game.kill_player(state))


class ControllerRespawnTests(StateTestCase):
    def test_suspends_healing_autosave_and_input_until_player_respawns(self):
        with tempfile.TemporaryDirectory() as folder:
            args = SimpleNamespace(state_dir=Path(folder), walk=True, mode='free')
            memory, backend = Mock(), Mock()
            memory.profile.id = 'san_andreas'
            backend.CAPABILITIES = sa.CAPABILITIES
            backend.WALK_KEYS = None
            backend.TURN_MULTIPLIER = 1
            backend.MISSION_DEFAULTS = None
            backend.RuntimeChanges.return_value.restore.return_value = []
            game = backend.Game.return_value
            state = dict(ready=True, foreground=True, paused=False, controls_disabled=False,
                         in_vehicle=False, health=100, player_state=0, standing=True,
                         in_air=False, interior=0, position={'x':0,'y':0,'z':0})
            game.snapshot.return_value = state
            game.kill_player.return_value = True
            autosave = Mock()
            autosave.tick.return_value = False
            with patch('core.controller.Keys'), patch('core.controller.HighJump'), \
                    patch('core.controller.ViewControl'), patch('core.controller.Hotkeys') as hotkeys:
                hotkeys.return_value.pressed.return_value = []
                controller = Controller(memory, args, backend, lambda mem: None, lambda *a: autosave)
                controller.write_report = Mock()
                controller.update_game = Mock()
                controller.movement = Mock()
                controller.missions = Mock()
                controller.missions.tick.return_value = False
                controller.movement.navigator = None
                controller.movement.keys.held = set()
                req = dict(control_request(), session=controller.session)
                write_state(controller.control_path, dict(controller.settings, respawn=req))
                self.assertTrue(controller.tick(10))
                self.assertEqual(autosave.tick.call_args.args[2], False)
                controller.update_game.assert_not_called()
                controller.jump.cancel.assert_called()
                game.snapshot.return_value = dict(state, health=0)
                self.assertTrue(controller.tick(11))
                controller.update_game.assert_not_called()
                game.snapshot.return_value = state
                self.assertTrue(controller.tick(15))
                controller.update_game.assert_called_once()
                self.assertTrue(controller.settings['walking'])
                game.kill_player.assert_called_once()


class WaterRecoveryTests(StateTestCase):
    def controller(self):
        controller = Controller.__new__(Controller)
        controller.settings = {'protection': True, 'peaceful': False, 'unlock_map': False, 'first_person': False}
        controller.capabilities = Capabilities(protection=True)
        controller.game = Mock()
        controller.backend = Mock()
        controller.movement = Mock()
        controller.camera = controller.peaceful = controller.world = controller.display = None
        controller.protection = Mock(entries={'owned': True})
        return controller

    def test_protection_yields_to_water_and_death_then_resumes_after_respawn(self):
        controller = self.controller()
        dry = {'health': 100, 'player_state': 0, 'in_water': False}
        controller.update_game(dry, 0, False)
        controller.backend.apply_protection.assert_called_once()
        for state in (dict(dry, in_water=True), dict(dry, health=0), dict(dry, player_state=1)):
            controller.backend.apply_protection.reset_mock()
            controller.update_game(state, 1, False)
            controller.backend.apply_protection.assert_not_called()
        self.assertEqual(controller.protection.restore.call_count, 3)
        controller.update_game(dry, 2, False)
        controller.backend.apply_protection.assert_called_once()

    def test_walking_releases_input_during_drowning_and_resumes_after_respawn(self):
        dry = {'ready': True, 'foreground': True, 'paused': False, 'controls_disabled': False,
               'in_vehicle': False, 'health': 100, 'player_state': 0, 'ped_state': 1,
               'position': {'x': 0, 'y': 0, 'z': 0}, 'heading': 0, 'camera_heading': 0}
        with patch('core.controller.Keys') as keys:
            keys.return_value.held = set()
            walking = Walking()
            self.assertFalse(can_control(dict(dry, drowning=True)))
            walking.tick(dict(dry, drowning=True), True)
            keys.return_value.release.assert_called()
            keys.return_value.set.reset_mock()
            walking.tick(dry, True)
            keys.return_value.set.assert_called()

    def test_sa_native_submerged_and_touching_water_flags_skip_protection(self):
        for address, mask in ((0x41, 1), (0x43, 8)):
            with self.subTest(address=address):
                memory = MemoryFixture()
                memory.pid = 42
                memory.foreground = lambda: True
                ped = 0x20000
                memory.write(0xB7CD98, struct.pack('<I', ped))
                memory.write(ped + address, bytes([mask]))
                game = Game(memory)
                state = game.snapshot()
                self.assertTrue(state['in_water'])
                changes = Mock()
                before = dict(memory.data)
                apply_protection(game, changes, state)
                changes.restore.assert_called_once()
                self.assertEqual(memory.data, before)


class ClassicViewTests(StateTestCase):
    def test_classic_games_up_down_and_center_follow_the_native_vertical_direction(self):
        for backend in (iii, vc):
            with self.subTest(game=backend.__name__):
                self.check_vertical_direction(backend)

    def check_vertical_direction(self, backend):
        with patch('core.controls.Keys'):
            view = ViewControl(look_multiplier=backend.LOOK_MULTIPLIER)
        state = dict(ready=True, foreground=True, paused=False, controls_disabled=False,
                     in_vehicle=False, health=100, player_state=0, camera_pitch=0.)

        def look(amount):
            # The classic games' positive mouse Y raises the view; the shared default
            # has the opposite sign. Exercise the actual calibrated output.
            state['camera_pitch'] += math.radians(amount * (.2 if backend is vc else .1))

        view.keys.look.side_effect = look
        for direction, expected in (('up', 1), ('down', -1)):
            with self.subTest(direction=direction):
                state['camera_pitch'] = 0
                view.cancel()
                request = control_request(direction, 10)
                for step in range(240):
                    view.tick(request, state, step / 120)
                self.assertGreater(state['camera_pitch'] * expected, 0)
        for pitch in (-.6, .6):
            with self.subTest(pitch=pitch):
                state['camera_pitch'] = pitch
                request = control_request('center')
                for step in range(720):
                    view.tick(request, state, step / 120)
                self.assertLessEqual(abs(state['camera_pitch']), .02)
                self.assertFalse(view.centering)


class ClassicTurningTests(StateTestCase):
    def test_planner_retries_keep_walking_until_a_blockage_is_confirmed(self):
        for backend in (iii, vc, sa):
            with self.subTest(game=backend.__name__):
                self.configure(backend)
                walk = self.walking
                walk.set_mode('explore')
                walk.navigator.waypoint = Mock(return_value=(0, 100, 0))
                walk.terrain = SimpleNamespace(enabled=True, blocked=False, temporary=False,
                    route=Mock(return_value=None), reset=Mock(), status='pending')
                walk.begin_free_walk(self.state, 0)
                for now in (0, 9.9, 10, 10.5, 11.1, 21.2):
                    self.state['position'] = dict(x=0, y=now, z=0)
                    self.tick(now, True, 'explore')
                    self.keys.set.assert_called_with({'forward'})
                walk.terrain.reset.assert_not_called()
                walk.terrain.route.return_value = (0, 100, 0)
                self.tick(21.3, True, 'explore')
                self.assertEqual(walk.free_walk_until, 0)
                self.keys.set.assert_called_with({'forward'})
                walk.terrain.route.return_value = None
                walk.terrain.blocked = True
                self.tick(21.4, True, 'explore')
                self.keys.set.assert_called_with(set())
                self.assertIsNotNone(walk.recovery_target)

    def test_circle_escape_survives_sensor_timeout_and_curves_forward_in_all_games(self):
        for backend in (iii, vc, sa):
            with self.subTest(game=backend.__name__):
                self.configure(backend)
                walk = self.walking
                walk.set_mode('explore')
                navigator = walk.navigator
                navigator.open_goal = (-100, 0, 0)  # Stale goal pointed sideways.
                navigator.local_origin, navigator.local_target = (0, 0, 0), (-10, 0, 0)
                navigator.rng.choice = Mock(return_value=1)
                for frame in range(81):
                    angle = frame * math.tau / 80
                    navigator.motion_history.append((frame / 4,
                        (4 * math.sin(angle), 4 * math.cos(angle) - 4, 0)))
                walk.terrain = SimpleNamespace(enabled=True, blocked=False, temporary=False,
                    route=Mock(return_value=None), reset=Mock(), status='pending')
                self.tick(20, True, 'explore')
                self.assertEqual(navigator.loop_escapes, 1)
                self.assertAlmostEqual(abs(angle_delta(navigator.course, 0)), math.pi / 2)
                goal = navigator.open_goal
                self.assertAlmostEqual(abs(goal[0]), 100)
                self.assertAlmostEqual(goal[1], 0)
                self.tick(21.1, True, 'explore')
                self.assertEqual(navigator.open_goal, goal)
                self.keys.reset_mock()
                for frame in range(1, 361):
                    self.tick(21.1 + frame / 120, True, 'explore')
                    self.keys.set.assert_called_with({'forward'})
                    facing = self.state['camera_heading']
                    self.assertGreaterEqual(math.cos(facing), 0)
                    self.state['position']['x'] -= math.sin(facing) / 60
                    self.state['position']['y'] += math.cos(facing) / 60
                self.assertGreater(self.state['position']['y'], 3)
                self.assertGreater(abs(self.state['position']['x']), .3)
                self.assertLessEqual(abs(walk.smooth_turn.velocity), 30 * walk.turn_multiplier)
                self.assertEqual(navigator.open_goal, goal)
                self.assertEqual(walk.recoveries, 0)

    def configure(self, backend):
        self.backend = backend
        with patch("core.controller.Keys"):
            self.walking = Walking(bindings={"forward": "w", "back": "s"},
                                   turn_multiplier=getattr(backend, "TURN_MULTIPLIER", 1))
        self.keys = self.walking.keys
        self.state = dict(ready=True, foreground=False, background_control=True,
                          paused=False, controls_disabled=False, in_vehicle=False,
                          health=100, player_state=0, ped_state=0, interior=0,
                          position=dict(x=0, y=0, z=0), heading=0, camera_heading=0)
        self.keys.turn.side_effect = self.rotate

    def rotate(self, amount):
        # Measured first-person response to a single native mouse pulse.
        gain = .2 if self.backend is vc else .31334 if self.backend is sa else .1
        self.state["camera_heading"] -= math.radians(amount * gain)
        self.state["heading"] = self.state["camera_heading"]

    def tick(self, now, enabled=False, mode="free"):
        with patch("core.controller.time.monotonic", return_value=now):
            return self.walking.tick(self.state, enabled, mode)

    def test_numbered_turns_finish_smoothly_in_both_directions_while_stopped(self):
        for backend in (iii, vc, sa):
            with self.subTest(game=backend.__name__):
                self.configure(backend)
                for direction, sign in (("left", 1), ("right", -1)):
                    with self.subTest(direction=direction):
                        self.walking.stop()
                        self.keys.reset_mock()
                        self.state["camera_heading"] = 0
                        self.assertTrue(self.walking.steer(direction, "free", self.state, 25))
                        for step in range(720):
                            self.tick(step / 120)
                        multiplier = getattr(backend, "TURN_MULTIPLIER", 1)
                        gain = .2 if backend is vc else .31334 if backend is sa else .1
                        self.assertAlmostEqual(self.state["camera_heading"], sign * math.radians(25 * 10 * multiplier * gain))
                        self.assertEqual(self.walking.manual_turn, 0)
                        self.assertLessEqual(max(abs(c.args[0]) for c in self.keys.turn.call_args_list), 8)
                        self.keys.set.assert_not_called()

    def test_turn_speed_and_ramp_are_gentle_in_all_three_games(self):
        for backend in (iii, vc, sa):
            for direction in ("left", "right"):
                with self.subTest(game=backend.__name__, direction=direction):
                    self.configure(backend)
                    self.walking.steer(direction, "free", self.state, 100)
                    totals = []
                    for step in range(120):
                        self.tick(step / 120)
                        totals.append(sum(abs(c.args[0]) for c in self.keys.turn.call_args_list))
                    multiplier = getattr(backend, "TURN_MULTIPLIER", 1)
                    # The first half-second ramps up; the following half-second
                    # stays within half the old 160-unit/second speed limit.
                    self.assertLess(totals[59], 20 * multiplier)
                    self.assertLessEqual(totals[-1] - totals[59], 40 * multiplier)
                    amounts = [abs(c.args[0]) for c in self.keys.turn.call_args_list]
                    self.assertIn(1, amounts)
                    self.assertLessEqual(max(amounts), math.ceil(80 * multiplier / 120))

    def test_explore_and_mission_align_with_a_right_angle_within_six_seconds(self):
        for backend in (iii, vc):
            with self.subTest(game=backend.__name__):
                self.configure(backend)
                for mode in ("explore", "mission"):
                    with self.subTest(mode=mode):
                        self.walking.stop()
                        self.walking.navigator = Mock()
                        self.walking.navigator.waypoint.return_value = (-10, 0, 0)
                        self.walking.navigator.message = "following pedestrian path"
                        self.state["heading"] = self.state["camera_heading"] = 0
                        for step in range(720):
                            self.tick(step / 120, True, mode)
                            if 'forward' in self.keys.set.call_args.args[0]:
                                self.state['position'] = dict(
                                    x=self.state['position']['x'] - math.sin(self.state['camera_heading']) / 120,
                                    y=self.state['position']['y'] + math.cos(self.state['camera_heading']) / 120,
                                    z=0)
                            if abs(angle_delta(math.pi / 2, self.state["camera_heading"])) <= 0.04:
                                break
                        else:
                            self.fail("Classic camera took more than six seconds to align with the path")
                        self.assertEqual(self.walking.recoveries, 0)
                        self.keys.set.assert_called_with({"forward"})

    def test_obstacle_detour_turns_gradually_while_continuing_to_walk(self):
        for backend in (iii, vc):
            with self.subTest(game=backend.__name__):
                self.configure(backend)
                self.walking.set_mode("free")
                self.walking.recover(self.state["position"], 0, self.state)
                target = self.walking.recovery_target
                initial_error = abs(heading_to((0, 0), target))
                walking_during_turn = False
                for step in range(240):
                    self.tick(step / 120, True)
                    error = abs(angle_delta(heading_to((0, 0), target), self.state['camera_heading']))
                    if self.keys.set.call_args.args[0] == {'forward'} and error > .1:
                        walking_during_turn = True
                    if error < .08:
                        break
                self.assertLess(error, initial_error)
                self.assertTrue(walking_during_turn)
                self.assertLessEqual(max(abs(c.args[0]) for c in self.keys.turn.call_args_list), 8)
