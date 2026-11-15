"""Viewer ballots and timed command votes with a strict yes majority."""

import threading
import time
from collections import deque
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


class CommandVoting:
    def __init__(self, execute, duration=20, timer_factory=threading.Timer):
        self.execute = execute
        self.duration = duration
        self.timer_factory = timer_factory
        self.vote = VoteState()
        self.lock = threading.Lock()
        self.events = deque()
        self.closes_at = 0.0

    def start(self, name, args, starter, help_text):
        with self.lock:
            if self.vote.active:
                return "Vote already in progress."
            vote = VoteState(active=True, command=name, args=list(args))
            cast_ballot(vote.votes, starter, "yes")
            self.vote = vote
            self.closes_at = time.monotonic() + self.duration
            # A thread timer survives Flask's per-request asyncio event loop.
            timer = self.timer_factory(
                self.duration, lambda: self._finish(vote, help_text)
            )
            timer.daemon = True
            vote.timer = timer
            timer.start()
            return (
                f"Vote started! {help_text} • Type !yes or !no • "
                f"{self.duration:g} seconds • {self._tally(vote)}"
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

    def _finish(self, vote, help_text):
        with self.lock:
            if self.vote is not vote or not vote.active:
                return
            yes, no = tally(vote.votes, ("yes", "no"))
            vote.active = False
            self.events.append(
                f"Vote {'passed' if yes > no else 'failed'}! ({yes}-{no}) • {help_text}"
            )
            try:
                if yes > no:
                    feedback = self.execute(vote.command, vote.args)
                    if feedback:
                        self.events.append(feedback)
            except (OSError, ValueError, RuntimeError) as error:
                self.events.append(f"Could not apply voted command: {error}")
            finally:
                self.vote = VoteState()

    def drain_events(self):
        with self.lock:
            events = list(self.events)
            self.events.clear()
            return events

    def close(self):
        with self.lock:
            if self.vote.timer:
                self.vote.timer.cancel()
            self.vote = VoteState()
