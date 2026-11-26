"""Offline Ping contract tests. No network, SSH connection or real device."""

import unittest

from localweb.ping_tools import (
    PingOptions,
    PingPlan,
    parse_ping_output,
    preview_command,
    unicast_ipv4,
    verify_source,
)


def plan(**kwargs):
    return PingPlan.parse(
        {
            "targets": "192.0.2.11\n192.0.2.12:2222",
            "destinations": [{"ip": "198.51.100.10", "label": "NMS"}],
            **kwargs,
        }
    )


class PingPlanTests(unittest.TestCase):
    def test_default_and_presets(self):
        p = plan()
        self.assertEqual((p.total_pairs, p.total_requests), (2, 10))
        self.assertEqual(PingOptions.preset("quick").repetitions, 3)
        self.assertEqual(PingOptions.preset("detail").timeout, 3)
        self.assertEqual(
            preview_command(p, p.targets[0], p.destinations[0]),
            "ping 198.51.100.10 repetitions 5 timeout 2 source 192.0.2.11",
        )

    def test_auto_source_and_explicit_zero_size(self):
        p = plan(options={"source_mode": "auto", "data_size": 0})
        self.assertEqual(
            preview_command(p, p.targets[1], p.destinations[0]),
            "ping 198.51.100.10 repetitions 5 timeout 2 data-size 0",
        )
        self.assertIsNone(verify_source(p, p.targets[1], ()))

    def test_verified_ip_and_vlan_no_fallback(self):
        p = plan()
        self.assertEqual(verify_source(p, p.targets[0], ((10, "192.0.2.11"),)), "192.0.2.11")
        with self.assertRaises(ValueError):
            verify_source(p, p.targets[0], ((10, "192.0.2.12"),))
        p = plan(options={"source_mode": "vlan", "vlan": 10})
        self.assertEqual(verify_source(p, p.targets[0], ((10, "192.0.2.11"),)), "10")
        with self.assertRaises(ValueError):
            verify_source(p, p.targets[0], ((10, "192.0.2.11"), (10, "192.0.2.12")))

    def test_direct_source_requires_exact_endpoint_mapping(self):
        options = {"source_mode": "ip"}
        sources = {"192.0.2.11:22": "192.0.2.21", "192.0.2.12:2222": "192.0.2.22"}
        p = plan(options=options, source_ips=sources)
        self.assertEqual(verify_source(p, p.targets[1], ((10, "192.0.2.22"),)), "192.0.2.22")
        with self.assertRaises(ValueError):
            plan(options=options, source_ips={"192.0.2.11:22": "192.0.2.21"})
        with self.assertRaises(ValueError):
            plan(source_ips=sources)

    def test_strict_inputs_and_no_command_injection(self):
        for value in (
            "198.51.100.1;show running-config",
            "198.51.100.1\n",
            "0.0.0.0",
            "127.0.0.1",
            "224.0.0.1",
            "255.255.255.255",
            "198.51.100.01",
            "2001:db8::1",
            True,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                unicast_ipv4(value)
        for changes in (
            {"options": {"repetitions": True}},
            {"options": {"timeout": 11}},
            {"options": {"data_size": 1473}},
            {"options": {"source_mode": "vlan", "vlan": 4095}},
            {"options": {"command": "show running-config"}},
            {"targets": "192.0.2.11\n192.0.2.11:22"},
            {"targets": "192.0.2.11\t22"},
            {"targets": "192.0.2.11:0"},
            {"destinations": [{"label": "missing IP"}]},
            {"destinations": [{"ip": "198.51.100.10"}] * 2},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                plan(**changes)

    def test_large_plan_is_bounded(self):
        targets = "\n".join(f"192.0.2.{i}:22" for i in range(1, 251))
        targets += "\n" + "\n".join(f"203.0.113.{i}" for i in range(1, 51))
        destinations = [{"ip": f"198.51.100.{i}"} for i in range(1, 11)]
        p = plan(targets=targets, destinations=destinations)
        self.assertEqual((p.total_pairs, p.total_requests), (3000, 15000))
        with self.assertRaises(ValueError):
            plan(targets=targets + "\n203.0.113.51", destinations=destinations)


class PingOutputTests(unittest.TestCase):
    def parse(self, text, repetitions=1, completed=True):
        return parse_ping_output(text, "198.51.100.10", repetitions, completed=completed)

    def test_single_success_and_submillisecond(self):
        self.assertEqual(self.parse("198.51.100.10 is alive, time = 15 ms").rtt_avg_ms, 15)
        result = self.parse("198.51.100.10 is alive, time = <1 ms")
        self.assertEqual((result.status, result.sent, result.received), ("normal", 1, 1))
        self.assertIsNone(result.rtt_avg_ms)
        self.assertEqual(result.rtt_evidence, "<1/<1/<1")

    def test_single_timeout_unreachable_and_incomplete(self):
        self.assertEqual(self.parse("Request timed out.").loss_pct, 100)
        self.assertEqual(self.parse("The destination address is unreachable.").status, "unreachable")
        self.assertIsNone(self.parse("The destination address is unreachable.").loss_pct)
        self.assertEqual(self.parse("Request timed out.", completed=False).status, "unknown")
        self.assertEqual(self.parse("Request timed out.", 3).status, "unknown")

    def test_summaries_with_valid_counts_and_rtt(self):
        result = self.parse(
            "5 packets transmitted, 3 packets received, 40% packet loss\n"
            "round-trip (ms) min/avg/max = 1/3/5",
            5,
        )
        self.assertEqual((result.status, result.loss_pct, result.rtt_avg_ms), ("partial_loss", 40, 3))
        result = self.parse("5 packets transmitted, 0 packets received, 100% packet loss", 5)
        self.assertEqual(result.status, "no_reply")
        self.assertIsNone(result.rtt_avg_ms)

    def test_contradictory_or_partial_results_are_unknown(self):
        cases = (
            "4 packets transmitted, 4 packets received, 0% packet loss",
            "5 packets transmitted, 6 packets received, 0% packet loss",
            "5 packets transmitted, 4 packets received, 0% packet loss",
            "198.51.100.11 is alive, iteration 1, time = 1 ms\n"
            "5 packets transmitted, 5 packets received, 0% packet loss",
            "198.51.100.10 is alive, iteration 1, time = 1 ms",
        )
        for value in cases:
            with self.subTest(value=value):
                result = self.parse(value, 5)
                self.assertEqual(result.status, "unknown")
                self.assertIsNone(result.loss_pct)

    def test_cli_and_source_rejection_not_packet_loss(self):
        self.assertEqual(self.parse("Invalid input: source").status, "cli_error")
        self.assertEqual(self.parse("Invalid source IP address.").status, "source_error")
        self.assertIsNone(self.parse("Invalid source IP address.").received)
        self.assertEqual(self.parse("Unexpected CLI output").status, "unknown")


if __name__ == "__main__":
    unittest.main()
