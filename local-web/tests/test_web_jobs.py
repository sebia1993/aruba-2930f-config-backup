"""Local job persistence, serialization, and the download registry."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from localweb.jobs import Busy, Jobs


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
