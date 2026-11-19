"""Deadline and ownership rules for the local browser server lifecycle."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from localweb.jobs import Busy, Jobs
from server import Handler, LocalServer


class BrowserPresenceTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.server = self.enterContext(LocalServer(("127.0.0.1", 0), Handler))
        self.server.jobs = Jobs(Path(directory))
        self.now = 100.0
        self.enterContext(patch("server.time.monotonic", side_effect=lambda: self.now))
        # There is no serve_forever loop in this unit test. Observe the shutdown request directly.
        self.stop_thread = self.enterContext(patch("server.threading.Thread"))

    def advance(self, seconds):
        self.now += seconds
        self.server.service_actions()

    def assert_running(self):
        self.assertFalse(self.server.jobs.closing)
        self.stop_thread.assert_not_called()

    def assert_stopping(self):
        self.assertTrue(self.server.jobs.closing)
        self.stop_thread.assert_called_once_with(target=self.server.shutdown, daemon=True)
        self.stop_thread.return_value.start.assert_called_once()

    def test_initial_navigation_arms_cleanup_only_until_an_owner_connects(self):
        self.advance(3600)
        self.assert_running()
        self.server.browser_opening()
        self.advance(self.server.browser_attach_grace - 1)
        self.assert_running()
        with self.server.browser_connection(object()):
            self.advance(self.server.browser_attach_grace)
            self.assert_running()
        self.advance(self.server.browser_close_grace - 1)
        self.assert_running()
        self.advance(1)
        self.assert_stopping()

    def test_only_last_owner_starts_the_close_grace(self):
        first = self.server.browser_connection(object())
        with self.server.browser_connection(object()):
            with first:
                self.advance(20)
            self.advance(60)
            self.assert_running()
        self.advance(self.server.browser_close_grace - 1)
        with self.server.browser_connection(object()):
            self.advance(60)
            self.assert_running()
        self.advance(self.server.browser_close_grace)
        self.assert_stopping()
        with self.assertRaises(Busy), self.server.browser_connection(object()):
            self.fail("The committed shutdown accepted a new browser")
        with self.assertRaises(Busy):
            self.server.jobs.submit("session.close", {})

    def test_jobs_drain_before_closing_and_a_returning_tab_keeps_the_server(self):
        with self.server.browser_connection(object()):
            self.server.jobs.active = "backup in progress"
        self.advance(self.server.browser_close_grace)
        self.assert_running()
        with self.server.browser_connection(object()):
            self.server.jobs.active = None
            self.advance(60)
            self.assert_running()
            self.server.jobs.active = "second backup"
        self.advance(self.server.browser_close_grace + 10)
        self.assert_running()
        self.server.jobs.active = None
        self.advance(1)
        self.assert_stopping()
        self.advance(60)
        self.assert_stopping()


if __name__ == "__main__":
    unittest.main()
