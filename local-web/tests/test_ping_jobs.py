"""Offline checks of general Ping admission and the shared device slot."""

import sqlite3
import tempfile
import time
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import Mock, patch

from localweb import ping_general_store
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

    def wait_idle(self, timeout=5):
        deadline = time.monotonic() + timeout
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

    @contextmanager
    def transport(self, effect=None):
        execute = Mock(side_effect=effect or self.result)

        @contextmanager
        def device(_plan, target, credentials, _cancel):
            def run(plan, destination, *, cancel, before_send):
                if not isinstance(effect, CollectionFailure):
                    before_send()
                return execute(plan, target, destination, credentials, cancel)

            yield SimpleNamespace(run_pair=run)

        with patch.object(self.jobs.operations, "_ping_device_session", side_effect=device):
            yield execute

    def test_order_and_request_id_recovery(self):
        with self.transport() as execute:
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
                sorted((r["ip"], r["destination_ip"]) for r in rows),
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
        self.body["plan"]["options"] = {"concurrency": 1}
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
            self.transport(execute),
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
                self.assertEqual(
                    self.jobs.ping_status(key)["progress"]["waitReason"], "MANUAL_QUEUE"
                )
                self.jobs.tick_monitor()
                self.assertEqual(events, ["ping"])
            finally:
                release.set()
            self.wait_idle()
            self.assertEqual(events, ["ping", "cable.test"])
            self.assertEqual(self.jobs.get(key)["progress"]["completed"], 1)
            self.jobs.cancel(key)
            self.assertEqual(self.jobs.get(key)["state"], "cancelled")

    def test_reruns_keep_exact_pairs_options_and_parent_history_after_restart(self):
        self.body["plan"].update(
            targets="192.0.2.11:22\n192.0.2.11:2222",
            options={"repetitions": 7, "timeout": 4, "source_mode": "ip", "data_size": 0},
            source_ips={"192.0.2.11:22": "203.0.113.1", "192.0.2.11:2222": "203.0.113.2"},
        )
        parent = self.jobs.submit_ping(self.body)
        self.jobs.cancel_ping(parent)
        self.jobs = Jobs(self.jobs.directory)
        self.jobs.operations.credentials = Credentials("fixture-reader", "fixture")
        self.assertFalse(self.jobs.ping_status(parent)["historyOnly"])
        selected = {
            "requestId": "b" * 32,
            "rerun": {"parentId": parent, "mode": "selected", "indices": [3, 0]},
        }
        calls = []

        def execute(plan, target, destination, credentials, cancel):
            calls.append((target.port, destination.ip, plan.requested_source(target)))
            self.assertEqual(
                (plan.options.timeout, plan.options.repetitions, plan.options.data_size), (4, 7, 0)
            )
            return PingPairResult(
                DeviceIdentity("fixture", "2930F", "JL255A", "WC.16.11.0010"),
                PingObservation("no_reply" if target.port == 2222 else "unknown"),
                plan.requested_source(target),
                None,
                True,
            )

        with self.transport(execute):
            child = self.jobs.submit_ping(selected)
            self.assertEqual(self.jobs.submit_ping(selected), child)
            with self.assertRaises(Busy):
                self.jobs.submit_ping(dict(selected, rerun={"parentId": parent, "mode": "all"}))
            for _ in range(2):
                self.jobs.tick_monitor()
                self.wait_idle()
            result = self.jobs.ping_status(child)["result"]
            self.assertEqual((result["parentRunId"], result["total"]), (parent, 2))
            failed = self.jobs.submit_ping(
                {"requestId": "c" * 32, "rerun": {"parentId": child, "mode": "failed"}}
            )
            self.jobs.tick_monitor()
            self.wait_idle()
            self.assertEqual(self.jobs.ping_status(failed)["result"]["total"], 1)
            replay = self.jobs.submit_ping(
                {"requestId": "d" * 32, "rerun": {"parentId": child, "mode": "all"}}
            )
            for _ in range(2):
                self.jobs.tick_monitor()
                self.wait_idle()
            self.assertEqual(self.jobs.ping_status(replay)["result"]["options"]["repetitions"], 7)
        diagonal = [
            (22, "198.51.100.10", "203.0.113.1"),
            (2222, "198.51.100.20", "203.0.113.2"),
        ]
        self.assertEqual(calls, diagonal + diagonal[1:] + diagonal)
        reopened = Jobs(self.jobs.directory)
        self.assertEqual(reopened.ping_status(parent)["result"]["total"], 4)
        self.assertEqual(reopened.ping_status(child, after=1)["result"]["rows"][0]["index"], 1)
        self.assertEqual(reopened.ping_status(replay)["result"]["parentRunId"], child)

    def test_invalid_rerun_rejected_without_admission(self):
        parent = self.jobs.submit_ping(self.body)
        key = "f" * 32
        request = {"requestId": key, "rerun": {"parentId": parent, "mode": "all"}}
        with self.assertRaises(Busy):
            self.jobs.submit_ping(request)
        self.jobs.cancel_ping(parent)
        for indices in ([], [4], [-1], [0, 0], [True], ["0"], {"0": 1}):
            with self.subTest(indices=indices), self.assertRaises(ValueError):
                self.jobs.submit_ping(
                    {
                        **request,
                        "rerun": {"parentId": parent, "mode": "selected", "indices": indices},
                    }
                )
        for spec in (
            {"mode": "failed"},  # Unmeasured rows are not failed observations.
            {"mode": "all", "indices": [0]},
            {"mode": "selected", "indices": [0], "options": {}},
        ):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                self.jobs.submit_ping({**request, "rerun": {"parentId": parent, **spec}})
        self.assertIsNone(self.jobs.current_ping()["id"])
        with self.assertRaises(KeyError):
            self.jobs.ping_status(key)

    def test_auth_denial_stops_remaining_pairs_without_loss(self):
        self.body["plan"]["options"] = {"concurrency": 1}
        failure = CollectionFailure(ErrorCode.AUTH_FAILED, "fixture rejection")
        with self.transport(failure) as execute:
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
        with self.transport() as execute:
            key = self.jobs.submit_ping(self.body)
            self.jobs.operations.credentials = Credentials("different", "fixture")
            self.jobs.tick_monitor()
            execute.assert_not_called()
        result = self.jobs.get(key)
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(result["result"]["stopReason"], "SESSION_CHANGED")

    def test_300_devices_reuse_sessions_and_publish_stable_completion_pages(self):
        wave_timeout = 30  # Each wave commits up to 200 separate durable command boundaries.
        self.body["plan"].update(
            targets="\n".join(f"192.0.{2 + i // 250}.{i % 250 + 1}" for i in range(300)),
            destinations=[{"ip": f"198.51.100.{i + 1}"} for i in range(10)],
            options={"concurrency": 10},
        )
        opened, release, nine_done = Event(), Event(), Event()
        lock = Lock()
        live = set()
        sessions = {}
        peak = 0

        @contextmanager
        def device(plan, target, creds, cancel):
            nonlocal peak
            with lock:
                self.assertNotIn(target.ip, live)
                self.assertNotIn(target.ip, sessions)
                observed = sessions[target.ip] = []
                live.add(target.ip)
                peak = max(peak, len(live))
                if len(live) == 10:
                    opened.set()
            try:
                self.assertTrue(opened.wait(wave_timeout))
                if target.ip == "192.0.2.1":
                    self.assertTrue(release.wait(wave_timeout))

                def run(_plan, destination, *, cancel, before_send):
                    before_send()
                    observed.append(destination.ip)
                    return self.result(plan, target, destination, creds, cancel)

                yield SimpleNamespace(run_pair=run)
            finally:
                with lock:
                    live.remove(target.ip)
                    if len(sessions) == 10 and len(live) == 1:
                        nine_done.set()

        with patch.object(self.jobs.operations, "_ping_device_session", side_effect=device):
            key = self.jobs.submit_ping(self.body)
            self.jobs.tick_monitor()
            try:
                self.assertTrue(nine_done.wait(wave_timeout))
                progress = self.jobs.ping_status(key)["progress"]
                self.assertEqual(progress["completed"], 90)
                first = progress["rows"][0]
                self.assertEqual(first["index"], 0)
                self.assertNotEqual(first["pairIndex"], 0)
            finally:
                release.set()
            self.wait_idle(wave_timeout)
            for _ in range(29):
                self.jobs.tick_monitor()
                self.wait_idle(wave_timeout)
        self.assertEqual((len(sessions), peak, len(live)), (300, 10, 0))
        self.assertTrue(all(len(x) == len(set(x)) == 10 for x in sessions.values()))
        reloaded = Jobs(self.jobs.directory)
        page = []
        while len(page) < 3000:
            snap = reloaded.ping_status(key, len(page))["result"]
            self.assertGreater(len(snap["rows"]), 0)
            page.extend(snap["rows"])
        self.assertEqual(first, page[0])
        self.assertEqual([x["index"] for x in page], list(range(3000)))
        self.assertEqual(len({x["pairIndex"] for x in page}), 3000)
        with self.transport() as execute:
            self.jobs.submit_ping(
                {
                    "requestId": "b" * 32,
                    "rerun": {"parentId": key, "mode": "selected", "indices": [0]},
                }
            )
            self.jobs.tick_monitor()
            self.wait_idle()
            self.assertEqual(execute.call_args.args[1].ip, first["ip"])
            self.assertEqual(execute.call_args.args[2].ip, first["destination_ip"])

    def test_parallel_devices_drain_before_manual_and_release_all_sessions(self):
        both, release = Event(), Event()
        lock = Lock()
        live = set()
        sent = []
        manual_started = Event()

        @contextmanager
        def device(plan, target, creds, cancel):
            with lock:
                live.add(target.ip)
                if len(live) == 2:
                    both.set()

            def run(_plan, dest, *, cancel, before_send):
                before_send()
                self.assertTrue(release.wait(5))
                sent.append((target.ip, dest.ip))
                return self.result(plan, target, dest, creds, cancel)

            try:
                yield SimpleNamespace(run_pair=run)
            finally:
                with lock:
                    live.remove(target.ip)

        def manual(*args):
            self.assertFalse(live)
            manual_started.set()
            return {"rows": []}

        with (
            patch.object(self.jobs.operations, "_ping_device_session", side_effect=device),
            patch.object(self.jobs.operations, "dispatch", side_effect=manual),
        ):
            key = self.jobs.submit_ping(self.body)
            self.jobs.tick_monitor()
            try:
                self.assertTrue(both.wait(5))
                self.jobs.submit(
                    "cable.test",
                    {"requestId": "b" * 32, "confirmed": True, "targets": "192.0.2.11 1"},
                )
            finally:
                release.set()
            self.assertTrue(manual_started.wait(5))
            self.wait_idle()
            self.assertEqual(len(sent), 2)
            self.assertEqual(self.jobs.ping_status(key)["progress"]["completed"], 2)
            self.jobs.cancel_ping(key)

    def test_ping_status_recovers_history_without_claiming_packet_results(self):
        key = self.jobs.submit_ping(self.body)
        self.assertEqual(self.jobs.current_ping(), {"id": key})
        self.assertEqual(self.jobs.ping_status(key)["progress"]["total"], 4)
        reopened = Jobs(self.jobs.directory)
        receipt = reopened.ping_status(key)
        self.assertEqual(receipt["state"], "interrupted")
        self.assertFalse(receipt["historyOnly"])
        self.assertEqual([x["status"] for x in receipt["result"]["rows"]], ["not_run"] * 4)
        self.assertEqual(reopened.cancel_ping(key), {"requested": False})
        self.jobs.cancel_ping(key)

    def test_process_loss_keeps_durable_rows_uncertain_writes_and_unsent_slots(self):
        entered, release = Event(), Event()
        self.body["plan"]["options"] = {"concurrency": 1}

        def result(plan, target, destination, creds, cancel):
            if destination.ip == "198.51.100.20":
                entered.set()
                self.assertTrue(release.wait(10))
            return self.result(plan, target, destination, creds, cancel)

        with self.transport(result), tempfile.TemporaryDirectory() as recovery:
            key = self.jobs.submit_ping(self.body)
            self.jobs.tick_monitor()
            try:
                self.assertTrue(entered.wait(5))
                first = self.jobs.ping_status(key)["progress"]["rows"][0]
                # Snapshot a running process after the second durable intent but before its result.
                with (
                    closing(sqlite3.connect(self.jobs.db)) as live,
                    closing(sqlite3.connect(Path(recovery) / self.jobs.db.name)) as interrupted,
                ):
                    live.backup(interrupted)
            finally:
                release.set()
            self.wait_idle()
            self.jobs.cancel_ping(key)
            recovered = Jobs(recovery)
            job = recovered.ping_status(key)
            self.assertIsNone(recovered.ping_batch)
            self.assertEqual((job["state"], job["result"]["cursor"]), ("interrupted", 4))
            rows = job["result"]["rows"]
            self.assertEqual(rows[0], first)
            self.assertEqual(
                [r["status"] for r in rows], ["normal", "unknown", "not_run", "not_run"]
            )
            self.assertIsNone(rows[1]["loss_pct"])
            self.assertTrue(rows[1]["command_may_have_started"])
            self.assertFalse(rows[2]["command_may_have_started"])
            self.assertEqual(Jobs(recovery).ping_status(key, 1)["result"]["rows"], rows[1:])
            recovered.operations.credentials = Credentials("fixture-reader", "fixture")
            with self.assertRaises(Busy):
                recovered.submit_ping(self.body)
            child = recovered.submit_ping(
                {
                    "requestId": "b" * 32,
                    "rerun": {"parentId": key, "mode": "selected", "indices": [1]},
                }
            )
            self.assertEqual(recovered.ping_status(child)["progress"]["total"], 1)
            self.assertIsNone(recovered.active)
            recovered.cancel_ping(child)

    def test_failed_write_ahead_marker_prevents_packet_dispatch(self):
        self.body["plan"]["options"] = {"concurrency": 1}
        with (
            self.transport() as wire,
            patch(
                "localweb.ping_general_store.before_send",
                side_effect=sqlite3.OperationalError("fixture disk full"),
            ),
        ):
            key = self.jobs.submit_ping(self.body)
            self.jobs.tick_monitor()
            self.wait_idle()
        wire.assert_not_called()
        job = self.jobs.ping_status(key)
        self.assertEqual((job["state"], job["error"]), ("failed", "PING_HISTORY_WRITE_FAILED"))
        self.assertEqual(job["result"]["possiblyStartedPairs"], 0)
        self.assertTrue(all(r["status"] == "not_run" for r in job["result"]["rows"]))

    def test_legacy_unjournaled_interruption_has_no_definitive_rows(self):
        key = self.jobs.submit_ping(self.body)
        with closing(sqlite3.connect(self.jobs.db)) as db, db:
            db.execute("UPDATE general_ping_runs SET journal_version=0 WHERE id=?", (key,))
        recovered = Jobs(self.jobs.directory)
        status = recovered.ping_status(key)
        self.assertEqual(status["state"], "interrupted")
        self.assertTrue(status["historyOnly"])
        self.assertIsNone(status["result"])

    def test_persistent_disk_failure_after_send_keeps_published_prefix_stable(self):
        self.body["plan"]["options"] = {"concurrency": 1}
        save = ping_general_store.record

        def fail_after_first(path, key, row):
            if row["index"] > 0:
                raise sqlite3.OperationalError("fixture disk full")
            save(path, key, row)

        with (
            self.transport(),
            patch.object(ping_general_store, "record", side_effect=fail_after_first),
            patch.object(ping_general_store, "finalize", side_effect=sqlite3.OperationalError()),
        ):
            key = self.jobs.submit_ping(self.body)
            self.jobs.tick_monitor()
            self.wait_idle()
        live = self.jobs.ping_status(key)
        self.assertEqual((live["state"], live["error"]), ("failed", "PING_HISTORY_WRITE_FAILED"))
        self.assertEqual(live["result"]["cursor"], 1)
        reopened = Jobs(self.jobs.directory)
        result = reopened.ping_status(key)["result"]
        self.assertEqual(result["rows"][0], live["result"]["rows"][0])
        self.assertEqual(
            [x["status"] for x in result["rows"][1:]], ["unknown", "not_run", "not_run"]
        )

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
            self.transport() as execute,
        ):
            key = self.jobs.submit_ping(self.body)
            self.jobs.tick_monitor()
            execute.assert_not_called()
            self.assertEqual(self.jobs.get(key)["progress"]["completed"], 0)
            self.assertEqual(
                self.jobs.ping_status(key)["progress"]["waitReason"], "OPERATOR_CONFIRMATION"
            )
            self.jobs.cancel(key)


if __name__ == "__main__":
    unittest.main()
