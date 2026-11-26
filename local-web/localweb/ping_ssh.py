"""Read-only Ping over the existing pinned ArubaOS-Switch SSH transport.

This adapter is not an API or scheduler. Its caller must own the existing
device/global operation leases, obtain the session credentials and host-key
approval through the existing service, and prepare an EXEC session.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from aruba2930f_backup.models import CollectionFailure, DeviceIdentity, ErrorCode
from aruba2930f_backup.ssh import (
    CancellationSignal,
    NetmikoSSHSession,
    _mapped_failure,
    _strip_command_and_prompt,
)
from aruba2930f_backup.validation import identify_connected_device, validate_cli_response

from .ping_source import parse_owned_ipv4
from .ping_tools import (
    Destination,
    PingObservation,
    PingPlan,
    parse_ping_output,
    preview_command,
    verify_source,
)

# Documented initial CLI profiles. Do not extrapolate to older WC releases.
_SUPPORTED_VERSION = re.compile(r"WC\.16\.(?:10|11)\.[0-9]{4}")


@dataclass(frozen=True)
class PingPairResult:
    identity: DeviceIdentity
    observation: PingObservation
    requested_source: str | None
    verified_source_ip: str | None
    command_may_have_started: bool
    transport_error: ErrorCode | None = None


def _check_cancel(cancel: CancellationSignal | None) -> None:
    if cancel is not None and cancel.is_set():
        raise CollectionFailure(ErrorCode.CANCELLED, "Ping 작업이 취소되었습니다.")


class PingSSHSession(NetmikoSSHSession):
    """No new arbitrary-command public API; each Ping is generated from a plan."""

    def _require_prepared(self) -> str:
        self._require_connection()
        if not self._paging_prepared:
            raise CollectionFailure(ErrorCode.PAGING_SETUP_FAILED, "페이지 출력 해제가 필요합니다.")
        if not self._terminal_width_prepared:
            raise CollectionFailure(ErrorCode.COMMAND_REJECTED, "터미널 출력 너비 확인이 필요합니다.")
        return self._require_cached_prompt()

    def _source_table(self, *, vlan: bool, cancel: CancellationSignal | None) -> str:
        command = "show vlans" if vlan else "show ip"
        try:
            prompt = self._require_prepared()
            connection = self._require_connection()
            _check_cancel(cancel)
            connection.clear_buffer()
            _check_cancel(cancel)
            connection.write_channel(connection.normalize_cmd(command))
            raw = self._read_until_prompt(prompt, cancel_event=cancel, command=command)
            output = _strip_command_and_prompt(raw, command, prompt)
            validate_cli_response(command, output)
            return output
        except Exception as exc:
            failure = _mapped_failure(exc, during="command")
            self.close()
            raise failure from exc

    def _identify(self, cancel: CancellationSignal | None) -> DeviceIdentity:
        _check_cancel(cancel)
        version = self.send_show("show version", cancel_event=cancel)
        _check_cancel(cancel)
        try:
            modules = self.send_show("show modules", cancel_event=cancel)
        except CollectionFailure as exc:
            if exc.code is not ErrorCode.COMMAND_REJECTED:
                raise
            modules = ""
        _check_cancel(cancel)
        identity = identify_connected_device(
            version,
            modules,
            lambda: self.send_show("show stacking", cancel_event=cancel),
        )
        if not _SUPPORTED_VERSION.fullmatch(identity.software_version or ""):
            raise CollectionFailure(
                ErrorCode.MODEL_UNSUPPORTED,
                "이 AOS-Switch 버전의 Ping CLI 호환성을 확인하지 못했습니다.",
            )
        return identity

    def run_pair(
        self, plan: PingPlan, destination: Destination, *, cancel: CancellationSignal | None = None
    ) -> PingPairResult:
        """Execute at most one Ping command, with no automatic replay or fallback.

        Exceptions before the Ping write are preflight failures, not packet
        observations. After the write is attempted, an uncertain transport
        outcome is returned as unknown, never as a measured timeout.
        A verified source is ownership evidence, not a packet capture.
        """
        command = preview_command(plan, self.target, destination)
        requested = plan.requested_source(self.target)
        if self._options.command_timeout_seconds < plan.options.command_timeout:
            raise ValueError("Ping 명령 실행 제한 시간이 충분하지 않습니다.")
        self._require_prepared()
        try:
            identity = self._identify(cancel)
        except Exception:
            self.close()
            raise
        verified_ip = None
        if requested is not None:
            try:
                vlans = self._source_table(vlan=True, cancel=cancel)
                addresses = self._source_table(vlan=False, cancel=cancel)
                owned = parse_owned_ipv4(vlans, addresses, completed=True)
                verify_source(plan, self.target, owned)
                verified_ip = (
                    next(ip for vid, ip in owned if vid == plan.options.vlan)
                    if plan.options.source_mode == "vlan"
                    else requested
                )
            except ValueError:
                self.close()
                return PingPairResult(
                    identity, PingObservation("source_error"), requested, None, False
                )
            except CollectionFailure as exc:
                self.close()
                if exc.code is not ErrorCode.COMMAND_REJECTED:
                    raise
                return PingPairResult(
                    identity, PingObservation("source_error"), requested, None, False, exc.code
                )
        may_have_started = False
        try:
            prompt = self._require_prepared()
            connection = self._require_connection()
            _check_cancel(cancel)
            connection.clear_buffer()
            _check_cancel(cancel)
            wire = connection.normalize_cmd(command)
            may_have_started = True
            connection.write_channel(wire)
            raw = self._read_until_prompt(prompt, cancel_event=cancel, command=command)
            output = _strip_command_and_prompt(raw, command, prompt)
            observation = parse_ping_output(
                output, destination.ip, plan.options.repetitions, completed=True
            )
            # Retain the parser's conservative classification. The common
            # rejection checker can also detect unsupported CLI error variants.
            if observation.status == "unknown":
                try:
                    validate_cli_response(command, output)
                except CollectionFailure:
                    observation = PingObservation("cli_error")
            return PingPairResult(identity, observation, requested, verified_ip, True)
        except Exception as exc:
            failure = _mapped_failure(exc, during="command")
            self.close()
            if not may_have_started:
                raise failure from exc
            return PingPairResult(
                identity,
                PingObservation("unknown"),
                requested,
                verified_ip,
                True,
                failure.code,
            )
