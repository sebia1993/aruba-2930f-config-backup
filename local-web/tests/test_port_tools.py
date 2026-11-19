"""Desktop target editor and native matrix parity on local, offline inputs."""

import tempfile
import unittest
from pathlib import Path
from threading import Event

from localweb.operations import Operations
from localweb.port_tools import matrix_fields, matrix_observation

from aruba2930f_backup.models import DeviceTarget
from aruba2930f_backup.poe_query_models import PoEQueryResult, PoETarget, QueryStatus


class TargetToolTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.ops = Operations(Path(temp.name))

    def run_action(self, action, **body):
        return self.ops.dispatch(action, body, Event())

    def test_preview_apply_edit_append_and_empty_rows(self):
        rows = [{"ip": "192.0.2.5", "ssh_port": 2222, "interfaces": "1-3,3", "label": '2층, "AP"'}]
        preview = self.run_action("targets.preview", rows=rows)
        self.assertEqual(preview["port_count"], 3)
        self.assertEqual(self.ops._inventory, ())
        applied = self.run_action("targets.apply", targets=preview["targets"])
        self.assertEqual(applied["groups"], preview["groups"])
        second = self.run_action(
            "targets.preview",
            targets="192.0.2.5:2222 3,4\n192.0.2.5 1",
            mode="append",
            existing=applied["targets"],
        )
        self.assertEqual(second["port_count"], 5)
        self.assertEqual(second["groups"][0]["label"], '2층, "AP"')
        self.assertEqual(len(self.ops._inventory), 1)
        self.assertEqual(self.run_action("targets.apply", rows=[])["groups"], [])
        self.assertFalse(self.ops._inventory)

    def test_failed_preview_or_apply_preserve_inventory(self):
        original = self.run_action("targets.apply", targets="192.0.2.7 1,2")
        seeded = self.ops._inventory
        for bad in (
            {"targets": "not-a-switch 2"},
            {"rows": [{"ip": "192.0.2.7\n192.0.2.8", "interfaces": "3"}]},
            {"rows": [{"ip": "192.0.2.7", "ssh_port": 0, "interfaces": "4"}]},
            {"targets": "192.0.2.7 2", "mode": "bad"},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.run_action("targets.apply", **bad)
            self.assertEqual(self.ops._inventory, seeded)
        self.assertEqual(original["groups"][0]["interfaces"], ["1", "2"])

    def test_live_observation_and_export_fields_use_native_state(self):
        group = PoETarget(DeviceTarget("192.0.2.5", 2222), ("1/1",), "창고")
        for complete, expected in [(True, "unknown"), (False, "loading")]:
            native = PoEQueryResult(group, "1/1", QueryStatus.FAILED, complete=complete)
            live = matrix_observation(native)
            self.assertEqual(live["matrix_state"], expected)
            self.assertEqual(live["interface"], "1/1")
            self.assertEqual(live["ssh_port"], 2222)
            self.assertEqual({k: live[k] for k in matrix_fields(native)}, matrix_fields(native))
