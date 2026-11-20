"""Check log retention and simultaneous writes from separate processes."""

import logging
import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from concurrent_log_handler import ConcurrentRotatingFileHandler

from core.files import atomic_json, read_json
from core.runtime import LOG_BACKUPS, LOG_MAX_BYTES, configure_logging, read_controls


def write_logs(folder, worker):
    handler = ConcurrentRotatingFileHandler(
        str(Path(folder) / "shared.log"),
        maxBytes=1024,
        backupCount=LOG_BACKUPS,
        encoding="utf-8",
    )
    logger = logging.Logger(f"worker-{worker}", level=logging.INFO)
    logger.addHandler(handler)
    try:
        for index in range(80):
            logger.info("worker=%s record=%s %s", worker, index, "x" * 80)
    finally:
        handler.close()


def publish_controls(folder):
    for value in range(200):
        atomic_json(Path(folder) / "control.json", {"value": value})


class RuntimeFileTests(unittest.TestCase):
    def test_read_retries_a_temporary_permission_error(self):
        path = unittest.mock.Mock()
        path.read_text.side_effect = [
            PermissionError("file is being replaced"),
            '{"walking":false}',
        ]
        with patch("core.runtime.time.sleep"):
            self.assertEqual(read_json(path), {"walking": False})
        self.assertEqual(path.read_text.call_count, 2)

    def test_blocked_control_read_keeps_existing_settings(self):
        path = unittest.mock.Mock()
        path.read_text.side_effect = PermissionError("temporarily locked")
        settings = {"walking": True, "mode": "free"}
        with patch("core.runtime.time.sleep"):
            read_controls(path, settings)
        self.assertEqual(settings, {"walking": True, "mode": "free"})
        self.assertEqual(path.read_text.call_count, 6)

    def test_replace_retries_then_publishes_complete_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "control.json"
            atomic_json(path, {"value": 1})
            replace = Path.replace
            attempts = 0

            def replace_after_unlock(source, destination):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise PermissionError("reader still has the file open")
                return replace(source, destination)

            with patch.object(Path, "replace", replace_after_unlock), patch("core.runtime.time.sleep"):
                atomic_json(path, {"value": 2})
            self.assertEqual(read_json(path), {"value": 2})
            self.assertEqual(attempts, 3)
            with (
                patch.object(Path, "replace", side_effect=PermissionError("still locked")),
                patch("core.runtime.time.sleep"),
            ):
                with self.assertRaises(PermissionError):
                    atomic_json(path, {"value": 3})
            self.assertEqual(read_json(path), {"value": 2})
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])

    def test_separate_process_can_publish_while_json_is_being_read(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "control.json"
            atomic_json(path, {"value": -1})
            worker = multiprocessing.get_context("spawn").Process(
                target=publish_controls, args=(folder,)
            )
            worker.start()
            deadline = time.monotonic() + 15
            try:
                while worker.is_alive() and time.monotonic() < deadline:
                    self.assertIn(read_json(path)["value"], range(-1, 200))
                    time.sleep(0.001)
                worker.join(timeout=1)
                self.assertEqual(worker.exitcode, 0)
                self.assertEqual(read_json(path), {"value": 199})
            finally:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)


class RuntimeLoggingTests(unittest.TestCase):
    def test_shared_configuration_records_debug_and_tracebacks_once(self):
        root = logging.getLogger()
        previous_level = root.level
        previous_http_level = logging.getLogger("werkzeug").level
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(root, "handlers", []), patch("logging.captureWarnings"):
                try:
                    path = configure_logging(folder)
                    self.assertEqual(path, Path(folder) / "san_andreas.log")
                    configure_logging(folder)
                    self.assertEqual(len(root.handlers), 2)
                    file_handler, console = root.handlers
                    self.assertEqual(file_handler.maxBytes, LOG_MAX_BYTES)
                    self.assertEqual(file_handler.backupCount, LOG_BACKUPS)
                    self.assertEqual(console.level, logging.INFO)
                    self.assertEqual(logging.getLogger("werkzeug").level, logging.WARNING)
                    console.setLevel(logging.CRITICAL)
                    root.debug("command received")
                    try:
                        raise RuntimeError("test failure")
                    except RuntimeError:
                        root.exception("operation failed")
                    content = path.read_text(encoding="utf-8")
                    self.assertEqual(content.count("command received"), 1)
                    self.assertIn("Traceback", content)
                    self.assertIn("test failure", content)
                finally:
                    for handler in root.handlers:
                        handler.close()
                    root.setLevel(previous_level)
                    logging.getLogger("werkzeug").setLevel(previous_http_level)

    def test_multiple_processes_share_bounded_rotating_log(self):
        with tempfile.TemporaryDirectory() as folder:
            context = multiprocessing.get_context("spawn")
            workers = [
                context.Process(target=write_logs, args=(folder, index)) for index in range(2)
            ]
            try:
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=15)
                    self.assertEqual(worker.exitcode, 0)
                logs = sorted(Path(folder).glob("shared.log*"))
                self.assertEqual(len(logs), 1 + LOG_BACKUPS)
                for path in logs:
                    self.assertLess(path.stat().st_size, 1200)
                    for line in path.read_text(encoding="utf-8").splitlines():
                        self.assertRegex(line, r"^worker=[01] record=\d+ x{80}$")
            finally:
                for worker in workers:
                    if worker.is_alive():
                        worker.terminate()
                        worker.join(timeout=5)
