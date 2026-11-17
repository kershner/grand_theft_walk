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


class HalfwayReminderTests(unittest.TestCase):
    def setUp(self):
        self.execute = Mock(return_value=None)
        self.timer_factory = Mock(side_effect=lambda *args: Mock())
        self.system = CommandVoting(self.execute, timer_factory=self.timer_factory)
        self.addCleanup(self.system.close)

    def start(self, name="respawn", duration=60):
        self.system.start(name, [], "alice", "Respawn the player", duration=duration)
        return [call.args[1] for call in self.timer_factory.call_args_list]

    def test_every_sixty_second_command_gets_one_reminder_with_current_tally(self):
        for name in ("respawn", "other-command"):
            with self.subTest(command=name):
                self.timer_factory.reset_mock()
                halfway, finish = self.start(name)
                self.assertEqual([call.args[0] for call in self.timer_factory.call_args_list], [30, 60])
                self.system.cast("bob", "no")
                self.assertEqual(self.system.drain_events(), [])
                with patch("core.voting.time.monotonic", return_value=self.system.closes_at-30):
                    halfway()
                    halfway()
                events = self.system.drain_events()
                self.assertEqual(len(events), 1)
                self.assertIn("Vote halfway!", events[0])
                self.assertIn("30 seconds remaining", events[0])
                self.assertIn("Yes: 1 | No: 1", events[0])
                self.execute.assert_not_called()
                finish()
                self.system.drain_events()

    def test_other_durations_retain_their_existing_behavior(self):
        for duration in (20, 45, 120):
            self.timer_factory.reset_mock()
            (finish,) = self.start(duration=duration)
            self.assertEqual(self.timer_factory.call_args.args[0], duration)
            finish()
            self.assertFalse(any("halfway" in event for event in self.system.drain_events()))

    def test_no_late_or_cancelled_reminders(self):
        halfway, finish = self.start()
        with patch("core.voting.time.monotonic", return_value=self.system.closes_at):
            halfway()
        self.assertEqual(self.system.drain_events(), [])
        vote = self.system.vote
        finish()
        vote.halfway_timer.cancel.assert_called_once()
        self.system.drain_events()
        halfway()
        self.assertEqual(self.system.drain_events(), [])
        self.timer_factory.reset_mock()
        halfway, _ = self.start()
        vote = self.system.vote
        self.system.close()
        vote.timer.cancel.assert_called_once()
        vote.halfway_timer.cancel.assert_called_once()
        halfway()
        self.assertEqual(self.system.drain_events(), [])

    def test_old_reminder_cannot_announce_during_a_new_vote(self):
        halfway, finish = self.start()
        finish()
        self.system.drain_events()
        self.start()
        halfway()
        self.assertEqual(self.system.drain_events(), [])
