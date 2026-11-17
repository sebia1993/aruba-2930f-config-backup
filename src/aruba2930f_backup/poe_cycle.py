"""Fail-closed, operator-approved two-stage PoE power cycle planning.

This module never sends commands. It binds the initial verified AP-535
targets to a separate OFF job, then requires fresh switch inspection and a
second independent ON approval. Existing batch journals remain authoritative.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .batch_port_control import (
    BatchControlError,
    BatchOutcome,
    BatchPlan,
    BatchStatus,
    BatchTarget,
    create_batch_plan,
)
from .poe_query_models import PoEQueryResult, PoETarget, QueryStatus
from .poe_targets import merge_targets
from .port_control import PortAction

PortKey = tuple[str, str]


class PowerCyclePhase(StrEnum):
    OFF_READY = "off_ready"
    OFF_RUNNING = "off_running"
    REQUERY_OFF = "requery_off"
    ON_PREPARE = "on_prepare"
    ON_READY = "on_ready"
    ON_RUNNING = "on_running"
    REQUERY_ON = "requery_on"
    VERIFY_ON = "verify_on"
    COMPLETE = "complete"
    INTERRUPTED = "interrupted"


def _key(target: BatchTarget) -> PortKey:
    return target.target.endpoint, target.interface


def prepare_cycle_off(results: Sequence[PoEQueryResult]) -> BatchPlan:
    """No silent target exclusions and no cycle of already-disabled ports."""
    if any(row.snapshot is None or row.snapshot.poe_enabled is not True for row in results):
        raise BatchControlError(
            "PoE 재가동은 현재 PoE ON으로 확인된 포트만 시작할 수 있습니다. "
            "기존 OFF·미확인 포트는 별도 조회 및 수동 제어가 필요합니다."
        )
    return create_batch_plan(results, PortAction.POE_OFF)


@dataclass(slots=True)
class PowerCycleSession:
    """One volatile UI workflow; durable OFF/ON jobs keep independent journals."""

    off_plan: BatchPlan
    phase: PowerCyclePhase = PowerCyclePhase.OFF_READY
    off_verified: tuple[BatchTarget, ...] = ()
    on_plan: BatchPlan | None = None
    off_journal: Path | None = None
    on_journal: Path | None = None
    excluded: int = 0

    def mark_running(self, plan: BatchPlan) -> None:
        if self.phase is PowerCyclePhase.OFF_READY and plan.plan_id == self.off_plan.plan_id:
            self.phase = PowerCyclePhase.OFF_RUNNING
        elif (
            self.phase is PowerCyclePhase.ON_READY
            and self.on_plan is not None
            and plan.plan_id == self.on_plan.plan_id
        ):
            self.phase = PowerCyclePhase.ON_RUNNING
        else:
            raise BatchControlError("현재 PoE 재가동 단계의 제어 계획과 일치하지 않습니다.")

    def record_off(self, outcome: BatchOutcome) -> None:
        if (
            self.phase is not PowerCyclePhase.OFF_RUNNING
            or outcome.plan.plan_id != self.off_plan.plan_id
        ):
            raise BatchControlError("PoE OFF 작업 결과와 계획이 일치하지 않습니다.")
        self.off_journal = outcome.journal_path
        if len(outcome.results) != len(self.off_plan.targets) or any(
            result.target != target
            for result, target in zip(outcome.results, self.off_plan.targets, strict=True)
        ):
            self.phase = PowerCyclePhase.INTERRUPTED
            return
        if any(result.status is BatchStatus.UNVERIFIED for result in outcome.results):
            self.phase = PowerCyclePhase.INTERRUPTED
            return
        self.off_verified = tuple(
            result.target for result in outcome.results if result.status is BatchStatus.VERIFIED
        )
        self.excluded = len(self.off_plan.targets) - len(self.off_verified)
        self.phase = (
            PowerCyclePhase.REQUERY_OFF if self.off_verified else PowerCyclePhase.INTERRUPTED
        )

    def off_requery_groups(self) -> tuple[PoETarget, ...]:
        if self.phase is not PowerCyclePhase.REQUERY_OFF:
            raise BatchControlError("PoE OFF 확인 후에만 복구 대상 재조회가 가능합니다.")
        return merge_targets(
            [PoETarget(item.target, (item.interface,)) for item in self.off_verified]
        )

    def mark_off_requeried(self) -> None:
        if self.phase is not PowerCyclePhase.REQUERY_OFF:
            raise BatchControlError("PoE OFF 재조회 단계가 아닙니다.")
        self.phase = PowerCyclePhase.ON_PREPARE

    def prepare_on(self, results: Mapping[PortKey, PoEQueryResult]) -> BatchPlan:
        """Recheck exact original targets before offering a *separate* ON plan."""
        if self.phase is not PowerCyclePhase.ON_PREPARE:
            raise BatchControlError("먼저 PoE OFF 대상의 상태를 새로 조회하세요.")
        verified: list[PoEQueryResult] = []
        for target in self.off_verified:
            row = results.get(_key(target))
            snapshot = row.snapshot if row is not None else None
            if (
                row is None
                or not row.complete
                or row.status is not QueryStatus.OK
                or snapshot is None
                or snapshot.hostname != target.expected_hostname
                or snapshot.fingerprint != target.fingerprint
                or snapshot.target != target.target
                or snapshot.interface != target.interface
                or snapshot.poe_enabled is not False
            ):
                raise BatchControlError(
                    "OFF 대상 중 재조회가 실패했거나 스위치 식별·PoE 상태가 일치하지 않습니다. "
                    "해당 포트를 수동으로 확인하고 다시 조회하세요."
                )
            verified.append(row)
        self.on_plan = create_batch_plan(verified, PortAction.POE_ON)
        self.phase = PowerCyclePhase.ON_READY
        return self.on_plan

    def record_on(self, outcome: BatchOutcome) -> None:
        if (
            self.phase is not PowerCyclePhase.ON_RUNNING
            or self.on_plan is None
            or outcome.plan.plan_id != self.on_plan.plan_id
        ):
            raise BatchControlError("PoE ON 작업 결과와 계획이 일치하지 않습니다.")
        self.on_journal = outcome.journal_path
        if (
            len(outcome.results) != len(self.on_plan.targets)
            or any(
                result.target != target
                for result, target in zip(outcome.results, self.on_plan.targets, strict=True)
            )
            or any(
                result.status not in (BatchStatus.VERIFIED, BatchStatus.UNCHANGED)
                for result in outcome.results
            )
        ):
            self.phase = PowerCyclePhase.INTERRUPTED
        else:
            self.phase = PowerCyclePhase.REQUERY_ON

    def on_requery_groups(self) -> tuple[PoETarget, ...]:
        if self.phase is not PowerCyclePhase.REQUERY_ON or self.on_plan is None:
            raise BatchControlError("확인된 PoE ON 작업 이후에만 복구 재조회할 수 있습니다.")
        return merge_targets(
            [PoETarget(item.target, (item.interface,)) for item in self.on_plan.targets]
        )

    def mark_on_requeried(self) -> None:
        if self.phase is not PowerCyclePhase.REQUERY_ON:
            raise BatchControlError("PoE ON 재조회 단계가 아닙니다.")
        self.phase = PowerCyclePhase.VERIFY_ON

    def verify_on(self, results: Mapping[PortKey, PoEQueryResult]) -> None:
        if self.phase is not PowerCyclePhase.VERIFY_ON or self.on_plan is None:
            raise BatchControlError("PoE ON 이후 재조회가 필요합니다.")
        for target in self.on_plan.targets:
            row = results.get(_key(target))
            snapshot = row.snapshot if row is not None else None
            if (
                row is None
                or not row.complete
                or row.status is not QueryStatus.OK
                or snapshot is None
                or snapshot.hostname != target.expected_hostname
                or snapshot.fingerprint != target.fingerprint
                or snapshot.target != target.target
                or snapshot.interface != target.interface
                or snapshot.poe_enabled is not True
            ):
                raise BatchControlError(
                    "복구 재조회에서 PoE ON 또는 장비 식별이 확인되지 않았습니다. "
                    "AP 서비스 상태는 별도로 확인하세요."
                )
        self.phase = PowerCyclePhase.COMPLETE

    def interrupt(self) -> None:
        """No implicit ON, resume, auto retry or rollback on any interruption."""
        self.phase = PowerCyclePhase.INTERRUPTED
