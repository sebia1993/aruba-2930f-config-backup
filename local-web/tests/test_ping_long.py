"""Bounded, deterministic long-Ping tests. All command sends are synthetic."""

import sqlite3
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from localweb.jobs import Jobs
from localweb.ping_long import LongPing
from localweb.ping_tools import PingObservation

from aruba2930f_backup.models import CollectionFailure, Credentials, ErrorCode


class Clock:
    origin = time.time()
    value = 0.0
    correction = 0.0

    def wall(self):
        return self.origin + self.value + self.correction


class LongPingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.jobs = Jobs(directory.name)
        self.jobs.operations.credentials = Credentials("fixture-reader", "fixture")
        self.clock = Clock()
        self.engine = LongPing(self.jobs, monotonic=lambda: self.clock.value, wall=self.clock.wall)
        self.jobs.long_ping = self.engine
        self.key = "c" * 32
        self.settings = {
            "plan": {"targets": "192.0.2.10", "destinations": [{"ip": "198.51.100.10"}]},
            "count": 2,
            "interval": 5,
        }
        self.result = PingObservation("normal", 1, 1, 0, rtt_avg_ms=2)
        self.calls = []

    def start(self):
        return self.engine.submit({"requestId": self.key, "settings": self.settings})

    def fake_send(self, plan, target, dest, creds, cancel, *, before_send):
        self.assertEqual(plan.options.repetitions, 1)
        before_send()
        self.calls.append((self.clock.value, target.endpoint, dest.ip))
        return SimpleNamespace(
            observation=self.result, transport_error=None, verified_source_ip=target.ip
        )

    def tick(self, fn=None):
        with patch.object(self.jobs.operations, "_ping_pair", side_effect=fn or self.fake_send):
            self.jobs.tick_monitor()
            end = time.monotonic() + 3
            while time.monotonic() < end:
                with self.jobs.lock:
                    if not self.jobs.active:
                        return
                time.sleep(0.005)
        self.fail("SSH slot was not released")

    def status(self):
        return self.engine.status(self.key)

    def samples(self):
        with self.engine.history.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM ping_long_samples ORDER BY id")]

    def test_per_pair_goal_time_and_rate_limited_without_enumerating_attempts(self):
        self.settings["count"] = 10_000
        preview = self.engine.preview(self.settings)
        self.assertEqual((preview["total"], preview["initialEta"]), (10_000, 50_000))
        self.settings["plan"]["targets"] = "\n".join(f"192.0.2.{i}" for i in range(1, 21))
        preview = self.engine.preview(self.settings)
        self.assertEqual(preview["total"], 200_000)
        self.assertGreaterEqual(preview["initialEta"], 100_000)
        self.assertFalse(preview["overDeadline"])
        self.assertEqual(preview["effectiveConcurrency"], 5)
        self.settings["plan"]["destinations"].append({"ip": "198.51.100.23"})
        with self.assertRaises(ValueError):
            self.start()

    def test_durable_admission_and_sampling_no_packet_replay_or_fake_loss(self):
        self.assertEqual(self.start(), self.start())
        self.tick()
        self.assertEqual(self.samples()[0]["attempt"], 1)
        self.assertEqual(self.samples()[0]["rtt_ms"], 2)
        self.assertEqual(self.status()["completed"], 1)
        self.tick()
        self.assertEqual(len(self.calls), 1)
        self.clock.value = 23
        self.result = PingObservation("unknown")
        self.tick()
        status = self.status()
        self.assertEqual(
            (status["state"], status["completed"], status["success"], status["loss"]),
            ("done", 2, 1, 0),
        )
        self.assertEqual(status["gaps"], 3)
        self.assertEqual(status["etaSeconds"], 0)
        recovered = LongPing(self.jobs).status(self.key)
        self.assertEqual(
            (recovered["state"], recovered["completed"], recovered["gaps"]), ("done", 2, 3)
        )
        self.settings["count"] = 3
        with self.assertRaises(ValueError):
            self.start()

    def test_pause_releases_slot_and_resume_has_gap_no_burst_absolute_timeout(self):
        self.start()
        self.tick()
        self.engine.control(self.key, "pause")
        self.clock.value = 100
        self.tick()
        self.assertEqual(self.status()["state"], "paused")
        self.assertIsNone(self.status()["etaSeconds"])
        self.engine.control(self.key, "resume")
        self.assertEqual(self.status()["pausedSeconds"], 100)
        self.tick()
        status = self.status()
        self.assertEqual(status["completed"], 2)
        self.assertEqual(status["gaps"], 20)
        self.assertEqual(self.samples()[1]["reason"], "USER_PAUSED")
        self.key = "d" * 32
        self.settings["maxHours"] = 1
        self.start()
        self.engine.control(self.key, "pause")
        self.clock.value += 3600
        self.tick()
        self.assertEqual((self.status()["reason"], self.status()["completed"]), ("TIME_LIMIT", 0))
        self.engine.control(self.key, "resume")
        self.assertEqual(self.status()["state"], "cancelled")

    def test_preflight_errors_never_consume_attempts_and_auth_stops_all(self):
        self.start()

        def failed(*unused, **kwargs):
            raise CollectionFailure(ErrorCode.AUTH_FAILED, "fixture")

        self.tick(failed)
        self.assertEqual(
            (self.status()["state"], self.status()["started"], self.status()["gaps"]),
            ("failed", 0, 1),
        )
        self.assertIsNone(self.samples()[0]["started_at"])
        self.assertIsNone(self.samples()[0]["attempt"])

    def test_inflight_intent_is_visible_pause_and_recovery_never_reissues(self):
        self.start()
        entered, release = threading.Event(), threading.Event()

        def delayed(*args, before_send):
            result = self.fake_send(*args, before_send=before_send)
            entered.set()
            release.wait(3)
            return result

        with patch.object(self.jobs.operations, "_ping_pair", side_effect=delayed):
            self.jobs.tick_monitor()
            self.assertTrue(entered.wait(2))
            try:
                self.assertEqual((self.status()["started"], self.status()["completed"]), (1, 0))
                self.engine.control(self.key, "pause")
                self.assertEqual(self.status()["state"], "pausing")
                # Copy SQLite while an on-wire command has no result and simulate a fresh process.
                with tempfile.TemporaryDirectory() as sibling:
                    from contextlib import closing
                    from pathlib import Path

                    with (
                        self.engine.history.connect() as source,
                        closing(sqlite3.connect(Path(sibling) / "ping-history.sqlite")) as dest,
                    ):
                        source.backup(dest)
                    recovered = Jobs(sibling).long_ping
                    snap = recovered.status(self.key)
                    self.assertEqual(
                        (snap["state"], snap["completed"], snap["loss"]), ("interrupted", 1, 0)
                    )
                    self.assertIsNone(recovered.current()["id"])
            finally:
                release.set()
            end = time.monotonic() + 3
            while self.status()["state"] == "pausing" and time.monotonic() < end:
                time.sleep(0.005)
            self.assertEqual(self.status()["state"], "paused")
            self.assertIsNone(self.jobs.active)
        self.engine.control(self.key, "cancel")
        self.assertEqual(self.status()["completed"], 1)

    def test_scheduler_yields_and_shares_rate_and_distinct_pair_targets(self):
        self.settings["count"] = 1
        self.settings["plan"]["targets"] += "\n192.0.2.20"
        self.settings["plan"]["destinations"].append({"ip": "198.51.100.23"})
        self.start()
        with patch.object(self.jobs.controls, "awaiting_confirmation", return_value=True):
            self.tick()
        self.assertEqual(self.status()["wait"], "OTHER_OPERATION")
        self.assertFalse(self.calls)
        for clock in [0, 0.1, 0.21, 0.41, 0.51, 0.72]:
            self.clock.value = clock
            self.tick()
        self.assertEqual(self.status()["state"], "done")
        self.assertEqual(len(set((t, d) for _, t, d in self.calls)), 4)
        for a, b in zip(self.calls, self.calls[1:], strict=False):
            self.assertGreaterEqual(b[0] - a[0], 0.2)
        for dest in {r[2] for r in self.calls}:
            starts = [r[0] for r in self.calls if r[2] == dest]
            self.assertGreaterEqual(starts[1] - starts[0], 0.5)

    def test_eta_increases_after_delay_wall_clock_jump_never_changes_counter(self):
        self.settings["count"] = 8
        self.start()
        original = self.status()["etaSeconds"]
        self.tick()
        self.clock.value = 55
        self.clock.correction = -600
        self.tick()
        snap = self.status()
        self.assertGreater(snap["etaSeconds"], original)
        self.assertEqual(snap["completed"], 2)
        self.assertTrue(snap["clockUncertain"])
        self.assertEqual(self.samples()[-1]["clock_uncertain"], 1)
        self.engine.stop("SERVER_SHUTDOWN")
        self.assertIsNone(self.status()["etaSeconds"])

    def test_trace_once_only_for_consecutive_confirmed_loss(self):
        self.settings.update(count=6, autoTrace=True)
        self.result = PingObservation("no_reply", 1, 0, 100)
        self.start()
        with patch.object(self.jobs, "offer_ping_trace") as offer:
            for n in range(4):
                self.clock.value = 5 * n
                self.tick()
            self.assertEqual(offer.call_count, 1)
            self.clock.value = 30
            self.tick()
            self.clock.value = 35
            self.tick()
            self.assertEqual(offer.call_count, 1)
            self.assertEqual(self.status()["gaps"], 2)

    def test_five_distinct_switches_can_hold_one_reservation_and_pause_drains(self):
        self.settings["plan"]["targets"] = "\n".join(f"192.0.2.{i}" for i in range(1, 8))
        self.settings["count"] = 1
        self.start()
        ready = threading.Semaphore(0)
        release = threading.Event()

        def hold(*args, before_send):
            result = self.fake_send(*args, before_send=before_send)
            ready.release()
            release.wait(5)
            return result

        with patch.object(self.jobs.operations, "_ping_pair", side_effect=hold):
            try:
                for i in range(5):
                    self.clock.value = i * 0.5
                    self.jobs.tick_monitor()
                    self.assertTrue(ready.acquire(timeout=2))
                self.clock.value = 3
                self.jobs.tick_monitor()
                self.assertEqual(len(self.calls), 5)
                self.assertEqual(self.status()["started"], 5)
                self.assertEqual(len({item[1] for item in self.calls}), 5)
                self.assertTrue(self.engine.owns(self.jobs.active))
                self.engine.control(self.key, "pause")
                self.assertEqual(self.status()["state"], "pausing")
            finally:
                release.set()
            end = time.monotonic() + 3
            while self.status()["state"] == "pausing" and time.monotonic() < end:
                time.sleep(0.005)
            self.assertEqual((self.jobs.active, self.status()["completed"]), (None, 5))
            self.assertEqual(self.status()["state"], "paused")

    def test_retention_removes_only_expired_terminal_history_and_retains_receipt(self):
        self.settings["count"] = 1
        self.start()
        self.tick()
        first = self.key
        history = self.engine.history
        history.set_retention(1, 64)
        self.assertEqual(history.policy(), {"days": 1, "maxMb": 64})
        self.key = "e" * 32
        self.start()
        with history.connect() as db:
            db.execute(
                "UPDATE ping_long_runs SET finished_at='2000-01-01T00:00:00.000+00:00' WHERE id=?",
                (first,),
            )
            self.assertFalse(history.delete_completed(db, self.key))
            history.prune(db)
            self.assertEqual(db.execute("SELECT count(*) FROM ping_long_samples").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM ping_long_series").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.key = first
        with self.assertRaises(KeyError):
            self.status()
        with self.assertRaises(ValueError):
            self.start()

    def test_storage_failure_before_send_fails_closed(self):
        self.start()
        with patch.object(self.engine, "_check_disk", side_effect=[1000, OSError("fixture")]):
            self.tick()
        self.assertFalse(self.calls)
        self.assertEqual(self.status()["reason"], "PING_HISTORY_WRITE_FAILED")
        self.assertIsNone(self.jobs.active)
        self.assertEqual(self.status()["started"], 0)

    def test_same_public_receipt_id_never_steals_foreground_lease(self):
        self.start()
        self.jobs.active = self.key
        try:
            self.engine.tick()
            self.assertFalse(self.engine.owns(self.key))
            self.assertFalse(self.engine.run.slots)
            self.assertEqual(self.jobs.active, self.key)
        finally:
            self.jobs.active = None
        self.tick()
        self.assertEqual(self.status()["started"], 1)

    def test_out_of_order_pause_resume_does_not_overwrite_new_operator_decision(self):
        self.start()
        rev = self.status()["revision"]
        paused = self.engine.control(self.key, "pause", rev)
        self.engine.control(self.key, "resume", paused["revision"])
        stale = self.engine.control(self.key, "pause", rev)
        self.assertTrue(stale["staleControl"])
        self.assertEqual(stale["state"], "running")
        self.engine.control(self.key, "pause", stale["revision"])
        stale = self.engine.control(self.key, "resume", paused["revision"])
        self.assertEqual(stale["state"], "paused")
        self.assertTrue(stale["staleControl"])

    def test_unverified_source_only_blocks_affected_series_persists_partial_result(self):
        self.settings["plan"]["targets"] += "\n192.0.2.22"
        self.settings["count"] = 1
        self.start()

        def source_error(*args, before_send):
            return SimpleNamespace(
                observation=PingObservation("source_error"), transport_error=None
            )

        self.tick(source_error)
        self.assertEqual(self.status()["state"], "running")
        self.assertEqual(self.status()["series"][0]["blockReason"], "source_error")
        self.tick()
        status = self.status()
        self.assertEqual((status["state"], status["completed"], status["target"]), ("failed", 1, 2))
        self.assertEqual(status["success"], 1)
        recovered = LongPing(self.jobs).status(self.key)
        self.assertEqual(recovered["series"][0]["blockReason"], "source_error")
        self.assertEqual(recovered["completed"], 1)

    def test_multi_switch_per_destination_eta_bounds_and_deadline_warning(self):
        self.settings.update(count=10000, maxHours=1)
        self.settings["plan"]["targets"] = "\n".join(f"192.0.2.{i}" for i in range(1, 6))
        self.assertEqual(self.engine.preview(self.settings)["initialEta"], 50000)
        self.settings["plan"]["destinations"].append({"ip": "198.51.100.20"})
        self.assertEqual(self.engine.preview(self.settings)["total"], 100000)
        self.assertTrue(self.engine.preview(self.settings)["overDeadline"])
        self.settings["plan"]["options"] = {"concurrency": 1}
        self.assertGreaterEqual(self.engine.preview(self.settings)["initialEta"], 100000)
