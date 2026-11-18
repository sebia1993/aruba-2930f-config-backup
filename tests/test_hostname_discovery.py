"""Read-only ArubaOS-Switch Hostname resolution regression coverage."""

from __future__ import annotations

import pytest

from aruba2930f_backup.models import CollectionFailure, ErrorCode


class HostnameSession:
    def __init__(self, *, prompt: str, system: str | Exception, config: str | Exception = ""):
        self.prompt = prompt
        self.responses: dict[str, str | Exception] = {
            "show system information": system,
            "show running-config": config,
        }
        self.calls: list[str] = []

    def get_prompt(self) -> str:
        self.calls.append("get_prompt")
        return self.prompt

    def send_show(self, command: str) -> str:
        self.calls.append(command)
        reply = self.responses[command]
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_system_name_has_priority_over_exec_prompt_alias() -> None:
    from aruba2930f_backup.hostname_discovery import resolve_switch_hostname

    session = HostnameSession(
        prompt="alias-1#",
        system="Status and Counters - General System Information\r\nSystem Name : SEOUL-SW-2930F\r\n",
    )
    observed = resolve_switch_hostname(session)
    assert (observed.hostname, observed.source) == ("SEOUL-SW-2930F", "system-information")
    assert session.calls == ["show system information"]


def test_unsupported_system_command_falls_back_to_exec_prompt_without_config_read() -> None:
    from aruba2930f_backup.hostname_discovery import resolve_switch_hostname

    session = HostnameSession(
        prompt="SW-BASEMENT-01#",
        system=CollectionFailure(ErrorCode.COMMAND_REJECTED, "Unsupported"),
    )
    observed = resolve_switch_hostname(session)
    assert (observed.hostname, observed.source) == ("SW-BASEMENT-01", "prompt")
    assert session.calls == ["show system information", "get_prompt"]


def test_unusable_prompt_falls_back_to_banner_safe_running_config() -> None:
    from aruba2930f_backup.hostname_discovery import resolve_switch_hostname

    session = HostnameSession(
        prompt="(Aruba 2930F) #",
        system="System Name   : \r\n",
        config='hostname "서울 RND SW 01"\nbanner motd ^\nhostname forged\n^\n',
    )
    observed = resolve_switch_hostname(session)
    assert (observed.hostname, observed.source) == ("서울 RND SW 01", "running-config")
    assert session.calls == ["show system information", "get_prompt", "show running-config"]


def test_unsupported_fallback_stays_unavailable_when_no_host_name() -> None:
    from aruba2930f_backup.hostname_discovery import resolve_switch_hostname

    session = HostnameSession(
        prompt="(Aruba 2930F) #",
        system=CollectionFailure(ErrorCode.COMMAND_REJECTED, "Permission denied"),
        config=CollectionFailure(ErrorCode.COMMAND_REJECTED, "No permission"),
    )
    observed = resolve_switch_hostname(session)
    assert (observed.hostname, observed.source) == (None, "unavailable")


def test_transport_timeout_is_not_silently_swallowed_by_hostname_fallback() -> None:
    from aruba2930f_backup.hostname_discovery import resolve_switch_hostname

    session = HostnameSession(
        prompt="edge-lab#",
        system=CollectionFailure(ErrorCode.COMMAND_TIMEOUT, "Transport timeout"),
    )
    with pytest.raises(CollectionFailure) as exc:
        resolve_switch_hostname(session)
    assert exc.value.code is ErrorCode.COMMAND_TIMEOUT
    assert session.calls == ["show system information"]


def test_cancel_before_next_fallback_read_prevents_next_ssh_command() -> None:
    from aruba2930f_backup.hostname_discovery import resolve_switch_hostname

    session = HostnameSession(prompt="(Aruba 2930F) #", system="System Name :\n")
    count = 0

    def check_cancel() -> None:
        nonlocal count
        count += 1
        if count == 2:
            raise CollectionFailure(ErrorCode.CANCELLED, "Canceled")

    with pytest.raises(CollectionFailure) as exc:
        resolve_switch_hostname(session, before_read=check_cancel)
    assert exc.value.code is ErrorCode.CANCELLED
    assert session.calls == ["show system information"]


def test_oversize_config_is_not_accepted_as_hostname_evidence() -> None:
    from aruba2930f_backup.hostname_discovery import resolve_switch_hostname

    session = HostnameSession(
        prompt="bad prompt#",
        system="Status and Counters\nSystem Name :\n",
        config="hostname forged\n" + "X" * (1024 * 1024 + 1),
    )
    observed = resolve_switch_hostname(session)
    assert observed.hostname is None
    assert observed.source == "unavailable"
