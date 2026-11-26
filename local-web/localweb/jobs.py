"""Run a single device operation at a time; retain only summaries and download handles on disk."""

import json
import re
import secrets
import sqlite3
import time
from collections import OrderedDict
from contextlib import closing, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, RLock, Thread

from aruba2930f_backup.models import CollectionFailure, ErrorCode

from .controls import Controls
from .manual_queue import ManualRequest, manual_outcome
from .monitoring import PortMonitoring
from .operations import Operations, TargetInputError, _groups
from .ping_jobs import PingBatch
from .ping_tools import PingPlan
from .port_tools import group_targets
from .power_history import PowerHistory, sampled_rows

ACTIONS = frozenset(
    [
        "session.open",
        "session.close",
        "hostkey.inspect",
        "hostkey.approve",
        "hostkey.list",
        "hostkey.forget",
        "ports.query",
        "ports.refresh",
        "cable.test",
        "targets.preview",
        "targets.apply",
        "profiles.import",
        "profiles.export",
        "backup.run",
        "backup.history",
        "backup.pick-directory",
        "history.open-folder",
        "control.preview",
        "control.apply",
        "control.history",
        "control.inspect",
        "control.quick",
        "control.quick-execute",
        "control.import-history",
    ]
)
PERSIST = {
    "ports.query",
    "ports.refresh",
    "cable.test",
    "profiles.export",
    "backup.run",
    "control.quick",
    "control.quick-execute",
    "control.apply",
}
EXTENSIONS = {".jsonl", ".json", ".txt", ".xlsx"}
MAX_RESULTS = 8
MANUAL_ACTIONS = frozenset({"control.quick-execute", "cable.test"})
MAX_PENDING_MANUAL = 24
MANUAL_RESERVATION_TIMEOUT = 10 * 60
DEFAULT_MONITOR_MINUTES = 1
DUPLICATE_POE_REQUEST = "이미 접수한 빠른 PoE 요청입니다. 해당 포트의 현재 상태를 다시 조회하세요."
POWER_HISTORY_WARNING = (
    "일부 측정값을 저장하지 못했습니다. 로컬 데이터 폴더와 남은 용량을 확인하세요."
)


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def _observation_for_comparison(row):
    # Only switch readings used by the matrix. No control permissions or older history.
    fields = (
        "ip",
        "ssh_port",
        "interface",
        "status",
        "complete",
        "error_code",
        "observed_at",
        "finished_at",
        "watts",
        "poe_enabled",
        "poe_status",
        "assessment_status",
        "assessment_reason",
        "requested_watts",
        "allocated_watts",
        "lldp_status",
        "lldp_error_code",
        "lldp_observed_at",
        "lldp_neighbors",
        "link_status",
        "link_observed_at",
    )
    snapshot = {key: row[key] for key in fields if key in row}
    evidence = row.get("power_evidence")
    if evidence:
        snapshot["power_evidence"] = {"issues": evidence.get("issues", [])}
    return snapshot


def _without_excerpts(rows):
    return [{k: v for k, v in row.items() if k != "responseExcerpt"} for row in rows]


class Busy(Exception):
    def __init__(self, message, conflicts=()):
        super().__init__(message)
        self.conflicts = list(conflicts)


class Jobs:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.operations = Operations(self.directory)
        self.power_history = PowerHistory(self.directory / "power-history.sqlite")
        self.power_write_errors = {}
        self.controls = Controls(self.operations)
        self.db = self.directory / "web-history.sqlite"
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, action TEXT, state TEXT, started TEXT, finished TEXT, summary TEXT, files TEXT)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS downloads (id TEXT PRIMARY KEY, path TEXT)")
            if "folder" not in {col[1] for col in db.execute("PRAGMA table_info(downloads)")}:
                db.execute("ALTER TABLE downloads ADD COLUMN folder TEXT")
            db.execute(
                "CREATE TABLE IF NOT EXISTS manual_aliases (id TEXT PRIMARY KEY, job TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS closed_manual_receipts (id TEXT PRIMARY KEY, closed TEXT NOT NULL)"
            )
            db.execute(
                "UPDATE jobs SET state='cancelled',finished=?,summary=? WHERE state='queued'",
                (now(), "서버가 다시 시작되어 대기 중이던 예약을 취소했습니다."),
            )
            db.execute(
                "UPDATE jobs SET state='uncertain',finished=?,summary=? WHERE state='running' "
                "AND action IN ('control.quick-execute','cable.test')",
                (now(), "서버가 작업 중 종료되었습니다. 수동으로 포트 상태를 확인하세요."),
            )
            db.execute(
                "UPDATE jobs SET state='interrupted',finished=?,summary=? WHERE state='running'",
                (now(), "이전 실행 중 서버가 종료되었습니다."),
            )
        self.lock = RLock()
        self.receipt_log_lock = RLock()
        self.recent = OrderedDict()
        self.manual = {}
        self.pending = OrderedDict()
        self.pending_since = {}
        self.busy_workspace = None
        self.port_view = None
        self.active = None
        self.closing = False
        self.cancel_event = None
        self.monitor: PortMonitoring | None = None
        self.ping_batch: PingBatch | None = None

    def latest_cable_job(self):
        with self.lock:
            return next(
                (
                    job["id"]
                    for job in reversed(self.recent.values())
                    if job["action"] == "cable.test"
                ),
                None,
            )

    def monitor_status(self):
        with self.lock:
            plan = self.monitor
            return {
                "active": plan is not None,
                "suspended": bool(plan and plan.suspended),
                "monitorId": plan.id if plan else None,
                "intervalMinutes": plan.minutes if plan else DEFAULT_MONITOR_MINUTES,
                "portCount": plan.port_count if plan else 0,
                "skippedRounds": plan.skipped_rounds if plan else 0,
                "nextAt": plan.next_at if plan else None,
                "jobId": plan.job if plan else None,
                "lastJobId": plan.last_job if plan else None,
                "lastFinished": plan.last_finished if plan else None,
                "lastSuccess": plan.last_success if plan else None,
                "error": plan.error if plan else "",
                "session": self.operations.session_summary(),
            }

    def start_monitor(self, body):
        minutes = body.get("intervalMinutes", DEFAULT_MONITOR_MINUTES)
        if type(minutes) is not int or not 1 <= minutes <= 60:
            raise ValueError("조회 간격은 1~60분 사이의 정수로 입력하세요.")
        groups = _groups(body)
        with self.lock:
            if self.monitor:
                if self.monitor.groups == groups and self.monitor.minutes == minutes:
                    return {**self.monitor_status(), "groups": group_targets(groups)}
                raise Busy("실행 중인 모니터링을 중지한 후 대상이나 간격을 바꾸세요.")
            if self.pending:
                raise Busy("대기 중인 수동 작업을 마친 뒤 모니터링을 시작하세요.")
            if not self.operations.credentials:
                raise ValueError("먼저 SSH 접속 정보를 입력하세요.")
            # Publish the plan before the new worker can acquire the lock and finish.
            key = self.submit("ports.query", {"targets": body["targets"]}, monitored=True)
            self.monitor = PortMonitoring(key, groups, body["targets"], minutes, job=key)
            return {**self.monitor_status(), "groups": group_targets(groups)}

    def stop_monitor(self, monitor_id=None):
        with self.lock:
            if self.monitor and (monitor_id is None or self.monitor.id == monitor_id):
                if self.monitor.job and self.monitor.job == self.active:
                    self.cancel_event.set()
                self.monitor = None
            return self.monitor_status()

    def tick_monitor(self):
        with self.lock:
            self._expire_pending()
            if self.pending and not self.active:
                self._next_manual()
            plan = self.monitor
            if (
                self.closing
                or not plan
                or plan.job
                or plan.suspended
                or time.monotonic() < plan.due
            ):
                self._tick_ping()
                return
            occupied = bool(self.active or self.pending or self.controls.awaiting_confirmation())
            plan.advance_to_next_slot(skipped=occupied)
            if occupied:
                # A queued manual action always wins the next device slot.
                return
            plan.job = self.submit("ports.refresh", {"targets": plan.targets}, monitored=True)
            plan.next_at = None

    def claim_shutdown(self):
        with self.lock:
            if self.active:
                raise Busy("작업을 마치거나 취소한 후 서버를 종료하세요.")
            self.invalidate_pending("서버 종료로 예약을 취소했습니다.")
            if self.ping_batch:
                self.ping_batch.stop("SERVER_SHUTDOWN")
                self._finish_ping(self.ping_batch)
            self.monitor = None
            self.closing = True

    def manual_queue(self):
        with self.lock:
            self._expire_pending()
            items = []
            for key, entry in self.recent.items():
                if key not in self.manual:
                    continue
                request, queued = self.manual[key]
                items.append(
                    {
                        "id": key,
                        "action": request.action,
                        "operation": request.operation,
                        "targets": request.public_targets(),
                        "wasQueued": queued,
                        "state": entry.get("_manual_state", entry["state"]),
                        "message": entry.get("_manual_message", ""),
                        "started": entry["started"],
                        "finished": entry["finished"],
                    }
                )
            return {"items": items}

    def invalidate_pending(self, reason):
        with self.lock:
            self._cancel_reservations(tuple(self.pending), reason)

    def _expire_pending(self):
        expired = tuple(
            key
            for key in self.pending
            if time.monotonic() - self.pending_since[key] >= MANUAL_RESERVATION_TIMEOUT
        )
        self._cancel_reservations(
            expired, "예약 대기 10분이 지나 자동 취소했습니다. 필요한 경우 새로 신청하세요."
        )

    def _cancel_reservations(self, keys, reason):
        if not keys:
            return
        finished = now()
        try:
            with closing(sqlite3.connect(self.db)) as db, db:
                db.executemany(
                    "UPDATE jobs SET state='cancelled',finished=?,summary=? WHERE id=?",
                    ((finished, reason, key) for key in keys),
                )
        except sqlite3.Error:
            # Queued rows left on disk are cancelled on startup. Drop all in-memory
            # reservations even when the journal is temporarily unwritable.
            reason += " 작업 이력에 취소를 기록하지 못했습니다."
        for key in keys:
            self.pending.pop(key, None)
            self.pending_since.pop(key, None)
            self.recent[key].update(
                state="cancelled", finished=finished, error=reason, _manual_message=reason
            )
        self._trim_recent()

    def _trim_recent(self):
        terminal = [
            key for key, job in self.recent.items() if job["state"] not in ("queued", "running")
        ]
        for key in terminal[:-MAX_RESULTS]:
            self.recent.pop(key)
            self.manual.pop(key, None)

    def _start_worker(self, key, action, body, persist, queued=False):
        self.active = key
        self.cancel_event = Event()
        self.operations.reset_job_progress()
        self.recent[key]["state"] = "running"
        Thread(
            target=self._run,
            args=(key, action, body, self.cancel_event, persist, queued),
            daemon=True,
        ).start()

    def _next_manual(self):
        self._expire_pending()
        while self.pending and not self.active:
            key = next(iter(self.pending))
            if self.closing or self.pending[key] is not self.operations.credentials:
                self._cancel_reservations((key,), "SSH 세션이 변경되어 예약을 취소했습니다.")
                continue
            request, _ = self.manual[key]
            try:
                with closing(sqlite3.connect(self.db)) as db, db:
                    changed = db.execute(
                        "UPDATE jobs SET state='running' WHERE id=? AND state='queued'", (key,)
                    )
                    if changed.rowcount != 1:
                        raise sqlite3.DatabaseError("missing reservation")
            except sqlite3.Error:
                self.invalidate_pending(
                    "실행 시작을 이력에 기록할 수 없어 대기 중인 예약을 취소했습니다."
                )
                return
            self.pending.pop(key)
            self.pending_since.pop(key, None)
            self.busy_workspace = self.workspace()
            self._start_worker(key, request.action, request.body(key), True, queued=True)

    def _deduplicate(self, key, request, repeat_message):
        with closing(sqlite3.connect(self.db)) as db, db:
            if db.execute("SELECT 1 FROM closed_manual_receipts WHERE id=?", (key,)).fetchone():
                raise Busy("접수 확인을 종료한 요청입니다. 이 요청은 실행되지 않습니다.")
            alias = db.execute("SELECT job FROM manual_aliases WHERE id=?", (key,)).fetchone()
            previous = alias[0] if alias else key
            if previous in self.manual:
                existing, _ = self.manual[previous]
                if request and existing.signature == request.signature:
                    return previous
                raise Busy(repeat_message)
            if (
                alias
                or key in self.recent
                or db.execute("SELECT 1 FROM jobs WHERE id=?", (key,)).fetchone()
            ):
                raise Busy(repeat_message)
            if not request:
                return None
            conflicts = []
            partial_overlap = False
            for other_id, (other, _) in self.manual.items():
                if self.recent[other_id]["state"] not in ("queued", "running"):
                    continue
                if request.signature == other.signature:
                    db.execute("INSERT INTO manual_aliases VALUES(?,?)", (key, other_id))
                    return other_id
                if request.action == other.action and not set(request.targets).isdisjoint(
                    other.targets
                ):
                    conflicts.append(other_id)
                    if request.operation == other.operation:
                        partial_overlap = True
            if partial_overlap:
                raise Busy(
                    "이미 예약했거나 실행 중인 작업과 대상 포트가 일부 겹칩니다. 겹치지 않는 포트만 선택하거나 기존 예약을 취소하세요.",
                    conflicts,
                )
            if conflicts:
                raise Busy(
                    "같은 포트에 반대 방향의 PoE 요청이 있습니다. 대기 중인 예약은 취소 후 다시 신청하고, 실행 중인 작업은 완료를 기다리세요.",
                    conflicts,
                )
        return None

    def submit_ping(self, body):
        """Internal only until the authenticated Ping API and its tests are ready."""
        if not isinstance(body, dict) or set(body) != {"requestId", "plan"}:
            raise ValueError("Ping request requires an ID and plan.")
        key = body["requestId"]
        if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{32}", key) is None:
            raise ValueError("Invalid Ping request ID.")
        plan = PingPlan.parse(body["plan"])
        with self.lock:
            if self.closing:
                raise Busy("서버가 종료 중입니다.")
            if self.ping_batch and self.ping_batch.key == key:
                if self.ping_batch.plan == plan:
                    return key
                raise Busy("Ping 요청 ID를 이미 사용했습니다.")
            if self.ping_batch:
                raise Busy("다른 Ping 작업이 실행 중입니다.")
            creds = self.operations.credentials
            if creds is None:
                raise ValueError("먼저 SSH 접속 정보를 입력하세요.")
            started = now()
            # Store acceptance before any SSH slot can be claimed. On restart
            # the existing running-job recovery marks this as interrupted.
            with closing(sqlite3.connect(self.db)) as db, db:
                tables = ("jobs", "manual_aliases", "closed_manual_receipts")
                if key in self.recent or any(
                    db.execute(f"SELECT 1 FROM {table} WHERE id=?", (key,)).fetchone()
                    for table in tables
                ):
                    raise Busy("Ping 요청 ID를 이미 사용했습니다. 작업 상태를 확인하세요.")
                db.execute(
                    "INSERT INTO jobs VALUES(?,?,?,?,?,?,?)",
                    (key, "ping.run", "running", started, None, "", "[]"),
                )
            self.ping_batch = PingBatch(key, plan, creds)
            self.recent[key] = dict(
                id=key,
                action="ping.run",
                state="running",
                started=started,
                finished=None,
                error="",
                result=None,
            )
            self._trim_recent()
            return key

    def _tick_ping(self):
        # Called under the same lock as monitor and manual queue admission.
        batch = self.ping_batch
        if batch is None:
            return
        if self.closing or batch.credentials is not self.operations.credentials:
            batch.stop("SESSION_CHANGED")
        if batch.terminal:
            self._finish_ping(batch)
            return
        if self.active or self.pending or self.controls.awaiting_confirmation():
            return
        slot = batch.claim()
        if slot is None:
            return
        self.busy_workspace = self.workspace()
        self.active = batch.key
        self.cancel_event = Event()
        self.operations.reset_job_progress()
        try:
            Thread(
                target=self._run_ping_slot,
                args=(batch, slot, self.cancel_event),
                daemon=True,
            ).start()
        except RuntimeError:
            batch.stop("WORKER_START_FAILED", state="failed")
            batch.record(slot, error=ErrorCode.UNEXPECTED_ERROR)
            self.active = self.cancel_event = self.busy_workspace = None
            self._finish_ping(batch)

    def _finish_ping(self, batch):
        if self.ping_batch is not batch or not batch.terminal:
            return
        snapshot = batch.snapshot(complete=True)
        finished = now()
        state = batch.state
        error = batch.stop_reason
        summary = f"Ping · {snapshot['processedPairs']}/{snapshot['total']}"
        try:
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute(
                    "UPDATE jobs SET state=?,finished=?,summary=? WHERE id=?",
                    (state, finished, summary, batch.key),
                )
        except sqlite3.Error:
            state = "failed"
            error = "PING_HISTORY_WRITE_FAILED"
        self.recent[batch.key].update(
            state=state,
            finished=finished,
            error=error,
            result=snapshot,
        )
        self.ping_batch = None
        self._trim_recent()

    def _run_ping_slot(self, batch, slot, cancel):
        result, error, uncertain = None, None, False
        try:
            result = self.operations._ping_pair(
                batch.plan, slot.target, slot.destination, batch.credentials, cancel
            )
        except CollectionFailure as exc:
            error = exc.code
        except Exception:
            error = ErrorCode.UNEXPECTED_ERROR
            uncertain = True
        with self.lock:
            try:
                batch.record(slot, result, error=error, uncertain=uncertain)
            except Exception:
                # Never replay a slot after an uncertain worker or projection failure.
                batch.stop("RESULT_PROJECTION_FAILED", state="failed")
                batch.record(slot, error=ErrorCode.UNEXPECTED_ERROR, uncertain=True)
            finally:
                self.active = self.cancel_event = self.busy_workspace = None
                if batch.terminal:
                    self._finish_ping(batch)
                # Manual FIFO wins immediately; monitor or another Ping slot
                # may run only on a subsequent normal service tick.
                self._next_manual()

    def submit(self, action, body, *, monitored=False):
        if not isinstance(action, str) or action not in ACTIONS or not isinstance(body, dict):
            raise ValueError("작업 이름과 입력 형식을 확인하세요.")
        with self.lock:
            if self.closing:
                raise Busy("서버가 종료 중입니다.")
            self._expire_pending()
            repeat_message = {
                "control.quick-execute": DUPLICATE_POE_REQUEST,
                "cable.test": "이미 접수한 케이블 진단입니다. 작업 이력을 확인하세요.",
            }.get(action)
            request = None
            queued = False
            if repeat_message:
                key = body.get("requestId")
                if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{32}", key) is None:
                    raise ValueError("작업 요청 식별자가 올바르지 않습니다.")
                # Immediate legacy callers receive validation errors through their job.
                # A deferred job must have an exact, confirmed target set before acceptance.
                try:
                    request = ManualRequest.parse(action, body)
                except ValueError:
                    if self.active and self.monitor and self.monitor.job == self.active:
                        raise
                existing = self._deduplicate(key, request, repeat_message)
                if existing:
                    return existing
                if request and (self.active or self.pending):
                    queued = bool(
                        self.pending
                        or self.active in self.manual
                        or (self.ping_batch and self.ping_batch.key == self.active)
                        or (self.monitor and self.monitor.job == self.active)
                    )
            else:
                key = secrets.token_hex(16)
            if (self.active or self.pending) and not queued:
                raise Busy("다른 작업이 실행 중입니다. 완료 후 다시 실행하세요.")
            if queued and len(self.pending) >= MAX_PENDING_MANUAL:
                raise Busy("수동 작업 예약 대기 한도(24건)에 도달했습니다. 기존 예약을 확인하세요.")
            if queued and self.operations.credentials is None:
                raise ValueError("먼저 SSH 접속 정보를 입력하세요.")
            started = now()
            state = "queued" if queued else "running"
            persist = action in PERSIST and not monitored
            if persist:
                with closing(sqlite3.connect(self.db)) as db, db:
                    db.execute(
                        "INSERT INTO jobs VALUES(?,?,?,?,?,?,?)",
                        (key, action, state, started, None, "", "[]"),
                    )
            self.recent[key] = dict(
                id=key,
                action=action,
                state=state,
                started=started,
                finished=None,
                error="",
                result=None,
            )
            if action == "cable.test":
                response_key = body.get("responseKey")
                if isinstance(response_key, str) and re.fullmatch(r"[0-9a-f]{32}", response_key):
                    self.recent[key]["_response_key"] = response_key
            if request:
                self.manual[key] = (request, queued)
            if queued:
                self.pending[key] = self.operations.credentials
                self.pending_since[key] = time.monotonic()
            else:
                self.busy_workspace = self.workspace() if (monitored or request) else None
                body = request.body(key) if request else dict(body)
                body.pop("responseKey", None)
                self._start_worker(key, action, body, persist)
            self._trim_recent()
            return key

    def submit_receipt(self, action, body):
        with self.lock:
            key = self.submit(action, body)
            return {
                "id": key,
                "state": self.recent[key]["state"],
                "wasQueued": key in self.manual and self.manual[key][1],
            }

    def _receipt_from_memory(self, key):
        entry = self.recent.get(key)
        if entry is None or entry["action"] not in MANUAL_ACTIONS:
            return None
        state = entry.get("_manual_state", entry["state"])
        message = entry.get("_manual_message") or entry["error"]
        # A receipt can be durable even if preparation raised before the
        # worker started. Never present an orphaned entry as an active job.
        if state == "running" and self.active != key:
            state = "uncertain"
            message = "실행 상태를 확인할 수 없습니다. 해당 포트 상태를 직접 조회하세요."
        elif state == "queued" and key not in self.pending:
            state = "uncertain"
            message = "예약 상태를 확인할 수 없습니다. 해당 포트 상태를 직접 조회하세요."
        return {
            "id": key,
            "accepted": True,
            "state": state,
            "wasQueued": key in self.manual and self.manual[key][1],
            "historyOnly": False,
            "message": message,
        }

    def manual_receipt(self, key, *, close_missing=False):
        """Resolve acceptance without accessing the switch or replaying a command.

        The same lock protects submission and closing a missing request. A closed
        ID is durably rejected even if its original HTTP POST arrives later.
        Only summary fields are returned; cable response keys and CLI are excluded.
        """
        if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{32}", key) is None:
            raise ValueError("작업 요청 식별자가 올바르지 않습니다.")
        with self.lock:
            current = self._receipt_from_memory(key)
            if current is not None:
                return current
            with closing(sqlite3.connect(self.db)) as db, db:
                if close_missing:
                    db.execute("BEGIN IMMEDIATE")
                alias = db.execute("SELECT job FROM manual_aliases WHERE id=?", (key,)).fetchone()
                canonical = alias[0] if alias else key
                current = self._receipt_from_memory(canonical)
                if current is not None:
                    return current
                row = db.execute(
                    "SELECT action,state,summary FROM jobs WHERE id=?", (canonical,)
                ).fetchone()
                if row and row[0] in MANUAL_ACTIONS:
                    state, message = row[1], row[2]
                    # On startup pending work is cancelled and running work is
                    # uncertain. Also handle an older failed journal update.
                    if state == "queued":
                        state = "cancelled"
                        message = "실행되지 않은 예약입니다. 작업 이력과 포트 상태를 확인하세요."
                    elif state == "running":
                        state = "uncertain"
                        message = "실행 결과를 확인할 수 없습니다. 포트 상태를 직접 조회하세요."
                    return {
                        "id": canonical,
                        "accepted": True,
                        "state": state,
                        "wasQueued": False,
                        "historyOnly": True,
                        "message": message,
                    }
                # Do not close an ID already owned by another job or a broken alias.
                if alias or row or key in self.recent:
                    raise Busy("작업 식별자의 접수 기록을 안전하게 확인할 수 없습니다.")
                closed = db.execute(
                    "SELECT 1 FROM closed_manual_receipts WHERE id=?", (key,)
                ).fetchone()
                if not closed and not close_missing:
                    return None
                if not closed:
                    db.execute("INSERT INTO closed_manual_receipts VALUES(?,?)", (key, now()))
                return {
                    "id": key,
                    "accepted": False,
                    "state": "not_accepted",
                    "wasQueued": False,
                    "historyOnly": True,
                    "message": "미접수로 확정했습니다. 이전 요청은 나중에 도착해도 실행되지 않습니다.",
                }

    def record_receipt_diagnostic(self, key, stage, status, code):
        """Keep bounded local diagnostics without request bodies, credentials or CLI."""
        if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{32}", key) is None:
            return
        path = self.directory / "receipt-diagnostics.jsonl"
        record = {
            "time": now(),
            "requestId": key,
            "stage": stage,
            "httpStatus": status,
            "code": code,
        }
        with self.receipt_log_lock, suppress(OSError):
            if path.exists() and path.stat().st_size >= 1024 * 1024:
                path.replace(path.with_suffix(".jsonl.1"))
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def cancel(self, key, *, queued_only=False):
        with self.lock:
            if key not in self.recent:
                raise KeyError(key)
            if self.ping_batch and key == self.ping_batch.key:
                if queued_only and self.active == key:
                    return {"requested": False}
                self.ping_batch.stop("OPERATOR_CANCELLED")
                if self.active == key:
                    self.cancel_event.set()
                else:
                    self._finish_ping(self.ping_batch)
                return {"requested": True}
            if key in self.pending:
                self._cancel_reservations((key,), "운영자가 실행 전에 예약을 취소했습니다.")
                return {"requested": True}
            if queued_only:
                return {"requested": False}
            if key == self.active:
                self.cancel_event.set()
            return {"requested": key == self.active}

    def get(self, key, after=0, *, response_key=None):
        with self.lock:
            if key not in self.recent:
                with closing(sqlite3.connect(self.db)) as db:
                    alias = db.execute(
                        "SELECT job FROM manual_aliases WHERE id=?", (key,)
                    ).fetchone()
                if alias:
                    key = alias[0]
            item = self.recent[key].copy()
            if key in self.manual:
                item["wasQueued"] = self.manual[key][1]
            owner_key = item.pop("_response_key", None)
            item.pop("_manual_state", None)
            item.pop("_manual_message", None)
            progress = self.operations.progress_snapshot() if self.active == key else {}
            if self.ping_batch and key == self.ping_batch.key:
                progress = self.ping_batch.snapshot(after=after)
            if self.active == key and item["action"] in {"ports.query", "ports.refresh"}:
                progress.update(self.operations.query_progress(after))
                if item["action"] == "ports.query" and "targets" in progress:
                    progress["targetText"] = self.operations.query_target_text
            if item["action"] == "cable.test":
                if self.active == key:
                    progress.update(self.operations.cable_progress(after))
                # Reconnected browsers can follow the job, but only its initiating tab
                # holds the ephemeral key for command output. Never store it on disk.
                can_read = (
                    owner_key is not None
                    and isinstance(response_key, str)
                    and response_key.isascii()
                    and secrets.compare_digest(owner_key, response_key)
                )
                if not can_read:
                    if "cableRows" in progress:
                        progress["cableRows"] = _without_excerpts(progress["cableRows"])
                    if item["result"] and "rows" in item["result"]:
                        item["result"] = dict(
                            item["result"], rows=_without_excerpts(item["result"]["rows"])
                        )
            return dict(item, progress=progress)

    def history(self):
        with self.lock, closing(sqlite3.connect(self.db)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM jobs WHERE action != 'ports.export' ORDER BY rowid DESC LIMIT 200"
            )
            history = []
            for row in rows:
                item = dict(row)
                item["files"] = [
                    file
                    for file in json.loads(row["files"])
                    if Path(file["name"]).suffix.lower() in EXTENSIONS
                ]
                if item["state"] == "queued" and item["id"] not in self.pending:
                    # A write fault may leave the database behind an in-memory cancellation.
                    job = self.recent.get(item["id"], {})
                    item.update(
                        state="cancelled",
                        finished=job.get("finished"),
                        summary=job.get("error")
                        or "예약을 취소했습니다. 이력에 취소를 기록하지 못했습니다.",
                    )
                history.append(item)
            return history

    def workspace(self):
        with self.lock:
            if self.active:
                return {**(self.busy_workspace or {}), "busy": True}
            workspace = {"groups": self.operations.inventory_view()}
            if self.operations.query_target_text is not None and self.operations.credentials:
                workspace["targetText"] = self.operations.query_target_text
            view = self.port_view
            if not view or not self.operations.credentials:
                return workspace
            available = {
                (r.group.target.ip, r.group.target.port, r.interface)
                for r in self.operations.last_results
            }
            rows = []
            for saved in view["rows"]:
                row = dict(saved, controlUsed=bool(saved.get("controlUsed")))
                key = row["ip"], int(row["ssh_port"]), row["interface"]
                if key not in available:
                    # A control consumed this observation and there is no replacement read.
                    row.update(controlUsed=True, link_status="", link_observed_at="")
                rows.append(row)
            workspace["query"] = {"rows": rows}
            if any(g.all_ports for g in self.operations.query_scope):
                workspace["query"]["discoveryErrors"] = self.operations.discovery_errors()
            return workspace

    def _store_port_view(self, result, *, monitored=False):
        sampled = set(result.pop("_sampledPorts", ()))
        if self.port_view:
            prior = {
                (row["ip"], row["ssh_port"], row["interface"]): row
                for row in self.port_view["rows"]
            }
            for row in result["rows"]:
                key = row["ip"], row["ssh_port"], row["interface"]
                old = prior.get(key)
                if old is None:
                    continue
                stale = row["status"] != "ok" or row.get("watts") is None
                retain_last = stale and (monitored or "lastSuccessfulObservation" in old)
                prior_observation = None
                if key in sampled or retain_last:
                    if (
                        old.get("complete")
                        and old["status"] == "ok"
                        and not old.get("refreshInvalidated")
                    ):
                        prior_observation = _observation_for_comparison(old)
                    if (not prior_observation or prior_observation.get("watts") is None) and (
                        monitored or retain_last
                    ):
                        prior_observation = old.get("lastSuccessfulObservation", prior_observation)
                # Display history is kept separately from current measurements and permissions.
                if retain_last and prior_observation and prior_observation.get("watts") is not None:
                    row["lastSuccessfulObservation"] = prior_observation
                if key in sampled:
                    if prior_observation:
                        row["previousObservation"] = prior_observation
                elif "previousObservation" in old:
                    row["previousObservation"] = old["previousObservation"]
        self.port_view = {"rows": result["rows"]}

    def _record_power(self, rows, plan, result):
        if not rows:
            return
        try:
            if plan:
                self.power_history.record(rows, plan.id, plan.slot, plan.minutes * 60)
            else:
                self.power_history.record(rows)
        except sqlite3.Error:
            # A storage problem cannot change the outcome of a device operation.
            failed_at = datetime.now(UTC)
            cutoff = failed_at - timedelta(days=7)
            self.power_write_errors = {
                key: at for key, at in self.power_write_errors.items() if at >= cutoff
            }
            for row in rows:
                self.power_write_errors[(row["ip"], row["ssh_port"], row["interface"])] = failed_at
            if result is not None:
                result["powerHistoryWarning"] = POWER_HISTORY_WARNING

    def read_power_history(self, ip, ssh_port, interface, hours):
        result = self.power_history.read(ip, ssh_port, interface, hours)
        with self.lock:
            failed_at = self.power_write_errors.get((ip, ssh_port, interface))
        if failed_at and failed_at >= datetime.now(UTC) - timedelta(hours=hours):
            result["warning"] = POWER_HISTORY_WARNING
        return result

    def _output(self, relative):
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise ValueError("결과 파일 경로를 확인하세요.")
        outputs = self.directory / "outputs"
        file = (self.directory / relative).resolve(strict=True)
        if (
            not file.is_relative_to(outputs)
            or not file.is_file()
            or file.suffix.lower() not in EXTENSIONS
        ):
            raise ValueError("결과 파일이 저장 폴더를 벗어났습니다.")
        return file

    def download(self, key):
        with closing(sqlite3.connect(self.db)) as db:
            row = db.execute("SELECT path FROM downloads WHERE id=?", (key,)).fetchone()
        if not row:
            raise KeyError(key)
        return self._output(row[0])

    def result_folder(self, key):
        file = self.download(key)
        with closing(sqlite3.connect(self.db)) as db:
            registered = db.execute("SELECT folder FROM downloads WHERE id=?", (key,)).fetchone()
        if registered and registered[0]:
            folder = Path(registered[0])
            # This directory comes from the completed backup, never from the open-folder request.
            if folder.is_absolute() and folder.is_dir():
                return folder.resolve()
        return file.parent

    def _run(self, key, action, body, cancel, persist, queued=False):
        result, error, state = None, "", "done"
        failure_code = None
        uncertain_failure = False
        try:
            if action in {
                "ports.query",
                "ports.refresh",
                "cable.test",
                "targets.apply",
                "session.open",
                "session.close",
                "hostkey.forget",
            }:
                self.controls.invalidate(
                    observations_changed=action in {"ports.query", "ports.refresh"}
                )
            service = self.controls if action.startswith("control.") else self.operations
            if queued and action == "control.quick-execute":
                result = self.controls.dispatch_queued(action, body, cancel)
            else:
                result = service.dispatch(action, body, cancel)
            if cancel.is_set():
                state = "cancelled"
        except (ValueError, CollectionFailure) as exc:
            uncertain_failure = getattr(exc, "control_outcome_unknown", False)
            if isinstance(exc, CollectionFailure):
                failure_code = exc.code
            error = getattr(exc, "safe_message", None) or str(exc)
            # Some underlying validation exceptions include the invalid value.
            creds = self.operations.credentials
            for secret in (
                body.get("password"),
                body.get("enable_secret"),
                getattr(creds, "password", ""),
                getattr(creds, "enable_secret", ""),
            ):
                if isinstance(secret, str) and secret:
                    error = error.replace(secret, "•••")
            if not isinstance(exc, TargetInputError):
                error = error[:450]
            state = "cancelled" if cancel.is_set() else "failed"
        except Exception:
            uncertain_failure = True
            error = "작업을 마치지 못했습니다. 대상 및 접속 설정을 확인하세요."
            state = "cancelled" if cancel.is_set() else "failed"
        finally:
            body.clear()
        manual = self.manual.get(key)
        outcome = None
        if manual:
            try:
                outcome = manual_outcome(
                    manual[0],
                    state,
                    result,
                    error,
                    uncertain_failure,
                    cable_progress=self.operations.cable_progress,
                )
            except Exception:
                # A malformed device readback or progress record must not kill
                # this worker between a device write and queue cleanup. Stop
                # overlapping requests instead of assuming the write was safe.
                outcome = (
                    "uncertain",
                    "작업 결과를 해석하지 못했습니다. 해당 포트를 직접 조회한 뒤 다시 신청하세요.",
                    set(manual[0].targets),
                )
        files, registrations = [], []
        finished = now()
        try:
            if result is not None:
                for item in result.get("files", []):
                    path = self._output(item["id"])
                    handle = secrets.token_hex(16)
                    folder = result.get("output_directory") if action == "backup.run" else None
                    registrations.append((handle, str(path.relative_to(self.directory)), folder))
                    files.append({"id": handle, "name": path.name})
                if "files" in result:
                    result["files"] = files
            if persist:
                labels = {"done": "완료", "failed": "실패", "cancelled": "취소됨"}
                history_state, summary = outcome[:2] if outcome else (state, labels[state])
                if result:
                    rows = result.get("results", result.get("rows", []))
                    if action == "control.quick-execute":
                        summary += (
                            f" · 선택 {result['selected']}개 · 건너뜀 {len(result['skipped'])}개"
                            f" · 제어 불가 {len(result['blocked'])}개"
                        )
                    if rows:
                        summary += f" · {len(rows)}건"
                        counts = {}
                        for row in rows:
                            status = row.get("status")
                            if isinstance(status, str):
                                counts[status] = counts.get(status, 0) + 1
                        if counts:
                            summary += " (" + ", ".join(f"{s} {n}" for s, n in counts.items()) + ")"
                with closing(sqlite3.connect(self.db)) as db, db:
                    db.executemany(
                        "INSERT INTO downloads (id,path,folder) VALUES (?,?,?)", registrations
                    )
                    db.execute(
                        "UPDATE jobs SET state=?,finished=?,summary=?,files=? WHERE id=?",
                        (
                            history_state,
                            finished,
                            summary,
                            json.dumps(files, ensure_ascii=False),
                            key,
                        ),
                    )
        except OSError, sqlite3.Error, ValueError, KeyError:
            error, state, result = (
                "작업 결과를 기록하지 못했습니다. 로컬 데이터 폴더와 남은 용량을 확인하세요.",
                "failed",
                None,
            )
            if manual:
                outcome = manual_outcome(manual[0], state, None, error, True)
            if persist:
                with suppress(sqlite3.Error), closing(sqlite3.connect(self.db)) as db, db:
                    db.execute(
                        "UPDATE jobs SET state=?,finished=?,summary=?,files='[]' WHERE id=?",
                        (
                            outcome[0] if outcome else state,
                            finished,
                            outcome[1] if outcome else error,
                            key,
                        ),
                    )
        finally:
            with self.lock:
                monitored = not persist and action in {"ports.query", "ports.refresh"}
                plan = (
                    self.monitor if monitored and self.monitor and self.monitor.job == key else None
                )
                try:
                    if result is not None:
                        self._record_power(sampled_rows(result), plan, result)
                    if action == "ports.query":
                        if result and "rows" in result:
                            self._store_port_view(result, monitored=monitored)
                        elif self.operations.query_started:
                            self._record_power(
                                self.operations.invalidate_failed_refresh(cancel.is_set()),
                                plan,
                                result,
                            )
                            self.port_view = None
                    elif action in {
                        "ports.refresh",
                        "control.quick",
                        "control.quick-execute",
                        "control.apply",
                    }:
                        if result and "rows" in result:
                            self._store_port_view(result, monitored=monitored)
                        elif state != "done":
                            invalid = self.operations.invalidate_failed_refresh(cancel.is_set())
                            if invalid or (
                                action == "ports.refresh" and self.operations.query_started
                            ):
                                self._record_power(invalid, plan, result)
                                previous = self.port_view["rows"] if self.port_view else []
                                registered = {
                                    (group["ip"], group["ssh_port"], port)
                                    for group in self.operations.inventory_view()
                                    for port in group["interfaces"]
                                }
                                rows = {
                                    (r["ip"], int(r["ssh_port"]), r["interface"]): r
                                    for r in [*previous, *invalid]
                                }
                                self._store_port_view(
                                    {
                                        "rows": [
                                            row for key, row in rows.items() if key in registered
                                        ]
                                    },
                                    monitored=monitored,
                                )
                    elif (
                        action
                        in {"session.open", "session.close", "targets.apply", "hostkey.forget"}
                        and state == "done"
                    ):
                        self.port_view = None

                except Exception:
                    # A device command can finish even when its readback or
                    # workspace/history projection is malformed. Never strand
                    # the single device slot or replay a possibly sent write.
                    error = "작업 결과 반영 중 오류가 발생했습니다. 대상 상태를 다시 조회하세요."
                    state, result = "failed", None
                    self.port_view = None
                    # Any outstanding approval may depend on the invalid readback.
                    with suppress(Exception):
                        self.controls.invalidate()
                    if manual:
                        outcome = (
                            "uncertain",
                            "작업 후 상태를 확인하지 못했습니다. 해당 포트를 직접 조회한 뒤 다시 신청하세요.",
                            set(manual[0].targets),
                        )
                    if persist:
                        with suppress(sqlite3.Error), closing(sqlite3.connect(self.db)) as db, db:
                            db.execute(
                                "UPDATE jobs SET state=?,finished=?,summary=?,files='[]' WHERE id=?",
                                (
                                    outcome[0] if outcome else state,
                                    finished,
                                    outcome[1] if outcome else error,
                                    key,
                                ),
                            )
                plan = self.monitor
                if plan and plan.job == key:
                    plan.finished(
                        key,
                        result,
                        error or ("조회 취소" if state == "cancelled" else ""),
                        finished,
                        failure_code,
                    )
                elif plan and (
                    (
                        action
                        in {"session.open", "session.close", "targets.apply", "hostkey.forget"}
                        and state == "done"
                    )
                    or (
                        action == "ports.query"
                        and self.operations.query_started
                        and self.operations.query_scope != plan.groups
                    )
                ):
                    self.monitor = None
                self.recent[key].update(state=state, finished=finished, result=result, error=error)
                self.active = self.cancel_event = self.busy_workspace = None
                if outcome:
                    display, message, uncertain = outcome
                    self.recent[key].update(_manual_state=display, _manual_message=message)
                    overlapping = tuple(
                        waiting
                        for waiting in self.pending
                        if not uncertain.isdisjoint(self.manual[waiting][0].targets)
                    )
                    self._cancel_reservations(
                        overlapping,
                        "앞선 작업 결과가 불확실해 예약을 취소했습니다. 해당 포트를 조회한 뒤 다시 신청하세요.",
                    )
                self._trim_recent()
                self._next_manual()
