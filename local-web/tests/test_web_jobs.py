"""Local job persistence, serialization, and the download registry."""

import csv
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from localweb.jobs import Busy, Jobs

from aruba2930f_backup.models import Credentials
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
        return self.jobs.workspace()["query"]

    def test_failed_refresh_keeps_other_rows_and_cannot_restore_stale_control_or_csv(self):
        for failure in ("collection", "cancel", "export"):
            with self.subTest(failure=failure):
                initial = self.initial_query()
                expected1, _, expected3 = initial["rows"]

                def collect(*args, failure=failure, **kwargs):
                    if failure == "cancel":
                        kwargs["cancel_event"].set()
                    if failure == "export":
                        return self.collect_ports(*args, **kwargs)
                    raise OSError("read failed")

                ops = self.jobs.operations
                with (
                    patch.object(ops.query_service, "collect", collect),
                    patch.object(ops, "_export_query", side_effect=OSError("output unavailable")),
                ):
                    job = self.finished(
                        self.jobs.submit("ports.refresh", {"targets": "192.0.2.5:2222 2"})
                    )
                self.assertEqual(job["state"], "cancelled" if failure == "cancel" else "failed")
                view = self.jobs.workspace()["query"]
                self.assertEqual(view["files"], [])
                first, invalid, third = view["rows"]
                self.assertEqual((first, third), (expected1, expected3))
                self.assertEqual(
                    invalid["status"], "cancelled" if failure == "cancel" else "failed"
                )
                self.assertIsNone(invalid["watts"])
                self.assertTrue(invalid["controlUsed"] and invalid["refreshInvalidated"])
                self.assertEqual(invalid["eligible_operations"], [])
                self.assertIsNone(ops.last_results[1].snapshot)
                manual = self.finished(self.jobs.submit("ports.export", {}))
                with self.jobs.download(manual["result"]["files"][0]["id"]).open(
                    encoding="utf-8-sig", newline=""
                ) as stream:
                    saved = list(csv.DictReader(stream))
                self.assertEqual([row["watts"] for row in saved], ["1.0", "", "3.0"])
                self.assertEqual(self.jobs.workspace()["query"]["files"], [])

    def test_reviewed_poe_restores_new_matrix_when_csv_download_registration_fails(self):
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
                return [replace(row, snapshot=replace(row.snapshot, watts=0.0, poe_enabled=False))]

            with (
                patch.object(ops.query_service, "collect", read_power),
                patch.object(self.jobs, "_output", side_effect=OSError("CSV no longer available")),
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
        self.assertIn("CSV", result["powerRefresh"]["message"])
        self.assertEqual(result["files"], [])
        restored = self.jobs.workspace()["query"]
        self.assertEqual([r["watts"] for r in restored["rows"]], ["1.0", "0", "3.0"])
        self.assertEqual(restored["files"], [])
        service.apply.assert_called_once()

    def test_partial_return_generates_current_csv_and_only_missing_port_loses_authority(self):
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
        self.assertNotEqual(view["files"], old["files"])
        self.assertEqual(view["rows"][0], old["rows"][0])
        self.assertFalse(view["rows"][1]["controlUsed"])
        self.assertTrue(view["rows"][2]["refreshInvalidated"])
        self.assertIsNone(ops.last_results[2].snapshot)
        with self.jobs.download(view["files"][0]["id"]).open(
            encoding="utf-8-sig", newline=""
        ) as stream:
            saved = list(csv.DictReader(stream))
        self.assertEqual([row["watts"] for row in saved], ["1.0", "2.0", ""])
        self.assertEqual(saved[2]["status"], "failed")
        with patch.object(ops.query_service, "collect", self.collect_ports):
            self.finished(self.jobs.submit("ports.refresh", {"targets": "192.0.2.5:2222 3"}))
        restored = self.jobs.workspace()["query"]["rows"][2]
        self.assertEqual(restored["watts"], "3.0")
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
        self.assertEqual(view["files"], [])
        self.assertEqual(view["rows"][0], initial["rows"][0])
        self.assertEqual(view["rows"][1]["status"], "cancelled")
        self.assertIsNone(view["rows"][1]["watts"])
        self.assertEqual(view["rows"][2], initial["rows"][2])
        invalid = self.finished(self.jobs.submit("ports.refresh", {"targets": "192.0.2.5:2223 2"}))
        self.assertEqual(invalid["state"], "failed")
        self.assertEqual(self.jobs.workspace()["query"], view)
