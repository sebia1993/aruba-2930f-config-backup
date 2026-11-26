"""Synthetic read-only Source evidence tests; no device or network access."""

import unittest

from localweb.ping_source import parse_owned_ipv4
from localweb.ping_tools import PingPlan, verify_source

VLANS = """
Status and Counters - VLAN Information
Maximum VLANs to support : 256
Primary VLAN : DEFAULT_VLAN
Management VLAN :

 VLAN ID Name                 | Status     Voice Jumbo
 ------- -------------------- + ---------- ----- -----
 1       DEFAULT_VLAN         | Port-based No    No
 10      Campus Management    | Port-based No    No
 20      Disabled VLAN        | Port-based No    No
 30      Waiting DHCP         | Port-based No    No
"""
IP = """
Internet (IP) Service
IP Routing : Disabled
Default Gateway : 192.0.2.254

 VLAN                 | IP Config  IP Address      Subnet Mask     Proxy ARP
 -------------------- + ---------- --------------- --------------- ---------
 DEFAULT_VLAN         | DHCP/Bootp 192.0.2.11      255.255.255.0   No
 Campus Management    | Manual     198.51.100.11   255.255.255.0   No
 Disabled VLAN        | Disabled   203.0.113.11    255.255.255.0   No
 Waiting DHCP         | DHCP/Bootp
"""


class SourceEvidenceTests(unittest.TestCase):
    def test_join_exact_names_and_ignore_inactive_or_gateway(self):
        owned = parse_owned_ipv4(VLANS, IP, completed=True)
        self.assertEqual(owned, ((1, "192.0.2.11"), (10, "198.51.100.11")))
        plan = PingPlan.parse(
            {
                "targets": "192.0.2.11",
                "destinations": [{"ip": "198.51.100.100"}],
            }
        )
        self.assertEqual(verify_source(plan, plan.targets[0], owned), "192.0.2.11")
        plan = PingPlan.parse(
            {
                "targets": "192.0.2.11",
                "destinations": [{"ip": "198.51.100.100"}],
                "options": {"source_mode": "vlan", "vlan": 20},
            }
        )
        with self.assertRaises(ValueError):
            verify_source(plan, plan.targets[0], owned)

    def test_reject_incomplete_or_unknown_tables(self):
        with self.assertRaises(ValueError):
            parse_owned_ipv4(VLANS, IP, completed=False)
        for value in (
            "",
            IP.replace("IP Config", "Unknown"),
            IP.replace("Waiting DHCP         | DHCP/Bootp", ""),
            IP.replace("Campus Management", "Wrong VLAN"),
            IP + "\nUnexpected prompt",
            IP.replace("198.51.100.11", "198.51.100.255"),
            IP.replace("198.51.100.11", "192.0.2.11"),
            IP.replace("255.255.255.0", "0.0.0.255"),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_owned_ipv4(VLANS, value, completed=True)

    def test_reject_ambiguous_vlan_mapping(self):
        for value in (
            VLANS.replace("10      Campus", "1       Campus"),
            VLANS.replace("Campus Management", "DEFAULT_VLAN"),
            VLANS.replace("Port-based", "Unknown"),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_owned_ipv4(value, IP, completed=True)

    def test_accept_dual_proxy_arp_columns(self):
        ip = IP.replace("Proxy ARP", "Std Local")
        ip = ip.replace("255.255.255.0   No", "255.255.255.0   No No")
        self.assertEqual(
            parse_owned_ipv4(VLANS, ip, completed=True),
            ((1, "192.0.2.11"), (10, "198.51.100.11")),
        )


if __name__ == "__main__":
    unittest.main()
