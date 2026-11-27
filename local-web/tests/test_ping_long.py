"""Bounded, deterministic long-Ping tests. All command sends are synthetic."""

import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from localweb.jobs import Busy, Jobs
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

    def test_inventory_reuses_shared_address_per_switch_and_keeps_roles_after_restart(self):
        gateway, shared = "198.51.100.1", "198.51.100.2"
        self.settings.update(
            count=2,
            plan={
                "inventoryRows": [
                    {
                        "hostname": "North",
                        "swIp": "192.0.2.1",
                        "gwIp": gateway,
                        "bbIp": gateway,
                        "nmsIp": shared,
                    },
                    {"hostname": "South", "swIp": "192.0.2.2", "nmsIp": shared},
                ],
                "inventoryRoles": ["gwIp", "bbIp", "nmsIp"],
            },
        )
        preview = self.engine.preview(self.settings)
        self.assertEqual((preview["pairs"], preview["total"]), (3, 6))
        self.start()
        for clock in [0, 1, 2, 3, 6, 7, 8, 9]:
            self.clock.value = clock
            self.tick()
        pairs = [(t, d) for _, t, d in self.calls]
        self.assertCountEqual(
            pairs,
            [("192.0.2.1:22", gateway), ("192.0.2.1:22", shared), ("192.0.2.2:22", shared)] * 2,
        )
        archived = LongPing(self.jobs).status(self.key)
        self.assertEqual((archived["state"], archived["completed"]), ("done", 6))
        self.assertEqual(archived["settings"], self.status()["settings"])
        self.assertEqual(archived["settings"]["inventoryRows"][0]["hostname"], "North")
        self.assertEqual(archived["settings"]["inventoryRoles"], ["gwIp", "bbIp", "nmsIp"])

    def use_role_inventory(self, switches, *, count=1, capacity=5):
        self.settings.update(
            count=count,
            interval=1,
            plan={
                "inventoryRoleSessions": True,
                "inventoryRoles": ["gwIp", "bbIp", "nmsIp"],
                "inventoryRows": [
                    dict(swIp=ip, gwIp="198.51.100.1", bbIp="198.51.100.1", nmsIp="198.51.100.1")
                    for ip in switches
                ],
                "options": {"concurrency": capacity},
            },
        )

    def wait_for_release(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.jobs.lock:
                if not self.jobs.active:
                    return
            time.sleep(0.005)
        self.fail("role sessions did not release the lease")

    def test_periodic_device_limit_releases_one_row_at_a_time(self):
        switches = ["192.0.2.1", "192.0.2.2", "192.0.2.3", "192.0.2.4"]
        self.use_role_inventory(switches)
        plan = self.settings["plan"]
        plan["options"] = {"concurrent_devices": 2}
        for i, row in enumerate(plan["inventoryRows"]):
            row["gwIp"] = f"198.51.100.{i + 1}"
            row["bbIp"] = row["nmsIp"] = row["gwIp"] if i in (0, 3) else ""
        preview = self.engine.preview(self.settings)
        self.assertEqual((preview["effectiveDevices"], preview["effectiveConcurrency"]), (2, 6))
        started = {ip: threading.Semaphore(0) for ip in switches}
        release = {ip: threading.Event() for ip in switches}

        def hold(plan, target, dest, creds, cancel, *, before_send):
            result = self.fake_send(plan, target, dest, creds, cancel, before_send=before_send)
            started[target.ip].release()
            release[target.ip].wait(15)
            return result

        self.start()
        with patch.object(self.jobs.operations, "_ping_pair", side_effect=hold):
            try:
                self.engine.tick()
                self.assertTrue(all(started[switches[0]].acquire(timeout=5) for _ in range(3)))
                self.engine.tick()
                self.assertTrue(started[switches[1]].acquire(timeout=5))
                self.clock.value = 2
                self.engine.tick()
                self.assertEqual(
                    {s.series.target.ip for s in self.engine.run.slots.values()}, set(switches[:2])
                )
                release[switches[0]].set()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    self.engine.tick()
                    if started[switches[2]].acquire(timeout=0.01):
                        break
                else:
                    self.fail("the next row waited for the other device")
                self.assertEqual(
                    {s.series.target.ip for s in self.engine.run.slots.values()}, set(switches[1:3])
                )
            finally:
                for event in release.values():
                    event.set()
                self.wait_for_release()

    def test_full_inventory_preview_counts_whole_role_groups_per_write_window(self):
        switches = [f"198.18.{i // 250}.{i % 250 + 1}" for i in range(300)]
        self.use_role_inventory(switches, count=20)
        self.settings["maxHours"] = 1
        plan = self.settings["plan"]
        plan["options"] = {"concurrent_devices": 300}
        for i, row in enumerate(plan["inventoryRows"]):
            for subnet, role in enumerate(("gwIp", "bbIp", "nmsIp")):
                row[role] = f"198.19.{2 * subnet + i // 250}.{i % 250 + 1}"

        preview = self.engine.preview(self.settings)
        self.assertEqual((preview["effectiveDevices"], preview["effectiveConcurrency"]), (300, 900))
        self.assertEqual(preview["initialEta"], 6000)
        self.assertTrue(preview["overDeadline"])

    def test_periodic_admission_respects_active_unified_quick_measurement(self):
        self.jobs.ping_batch = SimpleNamespace(
            plan=SimpleNamespace(options=SimpleNamespace(concurrent_devices=10))
        )
        with self.assertRaises(Busy):
            self.start()
        self.jobs.ping_batch = None
        receipt = self.start()
        self.jobs.ping_batch = SimpleNamespace(
            plan=SimpleNamespace(options=SimpleNamespace(concurrent_devices=10))
        )
        self.assertEqual(self.start(), receipt)

    def test_three_independent_role_sessions_overlap_and_next_row_waits_for_full_capacity(self):
        switches = ["192.0.2.1", "192.0.2.2"]
        self.use_role_inventory(switches)
        preview = self.engine.preview(self.settings)
        self.assertEqual((preview["pairs"], preview["effectiveConcurrency"]), (6, 3))
        self.start()
        sent = threading.Semaphore(0)
        release = threading.Event()
        opened, closed = [], []

        @contextmanager
        def session(_plan, target, _credentials, _cancel):
            def observe(_plan, dest, *, cancel, before_send):
                before_send()
                self.calls.append((target.ip, dest.ip, dest.label, ssh))
                sent.release()
                release.wait(4)
                observation = {
                    "GW": PingObservation("normal", 1, 1, 0, rtt_avg_ms=2),
                    "BB": PingObservation("no_reply", 1, 0, 100),
                    "NMS": PingObservation("unknown"),
                }[dest.label]
                return SimpleNamespace(
                    observation=observation, transport_error=None, verified_source_ip=target.ip
                )

            ssh = SimpleNamespace(run_pair=observe)
            opened.append(ssh)
            try:
                yield ssh
            finally:
                closed.append(ssh)

        with patch.object(self.jobs.operations, "_ping_device_session", side_effect=session):
            try:
                self.jobs.tick_monitor()
                self.assertTrue(all(sent.acquire(timeout=2) for _ in range(3)))
                self.assertEqual(closed, [])
                self.assertEqual({ip for ip, _, _, _ in self.calls}, {switches[0]})
                self.assertEqual({role for _, _, role, _ in self.calls}, {"GW", "BB", "NMS"})
                self.assertEqual(len({id(ssh) for *_, ssh in self.calls}), 3)
                self.clock.value = 0.6
                self.jobs.tick_monitor()
                self.assertEqual(len(self.engine.run.slots), 3)
            finally:
                release.set()
            self.wait_for_release()
            self.jobs.tick_monitor()
            self.assertEqual(len(opened), 3)
            self.clock.value = 1
            self.jobs.tick_monitor()
            self.wait_for_release()
            self.assertEqual(len(opened), 6)
            self.assertCountEqual(opened, closed)

        for snap in (self.status(), LongPing(self.jobs).status(self.key)):
            self.assertEqual((snap["state"], snap["completed"]), ("done", 6))
            self.assertTrue(snap["settings"]["inventoryRoleSessions"])
            self.assertEqual(
                [
                    (r["inventoryRole"], r["success"], r["loss"], r["unknown"])
                    for r in snap["series"]
                ],
                [("gwIp", 1, 0, 0), ("bbIp", 0, 1, 0), ("nmsIp", 0, 0, 1)] * 2,
            )

    def test_role_cohorts_hold_shared_nms_until_another_switch_can_send(self):
        first, second = "192.0.2.1", "192.0.2.2"
        self.use_role_inventory([first, second])
        rows = self.settings["plan"]["inventoryRows"]
        rows[0].update(gwIp="198.51.100.10", bbIp="198.51.100.11")
        rows[1].update(gwIp="198.51.100.20", bbIp="")
        self.start()
        waiting, release = threading.Event(), threading.Event()
        sent = threading.Semaphore(0)

        def delayed_nms(plan, target, dest, creds, cancel, *, before_send):
            if target.ip == first and dest.label == "NMS":
                waiting.set()
                release.wait(4)
            result = self.fake_send(plan, target, dest, creds, cancel, before_send=before_send)
            sent.release()
            return result

        with patch.object(self.jobs.operations, "_ping_pair", side_effect=delayed_nms):
            try:
                self.jobs.tick_monitor()
                self.assertTrue(waiting.wait(2))
                self.assertTrue(all(sent.acquire(timeout=2) for _ in range(2)))
                self.clock.value = 1
                self.jobs.tick_monitor()
                # The old global permits have expired; only the first row's pending
                # NMS login prevents the second row from starting its cohort.
                with self.jobs.lock:
                    self.assertFalse(
                        any(
                            slot.series.target.ip == second
                            for slot in self.engine.run.slots.values()
                        )
                    )
                self.assertFalse(any(endpoint == second + ":22" for _, endpoint, _ in self.calls))
            finally:
                release.set()
                self.wait_for_release()
            for clock, expected in [(1.499, 3), (1.5, 5)]:
                self.clock.value = clock
                self.jobs.tick_monitor()
                self.wait_for_release()
                self.assertEqual(len(self.calls), expected)
        self.assertEqual(self.status()["state"], "done")
        self.assertCountEqual([t for t, _, dest in self.calls if dest == "198.51.100.1"], [1, 1.5])

    def test_role_estimate_counts_one_shared_endpoint_visit_per_row_and_round(self):
        self.use_role_inventory([f"192.0.2.{i}" for i in range(1, 11)], count=100)
        for row in self.settings["plan"]["inventoryRows"]:
            row.update(gwIp="", bbIp="")
        self.assertEqual(self.engine.preview(self.settings)["initialEta"], 500)
        # An additional role sharing NMS within the same row does not double the endpoint wait.
        for row in self.settings["plan"]["inventoryRows"]:
            row["bbIp"] = row["nmsIp"]
        self.assertEqual(self.engine.preview(self.settings)["initialEta"], 500)

    def test_role_backoff_does_not_stall_healthy_siblings(self):
        self.use_role_inventory(["192.0.2.1"], count=2)
        self.start()

        def initially_unreachable(plan, target, dest, creds, cancel, *, before_send):
            if dest.label == "BB" and self.clock.value == 0:
                raise CollectionFailure(ErrorCode.TCP_TIMEOUT, "fixture")
            return self.fake_send(plan, target, dest, creds, cancel, before_send=before_send)

        for clock, completed in [(0, 2), (1, 4), (4, 4), (5, 5), (6, 6)]:
            self.clock.value = clock
            self.tick(initially_unreachable)
            self.assertEqual(self.status()["completed"], completed)
        self.assertEqual(self.status()["state"], "done")

    def test_role_cancel_during_ssh_preparation_prevents_every_command(self):
        self.use_role_inventory(["192.0.2.1"])
        self.start()
        ready = threading.Semaphore(0)
        release = threading.Event()

        def prepare(*args, before_send):
            ready.release()
            release.wait(4)
            return self.fake_send(*args, before_send=before_send)

        with patch.object(self.jobs.operations, "_ping_pair", side_effect=prepare):
            try:
                self.jobs.tick_monitor()
                self.assertTrue(all(ready.acquire(timeout=2) for _ in range(3)))
                self.assertEqual(self.engine.control(self.key, "cancel")["state"], "stopping")
            finally:
                release.set()
            self.wait_for_release()
        self.assertEqual(self.status()["state"], "cancelled")
        self.assertEqual(self.status()["started"], 0)
        self.assertEqual(self.calls, [])

    def test_partially_started_role_cohort_releases_every_lease_on_thread_failure(self):
        self.use_role_inventory(["192.0.2.1"])
        self.start()
        entered, release = threading.Event(), threading.Event()
        real_start = threading.Thread.start
        launches = []

        def cannot_spawn_second(worker):
            launches.append(worker)
            if len(launches) == 2:
                raise OSError("fixture thread limit")
            real_start(worker)

        def first_worker(*args, before_send):
            entered.set()
            release.wait(3)
            return self.fake_send(*args, before_send=before_send)

        with (
            patch.object(threading.Thread, "start", cannot_spawn_second),
            patch.object(self.jobs.operations, "_ping_pair", side_effect=first_worker),
        ):
            try:
                self.jobs.tick_monitor()
                self.assertTrue(entered.wait(2))
                self.assertEqual(len(self.engine.run.slots), 1)
                self.assertEqual(self.status()["reason"], "WORKER_START_FAILED")
            finally:
                release.set()
            self.wait_for_release()
        self.assertEqual(self.status()["state"], "failed")
        self.assertEqual(self.status()["started"], 0)
        self.assertFalse(self.engine.run.slots)

    def test_live_and_archived_mean_use_only_successful_numeric_rtts(self):
        self.settings["count"] = 4
        self.start()
        self.assertIsNone(self.status()["series"][0]["avgRttMs"])
        observations = [
            PingObservation("normal", 1, 1, 0, rtt_evidence="<1/<1/<1"),
            PingObservation("normal", 1, 1, 0, rtt_avg_ms=0),
            PingObservation("no_reply", 1, 0, 100),
            PingObservation("normal", 1, 1, 0, rtt_avg_ms=9),
        ]
        for idx, observation in enumerate(observations):
            self.result = observation
            self.clock.value = idx * 5
            self.tick()
        for status in (self.status(), LongPing(self.jobs).status(self.key)):
            series = status["series"][0]
            self.assertEqual((series["success"], series["loss"], series["avgRttMs"]), (3, 1, 4.5))

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
        for status in (self.status(), LongPing(self.jobs).status(self.key)):
            series = status["series"][0]
            self.assertEqual((series["sshFailures"], series["gaps"], series["unknown"]), (1, 1, 0))

    def test_unconfirmed_measurements_are_separate_from_preflight_and_timing_gaps(self):
        self.settings["count"] = 3
        self.start()

        def prep_failed(*args, **kwargs):
            raise CollectionFailure(ErrorCode.TCP_TIMEOUT, "fixture")

        self.tick(prep_failed)
        self.result = PingObservation("unreachable")
        self.clock.value = 5
        self.tick()
        self.result = PingObservation("no_reply", 1, 0, 100)
        self.clock.value = 15
        self.tick()
        self.result = PingObservation("normal", 1, 1, 0, rtt_avg_ms=2)
        self.clock.value = 20
        self.tick()
        for status in (self.status(), LongPing(self.jobs).status(self.key)):
            series = status["series"][0]
            self.assertEqual(series["completed"], 3)
            self.assertEqual((series["success"], series["loss"], series["unknown"]), (1, 1, 1))
            self.assertEqual((series["gaps"], series["sshFailures"]), (2, 1))

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
                    self.assertEqual(
                        (snap["series"][0]["unknown"], snap["series"][0]["sshFailures"]), (1, 0)
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
