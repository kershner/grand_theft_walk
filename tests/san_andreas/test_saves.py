import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from saves import MENU, SAVE_PREFIX, Autosave, NativeSaveRequest

from .helpers import MemoryFixture, checkpoint


class FakeBridge:
    def __init__(self):
        self.busy = False
        self.result = None
        self.calls = []

    def start(self, slot):
        self.calls.append(slot)
        self.busy = True

    def poll(self):
        if self.result is not None:
            self.busy = False
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self):
        pass


class NativeSaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = NativeSaveRequest(Mock(pid=1234))
        self.client.folder = Path(self.temp.name)
        hashes = {}
        for name in ("save_request.exe", "save_bridge.dll"):
            data = name.encode()
            (self.client.folder / name).write_bytes(data)
            hashes[name] = hashlib.sha256(data).hexdigest()
        (self.client.folder / "manifest.json").write_text(
            json.dumps({"format": 1, "files": hashes})
        )

    def test_helper_is_hidden_and_receives_only_game_pid_and_save_slot(self):
        with patch("saves.subprocess.Popen") as popen:
            self.client.start(2)
        self.assertEqual(popen.call_args.args[0][1:], ["1234", "2"])
        self.assertTrue(popen.call_args.kwargs["creationflags"])

    def test_modified_bridge_is_rejected_before_starting_process(self):
        (self.client.folder / "save_bridge.dll").write_bytes(b"modified")
        with patch("saves.subprocess.Popen") as popen:
            with self.assertRaisesRegex(ValueError, "validation"):
                self.client.start(0)
        popen.assert_not_called()

    def test_pending_request_never_blocks_or_is_terminated_on_close(self):
        process = Mock()
        process.poll.return_value = None
        self.client.process = process
        self.assertIsNone(self.client.poll())
        self.client.close()
        process.communicate.assert_not_called()
        process.terminate.assert_not_called()

    def test_native_completion_and_bad_responses(self):
        for output, code, success in (
            (b'{"status":"saved","duration_ms":9}', 0, True),
            (b'{"status":"deferred"}', 3, True),
            (b"null", 0, False),
            (b"broken", 1, False),
        ):
            with self.subTest(output=output):
                process = Mock(returncode=code)
                process.poll.return_value = code
                process.communicate.return_value = (output, None)
                self.client.process = process
                if success:
                    self.assertIn(self.client.poll()["status"], ("saved", "deferred"))
                else:
                    with self.assertRaises(RuntimeError):
                        self.client.poll()


class AutosaveTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.state_dir = root / "runtime"
        self.state_dir.mkdir()
        self.path = root / "GTASAsf1.b"
        self.original = checkpoint()
        self.path.write_bytes(self.original)
        self.record = self.state_dir / "launch.json"
        self.record.write_text(json.dumps({"slot": 0, "path": str(self.path)}))
        self.mem = MemoryFixture()
        self.mem.foreground = Mock(return_value=True)
        self.mem.write(SAVE_PREFIX, str(root / "GTASAsf").encode("mbcs") + b"\0")
        self.bridge = FakeBridge()
        self.saver = Autosave(self.mem, self.state_dir, bridge=self.bridge)
        self.saver.next_at = 60.0
        self.state = dict(
            ready=True,
            foreground=True,
            paused=False,
            controls_disabled=False,
            player_state=0,
            health=100,
            in_vehicle=False,
            interior=0,
            ped_state=1,
        )

    def page(self, page):
        self.mem.write(MENU + 0x5C, bytes((page is not None,)))
        if page is not None:
            self.mem.write(MENU + 0x15D, bytes((page,)))

    def start_save(self):
        self.assertTrue(self.saver.tick(self.state, 60))
        self.assertEqual(self.saver.phase, "saving")
        self.assertEqual(self.bridge.calls, [0])

    def test_save_runs_after_one_minute_and_keeps_previous_checkpoint(self):
        self.assertFalse(self.saver.tick(self.state, 59.9))
        self.start_save()
        new = checkpoint(12)
        self.path.write_bytes(new)
        self.bridge.result = {"status": "saved", "duration_ms": 8}
        self.saver.tick(self.state, 60.8)
        self.assertEqual(self.saver.count, 1)
        self.assertEqual(self.saver.backup.read_bytes(), self.original)
        self.assertEqual(self.mem.byte(MENU + 0x32), 0)
        record = json.loads(self.record.read_text())
        self.assertEqual(record["sha256"], hashlib.sha256(new).hexdigest())
        self.assertEqual(self.saver.phase, "idle")
        self.assertAlmostEqual(self.saver.next_at, 120.8)
        self.assertFalse(self.saver.tick(self.state, 120.7))
        self.assertEqual(self.saver.duration_ms, 8)

    def test_unsafe_gameplay_defers_without_opening_menu(self):
        for key, value in (
            ("paused", True),
            ("controls_disabled", True),
            ("in_vehicle", True),
            ("interior", 5),
            ("health", 0),
        ):
            with self.subTest(key=key):
                state = dict(self.state, **{key: value})
                self.assertFalse(self.saver.tick(state, 60))
        self.assertFalse(self.saver.backup.exists())
        self.assertEqual(self.bridge.calls, [])

    def test_missions_cutscenes_and_door_transitions_defer_save(self):
        for address in (0xB5F851, 0x96A7CC):
            self.mem.write(address, struct.pack("<I", 1))
            self.assertFalse(self.saver.tick(self.state, 60))
            self.mem.write(address, bytes(4))
        self.mem.write(0xA476AC, struct.pack("<I", 8))
        self.mem.write(0xA49960 + 8, struct.pack("<I", 1))
        self.assertFalse(self.saver.tick(self.state, 60))

    def test_background_save_uses_native_bridge_without_changing_game_memory(self):
        before = dict(self.mem.data)
        state = dict(self.state, foreground=False)
        self.assertTrue(self.saver.tick(state, 60))
        self.assertEqual(self.bridge.calls, [0])
        self.saver.tick(state, 90)
        self.bridge.result = {"status": "saved"}
        self.saver.tick(state, 91)
        self.assertEqual(self.saver.count, 1)
        self.assertEqual(self.mem.data, before)

    def test_existing_pause_menu_can_save_without_opening_or_closing_it(self):
        self.page(41)
        state = dict(self.state, foreground=False, paused=True, controls_disabled=True)
        self.assertTrue(self.saver.tick(state, 60))
        self.assertEqual(self.mem.byte(MENU + 0x15D), 41)
        self.assertEqual(self.mem.byte(MENU + 0x32), 0)

    def test_occupied_save_slot_mismatch_is_never_overwritten(self):
        self.record.write_text(json.dumps({"slot": 1, "path": str(self.path)}))
        with self.assertLogs("saves", level="WARNING"):
            self.assertFalse(self.saver.tick(self.state, 60))
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.mem.byte(MENU + 0x5F), 0)
        self.assertIsNotNone(self.saver.error)

    def test_invalid_native_save_restores_backup_and_reports_error(self):
        self.start_save()
        self.path.write_bytes(b"incomplete save")
        self.bridge.result = {"status": "saved"}
        with self.assertLogs("saves", level="WARNING"):
            self.saver.tick(self.state, 60.8)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.saver.count, 0)
        self.assertIsNotNone(self.saver.error)

    def test_pending_save_preserves_active_file_and_backup(self):
        self.start_save()
        self.path.write_bytes(b"save in progress")
        self.saver.tick(self.state, 90)
        self.assertEqual(self.path.read_bytes(), b"save in progress")
        self.assertEqual(self.saver.backup.read_bytes(), self.original)
        self.assertEqual(self.saver.phase, "saving")

    def test_deferred_native_request_retries_when_safe(self):
        self.start_save()
        self.bridge.result = {"status": "deferred"}
        self.assertFalse(self.saver.tick(self.state, 60.1))
        self.assertEqual(self.saver.count, 0)
        self.assertEqual(self.saver.next_at, 61.1)
        self.assertIsNone(self.saver.error)
        self.assertFalse(self.saver.tick(self.state, 61))
        self.bridge.result = None
        self.assertTrue(self.saver.tick(self.state, 61.1))
        self.assertEqual(self.bridge.calls, [0, 0])

    def test_disabled_autosave_waits_a_full_interval_after_reenable(self):
        self.assertFalse(self.saver.tick(self.state, 60, enabled=False))
        self.assertFalse(self.saver.tick(self.state, 119))
        self.assertTrue(self.saver.tick(self.state, 120))


if __name__ == "__main__":
    unittest.main()
