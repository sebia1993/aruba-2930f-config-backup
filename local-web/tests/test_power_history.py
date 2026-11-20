"""Only real, separately measured port observations become local power history."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from localweb.power_history import PowerHistory, parse_history_request, sampled_rows


class PowerHistoryTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "power.sqlite"
        self.store = PowerHistory(self.path)
        self.base = datetime.now(UTC) - timedelta(minutes=30)
        self.key = ("192.0.2.3", 2222, "1/1")

    def row(self, seconds, watts, **changes):
        instant = (self.base + timedelta(seconds=seconds)).isoformat()
        ip, ssh_port, interface = self.key
        return dict(
            dict(
                ip=ip,
                ssh_port=ssh_port,
                interface=interface,
                status="ok",
                complete=True,
                observed_at=instant,
                poe_finished_at=instant,
                watts=watts,
            ),
            **changes,
        )

    def test_gap_zero_previous_range_exact_endpoint_and_restart(self):
        before = self.row(-3600, 2)
        self.store.record([before])
        for slot, seconds, value in [
            (0, 0, 0),
            (1, 59, 4.5),
            (2, 120, None),
            (3, 179, 9),
            (5, 240, 6),
            (6, 541, 6),
        ]:
            self.store.record([self.row(seconds, value)], "first", slot, 60)
        manual = self.row(542, 9)
        self.store.record(
            [
                manual,
                manual,
                self.row(542, 99, ssh_port=22),
                self.row(542, 89, interface="1"),
                self.row(542, 98, ip="192.0.2.4"),
            ]
        )
        self.store.record([self.row(600, 7)], "first", 7, 60)
        self.store.record([self.row(660, 8)], "second", 0, 60)
        reopened = PowerHistory(self.path)
        points = reopened.read(*self.key, 1)["points"]
        self.assertEqual([p["watts"] for p in points], [0, 4.5, None, 9, 6, 6, 9, 7, 8])
        self.assertEqual([p["previousWatts"] for p in points], [2, 0, 4.5, 4.5, 9, 6, 6, 9, 7])
        self.assertEqual(points[0]["previousAt"], before["observed_at"])
        self.assertEqual([p["connected"] for p in points], [False, True] + [False] * 7)

    def test_invalidated_missing_nonfinite_and_incomplete_are_gaps(self):
        self.store.record(
            [
                self.row(0, 4, refreshInvalidated=True, lastSuccessfulObservation={"watts": 10}),
                self.row(1, float("nan")),
                self.row(2, float("inf")),
                self.row(3, -1),
                self.row(4, 5, status="failed"),
                self.row(5, 9, complete=False),
                self.row(6, 4, observed_at="", requested_watts=4),
                self.row(7, 8, observed_at="not a timestamp"),
            ]
        )
        points = self.store.read(*self.key, 1)["points"]
        self.assertEqual([p["watts"] for p in points], [None] * 7)
        self.assertFalse(any(p["connected"] for p in points))
        self.assertEqual(sampled_rows({"rows": [self.row(8, 1)]}), [])

    def test_prune_old_and_bound_latest_with_indexed_range(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT INTO samples VALUES(?,?,?,?,?,?,?,?)",
                (*self.key, (self.base - timedelta(days=8)).isoformat(), 7, None, None, None),
            )
            query = db.execute(
                "EXPLAIN QUERY PLAN SELECT at,watts FROM samples WHERE ip=? "
                "AND ssh_port=? AND interface=? AND at BETWEEN ? AND ? ORDER BY at DESC LIMIT ?",
                (*self.key, "a", "b", 5),
            ).fetchall()
            self.assertIn("PRIMARY KEY", str(query))
        self.store.record([self.row(i, i) for i in range(5)] + [self.row(-8 * 86400, 9)])
        with patch("localweb.power_history.POINT_LIMIT", 2):
            view = self.store.read(*self.key, 168)
        self.assertTrue(view["truncated"])
        self.assertEqual([p["watts"] for p in view["points"]], [3, 4])
        self.assertEqual(view["points"][0]["previousWatts"], 2)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 5)

    def test_request_single_valid_endpoint_and_range(self):
        valid = "ip=192.0.2.3&ssh_port=2222&interface=1%2F1&hours=168"
        self.assertEqual(parse_history_request(valid), (*self.key, 168))
        for invalid in [
            "",
            valid + "&hours=1",
            valid.replace("168", "2"),
            valid.replace("2222", "0"),
            valid.replace("192.0.2.3", "example"),
            valid.replace("1%2F1", "1-8"),
        ]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_history_request(invalid)
