"""Camera selection, native transitions, and fail-closed plugin validation."""

import hashlib
import math
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.san_andreas.helpers import MemoryFixture
from vice_city.camera import ACTIVE, PED_ZOOM_INDICATOR, FirstPersonCamera


class CameraTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "FirstPerson.asi"
        path.write_bytes(b"verified camera fixture")
        self.module = {"base": 0x20000000, "size": ACTIVE + 1, "path": path}
        self.mem = MemoryFixture()
        self.mem.module = Mock(return_value=self.module)
        self.mem.write(PED_ZOOM_INDICATOR, struct.pack("<f", 2))
        self.state = dict(ready=True, foreground=True, paused=False,
                          controls_disabled=False, in_vehicle=False, cutscene=False,
                          health=100, player_state=0)
        for target, kwargs in (
            ("vice_city.camera.CAMERA_MODULE_SHA256", {"new": hashlib.sha256(path.read_bytes()).hexdigest()}),
            ("vice_city.game.Game.snapshot", {"side_effect": lambda: dict(self.state)}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.camera = FirstPersonCamera(self.mem)

    def selector(self):
        return self.mem.unpack(PED_ZOOM_INDICATOR, "f")[0]

    def test_selection_uses_native_indicator_and_reports_actual_plugin_state(self):
        self.assertTrue(self.camera.apply(True))
        self.assertEqual(self.selector(), 3)
        # The game applies the transition on its next frame, not in apply().
        self.assertFalse(self.camera.report()["first_person"])
        self.mem.write(self.module["base"] + ACTIVE, b"\x01")
        self.assertTrue(self.camera.report()["first_person"])
        self.assertTrue(self.camera.apply(False))
        self.assertEqual(self.selector(), 2)
        self.camera.apply(True, rotate_player=False)
        self.assertEqual(self.selector(), 3)
        self.camera.close()
        self.assertEqual(self.selector(), 2)

    def test_initial_first_person_can_switch_to_third_and_restore(self):
        self.mem.write(PED_ZOOM_INDICATOR, struct.pack("<f", 3))
        self.camera.apply(False)
        self.assertEqual(self.selector(), 2)
        self.camera.close()
        self.assertEqual(self.selector(), 3)

    def test_missing_changed_small_or_invalid_plugin_cannot_write(self):
        for change in ("missing", "hash", "size", "flag"):
            with self.subTest(change=change):
                self.mem.module.return_value = None if change == "missing" else self.module
                self.module["path"].write_bytes(b"wrong" if change == "hash" else b"verified camera fixture")
                self.module["size"] = ACTIVE if change == "size" else ACTIVE + 1
                self.mem.write(self.module["base"] + ACTIVE, bytes([2 if change == "flag" else 0]))
                before = dict(self.mem.data)
                self.assertFalse(self.camera.apply(True))
                self.assertEqual(self.mem.data, before)
                self.assertIsNotNone(self.camera.error)

    def test_unsafe_gameplay_defers_selection_without_writes(self):
        for update in ({"ready": False}, {"foreground": False}, {"paused": True},
                       {"cutscene": True}, {"in_vehicle": True}, {"health": 0},
                       {"controls_disabled": True}, {"player_state": 1}):
            with self.subTest(update=update):
                original = dict(self.state)
                self.state.update(update)
                before = dict(self.mem.data)
                self.assertFalse(self.camera.apply(True))
                self.assertEqual(self.mem.data, before)
                self.state = original

    def test_invalid_selector_is_rejected(self):
        for value in (math.nan, math.inf, -1, 1.5, 5):
            self.mem.write(PED_ZOOM_INDICATOR, struct.pack("<f", value))
            before = dict(self.mem.data)
            self.assertFalse(self.camera.apply(True))
            self.assertEqual(self.mem.data, before)

    def test_external_selection_and_unloaded_module_are_not_restored(self):
        self.camera.apply(True)
        self.mem.write(PED_ZOOM_INDICATOR, struct.pack("<f", 1))
        self.camera.close()
        self.assertEqual(self.selector(), 1)
        self.camera.apply(True)
        self.mem.module.return_value = None
        self.camera.close()
        self.assertEqual(self.selector(), 3)
        self.assertFalse(self.camera.report()["available"])


if __name__ == "__main__":
    unittest.main()
