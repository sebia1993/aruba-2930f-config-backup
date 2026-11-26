"""Exercise the real bounded SSH read path with an in-memory fake channel."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from localweb.ping_ssh import PingSSHSession
from localweb.ping_tools import PingPlan

from aruba2930f_backup.hostkeys import HostKeyStore
from aruba2930f_backup.models import CollectionFailure, CollectionOptions, Credentials, ErrorCode

_VLANS = """
 VLAN ID Name | Status Voice Jumbo
 ------- ---- + ------ ----- -----
 10 Management | Port-based No No
"""
_IP = """
 VLAN | IP Config IP Address Subnet Mask Proxy ARP
 ---- + --------- ---------- ----------- ---------
 Management | Manual 192.0.2.11 255.255.255.0 No
"""
_PING = "ping 198.51.100.10 repetitions 1 timeout 2 source 192.0.2.11"


class Channel:
    def __init__(self):
        self.replies = {
            "show version": "Aruba JL253A 2930F\nSoftware revision WC.16.11.0010",
            "show modules": "Chassis: Aruba JL253A 2930F",
            "show vlans": _VLANS,
            "show ip": _IP,
            _PING: "198.51.100.10 is alive, time = 1 ms",
        }
        self.commands = []
        self.pending = []
        self.closed = False
        self.incomplete = None
        self.on_write = None

    def clear_buffer(self):
        self.pending.clear()

    def normalize_cmd(self, command):
        return command + "\n"

    def write_channel(self, wire):
        command = wire.rstrip("\n")
        self.commands.append(command)
        output = self.replies[command]
        response = f"{command}\n{output}\n"
        if self.incomplete != command:
            response += "edge-lab#"
        self.pending = [response]
        if self.on_write:
            self.on_write(command)

    def read_channel(self):
        return self.pending.pop(0) if self.pending else ""

    def disconnect(self):
        self.closed = True


class PingSSHTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.channel = Channel()
        self.now = 0.0
        self.plan = self.make_plan()
        self.session = PingSSHSession(
            self.plan.targets[0],
            Credentials("operator", "synthetic-only"),
            CollectionOptions(command_timeout_seconds=30),
            HostKeyStore(Path(self.temp.name) / "known_hosts.json"),
            clock=lambda: self.now,
            sleeper=self.advance,
        )
        # Only the in-memory channel is installed. No connect/authentication,
        # actual host-key approval or real network access occurs in these tests.
        self.session._connection = self.channel
        self.session._prompt = "edge-lab#"
        self.session._paging_prepared = True
        self.session._terminal_width_prepared = True
        self.addCleanup(self.session.close)

    def advance(self, seconds):
        self.now += seconds

    @staticmethod
    def make_plan(**options):
        return PingPlan.parse(
            {
                "targets": "192.0.2.11",
                "destinations": [{"ip": "198.51.100.10"}],
                "options": {"repetitions": 1, **options},
            }
        )

    def run_pair(self, plan=None, cancel=None):
        plan = plan or self.plan
        return self.session.run_pair(plan, plan.destinations[0], cancel=cancel)

    def test_verified_source_precedes_exact_ping(self):
        result = self.run_pair()
        self.assertEqual(
            self.channel.commands, ["show version", "show modules", "show vlans", "show ip", _PING]
        )
        self.assertEqual(result.verified_source_ip, "192.0.2.11")
        self.assertEqual(result.observation.status, "normal")
        self.assertEqual(result.observation.rtt_avg_ms, 1)

    def test_auto_mode_does_not_claim_or_fabricate_source(self):
        command = "ping 198.51.100.10 repetitions 1 timeout 2"
        self.channel.replies[command] = "Request timed out."
        result = self.run_pair(self.make_plan(source_mode="auto"))
        self.assertEqual(self.channel.commands, ["show version", "show modules", command])
        self.assertIsNone(result.verified_source_ip)
        self.assertEqual(result.observation.status, "no_reply")

    def test_vlan_source_preserves_address_evidence(self):
        command = "ping 198.51.100.10 repetitions 1 timeout 2 source 10"
        self.channel.replies[command] = "198.51.100.10 is alive, time = <1 ms"
        result = self.run_pair(self.make_plan(source_mode="vlan", vlan=10))
        self.assertEqual(self.channel.commands[-1], command)
        self.assertEqual((result.requested_source, result.verified_source_ip), ("10", "192.0.2.11"))
        self.assertIsNone(result.observation.rtt_avg_ms)

    def test_wrong_source_never_sends_ping_or_falls_back(self):
        self.channel.replies["show ip"] = _IP.replace("192.0.2.11", "192.0.2.12")
        result = self.run_pair()
        self.assertEqual(result.observation.status, "source_error")
        self.assertFalse(result.command_may_have_started)
        self.assertFalse(any(command.startswith("ping ") for command in self.channel.commands))

    def test_unknown_version_is_rejected_before_source_or_ping(self):
        self.channel.replies["show version"] = "Aruba JL253A 2930F\nWC.16.01.0008"
        with self.assertRaises(CollectionFailure) as raised:
            self.run_pair()
        self.assertEqual(raised.exception.code, ErrorCode.MODEL_UNSUPPORTED)
        self.assertEqual(self.channel.commands, ["show version", "show modules"])
        self.assertTrue(self.channel.closed)

    def test_missing_prompt_is_unknown_and_never_replayed(self):
        self.channel.incomplete = _PING
        result = self.run_pair()
        self.assertTrue(result.command_may_have_started)
        self.assertEqual(result.observation.status, "unknown")
        self.assertEqual(result.transport_error, ErrorCode.COMMAND_TIMEOUT)
        self.assertIsNone(result.observation.loss_pct)
        self.assertEqual(self.channel.commands.count(_PING), 1)
        self.assertTrue(self.channel.closed)

    def test_cancel_after_write_is_unknown_and_closes_channel(self):
        cancel = threading.Event()
        self.channel.on_write = lambda command: cancel.set() if command == _PING else None
        result = self.run_pair(cancel=cancel)
        self.assertEqual(result.observation.status, "unknown")
        self.assertEqual(result.transport_error, ErrorCode.CANCELLED)
        self.assertEqual(self.channel.commands.count(_PING), 1)
        self.assertTrue(self.channel.closed)

    def test_cancel_before_write_never_sends_ping(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(CollectionFailure) as raised:
            self.run_pair(cancel=cancel)
        self.assertEqual(raised.exception.code, ErrorCode.CANCELLED)
        self.assertEqual(self.channel.commands, [])


if __name__ == "__main__":
    unittest.main()
