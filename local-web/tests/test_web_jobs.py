"""Local job persistence, serialization, and the download registry."""

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from localweb.jobs import Busy, Jobs
from localweb.monitoring import PortMonitoring

from aruba2930f_backup.models import CollectionFailure, Credentials, ErrorCode
from aruba2930f_backup.poe_query_models import PoEQueryResult, QueryStatus
from aruba2930f_backup.port_control import PortControlService, PortOperationResult, PortSnapshot


class JobTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.jobs = Jobs(self.root)

    def finished(self, key):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            job = self.jobs.get(key)
            if job["state"] != "running":
                return job
            time.sleep(0.01)
        self.fail("job did not finish")

    def test_download_persists_without_storing_inputs(self):
        key = self.jobs.submit("profiles.export", {"targets": "192.0.2.7 1-2"})
        job = self.finished(key)
        file = job["result"]["files"][0]
        reopened = Jobs(self.root)
        self.assertEqual(reopened.history()[0]["files"], [file])
        self.assertEqual(reopened.download(file["id"]).name, "port-targets.json")
        self.assertEqual(reopened.result_folder(file["id"]), reopened.download(file["id"]).parent)
        self.assertNotIn("192.0.2", str(reopened.history()))
        self.assertFalse(reopened.operations.session_summary()["active"])

    def test_direct_quick_submission_cannot_replay_running_or_finished_id(self):
        reached, finish = threading.Event(), threading.Event()
        request = {"requestId": "1" * 32, "operation": "poe_off", "targets": []}

        def apply_once(action, body, cancel):
            reached.set()
            self.assertTrue(finish.wait(3))
            return {"results": [], "selected": 1, "blocked": [], "skipped": [{}]}

        with patch.object(self.jobs.controls, "dispatch", side_effect=apply_once) as dispatch:
            key = self.jobs.submit("control.quick-execute", dict(request))
            try:
                self.assertTrue(reached.wait(3))
                with self.assertRaises(Busy):
                    self.jobs.submit("control.quick-execute", dict(request))
            finally:
                finish.set()
            self.assertEqual(self.finished(key)["state"], "done")
            with self.assertRaises(Busy):
                self.jobs.submit("control.quick-execute", dict(request))
            dispatch.assert_called_once()
        reopened = Jobs(self.root)
        with self.assertRaises(Busy):
            reopened.submit("control.quick-execute", dict(request))
        self.assertEqual(len(reopened.history()), 1)
        self.assertIn("건너뜀 1개", reopened.history()[0]["summary"])

    def test_cable_progress_and_details_are_scoped_to_the_running_inspection(self):
        started, finish = threading.Event(), threading.Event()
        operator_key = "b" * 32
        request = {"requestId": "a" * 32, "responseKey": operator_key}
        row = {
            "ip": "127.0.0.1",
            "ssh_port": 22,
            "interface": "1",
            "status": "partial",
            "pairs": [{"pair": "1-2", "status": "Open", "distance": "0m"}],
            "responseExcerpt": "1 1-2 Open 0m",
        }
        ops = self.jobs.operations

        def diagnose(action, body, cancel):
            self.assertNotIn("responseKey", body)
            ops._start_progress("결과 조회", 1)
            ops._progress("페어 수신", cable_row=row)
            started.set()
            self.assertTrue(finish.wait(5))
            return {"rows": [row]}

        with patch.object(ops, "dispatch", side_effect=diagnose):
            key = self.jobs.submit("cable.test", request)
            try:
                self.assertTrue(started.wait(3))
                view = self.jobs.get(key, response_key=operator_key)
                self.assertNotIn(operator_key, json.dumps(view))
                self.assertEqual(view["progress"]["cableRows"], [row])
                self.assertEqual(view["progress"]["completed"], 0)
                self.assertEqual(self.jobs.get(key, after=1)["progress"]["cableRows"], [])
                for other in (None, "c" * 32):
                    public = self.jobs.get(key, response_key=other)["progress"]["cableRows"][0]
                    self.assertNotIn("responseExcerpt", public)
                    self.assertEqual(public["pairs"], row["pairs"])
            finally:
                finish.set()
            done = self.finished(key)["result"]["rows"][0]
            self.assertNotIn("responseExcerpt", done)
            self.assertEqual(self.jobs.get(key, response_key=operator_key)["result"]["rows"], [row])
        self.assertNotIn(row["responseExcerpt"], json.dumps(self.jobs.history()))
        self.assertNotIn(operator_key.encode(), self.jobs.db.read_bytes())
        self.assertNotIn(row["responseExcerpt"].encode(), self.jobs.db.read_bytes())

    def test_prior_downloads_hide_retired_csv_and_preserve_original_files(self):
        folder = self.root / "outputs"
        folder.mkdir()
        prior = folder / "saved.CsV"
        prior.write_bytes(b"operator content")
        report = folder / "saved.txt"
        report.write_bytes(b"log content")
        attachments = [{"id": "old", "name": prior.name}, {"id": "log", "name": report.name}]
        with closing(sqlite3.connect(self.jobs.db)) as db, db:
            for item in attachments:
                db.execute(
                    "INSERT INTO downloads(id,path) VALUES(?,?)",
                    (item["id"], "outputs/" + item["name"]),
                )
            for action in ("ports.query", "ports.export"):
                db.execute(
                    "INSERT INTO jobs VALUES(?,?, 'done', '', '', '', ?)",
                    (action, action, json.dumps(attachments)),
                )
        reopened = Jobs(self.root)
        (job,) = reopened.history()
        self.assertEqual(job["action"], "ports.query")
        self.assertEqual(job["files"], [attachments[1]])
        self.assertTrue(reopened.download("log").samefile(report))
        with self.assertRaises(ValueError):
            reopened.download("old")
        with self.assertRaises(ValueError):
            reopened.submit("ports.export", {})
        self.assertEqual(prior.read_bytes(), b"operator content")

    def test_direct_quick_requires_unique_well_formed_id(self):
        for invalid in (None, "", "z" * 32, 99):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.jobs.submit("control.quick-execute", {"requestId": invalid})
        self.assertEqual([], self.jobs.history())

    def test_out_of_scope_file_marks_job_failed(self):
        target = self.root / "private.json"
        target.write_text("{}")
        with patch.object(
            self.jobs.operations, "dispatch", return_value={"files": [{"id": "private.json"}]}
        ):
            job = self.finished(self.jobs.submit("profiles.export", {}))
        self.assertEqual(job["state"], "failed")
        self.assertIsNone(job["result"])
        self.assertEqual(self.jobs.history()[0]["state"], "failed")
        self.assertEqual(self.jobs.history()[0]["files"], [])

    def test_only_one_writer_and_cancellable(self):
        started = threading.Event()

        def wait_for_cancel(action, body, cancel):
            self.jobs.operations.progress = {"message": "장비 조회", "completed": 0, "total": 1}
            started.set()
            self.assertTrue(cancel.wait(3))
            return {"rows": []}

        with patch.object(self.jobs.operations, "dispatch", side_effect=wait_for_cancel):
            job = self.jobs.submit("ports.query", {})
            self.assertTrue(started.wait(1))
            self.assertEqual(self.jobs.get(job)["progress"]["total"], 1)
            with self.assertRaises(Busy):
                self.jobs.submit("ports.query", {})
            with self.assertRaises(Busy):
                self.jobs.claim_shutdown()
            self.jobs.cancel(job)
            self.assertEqual(self.finished(job)["state"], "cancelled")
        self.jobs.claim_shutdown()
        with self.assertRaises(Busy):
            self.jobs.submit("session.close", {})

    def collect_ports(self, groups, credentials, *, on_result, **kwargs):
        (group,) = groups
        results = []
        for port in group.interfaces:
            snapshot = PortSnapshot(
                group.target,
                port,
                "edge",
                "2930F",
                True,
                "Up",
                True,
                "Delivering",
                float(port),
                "key",
            )
            row = PoEQueryResult(group, port, QueryStatus.OK, snapshot=snapshot)
            on_result(row)
            results.append(row)
        return results

    def initial_query(self):
        self.jobs.operations.credentials = Credentials("reader", "test")
        with patch.object(self.jobs.operations.query_service, "collect", self.collect_ports):
            job = self.finished(self.jobs.submit("ports.query", {"targets": "192.0.2.5:2222 1-3"}))
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["result"]["groups"], self.jobs.workspace()["groups"])
        return self.jobs.workspace()["query"]

    def test_actual_power_history_partial_manual_and_skipped_monitor_slot(self):
        ops = self.jobs.operations
        ops.credentials = Credentials("reader", "test")
        targets = "192.0.2.5:2222 1-2"

        def collect(*args, **kwargs):
            return [
                replace(
                    row, snapshot=replace(row.snapshot, observed_at=datetime.now(UTC).isoformat())
                )
                for row in self.collect_ports(*args, **kwargs)
            ]

        def history(port):
            return self.jobs.read_power_history("192.0.2.5", 2222, port, 1)["points"]

        with patch.object(ops.query_service, "collect", collect):
            self.finished(self.jobs.submit("ports.query", {"targets": targets}))
            self.finished(self.jobs.submit("ports.refresh", {"targets": "192.0.2.5:2222 2"}))
            self.assertEqual([len(history(p)) for p in ("1", "2")], [1, 2])
            started = self.jobs.start_monitor({"targets": targets, "intervalMinutes": 1})
            self.finished(started["jobId"])
            self.jobs.monitor.due = time.monotonic() - 0.1
            self.jobs.tick_monitor()
            self.finished(self.jobs.monitor.job or self.jobs.monitor.last_job)
            self.assertTrue(history("2")[-1]["connected"])
            with patch.object(self.jobs.controls, "awaiting_confirmation", return_value=True):
                self.jobs.monitor.due = time.monotonic() - 0.1
                self.jobs.tick_monitor()
            self.jobs.monitor.due = time.monotonic() - 0.1
            self.jobs.tick_monitor()
            self.finished(self.jobs.monitor.job or self.jobs.monitor.last_job)
            self.assertFalse(history("2")[-1]["connected"])
            self.assertEqual([len(history(p)) for p in ("1", "2")], [4, 5])
            with patch.object(
                self.jobs.power_history, "record", side_effect=sqlite3.OperationalError
            ):
                failed_write = self.finished(
                    self.jobs.submit("ports.refresh", {"targets": "192.0.2.5:2222 2"})
                )
            self.assertEqual(failed_write["state"], "done")
            self.assertIn("powerHistoryWarning", failed_write["result"])
            self.assertIn("warning", self.jobs.read_power_history("192.0.2.5", 2222, "2", 1))
            self.assertNotIn("warning", self.jobs.read_power_history("192.0.2.5", 2222, "1", 1))

    def test_monitor_interval_and_targets_checked_before_first_ssh_read(self):
        body = {"targets": "192.0.2.5:2222 1-3"}
        with self.assertRaises(ValueError):
            self.jobs.start_monitor(body)
        self.jobs.operations.credentials = Credentials("reader", "test")
        with patch.object(self.jobs.operations.query_service, "collect") as collect:
            for invalid in (0, 61, True, 1.0, "1", None):
                with self.subTest(interval=invalid), self.assertRaises(ValueError):
                    self.jobs.start_monitor({**body, "intervalMinutes": invalid})
            for invalid in ("", "bad", "192.0.2.5 2-1"):
                with self.subTest(targets=invalid), self.assertRaises(ValueError):
                    self.jobs.start_monitor({"targets": invalid})
            collect.assert_not_called()
        self.assertFalse(self.jobs.monitor_status()["active"])

    def test_monitor_schedule_is_anchored_to_start_and_skips_overruns(self):
        with patch("localweb.monitoring.time.monotonic", return_value=100.0) as clock:
            plan = PortMonitoring("job", (), "", 1, job="job")
            # A 20-second first read leaves 40 seconds before the next round.
            clock.return_value = 120.0
            plan.finished("job", {"rows": [{"status": "ok", "watts": 8}]}, "", "first", None)
            self.assertEqual(plan.due, 160)
            self.assertIsNotNone(plan.next_at)
            clock.return_value = 160.2
            plan.advance_to_next_slot()
            self.assertEqual(plan.due, 220)
            # A long read crosses the next two slots, leaving just one future deadline.
            clock.return_value = 295.0
            plan.finished("next", {}, "SSH timeout", "second", None)
            self.assertEqual(plan.due, 340)
            self.assertFalse(plan.suspended)

    def test_monitor_shared_start_serial_reads_and_exact_endpoints(self):
        self.jobs.operations.credentials = Credentials("reader", "test")
        body = {"targets": "192.0.2.5:2222 1-3\n192.0.2.5:2223 2", "intervalMinutes": 60}
        started, finish = threading.Event(), threading.Event()
        scopes = []

        def read(groups, credentials, **kwargs):
            scopes.append([(g.target.endpoint, g.interfaces) for g in groups])
            started.set()
            self.assertTrue(finish.wait(3))
            return [r for g in groups for r in self.collect_ports((g,), credentials, **kwargs)]

        with patch.object(self.jobs.operations.query_service, "collect", read):
            status = self.jobs.start_monitor(body)
            try:
                self.assertTrue(started.wait(3))
                self.assertEqual(status["jobId"], self.jobs.start_monitor(body)["jobId"])
                self.assertEqual(status["portCount"], 4)
                self.assertEqual(status["intervalMinutes"], 60)
                self.assertIsNone(status["nextAt"])
                with self.assertRaises(Busy):
                    self.jobs.start_monitor({**body, "intervalMinutes": 1})
                with self.assertRaises(Busy):
                    self.jobs.submit("control.quick", {})
                self.jobs.tick_monitor()
            finally:
                finish.set()
            self.assertEqual(self.finished(status["jobId"])["state"], "done")
            self.assertFalse(self.jobs.history())
            status = self.jobs.monitor_status()
            self.assertTrue(status["lastSuccess"])
            self.assertTrue(status["nextAt"])
            self.assertEqual(status["lastJobId"], status["monitorId"])
            until = self.jobs.monitor.due
            self.assertGreater(until - time.monotonic(), 3598)
            with patch("localweb.jobs.time.monotonic", return_value=until - 1):
                self.jobs.tick_monitor()
            self.assertIsNone(self.jobs.monitor_status()["jobId"])
            with (
                patch("localweb.jobs.time.monotonic", return_value=until),
                patch.object(self.jobs.controls, "awaiting_confirmation", return_value=True),
            ):
                self.jobs.tick_monitor()
            self.assertIsNone(self.jobs.monitor_status()["jobId"])
            self.assertGreater(self.jobs.monitor.due, until)
            self.jobs.monitor.due = 0
            self.jobs.tick_monitor()
            key = self.jobs.monitor_status()["jobId"] or self.jobs.monitor_status()["lastJobId"]
            self.assertEqual(self.finished(key)["action"], "ports.refresh")
        self.assertEqual(len(scopes), 2)
        self.assertEqual(scopes[0], scopes[1])
        self.assertFalse(self.jobs.stop_monitor(status["monitorId"])["active"])
        self.jobs.tick_monitor()
        self.assertIsNone(self.jobs.active)
        self.assertFalse(Jobs(self.root).monitor_status()["active"])

    def test_failed_monitor_round_keeps_last_good_display_value_until_recovery(self):
        self.jobs.operations.credentials = Credentials("reader", "test")
        body = {"targets": "192.0.2.5:2222 1-3"}
        with patch.object(self.jobs.operations.query_service, "collect", self.collect_ports):
            status = self.jobs.start_monitor(body)
            self.finished(status["jobId"])
        self.assertEqual(self.jobs.monitor_status()["intervalMinutes"], 5)
        for collect in (lambda *a, **k: (), lambda *a, **k: (), self.collect_ports):
            with patch.object(self.jobs.operations.query_service, "collect", collect):
                self.jobs.monitor.due = 0
                self.jobs.tick_monitor()
                key = self.jobs.monitor_status()["jobId"] or self.jobs.monitor_status()["lastJobId"]
                self.finished(key)
            row = self.jobs.workspace()["query"]["rows"][1]
            if row["status"] == "ok":
                self.assertEqual(row["watts"], 2)
                self.assertEqual(row["previousObservation"]["watts"], 2)
                self.assertNotIn("lastSuccessfulObservation", row)
                self.assertFalse(self.jobs.monitor_status()["error"])
            else:
                self.assertEqual(row["eligible_operations"], ["disable"])
                self.assertTrue(row["controlUsed"])
                self.assertIsNone(row["watts"])
                self.assertEqual(row["lastSuccessfulObservation"]["watts"], 2)
                self.assertTrue(self.jobs.monitor_status()["error"])
        self.finished(self.jobs.submit("session.close", {}))
        self.assertFalse(self.jobs.monitor_status()["active"])

    def test_monitor_manual_query_scope_change_and_auto_shutdown(self):
        self.jobs.operations.credentials = Credentials("reader", "test")
        body = {"targets": "192.0.2.5 1-3"}
        with patch.object(self.jobs.operations.query_service, "collect", self.collect_ports):
            status = self.jobs.start_monitor(body)
            self.finished(status["jobId"])
            self.finished(self.jobs.submit("ports.query", dict(body)))
            self.assertTrue(self.jobs.monitor_status()["active"])
            self.finished(self.jobs.submit("ports.query", {"targets": "192.0.2.5 2"}))
            self.assertFalse(self.jobs.monitor_status()["active"])
            status = self.jobs.start_monitor(body)
            self.finished(status["jobId"])
            self.assertTrue(self.jobs.stop_monitor("obsolete-monitor-id")["active"])
        self.jobs.claim_shutdown()
        self.assertFalse(self.jobs.monitor_status()["active"])

    def test_stop_monitor_cancels_an_active_read_without_rearming(self):
        self.jobs.operations.credentials = Credentials("reader", "test")
        started = threading.Event()

        def read(groups, credentials, *, cancel_event, **kwargs):
            started.set()
            self.assertTrue(cancel_event.wait(3))
            return ()

        with patch.object(self.jobs.operations.query_service, "collect", read):
            status = self.jobs.start_monitor({"targets": "192.0.2.5 1", "intervalMinutes": 1})
            self.assertTrue(started.wait(3))
            self.assertFalse(self.jobs.stop_monitor(status["monitorId"])["active"])
            self.assertEqual(self.finished(status["jobId"])["state"], "cancelled")
        self.jobs.tick_monitor()
        self.assertFalse(self.jobs.monitor_status()["active"])
        self.assertIsNone(self.jobs.active)

    def test_explicit_auth_failure_suspends_reads_but_keeps_last_good_observation(self):
        self.jobs.operations.credentials = Credentials("reader", "test")
        body = {"targets": "192.0.2.5:2222 1-3", "intervalMinutes": 1}
        with patch.object(self.jobs.operations.query_service, "collect", self.collect_ports):
            initial = self.jobs.start_monitor(body)
            self.finished(initial["jobId"])

        def rejected(groups, credentials, **kwargs):
            return [
                PoEQueryResult(g, p, QueryStatus.FAILED, error_code=ErrorCode.AUTH_FAILED)
                for g in groups
                for p in g.interfaces
            ]

        for fail in (rejected, CollectionFailure(ErrorCode.AUTH_FAILED, "거부됨")):
            with patch.object(
                self.jobs.operations.query_service, "collect", side_effect=fail
            ) as collect:
                if callable(fail):
                    self.jobs.monitor.due = 0
                    self.jobs.tick_monitor()
                    running = self.jobs.monitor_status()
                else:
                    running = self.jobs.start_monitor(body)
                self.finished(running["jobId"] or running["lastJobId"])
                paused = self.jobs.monitor_status()
                self.assertTrue(paused["active"])
                self.assertTrue(paused["suspended"])
                self.assertIsNone(paused["nextAt"])
                self.assertIn("SSH 인증 거부", paused["error"])
                if callable(fail):
                    self.assertIn("192.0.2.5:2222", paused["error"])
                    row = self.jobs.workspace()["query"]["rows"][1]
                    self.assertIsNone(row["watts"])
                    self.assertEqual(row["lastSuccessfulObservation"]["watts"], 2)
                self.jobs.monitor.due = 0
                self.jobs.tick_monitor()
                self.assertIsNone(self.jobs.active)
                collect.assert_called_once()
                self.assertEqual(self.jobs.start_monitor(body)["monitorId"], paused["monitorId"])
                self.jobs.stop_monitor(paused["monitorId"])

    def test_busy_foreground_skips_round_and_never_cancels_foreground(self):
        self.jobs.operations.credentials = Credentials("reader", "test")
        with patch.object(self.jobs.operations.query_service, "collect", self.collect_ports):
            status = self.jobs.start_monitor({"targets": "192.0.2.5 1", "intervalMinutes": 1})
            self.finished(status["jobId"])
        started, finish = threading.Event(), threading.Event()

        def foreground(action, body, cancel):
            started.set()
            self.assertTrue(finish.wait(3))
            self.assertFalse(cancel.is_set())
            return {}

        with patch.object(self.jobs.controls, "dispatch", foreground):
            key = self.jobs.submit("control.apply", {})
            try:
                self.assertTrue(started.wait(3))
                self.jobs.monitor.due = time.monotonic() - 0.5
                self.jobs.tick_monitor()
                self.assertEqual(self.jobs.active, key)
                self.assertGreater(self.jobs.monitor.due, time.monotonic() + 58)
                self.assertFalse(self.jobs.stop_monitor()["active"])
            finally:
                finish.set()
            self.assertEqual(self.finished(key)["state"], "done")

    def test_port_comparison_uses_only_the_previous_sample_of_the_exact_endpoint(self):
        first = self.initial_query()["rows"]
        self.assertTrue(all("previousObservation" not in row for row in first))
        service = self.jobs.operations.query_service

        def query(action, target, watts):
            def collect(*args, **kwargs):
                return [
                    replace(read, snapshot=replace(read.snapshot, watts=watts))
                    for read in self.collect_ports(*args, **kwargs)
                ]

            with patch.object(service, "collect", collect):
                result = self.finished(self.jobs.submit(action, {"targets": target}))
            self.assertEqual(result["state"], "done")
            rows = result["result"]["rows"]
            restored = self.jobs.workspace()["query"]["rows"]
            self.assertEqual(
                [row.get("previousObservation") for row in rows],
                [row.get("previousObservation") for row in restored],
            )
            self.assertNotIn("_sampledPorts", result["result"])
            return rows

        one, two, three = query("ports.refresh", "192.0.2.5:2222 2", 0)
        self.assertNotIn("previousObservation", one)
        self.assertEqual((two["previousObservation"]["watts"], two["watts"]), (2, 0))
        self.assertNotIn("previousObservation", three)
        unrefreshed = query("ports.refresh", "192.0.2.5:2222 1", 5)[1]
        self.assertEqual(unrefreshed["previousObservation"], two["previousObservation"])
        again = query("ports.query", "192.0.2.5:2222 1", 3)[0]["previousObservation"]
        self.assertEqual(again["watts"], 5)
        self.assertNotIn("previousObservation", again)
        self.assertNotIn("eligible_operations", again)
        changed_endpoint = query("ports.query", "192.0.2.5:2223 1", 1)[0]
        self.assertNotIn("previousObservation", changed_endpoint)

    def test_editable_target_text_tracks_accepted_full_query_only(self):
        self.initial_query()
        original = "  192.0.2.5:2222  1-3\n"
        with patch.object(self.jobs.operations.query_service, "collect", self.collect_ports):
            full = self.finished(self.jobs.submit("ports.query", {"targets": original}))
            self.assertEqual(full["state"], "done")
            self.assertEqual(self.jobs.workspace()["targetText"], original)
            refresh = self.finished(
                self.jobs.submit("ports.refresh", {"targets": "192.0.2.5:2222 2"})
            )
            self.assertEqual(refresh["state"], "done")
            self.assertEqual(self.jobs.workspace()["targetText"], original)

        invalid = self.finished(self.jobs.submit("ports.query", {"targets": "invalid"}))
        self.assertEqual(invalid["state"], "failed")
        self.assertEqual(self.jobs.workspace()["targetText"], original)
        with patch.object(self.jobs.operations.query_service, "collect", side_effect=OSError()):
            failed = self.finished(self.jobs.submit("ports.query", {"targets": original + "\n"}))
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(self.jobs.workspace()["targetText"], original + "\n")
        self.finished(self.jobs.submit("session.close", {}))
        self.assertNotIn("targetText", self.jobs.workspace())

    def test_rejected_query_draft_preserves_live_workspace_until_new_targets_are_accepted(self):
        self.initial_query()
        saved = self.jobs.workspace()
        ops = self.jobs.operations
        with patch.object(ops.query_service, "collect") as collect:
            rejected = self.finished(self.jobs.submit("ports.query", {"targets": "bad input"}))
            self.assertEqual(rejected["state"], "failed")
            collect.assert_not_called()
        self.assertEqual(self.jobs.workspace(), saved)
        self.assertNotIn("targets", ops.query_progress(0))

        with patch.object(ops.query_service, "collect", side_effect=OSError("read failed")):
            accepted = self.finished(self.jobs.submit("ports.query", {"targets": "192.0.2.9 4"}))
        self.assertEqual(accepted["state"], "failed")
        self.assertNotIn("query", self.jobs.workspace())
        groups = self.jobs.workspace()["groups"]
        self.assertEqual([(g["ip"], g["interfaces"]) for g in groups], [("192.0.2.9", ["4"])])

    def test_failed_refresh_keeps_other_rows_and_cannot_restore_stale_control(self):
        for failure in ("collection", "cancel"):
            with self.subTest(failure=failure):
                initial = self.initial_query()
                expected1, _, expected3 = initial["rows"]

                def collect(*args, failure=failure, **kwargs):
                    if failure == "cancel":
                        kwargs["cancel_event"].set()
                    raise OSError("read failed")

                ops = self.jobs.operations
                with patch.object(ops.query_service, "collect", collect):
                    job = self.finished(
                        self.jobs.submit("ports.refresh", {"targets": "192.0.2.5:2222 2"})
                    )
                self.assertEqual(job["state"], "cancelled" if failure == "cancel" else "failed")
                view = self.jobs.workspace()["query"]
                self.assertNotIn("files", view)
                first, invalid, third = view["rows"]
                self.assertEqual((first, third), (expected1, expected3))
                self.assertEqual(
                    invalid["status"], "cancelled" if failure == "cancel" else "failed"
                )
                self.assertIsNone(invalid["watts"])
                self.assertTrue(invalid["controlUsed"] and invalid["refreshInvalidated"])
                self.assertEqual(invalid["eligible_operations"], ["disable"])
                self.assertIsNone(ops.last_results[1].snapshot)
                self.assertEqual([row["watts"] for row in view["rows"]], [1.0, None, 3.0])

    def test_refresh_job_preserves_reviewed_batch_disable_on_the_same_registered_port(self):
        self.initial_query()
        ops = self.jobs.operations
        snapshot = ops.last_results[1].snapshot
        target = {
            "ip": snapshot.target.ip,
            "sshPort": snapshot.target.port,
            "port": snapshot.interface,
        }
        prepared = self.finished(
            self.jobs.submit(
                "control.preview", {"scope": "batch", "operation": "disable", "targets": [target]}
            )
        )
        self.assertEqual(prepared["state"], "done")
        with patch.object(ops.query_service, "collect", self.collect_ports):
            refresh = self.finished(
                self.jobs.submit("ports.refresh", {"targets": "192.0.2.5:2222 2"})
            )
        self.assertEqual(refresh["state"], "done")
        plan = prepared["result"]
        service = Mock(spec=PortControlService)
        service.inspect.return_value = snapshot
        service.apply.return_value = PortOperationResult(
            "verified", "", snapshot, replace(snapshot, enabled=False)
        )
        with patch.object(self.jobs.controls, "_factory", return_value=service):
            applied = self.finished(
                self.jobs.submit(
                    "control.apply",
                    {
                        "planId": plan["planId"],
                        "confirmation": plan["confirmationText"],
                    },
                )
            )
        self.assertEqual(applied["state"], "done")
        self.assertEqual(applied["result"]["results"][0]["status"], "verified")
        service.apply.assert_called_once()

    def test_reviewed_poe_refreshes_matrix_without_creating_downloads(self):
        self.initial_query()
        ops = self.jobs.operations
        snap = ops.last_results[1].snapshot
        service = Mock(spec=PortControlService)
        service.inspect.return_value = snap
        service.apply.return_value = PortOperationResult("verified", "설정 변경 확인", snap, snap)
        target = {"ip": snap.target.ip, "sshPort": snap.target.port, "port": snap.interface}
        with patch.object(self.jobs.controls, "_factory", return_value=service):
            prepared = self.finished(
                self.jobs.submit(
                    "control.preview",
                    {"scope": "single", "operation": "poe_off", "targets": [target]},
                )
            )
            plan = prepared["result"]

            def read_power(groups, creds, **kwargs):
                (row,) = self.collect_ports(groups, creds, **kwargs)
                return [
                    replace(
                        row,
                        snapshot=replace(
                            row.snapshot,
                            watts=0.0,
                            poe_enabled=False,
                            observed_at=datetime.now(UTC).isoformat(),
                        ),
                    )
                ]

            with (
                patch.object(ops.query_service, "collect", read_power),
                patch.object(self.jobs, "_output") as register,
            ):
                job = self.finished(
                    self.jobs.submit(
                        "control.apply",
                        {"planId": plan["planId"], "confirmation": plan["confirmationText"]},
                    )
                )
        self.assertEqual(job["state"], "done")
        result = job["result"]
        self.assertEqual(result["results"][0]["status"], "verified")
        self.assertEqual(result["powerRefresh"]["readings"][0]["watts"], 0.0)
        points = self.jobs.read_power_history(snap.target.ip, snap.target.port, snap.interface, 1)[
            "points"
        ]
        self.assertEqual([p["watts"] for p in points], [0])
        self.assertEqual(points[0]["at"], result["powerRefresh"]["readings"][0]["observed"])
        self.assertNotIn("_powerHistoryRows", result)
        register.assert_not_called()
        self.assertNotIn("files", result)
        restored = self.jobs.workspace()["query"]
        self.assertEqual([r["watts"] for r in restored["rows"]], [1.0, 0, 3.0])
        self.assertNotIn("files", restored)
        self.assertEqual(restored["rows"][1]["previousObservation"]["watts"], 2)
        service.apply.assert_called_once()

    def test_partial_return_updates_live_matrix_and_only_missing_port_loses_authority(self):
        old = self.initial_query()

        def partial(groups, *args, **kwargs):
            returned = self.collect_ports(groups, *args, **kwargs)
            return returned[:1]

        ops = self.jobs.operations
        with patch.object(ops.query_service, "collect", partial):
            job = self.finished(
                self.jobs.submit("ports.refresh", {"targets": "192.0.2.5:2222 2,3"})
            )
        self.assertEqual(job["state"], "done")
        view = self.jobs.workspace()["query"]
        self.assertNotIn("files", view)
        self.assertEqual(view["rows"][0], old["rows"][0])
        self.assertFalse(view["rows"][1]["controlUsed"])
        self.assertTrue(view["rows"][2]["refreshInvalidated"])
        self.assertIsNone(ops.last_results[2].snapshot)
        self.assertEqual([row["watts"] for row in view["rows"]], [1.0, 2.0, None])
        self.assertEqual(view["rows"][2]["status"], "failed")
        with patch.object(ops.query_service, "collect", self.collect_ports):
            self.finished(self.jobs.submit("ports.refresh", {"targets": "192.0.2.5:2222 3"}))
        restored = self.jobs.workspace()["query"]["rows"][2]
        self.assertEqual(restored["watts"], 3.0)
        self.assertFalse(restored["controlUsed"] or restored.get("refreshInvalidated"))

    def test_cancel_before_collection_invalidates_only_the_registered_requested_port(self):
        initial = self.initial_query()
        ops = self.jobs.operations
        dispatch = ops.dispatch

        def stopped(action, body, cancel):
            cancel.set()
            return dispatch(action, body, cancel)

        with (
            patch.object(ops, "dispatch", stopped),
            patch.object(ops.query_service, "collect") as collect,
        ):
            result = self.finished(
                self.jobs.submit("ports.refresh", {"targets": "192.0.2.5:2222 2"})
            )
        collect.assert_not_called()
        self.assertEqual(result["state"], "cancelled")
        view = self.jobs.workspace()["query"]
        self.assertNotIn("files", view)
        self.assertEqual(view["rows"][0], initial["rows"][0])
        self.assertEqual(view["rows"][1]["status"], "cancelled")
        self.assertIsNone(view["rows"][1]["watts"])
        self.assertEqual(view["rows"][2], initial["rows"][2])
        invalid = self.finished(self.jobs.submit("ports.refresh", {"targets": "192.0.2.5:2223 2"}))
        self.assertEqual(invalid["state"], "failed")
        self.assertEqual(self.jobs.workspace()["query"], view)
