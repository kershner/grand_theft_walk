"""Voted respawn is one-shot, session-bound, and waits for safe native recovery."""
import struct
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.respawn import Respawn
from core.runtime import atomic_json, control_request
from core.controller import Controller
from games.san_andreas import game as sa
from games.vice_city import game as vc
from tests.san_andreas.helpers import MemoryFixture


class RespawnTests(unittest.TestCase):
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
        with self.assertLogs('core.respawn', level='ERROR'):
            self.assertFalse(self.respawn.tick(self.request, self.state, 10))
        self.game.kill_player.assert_not_called()

    def test_slow_or_failed_death_does_not_leave_controls_suspended_forever(self):
        self.assertTrue(self.respawn.tick(self.request, self.state, 10))
        with self.assertLogs('core.respawn', level='WARNING'):
            self.assertFalse(self.respawn.tick(self.request, self.state, 41))
        self.assertFalse(self.respawn.tick(self.request, self.state, 42))
        self.game.kill_player.assert_called_once()


class BackendRespawnTests(unittest.TestCase):
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


class ControllerRespawnTests(unittest.TestCase):
    def test_suspends_healing_autosave_and_input_until_player_respawns(self):
        with tempfile.TemporaryDirectory() as folder:
            args = SimpleNamespace(state_dir=Path(folder), walk=True, mode='free')
            memory, backend = Mock(), Mock()
            memory.profile.id = 'san_andreas'
            backend.CAPABILITIES = sa.CAPABILITIES
            backend.WALK_KEYS = None
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
                atomic_json(controller.control_path, dict(controller.settings, respawn=req))
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
