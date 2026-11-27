import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from localweb.ping_files import general_excel, import_profile, profile_request
from localweb.ping_path import RouteSession, parse_hops, run_route
from localweb.ping_tools import PingPlan
from openpyxl import load_workbook

PLAN = {"targets": "192.0.2.7", "destinations": [{"ip": "198.51.100.8", "label": "=SUM(1,2)"}]}
HOPS = "traceroute to 198.51.100.8\n 1 *\n 2 198.51.100.8 1500 us"


class PingFileTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.jobs = SimpleNamespace(directory=self.root, db=self.root / "web-history.sqlite")

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
        with closing(sqlite3.connect(self.jobs.db)) as db:
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
