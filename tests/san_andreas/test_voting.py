"""Command vote timing, voter replacement, results and execution failures."""

import unittest
from unittest.mock import Mock, patch

from core.voting import CommandVoting


class CommandVotingTests(unittest.TestCase):
    def setUp(self):
        self.execute = Mock(return_value="Applied")
        self.timer = Mock()
        self.system = CommandVoting(self.execute, timer_factory=self.timer)
        self.addCleanup(self.system.close)

    def finish(self):
        self.timer.call_args.args[1]()

    def test_starter_yes_twenty_seconds_and_delayed_execution_once(self):
        text = self.system.start("camera", ["first"], "Alice", "Camera first")
        self.assertIn("20 seconds", text)
        self.assertEqual(self.timer.call_args.args[0], 20)
        self.assertEqual(self.system.vote.votes, {"alice": "yes"})
        self.execute.assert_not_called()
        self.assertEqual(
            self.system.start("mode", ["free"], "b", "mode"),
            "Vote already in progress.",
        )
        self.finish()
        self.finish()
        self.execute.assert_called_once_with("camera", ["first"])
        self.assertIn("Vote passed! (1-0)", self.system.drain_events()[0])
        self.assertFalse(self.system.vote.active)

    def test_changed_votes_ties_fail_and_late_ballots_are_ignored(self):
        self.system.start("mode", ["free"], "alice", "Free mode")
        self.system.cast("bob", "no")
        self.system.cast("ALICE", "no")
        self.system.cast("alice", "yes")
        with patch("core.voting.time.monotonic", return_value=self.system.closes_at + 1):
            self.assertIsNone(self.system.cast("charlie", "yes"))
        self.finish()
        self.execute.assert_not_called()
        self.assertIn("Vote failed! (1-1)", self.system.drain_events()[0])

    def test_execution_failure_resets_vote_and_reports_error(self):
        self.execute.side_effect = RuntimeError("Controller offline")
        self.system.start("camera", [], "alice", "Camera")
        self.finish()
        self.assertIn("Controller offline", self.system.drain_events()[-1])
        self.assertFalse(self.system.vote.active)
