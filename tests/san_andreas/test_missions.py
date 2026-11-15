"""Mission persistence, ballot rules, arrival and directed routing."""

import random
import tempfile
import unittest
from pathlib import Path

from missions import MissionSystem
from navigation import Node, PedestrianGraph, SidewalkNavigator
from runtime import atomic_json


def graph_fixture():
    positions = [(0, 0, 0), (0, 10, 0), (-10, 0, 0), (10, 0, 0), (0, 20, 0)]
    links = [[1, 2, 3], [0, 4], [0], [0], [1]]
    return PedestrianGraph(
        [
            Node((0, i), p, tuple(((0, j), False) for j in links[i]))
            for i, p in enumerate(positions)
        ]
    )


class MissionTests(unittest.TestCase):
    def setUp(self):
        self.create_mission()

    def create_mission(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.graph = graph_fixture()
        self.destinations = [
            {"name": f"Area {i}", "node": [0, i], "position": list(n.position)}
            for i, n in enumerate(self.graph.nodes.values())
            if i
        ]
        self.system = MissionSystem(
            self.root, self.graph, self.destinations, random.Random(1)
        )
        self.state = {
            "ready": True,
            "interior": 0,
            "position": {"x": 0, "y": 0, "z": 0},
            "standing": True,
            "in_air": False,
            "move_state": 1,
        }

    def tick(self, now, usable=True, **kwargs):
        return self.system.tick(self.state, now, usable, wall=100 + now, **kwargs)

    def ballot(self, viewer, choice, issued, poll=None):
        path = self.root / "mission_votes" / f"{random.random()}.json"
        atomic_json(
            path,
            {
                "viewer": viewer,
                "choice": choice,
                "issued": issued,
                "poll": poll or self.system.data["poll"]["id"],
            },
        )

    def test_three_options_sixty_seconds_latest_vote_and_one_active(self):
        self.tick(0)
        poll = self.system.data["poll"]
        self.assertEqual(len({d["name"] for d in poll["options"]}), 3)
        self.assertEqual(poll["closes"] - poll["opened"], 60)
        self.assertIn("ft away)", self.system.data["events"][-1]["text"])
        self.ballot("Alice", 1, 101)
        self.ballot("alice", 2, 102)
        self.ballot("bob", 2, 103)
        self.ballot("late", 3, 160)
        self.ballot("wrong", 3, 104, "old-round")
        self.tick(1)
        self.assertEqual(self.system.report()["poll"]["counts"], [0, 2, 0])
        self.ballot("ALICE", 1, 100.5)
        self.tick(2)
        self.assertEqual(self.system.report()["poll"]["counts"], [0, 2, 0])
        self.tick(60)
        self.assertEqual(self.system.data["active"], poll["options"][1])
        self.assertIsNone(self.system.data["poll"])
        active = self.system.data["active"]
        self.tick(10000)
        self.assertEqual(active, self.system.data["active"])

    def test_no_votes_and_ties_choose_only_a_winning_option(self):
        self.tick(0)
        options = self.system.data["poll"]["options"]
        self.tick(60)
        self.assertIn(self.system.data["active"], options)
        self.system.data["active"] = None
        self.tick(61)
        options = self.system.data["poll"]["options"]
        self.ballot("a", 1, 162)
        self.ballot("b", 3, 162)
        self.tick(121)
        self.assertIn(self.system.data["active"], (options[0], options[2]))

    def test_restart_preserves_mission_poll_and_viewer_votes(self):
        self.tick(0)
        self.ballot("alice", 3, 101)
        self.tick(1)
        restored = MissionSystem(self.root, self.graph, self.destinations)
        self.assertEqual(restored.report()["poll"]["counts"], [0, 0, 1])
        self.tick(60)
        restored = MissionSystem(self.root, self.graph, self.destinations)
        self.assertEqual(restored.data["active"], self.system.data["active"])
        self.assertEqual(restored.held, 0)

    def activate(self):
        self.system.data["active"] = self.destinations[0]
        self.state["position"] = {"x": 0, "y": 10, "z": 0}

    def test_continuous_thirty_seconds_completes_once_and_starts_next_poll(self):
        self.activate()
        for i in range(300):
            self.assertTrue(self.tick(i / 10))
        self.assertEqual(self.system.data["completed"], 0)
        self.assertFalse(self.tick(30.1))
        self.assertEqual(self.system.data["completed"], 1)
        self.assertIsNone(self.system.data["active"])
        self.assertEqual(len(self.system.data["poll"]["options"]), 3)
        self.assertEqual(
            sum("Mission complete" in e["text"] for e in self.system.data["events"]), 1
        )

    def test_movement_pause_loading_height_and_frame_gaps_reset_hold(self):
        cases = [
            ({"move_state": 4}, True, False),
            ({"in_air": True}, True, False),
            ({"interior": 1}, True, False),
            ({"ready": False}, True, False),
            ({}, False, False),
            ({}, True, True),
            ({"position": {"x": 0, "y": 10, "z": 10}}, True, False),
        ]
        for changed, usable, saving in cases:
            with self.subTest(changed=changed, usable=usable, saving=saving):
                self.create_mission()
                self.activate()
                self.tick(0)
                self.tick(0.25)
                self.assertGreater(self.system.held, 0)
                self.state.update(changed)
                self.tick(0.5, usable, saving=saving)
                self.assertEqual(self.system.held, 0)
        self.create_mission()
        self.activate()
        self.tick(0)
        self.tick(0.1)
        self.tick(10)
        self.assertEqual(self.system.held, 0)
        self.state["position"]["x"] = 0.5
        self.tick(10.1)
        self.assertEqual(self.system.held, 0)

    def test_configurable_duration_and_invalid_config(self):
        atomic_json(self.root / "mission_config.json", {"hold_seconds": 1})
        self.system = MissionSystem(self.root, self.graph, self.destinations)
        self.activate()
        for i in range(12):
            self.tick(i / 10)
        self.assertEqual(self.system.data["completed"], 1)
        for value in (-1, True, "30", float("inf")):
            atomic_json(self.root / "mission_config.json", {"hold_seconds": value})
            with self.assertRaises(ValueError):
                MissionSystem(self.root, self.graph, self.destinations)


class MissionRoutingTests(unittest.TestCase):
    def test_shortest_route_resumes_after_viewer_detour_and_mode_reset(self):
        navigator = SidewalkNavigator(graph_fixture())
        navigator.set_destination((0, 4))
        navigator.turn = "right"
        self.assertEqual(navigator.waypoint((0, 0, 0), 0, 0, 0), (10, 0, 0))
        self.assertIsNone(navigator.turn)
        self.assertEqual(navigator.waypoint((10, 0, 0), 0, 0, 1), (0, 0, 0))
        self.assertEqual(navigator.waypoint((0, 0, 0), 0, 0, 2), (0, 10, 0))
        navigator.reset()
        self.assertEqual(navigator.destination, (0, 4))
        self.assertEqual(navigator.waypoint((0, 10, 0), 0, 0, 3), (0, 20, 0))
        self.assertEqual(navigator.waypoint((0, 20, 0), 0, 0, 4), (0, 20, 0))

    def test_directed_routes_and_blocked_edges(self):
        graph = graph_fixture()
        costs = graph.distances_to((0, 4))
        self.assertEqual(costs[(0, 2)], 30)
        self.assertNotIn((0, 0), graph.distances_to((0, 4), {((0, 1), (0, 4))}))
        graph = PedestrianGraph(
            [
                Node((0, 0), (0, 0, 0), (((0, 1), False),)),
                Node((0, 1), (10, 0, 0), (((0, 1), False),)),
            ]
        )
        self.assertNotIn((0, 1), graph.distances_to((0, 0)))


if __name__ == "__main__":
    unittest.main()
