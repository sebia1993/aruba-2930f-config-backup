"""Offline checks of the browser-to-native control boundary."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from localweb.controls import Controls
from localweb.operations import Operations

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
        self.state = Operations(Path(temporary.name))
        self.state.credentials = Credentials("test", "test-secret")
        self.state.query_service.collect = Mock(side_effect=self.post_read)
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

    def post_read(self, groups, credentials, **kwargs):
        rows = []
        for group in groups:
            for port in group.interfaces:
                snapshot = replace(
                    self.snapshot(port),
                    target=group.target,
                    poe_enabled=False,
                    watts=0.0,
                    observed_at="2026-11-19T09:00:01+00:00",
                )
                row = PoEQueryResult(group, port, QueryStatus.OK, snapshot=snapshot)
                kwargs["on_result"](row)
                rows.append(row)
        return rows

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
            "2026-11-19T09:00:00+00:00",
        )

    def query(self, count):
        neighbors = LLDPObservation(
            LLDPStatus.OK, (LLDPNeighbor(model="AP-535", chassis_id="fixture", port_id="eth0"),)
        )
        self.state._inventory = (
            PoETarget(self.target, tuple(str(i) for i in range(1, count + 1))),
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
        self.assertEqual([r.snapshot.watts for r in self.state.last_results], [0.0] * 10 + [8.0])
        self.assertEqual(first["powerRefresh"]["readings"][0]["watts"], 0.0)
        self.assertEqual(first["results"][0]["before"]["watts"], 8.0)
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
        rows = result["results"]
        self.assertEqual([r["status"] for r in rows], ["unverified"] + ["paused"] * 9)
        self.assertIn(self.target.endpoint, result["message"])
        self.assertIn("결과 불확실", rows[0]["message"])
        self.assertIn(self.target.endpoint, rows[1]["message"])
        self.assertIn("전송하지 않았습니다", rows[1]["message"])
        history = self.controls.dispatch("control.history", {}, self.cancel)["rows"]
        self.assertEqual(len(history), 12)
        for entry in history:
            if entry["port"] == "1":
                self.assertEqual(entry["message"], rows[0]["message"])
            else:
                self.assertEqual(entry["status"], "paused")
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

    def quick(self, ports, operation="poe_off", *, direct=False):
        self.state._inventory = (PoETarget(self.target, tuple(ports)),)
        return self.controls.dispatch(
            "control.quick-execute" if direct else "control.quick",
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
        self.assertEqual(len(result["powerRefresh"]["readings"]), 11)
        with self.assertRaises(ValueError):
            self.apply(plan)

    def test_direct_quick_runs_only_selected_ports_across_checkpoint_without_public_plan(self):
        self.query(12)
        ports = [str(i) for i in range(1, 12)]
        result = self.quick(ports, direct=True)
        self.assertEqual(result["selected"], 11)
        self.assertEqual([r["port"] for r in result["results"]], ports)
        self.assertEqual(self.service.apply.call_count, 11)
        self.assertEqual(self.state.progress["completed"], 11)
        self.assertNotIn("planId", result)
        self.assertNotIn("nextPlan", result)
        self.assertIsNone(self.controls._approval)
        self.assertEqual([r.interface for r in self.state.last_results], ports)
        self.assertEqual([r.snapshot.watts for r in self.state.last_results], [0.0] * 11)
        self.assertEqual(
            len(self.controls.dispatch("control.history", {}, self.cancel)["rows"]), 11
        )

    def test_direct_quick_no_write_when_any_requested_change_is_blocked(self):
        self.query(2)
        first, second = self.state.last_results
        self.state.last_results = first, replace(second, lldp=LLDPObservation(LLDPStatus.OK, ()))
        result = self.quick(["1", "2"], direct=True)
        self.assertEqual([row["port"] for row in result["blocked"]], ["2"])
        self.assertEqual(result["results"], [])
        self.service.apply.assert_not_called()
        self.service.inspect.assert_not_called()

    def test_direct_quick_skips_fresh_off_even_when_link_and_lldp_disappeared(self):
        self.query(2)
        first, second = self.state.last_results
        self.state.last_results = (
            first,
            replace(
                second,
                snapshot=replace(second.snapshot, poe_enabled=False, link_status="Down"),
                lldp=LLDPObservation(LLDPStatus.OK, ()),
            ),
        )
        rows = [
            {"ip": self.target.ip, "ssh_port": str(self.target.port), "interface": p}
            for p in ["1", "2"]
        ]
        self.state._query = Mock(return_value={"rows": rows, "files": []})
        result = self.quick(["1", "2"], direct=True)
        self.assertEqual(result["rechecked"], 1)
        self.assertEqual(self.state._query.call_args.args[0][0].interfaces, ("2",))
        self.assertEqual([r["port"] for r in result["skipped"]], ["2"])
        self.assertEqual([r["port"] for r in result["results"]], ["1"])
        self.assertEqual(result["rows"][0]["watts"], "0")
        self.assertFalse(result["rows"][0].get("controlUsed"))
        self.assertNotIn("controlUsed", result["rows"][1])
        self.service.apply.assert_called_once()

    def test_direct_quick_requires_poe_operation_and_respects_cancel(self):
        self.query(1)
        with self.assertRaises(ValueError):
            self.quick(["1"], "disable", direct=True)
        self.cancel.set()
        with self.assertRaises(ValueError):
            self.quick(["1"], direct=True)
        self.service.apply.assert_not_called()

    def test_delayed_read_collects_only_ports_still_drawing_after_verified_off(self):
        self.query(3)
        calls = []

        def settling(groups, creds, **kwargs):
            calls.append([(g.target, g.interfaces) for g in groups])
            rows = self.post_read(groups, creds, **kwargs)
            if len(calls) == 1:
                rows[0] = replace(rows[0], snapshot=replace(rows[0].snapshot, watts=6.5))
            return rows

        self.state.query_service.collect.side_effect = settling
        with patch("localweb.operations.POWER_SETTLE_SECONDS", 0):
            result = self.apply(self.preview(("1", "2")))
        self.assertEqual(calls, [[(self.target, ("1", "2"))], [(self.target, ("1",))]])
        self.assertEqual(self.service.apply.call_count, 2)
        self.assertEqual([r["watts"] for r in result["powerRefresh"]["readings"]], [0.0, 0.0])
        self.assertEqual([r["watts"] for r in result["rows"]], ["0", "0", "8.0"])
        self.assertNotEqual(
            result["results"][0]["before"]["observed"],
            result["powerRefresh"]["readings"][0]["observed"],
        )

    def test_missing_draw_on_valid_off_snapshot_allows_on_and_measures_real_power(self):
        self.query(1)

        def collect(groups, creds, **kwargs):
            rows = self.post_read(groups, creds, **kwargs)
            on = self.service.apply.call_count == 2
            return [
                replace(r, snapshot=replace(r.snapshot, poe_enabled=on, watts=6.25 if on else None))
                for r in rows
            ]

        self.state.query_service.collect.side_effect = collect
        off = self.quick(["1"], direct=True)
        reading = off["powerRefresh"]["readings"][0]
        self.assertEqual((reading["status"], reading["watts"]), ("unavailable", None))
        self.assertEqual(off["rows"][0]["watts"], "")
        self.assertIn("poe_on", off["rows"][0]["eligible_operations"])
        self.service.inspect.side_effect = lambda *args, **kw: self.state.last_results[0].snapshot
        on = self.quick(["1"], "poe_on", direct=True)
        self.assertEqual(self.service.apply.call_count, 2)
        self.assertEqual(on["results"][0]["status"], "verified")
        self.assertIsNone(on["results"][0]["before"]["watts"])
        self.assertEqual(on["powerRefresh"]["readings"][0]["watts"], 6.25)

    def test_uncertain_write_read_displays_watts_but_requires_independent_new_query(self):
        self.query(2)
        self.service.apply.side_effect = lambda snap, *args, **kw: PortOperationResult(
            "unverified", "결과 불확실", snap, None
        )
        result = self.quick(["1", "2"], direct=True)
        self.assertEqual([r["status"] for r in result["results"]], ["unverified", "paused"])
        self.assertEqual([r["port"] for r in result["powerRefresh"]["readings"]], ["1"])
        self.assertEqual(result["powerRefresh"]["readings"][0]["watts"], 0.0)
        first, second = result["rows"]
        self.assertTrue(first["controlUsed"] and first["powerObserved"])
        self.assertEqual(first["eligible_operations"], [])
        self.assertEqual(second["watts"], "8.0")
        self.assertEqual(self.quick(["1"], "poe_on", direct=True)["blocked"][0]["port"], "1")
        self.service.apply.assert_called_once()

        def missing_draw(groups, creds, **kwargs):
            (row,) = self.post_read(groups, creds, **kwargs)
            return [replace(row, snapshot=replace(row.snapshot, watts=None))]

        self.state.query_service.collect.side_effect = missing_draw
        refreshed = self.state.dispatch(
            "ports.refresh", {"targets": f"{self.target.endpoint} 1"}, self.cancel
        )
        self.assertIn("poe_on", refreshed["rows"][0]["eligible_operations"])
        self.assertFalse(self.state.unverified_controls)

    def test_error_cancel_and_export_failure_do_not_erase_command_results_or_other_ports(self):
        for situation in ("read-failure", "cancel", "export"):
            with self.subTest(situation=situation):
                self.cancel.clear()
                self.query(2)
                self.service.apply.reset_mock()

                def collect(groups, creds, *, situation=situation, **kwargs):
                    if situation == "cancel":
                        self.cancel.set()
                    if situation != "export":
                        raise OSError("no reader")
                    return self.post_read(groups, creds, **kwargs)

                self.state.query_service.collect.side_effect = collect
                with patch("localweb.operations.write_query_csv", side_effect=OSError("no CSV")):
                    result = self.apply(self.preview(("1",)))
                self.service.apply.assert_called_once()
                self.assertEqual(result["results"][0]["status"], "verified")
                self.assertEqual(result["files"], [])
                self.assertEqual(result["rows"][1]["watts"], "8.0")
                reading = result["powerRefresh"]["readings"][0]
                if situation == "export":
                    self.assertEqual((reading["status"], reading["watts"]), ("measured", 0.0))
                else:
                    status = "cancelled" if situation == "cancel" else "unavailable"
                    self.assertEqual((reading["status"], reading["watts"]), (status, None))
                    self.assertEqual(result["rows"][0]["watts"], "")
                    self.assertTrue(result["rows"][0]["refreshInvalidated"])

    def test_manual_non_inventory_port_is_read_by_exact_ssh_endpoint_without_expanding_matrix(self):
        other = DeviceTarget(self.target.ip, 22)
        group = PoETarget(other, ("1",), "other")
        existing = PoEQueryResult(
            group,
            "1",
            QueryStatus.OK,
            snapshot=replace(self.snapshot("1"), target=other, watts=15.0),
        )
        self.state._inventory = (group,)
        self.state.last_results = (existing,)

        def wrong_endpoint(groups, creds, **kwargs):
            self.assertEqual([(g.target, g.interfaces) for g in groups], [(self.target, ("1",))])
            self.assertFalse(kwargs["approve_host_key"](None))
            return [existing, replace(existing, group=groups[0])]

        self.state.query_service.collect.side_effect = wrong_endpoint
        result = self.apply(self.preview(scope="single", operation="poe_off"))
        (reading,) = result["powerRefresh"]["readings"]
        self.assertEqual(
            (reading["sshPort"], reading["status"], reading["watts"]), (2222, "unavailable", None)
        )
        self.assertEqual(result["results"][0]["status"], "verified")
        self.assertNotIn("rows", result)
        self.assertEqual(self.state.last_results, (existing,))
        self.assertEqual(self.state._inventory, (group,))
        self.service.apply.assert_called_once()

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
