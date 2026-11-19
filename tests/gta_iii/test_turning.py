"""Exercise commands and closed-loop steering with III's degree-based camera."""
import math
import unittest
from unittest.mock import Mock, patch

from core.controller import Walking
from core.navigation import angle_delta
from games.gta_iii.game import TURN_MULTIPLIER


class TurningTests(unittest.TestCase):
    def setUp(self):
        with patch("core.controller.Keys"):
            self.walking = Walking(bindings={"forward": "w", "back": "s"},
                                   turn_multiplier=TURN_MULTIPLIER)
        self.keys = self.walking.keys
        self.state = dict(ready=True, foreground=False, background_control=True,
                          paused=False, controls_disabled=False, in_vehicle=False,
                          health=100, player_state=0, ped_state=0, interior=0,
                          position=dict(x=0, y=0, z=0), heading=0, camera_heading=0)
        self.keys.turn.side_effect = self.rotate

    def rotate(self, amount):
        self.state["camera_heading"] -= math.radians(amount * 0.1)
        self.state["heading"] = self.state["camera_heading"]

    def tick(self, now, enabled=False, mode="free"):
        with patch("core.controller.time.monotonic", return_value=now):
            return self.walking.tick(self.state, enabled, mode)

    def test_numbered_turns_finish_smoothly_in_both_directions_while_stopped(self):
        for direction, sign in (("left", 1), ("right", -1)):
            with self.subTest(direction=direction):
                self.walking.stop()
                self.keys.reset_mock()
                self.state["camera_heading"] = 0
                self.assertTrue(self.walking.steer(direction, "free", self.state, 25))
                for step in range(720):
                    self.tick(step / 120)
                self.assertAlmostEqual(self.state["camera_heading"], sign * math.radians(100))
                self.assertEqual(self.walking.manual_turn, 0)
                self.assertLessEqual(max(abs(c.args[0]) for c in self.keys.turn.call_args_list), 8)
                self.keys.set.assert_not_called()

    def test_explore_and_mission_align_with_a_right_angle_within_six_seconds(self):
        for mode in ("explore", "mission"):
            with self.subTest(mode=mode):
                self.walking.stop()
                self.walking.navigator = Mock()
                self.walking.navigator.waypoint.return_value = (-10, 0, 0)
                self.walking.navigator.message = "following pedestrian path"
                self.state["heading"] = self.state["camera_heading"] = 0
                for step in range(720):
                    self.tick(step / 120, True, mode)
                    if abs(angle_delta(math.pi / 2, self.state["camera_heading"])) <= 0.04:
                        break
                else:
                    self.fail("GTA III took more than six seconds to align with the path")
                self.assertEqual(self.walking.recoveries, 0)
                self.keys.set.assert_called_with({"forward"})

    def test_obstacle_recovery_uses_the_same_turn_response(self):
        self.walking.set_mode("free")
        self.walking.recover(self.state["position"], 0)
        for step in range(720):
            self.tick(step / 120, True)
            if self.walking.recovery_turn == 0:
                break
        self.assertEqual(self.walking.recovery_turn, 0)
        self.assertAlmostEqual(self.state["camera_heading"], -math.radians(80))
