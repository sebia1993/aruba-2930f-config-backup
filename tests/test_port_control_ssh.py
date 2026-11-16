from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aruba2930f_backup.hostkeys import HostKeyStore
from aruba2930f_backup.models import (
    CollectionFailure,
    CollectionOptions,
    Credentials,
    DeviceTarget,
    ErrorCode,
)
from tests.test_ssh import FakeConnection


class PortConnection(FakeConnection):
    def __init__(self, responses: dict[str, list[str]] | None = None) -> None:
        super().__init__(responses)
        self.prompt = "edge-lab#"

    def find_prompt(self) -> str:
        return self.prompt

    def write_channel(self, value: str) -> None:
        self.calls.append(("write", value))
        command = value.strip()
        if command in self.responses:
            self.current_chunks = list(self.responses[command])
            return
        prompt = "edge-lab#" if command == "end" else "edge-lab(config)#"
        self.current_chunks = [f"{command}\n{prompt}"]

    @property
    def writes(self) -> list[str]:
        return [
            call[1].strip() for call in self.calls if isinstance(call, tuple) and call[0] == "write"
        ]


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 0.05
        return self.value


def make_port_session(tmp_path: Path, connection: PortConnection, **kwargs: Any) -> Any:
    from aruba2930f_backup.port_control_ssh import PortControlSSHSession

    session = PortControlSSHSession(
        DeviceTarget("192.0.2.20"),
        Credentials("operator", "test-password"),
        CollectionOptions(command_timeout_seconds=0.5, **kwargs),
        HostKeyStore(tmp_path / "known_hosts.json"),
        connection_builder=lambda *args: connection,
        clock=Clock(),
        sleeper=lambda _: None,
    )
    session.connect()
    session.enter_enable()
    session.disable_paging()
    session.set_terminal_width()
    return session


@pytest.mark.parametrize(
    ("action", "command"),
    [
        ("poe_off", "no interface 1/24 power-over-ethernet"),
        ("poe_on", "interface 1/24 power-over-ethernet"),
        ("disable", "interface 1/24 disable"),
        ("enable", "interface 1/24 enable"),
    ],
)
def test_one_allowed_change_and_return_to_exec(tmp_path: Path, action: str, command: str) -> None:
    connection = PortConnection()
    session = make_port_session(tmp_path, connection)
    session.change_port("1/24", action)
    assert connection.writes == ["configure terminal", command, "end"]
    assert session.get_prompt() == "edge-lab#"
    assert not connection.disconnected


@pytest.mark.parametrize(
    "port",
    ["0", "01", "all", "1-4", "1,2", "1/1/1", "1\nend", " 1", "1 ", "1;reload", "1234567890123"],
)
def test_invalid_interface_never_reaches_channel(tmp_path: Path, port: str) -> None:
    connection = PortConnection()
    session = make_port_session(tmp_path, connection)
    with pytest.raises(ValueError):
        session.change_port(port, "disable")
    with pytest.raises(ValueError):
        session.read_poe(port)
    assert connection.writes == []


@pytest.mark.parametrize("action", ["reload", "write memory", "enable\nreload", "POE_OFF", ""])
def test_arbitrary_actions_never_reach_channel(tmp_path: Path, action: str) -> None:
    connection = PortConnection()
    session = make_port_session(tmp_path, connection)
    with pytest.raises(ValueError):
        session.change_port("24", action)
    assert connection.writes == []


def test_fixed_reads_return_verified_body(tmp_path: Path) -> None:
    connection = PortConnection(
        {
            "show interfaces brief": ["show interfaces brief\n24 100/1000T | No Yes Up\nedge-lab#"],
            "show power-over-ethernet 24": [
                "show power-over-ethernet 24\nPower Enable : No\nedge-lab#"
            ],
        }
    )
    session = make_port_session(tmp_path, connection)
    assert session.read_interfaces() == "24 100/1000T | No Yes Up"
    assert session.read_poe("24") == "Power Enable : No"
    assert connection.writes == ["show interfaces brief", "show power-over-ethernet 24"]


@pytest.mark.parametrize("method", ["read_interfaces", "change_port"])
def test_preparation_required_before_port_operations(tmp_path: Path, method: str) -> None:
    connection = PortConnection()
    session = make_port_session(tmp_path, connection)
    session.enter_enable()  # This invalidates the completed preparation.
    with pytest.raises(CollectionFailure):
        getattr(session, method)(*([] if method == "read_interfaces" else ["24", "disable"]))
    assert connection.writes == []


@pytest.mark.parametrize("prompt", ["edge-lab>", "(edge-lab)#", "edge lab#", "#"])
def test_unsupported_control_prompt_is_rejected_before_write(tmp_path: Path, prompt: str) -> None:
    connection = PortConnection()
    session = make_port_session(tmp_path, connection)
    session._prompt = prompt
    with pytest.raises(CollectionFailure):
        session.change_port("24", "disable")
    assert connection.writes == []


@pytest.mark.parametrize(
    ("failed_command", "expected_writes"),
    [
        ("configure terminal", ["configure terminal"]),
        ("interface 24 disable", ["configure terminal", "interface 24 disable"]),
        ("end", ["configure terminal", "interface 24 disable", "end"]),
    ],
)
def test_timeout_closes_session_without_replaying_change(
    tmp_path: Path, failed_command: str, expected_writes: list[str]
) -> None:
    connection = PortConnection({failed_command: []})
    session = make_port_session(tmp_path, connection)
    with pytest.raises(CollectionFailure) as caught:
        session.change_port("24", "disable")
    assert caught.value.code is ErrorCode.COMMAND_TIMEOUT
    assert not caught.value.transient  # This operation must never enter backup retry logic.
    assert connection.writes == expected_writes
    assert connection.disconnected


def test_config_rejection_never_sends_port_change(tmp_path: Path) -> None:
    connection = PortConnection(
        {"configure terminal": ["configure terminal\nPermission denied\nedge-lab#"]}
    )
    session = make_port_session(tmp_path, connection)
    with pytest.raises(CollectionFailure):
        session.change_port("24", "disable")
    assert connection.writes == ["configure terminal"]
    assert connection.disconnected


def test_mutation_cli_error_is_sanitized_and_not_replayed(tmp_path: Path) -> None:
    connection = PortConnection(
        {
            "interface 24 disable": [
                "interface 24 disable\nInvalid input: private-detail\nedge-lab(config)#"
            ]
        }
    )
    session = make_port_session(tmp_path, connection)
    with pytest.raises(CollectionFailure) as caught:
        session.change_port("24", "disable")
    assert caught.value.code is ErrorCode.COMMAND_REJECTED
    assert "private-detail" not in str(caught.value)
    assert connection.writes == ["configure terminal", "interface 24 disable"]
    assert connection.disconnected


def test_port_read_retains_output_bounds(tmp_path: Path) -> None:
    connection = PortConnection({"show interfaces brief": ["x" * 256]})
    session = make_port_session(tmp_path, connection, max_output_bytes=128)
    with pytest.raises(CollectionFailure) as caught:
        session.read_interfaces()
    assert caught.value.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
    assert connection.disconnected


def test_inherited_backup_allowlist_does_not_expand(tmp_path: Path) -> None:
    connection = PortConnection()
    session = make_port_session(tmp_path, connection)
    for command in ("show interfaces brief", "configure terminal", "interface 24 disable"):
        with pytest.raises(ValueError):
            session.send_show(command)
    assert connection.writes == []


def test_lldp_read_uses_exact_validated_readonly_command(tmp_path: Path) -> None:
    command = "show lldp info remote-device 1/24"
    connection = PortConnection(
        {command: [f"{command}\nLLDP Remote Device Information Detail\nedge-lab#"]}
    )
    session = make_port_session(tmp_path, connection)
    assert session.read_lldp("1/24") == "LLDP Remote Device Information Detail"
    assert connection.writes == [command]
    with pytest.raises(ValueError):
        session.send_show(command)


@pytest.mark.parametrize("port", ["0", "01", "1-4", "1,2", "1/1/1", "1\nreload", " 1", "1;reload"])
def test_lldp_rejects_invalid_port_before_channel(tmp_path: Path, port: str) -> None:
    connection = PortConnection()
    session = make_port_session(tmp_path, connection)
    with pytest.raises(ValueError):
        session.read_lldp(port)
    assert connection.writes == []


def test_lldp_timeout_closes_bounded_session(tmp_path: Path) -> None:
    connection = PortConnection({"show lldp info remote-device 24": []})
    session = make_port_session(tmp_path, connection)
    with pytest.raises(CollectionFailure) as caught:
        session.read_lldp("24")
    assert caught.value.code is ErrorCode.COMMAND_TIMEOUT
    assert connection.disconnected


def test_lldp_preparation_required_before_channel(tmp_path: Path) -> None:
    connection = PortConnection()
    session = make_port_session(tmp_path, connection)
    session.enter_enable()
    with pytest.raises(CollectionFailure):
        session.read_lldp("24")
    assert connection.writes == []


@pytest.mark.parametrize("bound", ["bytes", "lines"])
def test_lldp_output_limits_close_session(tmp_path: Path, bound: str) -> None:
    command = "show lldp info remote-device 24"
    output = "x" * 256 if bound == "bytes" else "x\n" * 11
    connection = PortConnection({command: [output]})
    options = {"max_output_bytes": 128} if bound == "bytes" else {"max_output_lines": 10}
    session = make_port_session(tmp_path, connection, **options)
    with pytest.raises(CollectionFailure) as caught:
        session.read_lldp("24")
    assert caught.value.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
    assert connection.disconnected


def test_lldp_cli_rejection_keeps_verified_session_for_next_port(tmp_path: Path) -> None:
    first, second = "show lldp info remote-device 1", "show lldp info remote-device 2"
    connection = PortConnection(
        {
            first: [f"{first}\nPermission denied\nedge-lab#"],
            second: [f"{second}\nLLDP Remote Device Information Detail\nedge-lab#"],
        }
    )
    session = make_port_session(tmp_path, connection)
    with pytest.raises(CollectionFailure) as caught:
        session.read_lldp("1")
    assert caught.value.code is ErrorCode.COMMAND_REJECTED
    assert not connection.disconnected
    assert session.read_lldp("2") == "LLDP Remote Device Information Detail"
    assert connection.writes == [first, second]
