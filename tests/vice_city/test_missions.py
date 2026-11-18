"""Vice City destination reachability and the shared viewer mission cycle."""

import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.missions import MissionSystem
from core.navigation import Node, PedestrianGraph, SidewalkNavigator
from core.files import atomic_json
from games.vice_city.game import load_destinations, main_sidewalk_network


def graph_fixture():
    # Six mutually reachable street points, one duplicate position, an isolated
    # courtyard, and a one-way dead end that must never become a destination.
    nodes = [Node((0, i), (i * 20, 0, 10),
                  tuple(((0, j), False) for j in (i - 1, i + 1) if 0 <= j < 6))
             for i in range(6)]
    nodes[0] = Node((0, 0), nodes[0].position, nodes[0].links + (((0, 6), False),))
    nodes[5] = Node((0, 5), nodes[5].position, nodes[5].links + (((0, 8), False),))
    nodes.extend([
        Node((0, 6), (0, 0, 10), (((0, 0), False),)),
        Node((0, 7), (50, 1, 10), (((0, 10), False),)),
        Node((0, 8), (120, 0, 10), (((0, 9), False),)),
        Node((0, 9), (140, 0, 10), (((0, 8), False),)),
        Node((0, 10), (50, 2, 10), (((0, 7), False),)),
    ])
    return PedestrianGraph(nodes)


def destinations(graph):
    with patch("games.vice_city.game.load_zones", return_value=[
        ("Ocean Beach", (-10, -10, 0, 60, 10, 20)),
        ("Washington Beach", (60, -10, 0, 110, 10, 20)),
    ]):
        return load_destinations(Path("game"), graph, Mock(return_value={}))


class ViceCityMissionTests(unittest.TestCase):
    def test_every_distinct_main_network_point_is_available_and_routable(self):
        graph = graph_fixture()
        catalog = destinations(graph)
        self.assertEqual(main_sidewalk_network(graph), {(0, i) for i in range(7)})
        self.assertEqual(len(catalog), 6)
        self.assertEqual(len({tuple(d["position"]) for d in catalog}), 6)
        self.assertEqual(len({d["name"] for d in catalog}), 6)
        self.assertTrue(any("meeting point" in d["name"] for d in catalog))
        for destination in catalog:
            self.assertIn((0, 0), graph.distances_to(tuple(destination["node"])))
        self.assertEqual(catalog, destinations(graph))

    def test_empty_graph_has_no_destinations(self):
        self.assertEqual(destinations(PedestrianGraph([])), [])

    def test_vote_route_restart_arrival_and_next_poll(self):
        graph = graph_fixture()
        catalog = destinations(graph)
        state = {"ready": True, "interior": 0, "standing": True,
                 "in_air": False, "move_state": 1,
                 "position": {"x": 0, "y": 0, "z": 10}}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            atomic_json(root / "mission_config.json", {
                "hold_seconds": 1, "vote_seconds": 1, "radius": 3.0,
            })
            system = MissionSystem(root, graph, catalog, random.Random(1))
            system.tick(state, 0, True, wall=100)
            poll = system.data["poll"]
            self.assertEqual(len(poll["options"]), 3)
            for option in poll["options"]:
                self.assertIn((0, 0), graph.distances_to(tuple(option["node"])))
            atomic_json(root / "mission_votes" / "viewer.json", {
                "poll": poll["id"], "viewer": "alice", "choice": 2, "issued": 100.5,
            })
            system.tick(state, 1, True, wall=101)
            selected = poll["options"][1]
            self.assertEqual(system.data["active"], selected)
            navigator = SidewalkNavigator(graph)
            navigator.set_destination(tuple(selected["node"]))
            navigator.update_route()
            self.assertIn((0, 0), navigator.route_costs)
            system = MissionSystem(root, graph, catalog)
            self.assertEqual(system.data["active"], selected)
            state["position"] = dict(zip("xyz", selected["position"]))
            for step in range(13):
                system.tick(state, 2 + step / 10, True, wall=102 + step / 10)
            self.assertEqual(system.data["completed"], 1)
            self.assertIsNone(system.data["active"])
            self.assertEqual(len(system.data["poll"]["options"]), 3)
            self.assertNotIn(selected, system.data["poll"]["options"])

    def test_current_player_must_have_a_route_to_poll_options(self):
        graph = graph_fixture()
        with tempfile.TemporaryDirectory() as folder:
            system = MissionSystem(folder, graph, destinations(graph))
            state = {"interior": 0, "position": {"x": 120, "y": 0, "z": 10}}
            self.assertEqual(system.eligible(state), [])

    def test_completion_off_sidewalk_opens_next_vote_with_large_arrival_area(self):
        graph = PedestrianGraph([
            Node((0, i), (i * 20, 0, 6.75),
                 tuple(((0, j), False) for j in (i - 1, i + 1) if 0 <= j < 100))
            for i in range(100)
        ])
        catalog = [
            {"name": f"Area {i}", "node": [0, i], "position": [i * 20, 0, 6.75]}
            for i in (0, 40, 60, 80, 99)
        ]
        state = {"ready": True, "interior": 0,
                 "position": {"x": 0, "y": 10, "z": 14.86}}
        self.assertIsNone(graph.nearest((0, 10, 14.86)))
        with tempfile.TemporaryDirectory() as folder:
            system = MissionSystem(folder, graph, catalog, random.Random(1))
            system.data["active"] = catalog[0]
            for step in range(122):
                system.tick(state, step / 4, True, wall=100 + step / 4)
            self.assertEqual(system.data["completed"], 1)
            self.assertIsNotNone(system.data["poll"])
            self.assertEqual(len(system.data["poll"]["options"]), 3)
            messages = [event["text"] for event in system.data["events"]]
            self.assertIn("Mission location reached!", messages[0])
            self.assertIn("Mission complete:", messages[1])
            self.assertIn("Choose our next mission!", messages[2])
            self.assertIsNone(system.error)


if __name__ == "__main__":
    unittest.main()
