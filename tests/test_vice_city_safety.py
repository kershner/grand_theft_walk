"""Offline guards against repeating the interrupted live camera experiment."""

import struct
import unittest
from unittest.mock import Mock, patch

from gtw.games import get_game
from gtw.supervisor import watch
from gtw.winmemory import Memory as ProcessMemory
from vice_city.game import CONNECTIONS, COUNTS, Memory, read_graph


class ReadOnlyTests(unittest.TestCase):
    def test_consolidated_adapter_preserves_pedestrian_links_and_crossings(self):
        data = bytearray(COUNTS + 12)
        struct.pack_into("<3I", data, COUNTS, 2, 0, 2)
        for index in range(2):
            at = index * 20
            struct.pack_into("<3h", data, at + 4, index * 80, 0, 0)
            struct.pack_into("<H", data, at + 12, index)
            data[at + 16] = 1
            struct.pack_into("<H", data, CONNECTIONS + index * 2, (1 - index) | 0x8000)
        graph = read_graph(data)
        self.assertEqual(graph.nodes[(0, 1)].position, (10, 0, 0))
        self.assertEqual(graph.nodes[(0, 0)].links, (((0, 1), True),))

    def test_vice_city_is_not_marked_controller_verified(self):
        profile = get_game("vice_city")
        self.assertIsNone(profile.verified_executable)
        self.assertIsNotNone(profile.inspection_executable)

    def test_shared_api_rejects_vice_city_writes_before_opening_a_process(self):
        with patch("gtw.winmemory.k32.OpenProcess") as open_process:
            with self.assertRaisesRegex(RuntimeError, "writes are not enabled"):
                ProcessMemory(writable=True, pid=123, game="vice_city")
            open_process.assert_not_called()

    def test_vice_city_api_rejects_writable_handles_before_attaching(self):
        with patch("vice_city.game.ProcessMemory.__init__") as attach:
            with self.assertRaisesRegex(RuntimeError, "writes are disabled"):
                Memory(writable=True)
            attach.assert_not_called()

    def test_vice_city_write_method_is_disabled(self):
        memory = Memory.__new__(Memory)
        with self.assertRaisesRegex(RuntimeError, "read-only"):
            memory.write(0x7E3D90, b"\x29\0")


class LaunchWatchdogTests(unittest.TestCase):
    def test_deadline_terminates_only_the_owned_child(self):
        process = Mock()
        process.poll.return_value = None
        reason = watch(process, 10, lambda: False, clock=Mock(side_effect=[0, 11]), sleep=Mock())
        self.assertEqual(reason, "timeout")
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)

    def test_f12_rearms_after_release_and_stops_the_owned_child(self):
        process = Mock()
        process.poll.return_value = None
        reason = watch(process, 120, Mock(side_effect=[True, False, True]), clock=lambda: 0, sleep=Mock())
        self.assertEqual(reason, "f12")
        process.kill.assert_called_once_with()

    def test_normal_exit_does_not_terminate_a_process(self):
        process = Mock()
        process.poll.return_value = 0
        self.assertEqual(watch(process, 120, Mock(), clock=lambda: 0), "exited")
        process.kill.assert_not_called()
