"""Safety and staging regression tests for guided PoE power cycling."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from aruba2930f_backup.batch_port_control import (
    BatchControlError,
    BatchOutcome,
    BatchResult,
    BatchStatus,
)
from aruba2930f_backup.models import DeviceTarget
from aruba2930f_backup.poe_cycle import (
    PowerCyclePhase,
    PowerCycleSession,
    prepare_cycle_off,
)
from aruba2930f_backup.poe_power import LLDPNeighbor, LLDPObservation, LLDPStatus
from aruba2930f_backup.poe_query_models import PoEQueryResult, PoETarget, QueryStatus
from aruba2930f_backup.port_control import PortAction, PortSnapshot

TARGET = DeviceTarget("192.0.2.10", 22)


def measured(port: str, *, enabled: bool = True, model: str = "AP-535") -> PoEQueryResult:
    snapshot = PortSnapshot(
        TARGET,
        port,
        "F1-2930F-01",
        "Aruba 2930F",
        True,
        "Up",
        enabled,
        "Delivering" if enabled else "Disabled",
        13.5 if enabled else 0,
        "SHA256:operator-verified",
    )
    lldp = LLDPObservation(
        LLDPStatus.OK,
        (LLDPNeighbor(system_name=f"AP-{port}", model=model),) if enabled else (),
    )
    return PoEQueryResult(
        PoETarget(TARGET, (port,)),
        port,
        QueryStatus.OK,
        snapshot=snapshot,
        lldp=lldp,
    )


def batch_result(
    plan: object, statuses: tuple[BatchStatus, ...], name: str = "test.jsonl"
) -> BatchOutcome:
    from aruba2930f_backup.batch_port_control import BatchPlan

    assert isinstance(plan, BatchPlan)
    return BatchOutcome(
        plan,
        tuple(
            BatchResult(target, status, "synthetic off/on result")
            for target, status in zip(plan.targets, statuses, strict=True)
        ),
        Path(name),
    )


def off_success(count: int = 2) -> PowerCycleSession:
    ports = [measured(str(i)) for i in range(1, count + 1)]
    off = prepare_cycle_off(ports)
    session = PowerCycleSession(off)
    session.mark_running(off)
    session.record_off(batch_result(off, (BatchStatus.VERIFIED,) * count))
    return session


def test_cycle_off_accepts_only_confirmed_ap535_and_powered_ports() -> None:
    off = prepare_cycle_off([measured("1"), measured("2")])
    assert off.action is PortAction.POE_OFF
    assert len(off.targets) == 2
    with pytest.raises(BatchControlError, match="현재 PoE ON"):
        prepare_cycle_off([measured("1", enabled=False)])
    with pytest.raises(BatchControlError, match="AP-535"):
        prepare_cycle_off([measured("1", model="Aruba 2930F")])
    with pytest.raises(BatchControlError):
        prepare_cycle_off([])


def test_off_never_dispatches_on_and_requires_new_read_only_query() -> None:
    session = off_success()
    assert session.phase is PowerCyclePhase.REQUERY_OFF
    assert session.on_plan is None
    assert session.off_journal == Path("test.jsonl")
    assert [(g.target.endpoint, g.interfaces) for g in session.off_requery_groups()] == [
        (TARGET.endpoint, ("1", "2"))
    ]
    with pytest.raises(BatchControlError, match="먼저"):
        session.prepare_on({})


def test_requery_binds_on_to_only_verified_off_targets() -> None:
    plan = prepare_cycle_off([measured("1"), measured("2"), measured("3")])
    session = PowerCycleSession(plan)
    session.mark_running(plan)
    session.record_off(
        batch_result(
            plan,
            (BatchStatus.VERIFIED, BatchStatus.PREFLIGHT_FAILED, BatchStatus.PAUSED),
        )
    )
    assert session.off_verified == (plan.targets[0],)
    assert session.excluded == 2
    assert session.off_requery_groups()[0].interfaces == ("1",)
    session.mark_off_requeried()
    on = session.prepare_on({(TARGET.endpoint, "1"): measured("1", enabled=False)})
    assert on.action is PortAction.POE_ON
    assert [target.interface for target in on.targets] == ["1"]
    assert session.phase is PowerCyclePhase.ON_READY
    assert on.plan_id != plan.plan_id


@pytest.mark.parametrize(
    "row",
    [
        measured("1"),
        replace(measured("1", enabled=False), status=QueryStatus.FAILED),
        replace(measured("1", enabled=False), complete=False),
        replace(
            measured("1", enabled=False),
            snapshot=replace(measured("1").snapshot, hostname="unexpected-switch"),
        ),
        replace(
            measured("1", enabled=False),
            snapshot=replace(measured("1").snapshot, fingerprint="SHA256:changed-key"),
        ),
    ],
)
def test_on_refuses_stale_missing_failed_or_changed_identity(
    row: PoEQueryResult,
) -> None:
    session = off_success(1)
    session.mark_off_requeried()
    with pytest.raises(BatchControlError, match="OFF 대상"):
        session.prepare_on({(TARGET.endpoint, "1"): row})
    assert session.on_plan is None
    assert session.phase is PowerCyclePhase.ON_PREPARE


def test_off_result_unknown_never_automatically_restarts_mutation() -> None:
    plan = prepare_cycle_off([measured("1"), measured("2")])
    session = PowerCycleSession(plan)
    session.mark_running(plan)
    session.record_off(batch_result(plan, (BatchStatus.VERIFIED, BatchStatus.UNVERIFIED)))
    assert session.phase is PowerCyclePhase.INTERRUPTED
    assert session.on_plan is None
    with pytest.raises(BatchControlError):
        session.off_requery_groups()


def test_unapproved_next_batch_is_partial_but_verified_off_can_be_recovered() -> None:
    plan = prepare_cycle_off([measured("1"), measured("2")])
    session = PowerCycleSession(plan)
    session.mark_running(plan)
    session.record_off(batch_result(plan, (BatchStatus.VERIFIED, BatchStatus.CANCELLED)))
    assert session.phase is PowerCyclePhase.REQUERY_OFF
    assert len(session.off_verified) == 1
    assert session.excluded == 1


def test_on_is_separately_approved_then_verified_after_requery() -> None:
    session = off_success(1)
    session.mark_off_requeried()
    on = session.prepare_on({(TARGET.endpoint, "1"): measured("1", enabled=False)})
    with pytest.raises(BatchControlError):
        session.verify_on({})
    session.mark_running(on)
    session.record_on(batch_result(on, (BatchStatus.VERIFIED,), "on.jsonl"))
    assert session.on_journal == Path("on.jsonl")
    assert session.phase is PowerCyclePhase.REQUERY_ON
    assert session.on_requery_groups()[0].interfaces == ("1",)
    session.mark_on_requeried()
    with pytest.raises(BatchControlError, match="복구"):
        session.verify_on({(TARGET.endpoint, "1"): measured("1", enabled=False)})
    assert session.phase is PowerCyclePhase.VERIFY_ON
    session.verify_on({(TARGET.endpoint, "1"): measured("1")})
    assert session.phase is PowerCyclePhase.COMPLETE


def test_any_on_write_uncertainty_interrupts_and_keeps_both_audit_paths() -> None:
    session = off_success(1)
    session.mark_off_requeried()
    on = session.prepare_on({(TARGET.endpoint, "1"): measured("1", enabled=False)})
    session.mark_running(on)
    session.record_on(batch_result(on, (BatchStatus.UNVERIFIED,), "unknown.jsonl"))
    assert session.phase is PowerCyclePhase.INTERRUPTED
    assert session.off_journal is not None
    assert session.on_journal == Path("unknown.jsonl")
    with pytest.raises(BatchControlError):
        session.on_requery_groups()


def test_unrelated_plan_and_disallowed_phase_do_not_advance_cycle() -> None:
    session = off_success(1)
    unrelated = prepare_cycle_off([measured("2")])
    with pytest.raises(BatchControlError):
        session.mark_running(unrelated)
    with pytest.raises(BatchControlError):
        session.record_off(batch_result(unrelated, (BatchStatus.VERIFIED,)))
    session.interrupt()
    assert session.phase is PowerCyclePhase.INTERRUPTED


def test_more_than_300_ports_must_be_split_into_explicit_jobs() -> None:
    with pytest.raises(BatchControlError, match="300"):
        prepare_cycle_off([measured(str(i)) for i in range(1, 302)])


def test_off_results_out_of_order_cannot_arm_the_on_phase() -> None:
    plan = prepare_cycle_off([measured("1"), measured("2")])
    session = PowerCycleSession(plan)
    session.mark_running(plan)
    outcome = batch_result(plan, (BatchStatus.VERIFIED, BatchStatus.VERIFIED))
    switched = BatchOutcome(plan, tuple(reversed(outcome.results)), outcome.journal_path)
    session.record_off(switched)
    assert session.phase is PowerCyclePhase.INTERRUPTED
    assert session.off_verified == ()
    assert session.on_plan is None


def test_on_results_out_of_order_cannot_be_reported_as_recovered() -> None:
    session = off_success(2)
    session.mark_off_requeried()
    lookup = {(TARGET.endpoint, port): measured(port, enabled=False) for port in ("1", "2")}
    on = session.prepare_on(lookup)
    session.mark_running(on)
    outcome = batch_result(on, (BatchStatus.VERIFIED, BatchStatus.VERIFIED))
    session.record_on(BatchOutcome(on, tuple(reversed(outcome.results)), outcome.journal_path))
    assert session.phase is PowerCyclePhase.INTERRUPTED
