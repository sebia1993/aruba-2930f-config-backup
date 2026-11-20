"""A seven-day local record of measured PoE draw and missed observations."""

import math
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

from aruba2930f_backup.models import DeviceTarget
from aruba2930f_backup.port_control import validate_query_interface

RETENTION = timedelta(days=7)
POINT_LIMIT = 12_000


def _stamp(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _watts(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def sampled_rows(result):
    # A control read may include an independently inspected port outside the matrix.
    if "_powerHistoryRows" in result:
        return result.pop("_powerHistoryRows")
    keys = set(result.get("_sampledPorts", ()))
    if not keys:
        return []
    return [
        row
        for row in result.get("rows", ())
        if (row["ip"], row["ssh_port"], row["interface"]) in keys
    ]


def parse_history_request(query):
    fields = parse_qs(query, strict_parsing=True, keep_blank_values=True, max_num_fields=4)
    if fields.keys() != {"ip", "ssh_port", "interface", "hours"}:
        raise ValueError
    ssh_port = fields["ssh_port"][0]
    if not (ssh_port.isascii() and ssh_port.isdigit() and len(ssh_port) <= 5):
        raise ValueError
    target = DeviceTarget(fields["ip"][0], int(ssh_port))
    interface = validate_query_interface(fields["interface"][0])
    if fields["hours"][0] not in {"1", "24", "168"}:
        raise ValueError
    return target.ip, target.port, interface, int(fields["hours"][0])


class PowerHistory:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS samples (
                    ip TEXT NOT NULL, ssh_port INTEGER NOT NULL, interface TEXT NOT NULL,
                    at TEXT NOT NULL, watts REAL, monitor TEXT, slot INTEGER, interval INTEGER,
                    PRIMARY KEY (ip, ssh_port, interface, at)
                ) WITHOUT ROWID"""
            )
            db.execute("CREATE INDEX IF NOT EXISTS samples_age ON samples(at)")
            db.execute("DELETE FROM samples WHERE at < ?", (_stamp(datetime.now(UTC) - RETENTION),))

    def record(self, rows, monitor=None, slot=None, interval=None):
        end = datetime.now(UTC)
        start = end - RETENTION
        values = []
        for row in rows:
            watts = None
            if (
                row.get("complete")
                and row.get("status") == "ok"
                and not row.get("refreshInvalidated")
            ):
                watts = _watts(row.get("watts"))
            observed = row.get("observed_at")
            if not observed or watts is None:
                # A failed attempt has a completion time, but never supplies a wattage.
                watts = None
                observed = row.get("poe_finished_at") or row.get("finished_at")
            try:
                at = datetime.fromisoformat(observed)
            except ValueError, TypeError:
                continue
            if at.tzinfo is None or not start <= at <= end:
                continue
            values.append(
                (
                    row["ip"],
                    row["ssh_port"],
                    row["interface"],
                    _stamp(at),
                    watts,
                    monitor,
                    slot,
                    interval,
                )
            )
        if not values:
            return
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany("INSERT OR IGNORE INTO samples VALUES(?,?,?,?,?,?,?,?)", values)
            db.execute("DELETE FROM samples WHERE at < ?", (_stamp(start),))

    def read(self, ip, ssh_port, interface, hours):
        if hours not in (1, 24, 168):
            raise ValueError
        end = datetime.now(UTC)
        start = _stamp(end - timedelta(hours=hours))
        end_at = _stamp(end)
        key = (ip, ssh_port, interface)
        match = "ip=? AND ssh_port=? AND interface=?"
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("BEGIN")
            rows = db.execute(
                f"""SELECT at,watts,monitor,slot,interval FROM samples
                    WHERE {match} AND at BETWEEN ? AND ? ORDER BY at DESC LIMIT ?""",
                (*key, start, end_at, POINT_LIMIT + 1),
            ).fetchall()
            truncated = len(rows) > POINT_LIMIT
            if truncated:
                rows.pop()
            previous = None
            if rows:
                previous = db.execute(
                    f"""SELECT at,watts FROM samples
                        WHERE {match} AND at>=? AND at<? AND watts IS NOT NULL
                        ORDER BY at DESC LIMIT 1""",
                    (*key, _stamp(end - RETENTION), rows[-1][0]),
                ).fetchone()
        last_valid_at, last_watts = previous or (None, None)
        points = []
        preceding = None
        for at, watts, monitor, slot, interval in reversed(rows):
            connected = False
            if watts is not None and monitor and preceding:
                last_monitor, last_slot, last_at = preceding
                elapsed = (
                    datetime.fromisoformat(at) - datetime.fromisoformat(last_at)
                ).total_seconds()
                connected = (
                    monitor == last_monitor and slot == last_slot + 1 and elapsed <= interval * 1.75
                )
            points.append(
                dict(
                    at=at,
                    watts=watts,
                    connected=connected,
                    previousAt=last_valid_at,
                    previousWatts=last_watts,
                )
            )
            if watts is not None:
                last_valid_at, last_watts = at, watts
            preceding = (monitor, slot, at) if monitor and watts is not None else None
        return dict(start=start, end=end_at, points=points, truncated=truncated)
