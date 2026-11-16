"""Bounded, read-only grouped PoE collection over pinned SSH sessions."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime

from .hostkeys import HostKeyProbe, HostKeyStore, ParamikoHostKeyProbe
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
from .validation import ARUBA_2930F_MODELS, hostname_from_prompt, validate_device_identity

SessionFactory = Callable[[DeviceTarget, Credentials, CollectionOptions, HostKeyStore], PortSession]


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


def _terminal(group: PoETarget, interface: str, failure: CollectionFailure) -> PoEQueryResult:
    return PoEQueryResult(
        group,
        interface,
        QueryStatus.CANCELLED if failure.code is ErrorCode.CANCELLED else QueryStatus.FAILED,
        error_code=failure.code,
        message=failure.safe_message,
        finished_at=datetime.now(UTC).isoformat(),
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
        _check_cancel(cancel)
        while not self._trust_lock.acquire(timeout=0.05):
            _check_cancel(cancel)
        try:
            observation = _bounded(
                cancel,
                lambda: self._probe.probe(target, timeout=self._options.connect_timeout_seconds),
            )
            if observation.target != target:
                raise CollectionFailure(
                    ErrorCode.HOST_KEY_REJECTED, "SSH 지문 조회 대상이 일치하지 않습니다."
                )
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
    ) -> PoEState:
        known_model = ARUBA_2930F_MODELS.get(identity.sku or "")
        if (
            known_model is not None and "PoE" not in known_model
        ) or "SFP" in port.port_type.upper():
            return PoEState(None, "PoE 미지원", None)
        output = _bounded(cancel, lambda: session.read_poe(interface))
        if re.search(
            r"(?:not a PoE port|does not support PoE|no PoE ports)", output, re.IGNORECASE
        ):
            return PoEState(None, "PoE 미지원", None)
        # A normal CLI rejection is a failed query, never evidence of no PoE.
        return parse_poe_status(output, interface)

    def _collect_group(
        self,
        group: PoETarget,
        credentials: Credentials,
        approve: Callable[[HostKeyCheck], bool],
        on_result: Callable[[PoEQueryResult], None] | None,
        cancel: threading.Event,
    ) -> tuple[PoEQueryResult, ...]:
        session: PortSession | None = None
        rows: list[PoEQueryResult] = []
        fatal: CollectionFailure | None = None
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
                hostname=hostname_from_prompt(_bounded(cancel, session.get_prompt)),
            )
            for interface in group.interfaces:
                try:
                    output = _bounded(cancel, session.read_interfaces)
                    port = parse_interface_status(output, interface)
                    poe = self._read_poe(session, interface, identity, port, cancel)
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
                        finished_at=datetime.now(UTC).isoformat(),
                    )
                except CollectionFailure as exc:
                    row = _terminal(group, interface, exc)
                    if exc.code is not ErrorCode.COMMAND_REJECTED:
                        fatal = exc
                except Exception:
                    fatal = _unexpected_failure()
                    row = _terminal(group, interface, fatal)
                rows.append(row)
                _deliver(row, on_result)
                if fatal is not None:
                    break
        except CollectionFailure as exc:
            fatal = exc
        except Exception:
            fatal = _unexpected_failure()
        finally:
            if session is not None:
                with suppress(Exception):
                    session.close()
        for interface in group.interfaces[len(rows) :]:
            failure = fatal or _unexpected_failure()
            if cancel.is_set():
                failure = CollectionFailure(ErrorCode.CANCELLED, "포트 전력 조회를 중지했습니다.")
            row = _terminal(group, interface, failure)
            rows.append(row)
            _deliver(row, on_result)
        return tuple(rows)
