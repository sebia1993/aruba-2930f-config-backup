from __future__ import annotations

import importlib
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from aruba2930f_backup.hostkeys import HostKeyStore, sha256_fingerprint
from aruba2930f_backup.models import (
    CollectionOptions,
    Credentials,
    DeviceTarget,
    HostKeyObservation,
)
from aruba2930f_backup.port_control_ssh import PortControlSSHSession
from tests.test_ssh import FakeConnection

CREDENTIALS = Credentials("operator", "secret-query-password", "secret-enable")


def target(ip: str, interfaces: str, ssh_port: int = 22) -> Any:
    return importlib.import_module("aruba2930f_backup.poe_targets").make_target(
        ip, ssh_port, interfaces
    )


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 0.05
        return self.value


class QueryConnection(FakeConnection):
    """A fake wire, using the production bounded SSH adapter and real parsers."""

    def __init__(self, network: Network, endpoint: DeviceTarget) -> None:
        responses = {
            "show version": ["show version\nAruba JL255A 2930F Switch\nedge-lab#"],
            "show modules": ["show modules\nChassis: Aruba 2930F JL255A\nedge-lab#"],
            "show system information": [
                "show system information\nStatus and Counters - General System Information\n"
                "System Name : DIRECT-SW-2930F\nSystem Contact :\nedge-lab#"
            ],
            "show interfaces brief": [
                "show interfaces brief\nPort Type | Alert Enabled Status Mode\n"
                + "\n".join(f"{p} 100/1000T | No Yes Up Auto" for p in network.ports[endpoint])
                + "\nedge-lab#"
            ],
        }
        for port, watts in network.ports[endpoint].items():
            responses[f"show power-over-ethernet {port}"] = [
                f"show power-over-ethernet {port}\n"
                f"Status and Configuration Information for port {port}\n"
                f"Power Enable : Yes\nPoE Port Status : Delivering\n"
                f"PD Power Draw : {watts} W\nedge-lab#"
            ]
            responses[f"show lldp info remote-device {port}"] = [
                f"show lldp info remote-device {port}\n"
                "LLDP Remote Device Information Detail\n"
                f"Local Port : {port}\nChassisType : mac-address\n"
                "ChassisId : 00 00 00 00 00 01\nPortType : interface-name\n"
                f"PortId : eth0\nSysName : fictional-ap-{port}\n"
                "System Descr : ArubaOS (MODEL:535), Version Aruba AP\nedge-lab#"
            ]
        responses.update(network.responses.get(endpoint, {}))
        super().__init__(responses)
        self.network = network
        self.endpoint = endpoint

    def establish_connection(self) -> None:
        super().establish_connection()
        with self.network.lock:
            self.network.active += 1
            self.network.peak = max(self.network.peak, self.network.active)
        if self.network.on_connect:
            self.network.on_connect(self)

    def write_channel(self, value: str) -> None:
        super().write_channel(value)
        if self.network.on_write:
            self.network.on_write(self, value.strip())

    def disconnect(self) -> None:
        if not self.disconnected:
            with self.network.lock:
                self.network.active -= 1
        super().disconnect()

    @property
    def writes(self) -> list[str]:
        return [c[1].strip() for c in self.calls if isinstance(c, tuple) and c[0] == "write"]


class Network:
    def __init__(self, ports: dict[DeviceTarget, dict[str, float]]) -> None:
        self.ports = ports
        self.responses: dict[DeviceTarget, dict[str, list[str]]] = {}
        self.connections: list[QueryConnection] = []
        self.options: list[CollectionOptions] = []
        self.probes: list[DeviceTarget] = []
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.on_connect: Callable[[QueryConnection], None] | None = None
        self.on_write: Callable[[QueryConnection, str], None] | None = None
        self.probe_target: DeviceTarget | None = None
        self.fingerprint = sha256_fingerprint(b"synthetic-poe-query-key")

    def probe(self, endpoint: DeviceTarget, *, timeout: float = 15.0) -> HostKeyObservation:
        assert timeout == 15
        self.probes.append(endpoint)
        return HostKeyObservation(self.probe_target or endpoint, "ssh-ed25519", self.fingerprint)

    def session(
        self,
        endpoint: DeviceTarget,
        credentials: Credentials,
        options: CollectionOptions,
        store: HostKeyStore,
    ) -> PortControlSSHSession:
        assert credentials is CREDENTIALS
        store.require_trusted(HostKeyObservation(endpoint, "ssh-ed25519", self.fingerprint))
        self.options.append(options)
        connection = QueryConnection(self, endpoint)
        self.connections.append(connection)
        return PortControlSSHSession(
            endpoint,
            credentials,
            options,
            store,
            connection_builder=lambda *args: connection,
            clock=Clock(),
            sleeper=lambda _: None,
        )

    def service(self, tmp_path: Path, **kwargs: Any) -> Any:
        cls = importlib.import_module("aruba2930f_backup.poe_query").PoEQueryService
        return cls(
            HostKeyStore(tmp_path / "known_hosts.json"),
            host_key_probe=self,
            session_factory=self.session,
            **kwargs,
        )


def test_requested_pairs_use_one_session_and_fresh_status_per_port(tmp_path: Path) -> None:
    a, b = DeviceTarget("192.0.2.1"), DeviceTarget("192.0.2.2", 2222)
    network = Network({a: {"24": 0, "1/3": 4.2}, b: {"7": 8.5}})
    groups = (target(a.ip, "24,1/3"), target(b.ip, "7", b.port))
    delivered: list[Any] = []
    rows = network.service(tmp_path).collect(
        groups, CREDENTIALS, approve_host_key=lambda check: True, on_result=delivered.append
    )
    assert [(r.group.target, r.interface, r.status.value, r.snapshot.watts) for r in rows] == [
        (a, "24", "ok", 0),
        (a, "1/3", "ok", 4.2),
        (b, "7", "ok", 8.5),
    ]
    assert set(id(r) for r in delivered if r.complete) == set(id(r) for r in rows)
    assert len(network.connections) == 2
    for connection in network.connections:
        assert connection.disconnected
        expected = ["show version", "show modules", "show system information"]
        expected.append("show interfaces brief")
        for port in network.ports[connection.endpoint]:
            expected.append(f"show power-over-ethernet {port}")
        expected += [
            f"show lldp info remote-device {p}" for p in network.ports[connection.endpoint]
        ]
        assert connection.writes == expected
        setup = [c[1] for c in connection.calls if isinstance(c, tuple) and c[0] == "setup"]
        assert setup == ["no page", "terminal width 511"]
    for row in rows:
        assert datetime.fromisoformat(row.snapshot.observed_at).utcoffset().total_seconds() == 0
        assert datetime.fromisoformat(row.finished_at) >= datetime.fromisoformat(
            row.snapshot.observed_at
        )
    assert rows[0].snapshot.observed_at != rows[1].snapshot.observed_at
    for options in network.options:
        assert (
            options.max_attempts,
            options.connect_timeout_seconds,
            options.command_timeout_seconds,
            options.max_output_bytes,
            options.max_output_lines,
            options.retry_delays_seconds,
        ) == (1, 15, 20, 1024 * 1024, 10_000, ())


def test_system_name_has_priority_over_prompt_for_every_port(tmp_path: Path) -> None:
    endpoint = DeviceTarget("192.0.2.1")
    network = Network({endpoint: {"1": 3.0, "2": 8.0}})
    rows = network.service(tmp_path).collect(
        [target(endpoint.ip, "1,2")], CREDENTIALS, approve_host_key=lambda _: True
    )
    assert [item.snapshot.hostname for item in rows] == ["DIRECT-SW-2930F", "DIRECT-SW-2930F"]
    assert network.connections[0].writes.count("show system information") == 1


def test_duplicate_endpoint_merges_ports_and_preserves_other_ssh_endpoint(tmp_path: Path) -> None:
    a, b = DeviceTarget("192.0.2.1"), DeviceTarget("192.0.2.1", 2222)
    network = Network({a: {"1": 1, "2": 2}, b: {"3": 3}})
    rows = network.service(tmp_path).collect(
        [target(a.ip, "1"), target(a.ip, "1,2"), target(b.ip, "3", b.port)],
        CREDENTIALS,
        approve_host_key=lambda check: True,
    )
    assert [(r.group.target, r.interface) for r in rows] == [(a, "1"), (a, "2"), (b, "3")]
    assert len(network.connections) == 2


def test_malformed_port_fails_locally_and_later_port_is_still_read(tmp_path: Path) -> None:
    a = DeviceTarget("192.0.2.1")
    network = Network({a: {"1": 1, "2": 2}})
    network.responses[a] = {
        "show power-over-ethernet 1": [
            "show power-over-ethernet 1\nStatus and Configuration Information for port 99\n"
            "Power Enable : No\nedge-lab#"
        ]
    }
    rows = network.service(tmp_path).collect(
        [target(a.ip, "1,2")], CREDENTIALS, approve_host_key=lambda check: True
    )
    assert [r.status.value for r in rows] == ["failed", "ok"]
    assert rows[0].snapshot is None
    assert rows[0].error_code.value == "COMMAND_REJECTED"
    assert rows[1].snapshot.watts == 2


@pytest.mark.parametrize("kind", ["model", "sfp", "explicit"])
def test_confirmed_unsupported_has_no_watts_and_is_distinct_from_failure(
    tmp_path: Path,
    kind: str,
) -> None:
    a = DeviceTarget("192.0.2.1")
    network = Network({a: {"1": 0}})
    if kind == "model":
        network.responses[a] = {
            "show version": ["show version\nAruba JL253A 2930F\nedge-lab#"],
            "show modules": ["show modules\nChassis: Aruba 2930F JL253A\nedge-lab#"],
        }
    elif kind == "sfp":
        network.responses[a] = {
            "show interfaces brief": [
                "show interfaces brief\nPort Type | Alert Enabled Status Mode\n"
                "1 SFP+SR | No Yes Down Auto\nedge-lab#"
            ]
        }
    else:
        network.responses[a] = {
            "show power-over-ethernet 1": [
                "show power-over-ethernet 1\nThis is not a PoE port\nedge-lab#"
            ]
        }
    rows = network.service(tmp_path).collect(
        [target(a.ip, "1")], CREDENTIALS, approve_host_key=lambda check: True
    )
    assert rows[0].status.value == "unsupported"
    assert rows[0].snapshot.watts is None
    assert rows[0].snapshot.poe_enabled is None
    assert rows[0].error_code is None
    if kind != "explicit":
        assert not any("power-over-ethernet" in c for c in network.connections[0].writes)


def test_cli_rejection_is_failed_not_unsupported_and_session_can_continue(tmp_path: Path) -> None:
    a = DeviceTarget("192.0.2.1")
    network = Network({a: {"1": 1, "2": 2}})
    network.responses[a] = {
        "show power-over-ethernet 1": ["show power-over-ethernet 1\nPermission denied\nedge-lab#"]
    }
    rows = network.service(tmp_path).collect(
        [target(a.ip, "1,2")], CREDENTIALS, approve_host_key=lambda check: True
    )
    assert [r.status.value for r in rows] == ["failed", "ok"]
    assert rows[0].error_code.value == "COMMAND_REJECTED"
    assert rows[0].snapshot is None
    assert len(network.connections) == 1


@pytest.mark.parametrize("kind", ["auth", "identity", "transport", "unexpected"])
def test_group_failure_does_not_erase_other_groups_or_reuse_uncertain_session(
    tmp_path: Path,
    kind: str,
) -> None:
    from paramiko.ssh_exception import AuthenticationException

    a, b = DeviceTarget("192.0.2.1"), DeviceTarget("192.0.2.2")
    network = Network({a: {"1": 1, "2": 2}, b: {"7": 7}})
    if kind == "auth":

        def fail_auth(connection: QueryConnection) -> None:
            if connection.endpoint == a:
                raise AuthenticationException("secret-query-password")

        network.on_connect = fail_auth
    elif kind == "identity":
        network.responses[a] = {"show modules": ["show modules\nAruba 6300 CX Switch\nedge-lab#"]}
    elif kind == "transport":
        network.responses[a] = {"show power-over-ethernet 1": []}
    else:

        def fail_read(connection: QueryConnection, command: str) -> None:
            if connection.endpoint == a and command == "show power-over-ethernet 1":
                raise RuntimeError("secret-query-password")

        network.on_write = fail_read
    delivered: list[Any] = []
    rows = network.service(tmp_path).collect(
        [target(a.ip, "1,2"), target(b.ip, "7")],
        CREDENTIALS,
        approve_host_key=lambda check: True,
        on_result=delivered.append,
    )
    assert [r.status.value for r in rows] == ["failed", "failed", "ok"]
    assert len([r for r in delivered if r.complete]) == 3
    expected = {
        "auth": "AUTH_FAILED",
        "identity": "MODEL_UNSUPPORTED",
        "transport": "COMMAND_TIMEOUT",
        "unexpected": "SSH_NEGOTIATION_FAILED",
    }
    assert all(r.error_code.value == expected[kind] and r.snapshot is None for r in rows[:2])
    assert all("secret-query-password" not in r.message for r in rows)
    assert rows[2].snapshot.watts == 7
    assert len(network.connections) == 2
    assert all(c.disconnected for c in network.connections)
    failed = next(c for c in network.connections if c.endpoint == a)
    assert "show power-over-ethernet 2" not in failed.writes


@pytest.mark.parametrize("kind", ["bytes", "lines"])
def test_output_limit_terminates_remaining_ports_without_retry(tmp_path: Path, kind: str) -> None:
    a = DeviceTarget("192.0.2.1")
    network = Network({a: {"1": 1, "2": 2}})
    oversized = "X" * (1024 * 1024 + 1) if kind == "bytes" else "x\n" * 10_001
    network.responses[a] = {"show power-over-ethernet 1": [oversized + "\nedge-lab#"]}
    rows = network.service(tmp_path).collect(
        [target(a.ip, "1,2")], CREDENTIALS, approve_host_key=lambda check: True
    )
    assert [r.error_code.value for r in rows] == ["OUTPUT_LIMIT_EXCEEDED"] * 2
    assert len(network.connections) == 1
    assert "show power-over-ethernet 2" not in network.connections[0].writes


def test_cancel_before_work_emits_every_row_without_probe_or_connect(tmp_path: Path) -> None:
    a = DeviceTarget("192.0.2.1")
    network = Network({a: {"1": 1, "2": 2}})
    cancel = threading.Event()
    cancel.set()
    delivered: list[Any] = []
    rows = network.service(tmp_path).collect(
        [target(a.ip, "1,2")],
        CREDENTIALS,
        approve_host_key=lambda check: True,
        on_result=delivered.append,
        cancel_event=cancel,
    )
    assert [r.status.value for r in rows] == ["cancelled"] * 2
    assert all(r.error_code.value == "CANCELLED" and r.snapshot is None for r in rows)
    assert delivered == list(rows)
    assert not network.probes and not network.connections


@pytest.mark.parametrize(
    "command",
    [
        "connect",
        "no page",
        "terminal width 511",
        "show version",
        "show modules",
        "show interfaces brief",
        "show power-over-ethernet 1",
    ],
)
def test_cancel_between_bounded_operations_prevents_the_next_command(
    tmp_path: Path,
    command: str,
) -> None:
    a = DeviceTarget("192.0.2.1")
    network = Network({a: {"1": 1, "2": 2}})
    cancel = threading.Event()
    if command == "connect":
        network.on_connect = lambda connection: cancel.set()
    elif command in {"no page", "terminal width 511"}:
        # Exercise real session preparation, changing only the fake network's response.
        original = QueryConnection.send_command

        def send_setup(self: QueryConnection, text: str, **kwargs: Any) -> str:
            response = original(self, text, **kwargs)
            if text == command:
                cancel.set()
            return response

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(QueryConnection, "send_command", send_setup)
            rows = network.service(tmp_path).collect(
                [target(a.ip, "1,2")],
                CREDENTIALS,
                approve_host_key=lambda check: True,
                cancel_event=cancel,
            )
    else:
        network.on_write = lambda connection, text: cancel.set() if text == command else None
    if command not in {"no page", "terminal width 511"}:
        rows = network.service(tmp_path).collect(
            [target(a.ip, "1,2")],
            CREDENTIALS,
            approve_host_key=lambda check: True,
            cancel_event=cancel,
        )
    assert [r.status.value for r in rows] == ["cancelled"] * 2
    connection = network.connections[0]
    assert connection.disconnected
    assert "show power-over-ethernet 2" not in connection.writes
    if command == "connect":
        assert "enable" not in connection.calls
    elif command in connection.writes:
        assert connection.writes[-1] == command


def test_cancel_after_incremental_result_keeps_completed_row_and_cancels_queued_work(
    tmp_path: Path,
) -> None:
    a, b = DeviceTarget("192.0.2.1"), DeviceTarget("192.0.2.2")
    network = Network({a: {"1": 1, "2": 2}, b: {"7": 7}})
    cancel = threading.Event()
    delivered: list[Any] = []

    def receive(row: Any) -> None:
        delivered.append(row)
        cancel.set()

    rows = network.service(tmp_path, max_workers=1).collect(
        [target(a.ip, "1,2"), target(b.ip, "7")],
        CREDENTIALS,
        approve_host_key=lambda check: True,
        on_result=receive,
        cancel_event=cancel,
    )
    assert [r.status.value for r in rows] == ["ok", "cancelled", "cancelled"]
    assert len([r for r in delivered if r.complete]) == 3 and len(network.connections) == 1
    assert "show power-over-ethernet 2" not in network.connections[0].writes


def test_cancel_during_approval_does_not_save_approval_or_authenticate(tmp_path: Path) -> None:
    a = DeviceTarget("192.0.2.1")
    network = Network({a: {"1": 1, "2": 2}})
    cancel = threading.Event()
    service = network.service(tmp_path)

    def approve(check: Any) -> bool:
        assert check.target == a
        cancel.set()
        return True

    rows = service.collect(
        [target(a.ip, "1,2")], CREDENTIALS, approve_host_key=approve, cancel_event=cancel
    )
    assert [r.status.value for r in rows] == ["cancelled"] * 2
    assert not service.host_key_store.list_approved()
    assert not network.connections


def test_bad_or_changed_probe_cannot_approve_a_different_endpoint(tmp_path: Path) -> None:
    a, b = DeviceTarget("192.0.2.1"), DeviceTarget("192.0.2.99")
    network = Network({a: {"1": 1}})
    network.probe_target = b
    calls: list[Any] = []
    rows = network.service(tmp_path).collect(
        [target(a.ip, "1")], CREDENTIALS, approve_host_key=lambda check: calls.append(check) or True
    )
    assert rows[0].error_code.value == "HOST_KEY_REJECTED"
    assert not calls and not network.connections


def test_changed_pinned_key_cannot_be_approved(tmp_path: Path) -> None:
    a = DeviceTarget("192.0.2.1")
    network = Network({a: {"1": 1}})
    service = network.service(tmp_path)
    observation = HostKeyObservation(a, "ssh-ed25519", sha256_fingerprint(b"old-key"))
    service.host_key_store.approve([service.host_key_store.check(observation)])
    calls: list[Any] = []
    rows = service.collect(
        [target(a.ip, "1")], CREDENTIALS, approve_host_key=lambda check: calls.append(check) or True
    )
    assert rows[0].error_code.value == "HOST_KEY_CHANGED"
    assert not calls and not network.connections


def test_three_concurrent_endpoints_and_incremental_out_of_order_delivery(tmp_path: Path) -> None:
    endpoints = [DeviceTarget(f"192.0.2.{n}") for n in range(1, 7)]
    network = Network({endpoint: {"1": float(i)} for i, endpoint in enumerate(endpoints)})
    first_three, release = threading.Event(), threading.Event()

    def block_first(connection: QueryConnection, command: str) -> None:
        if command == "show power-over-ethernet 1" and connection.endpoint in endpoints[:3]:
            if network.active == 3:
                first_three.set()
            assert release.wait(3)

    network.on_write = block_first
    result: list[Any] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            result.extend(
                network.service(tmp_path).collect(
                    [target(e.ip, "1") for e in endpoints],
                    CREDENTIALS,
                    approve_host_key=lambda check: True,
                )
            )
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert first_three.wait(3)
        assert network.active == network.peak == len(network.connections) == 3
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not failures
    assert [r.group.target for r in result] == endpoints
    assert all(r.status.value == "ok" for r in result)
    assert network.peak == 3


def test_host_key_approval_is_serialized_and_callbacks_deliver_as_ready(tmp_path: Path) -> None:
    endpoints = [DeviceTarget(f"192.0.2.{n}") for n in range(1, 4)]
    network = Network({endpoint: {"1": 1} for endpoint in endpoints})
    approval_active = 0
    approval_peak = 0
    lock = threading.Lock()

    def approve(check: Any) -> bool:
        nonlocal approval_active, approval_peak
        with lock:
            approval_active += 1
            approval_peak = max(approval_peak, approval_active)
        threading.Event().wait(0.01)
        with lock:
            approval_active -= 1
        return True

    first_waiting, other_delivered = threading.Event(), threading.Event()

    def delay_first(connection: QueryConnection, command: str) -> None:
        if connection.endpoint == endpoints[0] and command == "show power-over-ethernet 1":
            first_waiting.set()
            assert other_delivered.wait(3)

    network.on_write = delay_first
    delivered: list[Any] = []

    def receive(row: Any) -> None:
        delivered.append(row)
        if row.group.target != endpoints[0]:
            assert first_waiting.wait(3)
            other_delivered.set()

    rows = network.service(tmp_path).collect(
        [target(e.ip, "1") for e in endpoints],
        CREDENTIALS,
        approve_host_key=approve,
        on_result=receive,
    )
    assert approval_peak == 1
    assert delivered[0].group.target != endpoints[0]
    assert [r.group.target for r in rows] == endpoints


@pytest.mark.parametrize("workers", [0, 4, True])
def test_worker_count_cannot_exceed_policy(tmp_path: Path, workers: int) -> None:
    with pytest.raises(ValueError):
        Network({}).service(tmp_path, max_workers=workers)


def test_poe_first_progressive_updates_then_lldp_in_same_session(tmp_path: Path) -> None:
    a = DeviceTarget("192.0.2.1")
    network = Network({a: {"1": 8.7, "2": 2}})
    delivered: list[Any] = []
    rows = network.service(tmp_path).collect(
        [target(a.ip, "1,2")],
        CREDENTIALS,
        approve_host_key=lambda check: True,
        on_result=delivered.append,
    )
    assert len(delivered) == 4
    assert [(r.interface, r.complete) for r in delivered] == [
        ("1", False),
        ("2", False),
        ("1", True),
        ("2", True),
    ]
    assert all(r.lldp.status.value == "pending" and not r.finished_at for r in delivered[:2])
    assert all(r.complete and r.lldp.status.value == "ok" for r in rows)
    assert all(r.identity.hostname == "edge-lab" for r in delivered)
    assert rows[0].snapshot is delivered[0].snapshot
    assert rows[0].poe_finished_at == delivered[0].poe_finished_at
    assert rows[0].lldp.neighbors[0].system_name == "fictional-ap-1"
    assert len(network.connections) == 1
    writes = network.connections[0].writes
    assert writes.index("show power-over-ethernet 2") < writes.index(
        "show lldp info remote-device 1"
    )
    assert writes[-2:] == ["show lldp info remote-device 1", "show lldp info remote-device 2"]


@pytest.mark.parametrize("kind", ["cli", "malformed", "timeout", "cancel"])
def test_lldp_failure_preserves_all_poe_rows_and_identity(tmp_path: Path, kind: str) -> None:
    a = DeviceTarget("192.0.2.1")
    network = Network({a: {"1": 8.7, "2": 2, "3": 3}})
    cancel = threading.Event()
    command = "show lldp info remote-device 1"
    if kind == "cli":
        network.responses[a] = {command: [f"{command}\nPermission denied\nedge-lab#"]}
    elif kind == "malformed":
        network.responses[a] = {command: [f"{command}\nnot lldp\nedge-lab#"]}
    elif kind == "timeout":
        network.responses[a] = {command: []}
    else:
        network.on_write = lambda connection, text: cancel.set() if text == command else None
    delivered: list[Any] = []
    rows = network.service(tmp_path).collect(
        [target(a.ip, "1,2,3")],
        CREDENTIALS,
        approve_host_key=lambda check: True,
        on_result=delivered.append,
        cancel_event=cancel,
    )
    assert [r.interface for r in rows] == ["1", "2", "3"]
    assert [r.snapshot.watts for r in rows] == [8.7, 2, 3]
    assert all(r.status.value == "ok" and r.complete for r in rows)
    assert all(r.identity.hostname == "edge-lab" and r.poe_finished_at for r in rows)
    assert all(rows[i].snapshot is delivered[i].snapshot for i in range(3))
    assert rows[0].lldp.status.value == ("cancelled" if kind == "cancel" else "failed")
    if kind in {"cli", "malformed"}:
        assert [r.lldp.status.value for r in rows[1:]] == ["ok", "ok"]
    else:
        assert all(r.lldp.status == rows[0].lldp.status for r in rows)
        assert "show lldp info remote-device 2" not in network.connections[0].writes
    assert network.connections[0].disconnected


def test_all_poe_rejections_keep_identity_and_still_query_lldp(tmp_path: Path) -> None:
    a = DeviceTarget("192.0.2.1")
    network = Network({a: {"1": 1, "2": 2}})
    network.responses[a] = {
        f"show power-over-ethernet {p}": [
            f"show power-over-ethernet {p}\nPermission denied\nedge-lab#"
        ]
        for p in ("1", "2")
    }
    rows = network.service(tmp_path).collect(
        [target(a.ip, "1,2")],
        CREDENTIALS,
        approve_host_key=lambda check: True,
    )
    assert all(r.status.value == "failed" and r.snapshot is None for r in rows)
    assert all(r.identity.hostname == "edge-lab" and r.complete for r in rows)
    assert all(r.lldp.status.value == "ok" and len(r.lldp.neighbors) == 1 for r in rows)


def test_unsupported_poe_still_reads_lldp(tmp_path: Path) -> None:
    a = DeviceTarget("192.0.2.1")
    network = Network({a: {"1": 1}})
    network.responses[a] = {
        "show version": ["show version\nAruba JL253A 2930F\nedge-lab#"],
        "show modules": ["show modules\nChassis: Aruba 2930F JL253A\nedge-lab#"],
    }
    (row,) = network.service(tmp_path).collect(
        [target(a.ip, "1")],
        CREDENTIALS,
        approve_host_key=lambda check: True,
    )
    assert row.status.value == "unsupported"
    assert row.lldp.status.value == "ok"
    assert row.lldp.neighbors[0].system_name == "fictional-ap-1"


def test_transport_loss_in_later_poe_preserves_earlier_measurement(tmp_path: Path) -> None:
    a = DeviceTarget("192.0.2.1")
    network = Network({a: {"1": 8.7, "2": 2, "3": 3}})
    network.responses[a] = {"show power-over-ethernet 2": []}
    delivered: list[Any] = []
    rows = network.service(tmp_path).collect(
        [target(a.ip, "1,2,3")],
        CREDENTIALS,
        approve_host_key=lambda check: True,
        on_result=delivered.append,
    )
    assert [r.status.value for r in rows] == ["ok", "failed", "failed"]
    assert all(r.complete and r.identity.hostname == "edge-lab" for r in rows)
    assert rows[0].snapshot is delivered[0].snapshot
    assert rows[0].snapshot.watts == 8.7
    assert all(r.lldp.status.value == "failed" for r in rows)
    assert not any("lldp" in cmd for cmd in network.connections[0].writes)
    assert "show power-over-ethernet 3" not in network.connections[0].writes


def test_power_evidence_uses_single_poe_read_and_survives_lldp_rejection(tmp_path: Path) -> None:
    from decimal import Decimal

    a = DeviceTarget("192.0.2.1")
    network = Network({a: {"1": 8.7}})
    command = "show power-over-ethernet 1"
    network.responses[a] = {
        command: [
            f"{command}\nStatus and Configuration Information for port 1\n"
            "Power Enable : Yes\nPoE Port Status : Delivering\n"
            "PLC Class/Type : 4/2\nDLC Class/Type : 4/2\nAlloc By Actual : lldp\n"
            "PD Requested Power Value : 25.5 W\nPSE Allocated Power Value : 25.5 W\n"
            "PD Power Draw : 8.7 W\nedge-lab#"
        ],
        "show lldp info remote-device 1": [
            "show lldp info remote-device 1\nPermission denied\nedge-lab#"
        ],
    }
    (row,) = network.service(tmp_path).collect(
        [target(a.ip, "1")],
        CREDENTIALS,
        approve_host_key=lambda check: True,
    )
    assert row.power_evidence.requested_watts == Decimal("25.5")
    assert row.power_evidence.allocated_watts == Decimal("25.5")
    assert row.snapshot.watts == 8.7
    assert row.lldp.status.value == "failed"
    assert row.status.value == "ok" and row.complete
    assert row.assessment is not None
    assert network.connections[0].writes.count(command) == 1


def test_slow_host_key_probes_do_not_serialize_other_switches(tmp_path: Path) -> None:
    """Probe I/O must not occupy the shared, operator-approval-only lock."""
    from aruba2930f_backup.poe_query import PoEQueryService

    class ParallelProbe:
        def __init__(self) -> None:
            self.barrier = threading.Barrier(2)
            self.lock = threading.Lock()
            self.active = 0
            self.peak = 0

        def probe(self, target: DeviceTarget, *, timeout: float = 15.0) -> HostKeyObservation:
            del timeout
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                self.barrier.wait(timeout=3)
            finally:
                with self.lock:
                    self.active -= 1
            return HostKeyObservation(
                target, "ssh-ed25519", sha256_fingerprint(target.ip.encode("ascii"))
            )

    probe = ParallelProbe()
    service = PoEQueryService(
        host_key_store=HostKeyStore(tmp_path / "known_hosts.json"), host_key_probe=probe
    )
    failures: list[Exception] = []
    endpoints = (DeviceTarget("192.0.2.71"), DeviceTarget("192.0.2.72"))

    def check(target: DeviceTarget) -> None:
        try:
            service._trusted_key(target, lambda _check: True, threading.Event())
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=check, args=(endpoint,)) for endpoint in endpoints]
    for worker in threads:
        worker.start()
    for worker in threads:
        worker.join(4)
    assert not any(worker.is_alive() for worker in threads)
    assert not failures
    assert probe.peak == 2
    assert len(service.host_key_store.list_approved()) == 2
