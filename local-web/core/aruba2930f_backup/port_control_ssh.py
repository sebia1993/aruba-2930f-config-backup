"""Explicit, bounded single-port control over the existing pinned SSH transport.

The backup session's ``send_show`` allowlist is deliberately unchanged. Model,
target and operator approval are enforced by the port-control service before it
calls ``change_port``. This adapter only accepts four fixed operations.
"""

from __future__ import annotations

import re

from .models import CollectionFailure, ErrorCode
from .ssh import NetmikoSSHSession, _mapped_failure, _strip_command_and_prompt
from .validation import validate_cli_response

_INTERFACE_RE = re.compile(r"[1-9][0-9]*(?:/[1-9][0-9]*)?", re.ASCII)
_CONTROL_PROMPT_RE = re.compile(r"[A-Za-z0-9_.+-]{1,255}#", re.ASCII)
_OPERATIONS = {
    "poe_off": "no interface {port} power-over-ethernet",
    "poe_on": "interface {port} power-over-ethernet",
    "disable": "interface {port} disable",
    "enable": "interface {port} enable",
}


def validate_port_identifier(value: str) -> str:
    """Reject ranges, lists, alternate numbering and command injection as-is."""

    if not isinstance(value, str) or len(value) > 12 or _INTERFACE_RE.fullmatch(value) is None:
        raise ValueError("인터페이스는 단일 포트 번호(예: 24, VSF 1/24)여야 합니다.")
    return value


def port_change_command(interface: str, action: str) -> str:
    """Use the same validated CLI text in operator reviews and on the SSH connection."""
    port = validate_port_identifier(interface)
    if not isinstance(action, str) or action not in _OPERATIONS:
        raise ValueError("지원하지 않는 포트 제어 동작입니다.")
    return _OPERATIONS[action].format(port=port)


class PortControlSSHSession(NetmikoSSHSession):
    """A separate command surface; no inherited backup permission is widened."""

    def read_interfaces(self) -> str:
        return self._read_port_command("show interfaces brief")

    def read_poe(self, interface: str) -> str:
        port = validate_port_identifier(interface)
        return self._read_port_command(f"show power-over-ethernet {port}")

    def read_lldp(self, interface: str) -> str:
        port = validate_port_identifier(interface)
        return self._read_port_command(f"show lldp info remote-device {port}")

    def change_port(self, interface: str, action: str) -> None:
        """Send a single generated change once, then return to the same EXEC prompt.

        Any failure closes the connection and is non-retryable. A caller must
        re-read state to resolve an indeterminate outcome; it must not replay
        this transaction after a timeout.
        """

        command = port_change_command(interface, action)
        prompt = self._require_prepared()
        if _CONTROL_PROMPT_RE.fullmatch(prompt) is None:
            raise CollectionFailure(
                ErrorCode.PROMPT_PARSE_FAILED,
                "포트 제어에 필요한 관리자 EXEC 프롬프트를 확인하지 못했습니다.",
            )
        config_prompt = f"{prompt[:-1]}(config)#"
        try:
            self._exchange("configure terminal", config_prompt)
            self._exchange(command, config_prompt)
            self._exchange("end", prompt)
            self._prompt = prompt
        except Exception as exc:
            failure = _mapped_failure(exc, during="command")
            self.close()
            raise CollectionFailure(
                failure.code,
                "포트 제어 응답을 확인하지 못했습니다. 자동 재시도 없이 현재 상태를 다시 조회하십시오.",
                diagnostic_phase=failure.diagnostic_phase,
                diagnostic_detail=failure.diagnostic_detail,
            ) from exc

    def _read_port_command(self, command: str) -> str:
        prompt = self._require_prepared()
        try:
            return self._exchange(command, prompt)
        except Exception as exc:
            failure = _mapped_failure(exc, during="command")
            # A normal CLI rejection ended at the verified prompt, so the
            # service can classify an unsupported PoE query without a hidden
            # reconnect. All uncertain transport states close the connection.
            if failure.code is not ErrorCode.COMMAND_REJECTED:
                self.close()
            raise failure from exc

    def _require_prepared(self) -> str:
        self._require_connection()
        if not self._paging_prepared:
            raise CollectionFailure(
                ErrorCode.PAGING_SETUP_FAILED,
                "포트 작업 전에 페이지 출력 해제를 확인해야 합니다.",
            )
        if not self._terminal_width_prepared:
            raise CollectionFailure(
                ErrorCode.COMMAND_REJECTED,
                "포트 작업 전에 터미널 출력 너비를 확인해야 합니다.",
            )
        return self._require_cached_prompt()

    def _exchange(self, command: str, expected_prompt: str) -> str:
        """Private primitive; public methods generate every command internally."""

        connection = self._require_connection()
        if hasattr(connection, "clear_buffer"):
            connection.clear_buffer()
        connection.write_channel(connection.normalize_cmd(command))
        raw = self._read_until_prompt(expected_prompt, cancel_event=None)
        output = _strip_command_and_prompt(raw, command, expected_prompt)
        validate_cli_response(command, output)
        return output
