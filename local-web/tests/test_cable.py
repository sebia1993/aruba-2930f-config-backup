"""Cable tests use fixture strings and unconnected SSH channels only."""

import itertools
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from threading import Event
from unittest.mock import Mock, call

from localweb.cable import CableSession, UncertainTest, diagnose_ports, parse_result
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
        self.assertEqual(reading["status"], "complete")
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
                self.assertEqual(parse_result(data, "1/2")["status"], "complete")
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
        self.assertEqual(self.session.read_cable("1", self.cancel)["status"], "complete")
        self.assertEqual(
            self.writes(), ["test cable-diagnostics 1\n", "y\n", "show cable-diagnostics 1\n"]
        )

    def test_clear_requires_verified_config_and_return_to_original_exec(self):
        self.feed(
            "fixture# configure terminal\nfixture(config)#",
            "",
            "fixture(config)# clear cable-diagnostics\nfixture(config)#",
            "",
            "fixture(config)# end\nfixture#",
            "",
        )
        self.session.clear_cable(self.cancel)
        self.assertEqual(
            self.writes(), ["configure terminal\n", "clear cable-diagnostics\n", "end\n"]
        )

    def test_clear_rejection_wrong_echo_or_unrecognized_response_stops_before_test(self):
        for clear_response in (
            "clear cable-diagnostics\nPermission denied\nfixture(config)#",
            "clear cable-diagnostics\nResult buffer busy\nfixture(config)#",
            "clear cable-diagnostics\nAre you sure [y/n]?",
            "some other command\nfixture(config)#",
        ):
            with self.subTest(reply=clear_response):
                self.channel.reset_mock()
                self.feed("configure terminal\nfixture(config)#", "", clear_response, "")
                result = diagnose_ports(
                    _groups({"targets": "127.0.0.1 1,2"}),
                    lambda _: nullcontext(self.session),
                    self.cancel,
                    Mock(),
                )
                self.assertEqual([r["status"] for r in result["rows"]], ["failed", "failed"])
                self.assertTrue(all(not r["pairs"] for r in result["rows"]))
                self.assertEqual(
                    self.writes(), ["configure terminal\n", "clear cable-diagnostics\n"]
                )

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

    def test_clear_once_per_endpoint_test_then_show_for_each_port_and_poll_only_show(self):
        groups = _groups({"targets": "127.0.0.1:2202 1\n127.0.0.1:2203 1\n127.0.0.1:2202 1/2"})
        first, second = Mock(), Mock()
        # Inline data is never shown, including when it looks complete.
        first.test_cable.side_effect = [table("1", "100 m"), ""]
        first.read_cable.side_effect = [
            parse_result("Test pending\n" + table("1", "100 m"), "1"),
            parse_result("No results\n" + table("1", "100 m"), "1"),
            parse_result(table("1"), "1"),
            parse_result(table("1/2"), "1/2"),
        ]
        second.test_cable.return_value = "Cable diagnostics completed."
        second.read_cable.return_value = parse_result(table("1"), "1")
        cancel = Mock(spec=Event)
        cancel.wait.return_value = cancel.is_set.return_value = False
        sessions = {2202: first, 2203: second}
        rows = diagnose_ports(
            groups, lambda target: nullcontext(sessions[target.port]), cancel, Mock()
        )["rows"]
        self.assertEqual(
            [(r["ssh_port"], r["interface"], r["status"]) for r in rows],
            [(2202, "1", "measured"), (2202, "1/2", "measured"), (2203, "1", "measured")],
        )
        self.assertEqual(
            first.method_calls,
            [
                call.clear_cable(cancel),
                call.test_cable("1", cancel),
                call.read_cable("1", cancel),
                call.read_cable("1", cancel),
                call.read_cable("1", cancel),
                call.test_cable("1/2", cancel),
                call.read_cable("1/2", cancel),
            ],
        )
        self.assertEqual(
            second.method_calls,
            [
                call.clear_cable(cancel),
                call.test_cable("1", cancel),
                call.read_cable("1", cancel),
            ],
        )
        self.assertEqual(rows[0]["pairs"][0]["distance"], "3 m")
        self.assertEqual(cancel.wait.call_count, 2)

    def test_no_prior_or_partial_pairs_on_test_failure_or_inconclusive_readback(self):
        for inline, shown, status in [
            ("Test failed", table("1"), "failed"),
            ("Unexpected response", table("1"), "uncertain"),
            ("", table("2"), "failed"),
            ("", "Test has not yet been completed.\n" + table("1"), "uncertain"),
            ("", "No results\n" + table("1"), "uncertain"),
            ("", "Warning: response ambiguous\n" + table("1"), "failed"),
            ("", table("1") + "\n 7-8 Open 0m", "uncertain"),
            ("", "1 1-2 Open 100 m", "uncertain"),
        ]:
            with self.subTest(inline=inline, shown=shown):
                session = Mock()
                session.test_cable.return_value = inline
                session.read_cable.return_value = parse_result(shown, "1")
                cancel = Mock(spec=Event)
                cancel.wait.return_value = cancel.is_set.return_value = False
                rows = diagnose_ports(
                    _groups({"targets": "127.0.0.1 1"}),
                    lambda _, session=session: nullcontext(session),
                    cancel,
                    Mock(),
                )["rows"]
                self.assertEqual((rows[0]["status"], rows[0]["pairs"]), (status, []))
                if inline:
                    session.read_cable.assert_not_called()
                session.clear_cable.assert_called_once()
                session.test_cable.assert_called_once()

    def test_partial_group_failure_keeps_exact_endpoints_and_skips_other_tests_on_that_switch(self):
        groups = _groups({"targets": "127.0.0.1:2202 1,2\n127.0.0.1:2203 1"})
        first, second = Mock(), Mock()
        first.test_cable.side_effect = UncertainTest
        second.test_cable.return_value = ""
        second.read_cable.return_value = parse_result(table("1"), "1")
        sessions = {2202: first, 2203: second}
        rows = diagnose_ports(
            groups, lambda target: nullcontext(sessions[target.port]), self.cancel, Mock()
        )["rows"]
        self.assertEqual(
            [(r["ssh_port"], r["interface"], r["status"]) for r in rows],
            [(2202, "1", "uncertain"), (2202, "2", "skipped"), (2203, "1", "measured")],
        )
        first.test_cable.assert_called_once()
        first.read_cable.assert_not_called()

    def test_cancel_before_show_prevents_any_pairs_publication(self):
        session = Mock()

        def cancel_read(*args):
            self.cancel.set()
            return parse_result(table("1"), "1")

        session.test_cable.return_value = ""
        session.read_cable.side_effect = cancel_read
        rows = diagnose_ports(
            _groups({"targets": "127.0.0.1 1,2"}),
            lambda _: nullcontext(session),
            self.cancel,
            Mock(),
        )["rows"]
        self.assertEqual(
            [(r["status"], r["pairs"]) for r in rows], [("cancelled", []), ("cancelled", [])]
        )
        session.test_cable.assert_called_once()

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
            with self.assertRaises(ValueError):
                ops.dispatch(
                    "cable.show", {"targets": "127.0.0.1 1", "confirmed": True}, self.cancel
                )
            ops.collector.host_key_probe.probe.assert_not_called()
