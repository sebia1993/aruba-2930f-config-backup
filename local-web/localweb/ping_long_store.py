"""Local incremental history for bounded switch-originated Ping measurements."""

import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta


def sample_category(status, sent, received, gap_count):
    if status == "normal" and sent == received == 1:
        return "success"
    if status in {"no_reply", "unreachable"} and sent == 1 and received == 0:
        return "loss"
    return "gap" if gap_count or status == "not_run" else "unknown"


@contextmanager
def history_connection(path):
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        yield db


def _migrate_routes(db, directory):
    """Commit the destination first. A restart may repeat the copy without duplicates."""
    legacy_path = directory / "web-history.sqlite"
    if not legacy_path.is_file():
        return
    with closing(sqlite3.connect(legacy_path, timeout=2)) as old, old:
        if not old.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ping_routes'"
        ).fetchone():
            return
        old.execute("BEGIN IMMEDIATE")
        # No legacy rows are cleared until the history transaction is durable.
        known = {row[0] for row in db.execute("SELECT id FROM ping_long_runs")}
        for row in old.execute("""SELECT id,endpoint,destination,source_key,observed,purpose,
                                 event_id,failed_at,output FROM ping_routes ORDER BY id"""):
            legacy_id, endpoint, destination, source, observed, purpose, event, failed, output = row
            owner = None
            if event is not None or purpose == "event":
                owner = event.split(":", 1)[0] if isinstance(event, str) else None
                if owner not in known:
                    continue
            db.execute(
                """INSERT INTO ping_routes
                            (legacy_id,run_id,endpoint,destination,source_key,observed,purpose,event_id,failed_at,output)
                          VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(legacy_id) DO NOTHING""",
                (
                    legacy_id,
                    owner,
                    endpoint,
                    destination,
                    source,
                    observed,
                    purpose,
                    event,
                    failed,
                    output,
                ),
            )
        db.commit()
        old.execute("DROP TABLE ping_routes")


class LongHistory:
    def __init__(self, directory):
        self.path = directory / "ping-history.sqlite"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS ping_long_retired (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS ping_long_policy (singleton INTEGER PRIMARY KEY CHECK(singleton=1), days INTEGER NOT NULL, max_mb INTEGER NOT NULL);
                INSERT OR IGNORE INTO ping_long_policy VALUES(1,30,512);
                CREATE TABLE IF NOT EXISTS ping_long_runs (
                    id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                    state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                    fingerprint TEXT NOT NULL, settings TEXT NOT NULL,
                    initial_eta REAL NOT NULL, elapsed REAL NOT NULL DEFAULT 0,
                    paused REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS ping_routes (
                    id INTEGER PRIMARY KEY, legacy_id INTEGER UNIQUE,
                    run_id TEXT REFERENCES ping_long_runs(id) ON DELETE CASCADE,
                    endpoint TEXT NOT NULL, destination TEXT NOT NULL, source_key TEXT NOT NULL,
                    observed TEXT NOT NULL, purpose TEXT NOT NULL, event_id TEXT,
                    failed_at TEXT, output TEXT NOT NULL,
                    CHECK((run_id IS NULL AND event_id IS NULL) OR
                          (run_id IS NOT NULL AND event_id IS NOT NULL))
                );
                CREATE INDEX IF NOT EXISTS ping_route_run ON ping_routes(run_id,id);
                CREATE INDEX IF NOT EXISTS ping_route_match ON ping_routes(endpoint,destination,source_key,id);
                CREATE TABLE IF NOT EXISTS ping_long_series (
                    run_id TEXT NOT NULL, series_id INTEGER NOT NULL, switch_ip TEXT NOT NULL,
                    ssh_port INTEGER NOT NULL, destination_ip TEXT NOT NULL,
                    destination_label TEXT NOT NULL, source_mode TEXT NOT NULL,
                    requested_source TEXT, target_count INTEGER NOT NULL,
                    PRIMARY KEY(run_id,series_id)
                );
                CREATE TABLE IF NOT EXISTS ping_long_blocks (
                    run_id TEXT NOT NULL REFERENCES ping_long_runs(id) ON DELETE CASCADE,
                    series_id INTEGER NOT NULL, reason TEXT NOT NULL,
                    PRIMARY KEY(run_id,series_id)
                );
                CREATE TABLE IF NOT EXISTS ping_long_samples (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, series_id INTEGER NOT NULL,
                    scheduled_at TEXT NOT NULL, started_at TEXT, received_at TEXT, finished_at TEXT,
                    attempt INTEGER, status TEXT NOT NULL, sent INTEGER, received INTEGER,
                    rtt_ms REAL, rtt_evidence TEXT, reason TEXT NOT NULL DEFAULT '',
                    gap_count INTEGER NOT NULL DEFAULT 0, clock_uncertain INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER, verified_source_ip TEXT,
                    UNIQUE(run_id,series_id,attempt)
                );
                CREATE INDEX IF NOT EXISTS ping_long_series_samples
                  ON ping_long_samples(run_id,series_id,id);
                CREATE INDEX IF NOT EXISTS ping_long_times
                  ON ping_long_samples(run_id,scheduled_at);
            """)
            _migrate_routes(db, directory)
            # A committed pre-write intent may already have reached the device.
            # Never replay or convert a missing prompt to a confirmed timeout.
            db.execute("""UPDATE ping_long_samples SET status='unknown', reason='SERVER_RESTART'
                          WHERE finished_at IS NULL AND reason='IN_FLIGHT'""")
            db.execute("""UPDATE ping_long_runs SET state='interrupted',reason='SERVER_RESTART'
                          WHERE finished_at IS NULL AND state IN
                          ('running','paused','pausing','stopping')""")

            self._policy = dict(
                db.execute(
                    "SELECT days,max_mb AS maxMb FROM ping_long_policy WHERE singleton=1"
                ).fetchone()
            )
            self.prune(db)

    def policy(self):
        return dict(self._policy)

    def set_retention(self, days, max_mb):
        if (
            type(days) is not int
            or not 1 <= days <= 365
            or type(max_mb) is not int
            or not 32 <= max_mb <= 2048
        ):
            raise ValueError("보존 기간 1~365일, 용량 32~2048 MiB를 확인하세요.")
        with self.connect() as db:
            db.execute(
                "UPDATE ping_long_policy SET days=?,max_mb=? WHERE singleton=1", (days, max_mb)
            )
        self._policy = {"days": days, "maxMb": max_mb}
        return self.policy()

    @staticmethod
    def delete_completed(db, key):
        row = db.execute("SELECT state FROM ping_long_runs WHERE id=?", (key,)).fetchone()
        if not row or row[0] not in {"done", "cancelled", "failed", "interrupted"}:
            return False
        db.execute("INSERT OR IGNORE INTO ping_long_retired VALUES(?)", (key,))
        db.execute("DELETE FROM ping_long_samples WHERE run_id=?", (key,))
        db.execute("DELETE FROM ping_long_series WHERE run_id=?", (key,))
        db.execute("DELETE FROM ping_long_runs WHERE id=?", (key,))
        return True

    def prune(self, db, *, at=None):
        cutoff = ((at or datetime.now(UTC)) - timedelta(days=self._policy["days"])).isoformat(
            timespec="milliseconds"
        )
        expired = db.execute(
            """SELECT id FROM ping_long_runs WHERE state IN ('done','cancelled','failed','interrupted')
                              AND COALESCE(finished_at,started_at) < ?""",
            (cutoff,),
        ).fetchall()
        for row in expired:
            self.delete_completed(db, row[0])

    def connect(self):
        return history_connection(self.path)

    def insert_sample(
        self,
        db,
        run,
        series,
        scheduled_at,
        *,
        attempt=None,
        started_at=None,
        status="not_run",
        reason="",
        gaps=0,
        finished_at=None,
        uncertain=False,
    ):
        row = db.execute(
            """
            INSERT INTO ping_long_samples
              (run_id,series_id,scheduled_at,started_at,finished_at,attempt,status,reason,
               gap_count,clock_uncertain) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run,
                series.index,
                scheduled_at,
                started_at,
                finished_at,
                attempt,
                status,
                reason,
                gaps,
                int(uncertain),
            ),
        )
        return row.lastrowid
