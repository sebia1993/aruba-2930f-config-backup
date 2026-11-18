"""Offline safety and scale tests for unified batch port control."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from aruba2930f_backup.batch_port_control import (
    BatchControlError,
    BatchPlan,
    BatchPortController,
    BatchStatus,
    _BatchJournal,
    create_batch_plan,
    journal_states,
)
from aruba2930f_backup.models import Credentials, DeviceTarget
from aruba2930f_backup.poe_power import LLDPNeighbor, LLDPObservation, LLDPStatus
from aruba2930f_backup.poe_query_models import PoEQueryResult, PoETarget, QueryStatus
from aruba2930f_backup.poe_targets import make_target, merge_targets
from aruba2930f_backup.port_control import PortAction, PortOperationResult, PortSnapshot

CREDS = Credentials("operator", "do-not-save-this-password", "do-not-save-enable-secret")
SWITCH = DeviceTarget("192.0.2.10", 22)


def row(
    port: str,
    *,
    connected_model: str = "AP-535",
    neighbor: bool = True,
    status: QueryStatus = QueryStatus.OK,
) -> PoEQueryResult:
    snapshot = PortSnapshot(
        SWITCH,
        port,
        "test-switch",
        "Aruba 2930F",
        True,
        "Up",
        True,
        "Delivering",
        12.3,
        "SHA256:approved-fixture",
        "2026-11-17T00:00:00Z",
    )
    lldp = LLDPObservation(
        LLDPStatus.OK,
        (
            LLDPNeighbor(
                system_name=f"AP-{port}",
                model=connected_model,
                chassis_id=f"chassis-{port}",
                port_id=f"eth-{port}",
            ),
        )
        if neighbor
        else (),
    )
    return PoEQueryResult(
        PoETarget(SWITCH, (port,)),
        port,
        status,
        snapshot=snapshot,
        lldp=lldp,
        complete=True,
    )


class FakeControl:
    def __init__(self, *, actual: PortSnapshot | None = None, result: str = "verified") -> None:
        self.actual = actual
        self.result = result
        self.inspections: list[str] = []
        self.applied: list[tuple[str, PortAction]] = []

    def inspect(
        self,
        target: DeviceTarget,
        interface: str,
        credentials: Credentials,
        *,
        approve_host_key: object,
    ) -> PortSnapshot:
        assert credentials is CREDS
        assert callable(approve_host_key)
        self.inspections.append(interface)
        return self.actual or row(interface).snapshot  # type: ignore[return-value]

    def apply(
        self,
        snapshot: PortSnapshot,
        action: PortAction,
        credentials: Credentials,
        *,
        confirmation: str,
        expected_lldp_identity: tuple[str, str] | None = None,
    ) -> PortOperationResult:
        assert credentials is CREDS
        if action in (PortAction.POE_OFF, PortAction.DISABLE):
            assert expected_lldp_identity == (
                f"chassis-{snapshot.interface}",
                f"eth-{snapshot.interface}",
            )
        assert confirmation == snapshot.confirmation_text
        self.applied.append((snapshot.interface, action))
        return PortOperationResult(self.result, self.result, snapshot, snapshot)


def test_plan_binds_selected_port_and_action_without_live_mutation() -> None:
    plan = create_batch_plan([row("12"), row("24")], PortAction.POE_OFF)
    assert [(t.target.endpoint, t.interface, t.ap_name) for t in plan.targets] == [
        ("192.0.2.10:22", "12", "AP-12"),
        ("192.0.2.10:22", "24", "AP-24"),
    ]
    assert plan.action is PortAction.POE_OFF
    assert plan.targets[0].expected_chassis_id == "chassis-12"
    assert plan.targets[0].expected_remote_port_id == "eth-12"
    assert plan.plan_id


def test_ap535_without_lldp_chassis_or_remote_port_is_not_safe_for_bulk_off() -> None:
    candidate = row("7")
    assert candidate.lldp is not None
    without_chassis = replace(
        candidate,
        lldp=LLDPObservation(LLDPStatus.OK, (replace(candidate.lldp.neighbors[0], chassis_id=""),)),
    )
    without_port = replace(
        candidate,
        lldp=LLDPObservation(LLDPStatus.OK, (replace(candidate.lldp.neighbors[0], port_id=""),)),
    )
    for unsafe in (without_chassis, without_port):
        with pytest.raises(BatchControlError, match="Chassis ID"):
            create_batch_plan([unsafe], PortAction.POE_OFF)
        assert create_batch_plan([unsafe], PortAction.POE_ON).targets


def test_missing_hostname_poe_on_uses_pinned_identity_and_fresh_preflight(
    tmp_path: Path,
) -> None:
    observed = row("8")
    assert observed.snapshot is not None
    unknown = replace(
        observed,
        snapshot=replace(
            observed.snapshot, hostname="확인 불가", poe_enabled=False
        ),
    )
    plan = create_batch_plan([unknown], PortAction.POE_ON)
    target = plan.targets[0]
    assert target.allow_unknown_hostname
    assert target.expected_model == "Aruba 2930F"
    assert target.fingerprint.startswith("SHA256:")

    # The fresh read may recover a usable hostname. The pinned SSH identity,
    # target, model and PoE setting still have to match before any write.
    fresh = replace(unknown.snapshot, hostname="SW-LAB-08")
    controller = FakeControl(actual=fresh)
    outcome = BatchPortController(
        controller_factory=lambda: controller,
        journal_directory=tmp_path,
    ).execute(plan, CREDS)
    assert outcome.results[0].status is BatchStatus.VERIFIED
    assert controller.applied == [("8", PortAction.POE_ON)]

    for action in (PortAction.POE_OFF, PortAction.DISABLE, PortAction.ENABLE):
        with pytest.raises(BatchControlError):
            create_batch_plan([unknown], action)
    with pytest.raises(BatchControlError, match="PoE ON"):
        BatchPlan(plan.plan_id, plan.created_at, PortAction.POE_OFF, plan.targets)

    invalid_model = replace(
        unknown, snapshot=replace(unknown.snapshot, model="모델 확인 불가")
    )
    with pytest.raises(BatchControlError, match="2930F"):
        create_batch_plan([invalid_model], PortAction.POE_ON)
    invalid_key = replace(
        unknown, snapshot=replace(unknown.snapshot, fingerprint="")
    )
    with pytest.raises(BatchControlError, match="SSH 지문"):
        create_batch_plan([invalid_key], PortAction.POE_ON)


def test_missing_hostname_poe_on_does_not_skip_identity_and_port_safety(
    tmp_path: Path,
) -> None:
    observed = row("9")
    assert observed.snapshot is not None
    unknown = replace(
        observed,
        snapshot=replace(observed.snapshot, hostname="확인 불가", poe_enabled=False),
    )
    variants = (
        replace(unknown.snapshot, fingerprint="SHA256:unexpected"),
        replace(unknown.snapshot, model="Aruba 2930F VSF"),
        replace(unknown.snapshot, enabled=False),
        replace(unknown.snapshot, poe_enabled=True),
        replace(unknown.snapshot, target=DeviceTarget("192.0.2.11", 22)),
        replace(unknown.snapshot, interface="10"),
    )
    for index, fresh in enumerate(variants):
        fake = FakeControl(actual=fresh)
        outcome = BatchPortController(
            controller_factory=lambda fake=fake: fake,
            journal_directory=tmp_path / str(index),
        ).execute(create_batch_plan([unknown], PortAction.POE_ON), CREDS)
        assert outcome.results[0].status is BatchStatus.PREFLIGHT_FAILED
        assert fake.applied == []


def test_unconfirmed_ap_cannot_be_mass_powered_off_or_disabled() -> None:
    for candidate in (row("7", neighbor=False), row("7", connected_model="2930F")):
        for action in (PortAction.POE_OFF, PortAction.DISABLE):
            with pytest.raises(BatchControlError, match="AP-535"):
                create_batch_plan([candidate], action)
        # An ON command does not disconnect a live, possibly critical port.
        assert create_batch_plan([candidate], PortAction.POE_ON).targets


def test_malformed_duplicate_and_stale_selection_rejected_atomically() -> None:
    with pytest.raises(BatchControlError, match="중복"):
        create_batch_plan([row("1"), row("1")], PortAction.POE_ON)
    with pytest.raises(BatchControlError, match="완료"):
        create_batch_plan([replace(row("2"), complete=False)], PortAction.POE_ON)
    with pytest.raises(BatchControlError, match="완료"):
        create_batch_plan([replace(row("2"), status=QueryStatus.FAILED)], PortAction.POE_ON)


def test_success_is_written_before_advancing_and_password_never_persisted(tmp_path: Path) -> None:
    fake = FakeControl()
    plan = create_batch_plan([row("1"), row("2")], PortAction.POE_OFF)
    completed = BatchPortController(
        controller_factory=lambda: fake, journal_directory=tmp_path
    ).execute(plan, CREDS)
    assert [r.status for r in completed.results] == [BatchStatus.VERIFIED] * 2
    assert fake.inspections == ["1", "2"]
    assert fake.applied == [("1", PortAction.POE_OFF), ("2", PortAction.POE_OFF)]
    events = [
        json.loads(line) for line in completed.journal_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in events] == [
        "plan",
        "preflight",
        "intent",
        "result",
        "preflight",
        "intent",
        "result",
    ]
    assert journal_states(completed.journal_path) == {0: "verified", 1: "verified"}
    journal = completed.journal_path.read_text(encoding="utf-8")
    assert CREDS.password not in journal
    assert CREDS.enable_secret not in journal


def test_changed_host_identity_or_config_skips_mutation(tmp_path: Path) -> None:
    first = row("1")
    assert first.snapshot is not None
    fake = FakeControl(actual=replace(first.snapshot, enabled=False))
    outcome = BatchPortController(
        controller_factory=lambda: fake, journal_directory=tmp_path
    ).execute(create_batch_plan([first], PortAction.POE_OFF), CREDS)
    assert outcome.results[0].status is BatchStatus.PREFLIGHT_FAILED
    assert fake.applied == []
    assert journal_states(outcome.journal_path) == {0: "preflight_failed"}


def test_unknown_write_outcome_stops_remaining_targets_without_replay(tmp_path: Path) -> None:
    fake = FakeControl(result="unverified")
    plan = create_batch_plan([row("1"), row("2"), row("3")], PortAction.POE_OFF)
    outcome = BatchPortController(
        controller_factory=lambda: fake, journal_directory=tmp_path
    ).execute(plan, CREDS)
    assert [result.status for result in outcome.results] == [
        BatchStatus.UNVERIFIED,
        BatchStatus.PAUSED,
        BatchStatus.PAUSED,
    ]
    assert len(fake.applied) == 1
    assert journal_states(outcome.journal_path)[0] == "unverified"


def test_checkpoint_requires_operator_reapproval_after_ten(tmp_path: Path) -> None:
    ports = [row(str(i)) for i in range(1, 13)]
    plan = create_batch_plan(ports, PortAction.POE_ON)
    fake = FakeControl()
    stopped = BatchPortController(
        controller_factory=lambda: fake, journal_directory=tmp_path
    ).execute(plan, CREDS)
    assert sum(x.status is BatchStatus.VERIFIED for x in stopped.results) == 10
    assert [x.status for x in stopped.results[10:]] == [BatchStatus.PAUSED] * 2

    calls: list[tuple[int, int]] = []
    fake2 = FakeControl()
    complete = BatchPortController(
        controller_factory=lambda: fake2,
        journal_directory=tmp_path,
    ).execute(
        create_batch_plan(ports, PortAction.POE_ON),
        CREDS,
        continue_after_chunk=lambda done, total: calls.append((done, total)) or True,
    )
    assert calls == [(10, 12)]
    assert sum(x.status is BatchStatus.VERIFIED for x in complete.results) == 12


def test_cancelled_before_start_never_changes_port(tmp_path: Path) -> None:
    fake = FakeControl()
    event = threading.Event()
    event.set()
    outcome = BatchPortController(
        controller_factory=lambda: fake, journal_directory=tmp_path
    ).execute(create_batch_plan([row("1"), row("2")], PortAction.POE_ON), CREDS, cancel_event=event)
    assert all(r.status is BatchStatus.CANCELLED for r in outcome.results)
    assert fake.inspections == []
    assert fake.applied == []


def test_duplicate_plan_journal_cannot_be_replayed(tmp_path: Path) -> None:
    fake = FakeControl()
    controller = BatchPortController(controller_factory=lambda: fake, journal_directory=tmp_path)
    plan = create_batch_plan([row("1")], PortAction.POE_ON)
    controller.execute(plan, CREDS)
    with pytest.raises(BatchControlError, match="기록"):
        controller.execute(plan, CREDS)
    assert len(fake.applied) == 1


def test_crash_after_intent_is_not_marked_safe_to_retry(tmp_path: Path) -> None:
    plan = create_batch_plan([row("1"), row("2")], PortAction.POE_ON)
    journal = _BatchJournal(tmp_path / "power-cycle.jsonl", plan)
    journal.append(0, "preflight", status="ok")
    journal.append(0, "intent", action="poe_on")
    assert journal_states(journal.path) == {0: "result_unknown", 1: "queued"}


def test_journal_failure_before_intent_prevents_write_and_stops_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _BatchJournal.append

    def fail_intent(self: _BatchJournal, index: int, event: str, **fields: object) -> None:
        if event == "intent":
            raise BatchControlError("작업 이력을 저장할 수 없습니다.")
        original(self, index, event, **fields)

    monkeypatch.setattr(_BatchJournal, "append", fail_intent)
    fake = FakeControl()
    with pytest.raises(BatchControlError):
        BatchPortController(controller_factory=lambda: fake, journal_directory=tmp_path).execute(
            create_batch_plan([row("1"), row("2")], PortAction.POE_ON), CREDS
        )
    assert fake.applied == []


def test_300_switches_and_14400_selected_ports_fit_and_301_stops() -> None:
    targets = [
        make_target(
            f"{'192.0.2' if i < 250 else '198.51.100'}.{i % 250 + 1}",
            22,
            ",".join(str(port) for port in range(1, 49)),
        )
        for i in range(300)
    ]
    assert len(merge_targets(targets)) == 300
    assert sum(len(target.interfaces) for target in merge_targets(targets)) == 14_400
    with pytest.raises(ValueError, match="300"):
        merge_targets([*targets, make_target("203.0.113.250", 22, "1")])


def test_cached_lldp_ap_is_not_enough_when_port_link_is_down() -> None:
    checked = row("7")
    assert checked.snapshot is not None
    disconnected = replace(checked, snapshot=replace(checked.snapshot, link_status="Down"))
    for action in (PortAction.POE_OFF, PortAction.DISABLE):
        with pytest.raises(BatchControlError, match="Link Up"):
            create_batch_plan([disconnected], action)
    assert create_batch_plan([disconnected], PortAction.POE_ON).targets


def test_known_prewrite_failure_is_recorded_without_stopping_other_ports(tmp_path: Path) -> None:
    from aruba2930f_backup.models import ErrorCode
    from aruba2930f_backup.port_control import PreMutationRejected

    class RejectFirst(FakeControl):
        def apply(
            self,
            snapshot: PortSnapshot,
            action: PortAction,
            credentials: Credentials,
            *,
            confirmation: str,
            expected_lldp_identity: tuple[str, str] | None = None,
        ) -> PortOperationResult:
            if snapshot.interface == "1":
                raise PreMutationRejected(ErrorCode.INPUT_INVALID, "사전 LLDP 확인 실패")
            return super().apply(
                snapshot,
                action,
                credentials,
                confirmation=confirmation,
                expected_lldp_identity=expected_lldp_identity,
            )

    fake = RejectFirst()
    plan = create_batch_plan([row("1"), row("2")], PortAction.POE_OFF)
    outcome = BatchPortController(
        controller_factory=lambda: fake, journal_directory=tmp_path
    ).execute(plan, CREDS)
    assert [result.status for result in outcome.results] == [
        BatchStatus.PREFLIGHT_FAILED,
        BatchStatus.VERIFIED,
    ]
    assert fake.applied == [("2", PortAction.POE_OFF)]
    assert journal_states(outcome.journal_path) == {
        0: "preflight_failed",
        1: "verified",
    }


def test_batch_emits_current_port_stages_without_credentials(tmp_path: Path) -> None:
    fake = FakeControl()
    plan = create_batch_plan([row("12")], PortAction.POE_ON)
    stages: list[tuple[int, str, str, str]] = []
    result = BatchPortController(
        controller_factory=lambda: fake, journal_directory=tmp_path
    ).execute(
        plan,
        CREDS,
        on_stage=lambda index, target, phase: stages.append(
            (index, target.target.endpoint, target.interface, phase)
        ),
    )
    assert result.results[0].status is BatchStatus.VERIFIED
    assert stages == [
        (0, SWITCH.endpoint, "12", "preflight"),
        (0, SWITCH.endpoint, "12", "apply"),
    ]
    assert CREDS.password not in repr(stages)
