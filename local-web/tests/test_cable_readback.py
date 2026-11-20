"""Operational regression tests for the interval after a single cable test request."""

import unittest
from contextlib import nullcontext
from threading import Event
from unittest.mock import Mock, call, patch

from localweb.cable import CableSession, UnconfirmedCableRequest, diagnose_ports, parse_result
from localweb.operations import _groups

from aruba2930f_backup.models import CollectionFailure, ErrorCode


class ReadbackTests(unittest.TestCase):
    def setUp(self):
        self.cancel = Mock(spec=Event)
        self.cancel.is_set.return_value = self.cancel.wait.return_value = False
        self.original, self.recovered = Mock(), Mock()
        self.original.test_cable.return_value = self.recovered.test_cable.return_value = ""
        self.open_session = Mock(
            side_effect=[nullcontext(self.original), nullcontext(self.recovered)]
        )
        self.progress = Mock()

    def diagnose(self, ports="1"):
        return diagnose_ports(
            _groups({"targets": f"127.0.0.1:2202 {ports}"}),
            self.open_session,
            self.cancel,
            self.progress,
        )["rows"]

    def test_show_drop_recovers_readback_and_keeps_prior_pairs_on_same_switch(self):
        early = parse_result("1 1-2 OK 7m", "1")
        rest = parse_result("1 3-6 OK 2m\n4-5 OK 2m\n7-8 OK 3m", "1")
        other = parse_result("2 1-2 OK 1m\n3-6 OK 1m\n4-5 OK 1m\n7-8 OK 1m", "2")
        self.original.read_cable.side_effect = [
            early,
            CollectionFailure(ErrorCode.COMMAND_TIMEOUT, "show timed out", transient=True),
        ]
        self.recovered.read_cable.side_effect = [rest, other]
        current, following = self.diagnose("1,2")
        self.assertEqual([current["status"], following["status"]], ["measured", "measured"])
        self.assertEqual(current["pairs"][0]["distance"], "7m")
        self.assertEqual(self.open_session.call_args_list[0], self.open_session.call_args_list[1])
        self.assertEqual(
            self.original.method_calls,
            [
                call.clear_cable(self.cancel),
                call.test_cable("1", self.cancel),
                call.read_cable("1", self.cancel),
                call.read_cable("1", self.cancel),
            ],
        )
        self.assertEqual(
            self.recovered.method_calls,
            [
                call.read_cable("1", self.cancel),
                call.test_cable("2", self.cancel),
                call.read_cable("2", self.cancel),
            ],
        )

    def test_recovery_budget_shared_with_confirmation_loss(self):
        self.original.test_cable.side_effect = UnconfirmedCableRequest
        self.recovered.read_cable.side_effect = CollectionFailure(
            ErrorCode.COMMAND_TIMEOUT, "show timed out", transient=True
        )
        current, following = self.diagnose("1,2")
        self.assertEqual((current["stage"], following["status"]), ("show", "skipped"))
        self.assertEqual(self.open_session.call_count, 2)
        self.assertEqual(self.recovered.method_calls, [call.read_cable("1", self.cancel)])

    def test_explicit_no_results_withdraws_prior_pairs_even_after_later_updates(self):
        early = parse_result("1 1-2 OK 99m", "1")
        empty = parse_result("No cable diagnostic results available.", "1")
        rest = parse_result("1 3-6 OK 2m\n4-5 OK 2m\n7-8 OK 3m", "1")
        self.original.read_cable.side_effect = [early, empty] + [rest] * 14
        (current,) = self.diagnose()
        self.assertEqual(current["status"], "partial")
        self.assertEqual([item["pair"] for item in current["pairs"]], ["3-6", "4-5", "7-8"])
        updates = [
            c.kwargs["cable_row"] for c in self.progress.call_args_list if "cable_row" in c.kwargs
        ]
        first_partial = next(i for i, r in enumerate(updates) if r["pairs"])
        self.assertEqual(updates[first_partial + 1]["pairs"], [])
        self.assertEqual(self.open_session.call_count, 1)
        self.original.test_cable.assert_called_once()

    def test_pending_result_at_deadline_does_not_start_next_port_on_busy_device(self):
        partial = parse_result("Cable test in progress\n1 1-2 Open 0m", "1")
        self.original.read_cable.return_value = partial
        current, following = self.diagnose("1,2")
        self.assertEqual(current["status"], "partial")
        self.assertEqual(current["pairs"], partial["pairs"])
        self.assertEqual(following["status"], "skipped")
        self.assertIn("진행", following["message"])
        self.original.test_cable.assert_called_once_with("1", self.cancel)
        self.assertEqual(self.original.read_cable.call_count, 16)

    def test_disconnect_in_actual_show_is_reported_as_retryable_read_failure(self):
        from netmiko.exceptions import ReadException, WriteException
        from paramiko.ssh_exception import SSHException

        session = Mock(spec=CableSession)
        for exc in (
            EOFError(),
            ConnectionResetError(),
            ReadException(),
            WriteException(),
            SSHException(),
            TimeoutError(),
        ):
            with (
                self.subTest(error=type(exc).__name__),
                patch.object(session, "_command", side_effect=exc),
            ):
                with self.assertRaises(CollectionFailure) as failure:
                    CableSession.read_cable(session, "1", self.cancel)
                self.assertTrue(failure.exception.transient)
                self.assertIn(
                    failure.exception.code,
                    {ErrorCode.SSH_NEGOTIATION_FAILED, ErrorCode.COMMAND_TIMEOUT},
                )

    def test_second_show_timeout_keeps_partial_and_stops_this_switch(self):
        early = parse_result("1 1-2 Open 0m", "1")
        timeout = CollectionFailure(ErrorCode.COMMAND_TIMEOUT, "show timed out", transient=True)
        self.original.read_cable.side_effect = [early, timeout]
        self.recovered.read_cable.side_effect = timeout
        current, following = self.diagnose("1,2")
        self.assertEqual(self.open_session.call_count, 2)
        self.assertEqual((current["status"], following["status"]), ("failed", "skipped"))
        self.assertEqual(current["pairs"], early["pairs"])
        self.assertEqual(current["message"], timeout.safe_message)
        self.assertEqual(self.recovered.method_calls, [call.read_cable("1", self.cancel)])

    def test_cancellation_during_show_failure_does_not_reconnect(self):
        def closing(*_args):
            self.cancel.is_set.return_value = True
            raise CollectionFailure(ErrorCode.COMMAND_TIMEOUT, "closed")

        self.original.read_cable.side_effect = closing
        (current,) = self.diagnose()
        self.assertEqual((current["status"], self.open_session.call_count), ("cancelled", 1))

    def test_explicit_show_rejection_does_not_reconnect(self):
        self.original.read_cable.side_effect = CollectionFailure(
            ErrorCode.COMMAND_REJECTED, "rejected"
        )
        (current,) = self.diagnose()
        self.assertEqual((current["status"], self.open_session.call_count), ("failed", 1))
