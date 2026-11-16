"""Bounded diagnostics, transition failures, and read-only attachment lifecycle."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.navigation import Node, PedestrianGraph
from games.vice_city import diagnostics


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.output = self.directory / "capture.jsonl"
        self.memory = SimpleNamespace(pid=123, exe=self.directory / "game/gta-vc.exe", alive=lambda: True)
        self.now = 0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def observe(self, **kwargs):
        return diagnostics.observe(self.memory, self.output, seconds=1,
                                   clock=self.clock, sleep=self.sleep, pressed=lambda: False,
                                   **kwargs)

    def rows(self):
        return [json.loads(line) for line in self.output.read_text().splitlines()]

    def test_read_errors_and_loading_do_not_abort_the_bounded_capture(self):
        game = Mock()
        game.snapshot.side_effect = [OSError("world changing"), {"ready": False},
                                    {"ready": False}, {"ready": False}]
        with patch.object(diagnostics, "Game", return_value=game), patch.object(diagnostics, "load_graph") as graph:
            report = self.observe()
        self.assertEqual(report["reason"], "timeout")
        self.assertEqual(report["samples"], 4)
        self.assertEqual(report["errors"], 1)
        self.assertEqual(self.rows()[0]["error"], "world changing")
        graph.assert_not_called()
        self.assertFalse((self.directory / "state.json").exists())
        self.assertEqual(json.loads(self.output.with_suffix(".summary.json").read_text()), report)

    def test_gameplay_includes_graph_and_nearby_path_without_control_methods(self):
        game = Mock()
        game.snapshot.return_value = {"ready": True, "position": {"x": 1, "y": 0, "z": 0}}
        game.location.return_value = {"places": ["Vice City"]}
        graph = PedestrianGraph([
            Node((0, 0), (0, 0, 0), (((0, 1), False),)),
            Node((0, 1), (5, 0, 0), (((0, 0), False),)),
        ])
        with patch.object(diagnostics, "Game", return_value=game), patch.object(diagnostics, "load_graph", return_value=graph) as loader:
            report = self.observe()
        self.assertEqual(report["ready_samples"], 4)
        self.assertEqual(report["graph"], {"loaded": True, "nodes": 2})
        loader.assert_called_once_with(self.memory)
        self.assertEqual(self.rows()[0]["nearby_path"]["node"], [0, 0])
        self.assertEqual(self.rows()[0]["nearby_path"]["distance"], 1)

    def test_nonfinite_candidate_reading_is_recorded_as_an_error(self):
        game = Mock()
        game.snapshot.return_value = {"ready": False, "camera_pitch": float("nan")}
        with patch.object(diagnostics, "Game", return_value=game):
            report = self.observe()
        self.assertEqual(report["errors"], 4)
        self.assertTrue(all("error" in row for row in self.rows()))

    def test_f12_rearms_after_release_and_does_not_close_the_game(self):
        with patch.object(diagnostics, "Game") as game:
            game.return_value.snapshot.return_value = {"ready": False}
            report = diagnostics.observe(self.memory, self.output, seconds=2,
                                         clock=self.clock, sleep=self.sleep,
                                         pressed=Mock(side_effect=[True, False, True]))
        self.assertEqual(report["reason"], "f12")
        self.assertEqual(report["samples"], 2)

    def test_exit_and_interrupt_leave_a_final_summary(self):
        for reason in ("game_exited", "interrupted"):
            with self.subTest(reason=reason), patch.object(diagnostics, "Game") as game:
                self.memory.alive = Mock(return_value=False) if reason == "game_exited" else Mock(side_effect=KeyboardInterrupt)
                report = self.observe()
                self.assertEqual(report["reason"], reason)
                self.assertEqual(report["status"], "stopped")
                game.return_value.snapshot.assert_not_called()

    def test_invalid_limits_and_installation_output_are_rejected_before_reading(self):
        for seconds in (0, 301, float("nan"), float("inf")):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                diagnostics.observe(self.memory, self.output, seconds=seconds)
        for interval in (0, 6, float("nan")):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                self.observe(interval=interval)
        with self.assertRaisesRegex(ValueError, "outside"):
            diagnostics.observe(self.memory, self.memory.exe.parent / "capture.jsonl")

    def test_cli_opens_only_read_only_memory_and_always_closes_it(self):
        with patch.object(diagnostics, "Memory") as memory, patch.object(diagnostics, "observe", side_effect=RuntimeError("bad layout")):
            with self.assertRaises(SystemExit):
                diagnostics.main(["--pid", "123"])
        memory.assert_called_once_with(pid=123)
        memory.return_value.close.assert_called_once_with()
