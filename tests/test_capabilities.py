"""Exercise a partial backend without San Andreas features or memory writes."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from gtw.games import Capabilities
from gtw.controller import Controller
from gtw.navigation import Node, PedestrianGraph
from gtw.runtime import atomic_json, control_request


class PartialBackendTests(unittest.TestCase):
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
            with patch("gtw.controller.Keys") as keys, patch("gtw.controller.Hotkeys") as hotkeys:
                keys.return_value.held = set()
                hotkeys.return_value.pressed.return_value = []
                controller = Controller(memory, args, backend, lambda mem: graph)
                backend.load_destinations.assert_called_once_with(memory.exe.parent, graph, backend.read_game_text)
                # Stale or malicious control files cannot turn unavailable features on.
                atomic_json(directory / "control.json", {
                    "first_person": True, "protection": True, "peaceful": True,
                    "unlock_map": True, "autosave": True,
                    "jump": control_request(), "view": control_request("up", 1),
                })
                self.assertTrue(controller.tick(10))
                report = json.loads((directory / "state.json").read_text())
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
                report = json.loads((directory / "state.json").read_text())
                self.assertEqual(report["controller"], "stopped")
                self.assertEqual(report["restore_errors"], [])
                keys.return_value.release.assert_called()
