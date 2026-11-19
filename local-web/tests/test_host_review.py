import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from localweb.host_review import HostKeyReview


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.review = HostKeyReview()
        self.cancel = threading.Event()
        self.checks = [
            SimpleNamespace(
                target=SimpleNamespace(endpoint=f"192.0.2.{i}:22"),
                observation=SimpleNamespace(key_type="test", fingerprint=f"test-{i}"),
            )
            for i in (1, 2)
        ]

    def wait_request(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            pending = self.review.snapshot()
            if pending:
                return pending[0]
            time.sleep(0.005)
        self.fail("Host-key review did not open")

    def test_fingerprint_mismatch_and_replay(self):
        with ThreadPoolExecutor(1) as worker:
            waiting = worker.submit(self.review.ask, self.checks, self.cancel)
            try:
                request = self.wait_request()
                first = request["checks"][0]
                with self.assertRaises(ValueError):
                    self.review.answer(
                        dict(requestId=request["id"], approved=[dict(first, fingerprint="changed")])
                    )
                self.assertFalse(waiting.done())
                answer = dict(requestId=request["id"], approved=[first])
                self.review.answer(answer)
                self.assertEqual(waiting.result(1), self.checks[:1])
                with self.assertRaises(ValueError):
                    self.review.answer(answer)
            finally:
                self.cancel.set()

    def test_cancelled_job_clears_all_unanswered_keys(self):
        with ThreadPoolExecutor(1) as worker:
            waiting = worker.submit(self.review.ask, self.checks, self.cancel)
            self.wait_request()
            self.cancel.set()
            self.assertEqual(waiting.result(1), [])
            self.assertEqual(self.review.snapshot(), [])
