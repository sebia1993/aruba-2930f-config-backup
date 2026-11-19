"""Run a single device operation at a time; retain only summaries and download handles on disk."""

import json
import secrets
import sqlite3
from collections import OrderedDict
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock, Thread

from aruba2930f_backup.models import CollectionFailure

from .controls import Controls
from .operations import Operations, TargetInputError

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
        "ports.export",
        "targets.preview",
        "targets.apply",
        "profiles.import",
        "profiles.export",
        "backup.run",
        "backup.history",
        "backup.pick-directory",
        "history.open-folder",
        "logs.query",
        "control.preview",
        "control.apply",
        "control.history",
        "control.inspect",
        "control.quick",
        "control.import-history",
    ]
)
PERSIST = {
    "ports.query",
    "ports.refresh",
    "ports.export",
    "profiles.export",
    "backup.run",
    "logs.query",
    "control.quick",
    "control.apply",
}
EXTENSIONS = {".jsonl", ".json", ".txt", ".xlsx", ".csv"}
MAX_RESULTS = 8


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


class Busy(Exception):
    pass


class Jobs:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.operations = Operations(self.directory)
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
        self.lock = Lock()
        self.recent = OrderedDict()
        self.port_view = None
        self.active = None
        self.closing = False
        self.cancel_event = None

    def claim_shutdown(self):
        with self.lock:
            if self.active:
                raise Busy("작업을 마치거나 취소한 후 서버를 종료하세요.")
            self.closing = True

    def submit(self, action, body):
        if not isinstance(action, str) or action not in ACTIONS or not isinstance(body, dict):
            raise ValueError("작업 이름과 입력 형식을 확인하세요.")
        with self.lock:
            if self.closing or self.active:
                raise Busy("다른 작업이 실행 중이거나 서버가 종료 중입니다.")
            key = secrets.token_hex(16)
            started = now()
            if action in PERSIST:
                with closing(sqlite3.connect(self.db)) as db, db:
                    db.execute(
                        "INSERT INTO jobs VALUES(?,?,?,?,?,?,?)",
                        (key, action, "running", started, None, "", "[]"),
                    )
            self.recent[key] = dict(
                id=key,
                action=action,
                state="running",
                started=started,
                finished=None,
                error="",
                result=None,
            )
            while len(self.recent) > MAX_RESULTS:
                self.recent.popitem(last=False)
            self.active = key
            self.cancel_event = Event()
            self.operations.progress = {}
            self.operations.reset_query_progress()
            Thread(
                target=self._run, args=(key, action, body, self.cancel_event), daemon=True
            ).start()
            return key

    def cancel(self, key):
        with self.lock:
            if key not in self.recent:
                raise KeyError(key)
            if key == self.active:
                self.cancel_event.set()
            return {"requested": key == self.active}

    def get(self, key, after=0):
        with self.lock:
            item = self.recent[key]
            progress = self.operations.progress_snapshot() if self.active == key else {}
            if self.active == key and item["action"] in {"ports.query", "ports.refresh"}:
                progress.update(self.operations.query_progress(after))
            return dict(item, progress=progress)

    def history(self):
        with closing(sqlite3.connect(self.db)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM jobs ORDER BY rowid DESC LIMIT 200")
            return [dict(dict(row), files=json.loads(row["files"])) for row in rows]

    def workspace(self):
        with self.lock:
            if self.active:
                return {"busy": True}
            groups = self.operations.inventory_view()
            view = self.port_view
            if not view or not self.operations.credentials:
                return {"groups": groups}
            available = {
                (r.group.target.ip, r.group.target.port, r.interface)
                for r in self.operations.last_results
            }
            rows = [
                dict(
                    row,
                    controlUsed=(row["ip"], int(row["ssh_port"]), row["interface"])
                    not in available,
                )
                for row in view["rows"]
            ]
            return {"groups": groups, "query": {"rows": rows, "files": view.get("files", [])}}

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

    def _run(self, key, action, body, cancel):
        result, error, state = None, "", "done"
        try:
            if action in {
                "ports.query",
                "ports.refresh",
                "targets.apply",
                "session.open",
                "session.close",
                "hostkey.forget",
            }:
                self.controls.invalidate()
            service = self.controls if action.startswith("control.") else self.operations
            result = service.dispatch(action, body, cancel)
            if cancel.is_set():
                state = "cancelled"
        except (ValueError, CollectionFailure) as exc:
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
            if action in PERSIST:
                labels = {"done": "완료", "failed": "실패", "cancelled": "취소됨"}
                summary = labels[state]
                if result:
                    rows = result.get("rows", result.get("logs", result.get("results", [])))
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
            if action in PERSIST:
                with suppress(sqlite3.Error), closing(sqlite3.connect(self.db)) as db, db:
                    db.execute(
                        "UPDATE jobs SET state=?,finished=?,summary=?,files='[]' WHERE id=?",
                        (state, finished, error, key),
                    )
        finally:
            with self.lock:
                if action in {"ports.query", "ports.refresh"}:
                    self.port_view = result if result and "rows" in result else None
                elif action == "control.quick" and result and "rows" in result:
                    self.port_view = result
                elif (
                    action in {"session.open", "session.close", "targets.apply", "hostkey.forget"}
                    and state == "done"
                ):
                    self.port_view = None
                self.recent[key].update(state=state, finished=finished, result=result, error=error)
                self.active = self.cancel_event = None
