"""Offline checks of general Ping admission and the shared device slot."""

import tempfile
import time
import unittest
from threading import Event
from unittest.mock import patch

from localweb.jobs import Busy, Jobs
from localweb.ping_ssh import PingPairResult
from localweb.ping_tools import PingObservation

from aruba2930f_backup.models import CollectionFailure, Credentials, DeviceIdentity, ErrorCode


class PingJobsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.jobs = Jobs(directory.name)
        self.jobs.operations.credentials = Credentials("fixture-reader", "fixture")
        self.body = {
            "requestId": "a" * 32,
            "plan": {
                "targets": "192.0.2.11\n192.0.2.12",
                "destinations": [
                    {"ip": "198.51.100.10"},
                    {"ip": "198.51.100.20"},
                ],
            },
        }

    def wait_idle(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.jobs.lock:
                if self.jobs.active is None:
                    return
            time.sleep(0.01)
        self.fail("device slot did not release")

    def result(self, plan, target, destination, credentials, cancel):
        return PingPairResult(
            DeviceIdentity("fixture", "2930F", "JL255A", "WC.16.11.0010"),
            PingObservation("normal", 5, 5, 0),
            plan.requested_source(target),
            target.ip,
            True,
        )

    def test_order_and_request_id_recovery(self):
        with patch.object(self.jobs.operations, "_ping_pair", side_effect=self.result) as execute:
            key = self.jobs.submit_ping(self.body)
            self.assertEqual(self.jobs.submit_ping(self.body), key)
            self.assertEqual(self.jobs.get(key)["progress"]["completed"], 0)
            for _ in range(4):
                self.jobs.tick_monitor()
                self.wait_idle()
            self.assertEqual(self.jobs.get(key)["state"], "done")
            rows = self.jobs.get(key)["result"]["rows"]
            self.assertEqual(len(rows), 4)
            self.assertEqual(
                [(r["ip"], r["destination_ip"]) for r in rows],
                [
                    ("192.0.2.11", "198.51.100.10"),
                    ("192.0.2.11", "198.51.100.20"),
                    ("192.0.2.12", "198.51.100.10"),
                    ("192.0.2.12", "198.51.100.20"),
                ],
            )
            self.jobs.tick_monitor()
            self.assertEqual(execute.call_count, 4)
        with self.assertRaises(Busy):
            self.jobs.submit_ping(self.body)
        reopened = Jobs(self.jobs.directory)
        reopened.operations.credentials = self.jobs.operations.credentials
        with self.assertRaises(Busy):
            reopened.submit_ping(self.body)
        self.assertEqual(reopened.history()[0]["state"], "done")

    def test_manual_reservation_wins_before_next_ping(self):
        entered, release = Event(), Event()
        events = []

        def execute(*args):
            events.append("ping")
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release not signalled")
            return self.result(*args)

        def cable(action, body, cancel):
            events.append(action)
            return {"rows": []}

        with (
            patch.object(self.jobs.operations, "_ping_pair", side_effect=execute),
            patch.object(self.jobs.operations, "dispatch", side_effect=cable),
        ):
            key = self.jobs.submit_ping(self.body)
            self.jobs.tick_monitor()
            try:
                self.assertTrue(entered.wait(3))
                manual = self.jobs.submit(
                    "cable.test",
                    {
                        "requestId": "b" * 32,
                        "confirmed": True,
                        "targets": "192.0.2.11 1",
                    },
                )
                self.assertEqual(self.jobs.get(manual)["state"], "queued")
                self.jobs.tick_monitor()
                self.assertEqual(events, ["ping"])
            finally:
                release.set()
            self.wait_idle()
            self.assertEqual(events, ["ping", "cable.test"])
            self.assertEqual(self.jobs.get(key)["progress"]["completed"], 1)
            self.jobs.cancel(key)
            self.assertEqual(self.jobs.get(key)["state"], "cancelled")

    def test_auth_denial_stops_remaining_pairs_without_loss(self):
        failure = CollectionFailure(ErrorCode.AUTH_FAILED, "fixture rejection")
        with patch.object(self.jobs.operations, "_ping_pair", side_effect=failure) as execute:
            key = self.jobs.submit_ping(self.body)
            self.jobs.tick_monitor()
            self.wait_idle()
            self.jobs.tick_monitor()
            execute.assert_called_once()
        job = self.jobs.get(key)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["result"]["possiblyStartedPairs"], 0)
        self.assertEqual(
            [row["status"] for row in job["result"]["rows"]],
            ["ssh_failure", "not_run", "not_run", "not_run"],
        )
        self.assertTrue(all(row["loss_pct"] is None for row in job["result"]["rows"]))

    def test_changed_credentials_before_slot_prevents_ssh(self):
        with patch.object(self.jobs.operations, "_ping_pair") as execute:
            key = self.jobs.submit_ping(self.body)
            self.jobs.operations.credentials = Credentials("different", "fixture")
            self.jobs.tick_monitor()
            execute.assert_not_called()
        result = self.jobs.get(key)
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(result["result"]["stopReason"], "SESSION_CHANGED")

    def test_ping_status_recovers_history_without_claiming_packet_results(self):
        key = self.jobs.submit_ping(self.body)
        self.assertEqual(self.jobs.current_ping(), {"id": key})
        self.assertEqual(self.jobs.ping_status(key)["progress"]["total"], 4)
        reopened = Jobs(self.jobs.directory)
        receipt = reopened.ping_status(key)
        self.assertEqual(receipt["state"], "interrupted")
        self.assertTrue(receipt["historyOnly"])
        self.assertIsNone(receipt["result"])
        self.assertEqual(reopened.cancel_ping(key), {"requested": False})
        self.jobs.cancel_ping(key)

    def test_cancel_only_acts_on_ping_owned_ids(self):
        other = "c" * 32
        with self.jobs.lock:
            self.jobs.recent[other] = {"action": "cable.test", "state": "running"}
        with self.assertRaises(KeyError):
            self.jobs.cancel_ping(other)
        self.assertEqual(self.jobs.recent[other]["state"], "running")
        key = self.jobs.submit_ping(self.body)
        self.assertEqual(self.jobs.cancel_ping(key), {"requested": True})
        self.assertEqual(self.jobs.cancel_ping(key), {"requested": False})
        status = self.jobs.ping_status(key, after=99)
        self.assertEqual(status["result"]["rows"], [])
        self.assertEqual(status["result"]["cursor"], 4)

    def test_shutdown_cancels_waiting_batch_and_does_not_resume(self):
        key = self.jobs.submit_ping(self.body)
        self.jobs.claim_shutdown()
        self.assertEqual(self.jobs.get(key)["state"], "cancelled")
        self.assertEqual(self.jobs.history()[0]["state"], "cancelled")
        reopened = Jobs(self.jobs.directory)
        self.assertIsNone(reopened.ping_batch)

    def test_existing_confirmation_defers_ping(self):
        with (
            patch.object(self.jobs.controls, "awaiting_confirmation", return_value=True),
            patch.object(self.jobs.operations, "_ping_pair") as execute,
        ):
            key = self.jobs.submit_ping(self.body)
            self.jobs.tick_monitor()
            execute.assert_not_called()
            self.assertEqual(self.jobs.get(key)["progress"]["completed"], 0)
            self.jobs.cancel(key)


if __name__ == "__main__":
    unittest.main()
