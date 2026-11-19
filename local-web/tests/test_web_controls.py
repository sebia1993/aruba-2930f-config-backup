"""Offline checks of the browser-to-native control boundary."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

from localweb.backup_tools import HistoryFolders
from localweb.controls import Controls

from aruba2930f_backup.hostkeys import sha256_fingerprint
from aruba2930f_backup.models import (
    Credentials,
    DeviceTarget,
    HostKeyCheck,
    HostKeyObservation,
    HostKeyTrustState,
)
from aruba2930f_backup.poe_power import LLDPNeighbor, LLDPObservation, LLDPStatus
from aruba2930f_backup.poe_query_models import PoEQueryResult, PoETarget, QueryStatus
from aruba2930f_backup.port_control import (
    PortAction,
    PortControlService,
    PortOperationResult,
    PortSnapshot,
)


class WebControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.target = DeviceTarget("192.0.2.42", 2222)
        self.state = SimpleNamespace(
            credentials=Credentials("test", "test-secret"),
            host_key_store=object(),
            last_results=(),
            data_dir=Path(temporary.name),
            history_folders=HistoryFolders(),
        )
        self.clock = 100.0
        self.cancel = Event()
        self.service = Mock(spec=PortControlService)
        self.service.inspect.side_effect = lambda device, port, credentials, **kw: self.snapshot(
            port
        )
        self.service.apply.side_effect = lambda snapshot, *args, **kw: PortOperationResult(
            "verified",
            "변경 확인",
            snapshot,
            snapshot,
        )
        self.controls = Controls(
            self.state, clock=lambda: self.clock, service_factory=lambda: self.service
        )
        self.controls.legacy_directory = Path(temporary.name) / "legacy"

    def snapshot(self, port):
        return PortSnapshot(
            self.target,
            port,
            "edge",
            "Aruba 2930F JL255A",
            True,
            "Up",
            True,
            "Delivering",
            8.0,
            "SHA256:approved-fixture",
        )

    def query(self, count):
        neighbors = LLDPObservation(
            LLDPStatus.OK, (LLDPNeighbor(model="AP-535", chassis_id="fixture", port_id="eth0"),)
        )
        self.state.last_results = tuple(
            PoEQueryResult(
                PoETarget(self.target, (str(i),)),
                str(i),
                QueryStatus.OK,
                snapshot=self.snapshot(str(i)),
                lldp=neighbors,
            )
            for i in range(1, count + 1)
        )

    def preview(self, ports=("1",), operation="poe_off", scope="batch"):
        return self.controls.dispatch(
            "control.preview",
            {
                "operation": operation,
                "scope": scope,
                "targets": [{"ip": self.target.endpoint, "port": port} for port in ports],
            },
            self.cancel,
        )

    def apply(self, plan, **override):
        return self.controls.dispatch(
            "control.apply",
            {
                "planId": plan["planId"],
                "confirmation": plan["confirmationText"],
                **override,
            },
            self.cancel,
        )

    def test_first_key_can_be_registered_but_manual_control_still_requires_confirmation(self):
        plan = self.preview(scope="single", operation="disable")
        self.assertEqual(plan["confirmationText"], "192.0.2.42:2222 / 1")
        register = self.service.inspect.call_args.kwargs["approve_host_key"]
        observed = HostKeyObservation(self.target, "ssh-ed25519", sha256_fingerprint(b"switch"))
        unknown = HostKeyCheck(observed, HostKeyTrustState.UNKNOWN)
        self.assertTrue(register(unknown))
        self.assertFalse(register(replace(unknown, state=HostKeyTrustState.CHANGED)))
        self.assertFalse(register(replace(unknown, state=HostKeyTrustState.REJECTED)))
        self.cancel.set()
        self.assertFalse(register(unknown))
        self.cancel.clear()
        self.assertFalse(self.service.apply.called)
        result = self.apply(plan, operation="poe_on", targets=[{"ip": "192.0.2.1", "port": "9"}])
        self.assertEqual(result["results"][0]["status"], "verified")
        args = self.service.apply.call_args.args
        self.assertEqual(
            (args[0].target, args[0].interface, args[1]), (self.target, "1", PortAction.DISABLE)
        )
        with self.assertRaises(ValueError):
            self.apply(plan)
        self.service.apply.assert_called_once()

    def test_failed_confirmation_consumes_plan_without_calling_switch(self):
        plan = self.preview(scope="single")
        with self.assertRaises(ValueError):
            self.apply(plan, confirmation="not the port")
        with self.assertRaises(ValueError):
            self.apply(plan)
        self.service.apply.assert_not_called()

    def test_timeout_changed_session_or_changed_query_invalidate_plan(self):
        for reason in ("timeout", "session", "query", "cancel"):
            with self.subTest(reason=reason):
                self.cancel.clear()
                self.query(1)
                plan = self.preview()
                if reason == "timeout":
                    self.clock += 120
                elif reason == "session":
                    self.state.credentials = Credentials("test", "test-secret")
                elif reason == "query":
                    self.query(1)
                else:
                    self.cancel.set()
                with self.assertRaises(ValueError):
                    self.apply(plan)
        self.service.apply.assert_not_called()

    def test_batch_uses_only_server_observations_and_native_ap535_eligibility(self):
        self.query(1)
        original = self.state.last_results[0]
        for row in (
            replace(original, lldp=None),
            replace(original, complete=False),
            replace(original, snapshot=replace(original.snapshot, link_status="Down")),
        ):
            self.state.last_results = (row,)
            with self.assertRaises(ValueError):
                self.preview()
        self.state.last_results = (original,)
        with self.assertRaises(ValueError):
            self.preview(("1", "2"))
        with self.assertRaises(ValueError):
            self.preview(("1", "1"))
        self.service.inspect.assert_not_called()
        self.service.apply.assert_not_called()

    def test_probe_preserves_ap535_pinning_and_journals_with_separate_chunk_approvals(self):
        self.query(11)
        plan = self.preview(tuple(str(i) for i in range(1, 12)))
        self.assertEqual(
            (len(plan["targets"]), plan["remaining"], plan["confirmationText"]),
            (10, 1, "EXECUTE 11"),
        )
        self.assertEqual([r["port"] for r in plan["later"]], ["11"])
        first = self.apply(plan)
        self.assertEqual([r["status"] for r in first["results"]], ["verified"] * 10)
        self.assertEqual(self.service.apply.call_count, 10)
        self.assertEqual(
            self.service.apply.call_args.kwargs["expected_lldp_identity"], ("fixture", "eth0")
        )
        self.assertEqual([r.interface for r in self.state.last_results], ["11"])
        self.assertEqual(
            [r["status"] for r in self.controls._history() if r["port"] == "11"], ["queued"]
        )
        next_plan = first["nextPlan"]
        self.assertNotEqual(plan["planId"], next_plan["planId"])
        self.assertEqual(next_plan["confirmationText"], "EXECUTE 1")
        with self.assertRaises(ValueError):
            self.apply(plan)
        self.apply(next_plan)
        self.assertEqual(self.service.apply.call_count, 11)
        history = self.controls.dispatch("control.history", {}, self.cancel)["rows"]
        self.assertEqual(len(history), 11)
        self.assertEqual(len({row["source"] for row in history}), 1)
        self.assertTrue(all(item["status"] == "verified" for item in history))
        self.assertNotIn("test-secret", json.dumps(history))

    def test_uncertain_write_halts_all_subsequent_changes(self):
        self.query(12)
        self.service.apply.side_effect = lambda snap, *args, **kw: PortOperationResult(
            "unverified",
            "결과 불확실",
            snap,
            None,
        )
        result = self.apply(self.preview(tuple(str(i) for i in range(1, 13))))
        self.assertNotIn("nextPlan", result)
        self.assertEqual([r["status"] for r in result["results"]], ["unverified"] + ["paused"] * 9)
        self.service.apply.assert_called_once()

    def test_manual_path_cannot_accept_multiple_ports_and_syntax_is_strict(self):
        for ports in (("1", "2"), ("1-2",), ("1/1/1",), ("1\n",)):
            with self.subTest(ports=ports), self.assertRaises(ValueError):
                self.preview(ports, scope="single")
        self.service.inspect.assert_not_called()

    def test_legacy_unfinished_audit_is_exposed_as_unknown(self):
        directory = self.controls.legacy_directory
        directory.mkdir()
        intent = {
            "time": "2026-11-18",
            "target": self.target.endpoint,
            "interface": "1",
            "hostname": "edge",
            "action": "poe_off",
            "status": "pending",
        }
        (directory / "unfinished.jsonl").write_text(json.dumps(intent), encoding="utf-8")
        row = self.controls.dispatch("control.history", {}, self.cancel)["rows"][0]
        self.assertEqual((row["status"], row["source"]), ("result_unknown", "unfinished.jsonl"))
        self.service.apply.assert_not_called()

    def quick(self, ports, operation="poe_off"):
        self.state._inventory = (PoETarget(self.target, tuple(ports)),)
        return self.controls.dispatch(
            "control.quick",
            {
                "operation": operation,
                "targets": [{"ip": self.target.endpoint, "port": port} for port in ports],
            },
            self.cancel,
        )

    def test_quick_preflight_requires_all_checked_ports_and_no_mutation(self):
        self.query(2)
        row1, row2 = self.state.last_results
        self.state.last_results = (row1, replace(row2, lldp=LLDPObservation(LLDPStatus.OK, ())))
        result = self.quick(["1", "2"])
        self.assertEqual([r["port"] for r in result["blocked"]], ["2"])
        self.assertNotIn("planId", result)
        self.service.apply.assert_not_called()
        self.service.inspect.assert_not_called()

    def test_quick_on_rechecks_already_on_and_does_not_send_redundant_change(self):
        self.query(1)
        self.state._query = Mock(return_value={"rows": [{"watts": "8.0"}], "files": []})
        result = self.quick(["1"], "poe_on")
        self.assertEqual([r["port"] for r in result["skipped"]], ["1"])
        self.assertEqual(result["rechecked"], 1)
        self.assertEqual(result["rows"][0]["watts"], "8.0")
        self.assertNotIn("planId", result)
        self.state._query.assert_called_once()
        self.service.apply.assert_not_called()

    def test_quick_restores_missing_evidence_before_planning_and_honors_cancel(self):
        def reread(groups, credentials, cancel, **kwargs):
            self.assertEqual(groups[0].interfaces, ("1",))
            self.assertTrue(kwargs["refresh"])
            self.query(1)
            return {"rows": [], "files": []}

        self.state._query = Mock(side_effect=reread)
        plan = self.quick(["1"])
        self.assertEqual(plan["targets"][0]["command"], "no interface 1 power-over-ethernet")
        self.service.apply.assert_not_called()
        self.cancel.set()
        with self.assertRaises(ValueError):
            self.apply(plan)
        self.service.apply.assert_not_called()

    def test_approved_quick_plan_runs_all_eleven_and_publishes_each_port(self):
        self.query(11)
        plan = self.quick([str(i) for i in range(1, 12)])
        self.assertTrue(plan["quick"])
        self.assertEqual((len(plan["targets"]), plan["remaining"]), (11, 0))
        self.service.apply.assert_not_called()
        result = self.apply(plan)
        self.assertEqual(self.service.apply.call_count, 11)
        self.assertNotIn("nextPlan", result)
        self.assertEqual(len(result["results"]), 11)
        self.assertEqual(result["results"][-1]["after"]["watts"], 8.0)
        self.assertEqual(self.state.progress["completed"], 11)
        self.assertEqual(len(self.state.progress["ports"]), 11)
        with self.assertRaises(ValueError):
            self.apply(plan)

    def test_import_journal_does_not_replay_pending_intent(self):
        header = {
            "event": "plan",
            "created_at": "2026-11-18",
            "action": "poe_off",
            "items": [{"endpoint": self.target.endpoint, "interface": "1", "hostname": "edge"}],
        }
        content = "\n".join(json.dumps(r) for r in [header, {"event": "intent", "index": 0}])
        result = self.controls.dispatch(
            "control.import-history", {"name": "old.jsonl", "text": content}, self.cancel
        )
        self.assertEqual(result["rows"][0]["status"], "result_unknown")
        self.assertEqual(result["rows"][0]["source"], "old.jsonl")
        self.service.apply.assert_not_called()

    def test_direct_inspect_returns_observations_without_creating_a_plan(self):
        result = self.controls.dispatch(
            "control.inspect", {"targets": [{"ip": self.target.endpoint, "port": "1"}]}, self.cancel
        )
        self.assertEqual(result["snapshot"]["poeStatus"], "Delivering")
        self.assertEqual(result["snapshot"]["model"], "Aruba 2930F JL255A")
        self.assertEqual(result["snapshot"]["watts"], 8.0)
        self.assertNotIn("planId", result)
        self.service.apply.assert_not_called()
