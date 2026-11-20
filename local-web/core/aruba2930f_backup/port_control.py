"""Explicit, single-port changes kept separate from the read-only collector."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from itertools import pairwise
from math import isfinite
from pathlib import Path
from typing import Protocol

from .hostkeys import HostKeyProbe, HostKeyStore, ParamikoHostKeyProbe
from .hostname_discovery import resolve_switch_hostname
from .models import (
    CollectionFailure,
    CollectionOptions,
    Credentials,
    DeviceIdentity,
    DeviceTarget,
    ErrorCode,
    HostKeyCheck,
    HostKeyObservation,
    HostKeyTrustState,
)
from .storage import default_output_directory
from .validation import SUPPORTED_MODELS, identify_connected_device

_PORT_RE = re.compile(r"[1-9][0-9]*(?:/[1-9][0-9]*)?")


class PreMutationRejected(CollectionFailure):
    """Verified rejection before any disruptive port configuration command.

    Other exceptions, especially after a change command may have been sent,
    must never be reclassified as safe-to-retry preflight failures.
    """


class PortAction(StrEnum):
    POE_OFF = "poe_off"
    POE_ON = "poe_on"
    DISABLE = "disable"
    ENABLE = "enable"

    @property
    def is_poe(self) -> bool:
        return self in (PortAction.POE_OFF, PortAction.POE_ON)

    @property
    def desired_enabled(self) -> bool:
        return self in (PortAction.POE_ON, PortAction.ENABLE)


@dataclass(frozen=True, slots=True)
class PortSnapshot:
    target: DeviceTarget
    interface: str
    hostname: str
    model: str
    enabled: bool | None
    link_status: str
    poe_enabled: bool | None
    poe_status: str
    watts: float | None
    fingerprint: str = field(default="", repr=False)
    observed_at: str = ""
    admin_only: bool = False

    @property
    def confirmation_text(self) -> str:
        return f"{self.target.endpoint} / {self.interface}"


@dataclass(frozen=True, slots=True)
class PortOperationResult:
    status: str
    message: str
    before: PortSnapshot
    after: PortSnapshot | None
    audit_path: Path | None = None


@dataclass(frozen=True, slots=True)
class InterfaceState:
    port_type: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class PoEState:
    enabled: bool | None
    status: str
    watts: float | None


def validate_interface(value: str) -> str:
    """Accept one canonical AOS-S port; never expand ranges or CX identifiers."""

    if not isinstance(value, str) or len(value) > 12 or not _PORT_RE.fullmatch(value):
        raise ValueError("인터페이스는 단일 포트 번호(예: 24 또는 1/24)로 입력하세요.")
    return value


def _invalid_status(message: str) -> CollectionFailure:
    return CollectionFailure(ErrorCode.COMMAND_REJECTED, message)


def parse_interface_status(output: str, interface: str) -> InterfaceState:
    """Read only Type and administrative Enabled from the brief table."""

    validate_interface(interface)
    if not re.search(r"\bPort\s+Type\s*\|\s*Alert\s+Enabled\s+Status\b", output):
        raise _invalid_status("포트 상태 표의 열을 확인하지 못했습니다. 변경을 중단합니다.")
    port = re.escape(interface) + r"(?:-Trk[1-9][0-9]*)?"
    candidates = re.findall(rf"(?im)^[ \t]*{port}[ \t]+[^\n]*", output)
    if len(candidates) != 1:
        raise _invalid_status(
            "입력한 인터페이스를 장비의 포트 목록에서 유일하게 확인하지 못했습니다."
        )
    match = re.fullmatch(
        rf"[ \t]*{port}[ \t]+(\S+)[ \t]+\|[ \t]+"
        r"(?:Yes|No)[ \t]+(Yes|No)(?:[ \t]+[^\n]*)?",
        candidates[0].rstrip(),
        flags=re.IGNORECASE,
    )
    if match is None:
        raise _invalid_status("포트 유형과 관리 설정을 해석하지 못했습니다.")
    return InterfaceState(match[1], match[2].casefold() == "yes")


UNKNOWN_LINK = "확인 불가"
_STATUS_COLUMNS = ["port", "name", "status", "config-mode", "speed", "type", "tagged", "untagged"]


def _terminal_cells(line: str) -> str:
    """Represent the terminal width of non-ASCII names without creating ASCII status tokens."""
    cells: list[str] = []
    for char in line:
        if unicodedata.combining(char):
            if cells:
                cells[-1] = "?"
        elif unicodedata.east_asian_width(char) in {"W", "F"}:
            cells.extend("??")
        else:
            cells.append(char)
    return "".join(cells)


def parse_interface_links(output: str) -> dict[str, str]:
    """Read only the Port and Status cells of an unambiguous AOS-S status table."""
    lines = output.expandtabs().splitlines()
    headers = [i for i, line in enumerate(lines) if line.lower().split() == _STATUS_COLUMNS]
    if len(headers) != 1 or headers[0] + 1 >= len(lines):
        return {}
    header = headers[0]
    ruler = lines[header + 1]
    spans = list(re.finditer(r"-+", ruler))
    if len(spans) != len(_STATUS_COLUMNS) or not re.fullmatch(r"[ -]+", ruler):
        return {}
    if any(
        lines[header][span.start() : span.end()].strip().lower() != label
        for span, label in zip(spans, _STATUS_COLUMNS, strict=True)
    ):
        return {}

    port_span, status_span = spans[0], spans[2]
    gaps = [slice(left.end(), right.start()) for left, right in pairwise(spans)]
    config = slice(spans[3].start(), spans[3].end())
    status_cell = slice(status_span.start(), status_span.end())
    seen: set[str] = set()
    links: dict[str, str] = {}
    for line in lines[header + 2 :]:
        port = line[port_span.start() : port_span.end()].strip()
        match = re.fullmatch(r"([1-9][0-9]*(?:/[1-9][0-9]*)?)(?:-Trk[1-9][0-9]*)?", port, re.I)
        if not match or line[: port_span.start()].strip():
            continue
        interface = match[1]
        if interface in seen:
            links.pop(interface, None)
            continue
        seen.add(interface)
        # Firmware can pad a name by character count or by terminal cell width.
        # Only accept an aligned interpretation; conflicting readings are unknown.
        layouts = (line,) if line.isascii() else (line, _terminal_cells(line))
        readings = {
            layout[status_cell].strip().lower()
            for layout in layouts
            if layout[config].strip() and not any(layout[gap].strip() for gap in gaps)
        }
        if readings == {"up"}:
            links[interface] = "Up"
        elif readings == {"down"}:
            links[interface] = "Down"
    return links


def parse_poe_status(output: str, interface: str) -> PoEState:
    """Require a port-bound Power Enable value, distinct from power delivery."""

    validate_interface(interface)
    headers = re.findall(
        r"(Port Power Status|Status and Configuration Information)"
        r"[ \t]+for port[ \t]+([^\r\n]+)",
        output,
        re.IGNORECASE,
    )
    if len(headers) != 1 or headers[0][1].strip() != interface:
        raise _invalid_status("PoE 조회 결과의 대상 포트를 확인하지 못했습니다.")
    configuration_format = headers[0][0].casefold() == "status and configuration information"
    status_label = "PoE Port Status" if configuration_format else "Detection Status"
    power_label = "PD Power Draw" if configuration_format else "Power"
    enabled = re.findall(r"\bPower Enable[ \t]*:[ \t]*(Yes|No)\b", output, re.IGNORECASE)
    if len(enabled) != 1 or len(re.findall(r"\bPower Enable[ \t]*:", output, re.IGNORECASE)) != 1:
        raise _invalid_status("PoE 사용 설정을 유일하게 확인하지 못했습니다.")
    active = enabled[0].casefold() == "yes"
    detection = re.findall(
        rf"\b{status_label}[ \t]*:[ \t]*(Other Fault|Searching|Delivering|Disabled|Fault)\b",
        output,
        re.IGNORECASE,
    )
    detection_fields = re.findall(
        r"\b(Detection Status|PoE Port Status)[ \t]*:", output, re.IGNORECASE
    )
    # The configuration format places PD draw in the second column. Reserved
    # PSE power is a separate field, never the measured draw or legacy Power.
    power_output = re.sub(
        r"\bPSE[ \t]+Reserved[ \t]+Power(?=[ \t]*:)",
        "PSE_RESERVED_POWER",
        output,
        flags=re.IGNORECASE,
    )
    powers = re.findall(
        rf"\b{power_label}[ \t]*:[ \t]*([\d.]+)[ \t]*W\b",
        power_output,
        re.IGNORECASE,
    )
    power_fields = re.findall(r"\b(Power|PD Power Draw)[ \t]*:", power_output, re.IGNORECASE)
    if (
        any(label.casefold() != status_label.casefold() for label in detection_fields)
        or any(label.casefold() != power_label.casefold() for label in power_fields)
        or len(detection) != len(detection_fields)
        or len(powers) != len(power_fields)
        or len(detection) > 1
        or len(powers) > 1
        or (active and not detection)
    ):
        raise _invalid_status("PoE 공급 상태를 해석하지 못했습니다.")
    watts: float | None = None
    if powers:
        try:
            watts = float(powers[0])
        except ValueError as exc:
            raise _invalid_status("PoE 소비 전력 값을 해석하지 못했습니다.") from exc
        if not isfinite(watts) or watts < 0:
            raise _invalid_status("PoE 소비 전력 값이 올바르지 않습니다.")
    status = detection[0].title() if detection else "사용 안 함"
    return PoEState(active, status, watts)


class PortSession(Protocol):
    def connect(self) -> None: ...
    def enter_enable(self) -> None: ...
    def disable_paging(self) -> None: ...
    def set_terminal_width(self) -> None: ...
    def get_prompt(self) -> str: ...
    def send_show(self, command: str) -> str: ...
    def read_interfaces(self) -> str: ...

    def read_interface_status(self) -> str: ...
    def read_poe(self, interface: str) -> str: ...
    def read_lldp(self, interface: str) -> str: ...
    def change_port(self, interface: str, action: str) -> None: ...
    def close(self) -> None: ...


SessionFactory = Callable[[DeviceTarget, Credentials, CollectionOptions, HostKeyStore], PortSession]


def _production_session(
    target: DeviceTarget,
    credentials: Credentials,
    options: CollectionOptions,
    store: HostKeyStore,
) -> PortSession:
    from .port_control_ssh import PortControlSSHSession

    return PortControlSSHSession(target, credentials, options, store)


def _hostname_matches_for_action(
    before: PortSnapshot, after: PortSnapshot, action: PortAction
) -> bool:
    """Do not treat a missing CLI prompt hostname as a new device.

    Match two known names. PoE ON and the address-bound administrative
    disable also accept a missing name on the same pinned device.
    """
    if before.hostname == after.hostname:
        return True
    return (action is PortAction.POE_ON or before.admin_only) and (
        before.hostname in ("", "확인 불가") or after.hostname in ("", "확인 불가")
    )


def _same_settings_for_action(
    before: PortSnapshot, expected: PortSnapshot, action: PortAction
) -> bool:
    """Authoritative settings must match even if the CLI hostname is unavailable."""
    return (
        before.target == expected.target
        and before.interface == expected.interface
        and before.model == expected.model
        and before.fingerprint == expected.fingerprint
        and (expected.admin_only or before.enabled == expected.enabled)
        and (expected.admin_only or before.poe_enabled == expected.poe_enabled)
        and _hostname_matches_for_action(before, expected, action)
    )


def _check_readback(
    before: PortSnapshot, after: PortSnapshot, action: PortAction
) -> tuple[bool, str]:
    """Return whether a second read is safe, and any disagreement in this observation."""
    if (
        before.target != after.target
        or before.interface != after.interface
        or before.model != after.model
        or before.fingerprint != after.fingerprint
        or not _hostname_matches_for_action(before, after, action)
    ):
        return False, "조회한 장비 또는 포트 식별 정보가 변경 전과 다릅니다."
    if action.is_poe:
        if before.enabled != after.enabled:
            return False, "PoE 이외에 포트 사용 설정도 바뀌었습니다."
        actual = after.poe_enabled
        label = "Power Enable"
    else:
        if before.poe_enabled != after.poe_enabled:
            return False, "포트 사용 설정 이외에 PoE 사용 설정도 바뀌었습니다."
        actual = after.enabled
        label = "Enabled"
    if actual is action.desired_enabled:
        return True, ""
    observed = "확인 불가" if actual is None else ("Yes" if actual else "No")
    requested = "Yes" if action.desired_enabled else "No"
    return True, f"{label} 관측값은 {observed}이며 요청값은 {requested}입니다."


def _failure_code(exc: Exception) -> ErrorCode:
    if isinstance(exc, CollectionFailure):
        return exc.code
    return (
        ErrorCode.COMMAND_TIMEOUT if isinstance(exc, TimeoutError) else ErrorCode.UNEXPECTED_ERROR
    )


def _state_record(snapshot: PortSnapshot | None) -> dict[str, object] | None:
    if snapshot is None:
        return None
    return {
        "enabled": snapshot.enabled,
        "link_status": snapshot.link_status,
        "poe_enabled": snapshot.poe_enabled,
        "poe_status": snapshot.poe_status,
        "watts": snapshot.watts,
    }


class PortControlService:
    """Inspect, bind an operator confirmation, change once, and verify.

    This service does not use the collector's retry scheduler. A timed-out
    write may have reached the switch, so only a fresh explicit inspection can
    authorize another operation.
    """

    SNAPSHOT_MAX_AGE_SECONDS = 120.0

    def __init__(
        self,
        *,
        host_key_store: HostKeyStore | None = None,
        host_key_probe: HostKeyProbe | None = None,
        session_factory: SessionFactory | None = None,
        audit_directory: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.host_key_store = host_key_store if host_key_store is not None else HostKeyStore()
        self._probe = host_key_probe if host_key_probe is not None else ParamikoHostKeyProbe()
        self._factory = session_factory or _production_session
        self.audit_directory = audit_directory or default_output_directory() / "port-control"
        self._clock = clock
        self._lock = threading.Lock()
        self._snapshot: PortSnapshot | None = None
        self._snapshot_time = 0.0
        self._options = CollectionOptions(
            concurrency=1,
            max_attempts=1,
            connect_timeout_seconds=15,
            command_timeout_seconds=20,
            max_output_bytes=1024 * 1024,
            max_output_lines=10_000,
            max_pager_advances=0,
            retry_delays_seconds=(),
        )

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        if not self._lock.acquire(blocking=False):
            raise ValueError("포트 작업이 진행 중입니다. 완료 후 다시 시도하세요.")
        try:
            yield
        finally:
            self._lock.release()

    def inspect(
        self,
        target: DeviceTarget,
        interface: str,
        credentials: Credentials,
        *,
        approve_host_key: Callable[[HostKeyCheck], bool],
        display_only: bool = False,
        admin_only: bool = False,
    ) -> PortSnapshot:
        with self._exclusive():
            self._snapshot = None
            validate_interface(interface)
            session: PortSession | None = None
            try:
                observation = self._trusted_key(target, approve_host_key)
                session = self._factory(target, credentials, self._options, self.host_key_store)
                self._prepare(session)
                snapshot = self._read_snapshot(
                    session,
                    target,
                    interface,
                    observation.fingerprint,
                    display_only=display_only,
                    admin_only=admin_only,
                )
                if not display_only:
                    self._snapshot = snapshot
                    self._snapshot_time = self._clock()
                return snapshot
            except CollectionFailure:
                raise
            except Exception as exc:
                raise CollectionFailure(
                    ErrorCode.UNEXPECTED_ERROR,
                    "장비 상태를 확인하지 못했습니다. 접속 정보와 장비 상태를 확인한 뒤 다시 조회하세요.",
                ) from exc
            finally:
                if session is not None:
                    with suppress(Exception):
                        session.close()

    def apply(
        self,
        snapshot: PortSnapshot,
        action: PortAction,
        credentials: Credentials,
        *,
        confirmation: str,
        expected_lldp_identity: tuple[str, str] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> PortOperationResult:
        with self._exclusive():
            approved, self._snapshot = self._snapshot, None
            if approved is None or snapshot is not approved:
                raise ValueError("유효한 조회 결과가 없습니다. 대상 포트를 다시 조회하세요.")
            if not 0 <= self._clock() - self._snapshot_time < self.SNAPSHOT_MAX_AGE_SECONDS:
                raise ValueError("조회 결과가 만료되었습니다. 대상 포트를 다시 조회하세요.")
            if confirmation != snapshot.confirmation_text:
                raise ValueError(
                    "입력한 확인 문자열이 조회한 대상과 일치하지 않습니다. 다시 조회하세요."
                )
            if not isinstance(action, PortAction):
                raise ValueError("지원하지 않는 포트 제어 동작입니다.")
            if snapshot.admin_only and action is not PortAction.DISABLE:
                raise ValueError("관리 상태만 조회한 포트는 일괄 비활성화에만 사용할 수 있습니다.")
            if expected_lldp_identity is not None and (
                action not in (PortAction.POE_OFF, PortAction.DISABLE)
                or len(expected_lldp_identity) != 2
                or not all(isinstance(value, str) and value for value in expected_lldp_identity)
            ):
                raise ValueError("일괄 전원 차단의 LLDP 장비 식별 정보가 올바르지 않습니다.")
            if action.is_poe and snapshot.poe_enabled is None:
                raise ValueError(
                    "이 포트의 PoE 지원/사용 설정을 확인할 수 없어 전원을 제어할 수 없습니다."
                )
            session: PortSession | None = None
            mutation_started = False
            try:
                observation = self._trusted_key(snapshot.target, None)
                if observation.fingerprint != snapshot.fingerprint:
                    raise CollectionFailure(
                        ErrorCode.HOST_KEY_CHANGED, "조회 후 SSH 장비 지문이 변경되었습니다."
                    )
                session = self._factory(
                    snapshot.target, credentials, self._options, self.host_key_store
                )
                self._prepare(session)
                before = self._read_snapshot(
                    session,
                    snapshot.target,
                    snapshot.interface,
                    observation.fingerprint,
                    admin_only=snapshot.admin_only,
                )
                if not _same_settings_for_action(before, snapshot, action):
                    raise PreMutationRejected(
                        ErrorCode.INPUT_INVALID,
                        "조회 후 대상 정보 또는 포트 설정이 변경되었습니다. 다시 조회하세요.",
                    )
                if expected_lldp_identity is not None:
                    # A cached LLDP record is not proof of a live AP. Require
                    # live Link Up alongside the pinned model/Chassis/Port.
                    # Link Up is necessary, not sufficient proof of LLDP freshness.
                    if before.link_status.casefold() != "up":
                        raise PreMutationRejected(
                            ErrorCode.INPUT_INVALID,
                            "Link Up이 확인되지 않아 캐시된 LLDP AP 이웃으로 전원 차단을 허용하지 않습니다.",
                        )
                    from .poe_power import parse_lldp_neighbors

                    neighbors = parse_lldp_neighbors(
                        session.read_lldp(before.interface), before.interface
                    )
                    if (
                        len(neighbors) != 1
                        or neighbors[0].model != "AP-535"
                        or (neighbors[0].chassis_id, neighbors[0].port_id) != expected_lldp_identity
                    ):
                        raise PreMutationRejected(
                            ErrorCode.INPUT_INVALID,
                            "LLDP 이웃 장비가 최초 AP-535 조회와 일치하지 않아 명령을 차단했습니다.",
                        )
                # From this line onward, pending audit or switch command
                # failures are uncertain and must never be auto-replayed.
                if cancel_event is not None and cancel_event.is_set():
                    raise PreMutationRejected(
                        ErrorCode.CANCELLED, "포트 변경 명령 전 중지했습니다."
                    )
                mutation_started = True
                return self._apply_once(session, before, action, credentials, cancel_event)
            except CollectionFailure as exc:
                if mutation_started or isinstance(exc, PreMutationRejected):
                    raise
                raise PreMutationRejected(
                    exc.code,
                    exc.safe_message,
                    transient=exc.transient,
                    diagnostic_phase=exc.diagnostic_phase,
                    diagnostic_detail=exc.diagnostic_detail,
                ) from exc
            except Exception as exc:
                if mutation_started:
                    raise CollectionFailure(
                        ErrorCode.UNEXPECTED_ERROR,
                        "변경 명령의 결과를 확인할 수 없습니다. 현재 포트 상태를 직접 재조회하세요.",
                    ) from exc
                raise PreMutationRejected(
                    ErrorCode.UNEXPECTED_ERROR,
                    "실행 전 확인을 완료하지 못해 변경을 중단했습니다. 현재 상태를 다시 조회하세요.",
                ) from exc
            finally:
                if session is not None:
                    with suppress(Exception):
                        session.close()

    def _trusted_key(
        self,
        target: DeviceTarget,
        approve: Callable[[HostKeyCheck], bool] | None,
    ) -> HostKeyObservation:
        observation = self._probe.probe(target, timeout=self._options.connect_timeout_seconds)
        if observation.target != target:
            raise CollectionFailure(
                ErrorCode.HOST_KEY_REJECTED, "SSH 지문 조회 대상이 일치하지 않습니다."
            )
        check = self.host_key_store.check(observation)
        if (
            check.state is HostKeyTrustState.UNKNOWN
            and approve is not None
            and approve(check) is True
        ):
            self.host_key_store.approve([check])
        self.host_key_store.require_trusted(observation)
        return observation

    @staticmethod
    def _prepare(session: PortSession) -> None:
        session.connect()
        session.enter_enable()
        session.disable_paging()
        session.set_terminal_width()

    @staticmethod
    def _read_snapshot(
        session: PortSession,
        target: DeviceTarget,
        interface: str,
        fingerprint: str,
        *,
        display_only: bool = False,
        admin_only: bool = False,
    ) -> PortSnapshot:
        version = session.send_show("show version")
        modules = session.send_show("show modules")
        resolved = resolve_switch_hostname(session)
        identity = identify_connected_device(
            version, modules, lambda: session.send_show("show stacking"), hostname=resolved.hostname
        )
        if admin_only:
            # Administrative disable addresses the port directly. An unavailable
            # status display is not evidence that the selected port is enabled or disabled.
            try:
                enabled = parse_interface_status(session.read_interfaces(), interface).enabled
            except CollectionFailure as exc:
                if exc.code is not ErrorCode.COMMAND_REJECTED:
                    raise
                enabled = None
            poe = PoEState(None, "미조회", None)
            links = {}
        else:
            port = parse_interface_status(session.read_interfaces(), interface)
            enabled = port.enabled
            try:
                poe = PortControlService._read_poe(session, interface, identity, port)
            except CollectionFailure as exc:
                if not display_only or exc.code is not ErrorCode.COMMAND_REJECTED:
                    raise
                # A completed, malformed PoE response cannot authorize a change.
                # The independently observed physical link can still be displayed.
                poe = PoEState(None, "PoE 조회 불가", None)
            try:
                links = parse_interface_links(session.read_interface_status())
            except CollectionFailure as exc:
                if exc.code is not ErrorCode.COMMAND_REJECTED:
                    raise
                links = {}
        return PortSnapshot(
            target,
            interface,
            identity.hostname or "확인 불가",
            identity.model,
            enabled,
            links.get(interface, UNKNOWN_LINK),
            poe.enabled,
            poe.status,
            poe.watts,
            fingerprint,
            datetime.now(UTC).isoformat(),
            admin_only=admin_only,
        )

    @staticmethod
    def _read_poe(
        session: PortSession,
        interface: str,
        identity: DeviceIdentity,
        port: InterfaceState,
    ) -> PoEState:
        known_model = SUPPORTED_MODELS.get(identity.sku or "")
        if (
            known_model is not None and "PoE" not in known_model
        ) or "SFP" in port.port_type.upper():
            return PoEState(None, "PoE 미지원", None)
        try:
            output = session.read_poe(interface)
        except CollectionFailure as exc:
            if exc.code is ErrorCode.COMMAND_REJECTED:
                # A command can be unavailable or denied without proving the
                # port lacks PoE. Disable only the PoE controls in that case.
                return PoEState(None, "PoE 조회 불가", None)
            raise
        if re.search(
            r"(?:not a PoE port|does not support PoE|no PoE ports)", output, re.IGNORECASE
        ):
            return PoEState(None, "PoE 미지원", None)
        return parse_poe_status(output, interface)

    def _read_again(
        self, before: PortSnapshot, credentials: Credentials, cancel: threading.Event | None
    ) -> PortSnapshot:
        """Inspect via a fresh pinned connection without creating a new write authorization."""

        def check_cancel() -> None:
            if cancel is not None and cancel.is_set():
                raise CollectionFailure(ErrorCode.CANCELLED, "읽기 전용 재조회를 중지했습니다.")

        check_cancel()
        observation = self._trusted_key(before.target, None)
        if observation.fingerprint != before.fingerprint:
            raise CollectionFailure(ErrorCode.HOST_KEY_CHANGED, "변경 전 SSH 지문과 다릅니다.")
        check_cancel()
        session = self._factory(before.target, credentials, self._options, self.host_key_store)
        try:
            self._prepare(session)
            check_cancel()
            after = self._read_snapshot(
                session, before.target, before.interface, observation.fingerprint
            )
            check_cancel()
            return after
        finally:
            with suppress(Exception):
                session.close()

    def _apply_once(
        self,
        session: PortSession,
        before: PortSnapshot,
        action: PortAction,
        credentials: Credentials,
        cancel: threading.Event | None,
    ) -> PortOperationResult:
        current = before.poe_enabled if action.is_poe else before.enabled
        audit_path = (
            self.audit_directory / f"{datetime.now(UTC):%Y%m%dT%H%M%S}_{uuid.uuid4().hex}.jsonl"
        )
        if current is action.desired_enabled:
            self._audit(audit_path, action, before, before, "unchanged")
            return PortOperationResult(
                "unchanged",
                "이미 요청한 설정입니다. 변경 명령을 전송하지 않았습니다.",
                before,
                before,
                audit_path,
            )
        # Durable intent precedes a potentially disruptive write. Failure here
        # means no mutation was transmitted.
        self._audit(audit_path, action, before, None, "pending")
        after: PortSnapshot | None = None
        acknowledged = False
        try:
            session.change_port(before.interface, action.value)
            acknowledged = True
            after = self._read_snapshot(
                session,
                before.target,
                before.interface,
                before.fingerprint,
                admin_only=before.admin_only,
            )
            can_recheck, reason = _check_readback(before, after, action)
            if not can_recheck:
                after = None
        except Exception as exc:
            if not acknowledged and isinstance(exc, PreMutationRejected):
                self._audit(
                    audit_path, action, before, None, "preflight_failed", message=exc.safe_message
                )
                raise
            code = _failure_code(exc)
            stage = "변경 직후 상태 조회" if acknowledged else "포트 변경 명령 응답"
            reason = f"{stage} 실패 ({code.value})."
            # Never infer a successful write from an explicit rejection or a different identity.
            can_recheck = code not in {
                ErrorCode.HOST_KEY_CHANGED,
                ErrorCode.HOST_KEY_REJECTED,
                ErrorCode.MODEL_UNSUPPORTED,
                ErrorCode.CANCELLED,
            } and not (not acknowledged and code is ErrorCode.COMMAND_REJECTED)

        recovered = False
        if reason and can_recheck and action is PortAction.POE_ON:
            with suppress(Exception):
                session.close()
            try:
                reread = self._read_again(before, credentials, cancel)
                consistent, disagreement = _check_readback(before, reread, action)
                after = reread if consistent else None
                if disagreement:
                    reason += f" 읽기 전용 재조회 결과: {disagreement}"
                else:
                    reason = ""
                    recovered = True
            except Exception as exc:
                reason += f" 읽기 전용 재조회 실패 ({_failure_code(exc).value})."
        if reason:
            status = "unverified"
            message = f"{reason} 명령을 재전송하지 않았습니다. 현재 상태를 다시 조회하세요."
        elif recovered:
            status = "verified"
            message = "읽기 전용 재조회에서 PoE ON(Power Enable: Yes)을 확인했습니다."
        else:
            status = "verified"
            message = (
                "PoE 사용 설정 변경을 확인했습니다. AP 통신 복구 여부는 별도 확인이 필요합니다."
                if action.is_poe
                else "포트 사용 설정 변경을 확인했습니다. 물리 링크 상태는 별도로 표시합니다."
            )
        try:
            self._audit(audit_path, action, before, after, status, message=message)
        except CollectionFailure:
            message += " 작업 결과 이력 저장에 실패했습니다. 이전 실행 예정 기록과 현재 장비 상태를 확인하세요."
        return PortOperationResult(status, message, before, after, audit_path)

    @staticmethod
    def _audit(
        path: Path,
        action: PortAction,
        before: PortSnapshot,
        after: PortSnapshot | None,
        status: str,
        *,
        message: str = "",
    ) -> None:
        record = {
            "time": datetime.now(UTC).isoformat(),
            "target": before.target.endpoint,
            "hostname": before.hostname,
            "interface": before.interface,
            "action": action.value,
            "status": status,
            "before": _state_record(before),
            "after": _state_record(after),
        }
        if message:
            record["message"] = message
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except (OSError, ValueError) as exc:
            raise CollectionFailure(
                ErrorCode.REPORT_WRITE_FAILED,
                "포트 작업 이력을 저장할 수 없습니다. 저장 위치와 권한을 확인하세요.",
            ) from exc
