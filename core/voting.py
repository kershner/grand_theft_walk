"""Viewer ballots and timed command votes with a strict yes majority."""

import math
import threading
import time
from dataclasses import dataclass, field


def tally(votes, choices):
    return [sum(value == choice for value in votes.values()) for choice in choices]


def cast_ballot(votes, user, choice):
    user = (user or "").strip().casefold()
    if not user:
        raise ValueError("Could not identify your viewer account.")
    votes[user] = choice
    return user


@dataclass
class VoteState:
    active: bool = False
    command: str = ""
    args: list[str] = field(default_factory=list)
    votes: dict = field(default_factory=dict)
    timer: threading.Timer | None = None
    halfway_timer: threading.Timer | None = None
    halfway_announced: bool = False


class CommandVoting:
    def __init__(self, execute, duration=20, timer_factory=threading.Timer):
        self.execute = execute
        self.duration = duration
        self.timer_factory = timer_factory
        self.vote = VoteState()
        self.lock = threading.Lock()
        self.events = []
        self.closes_at = 0.0

    def start(self, name, args, starter, help_text, execute=None, duration=None):
        with self.lock:
            if self.vote.active:
                return "Vote already in progress."
            vote = VoteState(active=True, command=name, args=list(args))
            cast_ballot(vote.votes, starter, "yes")
            self.vote = vote
            duration = self.duration if duration is None else duration
            self.closes_at = time.monotonic() + duration
            # Thread timers survive Flask's per-request asyncio event loop.
            if duration == 60:
                halfway = self.timer_factory(30, lambda: self._announce_halfway(vote, help_text))
                halfway.daemon = True
                vote.halfway_timer = halfway
                halfway.start()
            timer = self.timer_factory(
                duration, lambda: self._finish(vote, help_text, execute)
            )
            timer.daemon = True
            vote.timer = timer
            timer.start()
            return (
                f"Vote started! {help_text} • Type !yes or !no • "
                f"{duration:g} seconds • {self._tally(vote)}"
            )

    @staticmethod
    def _tally(vote):
        yes, no = tally(vote.votes, ("yes", "no"))
        return f"(Yes: {yes} | No: {no})"

    def cast(self, user, side):
        with self.lock:
            if not self.vote.active or time.monotonic() >= self.closes_at:
                return None
            if side not in ("yes", "no"):
                raise ValueError("Vote with !yes or !no.")
            user = cast_ballot(self.vote.votes, user, side)
            return f"{user} voted {side.upper()} • {self._tally(self.vote)}"

    def _announce_halfway(self, vote, help_text):
        with self.lock:
            if (self.vote is not vote or not vote.active or vote.halfway_announced
                    or time.monotonic() >= self.closes_at):
                return
            remaining = math.ceil(self.closes_at - time.monotonic())
            self.events.append(
                f"Vote halfway! {help_text} \u2022 Type !yes or !no \u2022 "
                f"{remaining} seconds remaining \u2022 {self._tally(vote)}"
            )
            vote.halfway_announced = True

    def _finish(self, vote, help_text, execute=None):
        with self.lock:
            if self.vote is not vote or not vote.active:
                return
            yes, no = tally(vote.votes, ("yes", "no"))
            vote.active = False
            if vote.halfway_timer:
                vote.halfway_timer.cancel()
            self.events.append(
                f"Vote {'passed' if yes > no else 'failed'}! ({yes}-{no}) • {help_text}"
            )
            try:
                if yes > no:
                    feedback = (execute or self.execute)(vote.command, vote.args)
                    if feedback:
                        self.events.append(feedback)
            except (OSError, ValueError, RuntimeError) as error:
                self.events.append(f"Could not apply voted command: {error}")
            finally:
                self.vote = VoteState()

    def drain_events(self):
        with self.lock:
            events = self.events
            self.events = []
            return events

    def close(self):
        with self.lock:
            if self.vote.timer:
                self.vote.timer.cancel()
            if self.vote.halfway_timer:
                self.vote.halfway_timer.cancel()
            self.vote = VoteState()
