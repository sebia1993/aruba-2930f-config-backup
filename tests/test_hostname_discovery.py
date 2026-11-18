"""Hostname lookup safety and fallbacks without network connections."""

from __future__ import annotations

import pytest

from aruba2930f_backup.hostname_discovery import discover_hostname
from aruba2930f_backup.models import CollectionFailure, ErrorCode


def test_direct_system_name_wins_even_with_legacy_prompt() -> None:
    commands: list[str] = []

    def read(command: str) -> str:
        commands.append(command)
        assert command == "show system information"
        return "Status and Counters - General System Information\nSystem Name : CAMPUS-SW-01\n"

    assert discover_hostname(read, lambda: "unrelated#") == "CAMPUS-SW-01"
    assert commands == ["show system information"]


def test_unavailable_system_info_falls_back_to_prompt_without_config_read() -> None:
    commands: list[str] = []

    def read(command: str) -> str:
        commands.append(command)
        raise CollectionFailure(ErrorCode.COMMAND_REJECTED, "Not permitted")

    assert discover_hostname(read, lambda: "BACKUP-SW#") == "BACKUP-SW"
    assert commands == ["show system information"]


def test_empty_name_uses_existing_running_config_parser_last() -> None:
    commands: list[str] = []

    def read(command: str) -> str:
        commands.append(command)
        if command == "show system information":
            return "System Name : \nSystem Contact :\n"
        if command == "show running-config":
            return 'Running configuration:\nhostname "CAMPUS F3 SW"\nvlan 1\n'
        raise AssertionError(f"Unexpected CLI command: {command}")

    assert discover_hostname(read, lambda: "(Aruba 2930F) #") == "CAMPUS F3 SW"
    assert commands == ["show system information", "show running-config"]


def test_missing_all_sources_returns_unknown_without_unsafe_guessing() -> None:
    def read(command: str) -> str:
        if command == "show system information":
            return "System Name : \n"
        raise CollectionFailure(ErrorCode.COMMAND_REJECTED, "Not permitted")

    assert discover_hostname(read, lambda: "(Aruba 2930F) #") is None


def test_transport_failure_never_reuses_a_broken_connection() -> None:
    calls: list[str] = []

    def read(command: str) -> str:
        calls.append(command)
        raise CollectionFailure(ErrorCode.COMMAND_TIMEOUT, "timeout")

    with pytest.raises(CollectionFailure) as exc:
        discover_hostname(read, lambda: "switch#")
    assert exc.value.code is ErrorCode.COMMAND_TIMEOUT
    assert calls == ["show system information"]
