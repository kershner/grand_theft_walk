import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from camera import FirstPersonCamera, FIRST_PERSON, ROTATE_PLAYER
from tests.helpers import MemoryFixture


class CameraTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "ZMenuSA.asi"
        data = b"verified camera fixture"
        path.write_bytes(data)
        self.hash_patch = patch("camera.MODULE_SHA256", hashlib.sha256(data).hexdigest())
        self.hash_patch.start()
        self.addCleanup(self.hash_patch.stop)
        self.module = {"base": 0x10000000, "size": 0x600000, "path": path}
        self.mem = MemoryFixture()
        self.mem.module = Mock(return_value=self.module)
        self.camera = FirstPersonCamera(self.mem)

    def flag(self, offset):
        return self.mem.byte(self.module["base"] + offset)

    def test_toggle_and_shutdown_restore_original_camera_flags(self):
        self.assertTrue(self.camera.apply(True))
        self.assertEqual((self.flag(FIRST_PERSON), self.flag(ROTATE_PLAYER)), (1, 1))
        self.assertTrue(self.camera.apply(False))
        self.assertEqual((self.flag(FIRST_PERSON), self.flag(ROTATE_PLAYER)), (0, 0))
        self.camera.apply(True)
        self.camera.close()
        self.assertEqual((self.flag(FIRST_PERSON), self.flag(ROTATE_PLAYER)), (0, 0))

    def test_unknown_version_cannot_write_camera_memory(self):
        self.module["path"].write_bytes(b"unsupported version")
        self.assertFalse(self.camera.apply(True))
        self.assertIn("Unsupported", self.camera.error)
        self.assertFalse(self.mem.data)

    def test_missing_mod_reports_unavailable(self):
        self.mem.module.return_value = None
        self.assertFalse(self.camera.apply(True))
        self.assertFalse(self.camera.report()["available"])
        self.assertFalse(self.mem.data)

    def test_invalid_or_out_of_bounds_flags_are_rejected(self):
        self.mem.write(self.module["base"] + FIRST_PERSON, b"\x02")
        self.assertFalse(self.camera.apply(True))
        self.assertEqual(self.flag(FIRST_PERSON), 2)
        self.module["size"] = 100
        self.assertFalse(self.camera.apply(True))
        self.assertEqual(self.flag(ROTATE_PLAYER), 0)

    def test_unloaded_module_is_not_restored(self):
        self.camera.apply(True)
        before = dict(self.mem.data)
        self.mem.module.return_value = None
        self.camera.close()
        self.assertEqual(self.mem.data, before)
