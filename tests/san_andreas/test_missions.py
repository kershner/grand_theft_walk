"""Mission persistence, ballot rules, arrival and directed routing."""

import random
import tempfile
import unittest
from pathlib import Path

from core.missions import MissionSystem
from core.navigation import Node, PedestrianGraph, SidewalkNavigator
from core.files import atomic_json


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
        # Keep the small routing fixture's destinations outside the arrival area.
        atomic_json(self.root / "mission_config.json", {
            "radius": 3.0, "height_tolerance": 3.0,
        })
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
        self.state["camera_heading"] = 0
        self.tick(0)
        poll = self.system.data["poll"]
        self.assertEqual(len({d["name"] for d in poll["options"]}), 3)
        self.assertEqual(poll["closes"] - poll["opened"], 60)
        self.assertIn("ft away)", self.system.data["events"][-1]["text"])
        self.assertEqual(self.system.data["events"][-1]["text"].count("↑"), 1)
        self.assertEqual(self.system.data["events"][-1]["text"].count("←"), 1)
        self.assertEqual(self.system.data["events"][-1]["text"].count("→"), 1)
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

    def test_halfway_reminder_includes_options_time_and_latest_votes_once(self):
        self.state["camera_heading"] = 0
        self.tick(0)
        poll = self.system.data["poll"]
        self.tick(29.75)
        self.assertEqual(len(self.system.data["events"]), 1)
        self.ballot("alice", 2, 129)
        self.state["camera_heading"] = 3.141592653589793
        self.tick(30)
        reminders = [e["text"] for e in self.system.data["events"] if "halfway" in e["text"]]
        self.assertEqual(len(reminders), 1)
        self.assertIn("30 seconds remaining", reminders[0])
        self.assertIn("!vote 1/2/3", reminders[0])
        self.assertIn("1 vote)", reminders[0])
        self.assertEqual(reminders[0].count("↓"), 1)
        self.assertEqual(reminders[0].count("←"), 1)
        self.assertEqual(reminders[0].count("→"), 1)
        for option in poll["options"]:
            self.assertIn(option["name"], reminders[0])
        self.tick(31)
        self.assertEqual(len(self.system.data["events"]), 2)
        restored = MissionSystem(self.root, self.graph, self.destinations)
        restored.tick(self.state, 32, True, wall=132)
        self.assertEqual(restored.data["events"], [])

    def test_expired_poll_does_not_send_a_late_halfway_reminder(self):
        self.tick(0)
        self.tick(60)
        self.assertIsNotNone(self.system.data["active"])
        self.assertFalse(any("halfway" in e["text"] for e in self.system.data["events"]))

    def test_halfway_uses_the_poll_duration_instead_of_fixed_thirty_seconds(self):
        self.system.config["vote_seconds"] = 10
        self.tick(0)
        self.tick(4.75)
        self.assertEqual(len(self.system.data["events"]), 1)
        self.tick(5)
        self.assertIn("5 seconds remaining", self.system.data["events"][-1]["text"])
        self.assertTrue(self.system.data["poll"]["halfway_announced"])

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
        self.assertEqual(restored.standing_seconds, 0)
        self.assertEqual(restored.data["events"], [])

    def activate(self):
        self.system.data["active"] = self.destinations[0]
        self.state["position"] = {"x": 0, "y": 10, "z": 0}

    def test_larger_default_radius_keeps_height_check_and_custom_radius(self):
        (self.root / "mission_config.json").unlink()
        self.system = MissionSystem(self.root, self.graph, self.destinations)
        self.activate()
        self.assertEqual(self.system.config["radius"], 300)
        self.state["position"]["x"] = 300
        self.assertTrue(self.system.at_destination(self.state))
        self.state["position"]["x"] = 300.1
        self.assertFalse(self.system.at_destination(self.state))
        self.state["position"].update(x=20, z=30)
        self.assertTrue(self.system.at_destination(self.state))
        self.state["position"]["z"] = 30.1
        self.assertFalse(self.system.at_destination(self.state))
        atomic_json(self.root / "mission_config.json", {"radius": 50})
        self.system = MissionSystem(self.root, self.graph, self.destinations)
        self.assertEqual(self.system.config["radius"], 50)

    def test_vice_city_airport_height_difference_does_not_block_arrival(self):
        (self.root / "mission_config.json").unlink()
        self.system = MissionSystem(self.root, self.graph, self.destinations)
        self.system.data["active"] = {
            "name": "Escobar International: meeting point 36",
            "position": [-1340.25, -434.875, 6.75],
        }
        self.state["position"] = {
            "x": -1334.69873046875,
            "y": -444.1980285644531,
            "z": 10.95919132232666,
        }
        self.assertTrue(self.tick(0))
        self.assertIn("Mission location reached!", self.system.data["events"][0]["text"])
        self.tick(0.25)
        self.assertEqual(self.system.standing_seconds, 0.25)

    def test_arrival_announces_once_and_reannounces_after_restart_resets_hold(self):
        self.activate()
        self.state["position"]["x"] = 4
        self.tick(0)
        self.assertEqual(self.system.data["events"], [])
        self.state["position"]["x"] = 3
        self.state["move_state"] = 4
        self.tick(0.1)
        self.assertEqual(len(self.system.data["events"]), 1)
        self.assertEqual(
            self.system.data["events"][0]["text"],
            "Mission location reached!  Stay inside the area for 30 seconds.",
        )
        self.assertEqual(self.system.standing_seconds, 0)
        self.tick(0.2)
        self.state["position"]["x"] = 4
        self.tick(0.3)
        self.state["position"]["x"] = 0
        self.tick(0.4)
        self.assertEqual(len(self.system.data["events"]), 1)
        self.assertNotIn("arrival_announced", self.destinations[0])
        self.system = MissionSystem(self.root, self.graph, self.destinations)
        self.tick(0.5)
        self.assertEqual(len(self.system.data["events"]), 1)
        self.assertIn("Mission location reached!", self.system.data["events"][0]["text"])
        self.assertEqual(self.system.standing_seconds, 0)
        self.system.data["active"] = self.destinations[1]
        self.state["position"] = dict(zip("xyz", self.destinations[1]["position"]))
        self.system.config["hold_seconds"] = 5
        self.tick(0.6)
        self.assertIn("Stay inside the area for 5 seconds", self.system.data["events"][-1]["text"])

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

    def test_moving_inside_radius_counts_toward_completion(self):
        self.activate()
        self.state.update(standing=False, in_air=True, move_state=4)
        for i in range(122):
            self.state["position"]["x"] = 2 if i % 2 else -2
            self.tick(i / 4)
        self.assertEqual(self.system.data["completed"], 1)

    def test_leaving_radius_pause_loading_height_and_frame_gaps_reset_hold(self):
        cases = [
            ({"position": {"x": 4, "y": 10, "z": 0}}, True, False),
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
                self.assertGreater(self.system.standing_seconds, 0)
                self.state.update(changed)
                self.tick(0.5, usable, saving=saving)
                self.assertEqual(self.system.standing_seconds, 0)
        self.create_mission()
        self.activate()
        self.tick(0)
        self.tick(0.1)
        self.tick(10)
        self.assertEqual(self.system.standing_seconds, 0)
        self.state["position"]["x"] = 0.5
        self.tick(10.1)
        self.assertAlmostEqual(self.system.standing_seconds, 0.1)

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
