"""Manual reservations share the monitor's device slot and never replay a write."""

import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, closing, nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from localweb.cable import diagnose_ports
from localweb.jobs import MANUAL_RESERVATION_TIMEOUT, Busy, Jobs
from localweb.manual_queue import _interrupted_tests, manual_outcome
from localweb.operations import _groups

from aruba2930f_backup.models import Credentials


class ManualQueueTests(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.jobs = Jobs(self.root)
        self.jobs.operations.credentials = Credentials("operator", "secret-password")
        self.read_started = threading.Event()
        self.release_read = threading.Event()
        self.manual_started = threading.Event()
        self.release_manual = threading.Event()
        self.log = []
        self.status = "verified"
        self.failure = None

        def operation(action, body, cancel):
            if action == "ports.query":
                self.read_started.set()
                self.release_read.wait(4)
                return {"rows": []}
            self.log.append((action, dict(body)))
            return {
                "rows": [
                    {"ip": "192.0.2.1", "ssh_port": 22, "interface": "3", "status": "measured"}
                ]
            }

        def control(action, body, cancel):
            self.log.append((action, dict(body)))
            self.manual_started.set()
            self.release_manual.wait(4)
            if self.failure:
                raise self.failure
            return {
                "selected": len(body["targets"]),
                "skipped": [],
                "blocked": [],
                "results": [dict(t, status=self.status) for t in body["targets"]],
            }

        stack.enter_context(patch.object(self.jobs.operations, "dispatch", side_effect=operation))
        stack.enter_context(
            patch.object(self.jobs.controls, "dispatch_queued", create=True, side_effect=control)
        )
        stack.enter_context(patch.object(self.jobs.controls, "dispatch", side_effect=control))
        self.monitor_job = self.jobs.start_monitor({"targets": "192.0.2.1 1-3"})["jobId"]
        self.assertTrue(self.read_started.wait(2))
        self.addCleanup(self.finish)

    def finish(self):
        self.release_read.set()
        self.release_manual.set()
        until = time.monotonic() + 5
        while time.monotonic() < until:
            with self.jobs.lock:
                if self.jobs.active is None:
                    return
            time.sleep(0.005)

    @staticmethod
    def power(key, ports=("1",), operation="poe_off"):
        return {
            "requestId": key * 32,
            "operation": operation,
            "targets": [{"ip": "192.0.2.1", "sshPort": 22, "port": p} for p in ports],
        }

    @staticmethod
    def cable(key, port="3"):
        return {
            "requestId": key * 32,
            "confirmed": True,
            "responseKey": "9" * 32,
            "targets": "192.0.2.1 " + port,
        }

    def test_expired_reservation_is_cancelled_before_device_command(self):
        first = self.jobs.submit("control.quick-execute", self.power("1"))
        second = self.jobs.submit("cable.test", self.cable("2"))
        self.jobs.pending_since[first] -= MANUAL_RESERVATION_TIMEOUT + 1
        items = {item["id"]: item for item in self.jobs.manual_queue()["items"]}
        self.assertEqual(items[first]["state"], "cancelled")
        self.assertIn("10분", items[first]["message"])
        self.assertNotIn(first, self.jobs.pending)
        self.release_read.set()
        self.finish()
        self.assertEqual([row[0] for row in self.log], ["cable.test"])
        self.assertEqual(self.jobs.get(second)["state"], "done")

    def test_expired_reservation_is_cancelled_even_without_polling(self):
        key = self.jobs.submit("control.quick-execute", self.power("1"))
        self.jobs.pending_since[key] -= MANUAL_RESERVATION_TIMEOUT + 1
        self.finish()
        self.assertEqual(self.jobs.get(key)["state"], "cancelled")
        self.assertFalse(self.log)
        self.assertFalse(self.jobs.pending_since)
        with self.assertRaises(Busy):
            Jobs(self.root).submit("control.quick-execute", self.power("1"))

    def test_queue_limit_preserves_existing_reservations_and_duplicate_receipts(self):
        with patch("localweb.jobs.MAX_PENDING_MANUAL", 2):
            first = self.jobs.submit("control.quick-execute", self.power("1"))
            second = self.jobs.submit("cable.test", self.cable("2"))
            with self.assertRaises(Busy) as error:
                self.jobs.submit("control.quick-execute", self.power("4", ("4",)))
            self.assertIn("예약 대기 한도", str(error.exception))
            self.assertEqual(len(self.jobs.pending), 2)
            self.assertEqual(self.jobs.submit("control.quick-execute", self.power("1")), first)
        self.release_read.set()
        self.finish()
        self.assertEqual([row[0] for row in self.log], ["control.quick-execute", "cable.test"])
        self.assertEqual(self.jobs.get(second)["state"], "done")

    def test_fifo_freezes_targets_and_reserves_one_write_across_tabs(self):
        body = self.power("1", ("1", "2"))
        first = self.jobs.submit("control.quick-execute", body)
        body["targets"][0]["port"] = "9"
        alias = self.power("2", ("2", "1"))
        self.assertEqual(self.jobs.submit("control.quick-execute", alias), first)
        second = self.jobs.submit("cable.test", self.cable("3"))
        self.assertEqual(self.jobs.get(first)["state"], "queued")
        self.assertEqual(self.jobs.get(second)["state"], "queued")
        self.assertEqual(self.log, [])
        self.release_read.set()
        self.assertTrue(self.manual_started.wait(2))
        self.assertEqual(self.jobs.active, first)
        self.jobs.monitor.due = 0
        self.jobs.tick_monitor()
        self.finish()
        self.assertEqual([item[0] for item in self.log], ["control.quick-execute", "cable.test"])
        self.assertEqual([t["port"] for t in self.log[0][1]["targets"]], ["1", "2"])
        items = {item["id"]: item for item in self.jobs.manual_queue()["items"]}
        self.assertEqual(items[first]["state"], "done")
        self.assertEqual(items[second]["state"], "done")
        self.assertTrue(items[second]["wasQueued"])
        self.assertEqual(self.jobs.submit("control.quick-execute", alias), first)
        # The other tab's alias is durable too; neither token can replay after restart.
        reopened = Jobs(self.root)
        for request in (alias, self.power("1", ("1", "2"))):
            with self.assertRaises(Busy):
                reopened.submit("control.quick-execute", request)

    def test_receipt_and_get_preserve_queued_origin_across_transitions(self):
        body = self.power("1")
        receipt = self.jobs.submit_receipt("control.quick-execute", body)
        key = receipt["id"]
        self.assertEqual(receipt, {"id": key, "state": "queued", "wasQueued": True})
        self.release_read.set()
        self.assertTrue(self.manual_started.wait(2))
        self.assertEqual(
            self.jobs.submit_receipt("control.quick-execute", body),
            {"id": key, "state": "running", "wasQueued": True},
        )
        self.assertTrue(self.jobs.get(key)["wasQueued"])
        self.finish()
        self.assertTrue(self.jobs.get(key)["wasQueued"])
        next_receipt = self.jobs.submit_receipt("control.quick-execute", self.power("2"))
        self.assertEqual(next_receipt["state"], "running")
        self.assertFalse(next_receipt["wasQueued"])
        self.assertFalse(self.jobs.get(next_receipt["id"])["wasQueued"])

    def test_alias_recovers_canonical_cable_and_protects_owner_excerpt(self):
        first = self.cable("1")
        retry = dict(self.cable("2"), responseKey="8" * 32)
        canonical = self.jobs.submit("cable.test", first)
        self.assertEqual(self.jobs.submit("cable.test", retry), canonical)
        alias = retry["requestId"]
        details = {
            "rows": [
                {
                    "ip": "192.0.2.1",
                    "ssh_port": 22,
                    "interface": "3",
                    "status": "measured",
                    "responseExcerpt": "PRIVATE-CLI",
                }
            ]
        }
        with patch.object(self.jobs.operations, "dispatch", return_value=details) as run:
            self.finish()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.jobs.get(alias)["id"], canonical)
        self.assertTrue(self.jobs.get(alias)["wasQueued"])
        self.assertNotIn(
            "responseExcerpt",
            self.jobs.get(alias, response_key=retry["responseKey"])["result"]["rows"][0],
        )
        self.assertEqual(
            self.jobs.get(alias, response_key=first["responseKey"])["result"]["rows"][0][
                "responseExcerpt"
            ],
            "PRIVATE-CLI",
        )
        with self.assertRaises(KeyError):
            Jobs(self.root).get(alias)

    def test_opposite_overlap_requires_cancel_and_new_submit(self):
        first = self.jobs.submit("control.quick-execute", self.power("1", ("1", "2")))
        later = self.power("2", ("2",), "poe_on")
        with self.assertRaises(Busy) as error:
            self.jobs.submit("control.quick-execute", later)
        self.assertEqual(error.exception.conflicts, [first])
        self.assertTrue(self.jobs.cancel(first)["requested"])
        self.assertEqual(self.jobs.get(first)["state"], "cancelled")
        replacement = self.jobs.submit("control.quick-execute", later)
        self.finish()
        self.assertEqual(self.jobs.get(replacement)["state"], "done")
        self.assertEqual(len(self.log), 1)

    def test_dequeue_without_durable_history_cancels_reservations_without_sending(self):
        first = self.jobs.submit("control.quick-execute", self.power("1"))
        second = self.jobs.submit("cable.test", self.cable("2"))
        with closing(sqlite3.connect(self.jobs.db)) as db, db:
            # Simulate a journal that still reads, but refuses all queued state updates.
            db.execute("""CREATE TRIGGER no_queue_writes BEFORE UPDATE OF state ON jobs
                          WHEN OLD.state='queued' BEGIN SELECT RAISE(FAIL, 'read only'); END""")
        self.finish()
        for key in (first, second):
            self.assertEqual(self.jobs.get(key)["state"], "cancelled")
            self.assertIn("기록", self.jobs.get(key)["error"])
        self.assertFalse(self.jobs.pending)
        self.assertIsNone(self.jobs.active)
        self.assertFalse(self.log)
        self.assertEqual({job["state"] for job in self.jobs.history()}, {"cancelled"})
        # Recovery of the journal never dispatches the previously accepted reservations.
        with closing(sqlite3.connect(self.jobs.db)) as db, db:
            db.execute("DROP TRIGGER no_queue_writes")
        recovered = Jobs(self.root)
        self.assertEqual({job["state"] for job in recovered.history()}, {"cancelled"})

    def test_queued_only_cancel_never_interrupts_a_promoted_job(self):
        promoted = self.jobs.submit("control.quick-execute", self.power("1"))
        queued = self.jobs.submit("cable.test", self.cable("2"))
        self.release_read.set()
        self.assertTrue(self.manual_started.wait(2))
        self.assertFalse(self.jobs.cancel(promoted, queued_only=True)["requested"])
        self.assertFalse(self.jobs.cancel_event.is_set())
        self.assertTrue(self.jobs.cancel(queued, queued_only=True)["requested"])
        self.assertTrue(self.jobs.cancel(promoted)["requested"])
        self.assertTrue(self.jobs.cancel_event.is_set())

    def test_cancel_after_cable_send_holds_only_the_attempted_port(self):
        ops = self.jobs.operations
        session = Mock()

        def interrupt_test(action, body, cancel):
            def sent(port, _cancel):
                cancel.set()
                return ""

            session.test_cable.side_effect = sent
            ops._start_progress("케이블 검사", 2)
            return diagnose_ports(
                _groups(body), lambda _: nullcontext(session), cancel, ops._progress
            )

        with patch.object(ops, "dispatch", side_effect=interrupt_test):
            cable = self.jobs.submit("cable.test", self.cable("1", "1,2"))
            after_first = self.jobs.submit("control.quick-execute", self.power("2", ("1",)))
            after_second = self.jobs.submit("control.quick-execute", self.power("3", ("2",)))
            self.finish()
        self.assertEqual(session.test_cable.call_count, 1)
        self.assertEqual(self.jobs.get(cable)["state"], "cancelled")
        history = next(i for i in self.jobs.history() if i["id"] == cable)
        self.assertEqual(history["state"], "uncertain")
        self.assertEqual(self.jobs.get(after_first)["state"], "cancelled")
        self.assertEqual(self.jobs.get(after_second)["state"], "done")

    def test_uncertain_port_stops_overlapping_reservation_only(self):
        self.status = "unverified"
        first = self.jobs.submit("control.quick-execute", self.power("1"))
        overlap = self.jobs.submit("cable.test", self.cable("2", "1"))
        other = self.jobs.submit("cable.test", self.cable("3", "3"))
        self.finish()
        items = {item["id"]: item for item in self.jobs.manual_queue()["items"]}
        self.assertEqual(items[first]["state"], "uncertain")
        self.assertEqual(items[overlap]["state"], "cancelled")
        self.assertEqual(items[other]["state"], "done")
        self.assertEqual(len(self.log), 2)

    def test_partially_overlapping_same_operation_requires_explicit_scope(self):
        key = self.jobs.submit("control.quick-execute", self.power("1", ("1", "2")))
        with self.assertRaises(Busy) as error:
            self.jobs.submit("control.quick-execute", self.power("2", ("2", "3")))
        self.assertEqual(error.exception.conflicts, [key])
        self.assertEqual(len(self.jobs.manual_queue()["items"]), 1)

    def test_unexpected_post_write_failure_recorded_as_uncertain_in_history(self):
        self.failure = RuntimeError("transport unexpectedly lost")
        key = self.jobs.submit("control.quick-execute", self.power("1"))
        following = self.jobs.submit("cable.test", self.cable("2", "1"))
        self.finish()
        self.assertEqual(self.jobs.get(key)["state"], "failed")
        self.assertEqual(self.jobs.get(following)["state"], "cancelled")
        display = next(i for i in self.jobs.manual_queue()["items"] if i["id"] == key)
        history = next(i for i in self.jobs.history() if i["id"] == key)
        self.assertEqual(display["state"], "uncertain")
        self.assertEqual(history["state"], display["state"])
        self.assertIn("확인", history["summary"])

    def test_failed_preflight_is_not_an_uncertain_write(self):
        self.failure = ValueError("SSH 인증 거부")
        key = self.jobs.submit("control.quick-execute", self.power("1"))
        following = self.jobs.submit("cable.test", self.cable("2", "1"))
        self.finish()
        self.assertEqual(self.jobs.get(following)["state"], "done")
        history = next(i for i in self.jobs.history() if i["id"] == key)
        self.assertEqual(history["state"], "failed")
        self.assertIn("인증", history["summary"])

    def test_server_restart_cancels_pending_and_never_replays(self):
        import shutil
        from tempfile import TemporaryDirectory

        key = self.jobs.submit("cable.test", self.cable("1"))
        with TemporaryDirectory() as restored:
            shutil.copy2(self.jobs.db, restored)
            reopened = Jobs(restored)
            self.assertIsNone(reopened.active)
            self.assertEqual(reopened.history()[0]["state"], "cancelled")
            self.assertEqual(reopened.manual_queue()["items"], [])
            with self.assertRaises(Busy):
                reopened.submit("cable.test", self.cable("1"))
            db = reopened.db.read_bytes()
            self.assertNotIn(b"secret-password", db)
            self.assertNotIn(b"9" * 32, db)
        self.jobs.invalidate_pending("마지막 탭이 닫혀 예약을 취소했습니다.")
        self.finish()
        self.assertEqual(self.jobs.get(key)["state"], "cancelled")
        self.assertEqual(self.log, [])

    def test_nonadvancing_cable_progress_never_releases_uncertain_port(self):
        rows = [
            {
                "ip": "192.0.2.1",
                "sshPort": 22,
                "port": "3",
                "stage": "test",
                "status": "cancelled",
            }
        ]
        calls = []

        def stalled(cursor):
            calls.append(cursor)
            return {
                "cursor": cursor,
                "cableRows": [dict(rows[0], stage="test", status="pending")],
            }

        self.assertEqual(
            _interrupted_tests(rows, stalled),
            {("192.0.2.1", 22, "3")},
        )
        self.assertEqual(calls, [0])
        self.assertEqual(
            _interrupted_tests(rows, lambda _: {"cursor": "broken", "cableRows": rows}),
            {("192.0.2.1", 22, "3")},
        )

    def test_bad_outcome_decoder_keeps_worker_and_blocks_only_matching_port(self):
        first = self.jobs.submit("control.quick-execute", self.power("1", ("1",)))
        overlap = self.jobs.submit("cable.test", self.cable("2", "1"))
        unrelated = self.jobs.submit("cable.test", self.cable("3", "3"))
        decoded = manual_outcome

        def decode(request, *args, **kwargs):
            if request.operation == "poe_off":
                raise RuntimeError("simulated corrupt result parser")
            return decoded(request, *args, **kwargs)

        self.release_manual.set()
        with patch("localweb.jobs.manual_outcome", side_effect=decode):
            self.release_read.set()
            self.finish()

        self.assertIsNone(self.jobs.active)
        self.assertFalse(self.jobs.pending)
        self.assertEqual(self.jobs.get(first)["state"], "done")
        items = {item["id"]: item for item in self.jobs.manual_queue()["items"]}
        self.assertEqual(items[first]["state"], "uncertain")
        self.assertEqual(items[overlap]["state"], "cancelled")
        self.assertEqual(items[unrelated]["state"], "done")
        self.assertEqual([item[0] for item in self.log], ["control.quick-execute", "cable.test"])
        record = next(item for item in self.jobs.history() if item["id"] == first)
        self.assertEqual(record["state"], "uncertain")

    def test_postprocessing_failure_never_stalls_queue_or_replays_written_port(self):
        first = self.jobs.submit("control.quick-execute", self.power("1", ("1",)))
        overlapping = self.jobs.submit("cable.test", self.cable("2", "1"))
        independent = self.jobs.submit("cable.test", self.cable("3", "3"))
        original = self.jobs._record_power

        def record(rows, plan, result):
            if self.jobs.active == first:
                raise RuntimeError("synthetic post-processing failure")
            return original(rows, plan, result)

        self.release_manual.set()
        with patch.object(self.jobs, "_record_power", side_effect=record):
            self.release_read.set()
            self.finish()

        self.assertIsNone(self.jobs.active)
        self.assertFalse(self.jobs.pending)
        self.assertEqual(self.jobs.get(first)["state"], "failed")
        display = {item["id"]: item for item in self.jobs.manual_queue()["items"]}
        self.assertEqual(display[first]["state"], "uncertain")
        self.assertEqual(display[overlapping]["state"], "cancelled")
        self.assertEqual(display[independent]["state"], "done")
        self.assertEqual([action for action, _ in self.log], [
            "control.quick-execute",
            "cable.test",
        ])
        records = {item["id"]: item for item in self.jobs.history()}
        self.assertEqual(records[first]["state"], "uncertain")
        self.assertNotIn("synthetic post-processing failure", records[first]["summary"])

    def test_multi_switch_reservations_preempt_next_monitor_round_in_fifo(self):
        keys = []
        for i in range(1, 13):
            body = self.power(f"{i:x}", (str(i),))
            body["targets"][0]["ip"] = "192.0.2.2" if i % 2 else "192.0.2.1"
            keys.append(self.jobs.submit("control.quick-execute", body))
        self.assertEqual(len(self.jobs.pending), 12)
        duplicate = self.power("1", ("1",))
        duplicate["targets"][0]["ip"] = "192.0.2.2"
        self.assertEqual(self.jobs.submit("control.quick-execute", duplicate), keys[0])
        self.release_read.set()
        self.assertTrue(self.manual_started.wait(2))
        self.jobs.monitor.due = time.monotonic() - 0.1
        self.jobs.tick_monitor()
        self.assertEqual(self.jobs.monitor_status()["skippedRounds"], 1)
        self.release_manual.set()
        self.finish()
        self.assertEqual(
            [item[1]["targets"][0]["port"] for item in self.log], [str(i) for i in range(1, 13)]
        )
        records = {item["id"]: item for item in self.jobs.history()}
        self.assertEqual(set(records), set(keys))
        self.assertTrue(all(records[key]["state"] == "done" for key in keys))
        self.assertTrue(all(item["state"] == "done" for item in self.jobs.manual_queue()["items"]))
        self.assertFalse(self.jobs.pending)
        self.assertIsNone(self.jobs.active)

    def test_lost_credentials_never_run_queued_job(self):
        key = self.jobs.submit("cable.test", self.cable("1"))
        self.jobs.operations.credentials = Credentials("other", "changed-secret")
        self.finish()
        self.assertEqual(self.jobs.get(key)["state"], "cancelled")
        self.assertFalse(self.log)
        self.assertEqual(self.jobs.history()[0]["state"], "cancelled")
