"""Confirm three switch-originated Ping commands overlap on distinct SSH transports."""

import socket
import tempfile
import threading
import unittest
from collections import Counter
from unittest.mock import patch

from localweb.jobs import Jobs

from aruba2930f_backup.models import Credentials
from loopback_server import LoopbackArubaSSHServer, wait_for

SWITCH = "192.0.2.11"
ROLES = {"gwIp": "198.51.100.1", "bbIp": "198.51.100.2", "nmsIp": "198.51.100.3"}


class PingSwitch(LoopbackArubaSSHServer):
    def __init__(self):
        super().__init__()
        self.replies_allowed = threading.Event()
        self.transports = {}

    def _respond(self, channel, command):
        if command == "show vlans":
            output = " VLAN ID Name | Status Voice Jumbo\r\n ------- ---- + ------ ----- -----\r\n 10 Management | Port-based No No"
        elif command == "show ip":
            output = f" VLAN | IP Config IP Address Subnet Mask Proxy ARP\r\n ---- + --------- ---------- ----------- ---------\r\n Management | Manual {SWITCH} 255.255.255.0 No"
        elif command.startswith("ping "):
            with self._lock:
                self.transports.setdefault(channel.get_transport(), []).append(command)
                if len(self.transports) == len(ROLES):
                    self.replies_allowed.set()
            # A serialized client cannot receive the first response before the others arrive.
            if not self.replies_allowed.wait(10):
                return False
            output = command.split()[1] + " is alive, time = 1 ms"
        else:
            return super()._respond(channel, command)
        channel.sendall(f"{command}\r\n{output}\r\n{self.prompt}".encode())
        return False


class RoleSessionSSHTests(unittest.TestCase):
    def test_three_authenticated_transports_reach_ping_before_first_reply(self):
        with tempfile.TemporaryDirectory() as directory, PingSwitch() as switch:
            jobs = Jobs(directory)
            jobs.operations.credentials = Credentials(switch.username, switch.password)
            original_connect = socket.socket.connect

            def connect_loopback(sock, address):
                # Preserve the registered switch IP for source and host-key verification,
                # while redirecting all outbound test connections to the local SSH fixture.
                self.assertEqual(address, (SWITCH, 22))
                return original_connect(sock, (switch.ip, switch.port))

            plan = dict(
                inventoryRows=[dict(swIp=SWITCH, **ROLES)],
                inventoryRoles=list(ROLES),
                inventoryRoleSessions=True,
                options={"repetitions": 1},
            )
            with patch.object(socket.socket, "connect", connect_loopback):
                key = jobs.submit_ping({"requestId": "c" * 32, "plan": plan})
                jobs.tick_monitor()
                try:
                    self.assertTrue(
                        wait_for(lambda: jobs.ping_status(key)["state"] != "running", timeout=30)
                    )
                finally:
                    switch.replies_allowed.set()
                    if jobs.ping_status(key)["state"] == "running":
                        jobs.cancel_ping(key)
                        wait_for(lambda: jobs.ping_status(key)["state"] != "running", timeout=10)
            rows = jobs.ping_status(key)["result"]["rows"]
            self.assertEqual(
                {(r["inventory_roles"][0], r["destination_ip"], r["status"]) for r in rows},
                {(role, ip, "normal") for role, ip in ROLES.items()},
                [(r["status"], r["reason_code"]) for r in rows],
            )
            expected = [
                f"ping {ip} repetitions 1 timeout 2 source {SWITCH}" for ip in ROLES.values()
            ]
            self.assertEqual(len(switch.transports), 3)
            self.assertEqual(
                Counter(cmds[0] for cmds in switch.transports.values()), Counter(expected)
            )
            self.assertTrue(all(len(cmds) == 1 for cmds in switch.transports.values()))
