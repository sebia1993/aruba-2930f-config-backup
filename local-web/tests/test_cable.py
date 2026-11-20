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

from aruba2930f_backup.models import (
    CollectionFailure,
    CollectionOptions,
    Credentials,
    DeviceTarget,
    ErrorCode,
)


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
            ("The transceiver on port 1 does not support cable diagnostics.", "unsupported"),
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

    def test_no_prior_pairs_on_test_failure_or_inconclusive_readback(self):
        for inline, shown, status in [
            ("Test failed", table("1"), "failed"),
            ("Unexpected response", table("1"), "uncertain"),
            ("", table("2"), "failed"),
            ("", "Test has not yet been completed.\n" + table("1"), "uncertain"),
            ("", "No results\n" + table("1"), "uncertain"),
            ("", "Warning: response ambiguous\n" + table("1"), "failed"),
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

    def test_no_question_accepts_selected_port_table_completion_or_test_progress(self):
        for reply in (table("1/1"), "Cable diagnostics completed.", "Test in progress"):
            with self.subTest(reply=reply):
                self.channel.reset_mock()
                self.feed("test cable-diagnostics 1/1\n" + reply + "\nfixture#", "")
                self.assertEqual(self.session.test_cable("1/1", self.cancel).strip(), reply)
                self.assertEqual(self.writes(), ["test cable-diagnostics 1/1\n"])
        for reply in (table("2/1"), "Unknown response.", "", "Continue (y/n)?"):
            with self.subTest(reject=reply):
                self.channel.reset_mock()
                self.feed("test cable-diagnostics 1/1\n" + reply + "\nfixture#", "")
                with self.assertRaises(CollectionFailure):
                    self.session.test_cable("1/1", self.cancel)
                self.assertEqual(self.writes(), ["test cable-diagnostics 1/1\n"])

    def test_progress_retains_only_pairs_read_for_same_port_in_this_run(self):
        partial_text = "9 1-2 OK 399m\n1 1-2 Open 0m\n 3-6 OK 8m\n 4-5 OK 4m\n 4-5 OK 7m"
        partial = parse_result(partial_text, "1")
        self.assertEqual([pair["pair"] for pair in partial["pairs"]], ["1-2", "3-6"])
        self.assertNotIn("399m", partial["responseExcerpt"])
        self.assertIn("1-2 Open 0m", partial["responseExcerpt"])
        session = Mock()
        session.test_cable.return_value = table("1", "77m")
        session.read_cable.side_effect = [
            partial,
            parse_result("1 4-5 Impedance 12 m\n 7-8 Open 0m", "1"),
        ]
        progress = Mock()
        cancel = Mock(spec=Event)
        cancel.wait.return_value = cancel.is_set.return_value = False
        (row,) = diagnose_ports(
            _groups({"targets": "127.0.0.1 1"}), lambda _: nullcontext(session), cancel, progress
        )["rows"]
        self.assertEqual((row["status"], row["stage"]), ("measured", "show"))
        self.assertEqual([pair["distance"] for pair in row["pairs"]], ["0m", "8m", "12 m", "0m"])
        self.assertNotIn("responseExcerpt", row)
        interim = [
            c.kwargs["cable_row"] for c in progress.call_args_list if "cable_row" in c.kwargs
        ]
        self.assertEqual(
            [r["status"] for r in interim], ["pending", "pending", "partial", "measured"]
        )
        self.assertEqual([r["stage"] for r in interim], ["test", "show", "show", "show"])
        self.assertEqual(sum(bool(c.kwargs.get("finished")) for c in progress.call_args_list), 1)
        session.clear_cable.assert_called_once()
        session.test_cable.assert_called_once()
        self.assertEqual(session.read_cable.call_count, 2)

    def test_partial_expires_without_retesting_and_unknown_report_has_scoped_excerpt(self):
        session = Mock()
        session.test_cable.return_value = "Test completed."
        cancel = Mock(spec=Event)
        cancel.wait.return_value = cancel.is_set.return_value = False
        for text, count, status, reads in [
            ("1 1-2 Short 0m\n   3-6 Open 18m\n   4-5 OK 3m", 3, "partial", 16),
            ("1 1-2 ???\n2 1-2 OK 399m", 0, "failed", 1),
        ]:
            with self.subTest(text=text):
                session.reset_mock()
                session.read_cable.return_value = parse_result(text, "1")
                (row,) = diagnose_ports(
                    _groups({"targets": "127.0.0.1 1"}),
                    lambda _: nullcontext(session),
                    cancel,
                    Mock(),
                )["rows"]
                self.assertEqual(
                    (row["stage"], row["status"], len(row["pairs"])), ("show", status, count)
                )
                self.assertIn("1-2", row["responseExcerpt"])
                self.assertNotIn("399m", row["responseExcerpt"])
                self.assertNotIn("unresolved", row)
                session.clear_cable.assert_called_once()
                session.test_cable.assert_called_once()
                self.assertEqual(session.read_cable.call_count, reads)

    def test_read_failure_after_partial_preserves_evidence(self):
        session = Mock()
        session.test_cable.return_value = "Test completed."
        session.read_cable.side_effect = [
            parse_result("1 1-2 Open 0m", "1"),
            CollectionFailure(ErrorCode.COMMAND_TIMEOUT, "readback timed out"),
        ]
        cancel = Mock(spec=Event)
        cancel.wait.return_value = cancel.is_set.return_value = False
        current, next_port = diagnose_ports(
            _groups({"targets": "127.0.0.1 1,2"}), lambda _: nullcontext(session), cancel, Mock()
        )["rows"]
        self.assertEqual((current["stage"], current["status"]), ("show", "failed"))
        self.assertEqual(current["pairs"], [{"pair": "1-2", "status": "Open", "distance": "0m"}])
        self.assertEqual(next_port["status"], "skipped")
        session.test_cable.assert_called_once()

    def test_pending_warning_with_partial_rows_and_foreign_port_notices(self):
        source = (
            "The transceiver on port 51 does not support cable diagnostics.\n"
            "Warning: test incomplete\n"
            "1 1-2 Open 0m\n 3-6 OK 8m\n 4-5 Inter-short 7m\n"
            "2 1-2 OK 399m\nPort 2 result: No data"
        )
        result = parse_result(source, "1")
        self.assertEqual(result["status"], "partial")
        self.assertEqual([p["distance"] for p in result["pairs"]], ["0m", "8m", "7m"])
        self.assertNotIn("399m", result["responseExcerpt"])
        self.assertNotIn("port 51", result["responseExcerpt"])
        self.assertEqual(parse_result(source, "51")["status"], "unsupported")

    def test_unrecognized_response_excerpt_is_bounded_and_excludes_credentials_and_foreign_rows(
        self,
    ):
        source = (
            "Firmware response: unclassified text\n"
            "fixture#\nCredentials: must-not-be-displayed\n"
            "Port 1: VCT report\n 7-8 ??? 192.0.2.4\n"
            "9 1-2 OK 399m\n 3-6 OK 400m\n"
        )
        reading = parse_result(source, "1")
        self.assertEqual(reading["status"], "failed")
        self.assertIn("unclassified text", reading["responseExcerpt"])
        self.assertIn("7-8 ???", reading["responseExcerpt"])
        for omitted in ("fixture#", "must-not", "192.0.2.4", "399m", "400m"):
            self.assertNotIn(omitted, reading["responseExcerpt"])
        large = parse_result(("x" * 2000 + "\n") * 20, "1")["responseExcerpt"]
        self.assertLessEqual(len(large), 12 * 181)
        self.assertNotIn("responseExcerpt", parse_result("2 1-2 OK 399m", "1"))

    def test_four_accumulated_pairs_marked_incomplete_wait_for_confirmed_readback(self):
        session = Mock()
        session.test_cable.return_value = "Diagnostic status\nTest completed."
        cancel = Mock(spec=Event)
        cancel.wait.return_value = cancel.is_set.return_value = False
        first = parse_result("Warning: test incomplete\n1 1-2 Open 0m\n 3-6 OK 3m", "1")
        second = parse_result("Test pending\n1 4-5 OK 8m\n 7-8 OK 4m", "1")
        for readings, expected in [
            ([first] + [second] * 15, "partial"),
            ([first, second, parse_result(table("1"), "1")], "measured"),
        ]:
            with self.subTest(expected=expected):
                session.reset_mock()
                session.read_cable.side_effect = readings
                progress = Mock()
                (row,) = diagnose_ports(
                    _groups({"targets": "127.0.0.1 1"}),
                    lambda _: nullcontext(session),
                    cancel,
                    progress,
                )["rows"]
                self.assertEqual((row["status"], len(row["pairs"])), (expected, 4))
                self.assertEqual(session.read_cable.call_count, len(readings))
                self.assertNotIn("stillRunning", row)
                updates = [
                    c.kwargs["cable_row"]
                    for c in progress.call_args_list
                    if "cable_row" in c.kwargs
                ]
                self.assertEqual(
                    next(r["status"] for r in updates if len(r["pairs"]) == 4), "partial"
                )
                if expected == "partial":
                    self.assertIn("완료 확인", row["message"])

    def test_pending_invalid_pairs_withdraw_prior_measurements_until_valid_again(self):
        first = parse_result("1 1-2 OK 99m\n 3-6 OK 7m", "1")
        missing = parse_result("1 3-6 OK 7m\n 4-5 OK 8m\n 7-8 OK 4m", "1")
        restored = parse_result("1 1-2 Open 0m", "1")
        for bad, extra, expected in [
            ("1 1-2 ???", [], "partial"),
            ("1 1-2 OK 2m\n 1-2 Open 9m\n 3-6 ???", [restored], "measured"),
        ]:
            with self.subTest(bad=bad):
                session = Mock()
                session.test_cable.return_value = "Test completed."
                invalid = parse_result("Test pending\n" + bad, "1")
                expected_pairs = set(invalid["unresolved"])
                self.assertIn("1-2", expected_pairs)
                session.read_cable.side_effect = [first, invalid, missing] + (
                    extra or [missing] * 13
                )
                cancel = Mock(spec=Event)
                cancel.wait.return_value = cancel.is_set.return_value = False
                progress = Mock()
                (row,) = diagnose_ports(
                    _groups({"targets": "127.0.0.1 1"}),
                    lambda _, session=session: nullcontext(session),
                    cancel,
                    progress,
                )["rows"]
                updates = [
                    c.kwargs["cable_row"]
                    for c in progress.call_args_list
                    if c.kwargs.get("cable_row", {}).get("status") == "partial"
                ]
                withdrawn = updates[1]
                self.assertEqual(
                    {v["pair"] for v in withdrawn["pairs"]},
                    {"1-2", "3-6"} - expected_pairs,
                )
                self.assertEqual([v["pair"] for v in updates[2]["pairs"]], ["3-6", "4-5", "7-8"])
                self.assertEqual(row["status"], expected)
                self.assertNotIn("99m", [v["distance"] for v in row["pairs"]])
                if extra:
                    self.assertEqual(
                        row["pairs"][0], {"pair": "1-2", "status": "Open", "distance": "0m"}
                    )
                session.test_cable.assert_called_once()

    def test_terminal_malformed_read_invalidates_previous_measurement_for_same_pair(self):
        session = Mock()
        session.test_cable.return_value = "Test completed."
        session.read_cable.side_effect = [
            parse_result("1 1-2 Open 0m\n 3-6 OK 3m", "1"),
            parse_result("1 1-2 ???", "1"),
        ]
        cancel = Mock(spec=Event)
        cancel.wait.return_value = cancel.is_set.return_value = False
        (row,) = diagnose_ports(
            _groups({"targets": "127.0.0.1 1"}), lambda _: nullcontext(session), cancel, Mock()
        )["rows"]
        self.assertEqual([p["pair"] for p in row["pairs"]], ["3-6"])
        self.assertEqual(row["stage"], "show")
        self.assertEqual(row["status"], "failed")
        self.assertIn("???", row["responseExcerpt"])

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
