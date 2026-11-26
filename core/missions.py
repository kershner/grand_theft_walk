"""Controller-owned missions, viewer ballots, and timed command votes."""

import math
import random
import threading
import time
import uuid
import zlib
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from core.files import write_state, read_state, state_exists, delete_state, state_paths
from core.runtime import mission_distance


DEFAULT_CONFIG = {
    "hold_seconds": 30.0,
    "vote_seconds": 60.0,
    "radius": 300.0,
    "height_tolerance": 30.0,
}
BALLOT_CHOICES = (1, 2, 3)
POLL_INTERVAL = 0.25
MAX_FRAME_GAP = 0.5


def load_config(path, defaults=None):
    defaults = DEFAULT_CONFIG | (defaults or {})
    if not state_exists(path):
        write_state(path, defaults)
    config = defaults | read_state(path)
    for key in DEFAULT_CONFIG:
        value = config[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(
                f"mission_config: {key} must be a positive finite number"
            )
    return config


def load_destinations(game_directory, graph, read_text):
    """Use distinct named areas, with a meeting point on the actual path graph."""
    root = Path(game_directory)
    names = read_text(root / "text" / "american.gxt")
    zones = []
    for line in (root / "data" / "info.zon").read_text().splitlines():
        fields = [part.strip() for part in line.split(",")]
        if len(fields) != 10:
            continue
        bounds = tuple(float(value) for value in fields[2:8])
        name = names.get(zlib.crc32(fields[9].upper().encode("ascii")) ^ 0xFFFFFFFF)
        if name and name != "San Andreas":
            area = (bounds[3] - bounds[0]) * (bounds[4] - bounds[1])
            zones.append((area, bounds, name))
    zones.sort(key=lambda zone: zone[0])
    groups = defaultdict(list)
    for node in graph.nodes.values():
        x, y, z = node.position
        for _, bounds, name in zones:
            x1, y1, z1, x2, y2, z2 = bounds
            if x1 <= x <= x2 and y1 <= y <= y2 and z1 <= z <= z2:
                groups[name].append(node)
                break
    result = []
    for name, nodes in sorted(groups.items()):
        center = tuple(
            sum(node.position[i] for node in nodes) / len(nodes) for i in range(2)
        )
        node = min(nodes, key=lambda node: math.dist(node.position[:2], center))
        result.append(
            {"name": name, "node": list(node.key), "position": list(node.position)}
        )
    return result


class MissionSystem:
    def __init__(self, state_dir, graph, destinations, rng=None, *, defaults=None):
        self.state_dir = Path(state_dir)
        self.graph = graph
        self.destinations = destinations
        self.rng = rng or random.Random()
        self.config = load_config(self.state_dir / "mission_config", defaults)
        try:
            self.data = read_state(self.state_dir / "mission")
        except FileNotFoundError:
            self.data = {"active": None, "poll": None, "completed": 0, "events": []}
        self.data["events"] = []
        self.standing_seconds = 0.0
        self.last_tick_at = None
        self.holding = False
        self.next_vote_check_at = 0.0
        self.error = None
        self.saved_state = None
        if self.data["active"]:
            self.data["poll"] = None
            # A restarted controller begins a new hold and must announce it again.
            self.data["active"].pop("arrival_announced", None)
            self.data["active"].pop("arrival_event_id", None)

    def save(self):
        if self.data != self.saved_state:
            write_state(self.state_dir / "mission", self.data)
            self.saved_state = deepcopy(self.data)

    def event(self, text):
        self.data["events"].append({"id": uuid.uuid4().hex, "text": text})
        self.data["events"] = self.data["events"][-100:]
        return self.data["events"][-1]["id"]

    def eligible(self, state):
        if not self.graph or state.get("interior") != 0:
            return []
        position = tuple(state["position"][axis] for axis in ("x", "y", "z"))
        start = self.graph.nearest(position)
        if start is None:
            # Free roaming can finish a mission away from the narrow sidewalk
            # snap distance. Use the same area limits as mission arrival.
            start = min(
                (
                    node for node in self.graph.nodes.values()
                    if math.dist(node.position[:2], position[:2]) <= self.config["radius"]
                    and abs(node.position[2] - position[2]) <= self.config["height_tolerance"]
                ),
                key=lambda node: math.dist(node.position, position),
                default=None,
            )
        if start is None:
            return []
        reachable, pending = {start.key}, [start.key]
        while pending:
            for key, _ in self.graph.nodes[pending.pop()].links:
                if key in self.graph.nodes and key not in reachable:
                    reachable.add(key)
                    pending.append(key)
        return [
            destination
            for destination in self.destinations
            if tuple(destination["node"]) in reachable
            and math.dist(destination["position"], position) > self.config["radius"] * 2
            and destination["name"] != self.data.get("last_completed")
        ]

    def drain_votes(self):
        poll = self.data["poll"]
        consumed = []
        for path in state_paths(self.state_dir / "mission_votes"):
            try:
                ballot = read_state(path)
            except (ValueError, OSError):
                continue
            consumed.append(path)
            if not poll or not isinstance(ballot, dict):
                continue
            issued = ballot.get("issued")
            choice = ballot.get("choice")
            viewer = ballot.get("viewer")
            if not (
                ballot.get("poll") == poll["id"]
                and type(issued) in (int, float)
                and math.isfinite(issued)
                and poll["opened"] <= issued < poll["closes"]
                and type(choice) is int
                and choice in BALLOT_CHOICES
                and isinstance(viewer, str)
                and viewer.strip()
            ):
                continue
            viewer = viewer.strip().casefold()
            vote_times = poll.setdefault("vote_times", {})
            if issued >= vote_times.get(viewer, 0):
                cast_ballot(poll["votes"], viewer, choice)
                vote_times[viewer] = issued
        # Persist before removing ballots; replay after a crash is idempotent.
        if consumed:
            self.save()
        for path in consumed:
            delete_state(path)

    def at_destination(self, state):
        active = self.data["active"]
        if not active or not state.get("ready") or state.get("interior") != 0:
            return False
        position = state["position"]
        target_x, target_y, target_z = active["position"]
        return (
            math.hypot(position["x"] - target_x, position["y"] - target_y)
            <= self.config["radius"]
            and abs(position["z"] - target_z) <= self.config["height_tolerance"]
        )

    def _update_standing(self, now, holding):
        elapsed = 0 if self.last_tick_at is None else now - self.last_tick_at
        self.last_tick_at = now
        if not holding:
            self.holding = False
            self.standing_seconds = 0.0
            return
        if (
            not self.holding
            or not 0 <= elapsed <= MAX_FRAME_GAP
        ):
            self.standing_seconds = 0.0
        else:
            self.standing_seconds += elapsed
        self.holding = True

    def _complete_mission(self):
        name = self.data["active"]["name"]
        self.event(f"Mission complete: {name}!")
        self.data["last_completed"] = name
        self.data["completed"] += 1
        self.data["active"] = None
        self.holding = False
        self.standing_seconds = 0.0
        self.next_vote_check_at = 0.0
        self.save()

    def _select_mission(self, poll):
        counts = tally(poll["votes"], BALLOT_CHOICES)
        highest_count = max(counts)
        winner = self.rng.choice(
            [index for index, count in enumerate(counts) if count == highest_count]
        )
        mission = poll["options"][winner]
        self.data["active"] = mission
        self.data["poll"] = None
        self.event(
            f"Mission selected: {mission['name']} ({highest_count} votes). "
            f"Stay inside the mission area for {self.config['hold_seconds']:g} seconds."
        )

    def _open_poll(self, state, wall):
        destinations = self.eligible(state)
        if len(destinations) < len(BALLOT_CHOICES):
            self.error = "Waiting for at least three reachable named mission areas."
            return
        options = self.rng.sample(destinations, len(BALLOT_CHOICES))
        self.data["poll"] = {
            "id": uuid.uuid4().hex,
            "opened": wall,
            "closes": wall + self.config["vote_seconds"],
            "options": options,
            "votes": {},
        }
        self.error = None
        labels = " • ".join(
            f"{index}: {option['name']} ({mission_distance(state, option)})"
            for index, option in enumerate(options, 1)
        )
        self.event(
            f"Choose our next mission! {labels} • !vote 1/2/3 • {self.config['vote_seconds']:g} seconds"
        )

    def _announce_halfway(self, state, poll, wall):
        counts = tally(poll["votes"], BALLOT_CHOICES)
        labels = " • ".join(
            f"{index}: {option['name']} ({mission_distance(state, option)}; {count} {'vote' if count == 1 else 'votes'})"
            for index, (option, count) in enumerate(zip(poll["options"], counts), 1)
        )
        remaining = math.ceil(poll["closes"] - wall)
        self.event(f"Mission vote halfway! {labels} • !vote 1/2/3 • {remaining} seconds remaining")
        poll["halfway_announced"] = True

    def tick(self, state, now, usable, saving=False, wall=None):
        wall = time.time() if wall is None else wall
        arrived = self.at_destination(state)
        if arrived and not self.data["active"].get("arrival_announced"):
            arrival_event_id = self.event(
                "Mission location reached!  "
                f"Stay inside the area for {self.config['hold_seconds']:g} seconds."
            )
            self.data["active"] = dict(
                self.data["active"], arrival_announced=True,
                arrival_event_id=arrival_event_id,
            )
            self.save()
        self._update_standing(now, arrived and usable and not saving)
        if self.data["active"] and self.standing_seconds >= self.config["hold_seconds"]:
            self._complete_mission()
        if now >= self.next_vote_check_at:
            self.next_vote_check_at = now + POLL_INTERVAL
            self.drain_votes()
            if not self.data["active"]:
                poll = self.data["poll"]
                if poll and wall >= poll["closes"]:
                    self._select_mission(poll)
                elif (poll and not poll.get("halfway_announced")
                      and wall >= (poll["opened"] + poll["closes"]) / 2):
                    self._announce_halfway(state, poll, wall)
                elif not poll and usable:
                    self._open_poll(state, wall)
            self.save()
        return arrived and self.data["active"] is not None

    def report(self):
        poll = self.data["poll"]
        if poll:
            poll = {key: poll[key] for key in ("id", "closes", "options")} | {
                "counts": tally(poll["votes"], BALLOT_CHOICES)
            }
        return {
            "active": self.data["active"],
            "poll": poll,
            "hold_seconds": self.config["hold_seconds"],
            "held_seconds": round(self.standing_seconds, 1),
            "completed": self.data["completed"],
            "events": self.data["events"],
            "available_locations": len(self.destinations),
            "error": self.error,
        }


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
