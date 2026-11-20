"""SSH query coverage for switch-reported port inventories and their input contracts."""

import json
import secrets
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, call, patch

from verify_http_ssh import interface_status_table

from aruba2930f_backup.hostkeys import HostKeyStore, sha256_fingerprint
from aruba2930f_backup.models import (
    CollectionFailure,
    Credentials,
    DeviceTarget,
    ErrorCode,
    HostKeyObservation,
)
from aruba2930f_backup.poe_query import PoEQueryService
from aruba2930f_backup.poe_query_models import PoETarget, QueryStatus
from aruba2930f_backup.poe_targets import (
    load_target_profile,
    make_target,
    merge_targets,
    parse_pasted_targets,
    save_target_profile,
)
from aruba2930f_backup.port_control import (
    UNKNOWN_LINK,
    parse_interface_inventory,
    parse_interface_links,
)
from aruba2930f_backup.port_control_ssh import PortControlSSHSession, port_change_command


class DiscoveryTests(unittest.TestCase):
    def test_numeric_summary_below_status_table_is_not_a_physical_port(self):
        table = interface_status_table([("1", "", "Up"), ("2", "", "Unknown")])
        table.extend(["", "Other counters", "99".ljust(29) + "packet drops: 7"])
        self.assertEqual(
            parse_interface_inventory("\n".join(table)), {"1": "Up", "2": UNKNOWN_LINK}
        )

    def test_trunk_member_identifier_can_extend_beyond_port_ruler(self):
        for trunk in ("1/48-Trk10", "2/48-Trk120"):
            for gap in ("", " "):
                with self.subTest(trunk=trunk, gap=gap):
                    lines = interface_status_table([("1", "", "Down"), ("1", " Down", "Up")])
                    # A long token may fill the separator, or the printer can shift the name column.
                    lines[-1] = trunk + gap + lines[-1][10:]
                    expected = {"1": "Down", trunk.split("-")[0]: "Up"}
                    self.assertEqual(parse_interface_inventory("\n".join(lines)), expected)
                    self.assertEqual(parse_interface_links("\n".join(lines)), expected)

    def test_trunk_without_separator_keeps_port_and_uses_the_real_status_column(self):
        for trunk in ("1/48-Trk10", "2/48-Trk120", "1/A1-Trk22"):
            for name in ("AP floor 1", "1234 AP Up", "12345", "3층 Down"):
                for terminal_width in (True, False):
                    with self.subTest(trunk=trunk, name=name, terminal_width=terminal_width):
                        table = interface_status_table(
                            [("1", "", "Up"), (trunk, name, "Down")], terminal_width=terminal_width
                        )
                        self.assertIn(trunk + name, table[-1])
                        expected = {"1": "Up", trunk.split("-")[0]: "Down"}
                        self.assertEqual(parse_interface_inventory("\n".join(table)), expected)
                        self.assertEqual(parse_interface_links("\n".join(table)), expected)

        truncated = "\n".join([*interface_status_table([("1", "", "Up")]), "1/48-Trk10AP floor 1"])
        self.assertEqual(parse_interface_inventory(truncated), {})
        self.assertEqual(parse_interface_links(truncated), {"1": "Up"})

    def test_incomplete_physical_row_fails_discovery_without_erasing_other_explicit_links(self):
        table = interface_status_table([("1", "", "Up"), ("3", "", "Down")])
        table.insert(3, "2")
        self.session.read_interface_status.return_value = "\n".join(table)
        self.assertEqual(parse_interface_links("\n".join(table)), {"1": "Up", "3": "Down"})
        on_error, on_discovered = Mock(), Mock()
        self.assertEqual(
            self.collect([self.all], on_discovery_error=on_error, on_discovered=on_discovered), ()
        )
        on_discovered.assert_not_called()
        self.assertEqual(on_error.call_args.args[1].code, ErrorCode.COMMAND_REJECTED)
        self.session.read_poe.assert_not_called()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.address = DeviceTarget("192.0.2.44", 2222)
        self.all = PoETarget(self.address, (), "switch", all_ports=True)
        self.session = Mock()
        self.session.send_show.side_effect = {
            "show version": "Aruba 2930F JL255A",
            "show modules": "Chassis: Aruba 2930F JL255A",
            "show system information": "System Name : lab",
        }.__getitem__
        probe = Mock()
        probe.probe.side_effect = lambda target, **_: HostKeyObservation(
            target, "ssh-ed25519", sha256_fingerprint(b"fixture ssh key")
        )
        self.factory = Mock(return_value=self.session)
        self.query = PoEQueryService(
            HostKeyStore(self.directory / "keys.json"),
            host_key_probe=probe,
            session_factory=self.factory,
        )

    def collect(self, groups, **kwargs):
        return self.query.collect(
            groups,
            Credentials("reader", secrets.token_urlsafe()),
            approve_host_key=lambda _: True,
            **kwargs,
        )

    def test_discovers_only_observed_physical_ports_and_reuses_single_status_read(self):
        physical = ("1", "2/47", "1/A1", "A3")
        self.session.read_interface_status.return_value = "\n".join(
            interface_status_table(
                [
                    ("1", "", "Down"),
                    ("2/47-Trk1", "", "Up"),
                    ("1/A1", "", "Up"),
                    ("A3", "", "Unknown"),
                    ("Trk1", "", "Up"),
                    ("VLAN1", "", "Up"),
                    ("1/1/5", "", "Up"),
                ]
            )
        )
        self.session.read_interfaces.return_value = "\n".join(
            [
                "Port Type | Alert Enabled Status Mode",
                "1 100/1000T | No Yes Up Auto",
                "2/47-Trk1 100/1000T | No Yes Down Auto",
                "1/A1 SFP+ | No Yes Up Auto",
                "A3 SFP+ | No Yes Up Auto",
            ]
        )
        self.session.read_lldp.return_value = (
            "LLDP Remote Device Information Detail\nNo neighbors found."
        )
        self.session.read_poe.side_effect = lambda p: (
            f"Port Power Status for port {p}\nPower Enable : Yes\nDetection Status : Delivering\nPower : 1 W"
        )
        events = []
        rows = self.collect([self.all], on_discovered=events.append, on_result=events.append)
        self.assertEqual(events[0], PoETarget(self.address, physical, "switch"))
        self.assertEqual(tuple(row.interface for row in rows), physical)
        self.assertEqual([row.observed_link for row in rows], ["Down", "Up", "Up", UNKNOWN_LINK])
        self.assertTrue(all(row.complete and row.lldp.status.value == "ok" for row in rows))
        self.assertEqual(rows[2].status, QueryStatus.UNSUPPORTED)
        self.assertEqual(self.session.read_lldp.call_args_list, [call(port) for port in physical])
        self.assertEqual(self.session.read_poe.call_args_list, [call("1"), call("2/47")])
        self.session.read_interface_status.assert_called_once_with()
        self.factory.assert_called_once()
        self.session.close.assert_called_once_with()
        self.session.change_port.assert_not_called()

    def test_duplicate_link_is_unknown_and_live_names_never_become_port_ids(self):
        data = interface_status_table([("1", "9 Up", "Up"), ("1", "", "Down"), ("A1", "", "Down")])
        data.insert(3, "           10   Down")
        self.assertEqual(
            parse_interface_inventory("\n".join(data)), {"1": UNKNOWN_LINK, "A1": "Down"}
        )
        self.assertEqual(parse_interface_links("\n".join(data)), {"A1": "Down"})

    def test_bad_table_auth_cancel_or_cap_report_endpoint_failure_without_port_rows(self):
        self.session.read_interface_status.return_value = "Port Type | Alert Enabled Status Mode"
        on_error, on_row, discovered = Mock(), Mock(), Mock()
        rows = self.collect(
            [self.all], on_discovery_error=on_error, on_result=on_row, on_discovered=discovered
        )
        self.assertEqual(rows, ())
        self.assertEqual(on_error.call_args.args[0], self.all)
        self.assertEqual(on_error.call_args.args[1].code, ErrorCode.COMMAND_REJECTED)
        discovered.assert_not_called()
        on_row.assert_not_called()
        self.session.read_poe.assert_not_called()
        with self.assertRaises(CollectionFailure):
            self.collect([self.all])
        cancelled = Event()
        cancelled.set()
        self.collect([self.all], cancel_event=cancelled, on_discovery_error=on_error)
        self.assertEqual(on_error.call_args.args[1].code, ErrorCode.CANCELLED)
        self.session.connect.side_effect = CollectionFailure(ErrorCode.AUTH_FAILED, "SSH 인증 실패")
        self.collect([self.all], on_discovery_error=on_error)
        self.assertEqual(on_error.call_args.args[1].code, ErrorCode.AUTH_FAILED)
        self.session.connect.side_effect = None
        self.session.read_interface_status.return_value = "\n".join(
            interface_status_table([("1", "", "Up"), ("2", "", "Down")])
        )
        for limit in ("MAX_TOTAL_INTERFACES", "MAX_INTERFACES_PER_TARGET"):
            with self.subTest(limit=limit), patch(f"aruba2930f_backup.poe_query.{limit}", 1):
                self.collect([self.all], on_discovery_error=on_error, on_discovered=discovered)
                self.assertEqual(on_error.call_args.args[1].code, ErrorCode.COMMAND_REJECTED)
        discovered.assert_not_called()
        self.session.read_lldp.assert_not_called()

    def test_blank_scope_explicit_profile_compatibility_and_validation(self):
        subset = make_target(self.address.ip, 2222, "2/47", "switch")
        with self.assertRaises(ValueError):
            make_target(self.address.ip, 2222, "")
        for groups in ([subset, self.all], [self.all, subset]):
            self.assertEqual(merge_targets(groups), (self.all,))
        text = "IP\tssh_port\tinterfaces\tlabel\n192.0.2.44\t2222\t\tswitch\n"
        self.assertEqual(parse_pasted_targets(text, allow_all_ports=True), (self.all,))
        with self.assertRaises(ValueError):
            parse_pasted_targets(text)
        path = self.directory / "targets.json"
        for group, version in ((subset, 1), (self.all, 2)):
            save_target_profile(path, [group])
            data = json.loads(path.read_text("utf-8"))
            self.assertEqual(data["version"], version)
            self.assertEqual(load_target_profile(path), (group,))
        for edits in (
            {"interfaces": [], "all_ports": False},
            {"interfaces": ["1"], "all_ports": True},
            {"all_ports": 1},
        ):
            with self.subTest(edits=edits):
                malformed = {**data, "targets": [{**data["targets"][0], **edits}]}
                path.write_text(json.dumps(malformed), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_target_profile(path)
        data["version"] = 1
        del data["targets"][0]["all_ports"]
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_target_profile(path)

    def test_module_ports_go_only_to_validated_read_commands(self):
        session = Mock()
        PortControlSSHSession.read_poe(session, "A1")
        PortControlSSHSession.read_lldp(session, "1/A2")
        self.assertEqual(
            session._read_port_command.call_args_list,
            [call("show power-over-ethernet A1"), call("show lldp info remote-device 1/A2")],
        )
        for port in ("VLAN1", "Trk1", "1/1/3", "A1-4", "A1\nshow version"):
            with self.assertRaises(ValueError):
                PortControlSSHSession.read_lldp(session, port)
        with self.assertRaises(ValueError):
            port_change_command("1/A1", "disable")
