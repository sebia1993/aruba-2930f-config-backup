import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from localweb.ping_analysis import _path_export
from localweb.ping_files import general_excel, import_profile, profile_request
from localweb.ping_long_store import LongHistory
from localweb.ping_path import RouteSession, parse_hops, recent_routes, run_route
from localweb.ping_tools import PingPlan
from openpyxl import load_workbook

PLAN = {"targets": "192.0.2.7", "destinations": [{"ip": "198.51.100.8", "label": "=SUM(1,2)"}]}
HOPS = "traceroute to 198.51.100.8\n 1 *\n 2 198.51.100.8 1500 us"


class PingFileTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.history = LongHistory(self.root)
        self.jobs = SimpleNamespace(
            directory=self.root,
            db=self.root / "web-history.sqlite",
            long_ping=SimpleNamespace(history=self.history),
        )

    def test_profile_roundtrip_and_no_overwrite(self):
        def action(op, **data):
            return profile_request(self.jobs, op, data)

        action("save", name="지점", plan=PLAN)
        with self.assertRaises(ValueError):
            action("save", name="지점", plan=PLAN)
        self.assertEqual(action("list")["names"], ["지점"])
        raw = json.dumps(action("portable", plan=PLAN))
        self.assertEqual(action("import", text=raw), action("load", name="지점"))
        with self.assertRaises(ValueError):
            action("save", name="invalid", plan=dict(PLAN, password=None))
        action("delete", name="지점")
        self.assertEqual(action("list")["names"], [])

    def test_destination_profile_roundtrip_without_switch(self):
        destinations = {"scope": "destinations", "destinations": PLAN["destinations"]}
        profile_request(self.jobs, "save", {"name": "NMS", **destinations})
        selected = profile_request(self.jobs, "load", {"name": "NMS", "scope": "destinations"})
        self.assertEqual(selected, {"destinations": PLAN["destinations"]})
        portable = profile_request(self.jobs, "portable", destinations)
        self.assertEqual(import_profile(json.dumps(portable), self.root), selected)
        self.assertEqual(profile_request(self.jobs, "list", {})["names"], [])

    def test_imported_port_profile_preserves_only_unique_endpoints(self):
        from aruba2930f_backup.poe_targets import _PROFILE_FORMAT

        targets = [
            {"ip": "192.0.2.12", "ssh_port": 2200, "label": "", "interfaces": [s]}
            for s in ("1", "2")
        ]
        raw = json.dumps({"format": _PROFILE_FORMAT, "version": 1, "targets": targets})
        self.assertEqual(import_profile(raw, self.root), {"targets": "192.0.2.12:2200"})

    def test_excel_uses_server_rows_and_keeps_formula_text_and_nulls(self):
        row = {
            "index": 1,
            "destination_ip": "198.51.100.8",
            "destination_label": "=SUM(1,2)",
            "status": "unknown",
            "rtt_avg_ms": None,
            "ssh_port": 22,
        }
        job = {
            "state": "done",
            "started": "UTC",
            "finished": "UTC",
            "result": {
                "total": 2,
                "completed": 2,
                "rows": [dict(row, index=0), row],
                "options": {"timeout": 2},
            },
        }
        self.jobs.ping_status = lambda key: job
        report = load_workbook(BytesIO(general_excel(self.jobs, "0" * 32, {"indices": [1]})))
        self.addCleanup(report.close)
        sheet = report["상세 결과"]
        self.assertEqual(sheet.max_row, 2)
        self.assertEqual(sheet["F2"].data_type, "s")
        self.assertEqual(sheet["F2"].value, "=SUM(1,2)")
        self.assertEqual(sheet["C2"].value, 22)
        self.assertIsNone(sheet["N2"].value)
        with self.assertRaises(ValueError):
            general_excel(self.jobs, "0" * 32, {"indices": [True]})

    def test_route_forces_limited_command_and_verifies_source_before_write(self):
        session = MagicMock(spec=RouteSession)
        session._identify.return_value = SimpleNamespace(
            model="2930F", software_version="WC.16.11.0001"
        )
        session._require_prepared.return_value = "switch#"
        conn = session._require_connection.return_value
        conn.normalize_cmd.side_effect = lambda command: command + "\n"
        plan = PingPlan.parse(PLAN)
        with (
            patch("localweb.ping_path.parse_owned_ipv4", return_value=()),
            self.assertRaises(ValueError),
        ):
            RouteSession.observe(session, plan, None)
        conn.write_channel.assert_not_called()
        with (
            patch("localweb.ping_path.parse_owned_ipv4", return_value=((1, "192.0.2.7"),)),
            patch("localweb.ping_path._strip_command_and_prompt", return_value=HOPS),
        ):
            result = RouteSession.observe(session, plan, None)
        conn.write_channel.assert_called_once_with(
            "traceroute 198.51.100.8 maxttl 12 probes 1 source 192.0.2.7 timeout 1\n"
        )
        self.assertEqual((result["state"], result["hops"][1]["rttMs"]), ("reached", 1.5))
        self.assertIsNone(result["hops"][0]["ip"])
        for output in ("1 198.51.100.8 100", "4 *", "1 *\n1 *", HOPS + "\nbroken"):
            self.assertEqual(parse_hops(output, "198.51.100.8"), ([], "unknown"))

    def test_baseline_recovery_and_comparison_are_scoped_to_source(self):
        op = MagicMock(data_dir=self.root)
        op.data_dir = self.root
        session = op._prepared_diagnostic_session.return_value.__enter__.return_value

        def sample(ip):
            return {
                "hops": [{"ttl": 1, "ip": ip, "rttMs": 1}],
                "state": "reached",
                "sourceIp": "192.0.2.7",
            }

        session.observe.side_effect = [sample("192.0.2.8"), sample("192.0.2.9")]
        run_route(op, {"plan": PLAN, "purpose": "baseline"}, None)
        second = run_route(op, {"plan": PLAN, "purpose": "recovered"}, None)
        self.assertTrue(second["baselineAt"])
        self.assertEqual(second["changedTtls"], [1])
        with self.history.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ping_routes").fetchone()[0], 2)

    def test_failed_baseline_is_recorded_without_erasing_previous_baseline(self):
        from aruba2930f_backup.models import CollectionFailure, ErrorCode

        op = MagicMock()
        op.data_dir = self.root
        session = op._prepared_diagnostic_session.return_value.__enter__.return_value
        reached = {
            "hops": [{"ttl": 1, "ip": "198.51.100.8", "rttMs": 2}],
            "state": "reached",
            "sourceIp": "192.0.2.7",
        }
        session.observe.side_effect = [
            dict(reached),
            CollectionFailure(ErrorCode.MODEL_UNSUPPORTED, "미지원"),
            dict(reached),
        ]
        first = run_route(op, {"plan": PLAN, "purpose": "baseline"}, None)
        failed = run_route(op, {"plan": PLAN, "purpose": "baseline"}, None)
        last = run_route(op, {"plan": PLAN, "purpose": "incident"}, None)
        self.assertEqual(
            (failed["state"], failed["purpose"]), ("unsupported", "baseline_unconfirmed")
        )
        self.assertEqual(last["baselineAt"], first["observedAt"])

    def _terminal_run(self, key, when=None):
        at = when or datetime.now(UTC).isoformat(timespec="milliseconds")
        with self.history.connect() as db:
            db.execute(
                "INSERT INTO ping_long_runs(id,started_at,finished_at,state,fingerprint,settings,initial_eta) VALUES(?,?,?,'done','fixture','{}',1)",
                (key, at, at),
            )

    def _fake_route(self):
        op = MagicMock()
        op.data_dir = self.root
        session = op._prepared_diagnostic_session.return_value.__enter__.return_value
        session.observe.return_value = {
            "hops": [{"ttl": 1, "ip": "198.51.100.8", "rttMs": 2}],
            "state": "reached",
            "sourceIp": "192.0.2.7",
        }
        return op

    def _event_route(self, key):
        return {
            "purpose": "event",
            "plan": PLAN,
            "eventId": key + ":0:1",
            "failedAt": datetime.now(UTC).isoformat(timespec="milliseconds"),
        }

    def test_auto_routes_share_delete_and_retention_but_manual_baseline_remains(self):
        now = "b" * 32
        old = "c" * 32
        self._terminal_run(now)
        self._terminal_run(old, "2000-01-01T00:00:00.000+00:00")
        # Use distinct result dicts as each real SSH operation does.
        run_route(self._fake_route(), {"purpose": "baseline", "plan": PLAN}, None)
        for key in (now, old):
            run_route(self._fake_route(), self._event_route(key), None)
        with self.history.connect() as snapshot:
            snapshot.execute("BEGIN")
            self.assertEqual(len(list(_path_export(snapshot, now))), 2)
            with self.history.connect() as db:
                self.assertTrue(self.history.delete_completed(db, now))
                self.history.prune(db)
            # The export transaction that started before deletion remains consistent.
            self.assertEqual(len(list(_path_export(snapshot, now))), 2)
        with self.history.connect() as db:
            self.assertEqual(len(list(_path_export(db, now))), 1)
            self.assertEqual(len(list(_path_export(db, old))), 1)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual([r["purpose"] for r in recent_routes(self.jobs)["items"]], ["baseline"])

    def test_trace_finishing_after_run_deletion_does_not_restore_data(self):
        key = "d" * 32
        self._terminal_run(key)
        entered, release = threading.Event(), threading.Event()
        op = self._fake_route()
        session = op._prepared_diagnostic_session.return_value.__enter__.return_value

        def observe(plan, cancel):
            entered.set()
            release.wait(5)
            return {"state": "partial", "sourceIp": None, "hops": []}

        session.observe.side_effect = observe
        result = []
        worker = threading.Thread(
            target=lambda: result.append(run_route(op, self._event_route(key), None))
        )
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.history.connect() as db:
                self.assertTrue(self.history.delete_completed(db, key))
        finally:
            release.set()
            worker.join(3)
        self.assertEqual(result, [{"state": "discarded", "reason": "RUN_DELETED", "hops": []}])
        self.assertEqual(recent_routes(self.jobs)["items"], [])
        self.assertEqual(run_route(op, self._event_route(key), None), result[0])
        self.assertEqual(session.observe.call_count, 1)

    def test_legacy_route_transfer_is_replay_safe_and_omits_already_deleted_runs(self):
        key = "a" * 32
        self._terminal_run(key)
        with closing(sqlite3.connect(self.jobs.db)) as old, old:
            old.execute(
                "CREATE TABLE ping_routes(id INTEGER PRIMARY KEY,endpoint,destination,source_key,observed,purpose,event_id,failed_at,output)"
            )
            for num, purpose, event in [
                (1, "baseline", None),
                (2, "event", key + ":0:1"),
                (3, "event", "f" * 32 + ":0:1"),
            ]:
                old.execute(
                    "INSERT INTO ping_routes VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        num,
                        "switch",
                        "dest",
                        "source",
                        "date",
                        purpose,
                        event,
                        None,
                        json.dumps({"purpose": purpose}),
                    ),
                )
        # Destination row already committed on an interrupted older migration pass.
        with self.history.connect() as db:
            db.execute(
                "INSERT INTO ping_routes(legacy_id,endpoint,destination,source_key,observed,purpose,output) VALUES(1,'switch','dest','source','date','baseline','{}')"
            )
        migrated = LongHistory(self.root)
        with migrated.connect() as db:
            items = [
                tuple(r) for r in db.execute("SELECT legacy_id,run_id FROM ping_routes ORDER BY id")
            ]
            self.assertEqual(items, [(1, None), (2, key)])
        with closing(sqlite3.connect(self.jobs.db)) as old:
            self.assertIsNone(
                old.execute("SELECT 1 FROM sqlite_master WHERE name='ping_routes'").fetchone()
            )
