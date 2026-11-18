"""Child ownership and cleanup shared by both launch paths."""

import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from core.processes import ProcessJob, spawn, stop_processes


class ProcessOwnershipTests(unittest.TestCase):
    def test_failed_job_assignment_does_not_leave_a_child_running(self):
        child, job = Mock(), Mock()
        job.add.side_effect = RuntimeError("assignment failed")
        with patch("core.processes.subprocess.Popen", return_value=child):
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
