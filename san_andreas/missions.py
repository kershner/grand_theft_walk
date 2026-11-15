"""Controller-owned, persistent missions and viewer ballots."""

import json
import math
import random
import time
import uuid
import zlib
from collections import defaultdict
from pathlib import Path

from runtime import atomic_json, read_json
from voting import cast_ballot, tally

DEFAULT_CONFIG = {
    "hold_seconds": 30.0,
    "vote_seconds": 60.0,
    "radius": 3.0,
    "height_tolerance": 3.0,
}
BALLOT_CHOICES = (1, 2, 3)
POLL_INTERVAL = 0.25
STANDING_DRIFT = 0.3
MAX_FRAME_GAP = 0.5


def load_config(path):
    if not path.exists():
        atomic_json(path, DEFAULT_CONFIG)
    config = DEFAULT_CONFIG | read_json(path)
    for key in DEFAULT_CONFIG:
        value = config[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(
                f"mission_config.json: {key} must be a positive finite number"
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
    def __init__(self, state_dir, graph, destinations, rng=None):
        self.root = Path(state_dir)
        self.graph = graph
        self.destinations = destinations
        self.rng = rng or random.Random()
        self.config = load_config(self.root / "mission_config.json")
        try:
            self.data = read_json(self.root / "mission.json")
        except FileNotFoundError:
            self.data = {"active": None, "poll": None, "completed": 0, "events": []}
        self.held = 0.0
        self.last_tick = None
        self.anchor = None
        self.next_io = 0.0
        self.error = None
        self.saved_payload = None
        if self.data["active"]:
            self.data["poll"] = None

    def save(self):
        payload = json.dumps(self.data, sort_keys=True)
        if payload != self.saved_payload:
            atomic_json(self.root / "mission.json", self.data)
            self.saved_payload = payload

    def event(self, text):
        self.data["events"].append({"id": uuid.uuid4().hex, "text": text})
        self.data["events"] = self.data["events"][-100:]

    def eligible(self, state):
        if not self.graph or state.get("interior") != 0:
            return []
        position = tuple(state["position"][axis] for axis in ("x", "y", "z"))
        start = self.graph.nearest(position)
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
        for path in (self.root / "mission_votes").glob("*.json"):
            try:
                ballot = read_json(path)
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
            path.unlink(missing_ok=True)

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

    def _update_standing(self, state, now, standing):
        elapsed = 0 if self.last_tick is None else now - self.last_tick
        self.last_tick = now
        if not standing:
            self.anchor = None
            self.held = 0.0
            return
        position = tuple(state["position"][axis] for axis in ("x", "y", "z"))
        if (
            self.anchor is None
            or math.dist(position, self.anchor) > STANDING_DRIFT
            or not 0 <= elapsed <= MAX_FRAME_GAP
        ):
            self.anchor = position
            self.held = 0.0
        else:
            self.held += elapsed

    def _complete_mission(self):
        name = self.data["active"]["name"]
        self.event(f"Mission complete: {name}!")
        self.data["last_completed"] = name
        self.data["completed"] += 1
        self.data["active"] = None
        self.anchor = None
        self.held = 0.0
        self.next_io = 0.0
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
            f"Stand at the meeting point for {self.config['hold_seconds']:g} seconds."
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
            f"{index}: {option['name']}" for index, option in enumerate(options, 1)
        )
        self.event(
            f"Choose our next mission! {labels} • !vote 1/2/3 • {self.config['vote_seconds']:g} seconds"
        )

    def tick(self, state, now, usable, saving=False, wall=None):
        wall = time.time() if wall is None else wall
        arrived = self.at_destination(state)
        standing = (
            arrived
            and usable
            and not saving
            and state.get("standing")
            and not state.get("in_air")
            and state.get("move_state") in (0, 1)
        )
        self._update_standing(state, now, standing)
        if self.data["active"] and self.held >= self.config["hold_seconds"]:
            self._complete_mission()
        if now >= self.next_io:
            self.next_io = now + POLL_INTERVAL
            self.drain_votes()
            if not self.data["active"]:
                poll = self.data["poll"]
                if poll and wall >= poll["closes"]:
                    self._select_mission(poll)
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
            "held_seconds": round(self.held, 1),
            "completed": self.data["completed"],
            "events": self.data["events"],
            "available_locations": len(self.destinations),
            "error": self.error,
        }
