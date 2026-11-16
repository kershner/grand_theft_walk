"""Shared session ownership, launch gates, and cleanup for both backends."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import session
from games import ACTIVE_GAMES, get_game


class ControllerSessionTests(unittest.TestCase):
    def test_each_backend_owns_and_closes_its_memory_and_controller(self):
        for game in ACTIVE_GAMES:
            with self.subTest(game=game), contextlib.ExitStack() as stack:
                memory = Mock(pid=123)
                backend = Mock()
                controller = Mock()
                controller.tick.return_value = False
                stack.enter_context(patch.object(session, "backend_for", return_value=backend))
                opened = stack.enter_context(patch.object(session, "open_memory", return_value=memory))
                lock = stack.enter_context(patch.object(session, "process_mutex"))
                factory = stack.enter_context(patch.object(session, "Controller", return_value=controller))
                session.run(SimpleNamespace(seconds=None), game, pid=123)
                opened.assert_called_once_with(game, writable=True, pid=123)
                self.assertIn("Walker-123", lock.call_args.args[0])
                self.assertIs(factory.call_args.kwargs["backend"], backend)
                self.assertIs(factory.call_args.kwargs["autosave_factory"], backend.Autosave)
                controller.tick.assert_called_once()
                controller.close.assert_called_once()
                memory.close.assert_called_once()

    def test_tick_error_still_releases_controller_memory_and_mutex(self):
        memory, controller = Mock(pid=123), Mock()
        controller.tick.side_effect = RuntimeError("tick failed")
        with (
            patch.object(session, "backend_for"),
            patch.object(session, "open_memory", return_value=memory),
            patch.object(session, "process_mutex") as lock,
            patch.object(session, "Controller", return_value=controller),
            self.assertRaisesRegex(RuntimeError, "tick failed"),
        ):
            session.run(SimpleNamespace(seconds=None), "vice_city")
        controller.close.assert_called_once()
        memory.close.assert_called_once()
        lock.return_value.__exit__.assert_called_once()

    def test_duplicate_session_closes_memory_without_creating_a_controller(self):
        memory = Mock(pid=123)
        with (
            patch.object(session, "backend_for"),
            patch.object(session, "open_memory", return_value=memory),
            patch.object(session, "process_mutex") as lock,
            patch.object(session, "Controller") as factory,
        ):
            lock.return_value.__enter__.side_effect = RuntimeError("duplicate")
            with self.assertRaisesRegex(RuntimeError, "duplicate"):
                session.run(SimpleNamespace(seconds=None), "san_andreas")
        factory.assert_not_called()
        memory.close.assert_called_once()

    def test_session_deadline_stops_before_another_tick(self):
        memory, controller = Mock(pid=123), Mock()
        with (
            patch.object(session, "backend_for"),
            patch.object(session, "open_memory", return_value=memory),
            patch.object(session, "process_mutex"),
            patch.object(session, "Controller", return_value=controller),
            patch.object(session.time, "monotonic", side_effect=[10, 12]),
        ):
            session.run(SimpleNamespace(seconds=1), "vice_city")
        controller.tick.assert_not_called()
        controller.close.assert_called_once()
        memory.close.assert_called_once()


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.args = ["--game", str(Path(folder) / "game.exe"), "--state-dir", folder]
        self.memory = Mock(pid=123)
        self.startup = Mock()
        self.startup_type = Mock(return_value=self.startup)
        self.stack.enter_context(patch.object(session, "configure_logging"))
        self.stack.enter_context(patch.object(session, "process_mutex"))
        self.pids = self.stack.enter_context(patch.object(session, "game_pids", side_effect=[[], [], [123]]))
        self.status = self.stack.enter_context(patch.object(session, "component_status", return_value={"setup_matches": True}))
        self.digest = self.stack.enter_context(patch("core.setup.sha256"))
        self.process = Mock(pid=123)
        self.process.poll.return_value = None
        self.spawn = self.stack.enter_context(patch.object(session.subprocess, "Popen", return_value=self.process))
        self.opened = self.stack.enter_context(patch.object(session, "open_memory", return_value=self.memory))
        self.run = self.stack.enter_context(patch.object(session, "run"))

    def launch(self, game="vice_city", extra=()):
        self.digest.return_value = get_game(game).verified_executable
        session.launch(game, self.startup_type, [*self.args, *extra])

    def test_both_launchers_attach_controller_to_the_started_pid(self):
        for game in ACTIVE_GAMES:
            with self.subTest(game=game):
                self.pids.side_effect = [[], [], [123]]
                self.run.reset_mock()
                self.launch(game)
                args, selected_game = self.run.call_args.args
                self.assertEqual(selected_game, game)
                self.assertTrue(args.walk)
                self.assertIsNone(args.seconds)
                self.assertEqual(self.run.call_args.kwargs, {"pid": 123})

    def test_prepare_only_restores_startup_changes_without_running_controller(self):
        self.launch(extra=["--prepare-only"])
        self.run.assert_not_called()
        self.startup.world.restore.assert_called_once()
        self.startup.keys.release.assert_called_once()
        self.memory.close.assert_called_once()

    def test_failed_startup_restores_changes_and_closes_memory(self):
        self.startup.prepare.side_effect = RuntimeError("startup failed")
        with self.assertRaisesRegex(RuntimeError, "startup failed"):
            self.launch()
        self.run.assert_not_called()
        self.startup.world.restore.assert_called_once()
        self.startup.keys.release.assert_called_once()
        self.memory.close.assert_called_once()

    def test_restore_error_still_closes_memory(self):
        self.startup.world.restore.side_effect = OSError("restore failed")
        with self.assertRaisesRegex(OSError, "restore failed"):
            self.launch(extra=["--prepare-only"])
        self.memory.close.assert_called_once()

    def test_input_release_error_still_closes_memory(self):
        self.startup.keys.release.side_effect = OSError("input failed")
        with self.assertRaisesRegex(OSError, "input failed"):
            self.launch(extra=["--prepare-only"])
        self.memory.close.assert_called_once()

    def test_component_mismatch_blocks_process_creation(self):
        self.status.return_value = {"setup_matches": False}
        with self.assertRaisesRegex(RuntimeError, "mod files"):
            self.launch()
        self.spawn.assert_not_called()

    def test_unverified_executable_blocks_process_creation(self):
        self.digest.return_value = "unverified"
        with self.assertRaisesRegex(RuntimeError, "Unsupported"):
            session.launch("vice_city", self.startup_type, self.args)
        self.status.assert_not_called()
        self.spawn.assert_not_called()

    def test_existing_game_blocks_process_creation(self):
        self.pids.side_effect = [[123]]
        with self.assertRaisesRegex(RuntimeError, "Close the existing"):
            self.launch()
        self.spawn.assert_not_called()

    def test_nonfinite_or_out_of_bounds_timing_is_rejected_before_launch(self):
        for flag, value in (("--timeout", "nan"), ("--timeout", "9"),
                            ("--seconds", "inf"), ("--seconds", "0"), ("--seconds", "301")):
            with self.subTest(flag=flag, value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.launch(extra=[flag, value])
        self.spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
