"""Fail-closed, durable batch port changes over independently verified single-port services.

Read-only PoE/LLDP discovery and mutation deliberately remain separate transports.
A batch plan is immutable; every job gets a fresh inspect/apply pair and a
write-ahead event before the existing single-port adapter may send a mutation.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .models import CollectionFailure, Credentials, DeviceTarget, HostKeyCheck
from .poe_power import LLDPStatus
from .poe_query_models import PoEQueryResult, QueryStatus
from .port_control import (
    PortAction,
    PortControlService,
    PortOperationResult,
    PortSnapshot,
    PreMutationRejected,
    validate_interface,
)
from .storage import default_output_directory

MAX_BATCH_ACTIONS = 300
BATCH_CHECKPOINT = 10


class BatchStatus(StrEnum):
    VERIFIED = "verified"
    UNCHANGED = "unchanged"
    PREFLIGHT_FAILED = "preflight_failed"
    UNVERIFIED = "unverified"
    CANCELLED = "cancelled"
    PAUSED = "paused"


class BatchControlError(ValueError):
    """Operator-safe error from preparing or recording an intended mutation."""


@dataclass(frozen=True, slots=True)
class BatchTarget:
    target: DeviceTarget
    interface: str
    expected_hostname: str
    expected_enabled: bool
    expected_poe_enabled: bool | None
    fingerprint: str = field(repr=False)
    ap_name: str = ""
    confirmed_ap535: bool = False
    expected_chassis_id: str = ""
    expected_remote_port_id: str = ""
    expected_model: str = ""
    allow_unknown_hostname: bool = False

    def __post_init__(self) -> None:
        validate_interface(self.interface)
        hostname_unknown = (
            not self.expected_hostname.strip() or self.expected_hostname == "확인 불가"
        )
        if hostname_unknown and not self.allow_unknown_hostname:
            raise BatchControlError("장비 호스트네임을 확인한 포트만 일괄 제어할 수 있습니다.")
        if self.allow_unknown_hostname and (
            not hostname_unknown or "2930F" not in self.expected_model.upper()
        ):
            raise BatchControlError("호스트네임 미확인 시 검증된 Aruba 2930F 모델이 필요합니다.")
        if not self.fingerprint.startswith("SHA256:"):
            raise BatchControlError("승인된 SSH 지문이 없는 대상은 일괄 제어할 수 없습니다.")


@dataclass(frozen=True, slots=True)
class BatchPlan:
    plan_id: str
    created_at: str
    action: PortAction
    targets: tuple[BatchTarget, ...]

    def __post_init__(self) -> None:
        try:
            uuid.UUID(self.plan_id)
        except (ValueError, AttributeError) as exc:
            raise BatchControlError("작업 ID가 올바르지 않습니다.") from exc
        if not isinstance(self.action, PortAction):
            raise BatchControlError("지원하지 않는 포트 제어 동작입니다.")
        if not self.targets or len(self.targets) > MAX_BATCH_ACTIONS:
            raise BatchControlError("일괄 작업 대상은 1~300개 포트여야 합니다.")
        keys = [(t.target.endpoint, t.interface) for t in self.targets]
        if len(set(keys)) != len(keys):
            raise BatchControlError("같은 스위치·포트를 한 작업에 중복 등록할 수 없습니다.")
        if self.action is not PortAction.POE_ON and any(
            item.allow_unknown_hostname for item in self.targets
        ):
            raise BatchControlError("호스트네임 미확인 대상의 자동 검증은 PoE ON에서만 허용합니다.")


@dataclass(frozen=True, slots=True)
class BatchResult:
    target: BatchTarget
    status: BatchStatus
    message: str
    after: PortSnapshot | None = None
    audit_path: Path | None = None
    before: PortSnapshot | None = None


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    plan: BatchPlan
    results: tuple[BatchResult, ...]
    journal_path: Path

    @property
    def counts(self) -> dict[str, int]:
        return {
            status.value: sum(result.status is status for result in self.results)
            for status in BatchStatus
        }


class SinglePortController(Protocol):
    def inspect(
        self,
        target: DeviceTarget,
        interface: str,
        credentials: Credentials,
        *,
        approve_host_key: Callable[[HostKeyCheck], bool],
    ) -> PortSnapshot: ...

    def apply(
        self,
        snapshot: PortSnapshot,
        action: PortAction,
        credentials: Credentials,
        *,
        confirmation: str,
        expected_lldp_identity: tuple[str, str] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> PortOperationResult: ...


def validate_batch_target(
    row: PoEQueryResult,
    action: PortAction,
    *,
    allow_unverified_ap: bool = False,
) -> BatchTarget:
    """Share the exact fail-closed batch eligibility rule with the GUI.

    A selected row alone cannot authorize a mutation: inspect/apply still
    revalidate switch identity, host key and port configuration.
    """
    if not isinstance(action, PortAction):
        raise BatchControlError("지원하지 않는 포트 제어 동작입니다.")
    snapshot = row.snapshot
    if (
        not row.complete
        or snapshot is None
        or row.status not in (QueryStatus.OK, QueryStatus.UNSUPPORTED)
    ):
        raise BatchControlError("완료된 포트 상태가 없는 행은 일괄 제어할 수 없습니다.")
    if action.is_poe and snapshot.poe_enabled is None:
        raise BatchControlError("PoE 미지원·확인 불가 포트에는 전원 제어를 실행할 수 없습니다.")
    # An LLDP neighbor can remain cached after physical disconnection.
    # The link check is necessary but does not prove LLDP advertisement freshness.
    if (
        action in (PortAction.POE_OFF, PortAction.DISABLE)
        and snapshot.link_status.casefold() != "up"
    ):
        raise BatchControlError("일괄 PoE OFF/포트 Down은 물리 링크가 Link Up인 포트만 허용합니다.")
    neighbors = (
        row.lldp.neighbors if row.lldp is not None and row.lldp.status is LLDPStatus.OK else ()
    )
    confirmed = len(neighbors) == 1 and neighbors[0].model == "AP-535"
    if (
        action in (PortAction.POE_OFF, PortAction.DISABLE)
        and confirmed
        and (not neighbors[0].chassis_id or not neighbors[0].port_id)
    ):
        raise BatchControlError(
            "AP-535의 LLDP Chassis ID와 Port ID를 확인해야 일괄 차단할 수 있습니다."
        )
    if action in (PortAction.POE_OFF, PortAction.DISABLE) and not (
        confirmed or allow_unverified_ap
    ):
        raise BatchControlError(
            "일괄 전원 OFF/포트 Down은 LLDP에서 AP-535로 확인된 포트만 허용합니다. "
            "미확인 포트는 기존 단일 포트 제어에서 개별 확인하세요."
        )
    return BatchTarget(
        snapshot.target,
        snapshot.interface,
        snapshot.hostname,
        snapshot.enabled,
        snapshot.poe_enabled,
        snapshot.fingerprint,
        ap_name=neighbors[0].system_name if len(neighbors) == 1 else "",
        confirmed_ap535=confirmed,
        expected_chassis_id=neighbors[0].chassis_id if confirmed else "",
        expected_remote_port_id=neighbors[0].port_id if confirmed else "",
        expected_model=snapshot.model,
        allow_unknown_hostname=(
            action is PortAction.POE_ON
            and (not snapshot.hostname.strip() or snapshot.hostname == "확인 불가")
        ),
    )


def create_batch_plan(
    results: Sequence[PoEQueryResult],
    action: PortAction,
    *,
    allow_unverified_ap: bool = False,
) -> BatchPlan:
    """Bind a concrete, already-observed endpoint/port list to one fixed action.

    OFF and Down require a single manufacturer-identified AP-535 by default.
    An explicit per-job/manual override may be considered in a later release;
    it is not enabled from the normal batch GUI to avoid uplink mass outages.
    """
    if not isinstance(action, PortAction):
        raise BatchControlError("지원하지 않는 포트 제어 동작입니다.")
    if not results or len(results) > MAX_BATCH_ACTIONS:
        raise BatchControlError("한 번의 제어 계획에는 1~300개 포트만 넣을 수 있습니다.")
    targets = [
        validate_batch_target(row, action, allow_unverified_ap=allow_unverified_ap)
        for row in results
    ]
    return BatchPlan(str(uuid.uuid4()), datetime.now(UTC).isoformat(), action, tuple(targets))


class _BatchJournal:
    """Append-only fsynced ledger; never stores passwords or Enable secrets."""

    def __init__(self, path: Path, plan: BatchPlan) -> None:
        self.path = path
        self.plan = plan
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                header = {
                    "event": "plan",
                    "schema": 1,
                    "plan_id": plan.plan_id,
                    "created_at": plan.created_at,
                    "action": plan.action.value,
                    "items": [
                        {
                            "endpoint": item.target.endpoint,
                            "interface": item.interface,
                            "hostname": item.expected_hostname,
                            "ap_name": item.ap_name,
                            "confirmed_ap535": item.confirmed_ap535,
                        }
                        for item in plan.targets
                    ],
                }
                handle.write(json.dumps(header, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise BatchControlError(
                "작업 계획 파일을 안전하게 기록하지 못해 변경을 시작하지 않았습니다."
            ) from exc

    def append(self, index: int, event: str, **fields: object) -> None:
        data = {
            "event": event,
            "index": index,
            "timestamp": datetime.now(UTC).isoformat(),
            **fields,
        }
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(data, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise BatchControlError(
                "작업 이력 저장에 실패했습니다. 실행 상태가 불확실할 수 있으므로 "
                "다음 명령을 전송하지 않습니다."
            ) from exc


def journal_results(path: Path) -> dict[int, tuple[str, str]]:
    """Read each port's last durable status and result message."""
    if path.stat().st_size > 2 * 1024 * 1024:
        raise BatchControlError("작업 이력 파일이 예상보다 큽니다.")
    events: list[dict[str, object]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("Invalid record")
            events.append(value)
    except (OSError, UnicodeError, ValueError) as exc:
        raise BatchControlError("작업 이력을 안전하게 해석하지 못했습니다.") from exc
    if not events or events[0].get("event") != "plan":
        raise BatchControlError("작업 계획 기록이 없습니다.")
    entries = events[0].get("items")
    if not isinstance(entries, list):
        raise BatchControlError("작업 대상 기록이 올바르지 않습니다.")
    state = {index: ("queued", "") for index in range(len(entries))}
    for event in events[1:]:
        index = event.get("index")
        name = event.get("event")
        if type(index) is not int or index not in state or not isinstance(name, str):
            raise BatchControlError("작업 이력에 올바르지 않은 상태가 있습니다.")
        if name == "intent":
            state[index] = ("result_unknown", "")  # May have reached the switch.
        elif name == "result":
            value = event.get("status")
            message = event.get("message")
            state[index] = (
                value if isinstance(value, str) else "result_unknown",
                message if isinstance(message, str) else "",
            )
        elif name == "preflight":
            state[index] = ("preflight", "")
    return state


def journal_states(path: Path) -> dict[int, str]:
    """Read durable states without resuming or replaying an uncertain write."""
    return {index: status for index, (status, _) in journal_results(path).items()}


class BatchPortController:
    """Sequential mutation with per-item fresh identity check and strict checkpoints."""

    def __init__(
        self,
        *,
        controller_factory: Callable[[], SinglePortController] | None = None,
        journal_directory: Path | None = None,
        chunk_size: int = BATCH_CHECKPOINT,
    ) -> None:
        if type(chunk_size) is not int or not 1 <= chunk_size <= BATCH_CHECKPOINT:
            raise ValueError("작업 확인 단위는 1~10개 포트여야 합니다.")
        self._controller_factory = controller_factory or PortControlService
        self._journal_directory = (
            journal_directory or default_output_directory() / "port-control" / "batch"
        )
        self._chunk_size = chunk_size

    def begin_journal(self, plan: BatchPlan) -> _BatchJournal:
        return _BatchJournal(self._journal_directory / f"{plan.plan_id}.jsonl", plan)

    def execute(
        self,
        plan: BatchPlan,
        credentials: Credentials,
        *,
        cancel_event: threading.Event | None = None,
        on_result: Callable[[int, BatchResult], None] | None = None,
        on_stage: Callable[[int, BatchTarget, str], None] | None = None,
        continue_after_chunk: Callable[[int, int], bool] | None = None,
        journal: _BatchJournal | None = None,
        journal_offset: int = 0,
    ) -> BatchOutcome:
        cancel = cancel_event if cancel_event is not None else threading.Event()
        if journal is None:
            journal = self.begin_journal(plan)
        if (
            journal_offset < 0
            or journal.plan.plan_id != plan.plan_id
            or journal.plan.action != plan.action
            or journal.plan.targets[journal_offset : journal_offset + len(plan.targets)]
            != plan.targets
        ):
            raise BatchControlError("승인한 작업 이력의 대상 순서와 일치하지 않습니다.")
        outcomes: list[BatchResult] = []
        pause_reason = ""
        for index, item in enumerate(plan.targets):
            if cancel.is_set():
                result = BatchResult(item, BatchStatus.CANCELLED, "실행 전 사용자가 중지했습니다.")
            elif pause_reason:
                result = BatchResult(item, BatchStatus.PAUSED, pause_reason)
            elif index and index % self._chunk_size == 0:
                can_continue = (
                    continue_after_chunk(index, len(plan.targets))
                    if continue_after_chunk is not None
                    else False
                )
                if not can_continue or cancel.is_set():
                    pause_reason = (
                        "다음 작업 묶음이 승인되지 않아 이 포트에 변경 명령을 전송하지 않았습니다."
                    )
                    result = BatchResult(item, BatchStatus.PAUSED, pause_reason)
                else:
                    result = self._execute_one(
                        journal_offset + index,
                        item,
                        plan.action,
                        credentials,
                        journal,
                        cancel,
                        on_stage,
                    )
            else:
                result = self._execute_one(
                    journal_offset + index,
                    item,
                    plan.action,
                    credentials,
                    journal,
                    cancel,
                    on_stage,
                )
            if result.status is BatchStatus.UNVERIFIED:
                pause_reason = (
                    f"{item.target.endpoint} 포트 {item.interface}의 변경 결과를 확인하지 못해 "
                    "이 포트에 변경 명령을 전송하지 않았습니다."
                )
            journal.append(
                journal_offset + index, "result", status=result.status.value, message=result.message
            )
            outcomes.append(result)
            if on_result is not None:
                on_result(journal_offset + index, result)
        return BatchOutcome(plan, tuple(outcomes), journal.path)

    def _execute_one(
        self,
        index: int,
        item: BatchTarget,
        action: PortAction,
        credentials: Credentials,
        journal: _BatchJournal,
        cancel: threading.Event,
        on_stage: Callable[[int, BatchTarget, str], None] | None,
    ) -> BatchResult:
        controller = self._controller_factory()

        def emit_stage(phase: str) -> None:
            if on_stage is not None:
                # Diagnostic UI notifications never change network safety.
                with suppress(Exception):
                    on_stage(index, item, phase)

        try:
            emit_stage("preflight")
            snapshot = controller.inspect(
                item.target,
                item.interface,
                credentials,
                approve_host_key=lambda _check: False,
            )
            # AOS-S may return an EXEC prompt without a parseable hostname
            # on either the initial observation or the fresh inspection.
            # The fallback is PoE ON only; all other identity/configuration
            # fields remain strict and two known hostnames must match.
            hostname_matches = snapshot.hostname == item.expected_hostname or (
                action is PortAction.POE_ON
                and (
                    snapshot.hostname in ("", "확인 불가")
                    or item.expected_hostname in ("", "확인 불가")
                )
            )
            identity_matches = (
                snapshot.target == item.target
                and snapshot.interface == item.interface
                and snapshot.fingerprint == item.fingerprint
                and snapshot.enabled == item.expected_enabled
                and snapshot.poe_enabled == item.expected_poe_enabled
                and hostname_matches
                and (not item.expected_model or snapshot.model == item.expected_model)
            )
            if not identity_matches:
                return BatchResult(
                    item,
                    BatchStatus.PREFLIGHT_FAILED,
                    "조회 이후 스위치 식별 정보 또는 포트 설정이 달라져 변경을 건너뛰었습니다.",
                )
            if cancel.is_set():
                return BatchResult(item, BatchStatus.CANCELLED, "명령 전 중지 요청을 확인했습니다.")
            journal.append(index, "preflight", status="ok")
            # The pending intent is persisted BEFORE the single-port service
            # is allowed to send any mutation. Crash recovery never replays it.
            journal.append(index, "intent", action=action.value)
            # A fresh, same-session LLDP identity check happens inside apply,
            # immediately before OFF/Down. A stale AP model alone is not enough.
            emit_stage("apply")
            if action in (PortAction.POE_OFF, PortAction.DISABLE) and item.confirmed_ap535:
                result = controller.apply(
                    snapshot,
                    action,
                    credentials,
                    confirmation=snapshot.confirmation_text,
                    expected_lldp_identity=(item.expected_chassis_id, item.expected_remote_port_id),
                    cancel_event=cancel,
                )
            else:
                result = controller.apply(
                    snapshot,
                    action,
                    credentials,
                    confirmation=snapshot.confirmation_text,
                    cancel_event=cancel,
                )
            if result.status == "verified":
                state = BatchStatus.VERIFIED
            elif result.status == "unchanged":
                state = BatchStatus.UNCHANGED
            else:
                state = BatchStatus.UNVERIFIED
            return BatchResult(
                item, state, result.message, result.after, result.audit_path, result.before
            )
        except BatchControlError:
            # A failed durable append is a hard stop: later targets must not be
            # changed if a mutation cannot be audited reliably.
            raise
        except PreMutationRejected as exc:
            # The single-port adapter explicitly proved no port command was sent.
            # Its already-written intent becomes a safe preflight result only
            # after the durable result record is appended by execute(). A crash
            # before that result remains result_unknown in journal_states().
            return BatchResult(item, BatchStatus.PREFLIGHT_FAILED, exc.safe_message)
        except (CollectionFailure, ValueError) as exc:
            # After an intent has been persisted, a transport exception may mean
            # the command reached the device. Never retry or mislabel it as a
            # harmless preflight failure.
            if journal_states(journal.path).get(index) == "result_unknown":
                return BatchResult(
                    item,
                    BatchStatus.UNVERIFIED,
                    "변경 요청의 결과를 확인할 수 없습니다. 수동 재조회 전 다음 명령을 중단합니다.",
                )
            if isinstance(exc, CollectionFailure):
                message = exc.safe_message
            else:
                message = "실행 전 대상 또는 현재 상태를 확인하지 못했습니다."
            return BatchResult(item, BatchStatus.PREFLIGHT_FAILED, message)
