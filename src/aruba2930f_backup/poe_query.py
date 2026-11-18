"""Bounded, read-only grouped PoE collection over pinned SSH sessions."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import Protocol

from .hostkeys import HostKeyProbe, HostKeyStore, ParamikoHostKeyProbe
from .hostname_discovery import discover_hostname
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
from .poe_power import (
    LLDPObservation,
    LLDPStatus,
    PowerEvidence,
    assess_ap535_power,
    parse_lldp_neighbors,
    parse_power_evidence,
)
from .poe_query_models import PoEQueryResult, PoETarget, QueryStatus
from .poe_targets import merge_targets
from .port_control import (
    InterfaceState,
    PoEState,
    PortSession,
    PortSnapshot,
    parse_interface_status,
    parse_poe_status,
)
from .port_control_ssh import PortControlSSHSession
from .validation import ARUBA_2930F_MODELS, validate_device_identity


class QuerySession(PortSession, Protocol):
    """Query-only read surface; existing mutation and backup permissions stay fixed."""

    def read_lldp(self, interface: str) -> str: ...


SessionFactory = Callable[
    [DeviceTarget, Credentials, CollectionOptions, HostKeyStore], QuerySession
]


def _check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise CollectionFailure(ErrorCode.CANCELLED, "포트 전력 조회를 중지했습니다.")


def _bounded[T](cancel: threading.Event, operation: Callable[[], T]) -> T:
    """Cancellation stops the next operation; in-flight transport calls stay bounded."""
    _check_cancel(cancel)
    result = operation()
    _check_cancel(cancel)
    return result


def _unexpected_failure() -> CollectionFailure:
    return CollectionFailure(
        ErrorCode.UNEXPECTED_ERROR,
        "장비 상태를 확인하지 못했습니다. 접속 정보와 장비 상태를 확인한 뒤 다시 조회하세요.",
    )


def _terminal(
    group: PoETarget,
    interface: str,
    failure: CollectionFailure,
    identity: DeviceIdentity | None = None,
) -> PoEQueryResult:
    return PoEQueryResult(
        group,
        interface,
        QueryStatus.CANCELLED if failure.code is ErrorCode.CANCELLED else QueryStatus.FAILED,
        error_code=failure.code,
        message=failure.safe_message,
        identity=identity,
        poe_finished_at=datetime.now(UTC).isoformat(),
        complete=False,
        lldp=LLDPObservation(),
    )


def _finish(row: PoEQueryResult, lldp: LLDPObservation) -> PoEQueryResult:
    snapshot = row.snapshot
    poe = (
        PoEState(snapshot.poe_enabled, snapshot.poe_status, snapshot.watts)
        if snapshot is not None
        else None
    )
    return replace(
        row,
        lldp=lldp,
        assessment=assess_ap535_power(poe, row.power_evidence, lldp),
        finished_at=datetime.now(UTC).isoformat(),
        complete=True,
    )


def _lldp_failure(failure: CollectionFailure) -> LLDPObservation:
    return LLDPObservation(
        status=LLDPStatus.CANCELLED if failure.code is ErrorCode.CANCELLED else LLDPStatus.FAILED,
        error_code=failure.code,
        message=failure.safe_message,
        observed_at=datetime.now(UTC).isoformat(),
    )


def _deliver(row: PoEQueryResult, callback: Callable[[PoEQueryResult], None] | None) -> None:
    if callback is not None:
        # A display callback cannot erase completed device results or prevent
        # another target's cleanup. Returned records remain authoritative.
        with suppress(Exception):
            callback(row)


class PoEQueryService:
    """Read each endpoint once and keep its requested ports in one serial session.

    Only explicit, validated show reads and existing session preparation are
    used. No mutation method, retry scheduler or implicit reconnect is called.
    """

    def __init__(
        self,
        host_key_store: HostKeyStore | None = None,
        *,
        host_key_probe: HostKeyProbe | None = None,
        session_factory: SessionFactory | None = None,
        max_workers: int = 3,
    ) -> None:
        if type(max_workers) is not int or not 1 <= max_workers <= 3:
            raise ValueError("동시 조회 스위치 수는 1~3이어야 합니다.")
        self.host_key_store = host_key_store if host_key_store is not None else HostKeyStore()
        self._probe = host_key_probe if host_key_probe is not None else ParamikoHostKeyProbe()
        self._factory: SessionFactory = session_factory or PortControlSSHSession
        self._max_workers = max_workers
        self._trust_lock = threading.Lock()
        self._options = CollectionOptions(
            concurrency=max_workers,
            max_attempts=1,
            connect_timeout_seconds=15,
            command_timeout_seconds=20,
            max_output_bytes=1024 * 1024,
            max_output_lines=10_000,
            max_pager_advances=0,
            retry_delays_seconds=(),
        )

    def collect(
        self,
        groups: Sequence[PoETarget],
        credentials: Credentials,
        *,
        approve_host_key: Callable[[HostKeyCheck], bool],
        on_result: Callable[[PoEQueryResult], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[PoEQueryResult, ...]:
        normalized = merge_targets(groups)
        cancel = cancel_event if cancel_event is not None else threading.Event()
        with ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix="poe-query"
        ) as pool:
            futures = [
                pool.submit(
                    self._collect_group, group, credentials, approve_host_key, on_result, cancel
                )
                for group in normalized
            ]
            # Future iteration gives stable target/port order; workers deliver
            # their callbacks immediately, even when an earlier group is slow.
            return tuple(row for future in futures for row in future.result())

    def _trusted_key(
        self,
        target: DeviceTarget,
        approve: Callable[[HostKeyCheck], bool],
        cancel: threading.Event,
    ) -> HostKeyObservation:
        # Network probes are read-only and independently bounded. Do not hold
        # the global approval lock while a slow/unreachable switch is probed:
        # otherwise one timeout serializes all three query workers.
        observation = _bounded(
            cancel,
            lambda: self._probe.probe(target, timeout=self._options.connect_timeout_seconds),
        )
        if observation.target != target:
            raise CollectionFailure(
                ErrorCode.HOST_KEY_REJECTED, "SSH 지문 조회 대상이 일치하지 않습니다."
            )
        # Keep check / operator approval / store commit serialized to preserve
        # the existing no-auto-trust policy and deterministic prompt handling.
        while not self._trust_lock.acquire(timeout=0.05):
            _check_cancel(cancel)
        try:
            check = _bounded(cancel, lambda: self.host_key_store.check(observation))
            if (
                check.state is HostKeyTrustState.UNKNOWN
                and _bounded(cancel, lambda: approve(check)) is True
            ):
                _bounded(cancel, lambda: self.host_key_store.approve([check]))
            _bounded(cancel, lambda: self.host_key_store.require_trusted(observation))
            return observation
        finally:
            self._trust_lock.release()

    @staticmethod
    def _read_poe(
        session: PortSession,
        interface: str,
        identity: DeviceIdentity,
        port: InterfaceState,
        cancel: threading.Event,
    ) -> tuple[PoEState, PowerEvidence | None]:
        known_model = ARUBA_2930F_MODELS.get(identity.sku or "")
        if (
            known_model is not None and "PoE" not in known_model
        ) or "SFP" in port.port_type.upper():
            return PoEState(None, "PoE 미지원", None), None
        output = _bounded(cancel, lambda: session.read_poe(interface))
        if re.search(
            r"(?:not a PoE port|does not support PoE|no PoE ports)", output, re.IGNORECASE
        ):
            return PoEState(None, "PoE 미지원", None), None
        # A normal CLI rejection is a failed query, never evidence of no PoE.
        return parse_poe_status(output, interface), parse_power_evidence(output, interface)

    def _collect_group(
        self,
        group: PoETarget,
        credentials: Credentials,
        approve: Callable[[HostKeyCheck], bool],
        on_result: Callable[[PoEQueryResult], None] | None,
        cancel: threading.Event,
    ) -> tuple[PoEQueryResult, ...]:
        session: QuerySession | None = None
        rows: list[PoEQueryResult] = []
        fatal: CollectionFailure | None = None
        identity: DeviceIdentity | None = None
        try:
            observation = self._trusted_key(group.target, approve, cancel)
            _check_cancel(cancel)
            session = self._factory(group.target, credentials, self._options, self.host_key_store)
            _bounded(cancel, session.connect)
            _bounded(cancel, session.enter_enable)
            _bounded(cancel, session.disable_paging)
            _bounded(cancel, session.set_terminal_width)
            version = _bounded(cancel, lambda: session.send_show("show version"))
            modules = _bounded(cancel, lambda: session.send_show("show modules"))
            identity = validate_device_identity(
                version,
                modules,
                hostname=discover_hostname(
                    lambda command: _bounded(cancel, lambda: session.send_show(command)),
                    lambda: _bounded(cancel, session.get_prompt),
                ),
            )
            # AOS-S show interfaces brief reports all local interfaces.
            # Reading it once per switch prevents hundreds of duplicate SSH
            # commands when an operator selects many AP ports.
            interfaces_output: str | None = None
            for interface in group.interfaces:
                try:
                    if interfaces_output is None:
                        interfaces_output = _bounded(cancel, session.read_interfaces)
                    port = parse_interface_status(interfaces_output, interface)
                    poe, evidence = self._read_poe(session, interface, identity, port, cancel)
                    _check_cancel(cancel)
                    snapshot = PortSnapshot(
                        group.target,
                        interface,
                        identity.hostname or "확인 불가",
                        identity.model,
                        port.enabled,
                        port.link_status,
                        poe.enabled,
                        poe.status,
                        poe.watts,
                        observation.fingerprint,
                        datetime.now(UTC).isoformat(),
                    )
                    row = PoEQueryResult(
                        group,
                        interface,
                        QueryStatus.UNSUPPORTED if poe.enabled is None else QueryStatus.OK,
                        snapshot=snapshot,
                        identity=identity,
                        power_evidence=evidence,
                        lldp=LLDPObservation(),
                        assessment=assess_ap535_power(poe, evidence, LLDPObservation()),
                        poe_finished_at=datetime.now(UTC).isoformat(),
                        complete=False,
                    )
                except CollectionFailure as exc:
                    row = _terminal(group, interface, exc, identity)
                    if exc.code is not ErrorCode.COMMAND_REJECTED:
                        fatal = exc
                except Exception:
                    fatal = _unexpected_failure()
                    row = _terminal(group, interface, fatal, identity)
                rows.append(row)
                _deliver(row, on_result)
                if fatal is not None:
                    break

            # Every selected port's PoE outcome is published before LLDP starts.
            # CLI errors are local; an uncertain transport cannot be reused.
            for index, row in enumerate(rows):
                if fatal is not None:
                    break
                try:
                    output = _bounded(cancel, partial(session.read_lldp, row.interface))
                    neighbors = parse_lldp_neighbors(output, row.interface)
                    _check_cancel(cancel)
                    lldp = LLDPObservation(
                        status=LLDPStatus.OK,
                        neighbors=neighbors,
                        observed_at=datetime.now(UTC).isoformat(),
                    )
                except CollectionFailure as exc:
                    lldp = _lldp_failure(exc)
                    if exc.code is not ErrorCode.COMMAND_REJECTED:
                        fatal = exc
                except Exception:
                    fatal = _unexpected_failure()
                    lldp = _lldp_failure(fatal)
                finished = _finish(row, lldp)
                rows[index] = finished
                _deliver(finished, on_result)
        except CollectionFailure as exc:
            fatal = exc
        except Exception:
            fatal = _unexpected_failure()
        finally:
            if session is not None:
                with suppress(Exception):
                    session.close()
        failure = fatal or _unexpected_failure()
        if cancel.is_set():
            failure = CollectionFailure(ErrorCode.CANCELLED, "포트 전력 조회를 중지했습니다.")
        for index, row in enumerate(rows):
            if not row.complete:
                finished = _finish(row, _lldp_failure(failure))
                rows[index] = finished
                _deliver(finished, on_result)
        for interface in group.interfaces[len(rows) :]:
            row = _finish(_terminal(group, interface, failure, identity), _lldp_failure(failure))
            rows.append(row)
            _deliver(row, on_result)
        return tuple(rows)
