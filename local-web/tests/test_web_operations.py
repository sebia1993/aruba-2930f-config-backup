"""Offline exercise of the same web operations, exporters and native parsers used on site."""

import json
import secrets
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from localweb.backup_destination import copy_backup_files
from localweb.controls import Controls
from localweb.operations import Operations, TargetInputError, _backup_targets
from openpyxl import Workbook, load_workbook
from verify_http_ssh import interface_status_table

from aruba2930f_backup.collector import ArubaCollector
from aruba2930f_backup.hostkeys import HostKeyStore, sha256_fingerprint
from aruba2930f_backup.models import (
    CollectionFailure,
    DeviceTarget,
    HostKeyObservation,
    HostKeyTrustState,
)
from aruba2930f_backup.poe_query import PoEQueryService
from aruba2930f_backup.poe_query_models import PoEQueryResult, QueryStatus
from aruba2930f_backup.port_control import PortControlService


class OperationsTests(unittest.TestCase):
    target = DeviceTarget("192.0.2.47", 2222)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.ops = Operations(self.base)
        self.ops.legacy_key_store = HostKeyStore(self.base / "unused-desktop-key-store")
        self.ops.legacy_backup_dir = self.base / "desktop-backup"
        self.cancel = Event()
        self.probe = Mock()
        self.fingerprint = sha256_fingerprint(b"fixture original public key")
        self.probe.probe.side_effect = lambda target, **_: HostKeyObservation(
            target, "ssh-ed25519", self.fingerprint
        )
        self.factory = Mock()
        self.session = self.factory.create.return_value
        self.session.get_prompt.return_value = "lab-edge#"
        self.commands = {
            "show version": "Aruba 2930F JL255A",
            "show modules": "Chassis: Aruba 2930F JL255A",
            "show system information": "System Name : lab-edge",
            "show running-config": 'Running configuration:\nhostname "lab-edge"\n',
        }
        self.session.send_show.side_effect = lambda cmd, **_: self.commands[cmd]
        self.ops.collector = ArubaCollector(
            self.factory, host_key_probe=self.probe, host_key_store=self.ops.host_key_store
        )
        self.ops.query_service = PoEQueryService(
            self.ops.host_key_store,
            host_key_probe=self.probe,
            session_factory=lambda *args: self.session,
        )
        self.login_password = secrets.token_urlsafe()
        self.enable_secret = secrets.token_urlsafe()
        self.call(
            "session.open",
            username="fixture-operator",
            password=self.login_password,
            enable_secret=self.enable_secret,
        )

    def call(self, name, **body):
        return self.ops.dispatch(name, body, self.cancel)

    def read_cable(self, target):
        self.session.test_cable.return_value = "Test completed."
        self.session.read_cable.return_value = {"status": "complete", "pairs": []}
        with patch("localweb.operations.CableSession", self.factory.create):
            (row,) = self.call("cable.test", targets=f"{target.endpoint} 1", confirmed=True)["rows"]
            return row

    def approve(self):
        (check,) = self.call("hostkey.inspect", targets=self.target.endpoint)["checks"]
        self.assertEqual(check["state"], "unknown")
        self.assertTrue(
            self.call(
                "hostkey.approve", endpoint=check["endpoint"], fingerprint=check["fingerprint"]
            )["approved"]
        )

    def test_review_is_tied_to_observed_endpoint_and_fingerprint(self):
        self.call("hostkey.inspect", targets=self.target.endpoint)
        with self.assertRaises(ValueError):
            self.call("hostkey.approve", endpoint=self.target.endpoint, fingerprint="changed")
        self.approve()
        self.assertEqual(
            self.call("hostkey.inspect", targets=self.target.endpoint)["checks"][0]["state"],
            "trusted",
        )
        self.fingerprint = sha256_fingerprint(b"a different unapproved key")
        (check,) = self.call("hostkey.inspect", targets=self.target.endpoint)["checks"]
        self.assertEqual(check["state"], "changed")
        with self.assertRaises(ValueError):
            self.call(
                "hostkey.approve", endpoint=self.target.endpoint, fingerprint=self.fingerprint
            )
        self.assertEqual(self.read_cable(self.target)["status"], "failed")
        self.factory.create.assert_not_called()

    def test_first_contact_pins_each_ssh_port_and_reuses_persisted_records(self):
        second = DeviceTarget(self.target.ip, 2223)
        original = self.probe.probe(self.target)
        other = HostKeyObservation(second, "ssh-ed25519", sha256_fingerprint(b"other device key"))
        self.probe.probe.side_effect = [original, other, other]
        for device in (self.target, second):
            row = self.read_cable(device)
            self.assertEqual(row["status"], "measured")
        store = HostKeyStore(self.ops.host_key_store.path)
        self.assertEqual(store.check(original).state, HostKeyTrustState.TRUSTED)
        self.assertEqual(store.check(other).state, HostKeyTrustState.TRUSTED)
        before = store.path.read_bytes()
        row = self.read_cable(second)
        self.assertEqual(row["status"], "measured")
        self.assertEqual(store.path.read_bytes(), before)

    def test_cancel_during_first_probe_leaves_no_pin_or_authenticated_session(self):
        def cancel_on_probe(target, **kwargs):
            self.cancel.set()
            return HostKeyObservation(target, "ssh-ed25519", self.fingerprint)

        self.probe.probe.side_effect = cancel_on_probe
        self.read_cable(self.target)
        self.assertEqual(self.ops.host_key_store.list_approved(), ())
        self.factory.create.assert_not_called()

    def test_pin_write_failure_prevents_authentication(self):
        with patch.object(
            self.ops.host_key_store, "_write_endpoints", side_effect=OSError
        ) as write:
            row = self.read_cable(self.target)
        write.assert_called_once()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(self.ops.host_key_store.list_approved(), ())
        self.factory.create.assert_not_called()

    def test_unusable_or_misdirected_probe_never_authenticates_or_creates_a_pin(self):
        for observation in (
            HostKeyObservation(self.target, "ssh-ed25519", ""),
            HostKeyObservation(DeviceTarget(self.target.ip, 22), "ssh-ed25519", self.fingerprint),
        ):
            with self.subTest(observation=observation):
                self.probe.probe.side_effect = None
                self.probe.probe.return_value = observation
                row = self.read_cable(self.target)
                self.assertEqual(row["status"], "failed")
                self.assertEqual(self.ops.host_key_store.list_approved(), ())
                self.factory.create.assert_not_called()

    def test_cable_diagnosis_blocked_for_unsupported_switch(self):
        self.approve()
        self.commands.update({"show version": "Unidentified", "show modules": "Unknown"})
        row = self.read_cable(self.target)
        self.assertEqual(row["status"], "failed")
        self.session.clear_cable.assert_not_called()
        self.session.test_cable.assert_not_called()
        self.session.read_cable.assert_not_called()
        self.session.close.assert_called_once()

    def test_legacy_pin_import_is_read_only_and_forget_requires_exact_confirmation(self):
        legacy = self.ops.legacy_key_store
        original = self.probe.probe(self.target)
        legacy.approve([legacy.check(original)])
        legacy_bytes = legacy.path.read_bytes()
        (keys,) = self.call("hostkey.list")["keys"]
        self.assertEqual(keys["fingerprint"], self.fingerprint)
        self.assertEqual(
            self.call("hostkey.inspect", targets=self.target.endpoint)["checks"][0]["state"],
            "trusted",
        )
        with self.assertRaises(CollectionFailure):
            self.call(
                "hostkey.forget", endpoint=self.target.endpoint, fingerprint="wrong fingerprint"
            )
        self.assertTrue(
            self.call(
                "hostkey.forget", endpoint=self.target.endpoint, fingerprint=self.fingerprint
            )["removed"]
        )
        self.assertEqual(self.call("hostkey.list")["keys"], [])
        self.assertEqual(self.ops.host_key_store.check(original).state, HostKeyTrustState.UNKNOWN)
        self.assertEqual(legacy.path.read_bytes(), legacy_bytes)

    def test_remove_reviewed_keys_is_atomic_when_a_displayed_pin_is_stale(self):
        ids = [self.target, DeviceTarget("192.0.2.48", 22)]
        listed = self.call("hostkey.inspect", targets="\n".join(t.endpoint for t in ids))["checks"]
        self.call("hostkey.approve", checks=listed)
        with self.assertRaises(CollectionFailure):
            self.call(
                "hostkey.forget", checks=[listed[0], dict(listed[1], fingerprint="different")]
            )
        self.assertEqual(len(self.call("hostkey.list")["keys"]), 2)
        self.call("hostkey.forget", checks=listed)
        self.assertFalse(self.call("hostkey.list")["keys"])

    def test_backup_files_report_and_secret_free_metadata(self):
        response = self.call(
            "backup.run", targets=self.target.endpoint, filename_mode="ip", max_attempts=1
        )
        self.assertEqual(response["rows"][0]["status"], "success")
        paths = {f["name"]: self.base / f["id"] for f in response["files"]}
        self.assertEqual(set(paths), {"192.0.2.47.txt", "result.xlsx", "operation.jsonl"})
        self.assertIn(b"\r\n", paths["192.0.2.47.txt"].read_bytes())
        report = load_workbook(paths["result.xlsx"], read_only=True)
        try:
            cells = [str(c) for ws in report for row in ws.values for c in row]
            self.assertIn("lab-edge", cells)
        finally:
            report.close()
        log = paths["operation.jsonl"].read_text(encoding="utf-8")
        self.assertNotIn(self.target.ip, log)
        metadata = log + json.dumps(response) + str(cells)
        for value in (self.login_password, self.enable_secret, "Running configuration:"):
            self.assertNotIn(value, metadata)

    def test_changed_key_does_not_start_authenticated_backup_or_alter_pin(self):
        self.approve()
        original_key_store = self.ops.host_key_store.path.read_bytes()
        self.fingerprint = sha256_fingerprint(b"replacement device key")
        response = self.call("backup.run", targets=self.target.endpoint, max_attempts=1)
        self.assertEqual(response["rows"][0]["status"], "failed")
        self.assertFalse(any(f["name"].endswith(".txt") for f in response["files"]))
        self.factory.create.assert_not_called()
        self.assertEqual(self.ops.host_key_store.path.read_bytes(), original_key_store)

    def test_selected_backup_directory_exports_without_replacing_files(self):
        self.approve()
        destination = self.base / "다른 백업 폴더"
        response = self.call(
            "backup.run",
            targets=self.target.endpoint,
            max_attempts=1,
            output_directory=str(destination),
        )
        copied = Path(response["output_directory"])
        names = [f["name"] for f in response["files"]]
        canonical = (self.base / response["files"][0]["id"]).parent
        self.assertEqual(copied.parent.parent, destination)
        self.assertFalse(response["warnings"])
        for name in names:
            self.assertEqual((copied / name).read_bytes(), (canonical / name).read_bytes())
        sentinel = copied / names[0]
        sentinel.write_bytes(b"retained previous file")
        second, warnings = copy_backup_files(destination, canonical, names)
        self.assertEqual(warnings, [])
        self.assertNotEqual(copied, second)
        self.assertEqual(sentinel.read_bytes(), b"retained previous file")

    def test_partial_copy_reports_failure_and_keeps_original_artifacts(self):
        self.approve()
        destination = self.base / "secondary output"
        destination.mkdir()
        copy_stream = shutil.copyfileobj

        def fail_workbook(src, dest):
            if Path(src.name).suffix == ".xlsx":
                dest.write(b"incomplete")
                raise OSError("disk failure in fixture")
            copy_stream(src, dest)

        with patch("localweb.backup_destination.copyfileobj", side_effect=fail_workbook):
            response = self.call(
                "backup.run",
                targets=self.target.endpoint,
                max_attempts=1,
                output_directory=str(destination),
            )
        self.assertIn("result.xlsx", "".join(response["warnings"]))
        copy_dir = Path(response["output_directory"])
        self.assertTrue((copy_dir / "operation.jsonl").is_file())
        self.assertFalse((copy_dir / "result.xlsx").exists())
        self.assertTrue(all((self.base / f["id"]).is_file() for f in response["files"]))

    def test_relative_and_device_output_rejected_before_ssh(self):
        for invalid in ("relative/folder", "//./pipe/test", "//?/C:/Windows"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.call(
                    "backup.run",
                    targets=self.target.endpoint,
                    max_attempts=1,
                    output_directory=invalid,
                )
        self.probe.probe.assert_not_called()

    def test_backup_history_reads_web_and_desktop_devices_without_credentials(self):
        self.approve()
        response = self.call("backup.run", targets=self.target.endpoint, max_attempts=1)
        file = next(f for f in response["files"] if f["name"] == "result.xlsx")
        old_report = self.ops.legacy_backup_dir / "2019-05-06" / "120000" / "result.xlsx"
        old_report.parent.mkdir(parents=True)
        old_report.write_bytes((self.base / file["id"]).read_bytes())
        self.call("session.close")
        history = self.call("backup.history")["rows"]
        self.assertEqual([row["source"] for row in history], ["web", "desktop"])
        self.assertTrue(
            all(row["ip"] == self.target.ip and row["hostname"] == "lab-edge" for row in history)
        )
        self.assertEqual(history[-1]["run"], "2019-05-06/120000")
        with patch("localweb.backup_tools.os.startfile") as launch:
            self.call("history.open-folder", folder_id=history[-1]["folder_id"])
            launch.assert_called_once_with(old_report.parent, "explore")
            with self.assertRaises(ValueError):
                self.call("history.open-folder", folder_id=str(old_report))
            old_report.unlink()
            with self.assertRaises(ValueError):
                self.call("history.open-folder", folder_id=history[-1]["folder_id"])

    def test_common_port_with_log_write_failure_keeps_settings_and_report(self):
        self.approve()
        with patch("localweb.operations.SanitizedJsonlLogger.log", side_effect=OSError("fixture")):
            response = self.call(
                "backup.run", targets=self.target.ip, ssh_port=self.target.port, max_attempts=1
            )
        (row,) = response["rows"]
        self.assertEqual(row["endpoint"], self.target.endpoint)
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["sku"], "JL255A")
        self.assertEqual(row["host_key_attempts"], 1)
        self.assertEqual(row["attempts"], 1)
        self.assertTrue(any("진단 로그" in message for message in response["warnings"]))
        names = {item["name"] for item in response["files"]}
        self.assertIn("result.xlsx", names)
        self.assertNotIn("operation.jsonl", names)
        report = next(item for item in response["files"] if item["name"] == "result.xlsx")
        wb = load_workbook(self.base / report["id"], read_only=True)
        try:
            text = str(list(wb["Summary"].values))
            self.assertIn("진단 로그", text)
        finally:
            wb.close()

    def test_backup_reports_all_bad_or_duplicate_lines_before_network(self):
        raw = "\n192.0.2.2\ninvalid\n192.0.2.2:22\n192.0.2.2:99999"
        with self.assertRaises(TargetInputError) as caught:
            self.call("backup.run", targets=raw)
        for row in ("3행", "4행", "5행"):
            self.assertIn(row, str(caught.exception))
        self.probe.probe.assert_not_called()
        separate = _backup_targets({"targets": "192.0.2.2\n192.0.2.2:2222"})
        self.assertEqual([t.port for t in separate], [22, 2222])
        many = {"targets": "\n".join(f"192.0.{n // 250}.{n % 250 + 1}" for n in range(301))}
        self.assertEqual(len(_backup_targets(many)), 301)

    def test_ip_only_backup_connects_to_standard_ssh_port_for_each_address(self):
        ips = ["192.0.2.47", "192.0.2.48"]
        result = self.call("backup.run", ips="\ufeff" + " \r\n\r\n ".join(ips), max_attempts=1)
        self.assertCountEqual(
            [row["endpoint"] for row in result["rows"]], [f"{ip}:22" for ip in ips]
        )
        self.assertTrue(all(row["status"] == "success" for row in result["rows"]))
        self.assertEqual(
            {call.args[0] for call in self.probe.probe.call_args_list},
            {DeviceTarget(ip, 22) for ip in ips},
        )

    def test_ip_only_backup_rejects_ports_malformed_duplicate_and_mixed_input_before_network(self):
        bad_ips = "192.0.2.1\n192.0.2.1:22\n192.0.2.1:2222\n999.0.0.1\n192.0.2.1"
        with self.assertRaises(TargetInputError) as caught:
            self.call("backup.run", ips=bad_ips)
        for n in (2, 3, 4, 5):
            self.assertIn(f"{n}행", str(caught.exception))
        for overrides in ({"ssh_port": 2222}, {"targets": self.target.endpoint}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.call("backup.run", ips=self.target.ip, **overrides)
        self.probe.probe.assert_not_called()
        self.factory.create.assert_not_called()

    def test_folder_picker_returns_chosen_path_or_cancel(self):
        with patch("localweb.backup_tools.subprocess.Popen") as launch:
            dialog = launch.return_value.__enter__.return_value
            dialog.communicate.return_value = (str(self.base).encode("utf-8"), None)
            dialog.returncode = 0
            self.assertEqual(self.call("backup.pick-directory"), {"directory": str(self.base)})
            dialog.communicate.return_value = (b"", None)
            self.assertEqual(self.call("backup.pick-directory"), {"directory": ""})

    def test_history_includes_device_300_and_latest_30_reports(self):
        directory = self.ops.legacy_backup_dir / "2020-03-15"
        directory.mkdir(parents=True)
        book = Workbook()
        sheet = book.active
        sheet.title = "Devices"
        sheet.append(("IP", "Hostname", "Status"))
        for n in range(300):
            sheet.append((f"192.0.{n // 250}.{n % 250 + 1}", f"edge{n}", "success"))
        for n in range(31):
            run = directory / f"{n:06d}"
            run.mkdir()
            book.save(run / "result.xlsx")
        book.close()
        history = self.call("backup.history")["rows"]
        self.assertEqual(len(history), 300 * 30)
        self.assertEqual(history[299]["hostname"], "edge299")
        self.assertEqual(history[-1]["run"], "2020-03-15/000001")

    def test_port_query_uses_native_evidence_without_generating_files(self):
        self.session.read_interfaces.return_value = (
            "Port Type | Alert Enabled Status Mode\n24 100/1000T | No Yes Down Auto"
        )
        self.session.read_interface_status.return_value = "\n".join(
            interface_status_table([("24", "", "Up")])
        )
        self.session.read_poe.return_value = (
            "Status and Configuration Information for port 24\nPower Enable : Yes\n"
            "PoE Port Status : Delivering\nPD Power Draw : 13.2 W"
        )
        self.session.read_lldp.return_value = (
            "LLDP Remote Device Information Detail\nLocal Port : 24\nChassisType : mac-address\n"
            "ChassisId : 00 00 00 00 00 01\nPortType : interface-name\nPortId : eth0\n"
            "SysName : sample-ap\nSystem Descr : ArubaOS (MODEL:535), Version Aruba AP\n"
            "System Capabilities Supported : bridge, wlan-access-point\n"
            "System Capabilities Enabled : wlan-access-point"
        )
        response = self.call("ports.query", targets=f"{self.target.endpoint} 24")
        (row,) = response["rows"]
        self.assertEqual(
            (row["ip"], row["ssh_port"], row["status"], row["interface"], row["watts"]),
            (self.target.ip, 2222, "ok", "24", 13.2),
        )
        self.assertIn("sample-ap", row["ap_names"])
        self.assertEqual(
            list(row["lldp_neighbors"][0]["system_capabilities_enabled"]), ["wlan-access-point"]
        )
        self.assertIn("poe_off", row["eligible_operations"])
        self.assertEqual(row["link_status"], "Up")
        self.assertIsNotNone(self.ops.last_results[0].snapshot)
        self.assertNotIn("files", response)
        self.assertFalse((self.base / "outputs").exists())
        with self.assertRaises(ValueError):
            self.call("ports.export")
        self.call("session.close")
        self.assertEqual(self.ops.last_results, ())
        self.assertEqual(self.ops.session_summary(), {"active": False, "username": ""})

    def test_link_sample_and_completion_timestamps_follow_the_independent_reads(self):
        self.session.read_interfaces.return_value = (
            "Port Type | Alert Enabled Status Mode\n1 100/1000T | No Yes Down Auto"
            "\n2 100/1000T | No Yes Down Auto"
        )
        second = 0

        def power(port):
            nonlocal second
            second = int(port)
            return f"Port Power Status for port {port}\nPower Enable : Yes\nDetection Status : Delivering\nPower : 2 W"

        def lldp(port):
            nonlocal second
            second = 10 + int(port)
            return "LLDP Remote Device Information Detail\nNo neighbors found."

        self.session.read_poe.side_effect = power
        self.session.read_lldp.side_effect = lldp
        for rejected in (False, True):
            with self.subTest(rejected=rejected):

                def status(rejected=rejected):
                    nonlocal second
                    second = 20
                    if rejected:
                        raise TimeoutError
                    # The status response contains only port 1.
                    return "\n".join(interface_status_table([("1", "", "Up")]))

                self.session.read_interface_status.side_effect = status
                with patch("aruba2930f_backup.poe_query.datetime") as clock:
                    clock.now.side_effect = lambda tz: datetime(2026, 1, 1, 0, 0, second, tzinfo=tz)
                    response = self.call("ports.query", targets=f"{self.target.endpoint} 1-2")
                rows = response["rows"]
                completed = datetime(2026, 1, 1, 0, 0, 20, tzinfo=UTC).isoformat()
                self.assertEqual([r["finished_at"] for r in rows], [completed, completed])
                self.assertEqual(
                    [r["link_observed_at"] for r in rows], ["" if rejected else completed, ""]
                )
                for number, row in enumerate(rows, 1):
                    self.assertEqual(datetime.fromisoformat(row["poe_finished_at"]).second, number)
                    self.assertEqual(
                        datetime.fromisoformat(row["lldp_observed_at"]).second, 10 + number
                    )
                    self.assertEqual(row["watts"], 2.0)

    def test_display_inspection_preserves_physical_link_without_authorizing_bad_poe(self):
        self.session.read_interfaces.return_value = (
            "Port Type | Alert Enabled Status Mode\n1 100/1000T | No Yes Down Auto"
        )
        self.session.read_interface_status.return_value = "\n".join(
            interface_status_table([("1", "", "Up")])
        )
        # Power Enable is present but the required detection status is missing.
        self.session.read_poe.return_value = (
            "Port Power Status for port 1\nPower Enable : Yes\nPower : 11 W"
        )
        control_service = PortControlService(
            host_key_store=self.ops.host_key_store,
            host_key_probe=self.probe,
            session_factory=lambda *args: self.session,
        )
        web = Controls(self.ops, service_factory=lambda: control_service)
        targets = [{"ip": self.target.endpoint, "port": "1"}]
        view = web.dispatch("control.inspect", {"targets": targets}, self.cancel)["snapshot"]
        self.assertEqual((view["link"], view["watts"], view["poeEnabled"]), ("Up", None, None))
        self.assertIsNone(control_service._snapshot)
        with self.assertRaises(CollectionFailure):
            web.dispatch(
                "control.preview",
                {"targets": targets, "scope": "single", "operation": "poe_on"},
                self.cancel,
            )
        self.assertIsNone(web._approval)
        self.assertEqual(self.session.read_poe.call_count, 2)  # one per explicit request
        self.session.read_interface_status.assert_called_once()
        self.session.change_port.assert_not_called()

    def test_native_profiles_roundtrip_ranges_and_ssh_ports(self):
        response = self.call("profiles.export", targets=f"{self.target.endpoint} 1/23-1/24,1/25")
        profile = (self.base / response["files"][0]["id"]).read_text(encoding="utf-8")
        imported = self.call("profiles.import", text=profile)
        self.assertIn("2222\t1/23,1/24,1/25", imported["targets"])
        clone = self.call("profiles.export", targets=imported["targets"])
        self.assertEqual(profile, (self.base / clone["files"][0]["id"]).read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            self.call("profiles.export", targets=f"{self.target.endpoint} 1-60000000")
        self.probe.probe.assert_not_called()

    def test_partial_refresh_retains_other_observations_and_recovers_touched_ports(self):
        requested = []

        def collect(groups, credentials, *, on_result, **options):
            rows = []
            for group in groups:
                for port in group.interfaces:
                    requested.append(port)
                    row = PoEQueryResult(
                        group, port, QueryStatus.FAILED, message=f"fixture read {len(requested)}"
                    )
                    rows.append(row)
                    on_result(row)
            return rows

        self.ops.query_service.collect = collect
        self.call("ports.query", targets=f"{self.target.endpoint} 23-24")
        first, second = self.ops.last_results
        self.ops.last_results = (first,)  # single port control consumed its port 24 snapshot
        result = self.call("ports.refresh", targets=f"{self.target.endpoint} 24")
        self.assertEqual(requested, ["23", "24", "24"])
        self.assertIs(self.ops.last_results[0], first)
        self.assertIsNot(self.ops.last_results[1], second)
        self.assertEqual([r["interface"] for r in result["rows"]], ["23", "24"])
        self.assertEqual((self.ops.progress["completed"], self.ops.progress["total"]), (1, 1))
        with self.assertRaises(ValueError):
            self.call("ports.refresh", targets=f"{self.target.endpoint} 25")
        self.assertEqual(len(requested), 3)

    def test_cancel_and_invalid_addresses_never_open_a_connection(self):
        self.cancel.set()
        with self.assertRaises(CollectionFailure):
            self.call("cable.test", targets=f"{self.target.endpoint} 1", confirmed=True)
        self.cancel.clear()
        with self.assertRaises(ValueError):
            self.call("backup.run", targets=f"{self.target.endpoint}\ninvalid", max_attempts=1)
        self.probe.probe.assert_not_called()
        self.factory.create.assert_not_called()
