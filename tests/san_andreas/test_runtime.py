"""Check log retention and simultaneous writes from separate processes."""

import logging
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from concurrent_log_handler import ConcurrentRotatingFileHandler
from runtime import LOG_BACKUPS, LOG_MAX_BYTES, configure_logging


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
