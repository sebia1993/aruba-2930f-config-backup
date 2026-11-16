from __future__ import annotations

import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from aruba2930f_backup.hostkeys import HostKeyStore, sha256_fingerprint
from aruba2930f_backup.models import (
    CollectionFailure,
    CollectionOptions,
    Credentials,
    DeviceTarget,
    DiagnosticPhase,
    ErrorCode,
    HostKeyObservation,
)
from aruba2930f_backup.ssh import NetmikoSSHSession, _build_pinned_connection


class FakeConnection:
    def __init__(self, responses: dict[str, list[str]] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[object] = []
        self.current_chunks: list[str] = []
        self.deferred_chunks: list[str] = []
        self.disconnected = False

    def _modify_connection_params(self) -> None:
        self.calls.append("modify")

    def establish_connection(self) -> None:
        self.calls.append("establish")

    def set_base_prompt(self) -> None:
        self.calls.append("base_prompt")

    def find_prompt(self) -> str:
        self.calls.append("find_prompt")
        return "edge-lab#"

    def enable(self) -> None:
        self.calls.append("enable")

    def check_enable_mode(self) -> bool:
        return True

    def send_command(self, command: str, **kwargs: Any) -> str:
        self.calls.append(("setup", command, kwargs))
        return f"{command}\nedge-lab#"

    def clear_buffer(self) -> None:
        self.calls.append("clear")

    def normalize_cmd(self, command: str) -> str:
        return f"{command}\n"

    def write_channel(self, value: str) -> None:
        self.calls.append(("write", value))
        if value == " " and self.deferred_chunks:
            self.current_chunks.extend(self.deferred_chunks)
            self.deferred_chunks = []
            return
        command = value.strip()
        if command.startswith("show "):
            chunks = list(self.responses.get(command, [f"{command}\nedge-lab#"]))
            if len(chunks) > 1 and "next page: Space" in chunks[0]:
                self.current_chunks = chunks[:1]
                self.deferred_chunks = chunks[1:]
            else:
                self.current_chunks = chunks

    def read_channel(self) -> str:
        return self.current_chunks.pop(0) if self.current_chunks else ""

    def disconnect(self) -> None:
        self.calls.append("disconnect")
        self.disconnected = True


def make_session(
    tmp_path: Path,
    connection: FakeConnection,
    *,
    options: CollectionOptions | None = None,
    clock: Any = None,
    sleeper: Any = None,
) -> NetmikoSSHSession:
    kwargs: dict[str, Any] = {}
    if clock is not None:
        kwargs["clock"] = clock
    if sleeper is not None:
        kwargs["sleeper"] = sleeper
    return NetmikoSSHSession(
        DeviceTarget("192.0.2.20"),
        Credentials("operator", "session-password", "enable-password"),
        options or CollectionOptions(),
        HostKeyStore(tmp_path / "known_hosts.json"),
        connection_builder=lambda target, credentials, opts, store: connection,
        **kwargs,
    )


def prepare_session(session: NetmikoSSHSession) -> None:
    session.disable_paging()
    session.set_terminal_width()


def test_explicit_setup_and_show_read_order(tmp_path) -> None:
    connection = FakeConnection(
        {
            "show version": ["show version\r\nAruba JL253A 2930F\r\nedge-lab#"],
        }
    )
    session = make_session(tmp_path, connection)

    session.connect()
    session.enter_enable()
    session.disable_paging()
    session.set_terminal_width()
    output = session.send_show("show version")

    setup_commands = [
        call[1] for call in connection.calls if isinstance(call, tuple) and call[0] == "setup"
    ]
    writes = [
        call[1] for call in connection.calls if isinstance(call, tuple) and call[0] == "write"
    ]
    assert setup_commands == ["no page", "terminal width 511"]
    assert writes == ["show version\n"]
    assert output == "Aruba JL253A 2930F"
    assert connection.calls.index("modify") < connection.calls.index("establish")


def test_aruba_login_banner_is_advanced_and_control_codes_are_removed(tmp_path) -> None:
    class BannerConnection(FakeConnection):
        RETURN = "\n"

        def __init__(self) -> None:
            super().__init__()
            self.ansi_escape_codes = False
            self.banner_reads = [
                "\x1b[2J" + ("x" * 50_000) + "Copyright Hewlett Packard Enterprise",
                "Press any key to continue",
                "\x1b[2Kedge-lab#",
            ]

        def read_until_pattern(self, *, pattern: str, read_timeout: float) -> str:
            self.calls.append(("read_until_pattern", pattern, read_timeout))
            return self.banner_reads.pop(0)

        def find_prompt(self) -> str:
            self.calls.append("find_prompt")
            return "\x1b[2Kedge-labX\x08#"

    connection = BannerConnection()
    session = make_session(tmp_path, connection)

    session.connect()

    assert connection.ansi_escape_codes is True
    assert session.get_prompt() == "edge-lab#"
    assert connection.calls.count("find_prompt") == 1
    assert ("write", "\n") in connection.calls
    assert connection.banner_reads == []


def test_login_banner_timeout_after_enter_has_specific_detail(tmp_path) -> None:
    class ReadTimeout(Exception):
        pass

    class StuckBannerConnection(FakeConnection):
        RETURN = "\n"

        def __init__(self) -> None:
            super().__init__()
            self.reads = 0

        def read_until_pattern(self, *, pattern: str, read_timeout: float) -> str:
            del pattern, read_timeout
            self.reads += 1
            if self.reads == 1:
                return "Copyright"
            if self.reads == 2:
                return "Press any key to continue"
            raise ReadTimeout

    session = make_session(tmp_path, StuckBannerConnection())

    with pytest.raises(CollectionFailure) as captured:
        session.connect()

    assert captured.value.code is ErrorCode.PROMPT_PARSE_FAILED
    assert captured.value.diagnostic_detail.value == "login_banner_pending"
    assert captured.value.diagnostic_phase is DiagnosticPhase.SESSION_SETUP


def test_prompt_read_error_has_specific_detail(tmp_path) -> None:
    connection = FakeConnection()

    class ReadTimeout(Exception):
        pass

    def fail_prompt() -> str:
        raise ReadTimeout("raw device text must not cross the boundary")

    connection.find_prompt = fail_prompt  # type: ignore[method-assign]
    session = make_session(tmp_path, connection)

    with pytest.raises(CollectionFailure) as captured:
        session.connect()

    assert captured.value.code is ErrorCode.PROMPT_PARSE_FAILED
    assert captured.value.diagnostic_detail.value == "prompt_read_error"
    assert captured.value.diagnostic_phase is DiagnosticPhase.SESSION_SETUP


def test_setup_show_and_get_prompt_reuse_the_verified_prompt(tmp_path) -> None:
    connection = FakeConnection({"show version": ["show version\nAruba JL253A 2930F\nedge-lab#"]})
    session = make_session(tmp_path, connection)

    session.connect()
    prepare_session(session)
    session.send_show("show version")
    prompt = session.get_prompt()

    assert prompt == "edge-lab#"
    assert connection.calls.count("find_prompt") == 1


@pytest.mark.parametrize(
    "prompt",
    (
        "Aruba-2930F-48G-PoE+ #",
        "(Aruba 2930F) #",
        "branch switch >",
    ),
)
def test_complex_exec_prompt_is_cached_as_an_exact_opaque_terminator(
    tmp_path: Path,
    prompt: str,
) -> None:
    connection = FakeConnection()

    def find_prompt() -> str:
        connection.calls.append("find_prompt")
        return prompt

    connection.find_prompt = find_prompt  # type: ignore[method-assign]

    def setup(command: str, **kwargs: Any) -> str:
        assert kwargs["expect_string"] == re.escape(prompt)
        return f"{command}\n{prompt}"

    connection.send_command = setup  # type: ignore[method-assign]
    session = make_session(tmp_path, connection)

    session.connect()
    session.disable_paging()

    assert session.get_prompt() == prompt
    assert connection.calls.count("find_prompt") == 1


def test_split_and_delayed_prompt_is_read_to_exact_completion(tmp_path) -> None:
    connection = FakeConnection(
        {
            "show version": [
                "show version\nAruba JL253A 2930F",
                "",
                "\nedge-",
                "",
                "lab#",
            ]
        }
    )
    session = make_session(tmp_path, connection)
    session.connect()
    prepare_session(session)

    assert session.send_show("show version") == "Aruba JL253A 2930F"


def test_setup_command_requires_the_exact_cached_prompt(tmp_path) -> None:
    connection = FakeConnection()

    def mismatched(command: str, **kwargs: Any) -> str:
        del kwargs
        return f"{command}\nedge-lab#\nother-device#"

    connection.send_command = mismatched  # type: ignore[method-assign]
    session = make_session(tmp_path, connection)
    session.connect()

    with pytest.raises(CollectionFailure) as captured:
        session.disable_paging()

    assert captured.value.code is ErrorCode.PAGING_SETUP_FAILED
    assert captured.value.transient
    assert captured.value.diagnostic_detail.value == "prompt_mismatch"


def test_disable_paging_rejects_cli_error_and_does_not_hide_details(tmp_path) -> None:
    connection = FakeConnection()

    def rejected(command: str, **kwargs: Any) -> str:
        del kwargs
        return f"{command}\n% Invalid input\nedge-lab#"

    connection.send_command = rejected  # type: ignore[method-assign]
    session = make_session(tmp_path, connection)
    session.connect()

    with pytest.raises(CollectionFailure) as captured:
        session.disable_paging()

    assert captured.value.code is ErrorCode.PAGING_SETUP_FAILED
    assert "Invalid input" not in captured.value.safe_message


@pytest.mark.parametrize("prompt", ("edge-lab(config)#", "edge-lab(vlan-10)#"))
def test_configuration_mode_prompt_sends_no_setup_or_show_commands(
    tmp_path: Path,
    prompt: str,
) -> None:
    connection = FakeConnection()
    connection.find_prompt = lambda: prompt  # type: ignore[method-assign]
    session = make_session(tmp_path, connection)

    with pytest.raises(CollectionFailure) as captured:
        session.connect()

    assert captured.value.code is ErrorCode.PROMPT_PARSE_FAILED
    assert not any(
        isinstance(call, tuple) and call[0] in {"setup", "write"} for call in connection.calls
    )


def test_residual_pager_is_bounded_and_advanced(tmp_path) -> None:
    connection = FakeConnection(
        {
            "show running-config": [
                "show running-config\nline 1\n"
                "-- MORE --, next page: Space, next line: Enter, quit: Control-C",
                "\nline 2\nedge-lab#",
            ]
        }
    )
    session = make_session(tmp_path, connection)
    session.connect()
    prepare_session(session)

    output = session.send_show("show running-config")

    writes = [
        call[1] for call in connection.calls if isinstance(call, tuple) and call[0] == "write"
    ]
    assert writes == ["show running-config\n", " "]
    assert "MORE" not in output
    assert output.splitlines() == ["line 1", "line 2"]


def test_pager_words_inside_config_are_preserved_without_space_write(tmp_path) -> None:
    connection = FakeConnection(
        {
            "show running-config": [
                'show running-config\nbanner motd "Press any key to continue"\n'
                "; literal -- MORE -- text\nedge-lab#"
            ]
        }
    )
    session = make_session(tmp_path, connection)
    session.connect()
    prepare_session(session)

    output = session.send_show("show running-config")

    writes = [
        call[1] for call in connection.calls if isinstance(call, tuple) and call[0] == "write"
    ]
    assert writes == ["show running-config\n"]
    assert 'banner motd "Press any key to continue"' in output
    assert "; literal -- MORE -- text" in output


def test_literal_plain_more_line_at_chunk_boundary_is_not_pager_control(tmp_path) -> None:
    connection = FakeConnection(
        {
            "show running-config": [
                "show running-config\nbanner motd ^\n-- MORE --",
                "\n^\nedge-lab#",
            ]
        }
    )
    session = make_session(tmp_path, connection)
    session.connect()
    prepare_session(session)

    output = session.send_show("show running-config")

    writes = [
        call[1] for call in connection.calls if isinstance(call, tuple) and call[0] == "write"
    ]
    assert writes == ["show running-config\n"]
    assert "-- MORE --" in output.splitlines()


def test_output_limit_stops_channel_read(tmp_path) -> None:
    connection = FakeConnection({"show version": ["show version\n1234567890\nedge-lab#"]})
    session = make_session(
        tmp_path,
        connection,
        options=CollectionOptions(max_output_bytes=10, max_output_lines=100),
    )
    session.connect()
    prepare_session(session)

    with pytest.raises(CollectionFailure) as captured:
        session.send_show("show version")

    assert captured.value.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED


def test_command_timeout_is_transient(tmp_path) -> None:
    current = [0.0]

    def clock() -> float:
        return current[0]

    def sleeper(seconds: float) -> None:
        current[0] += seconds

    session = make_session(
        tmp_path,
        FakeConnection({"show version": []}),
        options=CollectionOptions(command_timeout_seconds=0.1),
        clock=clock,
        sleeper=sleeper,
    )
    session.connect()
    prepare_session(session)

    with pytest.raises(CollectionFailure) as captured:
        session.send_show("show version")

    assert captured.value.code is ErrorCode.COMMAND_TIMEOUT
    assert captured.value.transient


def test_only_show_commands_are_allowed(tmp_path) -> None:
    session = make_session(tmp_path, FakeConnection())
    session.connect()

    with pytest.raises(ValueError, match="read-only"):
        session.send_show("configure terminal")

    with pytest.raises(ValueError, match="registered"):
        session.send_show("show running-config | include password")


def test_show_requires_verified_paging_and_width_preparation(tmp_path) -> None:
    session = make_session(tmp_path, FakeConnection())
    session.connect()

    with pytest.raises(CollectionFailure) as paging:
        session.send_show("show version")
    assert paging.value.code is ErrorCode.PAGING_SETUP_FAILED

    session.disable_paging()
    with pytest.raises(CollectionFailure) as width:
        session.send_show("show version")
    assert width.value.code is ErrorCode.COMMAND_REJECTED


def test_actual_authenticated_driver_uses_pinned_policy(tmp_path, monkeypatch) -> None:
    store = HostKeyStore(tmp_path / "known_hosts.json")
    target = DeviceTarget("192.0.2.30", 2222)

    class FakeKey:
        def __init__(self, value: bytes) -> None:
            self.value = value

        def get_name(self) -> str:
            return "ssh-ed25519"

        def asbytes(self) -> bytes:
            return self.value

    expected_key = FakeKey(b"approved-key")
    observation = HostKeyObservation(
        target,
        expected_key.get_name(),
        sha256_fingerprint(expected_key.asbytes()),
    )
    store.approve([store.check(observation)])

    class FakeSSHClient:
        def __init__(self) -> None:
            self.policy: Any = None

        def set_missing_host_key_policy(self, policy: Any) -> None:
            self.policy = policy

    class FakeHPProcurveSSH:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    paramiko = types.ModuleType("paramiko")
    paramiko.SSHClient = FakeSSHClient  # type: ignore[attr-defined]
    netmiko = types.ModuleType("netmiko")
    hp = types.ModuleType("netmiko.hp")
    hp_procurve = types.ModuleType("netmiko.hp.hp_procurve")
    hp_procurve.HPProcurveSSH = FakeHPProcurveSSH  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paramiko", paramiko)
    monkeypatch.setitem(sys.modules, "netmiko", netmiko)
    monkeypatch.setitem(sys.modules, "netmiko.hp", hp)
    monkeypatch.setitem(sys.modules, "netmiko.hp.hp_procurve", hp_procurve)

    connection = _build_pinned_connection(
        target,
        Credentials("operator", "password"),
        CollectionOptions(),
        store,
    )
    client = connection._build_ssh_client()

    assert connection.kwargs["device_type"] == "aruba_osswitch"
    assert connection.kwargs["auto_connect"] is False
    assert connection.kwargs["disabled_algorithms"] == {
        "keys": ["ssh-rsa"],
        "pubkeys": ["ssh-rsa"],
    }
    client.policy.missing_host_key(client, target.ip, expected_key)
    with pytest.raises(CollectionFailure) as changed:
        client.policy.missing_host_key(client, target.ip, FakeKey(b"changed-key"))
    assert changed.value.code is ErrorCode.HOST_KEY_CHANGED


def test_credentials_repr_never_contains_secrets() -> None:
    credentials = Credentials("operator", "password-value", "enable-value")

    rendered = repr(credentials)
    assert "password-value" not in rendered
    assert "enable-value" not in rendered


def test_prompt_inside_banner_waits_through_idle_reads_for_remaining_config(tmp_path) -> None:
    connection = FakeConnection(
        {
            "show running-config": [
                'show running-config\r\nRunning configuration:\r\nhostname "edge-lab"\r\n'
                "banner motd ^\r\nedge-lab#",
                *([""] * 12),
                "\r\n^\r\ninterface 24\r\n   disable\r\nexit\r\nedge-",
                "",
                "lab#",
            ]
        }
    )
    session = make_session(tmp_path, connection, sleeper=lambda _: None)
    session.connect()
    prepare_session(session)

    assert session.send_show("show running-config") == (
        'Running configuration:\nhostname "edge-lab"\nbanner motd ^\nedge-lab#\n^\n'
        "interface 24\n   disable\nexit"
    )


@pytest.mark.parametrize(
    "banner", ["banner motd ^\nunfinished", 'banner motd "unfinished', "banner motd #\nunfinished"]
)
def test_unclosed_banner_cannot_use_tail_prompt_as_completion(tmp_path, banner) -> None:
    now = [0.0]
    connection = FakeConnection(
        {"show running-config": [f'show running-config\nhostname "edge-lab"\n{banner}\nedge-lab#']}
    )
    session = make_session(
        tmp_path,
        connection,
        options=CollectionOptions(command_timeout_seconds=0.2),
        clock=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    session.connect()
    prepare_session(session)

    with pytest.raises(CollectionFailure) as captured:
        session.send_show("show running-config")
    assert captured.value.code in {ErrorCode.COMMAND_TIMEOUT, ErrorCode.PROMPT_PARSE_FAILED}


@pytest.mark.parametrize(
    "banner",
    [
        'banner motd "Welcome"',
        'banner motd "Welcome\\" operator\\nnext line"',
        'banner motd "Welcome\nedge-lab#\nclosing text"',
        "banner motd ^Welcome^",
        "banner motd ^\nWelcome\n^",
        "banner motd #Welcome#",
        "no banner motd",
    ],
)
def test_supported_banner_forms_survive_prompt_boundary_check(tmp_path, banner) -> None:
    connection = FakeConnection(
        {"show running-config": [f"show running-config\n{banner}\ninterface 24\nedge-", "", "lab#"]}
    )
    session = make_session(tmp_path, connection, sleeper=lambda _: None)
    session.connect()
    prepare_session(session)
    assert session.send_show("show running-config") == f"{banner}\ninterface 24"


def test_running_config_without_command_echo_is_not_verified(tmp_path) -> None:
    now = [0.0]
    session = make_session(
        tmp_path,
        FakeConnection({"show running-config": ['hostname "edge-lab"\nedge-lab#']}),
        options=CollectionOptions(command_timeout_seconds=0.2),
        clock=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    session.connect()
    prepare_session(session)
    with pytest.raises(CollectionFailure) as captured:
        session.send_show("show running-config")
    assert captured.value.code in {ErrorCode.COMMAND_TIMEOUT, ErrorCode.PROMPT_PARSE_FAILED}


def test_every_response_split_has_identical_text_saved_bytes_and_hash(tmp_path) -> None:
    import hashlib

    from aruba2930f_backup.storage import write_config_atomic
    from aruba2930f_backup.validation import normalize_config_text

    wire = 'show running-config\r\nhostname "edge-lab"\r\nbanner motd "hello"\r\ninterface 24\r\nedge-lab#\r'
    expected = 'hostname "edge-lab"\nbanner motd "hello"\ninterface 24'
    payload = b'hostname "edge-lab"\r\nbanner motd "hello"\r\ninterface 24\r\n'
    for index in range(len(wire) + 1):
        connection = FakeConnection({"show running-config": [wire[:index], "", wire[index:], ""]})
        session = make_session(tmp_path, connection, sleeper=lambda _: None)
        session.connect()
        prepare_session(session)
        output = session.send_show("show running-config")
        assert output == expected, index
        stored = write_config_atomic(tmp_path / "partition.txt", normalize_config_text(output))
        assert stored.path.read_bytes() == payload, index
        assert stored.sha256 == hashlib.sha256(payload).hexdigest(), index
        session.close()


def test_open_banner_remains_cancellable_without_returning_partial_config(tmp_path) -> None:
    import threading

    cancelled = threading.Event()
    now = [0.0]

    def sleeper(seconds: float) -> None:
        now[0] += seconds
        if now[0] >= 0.15:
            cancelled.set()

    connection = FakeConnection(
        {
            "show running-config": [
                "show running-config\nbanner motd ^\nedge-lab#",
                "",
                "",
                "",
            ]
        }
    )
    session = make_session(tmp_path, connection, clock=lambda: now[0], sleeper=sleeper)
    session.connect()
    prepare_session(session)
    with pytest.raises(CollectionFailure) as captured:
        session.send_show("show running-config", cancel_event=cancelled)
    assert captured.value.code is ErrorCode.CANCELLED
    assert connection.disconnected
    assert now[0] < 0.3


@pytest.mark.parametrize(
    "chunks",
    [
        [
            "show running-config\r\nbanner motd ^\r\nWelcome\r\n"
            "-- MORE --, next page: Space, next line: Enter, quit: Control-C\r",
            "\n^\r\ninterface 24\r\nedge-lab#",
        ],
        [
            "show running-config\nbanner motd ^\n",
            "-- MORE --, next page: Space, next line: Enter, quit: Control-C",
            "",
            "",
            "",
            "\n^\ninterface 24\nedge-lab#",
        ],
    ],
)
def test_ambiguous_pager_inside_banner_cannot_delete_text_and_succeed(tmp_path, chunks) -> None:
    connection = FakeConnection({"show running-config": chunks})
    session = make_session(tmp_path, connection, sleeper=lambda _: None)
    session.connect()
    prepare_session(session)
    with pytest.raises(CollectionFailure) as captured:
        session.send_show("show running-config")
    assert captured.value.code is ErrorCode.PROMPT_PARSE_FAILED
    assert ("write", " ") not in connection.calls


def test_split_ansi_backspace_and_mixed_line_endings_preserve_response(tmp_path) -> None:
    wire = 'show running-config\r\n\x1b[0mhostname "edge-lab"\rinterface 24X\x08\nedge-lab#'
    for index in range(len(wire) + 1):
        connection = FakeConnection({"show running-config": [wire[:index], "", wire[index:]]})
        session = make_session(tmp_path, connection, sleeper=lambda _: None)
        session.connect()
        prepare_session(session)
        assert session.send_show("show running-config") == 'hostname "edge-lab"\ninterface 24', (
            index
        )
        session.close()


@pytest.mark.parametrize(
    "echo", ["show running-config", "edge-lab#show running-config", "edge-lab# show running-config"]
)
def test_echo_after_whitespace_only_leading_lines_remains_supported(tmp_path, echo) -> None:
    now = [0.0]
    session = make_session(
        tmp_path,
        FakeConnection({"show running-config": [f"  \r\n{echo}\r\nhostname lab\r\nedge-lab#"]}),
        options=CollectionOptions(command_timeout_seconds=0.2),
        clock=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    session.connect()
    prepare_session(session)
    assert session.send_show("show running-config") == "hostname lab"


def test_residual_pager_outside_banner_keeps_pending_cr_for_next_chunk(tmp_path) -> None:
    connection = FakeConnection(
        {
            "show running-config": [
                "show running-config\r\nhostname lab\r\n"
                "-- MORE --, next page: Space, next line: Enter, quit: Control-C\r",
                "\ninterface 24\r\nedge-lab#",
            ]
        }
    )
    session = make_session(tmp_path, connection, sleeper=lambda _: None)
    session.connect()
    prepare_session(session)
    assert session.send_show("show running-config") == "hostname lab\ninterface 24"
    assert ("write", " ") in connection.calls
