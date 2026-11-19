"""Cable tests use fixture strings and unconnected SSH channels only."""

import itertools
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from threading import Event
from unittest.mock import Mock

from localweb.cable import CableSession, UncertainTest, _measure, diagnose_ports, parse_result
from localweb.operations import Operations, _groups

from aruba2930f_backup.models import CollectionFailure, CollectionOptions, Credentials, DeviceTarget


def table(port, distance="3 m"):
    return (
        f"{port} 1-2 OK {distance} 0 ns Normal MDI\n"
        "    3-6 Inter-short 2m\n"
        "    4-5 Impedance 12 m\n"
        "    7-8 Open 0m"
    )


class CableTests(unittest.TestCase):
    def setUp(self):
        self.cancel = Event()
        self.channel = Mock()
        self.channel.normalize_cmd.side_effect = lambda cmd: cmd + "\n"
        ticks = itertools.count()
        self.session = CableSession(
            DeviceTarget("127.0.0.1", 2222),
            Credentials("tester", "fixture"),
            CollectionOptions(command_timeout_seconds=0.5),
            Mock(),
            clock=lambda: next(ticks) * 0.01,
            sleeper=lambda _: None,
        )
        self.session._connection = self.channel
        self.session._prompt = "fixture#"

    def feed(self, *chunks):
        self.channel.read_channel.side_effect = itertools.chain(chunks, itertools.repeat(""))

    def writes(self):
        return [c.args[0] for c in self.channel.write_channel.call_args_list]

    def test_standard_pairs_implicit_ports_extended_columns_and_fault_distances(self):
        data = table("1/1") + "\n" + table("2/1", "5m") + "\nA3 1-2 ??\n   3-6 OK 1m"
        reading = parse_result(data, "2/1")
        self.assertEqual(reading["status"], "stored")
        self.assertEqual(reading["pairs"][0], {"pair": "1-2", "status": "OK", "distance": "5m"})
        self.assertEqual(reading["pairs"][1]["status"], "Inter-short")
        self.assertEqual(reading["pairs"][2]["distance"], "12 m")
        self.assertEqual(parse_result(data, "1")["pairs"], [])

    def test_declined_missing_in_progress_partial_and_other_target(self):
        for text, status in (
            ("The transceiver on port 51 does not support cable diagnostics.", "unsupported"),
            ("No cable diagnostic results available.", "empty"),
            ("Warning: test not yet completed.\n" + table("1"), "pending"),
            ("1 1-2 Short 0m", "partial"),
            (table("1") + "\n   7-8 Open 0m", "partial"),
            (table("2"), "failed"),
            ("Invalid port number.", "failed"),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_result(text, "1")["status"], status)

    def test_only_selected_command_and_recognized_split_confirmation(self):
        for prompt in ("Continue [Y/N]?", "Continue (y/n)?"):
            with self.subTest(prompt=prompt):
                self.channel.reset_mock()
                self.feed(
                    "test cable-diagnostics 1/2\r\nThis command will cause a loss of link\r\n",
                    prompt[:8],
                    "",
                    prompt[8:],
                    "",
                    "y\r\n" + table("1/2") + "\r\nfixture#",
                    "",
                )
                data = self.session.test_cable("1/2", self.cancel)
                self.assertEqual(parse_result(data, "1/2")["status"], "stored")
                self.assertEqual(self.writes(), ["test cable-diagnostics 1/2\n", "y\n"])

    def test_key_mode_second_prompt_is_absorbed_and_readback_matches_own_command(self):
        self.feed(
            "test cable-diagnostics 1\nThis command will cause a loss of link\nContinue (y/n)?",
            "",
            "y\r\nfixture#fixture#",
            "",
            "show cable-diagnostics 1\r\n" + table("1") + "\r\nfixture#",
            "",
        )
        self.assertEqual(self.session.test_cable("1", self.cancel).strip(), "")
        self.assertEqual(self.session.read_cable("1", self.cancel)["status"], "stored")
        self.assertEqual(
            self.writes(), ["test cable-diagnostics 1\n", "y\n", "show cable-diagnostics 1\n"]
        )

    def test_show_or_test_on_an_already_running_measurement_keeps_pending_label(self):
        session = Mock()
        session.read_cable.return_value = parse_result("Test in progress", "2")
        for testing in (False, True):
            result = diagnose_ports(
                _groups({"targets": "127.0.0.1 2"}),
                lambda target: nullcontext(session),
                testing,
                self.cancel,
                Mock(),
            )
            self.assertEqual(result["rows"][0]["status"], "pending")
            self.assertEqual(result["rows"][0]["pairs"], [])
        session.test_cable.assert_not_called()

    def test_unexpected_prompt_or_wrong_echo_does_not_send_confirmation(self):
        for text in (
            "test cable-diagnostics 1\nOverwrite settings? Continue (y/n)?",
            "test cable-diagnostics 2\nThis command will cause a loss of link\nContinue (y/n)?",
        ):
            with self.subTest(text=text):
                self.channel.reset_mock()
                self.feed(text)
                with self.assertRaises(CollectionFailure):
                    self.session.test_cable("1", self.cancel)
                self.assertEqual(self.writes(), ["test cable-diagnostics 1\n"])

    def test_lost_prompt_after_yes_is_uncertain_and_never_replayed(self):
        self.feed(
            "test cable-diagnostics 1\nThis command will cause a loss of link\nContinue (y/n)?"
        )
        with self.assertRaises(UncertainTest):
            self.session.test_cable("1", self.cancel)
        self.assertEqual(self.writes(), ["test cable-diagnostics 1\n", "y\n"])

    def test_shell_input_and_cancelled_request_are_never_written(self):
        for operation in (self.session.read_cable, self.session.test_cable):
            with self.assertRaises(ValueError):
                operation("1\nreload", self.cancel)
        self.cancel.set()
        with self.assertRaises(CollectionFailure):
            self.session.test_cable("1", self.cancel)
        self.assertEqual(self.writes(), [])

    def test_stale_vs_fresh_and_pending_readback_without_test_replay(self):
        baseline = parse_result(table("1"), "1")
        for start, pending, end, expected in (
            ("", False, table("1", "3m"), "stored"),
            ("", False, table("1", "5m"), "measured"),
            ("", True, table("1"), "measured"),
            ("Cable diagnostics completed.", False, table("1"), "measured"),
            ("Test failed", False, table("1"), "failed"),
            ("", False, "No results", "empty"),
        ):
            with self.subTest(start=start, pending=pending, end=end):
                session = Mock()
                session.test_cable.return_value = start
                readings = [baseline]
                if pending:
                    readings.append(parse_result("Test in progress", "1"))
                session.read_cable.side_effect = [*readings, parse_result(end, "1")]
                signal = Mock(spec=Event)
                signal.wait.return_value = False
                self.assertEqual(_measure(session, "1", signal)["status"], expected)
                session.test_cable.assert_called_once_with("1", signal)

    def test_partial_group_failure_keeps_exact_endpoints_and_skips_other_tests_on_that_switch(self):
        groups = _groups({"targets": "127.0.0.1:2202 1,2\n127.0.0.1:2203 1"})
        first, second = Mock(), Mock()
        for session in (first, second):
            session.read_cable.return_value = parse_result("No previous results", "1")
        first.test_cable.side_effect = UncertainTest
        second.test_cable.return_value = table("1")
        sessions = {2202: first, 2203: second}
        rows = diagnose_ports(
            groups, lambda target: nullcontext(sessions[target.port]), True, self.cancel, Mock()
        )["rows"]
        self.assertEqual(
            [(r["ssh_port"], r["interface"], r["status"]) for r in rows],
            [(2202, "1", "uncertain"), (2202, "2", "skipped"), (2203, "1", "measured")],
        )
        first.test_cable.assert_called_once()

    def test_disruptive_action_requires_confirmation_and_validated_target_before_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            ops = Operations(Path(directory))
            ops.credentials = Credentials("fixture", "fixture")
            ops.collector = Mock()
            for body in (
                {"targets": "127.0.0.1 1"},
                {"targets": "127.0.0.1 1;reload", "confirmed": True},
            ):
                with self.assertRaises(ValueError):
                    ops.dispatch("cable.test", body, self.cancel)
            ops.collector.host_key_probe.probe.assert_not_called()
