"""Offline guards against repeating the interrupted live camera experiment."""

import struct
import unittest
from dataclasses import replace
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

    def test_vice_city_uses_the_pinned_inspected_runtime_for_controller_writes(self):
        profile = get_game("vice_city")
        self.assertEqual(profile.verified_executable, profile.inspection_executable)
        self.assertIsNotNone(profile.inspection_executable)

    def test_shared_api_rejects_vice_city_writes_before_opening_a_process(self):
        with patch("gtw.winmemory.k32.OpenProcess") as open_process, patch(
            "gtw.winmemory.get_game", return_value=replace(get_game("vice_city"), verified_executable=None)
        ):
            with self.assertRaisesRegex(RuntimeError, "writes are not enabled"):
                ProcessMemory(writable=True, pid=123, game="vice_city")
            open_process.assert_not_called()

    def test_vice_city_api_rejects_unknown_runtime_signatures_and_closes(self):
        with patch("vice_city.game.ProcessMemory.__init__") as attach, patch.object(
            Memory, "read", return_value=b"nope"
        ), patch.object(Memory, "close") as close:
            with self.assertRaisesRegex(RuntimeError, "signature does not match"):
                Memory(writable=True)
            attach.assert_called_once_with(writable=True, pid=None, game="vice_city")
            close.assert_called_once_with()

    def test_vice_city_write_method_is_disabled(self):
        memory = Memory.__new__(Memory)
        memory.writable = False
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
