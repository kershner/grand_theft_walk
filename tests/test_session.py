"""Shared session ownership, launch gates, backend capabilities, and cleanup."""

from tests.fixtures import StateTestCase
import contextlib
import io
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import session
from core.controller import Controller
from core.files import write_state, read_state
from core.navigation import Node, PedestrianGraph
from core.runtime import control_request
from core.winmemory import ProcessJob, module_export, spawn, stop_processes, watch
from games import ACTIVE_GAMES, Capabilities, get_game
from tests.fixtures import MemoryFixture, bridge_exports


class ControllerSessionTests(StateTestCase):
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


class LaunchTests(StateTestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.args = ["--game", str(Path(folder) / "game.exe"), "--state-dir", folder]
        self.memory = Mock(pid=123)
        self.startup = Mock()
        self.startup_type = Mock(return_value=self.startup)
        self.stack.enter_context(patch.object(session, "configure_logging"))
        self.stack.enter_context(patch.object(session, "configure_window"))
        self.stack.enter_context(patch.object(session, "process_mutex"))
        self.pids = self.stack.enter_context(patch.object(session, "game_pids", side_effect=[[] for _ in ACTIVE_GAMES] + [[123]]))
        self.status = self.stack.enter_context(patch.object(session, "component_status", return_value={"setup_matches": True}))
        self.digest = self.stack.enter_context(patch("core.files.sha256"))
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
                self.pids.side_effect = [[] for _ in ACTIVE_GAMES] + [[123]]
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

    def test_missing_components_block_process_creation(self):
        self.status.return_value = {"setup_matches": False}
        with self.assertRaisesRegex(RuntimeError, "mod setup is incomplete"):
            self.launch()
        self.spawn.assert_not_called()

    def test_changed_component_hashes_allow_direct_launch(self):
        self.status.return_value = {"setup_matches": False, "mismatches": [
            {"path": "scripts/GTW.BackgroundInput.asi", "expected": "bundled", "actual": "rebuilt"}]}
        with self.assertLogs("core.manager", level="WARNING"):
            self.launch()
        self.spawn.assert_called_once()
        self.run.assert_called_once()

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


class ModuleExportTests(StateTestCase):
    def test_relocated_and_decorated_exports_are_resolved(self):
        mem = MemoryFixture()
        module = {"base": 0x10000000, "size": 0x10000}
        bridge_exports(mem, module["base"], {"_GTWInput": 0x3000, "GTWTerrain": 0x4000})
        self.assertEqual(module_export(mem, module, "GTWInput", 40), module["base"] + 0x3000)
        self.assertEqual(module_export(mem, module, "GTWTerrain", 3868), module["base"] + 0x4000)
        with self.assertRaisesRegex(RuntimeError, "is missing"):
            module_export(mem, module, "Other", 40)

    def test_invalid_and_forwarded_exports_are_rejected(self):
        for target in (0, 0x210, 0xFFF0, 0x20000):
            mem = MemoryFixture()
            module = {"base": 0x10000000, "size": 0x10000}
            bridge_exports(mem, module["base"], {"GTWInput": target})
            with self.subTest(target=target), self.assertRaises(RuntimeError):
                module_export(mem, module, "GTWInput", 40)
        bridge_exports(mem, module["base"], {"GTWInput": 0x3000})
        mem.write(module["base"] + 0x3C, struct.pack("<I", 0xFFFFFFFF))
        with self.assertRaisesRegex(RuntimeError, "bounds"):
            module_export(mem, module, "GTWInput", 40)


class LaunchWatchdogTests(StateTestCase):
    def test_deadline_terminates_only_the_owned_child(self):
        process = Mock()
        process.poll.return_value = None
        reason = watch(process, 10, lambda: False, clock=Mock(side_effect=[0, 11]), sleep=Mock())
        self.assertEqual(reason, "timeout")
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)

    def test_f12_rearms_after_release_and_stops_the_owned_child(self):
        process = Mock()
        process.poll.return_value = None
        reason = watch(process, 120, Mock(side_effect=[True, False, True]), clock=lambda: 0, sleep=Mock())
        self.assertEqual(reason, "f12")
        process.kill.assert_called_once_with()

    def test_normal_exit_does_not_terminate_a_process(self):
        process = Mock()
        process.poll.return_value = 0
        self.assertEqual(watch(process, 120, Mock(), clock=lambda: 0), "exited")
        process.kill.assert_not_called()


class ProcessOwnershipTests(StateTestCase):
    def test_failed_job_assignment_does_not_leave_a_child_running(self):
        child, job = Mock(), Mock()
        job.add.side_effect = RuntimeError("assignment failed")
        with patch("core.winmemory.subprocess.Popen", return_value=child):
            with self.assertRaisesRegex(RuntimeError, "assignment failed"):
                spawn(job, ["python", "child.py"])
        child.kill.assert_called_once()
        child.wait.assert_called_once()

    def test_slow_child_does_not_prevent_reaping_other_children(self):
        slow, other, job = Mock(), Mock(), Mock()
        slow.wait.side_effect = [subprocess.TimeoutExpired("child", 10), None]
        stop_processes(job, [slow, other])
        job.close.assert_called_once()
        slow.kill.assert_called_once()
        other.wait.assert_called_once()

    @unittest.skipUnless(sys.platform == "win32", "Windows job objects")
    def test_closing_a_real_job_stops_its_child(self):
        job = ProcessJob()
        children = []
        try:
            child = spawn(job, [sys.executable, "-B", "-c", "import time; time.sleep(30)"])
            children.append(child)
            self.assertIsNone(child.poll())
        finally:
            stop_processes(job, children)
        self.assertIsNotNone(child.returncode)


class PartialBackendTests(StateTestCase):
    def test_partial_backend_ticks_reports_and_closes_without_optional_features(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            state = {
                "ready": True, "foreground": True, "paused": False,
                "controls_disabled": False, "in_vehicle": False,
                "health": 100, "player_state": 0, "ped_state": 1,
                "standing": True, "in_air": False, "interior": 0,
                "position": {"x": 0, "y": 0, "z": 0}, "heading": 0,
            }
            game = Mock()
            game.snapshot.return_value = state
            game.location.return_value = {}
            graph = PedestrianGraph([
                Node((0, 0), (0, 0, 0), (((0, 1), False),)),
                Node((0, 1), (0, 5, 0), (((0, 0), False),)),
            ])
            # Missing optional attributes must not be accessed at all.
            backend = SimpleNamespace(CAPABILITIES=Capabilities(), Game=Mock(return_value=game),
                                      read_game_text=Mock(), load_destinations=Mock(return_value=[]))
            memory = SimpleNamespace(profile=SimpleNamespace(id="vice_city"),
                                     exe=directory / "game/gta-vc.exe", foreground=lambda: True,
                                     alive=lambda: True)
            args = SimpleNamespace(state_dir=directory, walk=False, mode="free")
            with patch("core.controller.Keys") as keys, patch("core.controller.Hotkeys") as hotkeys:
                keys.return_value.held = set()
                hotkeys.return_value.pressed.return_value = []
                controller = Controller(memory, args, backend, lambda mem: graph)
                backend.load_destinations.assert_called_once_with(memory.exe.parent, graph, backend.read_game_text)
                # Stale or malicious control files cannot turn unavailable features on.
                write_state(directory / "control", {
                    "first_person": True, "protection": True, "peaceful": True,
                    "unlock_map": True, "autosave": True,
                    "jump": control_request(), "view": control_request("up", 1),
                })
                self.assertTrue(controller.tick(10))
                report = read_state(directory / "state")
                self.assertEqual(report["game"], "vice_city")
                self.assertEqual(report["capabilities"], Capabilities().report())
                self.assertEqual(report["camera"], {"supported": False})
                for name in ("first_person", "protection", "peaceful", "unlock_map", "autosave"):
                    self.assertFalse(report["settings"][name])
                self.assertNotIn("jump", controller.settings)
                self.assertNotIn("view", controller.settings)
                hotkeys.return_value.pressed.return_value = ["f11"]
                controller.handle_hotkeys(state)
                self.assertFalse(controller.settings["first_person"])
                controller.stop()
                controller.close()
                report = read_state(directory / "state")
                self.assertEqual(report["controller"], "stopped")
                self.assertEqual(report["restore_errors"], [])
                keys.return_value.release.assert_called()
