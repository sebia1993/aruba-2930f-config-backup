"""Finite low-priority Ping series. Device I/O always holds the existing Jobs lease."""

import hashlib
import json
import math
import re
import shutil
import sqlite3
import threading
import time
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from aruba2930f_backup.models import CollectionFailure, ErrorCode

from .ping_long_store import LongHistory
from .ping_tools import PingPlan

MAX_ATTEMPTS = 200_000
DISK_RESERVE = 32 * 1024 * 1024
TERMINAL = frozenset({"done", "cancelled", "failed", "interrupted"})
STOP_ERRORS = {ErrorCode.AUTH_FAILED, ErrorCode.ENABLE_FAILED}
DEVICE_ERRORS = {
    ErrorCode.MODEL_UNSUPPORTED,
    ErrorCode.HOST_KEY_CHANGED,
    ErrorCode.HOST_KEY_REJECTED,
}


def stamp(epoch):
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds")


def parse_settings(body):
    if not isinstance(body, dict) or set(body) - {
        "plan",
        "count",
        "interval",
        "maxHours",
        "autoTrace",
    }:
        raise ValueError("장시간 Ping 설정 형식을 확인하세요.")
    raw = body.get("plan")
    if not isinstance(raw, dict) or not isinstance(raw.get("options", {}), dict):
        raise ValueError("검토할 Ping 입력이 필요합니다.")
    plan = PingPlan.parse({**raw, "options": {**raw.get("options", {}), "repetitions": 1}})
    count, interval, hours = (
        body.get("count", 10_000),
        body.get("interval", 5),
        body.get("maxHours", 48),
    )
    auto = body.get("autoTrace", False)
    if type(count) is not int or not 1 <= count <= 10_000:
        raise ValueError("조합당 횟수는 1~10,000회입니다.")
    if type(interval) is not int or interval not in {1, 3, 5, 10}:
        raise ValueError("목표 주기를 1, 3, 5, 10초 중에서 선택하세요.")
    if type(hours) is not int or not 1 <= hours <= 48 or type(auto) is not bool:
        raise ValueError("실행 한도는 1~48시간입니다. 자동 경로 진단 선택을 확인하세요.")
    if plan.total_pairs * count > MAX_ATTEMPTS:
        raise ValueError("한 번에 최대 200,000회까지 실행할 수 있습니다. 조합이나 목표를 줄이세요.")
    return plan, {
        "count": count,
        "interval": interval,
        "maxHours": hours,
        "autoTrace": auto,
        "timeout": plan.options.timeout,
        "dataSize": plan.options.data_size,
    }


@dataclass
class Series:
    index: int
    target: object
    destination: object
    due: float
    started: int = 0
    completed: int = 0
    success: int = 0
    loss: int = 0
    gaps: int = 0
    interval: float | None = None
    last_send: float | None = None
    failures: int = 0
    streak: int = 0
    blocked: str = ""
    streak_start: int = 0
    streak_time: str | None = None


@dataclass
class Slot:
    series: Series
    scheduled_at: str
    acquired: float
    event: threading.Event
    row_id: int | None = None
    send_clock: float | None = None


class LongRun:
    def __init__(self, key, plan, settings, clock, wall, credentials):
        self.key, self.plan, self.settings, self.credentials = key, plan, settings, credentials
        self.lease = "long:" + key
        self.revision = 0
        self.origin, self.wall = clock, wall
        self.state, self.reason = "running", ""
        self.slots = {}
        self.stop_event = threading.Event()
        self.stop_state = "cancelled"
        self.capacity = min(5, plan.options.concurrency, len({t.ip for t in plan.targets}))
        self.pause_at = None
        self.paused = 0
        self.busy_since = None
        self.active_seconds = 0.0
        self.finished = None
        self.finished_clock = None
        self.wait = ""
        self.clock_uncertain = False
        self.last_send = float("-inf")
        self.dest_sends = {}
        self.duration = max(0.5, plan.options.timeout / 2)
        self.samples = 0
        self.series = [
            Series(i, target, dest, clock)
            for i, (target, dest) in enumerate(
                (target, dest) for target in plan.targets for dest in plan.destinations
            )
        ]
        self.initial_eta = self.estimate(clock)[0]

    def estimate(self, clock, duration=None):
        # A shared coordinator lease, one CLI per switch, five starts/sec globally, two per destination.
        # Initial expected occupancy assumes half the response timeout (one second by default).
        # Live estimates use measured worker duration including SSH and device preparation.
        duration = self.duration if duration is None else duration
        count, interval = self.settings["count"], self.settings["interval"]
        remaining = {s.index: 0 if s.blocked else count - s.completed for s in self.series}
        total = sum(remaining.values())
        destinations = Counter()
        devices = Counter()
        for s in self.series:
            destinations[s.destination.ip] += remaining[s.index]
            devices[s.target.ip] += remaining[s.index]
        contention = max(
            total / 5,
            total * duration / self.capacity,
            max(destinations.values()) / 2,
            max(devices.values()) * duration,
        )
        estimates = {}
        largest = max(remaining.values(), default=0)
        for s in self.series:
            n = remaining[s.index]
            if not n:
                estimates[s.index] = 0
                continue
            base = max(interval, s.interval or 0)
            estimates[s.index] = math.ceil(
                max(
                    n * base,
                    contention * n / max(1, largest),
                    max(0, s.due - clock) + (n - 1) * base + duration,
                )
            )
        return max(estimates.values(), default=0), estimates

    def _series_state(self, s, clock):
        if s.blocked:
            return "BLOCKED"
        if s.completed == self.settings["count"]:
            return "DONE"
        slot = self.slots.get(s.target.ip)
        if slot and slot.series is s:
            return "IN_FLIGHT" if slot.row_id else "SSH_PREPARATION"
        if self.state != "running":
            return self.state.upper()
        if self.wait:
            return self.wait
        if s.failures:
            return "RECONNECT"
        if slot:
            return "DEVICE_BUSY"
        if clock < s.due:
            return "INTERVAL"
        return "QUEUED"

    def snapshot(self, clock, wall):
        if self.finished_clock is not None:
            clock = self.finished_clock
        eta, estimates = self.estimate(clock)
        unknown = self.state not in {"running", "done"} or bool(self.wait)
        incomplete = any(s.blocked or s.failures for s in self.series)
        target = self.settings["count"]
        rows = []
        devices = {}
        active = self.active_seconds + (
            max(0, clock - self.busy_since) if self.busy_since is not None else 0
        )
        paused = self.paused + (max(0, clock - self.pause_at) if self.pause_at is not None else 0)
        for s in self.series:
            row = dict(
                index=s.index,
                switch=s.target.endpoint,
                destination=s.destination.ip,
                target=target,
                started=s.started,
                completed=s.completed,
                success=s.success,
                loss=s.loss,
                gaps=s.gaps,
                actualInterval=s.interval,
                wait=self._series_state(s, clock),
                blockReason=s.blocked,
                etaSeconds=None
                if s.blocked or s.failures or (unknown and s.completed < target)
                else estimates[s.index],
            )
            rows.append(row)
            device = devices.setdefault(
                s.target.endpoint,
                {"switch": s.target.endpoint, "target": 0, "completed": 0, "etaSeconds": 0},
            )
            device["target"] += target
            device["completed"] += s.completed
            if row["etaSeconds"] is None:
                device["etaSeconds"] = None
            elif device["etaSeconds"] is not None:
                device["etaSeconds"] = max(device["etaSeconds"], row["etaSeconds"])
        return dict(
            id=self.key,
            state=self.state,
            revision=self.revision,
            reason=self.reason,
            wait=self.wait,
            startedAt=stamp(self.wall),
            finishedAt=self.finished,
            estimatedAt=stamp(wall),
            etaSeconds=None if unknown or incomplete else eta,
            estimatedFinish=None if unknown or incomplete else (self.finished or stamp(wall + eta)),
            initialEta=self.initial_eta,
            settings=self.settings,
            effectiveConcurrency=self.capacity,
            elapsed=max(0, clock - self.origin),
            pausedSeconds=paused,
            activeSeconds=active,
            waitingSeconds=max(0, clock - self.origin - paused - active),
            quality="observed" if self.samples else "initial",
            clockUncertain=self.clock_uncertain,
            series=rows,
            devices=list(devices.values()),
            **{
                field: sum(row[field] for row in rows)
                for field in ("target", "started", "completed", "success", "loss", "gaps")
            },
        )


class LongPing:
    def __init__(self, jobs, *, monotonic=time.monotonic, wall=time.time):
        self.jobs, self.clock, self.wall = jobs, monotonic, wall
        self.history = LongHistory(jobs.directory)
        self.run = None

    def _check_disk(self, expected=0):
        size = self.history.path.stat().st_size
        wal = self.history.path.with_suffix(self.history.path.suffix + "-wal")
        with suppress(FileNotFoundError):
            size += wal.stat().st_size
        limit = self.history.policy()["maxMb"] * 1024 * 1024
        if size + expected >= limit:
            with self.history.connect() as db:
                size = (
                    db.execute("PRAGMA page_count").fetchone()[0]
                    - db.execute("PRAGMA freelist_count").fetchone()[0]
                ) * db.execute("PRAGMA page_size").fetchone()[0]
        if (
            size + expected >= limit
            or shutil.disk_usage(self.jobs.directory).free < DISK_RESERVE + expected
        ):
            raise ValueError(
                "Ping 기록 공간이 부족합니다. 데이터 폴더 여유 공간과 이력을 확인하세요."
            )
        return size

    def preview(self, body):
        plan, settings = parse_settings(body)
        clock, wall = self.clock(), self.wall()
        run = LongRun("", plan, settings, clock, wall, None)
        total = settings["count"] * plan.total_pairs
        timeout_estimate = run.estimate(clock, plan.options.timeout + 1)[0]
        return dict(
            total=total,
            pairs=plan.total_pairs,
            count=settings["count"],
            interval=settings["interval"],
            initialEta=run.initial_eta,
            timeoutEta=timeout_estimate,
            estimatedFinish=stamp(wall + run.initial_eta),
            overDeadline=run.initial_eta > settings["maxHours"] * 3600,
            effectiveConcurrency=run.capacity,
            maxHours=settings["maxHours"],
            historyBytes=self._check_disk(total * 512),
        )

    def submit(self, body):
        if not isinstance(body, dict) or set(body) != {"requestId", "settings"}:
            raise ValueError("장시간 작업 ID와 검토한 설정이 필요합니다.")
        key = body["requestId"]
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key):
            raise ValueError("작업 ID를 확인하세요.")
        plan, settings = parse_settings(body["settings"])
        fingerprint = hashlib.sha256(repr((plan, settings)).encode()).hexdigest()
        with self.jobs.lock, self.history.connect() as db:
            if db.execute("SELECT 1 FROM ping_long_retired WHERE id=?", (key,)).fetchone():
                raise ValueError("이전에 종료된 작업 ID입니다.")
            existing = db.execute(
                "SELECT fingerprint,state FROM ping_long_runs WHERE id=?", (key,)
            ).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("같은 ID에 다른 설정을 사용할 수 없습니다.")
                return {"id": key, "state": existing["state"]}
            if self.jobs.closing or (
                self.run and (self.run.slots or self.run.state not in TERMINAL)
            ):
                raise ValueError("현재 작업 종료 후 장시간 측정을 시작하세요.")
            credentials = self.jobs.operations.credentials
            if credentials is None:
                raise ValueError("SSH 접속 정보를 입력하세요.")
            self.history.prune(db, at=datetime.fromtimestamp(self.wall(), UTC))
            self._check_disk(plan.total_pairs * settings["count"] * 512)
            run = LongRun(key, plan, settings, self.clock(), self.wall(), credentials)
            db.execute(
                """INSERT INTO ping_long_runs
                       (id,started_at,state,fingerprint,settings,initial_eta) VALUES(?,?,?,?,?,?)""",
                (
                    key,
                    stamp(run.wall),
                    "running",
                    fingerprint,
                    json.dumps(settings),
                    run.initial_eta,
                ),
            )
            db.executemany(
                "INSERT INTO ping_long_series VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    (
                        key,
                        s.index,
                        s.target.ip,
                        s.target.port,
                        s.destination.ip,
                        s.destination.label,
                        plan.options.source_mode,
                        plan.requested_source(s.target),
                        settings["count"],
                    )
                    for s in run.series
                ],
            )
            db.commit()
            self.run = run
            return {"id": key, "state": run.state}

    def current(self):
        with self.jobs.lock:
            return {"id": self.run.key if self.run else None}

    def owns(self, key):
        return bool(key and self.run and self.run.slots and key == self.run.lease)

    def status(self, key):
        with self.jobs.lock:
            if self.run and self.run.key == key:
                return self.run.snapshot(self.clock(), self.wall())
        with self.history.connect() as db:
            record = db.execute("SELECT * FROM ping_long_runs WHERE id=?", (key,)).fetchone()
            if not record:
                raise KeyError(key)
            rows = [
                dict(r)
                for r in db.execute(
                    """
                SELECT s.series_id AS 'index',s.switch_ip || ':' || s.ssh_port AS switch,
                    s.destination_ip AS destination, s.target_count AS target, b.reason AS blockReason,
                    COUNT(m.attempt) AS started,COUNT(m.attempt) AS completed,
                    COUNT(CASE WHEN m.status='normal' AND m.received=1 THEN 1 END) AS success,
                    COUNT(CASE WHEN m.status IN ('no_reply','unreachable') AND m.sent=1 AND m.received=0 THEN 1 END) AS loss,
                    COALESCE(SUM(m.gap_count),0) AS gaps
                FROM ping_long_series s LEFT JOIN ping_long_samples m
                ON m.run_id=s.run_id AND m.series_id=s.series_id
                LEFT JOIN ping_long_blocks b ON b.run_id=s.run_id AND b.series_id=s.series_id
                WHERE s.run_id=? GROUP BY s.series_id ORDER BY s.series_id""",
                    (key,),
                )
            ]
            return dict(
                id=key,
                state=record["state"],
                reason=record["reason"],
                series=rows,
                devices=[],
                elapsed=record["elapsed"],
                pausedSeconds=record["paused"],
                startedAt=record["started_at"],
                finishedAt=record["finished_at"],
                wait="",
                initialEta=record["initial_eta"],
                estimatedAt=stamp(self.wall()),
                etaSeconds=0 if record["state"] == "done" else None,
                estimatedFinish=None,
                settings=json.loads(record["settings"]),
                quality="archived",
                **{
                    field: sum(row[field] for row in rows)
                    for field in ("target", "started", "completed", "success", "loss", "gaps")
                },
            )

    def _transition(self, run, state, reason=""):
        clock = self.clock()
        if run.pause_at is not None and state != "paused":
            run.paused += max(0, clock - run.pause_at)
            run.pause_at = None
        if state == "paused" and run.pause_at is None:
            run.pause_at = clock
        run.revision += 1
        run.state, run.reason, run.wait = state, reason, ""
        if state in TERMINAL:
            run.finished, run.finished_clock = stamp(self.wall()), clock
            run.credentials = None
        try:
            with self.history.connect() as db:
                db.execute(
                    """UPDATE ping_long_runs SET state=?,reason=?,finished_at=?,elapsed=?,paused=?
                           WHERE id=?""",
                    (state, reason, run.finished, max(0, clock - run.origin), run.paused, run.key),
                )
        except sqlite3.Error:
            self._storage_failed(run)

    def _storage_failed(self, run):
        run.state, run.reason = "failed", "PING_HISTORY_WRITE_FAILED"
        run.finished, run.finished_clock, run.credentials = stamp(self.wall()), self.clock(), None
        run.stop_event.set()
        with suppress(OSError, sqlite3.Error), self.history.connect() as db:
            db.execute(
                "UPDATE ping_long_runs SET state='failed',reason=?,finished_at=?,elapsed=? WHERE id=?",
                (run.reason, run.finished, max(0, run.finished_clock - run.origin), run.key),
            )

    def stop(self, reason):
        with self.jobs.lock:
            run = self.run
            if not run or run.state in TERMINAL or run.state == "stopping":
                return
            if run.state == "paused":
                try:
                    self._close_pause_gap(run)
                except sqlite3.Error:
                    self._storage_failed(run)
                    return
            self._transition(run, "stopping" if run.slots else "cancelled", reason)
            run.stop_event.set()

    def _fail(self, run, reason):
        run.stop_state = "failed"
        self._transition(run, "stopping" if run.slots else "failed", reason)
        run.stop_event.set()

    def control(self, key, action, revision=None):
        with self.jobs.lock:
            run = self.run
            if not run or key != run.key:
                return self.status(key)
            if revision is not None and (type(revision) is not int or revision != run.revision):
                return dict(self.status(key), staleControl=True)
            if action == "cancel":
                self.stop("OPERATOR_CANCELLED")
            elif action == "pause" and run.state == "running":
                self._transition(run, "pausing" if run.slots else "paused")
            elif action == "resume" and run.state == "paused":
                if self.clock() - run.origin >= run.settings["maxHours"] * 3600:
                    self.stop("TIME_LIMIT")
                elif (
                    run.credentials is not self.jobs.operations.credentials
                    or run.credentials is None
                ):
                    self.stop("SESSION_CHANGED")
                else:
                    try:
                        self._close_pause_gap(run)
                        self._transition(run, "running")
                    except sqlite3.Error:
                        self._storage_failed(run)
            elif action not in {"resume", "pause", "cancel"}:
                raise ValueError("지원하지 않는 장시간 작업입니다.")
            return self.status(key)

    def _gap(self, db, run, s, scheduled, n, reason):
        if n <= 0:
            return
        self.history.insert_sample(
            db,
            run.key,
            s,
            scheduled,
            reason=reason,
            gaps=n,
            finished_at=stamp(self.wall()),
            uncertain=run.clock_uncertain,
        )
        s.gaps += n
        s.streak = 0

    def _close_pause_gap(self, run):
        clock, wall = self.clock(), self.wall()
        with self.history.connect() as db:
            for s in run.series:
                if s.blocked or s.completed == run.settings["count"]:
                    continue
                if clock >= s.due:
                    n = 1 + int((clock - s.due) // run.settings["interval"])
                    self._gap(db, run, s, stamp(wall - (clock - s.due)), n, "USER_PAUSED")
                s.due, s.last_send, s.interval = clock, None, None

    def tick(self):
        with self.jobs.lock:
            run = self.run
            if not run or run.state in TERMINAL:
                return
            clock, wall = self.clock(), self.wall()
            if abs((wall - run.wall) - (clock - run.origin)) > 2:
                run.clock_uncertain = True
            if run.state == "stopping":
                return
            if self.jobs.closing or run.credentials is not self.jobs.operations.credentials:
                self.stop("SESSION_CHANGED")
                return
            if clock - run.origin >= run.settings["maxHours"] * 3600:
                self.stop("TIME_LIMIT")
                return
            if run.state != "running" or len(run.slots) >= run.capacity:
                return
            if (
                self.jobs.active not in (None, run.lease)
                or self.jobs.pending
                or self.jobs.controls.awaiting_confirmation()
                or (
                    run.slots
                    and (
                        self.jobs.ping_batch
                        or self.jobs.trace_queue.pending
                        or (
                            self.jobs.monitor
                            and not self.jobs.monitor.suspended
                            and time.monotonic() >= self.jobs.monitor.due
                        )
                    )
                )
            ):
                run.wait = "OTHER_OPERATION"
                return
            run.wait = ""
            candidates = [
                s
                for s in run.series
                if not s.blocked
                and s.completed < run.settings["count"]
                and s.target.ip not in run.slots
                and max(
                    s.due,
                    run.last_send + 0.2,
                    run.dest_sends.get(s.destination.ip, float("-inf")) + 0.5,
                )
                <= clock
            ]
            if not candidates:
                return
            s = min(candidates, key=lambda item: (item.due, item.completed, item.index))
            interval = run.settings["interval"]
            missed = int((clock - s.due) // interval)
            old_due = s.due
            s.due += missed * interval
            slot = Slot(s, stamp(wall - (clock - s.due)), clock, run.stop_event)
            try:
                self._check_disk()
                if missed:
                    with self.history.connect() as db:
                        self._gap(
                            db, run, s, stamp(wall - (clock - old_due)), missed, "SCHEDULE_DELAY"
                        )
                s.due += interval
                if not run.slots:
                    run.busy_since = clock
                    self.jobs.busy_workspace = self.jobs.workspace()
                    self.jobs.active, self.jobs.cancel_event = run.lease, slot.event
                run.slots[s.target.ip] = slot
                threading.Thread(target=self._work, args=(run, slot), daemon=True).start()
            except OSError, ValueError, sqlite3.Error:
                self._storage_failed(run)
            except RuntimeError:
                self._release(run, slot)
                self._fail(run, "WORKER_START_FAILED")

    def _before_send(self, run, slot):
        # Prep can finish out of order on different switches. Reserve the real
        # send boundary under the same lock that owns control and rate state.
        while True:
            with self.jobs.lock:
                delay = self._reserve_send(run, slot)
            if delay is None:
                return
            slot.event.wait(min(delay, 0.2))

    def _reserve_send(self, run, slot):
        # Called after host key, device and Source validation, immediately before channel write.
        with self.jobs.lock:
            clock, wall = self.clock(), self.wall()
            if (
                run.slots.get(slot.series.target.ip) is not slot
                or run.state != "running"
                or slot.event.is_set()
                or run.credentials is not self.jobs.operations.credentials
                or clock - run.origin >= run.settings["maxHours"] * 3600
            ):
                raise CollectionFailure(ErrorCode.CANCELLED, "장시간 Ping이 중단되었습니다.")
            if slot.row_id is not None:
                raise RuntimeError("Ping command intent already exists")
            s = slot.series
            delay = (
                max(run.last_send + 0.2, run.dest_sends.get(s.destination.ip, float("-inf")) + 0.5)
                - clock
            )
            if delay > 0:
                return delay
            try:
                self._check_disk()
                with self.history.connect() as db:
                    row_id = self.history.insert_sample(
                        db,
                        run.key,
                        s,
                        slot.scheduled_at,
                        started_at=stamp(wall),
                        attempt=s.started + 1,
                        status="unknown",
                        reason="IN_FLIGHT",
                        uncertain=run.clock_uncertain,
                    )
            except (OSError, ValueError, sqlite3.Error) as exc:
                self._storage_failed(run)
                raise CollectionFailure(
                    ErrorCode.CANCELLED, "측정 이력을 저장할 수 없습니다."
                ) from exc
            clock = self.clock()
            slot.row_id, slot.send_clock = row_id, clock
            s.started += 1
            if s.last_send is not None:
                spacing = max(0, clock - s.last_send)
                s.interval = spacing if s.interval is None else 0.7 * s.interval + 0.3 * spacing
            s.last_send = run.last_send = run.dest_sends[s.destination.ip] = clock
            s.due = max(s.due, clock + run.settings["interval"])

    def _release(self, run, slot):
        run.slots.pop(slot.series.target.ip, None)
        if run.slots:
            return
        if run.busy_since is not None:
            run.active_seconds += max(0, self.clock() - run.busy_since)
            run.busy_since = None
        self.jobs.active = self.jobs.cancel_event = self.jobs.busy_workspace = None
        self.jobs._next_manual()

    def _work(self, run, slot):
        result, error = None, ""
        s = slot.series
        try:
            result = self.jobs.operations._ping_pair(
                run.plan,
                s.target,
                s.destination,
                run.credentials,
                slot.event,
                before_send=lambda: self._before_send(run, slot),
            )
        except CollectionFailure as exc:
            error = exc.code
        except Exception:
            error = ErrorCode.UNEXPECTED_ERROR
        with self.jobs.lock:
            clock, wall = self.clock(), self.wall()
            if abs((wall - run.wall) - (clock - run.origin)) > 2:
                run.clock_uncertain = True
            try:
                if run.reason != "PING_HISTORY_WRITE_FAILED":
                    self._record(run, slot, result, error, clock, wall)
            except OSError, sqlite3.Error:
                self._storage_failed(run)
            except Exception:
                self._fail(run, "RESULT_PROJECTION_FAILED")
            finally:
                self._release(run, slot)
            if run.slots or run.state in TERMINAL:
                return
            if run.state == "stopping":
                self._transition(run, run.stop_state, run.reason)
            elif all(s.blocked or s.completed >= run.settings["count"] for s in run.series):
                if any(s.blocked for s in run.series):
                    self._transition(run, "failed", "SERIES_INCOMPLETE")
                else:
                    self._transition(run, "done")
            elif run.state == "pausing":
                self._transition(run, "paused")

    def _record(self, run, slot, result, error, clock, wall):
        s = slot.series
        observation = result.observation if result else None
        if result and result.transport_error:
            error = result.transport_error
        status = observation.status if observation else "unknown"
        sent = observation.sent if observation else None
        received = observation.received if observation else None
        with self.history.connect() as db:
            if slot.row_id is None:
                status = (
                    observation.status
                    if observation
                    else ("not_run" if error == ErrorCode.CANCELLED else "ssh_failure")
                )
                self.history.insert_sample(
                    db,
                    run.key,
                    s,
                    slot.scheduled_at,
                    finished_at=stamp(wall),
                    status=status,
                    reason=str(error or status),
                    gaps=1,
                    uncertain=run.clock_uncertain,
                )
                s.gaps += 1
                s.streak = 0
                if run.state == "running":
                    s.failures += 1
                    s.due = max(s.due, clock + min(60, 5 * s.failures))
            else:
                observed_at = getattr(result, "prompt_received_at", None)
                observed_clock = getattr(result, "prompt_received_clock", None)
                if observed_at is None:
                    observed_at = wall
                if observed_clock is None:
                    observed_clock = clock
                evidence = observation.rtt_evidence if observation else None
                rtt = observation.rtt_avg_ms if observation and status == "normal" else None
                db.execute(
                    """UPDATE ping_long_samples SET received_at=?,finished_at=?,status=?,
                           sent=?,received=?,rtt_ms=?,rtt_evidence=?,reason=?,verified_source_ip=?,
                           duration_ms=?,clock_uncertain=? WHERE id=?""",
                    (
                        stamp(observed_at) if observation and not error else None,
                        stamp(observed_at),
                        status,
                        sent,
                        received,
                        rtt,
                        evidence,
                        str(error or ""),
                        result.verified_source_ip if result else None,
                        round(max(0, observed_clock - slot.send_clock) * 1000),
                        int(run.clock_uncertain),
                        slot.row_id,
                    ),
                )
                s.completed += 1
                s.failures = 0
                loss = status in {"no_reply", "unreachable"} and sent == 1 and received == 0
                s.success += int(status == "normal" and received == 1)
                s.loss += int(loss)
                if loss and not s.streak:
                    s.streak_start = s.started
                    s.streak_time = stamp(observed_at)
                s.streak = s.streak + 1 if loss else 0
            if run.state == "running" and (
                error in DEVICE_ERRORS or s.failures >= 3 or status in {"cli_error", "source_error"}
            ):
                affected = (
                    [peer for peer in run.series if peer.target == s.target]
                    if error in DEVICE_ERRORS
                    else [s]
                )
                for peer in affected:
                    if peer.blocked or (peer is not s and peer.completed == run.settings["count"]):
                        continue
                    peer.blocked = str(error or status)
                    db.execute(
                        "INSERT OR IGNORE INTO ping_long_blocks VALUES(?,?,?)",
                        (run.key, peer.index, peer.blocked),
                    )
            db.execute(
                "UPDATE ping_long_runs SET elapsed=? WHERE id=?",
                (max(0, clock - run.origin), run.key),
            )
        if slot.row_id is not None:
            observed = max(0.01, clock - slot.acquired)
            run.duration = (0.7 * run.duration + 0.3 * observed) if run.samples else observed
            run.samples += 1
        if run.state == "running" and error in STOP_ERRORS:
            self._fail(run, str(error or status))
        if s.streak == 3 and run.settings["autoTrace"] and run.state == "running":
            self.jobs.offer_ping_trace(
                run.key,
                run.plan,
                s.target,
                s.destination,
                f"{run.key}:{s.index}:{s.streak_start}",
                s.streak_time,
            )
