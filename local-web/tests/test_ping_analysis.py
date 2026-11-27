"""Offline analysis over the same SQLite tables used by the long Ping scheduler."""

import base64
import csv
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from localweb.ping_analysis import _export_book, _sheet, analysis_request, export_history
from localweb.ping_long_store import LongHistory
from openpyxl import Workbook, load_workbook

RUN = "c" * 32
BASE = datetime(2026, 1, 1, tzinfo=UTC)
DEST = "198.51.100.2"


def stamp(second):
    return (BASE + timedelta(seconds=second)).isoformat(timespec="milliseconds")


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.history = LongHistory(Path(folder.name))
        with self.history.connect() as db:
            db.execute(
                """INSERT INTO ping_long_runs(id,started_at,state,fingerprint,settings,initial_eta)
                       VALUES(?,?,'done','fixture','{}',0)""",
                (RUN, stamp(0)),
            )
            for n, ip, dest in [
                (1, "192.0.2.11", DEST),
                (2, "192.0.2.12", DEST),
                (3, "192.0.2.13", DEST),
                (4, "192.0.2.11", "198.51.100.3"),
                (5, "192.0.2.11", "198.51.100.4"),
            ]:
                db.execute(
                    """INSERT INTO ping_long_series VALUES(?,?,?,?,?,?,?,?,?)""",
                    (RUN, n, ip, 22, dest, "=label", "ssh_ip", ip, 100),
                )

    def sample(self, sid, attempt, start, end, status, *, rtt=None, gaps=0, uncertain=False):
        with self.history.connect() as db:
            sent = 1 if status in ("normal", "no_reply") else None
            received = {"normal": 1, "no_reply": 0}.get(status)
            db.execute(
                """INSERT INTO ping_long_samples(run_id,series_id,attempt,scheduled_at,started_at,
                received_at,finished_at,status,sent,received,rtt_ms,gap_count,clock_uncertain)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    RUN,
                    sid,
                    attempt,
                    stamp(start),
                    stamp(start) if attempt else None,
                    stamp(end) if attempt else None,
                    stamp(end),
                    status,
                    sent,
                    received,
                    rtt,
                    gaps,
                    uncertain,
                ),
            )

    def api(self, action, **payload):
        return analysis_request(self.history.path, action, {"run": RUN, **payload})

    def test_excel_inventory_history_maps_shared_ips_to_the_correct_series(self):
        first = dict(hostname="SW-A", swIp="192.0.2.11", gwIp=DEST, bbIp=DEST, nmsIp="")
        second = dict(hostname="SW-B", swIp="192.0.2.12", gwIp=DEST, bbIp="", nmsIp="")
        settings = dict(inventoryRows=[first, second], inventoryRoles=["gwIp", "bbIp", "nmsIp"])
        with self.history.connect() as db:
            db.execute(
                "UPDATE ping_long_runs SET settings=? WHERE id=?", (json.dumps(settings), RUN)
            )
        self.sample(1, 1, 0, 1, "normal", rtt=2)
        self.sample(2, None, 0, 1, "ssh_failure", gaps=1)
        with export_history(self.history.path, RUN, "xlsx") as file:
            book = load_workbook(file)
            try:
                sheet = book["장비별 결과"]
                self.assertEqual(book.sheetnames[0], sheet.title)
                self.assertEqual(sheet["F2"].value, sheet["G2"].value)
                self.assertIn("평균 2.0 ms", sheet["F2"].value)
                self.assertIn("SSH 실패 1", sheet["F3"].value)
                self.assertEqual(sheet["H3"].value, "대상 없음")
            finally:
                book.close()

    def test_gap_interrupts_event_preserves_open_ends_and_loss_denominator(self):
        self.sample(1, 1, 0, 1, "no_reply")
        self.sample(1, 2, 2, 3, "no_reply")
        self.sample(1, None, 4, 8, "not_run", gaps=3)
        self.sample(1, 3, 9, 10, "no_reply")
        self.sample(1, 4, 11, 12, "normal", rtt=1)
        self.sample(1, 5, 13, 14, "normal", rtt=9)
        self.sample(1, 6, 15, 16, "unreachable")
        self.sample(1, 7, 17, 18, "no_reply")
        d = self.api("report", series=1)
        self.assertEqual(
            [e["state"] for e in d["events"]], ["interrupted", "observed_recovery", "open"]
        )
        self.assertIsNone(d["events"][0]["lastGood"])
        self.assertEqual(d["events"][0]["count"], 2)
        self.assertIsNone(d["events"][1]["lastGood"])
        self.assertEqual(d["metrics"]["lossRate"], 66.67)
        self.assertEqual(d["metrics"]["coverage"], 60)
        self.assertEqual(d["metrics"]["rttCount"], 2)
        self.assertEqual(d["metrics"]["p99"], 9)
        filtered = self.api("report", series=1, start=stamp(11), end=stamp(14))
        self.assertEqual(filtered["sampleCount"], 2)
        self.assertEqual(filtered["metrics"]["lossRate"], 0)

    def scheduler_gap(self, start, end, count, interval, reason):
        with self.history.connect() as db:
            db.execute(
                "UPDATE ping_long_runs SET settings=? WHERE id=?",
                (json.dumps({"interval": interval}), RUN),
            )
            db.execute(
                """INSERT INTO ping_long_samples(run_id,series_id,scheduled_at,finished_at,
                    status,reason,gap_count) VALUES(?,1,?,?,'not_run',?,?)""",
                (RUN, stamp(start), stamp(end), reason, count),
            )

    def test_report_counts_only_nominal_gap_slots_in_inclusive_window(self):
        self.scheduler_gap(5, 103, 20, 5, "USER_PAUSED")
        self.sample(1, 1, 104, 107, "normal", rtt=2)
        self.sample(1, None, 108, 112, "ssh_failure", gaps=1)
        inside = self.api("report", series=1, start=stamp(10), end=stamp(90))
        self.assertEqual(inside["metrics"]["gap"], 17)
        self.assertEqual(inside["sampleCount"], 1)
        self.assertEqual(inside["samples"][0]["gap_count"], 17)
        self.assertEqual(
            [point["utc"] for point in inside["points"] if point["gap"]],
            [stamp(t) for t in range(10, 91, 5)],
        )
        for start, end, gap, success in (
            (0, 5, 1, 0),
            (100, 100, 1, 0),
            (100.001, 103, 0, 0),
            (95, 107, 2, 1),
            (104, 106, 0, 0),
            (108, 111, 0, 0),
            (112, 112, 1, 0),
        ):
            with self.subTest(start=start, end=end):
                view = self.api("report", series=1, start=stamp(start), end=stamp(end))
                self.assertEqual(
                    (view["metrics"]["gap"], view["metrics"]["success"]), (gap, success)
                )
        complete = self.api("report", series=1)
        self.assertEqual((complete["metrics"]["gap"], complete["sampleCount"]), (21, 3))
        self.assertEqual(complete["metrics"]["rttCount"], 1)
        self.assertEqual(self.api("report", series=1, end=stamp(10))["metrics"]["gap"], 2)
        self.assertEqual(self.api("report", series=1, start=stamp(95))["metrics"]["gap"], 3)

    def test_heat_apportions_scheduler_delay_to_half_open_buckets(self):
        self.scheduler_gap(5, 107, 20, 5, "SCHEDULE_DELAY")
        for start, width, expected in (
            (0, 5, [0] + [1] * 11),
            (10, 10, [2] * 9 + [1, 0, 0]),
            (10, 60, [12, 7] + [0] * 10),
        ):
            with self.subTest(width=width):
                heat = self.api("heat", destination=DEST, bucket=width, start=stamp(start))
                device = next(row for row in heat["rows"] if row["device"] == "192.0.2.11:22")
                self.assertEqual([cell["gap"] for cell in device["cells"]], expected)
                self.assertTrue(
                    all(cell["loss"] == cell["success"] == 0 for cell in device["cells"])
                )
                self.assertTrue(all(not cell["devices"] for cell in heat["overlap"]))

    def test_large_gap_is_one_history_record_with_bounded_proportional_chart(self):
        self.scheduler_gap(0, 10000, 10000, 1, "SCHEDULE_DELAY")
        report = self.api("report", series=1)
        self.assertEqual((report["sampleCount"], report["metrics"]["gap"]), (1, 10000))
        points = report["points"]
        self.assertGreater(len(points), 1)
        self.assertLessEqual(len(points), 120)
        counts = [point["gap"] for point in points]
        self.assertEqual(sum(counts), 10000)
        self.assertEqual(points[0]["utc"], stamp(0))
        boundaries = [datetime.fromisoformat(point["utc"]) for point in points]
        boundaries.append(BASE + timedelta(seconds=10000))
        for left, right, count in zip(boundaries[:-1], boundaries[1:], counts, strict=True):
            self.assertEqual((right - left).total_seconds(), count)

    def test_time_window_overlap_not_only_same_bucket_and_healthy_counterevidence(self):
        self.sample(1, 1, 1, 2, "no_reply")
        self.sample(2, 1, 3, 4, "no_reply")
        d = self.api("heat", destination=DEST, bucket=60, start=stamp(0))
        self.assertEqual(d["overlap"][0]["failedCount"], 2)
        self.assertFalse(d["overlap"][0]["devices"])
        self.sample(1, 2, 3, 5, "no_reply")
        self.sample(3, 1, 3, 4, "normal", rtt=1)
        self.sample(2, 2, 61, 63, "no_reply", uncertain=True)
        self.sample(1, 3, 62, 63, "no_reply")
        d = self.api("heat", destination=DEST, bucket=60, start=stamp(0))
        self.assertEqual(d["overlap"][0]["devices"], ["192.0.2.11:22", "192.0.2.12:22"])
        self.assertEqual(d["overlap"][0]["counter"], ["192.0.2.13:22"])
        self.assertFalse(d["overlap"][1]["devices"])

    def test_nms_csv_review_bindings_and_reimport(self):
        text = (
            "switch,event,opened,closed\r\n"
            "192.0.2.11,=장애,2026-01-01 09:00:00,2026-01-01 09:00:05\r\n"
            "192.0.2.11,=장애,2026-01-01 09:00:00,2026-01-01 09:00:05\r\n"
            "192.0.2.12,open,2026-01-01 09:00:10,\r\n"
            "192.0.2.77,open,2026-01-01 09:00:11,\r\n"
            "192.0.2.11,backward,2026-01-01 09:01:11,2026-01-01 09:00:00"
        )
        body = dict(
            name="알람.csv",
            file=base64.b64encode(text.encode("cp949")).decode(),
            encoding="cp949",
            zone="KST",
            polling=30,
            columns=dict(device=0, message=1, opened=2, closed=3),
        )
        preview = self.api("nms-file", **body)
        self.assertEqual((preview["valid"], preview["invalid"], preview["duplicates"]), (2, 2, 1))
        self.assertEqual(preview["preview"][0]["opened"], stamp(0))
        info = dict(digest=preview["digest"], previewKey=preview["previewKey"], confirm=True)
        with self.assertRaises(ValueError):
            self.api("nms-commit", **{**body, "zone": "UTC"}, **info)
        self.assertEqual(self.api("nms-commit", **body, **info)["inserted"], 2)
        self.assertEqual(self.api("nms-commit", **body, **info)["inserted"], 0)
        self.sample(1, 1, 1, 3, "no_reply")
        self.sample(1, 2, 10, 13, "no_reply")
        self.sample(1, 3, 4, 5, "normal", rtt=1)
        comp = self.api("nms-compare", destination=DEST, tolerance=10)["rows"]
        self.assertIsNone(comp[0]["closed"])
        self.assertEqual(
            [comp[1][k] for k in ("strictLoss", "strictSuccess", "nearLoss")], [1, 1, 1]
        )
        with export_history(self.history.path, RUN, "xlsx") as file:
            wb = load_workbook(file, read_only=True)
            try:
                rows = list(wb["NMS 이력"].values)
                message_col = rows[0].index("message")
                self.assertIn("'=장애", [r[message_col] for r in rows])
            finally:
                wb.close()

    def test_excel_files_are_rejected_for_preview_and_import(self):
        book = Workbook()
        book.active.append(["device", "message", "opened"])
        book.active.append(["192.0.2.11", "alarm", stamp(0)])
        data = io.BytesIO()
        book.save(data)
        book.close()
        csv_data = b"switch,event,time\n192.0.2.11,alarm,2026-01-01T00:00:00Z"
        for name, raw in (
            ("alarms.xlsx", data.getvalue()),
            ("alarms.XLS", csv_data),
            ("alarms.xlsm", csv_data),
            ("renamed.csv", data.getvalue()),
            ("legacy.csv", bytes.fromhex("d0cf11e0a1b11ae1") + csv_data),
        ):
            for action in ("nms-file", "nms-commit"):
                with (
                    self.subTest(name=name, action=action),
                    self.assertRaisesRegex(ValueError, "CSV"),
                ):
                    self.api(action, name=name, file=base64.b64encode(raw).decode())
        with self.history.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ping_nms_events").fetchone()[0], 0)

    def test_historical_workbook_alarm_remains_queryable_and_exportable(self):
        self.api("nms-compare", destination=DEST)
        with self.history.connect() as db:
            db.execute(
                """INSERT INTO ping_nms_events(
                    run_id, dedup, device, message, opened, closed, file_name, sheet, row_number)
                    VALUES(?, 'prior', '192.0.2.11:22', '이전 알람', ?, ?, '이전.xlsx', '장애', 4)""",
                (RUN, stamp(0), stamp(10)),
            )
        self.sample(1, 1, 2, 3, "no_reply")
        row = self.api("nms-compare", destination=DEST)["rows"][0]
        self.assertEqual((row["message"], row["strictLoss"]), ("이전 알람", 1))
        with export_history(self.history.path, RUN, "xlsx") as path:
            book = load_workbook(path, read_only=True)
            try:
                header, values = list(book["NMS 이력"].values)
                saved = dict(zip(header, values, strict=True))
                self.assertEqual((saved["file_name"], saved["sheet"]), ("이전.xlsx", "장애"))
            finally:
                book.close()

    def test_csv_follows_run_uses_formula_safe_label_and_null_rtt(self):
        self.sample(1, 1, 0, 1, "no_reply")
        with (
            export_history(self.history.path, RUN, "csv") as file,
            file.open(encoding="utf-8-sig", newline="") as source,
        ):
            row = next(csv.DictReader(source))
            self.assertEqual(row["rtt_ms"], "")
            self.assertEqual(row["destination_label"], "'=label")
            self.assertTrue(row["observed_kst"].endswith("+09:00"))
            self.assertEqual(row["started_at"], stamp(0))
            self.assertEqual(row["started_at_kst"], "2026-01-01T09:00:00.000+09:00")
            self.assertEqual(row["received_at_kst"], "2026-01-01T09:00:01.000+09:00")
        self.assertFalse(file.exists())

    def test_path_candidates_need_validated_baseline_and_consistent_source(self):
        self.sample(1, 1, 1, 2, "no_reply")
        self.sample(4, 1, 2, 3, "normal")
        self.sample(5, 1, 4, 5, "normal")
        body = dict(series=[1, 4, 5], start=stamp(0), seconds=30)
        self.assertIsNone(self.api("path", **body)["candidate"])
        self.assertIsNone(self.api("path", **body, baselineVerified=True)["candidate"])
        with self.history.connect() as db:
            db.execute(
                "UPDATE ping_long_samples SET verified_source_ip='192.0.2.11' WHERE run_id=?",
                (RUN,),
            )
        self.assertIn("ICMP", self.api("path", **body, baselineVerified=True)["candidate"])
        with self.assertRaises(ValueError):
            self.api("path", **{**body, "series": [2, 4, 5]})

    def test_split_alarm_rows_require_manual_pairing_for_multiple_candidates(self):
        text = (
            "ip,msg,time,state,kind\n"
            "192.0.2.11,a,2026-01-01T00:00:00Z,ON,poll\n"
            "192.0.2.11,b,2026-01-01T00:00:02Z,ON,poll\n"
            "192.0.2.11,c,2026-01-01T00:00:05Z,OFF,poll\n"
            "192.0.2.12,d,2026-01-01T00:00:05Z,ON,poll"
        )
        body = dict(
            name="separate.csv",
            file=base64.b64encode(text.encode()).decode(),
            columns=dict(device=0, message=1, opened=2, closed=None),
            zone="embedded",
            split=dict(state=3, category=4, occurs="ON", recovers="OFF", pairs={}),
        )
        preview = self.api("nms-file", **body)
        self.assertEqual(preview["errors"][0]["candidates"], [2, 3])
        self.assertEqual(preview["valid"], 1)
        body["split"]["pairs"] = {"4": 2}
        checked = self.api("nms-file", **body)
        self.assertEqual(checked["valid"], 3)
        result = self.api(
            "nms-commit",
            **body,
            digest=checked["digest"],
            previewKey=checked["previewKey"],
            confirm=True,
        )
        self.assertEqual(result["inserted"], 3)
        rows = self.api("nms-compare", destination=DEST)["rows"]
        matched = next(r for r in rows if r["message"] == "a")
        self.assertEqual(
            (matched["recovery_row"], matched["recovery_message"], matched["closed"]),
            (4, "c", stamp(5)),
        )
        self.assertIsNone(next(r for r in rows if r["message"] == "b")["closed"])

    def test_saved_groups_and_safe_terminal_only_deletion(self):
        import threading
        from types import SimpleNamespace

        groups = {"192.0.2.11:22": "A", "192.0.2.12:22": "A", "192.0.2.13:22": "B"}
        self.assertEqual(self.api("groups", groups=groups)["groups"], groups)
        self.sample(1, 1, 1, 8, "no_reply")
        self.sample(2, 1, 1, 8, "no_reply")
        self.sample(3, 1, 1, 8, "normal")
        overlap = self.api("heat", start=stamp(0), bucket=60, destination=DEST)["overlap"][0]
        self.assertEqual((overlap["failureGroups"], overlap["healthyGroups"]), (["A"], ["B"]))
        jobs = SimpleNamespace(
            lock=threading.RLock(),
            long_ping=SimpleNamespace(history=self.history, run=SimpleNamespace(key=RUN)),
        )
        with self.history.connect() as db:
            db.execute("UPDATE ping_long_runs SET state='paused' WHERE id=?", (RUN,))
        with self.assertRaises(ValueError):
            analysis_request(self.history.path, "delete", dict(run=RUN, confirm=True), jobs=jobs)
        with self.history.connect() as db:
            db.execute("UPDATE ping_long_runs SET state='done' WHERE id=?", (RUN,))
        with self.assertRaises(ValueError):
            analysis_request(self.history.path, "delete", dict(run=RUN), jobs=jobs)
        analysis_request(self.history.path, "delete", dict(run=RUN, confirm=True), jobs=jobs)
        self.assertIsNone(jobs.long_ping.run)
        with self.history.connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM ping_analysis_groups").fetchone()[0], 0
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ping_long_samples").fetchone()[0], 0)

    def test_concurrent_approved_imports_report_exact_insert_and_duplicate_counts(self):
        lines = ["ip,message,time"] + [
            f"192.0.2.11,alarm-{i},2026-01-01T00:00:00Z" for i in range(200)
        ]
        body = dict(
            name="events.csv",
            file=base64.b64encode("\n".join(lines).encode()).decode(),
            zone="embedded",
            columns=dict(device=0, message=1, opened=2, closed=None),
        )
        ready = self.api("nms-file", **body)
        barrier = Barrier(2)

        def commit():
            barrier.wait(timeout=10)
            return self.api(
                "nms-commit",
                **body,
                confirm=True,
                digest=ready["digest"],
                previewKey=ready["previewKey"],
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            reader1, reader2 = pool.submit(commit), pool.submit(commit)
            results = [reader1.result(10), reader2.result(10)]
        self.assertEqual(sum(r["inserted"] for r in results), 200)
        self.assertEqual(sum(r["duplicates"] for r in results), 200)
        self.assertTrue(all(r["inserted"] + r["duplicates"] == 200 for r in results))

    def test_malformed_input_yields_validation_errors_and_never_persists_records(self):
        cases = [
            dict(name="bad.xlsx", file="not-base64!"),
            dict(name="bad.xlsx", file=base64.b64encode(b"incomplete archive").decode()),
            dict(name="ctrl\x01.csv", file=""),
            dict(name="bad.csv", file=base64.b64encode(b"a\nb").decode(), sheet={"unexpected": 0}),
            dict(name="bad.csv", file=base64.b64encode(b'a,b\n"unfinished').decode()),
            dict(name="big.csv", file="A" * (4 * 1024**2 + 1)),
        ]
        for case in cases:
            with self.subTest(name=case["name"]), self.assertRaises(ValueError):
                self.api("nms-file", **case)
        with self.assertRaises(ValueError):
            analysis_request(self.history.path, "report", [])
        with self.assertRaises(ValueError):
            self.api("heat", destination=DEST)
        with self.history.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ping_nms_events").fetchone()[0], 0)

    def test_export_splits_sheet_repeats_header_and_cleans_spool_on_failure(self):
        path = self.history.path.with_suffix(".xlsx")
        with patch("localweb.ping_analysis._EXCEL_DATA_ROWS", 3), _export_book() as book:
            _sheet(book, "data", iter([["value"], [None], [1], [2], [3], [4]]))
            book.save(path)
        reopened = load_workbook(path, read_only=True)
        try:
            self.assertEqual(list(reopened["data"].values), [("value",), (), (1,), (2,)])
            self.assertEqual(list(reopened["data 2"].values), [("value",), (3,), (4,)])
        finally:
            reopened.close()
        with self.assertRaises(OSError), _export_book() as book:
            ws = book.create_sheet("partial")
            ws.append(["private"])
            spool = Path(ws._writer.out)
            self.assertTrue(spool.exists())
            raise OSError("synthetic disk write failure")
        self.assertFalse(spool.exists())

    def test_no_measurements_have_null_ratios_and_no_pretend_samples(self):
        self.assertEqual(self.api("runs")["runs"][0]["id"], RUN)
        self.assertEqual(len(self.api("series")["series"]), 5)
        report = self.api("report", series=1)
        self.assertEqual(report["samples"], [])
        self.assertIsNone(report["metrics"]["lossRate"])
        self.assertIsNone(report["metrics"]["coverage"])
