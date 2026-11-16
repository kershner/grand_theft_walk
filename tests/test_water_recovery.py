"""Keep native water hazards and normal death/respawn recovery intact."""
import struct
import unittest
from unittest.mock import Mock, patch

from core.controller import Controller, Walking
from core.controls import can_control
from games import Capabilities
from games.san_andreas.game import Game, apply_protection
from tests.san_andreas.helpers import MemoryFixture


class WaterRecoveryTests(unittest.TestCase):
    def controller(self):
        controller = Controller.__new__(Controller)
        controller.settings = {'protection': True, 'peaceful': False, 'unlock_map': False, 'first_person': False}
        controller.capabilities = Capabilities(protection=True)
        controller.game = Mock()
        controller.backend = Mock()
        controller.movement = Mock()
        controller.camera = controller.peaceful = controller.world = None
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
