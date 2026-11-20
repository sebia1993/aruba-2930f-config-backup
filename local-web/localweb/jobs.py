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

from aruba2930f_backup.models import CollectionFailure

from .controls import Controls
from .monitoring import PortMonitoring
from .operations import Operations, TargetInputError, _groups
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
    pass


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
                "UPDATE jobs SET state='interrupted',finished=?,summary=? WHERE state='running'",
                (now(), "이전 실행 중 서버가 종료되었습니다."),
            )
        self.lock = RLock()
        self.recent = OrderedDict()
        self.port_view = None
        self.active = None
        self.closing = False
        self.cancel_event = None
        self.monitor: PortMonitoring | None = None

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
            plan = self.monitor
            if (
                self.closing
                or not plan
                or plan.job
                or plan.suspended
                or time.monotonic() < plan.due
            ):
                return
            plan.advance_to_next_slot()
            if self.active or self.controls.awaiting_confirmation():
                # Never queue missed reads behind a manual operation or its review.
                return
            plan.job = self.submit("ports.refresh", {"targets": plan.targets}, monitored=True)
            plan.next_at = None

    def claim_shutdown(self):
        with self.lock:
            if self.active:
                raise Busy("작업을 마치거나 취소한 후 서버를 종료하세요.")
            self.monitor = None
            self.closing = True

    def submit(self, action, body, *, monitored=False):
        if not isinstance(action, str) or action not in ACTIONS or not isinstance(body, dict):
            raise ValueError("작업 이름과 입력 형식을 확인하세요.")
        with self.lock:
            if self.closing or self.active:
                if self.active and self.monitor and self.monitor.job == self.active:
                    raise Busy("자동 전력 조회가 진행 중입니다. 조회 완료 후 다시 실행하세요.")
                raise Busy("다른 작업이 실행 중이거나 서버가 종료 중입니다.")
            repeat_message = {
                "control.quick-execute": DUPLICATE_POE_REQUEST,
                "cable.test": "이미 접수한 케이블 진단입니다. 진행 중인 작업의 완료를 기다려 주세요.",
            }.get(action)
            if repeat_message:
                key = body.get("requestId")
                if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{32}", key) is None:
                    raise ValueError("작업 요청 식별자가 올바르지 않습니다.")
                if key in self.recent:
                    raise Busy(repeat_message)
            else:
                key = secrets.token_hex(16)
            started = now()
            persist = action in PERSIST and not monitored
            if persist:
                with closing(sqlite3.connect(self.db)) as db, db:
                    try:
                        db.execute(
                            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?)",
                            (key, action, "running", started, None, "", "[]"),
                        )
                    except sqlite3.IntegrityError:
                        if repeat_message:
                            raise Busy(repeat_message) from None
                        raise
            self.recent[key] = dict(
                id=key,
                action=action,
                state="running",
                started=started,
                finished=None,
                error="",
                result=None,
            )
            if action == "cable.test":
                response_key = body.pop("responseKey", None)
                if isinstance(response_key, str) and re.fullmatch(r"[0-9a-f]{32}", response_key):
                    self.recent[key]["_response_key"] = response_key
            while len(self.recent) > MAX_RESULTS:
                self.recent.popitem(last=False)
            self.active = key
            self.cancel_event = Event()
            self.operations.reset_job_progress()
            Thread(
                target=self._run, args=(key, action, body, self.cancel_event, persist), daemon=True
            ).start()
            return key

    def cancel(self, key):
        with self.lock:
            if key not in self.recent:
                raise KeyError(key)
            if key == self.active:
                self.cancel_event.set()
            return {"requested": key == self.active}

    def get(self, key, after=0, *, response_key=None):
        with self.lock:
            item = self.recent[key].copy()
            owner_key = item.pop("_response_key", None)
            progress = self.operations.progress_snapshot() if self.active == key else {}
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
        with closing(sqlite3.connect(self.db)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM jobs WHERE action != 'ports.export' ORDER BY rowid DESC LIMIT 200"
            )
            return [
                dict(
                    dict(row),
                    files=[
                        item
                        for item in json.loads(row["files"])
                        if Path(item["name"]).suffix.lower() in EXTENSIONS
                    ],
                )
                for row in rows
            ]

    def workspace(self):
        with self.lock:
            if self.active:
                return {"busy": True}
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

    def _run(self, key, action, body, cancel, persist):
        result, error, state = None, "", "done"
        failure_code = None
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
            result = service.dispatch(action, body, cancel)
            if cancel.is_set():
                state = "cancelled"
        except (ValueError, CollectionFailure) as exc:
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
            error = "작업을 마치지 못했습니다. 대상 및 접속 설정을 확인하세요."
            state = "cancelled" if cancel.is_set() else "failed"
        finally:
            body.clear()
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
                summary = labels[state]
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
                        (state, finished, summary, json.dumps(files, ensure_ascii=False), key),
                    )
        except OSError, sqlite3.Error, ValueError, KeyError:
            error, state, result = (
                "작업 결과를 기록하지 못했습니다. 로컬 데이터 폴더와 남은 용량을 확인하세요.",
                "failed",
                None,
            )
            if persist:
                with suppress(sqlite3.Error), closing(sqlite3.connect(self.db)) as db, db:
                    db.execute(
                        "UPDATE jobs SET state=?,finished=?,summary=?,files='[]' WHERE id=?",
                        (state, finished, error, key),
                    )
        finally:
            with self.lock:
                monitored = not persist and action in {"ports.query", "ports.refresh"}
                plan = (
                    self.monitor if monitored and self.monitor and self.monitor.job == key else None
                )
                if result is not None:
                    self._record_power(sampled_rows(result), plan, result)
                if action == "ports.query":
                    if result and "rows" in result:
                        self._store_port_view(result, monitored=monitored)
                    elif self.operations.query_started:
                        self._record_power(
                            self.operations.invalidate_failed_refresh(cancel.is_set()), plan, result
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
                        if invalid:
                            self._record_power(invalid, plan, result)
                            previous = self.port_view["rows"] if self.port_view else []
                            rows = {
                                (r["ip"], int(r["ssh_port"]), r["interface"]): r
                                for r in [*previous, *invalid]
                            }
                            self._store_port_view(
                                {"rows": list(rows.values())}, monitored=monitored
                            )
                elif (
                    action in {"session.open", "session.close", "targets.apply", "hostkey.forget"}
                    and state == "done"
                ):
                    self.port_view = None
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
                self.active = self.cancel_event = None
