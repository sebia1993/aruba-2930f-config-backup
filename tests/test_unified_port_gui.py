"""Exercise the unified Qt controls without contacting a real switch."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from aruba2930f_backup.batch_port_control import BatchOutcome, BatchResult, BatchStatus
from aruba2930f_backup.models import DeviceTarget
from aruba2930f_backup.poe_power import LLDPNeighbor, LLDPObservation, LLDPStatus
from aruba2930f_backup.poe_query_models import PoEQueryResult, PoETarget, QueryStatus
from aruba2930f_backup.poe_quick_review_dialog import QuickPoEReviewDialog
from aruba2930f_backup.port_control import PortAction, PortSnapshot
from aruba2930f_backup.unified_port_gui import (
    BatchConfirmationDialog,
    UnifiedPortManagementDialog,
)

TARGET = DeviceTarget("192.0.2.10", 22)


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def queried(port: str, *, model: str = "AP-535") -> PoEQueryResult:
    group = PoETarget(TARGET, (port,))
    snapshot = PortSnapshot(
        TARGET,
        port,
        "lab-switch",
        "Aruba 2930F",
        True,
        "Up",
        True,
        "Delivering",
        13.2,
        "SHA256:test-verified",
    )
    lldp = LLDPObservation(
        LLDPStatus.OK,
        (
            LLDPNeighbor(
                system_name=f"AP-{port}",
                model=model,
                chassis_id=f"chassis-{port}",
                port_id=f"eth-{port}",
            ),
        ),
    )
    return PoEQueryResult(group, port, QueryStatus.OK, snapshot=snapshot, lldp=lldp)


def setup_dialog() -> UnifiedPortManagementDialog:
    dialog = UnifiedPortManagementDialog(
        initial_ips=("192.0.2.10",),
        username="operator",
        password="test-password",
    )
    group = PoETarget(TARGET, ("1", "2"))
    dialog._build_results((group,))
    dialog._on_result(queried("1"))
    dialog._on_result(queried("2"))
    return dialog


def test_unified_window_shows_one_console_and_multi_selects_ports(app: QApplication) -> None:
    dialog = setup_dialog()
    assert dialog.windowTitle() == "Aruba 2930F · 포트 통합 관리"
    assert dialog.result_tree.selectionMode() == dialog.result_tree.SelectionMode.SingleSelection
    first = dialog._items[(TARGET.endpoint, "1")]
    second = dialog._items[(TARGET.endpoint, "2")]
    dialog.result_tree.setCurrentItem(first)
    first.setCheckState(0, Qt.CheckState.Checked)
    second.setCheckState(0, Qt.CheckState.Checked)
    assert len(dialog._selected_rows()) == 2
    dialog.action_combo.setCurrentIndex(dialog.action_combo.findData(PortAction.POE_OFF.value))
    dialog._prepare_batch()
    assert dialog._batch_plan is not None
    assert len(dialog._batch_plan.targets) == 2
    assert dialog.run_button.isEnabled()
    dialog.close()


def test_confirmation_requires_exact_batch_size_text(app: QApplication) -> None:
    plan = setup_dialog()
    plan.result_tree.setCurrentItem(plan._items[(TARGET.endpoint, "1")])
    plan._items[(TARGET.endpoint, "1")].setCheckState(0, Qt.CheckState.Checked)
    plan._prepare_batch()
    assert plan._batch_plan is not None
    dialog = BatchConfirmationDialog(plan._batch_plan)
    assert not dialog.execute_button.isEnabled()
    dialog.confirm_input.setText("EXECUTE 300")
    assert not dialog.execute_button.isEnabled()
    dialog.confirm_input.setText("EXECUTE 1")
    assert dialog.execute_button.isEnabled()
    dialog.close()
    plan.close()


def test_destructive_batch_refuses_non_ap_neighbor(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = setup_dialog()
    dialog._on_result(queried("2", model="Aruba 2930F"))
    second = dialog._items[(TARGET.endpoint, "2")]
    dialog.result_tree.setCurrentItem(second)
    second.setCheckState(0, Qt.CheckState.Checked)
    dialog.action_combo.setCurrentIndex(dialog.action_combo.findData(PortAction.DISABLE.value))
    shown: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: shown.append(message) or QMessageBox.StandardButton.Ok,
    )
    dialog._prepare_batch()
    assert dialog._batch_plan is None
    assert shown and "AP-535" in shown[0]
    dialog.close()


class OfflineBatch:
    def execute(
        self,
        plan: object,
        credentials: object,
        *,
        cancel_event: object = None,
        on_result: object = None,
        continue_after_chunk: object = None,
    ) -> BatchOutcome:
        del credentials, cancel_event, continue_after_chunk
        from aruba2930f_backup.batch_port_control import BatchPlan

        assert isinstance(plan, BatchPlan)
        results = tuple(
            BatchResult(item, BatchStatus.VERIFIED, "설정 변경 확인") for item in plan.targets
        )
        if callable(on_result):
            for index, value in enumerate(results):
                on_result(index, value)
        return BatchOutcome(plan, results, Path("/tmp/synthetic-batch.jsonl"))


def test_batch_execution_clears_stale_ap_and_power_until_requery(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = setup_dialog()
    dialog._batch_controller = OfflineBatch()  # type: ignore[assignment]
    dialog.result_tree.setCurrentItem(dialog._items[(TARGET.endpoint, "1")])
    dialog._items[(TARGET.endpoint, "1")].setCheckState(0, Qt.CheckState.Checked)
    dialog._prepare_batch()
    assert dialog._batch_plan is not None
    monkeypatch.setattr(BatchConfirmationDialog, "exec", lambda _self: QDialog.DialogCode.Accepted)
    dialog._run_batch()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and dialog._batch_thread is not None:
        app.processEvents()
        time.sleep(0.01)
    assert dialog._batch_thread is None
    assert dialog._batch_outcome is not None
    item = dialog._items[(TARGET.endpoint, "1")]
    assert item.text(1) == "재조회 필요"
    assert item.text(2) == "—"
    assert (TARGET.endpoint, "1") not in dialog._results
    assert (TARGET.endpoint, "2") in dialog._results
    dialog.close()


def test_unified_tree_accepts_300_switches_with_14400_ports(app: QApplication) -> None:
    dialog = UnifiedPortManagementDialog(username="operator", password="test-password")
    groups = tuple(
        PoETarget(
            DeviceTarget(f"{'192.0.2' if i < 250 else '198.51.100'}.{i % 250 + 1}", 22),
            tuple(str(number) for number in range(1, 49)),
        )
        for i in range(300)
    )
    dialog._build_results(groups)
    assert len(dialog._groups) == 300
    assert len(dialog._parents) == 300
    assert len(dialog._items) == 14_400
    dialog.close()


def test_search_filters_by_ap_name_and_preserves_underlying_results(app: QApplication) -> None:
    dialog = setup_dialog()
    first = dialog._items[(TARGET.endpoint, "1")]
    second = dialog._items[(TARGET.endpoint, "2")]
    dialog.search_input.setText("AP-2")
    dialog._apply_search_filter()
    assert first.isHidden()
    assert not second.isHidden()
    assert len(dialog._results) == 2
    dialog.search_input.clear()
    dialog._apply_search_filter()
    assert not first.isHidden()
    assert not second.isHidden()
    dialog.close()


def test_failure_filter_does_not_mark_unqueried_port_as_failure(app: QApplication) -> None:
    dialog = setup_dialog()
    dialog._on_result(
        PoEQueryResult(
            PoETarget(TARGET, ("2",)),
            "2",
            QueryStatus.FAILED,
            message="synthetic query failed",
            complete=True,
        )
    )
    dialog.filter_combo.setCurrentIndex(dialog.filter_combo.findData("failed"))
    dialog._apply_search_filter()
    assert dialog._items[(TARGET.endpoint, "1")].isHidden()
    assert not dialog._items[(TARGET.endpoint, "2")].isHidden()
    dialog.filter_combo.setCurrentIndex(0)
    dialog._apply_search_filter()
    assert not dialog._items[(TARGET.endpoint, "1")].isHidden()
    dialog.close()


def test_filter_hides_but_preserves_checked_ports_and_invalidates_plan(app: QApplication) -> None:
    dialog = setup_dialog()
    first = dialog._items[(TARGET.endpoint, "1")]
    second = dialog._items[(TARGET.endpoint, "2")]
    dialog.result_tree.setCurrentItem(first)
    first.setCheckState(0, Qt.CheckState.Checked)
    second.setCheckState(0, Qt.CheckState.Checked)
    dialog._prepare_batch()
    assert dialog._batch_plan is not None
    assert len(dialog._batch_plan.targets) == 2
    dialog.search_input.setText("AP-2")
    assert dialog._batch_plan is None
    dialog._apply_search_filter()
    assert first.isHidden()
    assert first.checkState(0) is Qt.CheckState.Checked
    assert not second.isHidden()
    assert tuple(row.interface for row in dialog._selected_rows()) == ("1", "2")
    dialog._update_selection_summary()
    assert "화면 밖 1개" in dialog.selection_summary.text()
    assert not dialog.run_button.isEnabled()
    dialog.close()


def test_change_action_after_plan_forces_new_explicit_approval(app: QApplication) -> None:
    dialog = setup_dialog()
    dialog.result_tree.setCurrentItem(dialog._items[(TARGET.endpoint, "1")])
    dialog._items[(TARGET.endpoint, "1")].setCheckState(0, Qt.CheckState.Checked)
    dialog._prepare_batch()
    assert dialog._batch_plan is not None
    dialog.action_combo.setCurrentIndex(dialog.action_combo.findData(PortAction.POE_OFF.value))
    assert dialog._batch_plan is None
    assert not dialog.run_button.isEnabled()
    dialog.close()


def test_failed_batch_without_result_callbacks_invalidates_planned_ports(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aruba2930f_backup.batch_port_control import BatchControlError

    class FailedAuditController:
        def execute(self, plan: object, credentials: object, **kwargs: object) -> None:
            del plan, credentials, kwargs
            # Represents a durable result record failure after mutation.
            raise BatchControlError("result journal append failed")

    dialog = setup_dialog()
    dialog._batch_controller = FailedAuditController()  # type: ignore[assignment]
    first = dialog._items[(TARGET.endpoint, "1")]
    first.setCheckState(0, Qt.CheckState.Checked)
    dialog._prepare_batch()
    assert dialog._batch_plan is not None
    monkeypatch.setattr(BatchConfirmationDialog, "exec", lambda _self: QDialog.DialogCode.Accepted)
    dialog._run_batch()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and dialog._batch_thread is not None:
        app.processEvents()
        time.sleep(0.01)
    assert dialog._batch_thread is None
    assert dialog._batch_outcome is None
    assert (TARGET.endpoint, "1") not in dialog._results
    assert (TARGET.endpoint, "2") in dialog._results
    assert first.text(1) == "재조회 필요"
    assert first.text(2) == "—"
    assert "결과 불확실" in first.text(5)
    assert dialog._batch_plan is None
    assert not dialog.run_button.isEnabled()
    dialog.close()


def test_select_all_scope_and_eligible_only_keep_unsafe_ports_unselected(
    app: QApplication,
) -> None:
    dialog = setup_dialog()
    dialog.show()
    first = dialog._items[(TARGET.endpoint, "1")]
    second = dialog._items[(TARGET.endpoint, "2")]
    dialog._on_result(queried("2", model="Aruba 2930F"))
    dialog.action_combo.setCurrentIndex(dialog.action_combo.findData(PortAction.POE_OFF.value))
    dialog.select_all_button.click()
    assert (
        first.checkState(0) is Qt.CheckState.Checked
        and second.checkState(0) is Qt.CheckState.Checked
    )
    assert "체크 2개" in dialog.selection_summary.text()
    assert "가능 1개" in dialog.selection_summary.text()
    dialog.select_eligible_button.click()
    assert (
        first.checkState(0) is Qt.CheckState.Checked
        and second.checkState(0) is Qt.CheckState.Unchecked
    )
    assert len(dialog._selected_rows()) == 1
    dialog.clear_selection_button.click()
    assert (
        first.checkState(0) is Qt.CheckState.Unchecked
        and second.checkState(0) is Qt.CheckState.Unchecked
    )
    dialog.close()


def test_select_current_switch_and_filter_results_without_selecting_group_parents(
    app: QApplication,
) -> None:
    dialog = setup_dialog()
    dialog.show()
    first = dialog._items[(TARGET.endpoint, "1")]
    second = dialog._items[(TARGET.endpoint, "2")]
    dialog.result_tree.setCurrentItem(first)
    dialog.selection_scope.setCurrentIndex(dialog.selection_scope.findData("switch"))
    dialog.select_all_button.click()
    assert (
        first.checkState(0) is Qt.CheckState.Checked
        and second.checkState(0) is Qt.CheckState.Checked
    )
    assert dialog._parents[TARGET.endpoint].checkState(0) is Qt.CheckState.Checked
    dialog.search_input.setText("AP-2")
    dialog._apply_search_filter()
    dialog.selection_scope.setCurrentIndex(dialog.selection_scope.findData("filtered"))
    dialog.select_all_button.click()
    assert (
        first.checkState(0) is Qt.CheckState.Unchecked
        and second.checkState(0) is Qt.CheckState.Checked
    )
    dialog.selection_scope.setCurrentIndex(dialog.selection_scope.findData("all"))
    dialog.select_all_button.click()
    assert dialog.search_input.text() == "AP-2"
    assert (
        first.checkState(0) is Qt.CheckState.Checked
        and second.checkState(0) is Qt.CheckState.Checked
    )
    dialog.close()


def test_select_all_over_300_warns_without_creating_mutation_plan(
    app: QApplication,
) -> None:
    dialog = UnifiedPortManagementDialog(username="operator", password="test-password")
    groups = (
        PoETarget(DeviceTarget("192.0.2.10", 22), tuple(str(p) for p in range(1, 201))),
        PoETarget(DeviceTarget("192.0.2.20", 22), tuple(str(p) for p in range(1, 201))),
    )
    dialog._build_results(groups)
    dialog.show()
    dialog.select_all_button.click()
    assert len(dialog._checked_port_keys) == 400
    assert "최대 300포트" in dialog.selection_summary.text()
    assert dialog._batch_plan is None
    assert not dialog.run_button.isEnabled()
    dialog.close()


def test_guided_cycle_does_not_auto_send_on_after_off(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aruba2930f_backup.poe_cycle import PowerCyclePhase

    dialog = setup_dialog()
    dialog._batch_controller = OfflineBatch()  # type: ignore[assignment]
    dialog.show()
    dialog.result_tree.setCurrentItem(dialog._items[(TARGET.endpoint, "1")])
    dialog._items[(TARGET.endpoint, "1")].setCheckState(0, Qt.CheckState.Checked)
    dialog._cycle_next()
    assert dialog._cycle is not None
    assert dialog._cycle.phase is PowerCyclePhase.OFF_READY
    assert dialog._batch_plan is not None
    assert dialog._batch_plan.action is PortAction.POE_OFF
    monkeypatch.setattr(BatchConfirmationDialog, "exec", lambda _self: QDialog.DialogCode.Accepted)
    dialog._run_batch()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and dialog._batch_thread is not None:
        app.processEvents()
        time.sleep(0.01)
    assert dialog._batch_thread is None
    assert dialog._cycle.phase is PowerCyclePhase.REQUERY_OFF
    assert dialog._cycle.on_plan is None
    assert dialog._batch_plan is None
    assert dialog.cycle_button.isEnabled()
    assert "재조회" in dialog.cycle_button.text()
    dialog.close()


def test_replace_two_column_paste_cleans_incomplete_placeholder_atomically(
    app: QApplication,
) -> None:
    dialog = UnifiedPortManagementDialog(username="operator", password="test-password")
    dialog.targets_table.item(0, 0).setText("192.0.2.99")
    assert dialog.targets_table.item(0, 2).text() == ""
    dialog.paste_targets("스위치 IP\t포트\n192.0.2.10\t12\n192.0.2.10\t24", replace=True)
    assert dialog.targets_table.rowCount() == 1
    assert dialog.targets_table.item(0, 0).text() == "192.0.2.10"
    assert dialog.targets_table.item(0, 2).text() == "12,24"
    with pytest.raises(ValueError):
        dialog.paste_targets("192.0.2.10\tall", replace=True)
    assert dialog.targets_table.item(0, 2).text() == "12,24"
    dialog.close()


def test_cycle_eligible_button_uses_ap535_and_current_poe_on_not_generic_on(
    app: QApplication,
) -> None:
    dialog = setup_dialog()
    dialog.show()
    dialog._on_result(queried("2", model="Aruba 2930F"))
    # Default generic PoE ON permits non-AP neighbors. Cycle candidacy must not.
    assert dialog.action_combo.currentData() == PortAction.POE_ON.value
    dialog.select_cycle_eligible_button.click()
    assert dialog._items[(TARGET.endpoint, "1")].checkState(0) is Qt.CheckState.Checked
    assert dialog._items[(TARGET.endpoint, "2")].checkState(0) is Qt.CheckState.Unchecked
    assert len(dialog._selected_rows()) == 1
    dialog.close()


def test_refresh_invalidates_ready_off_plan_without_stranding_wizard(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aruba2930f_backup.models import Credentials
    from aruba2930f_backup.poe_cycle import PowerCyclePhase
    from aruba2930f_backup.poe_query_gui import PoEQueryDialog

    dialog = setup_dialog()
    dialog.result_tree.setCurrentItem(dialog._items[(TARGET.endpoint, "1")])
    dialog._items[(TARGET.endpoint, "1")].setCheckState(0, Qt.CheckState.Checked)
    dialog._cycle_next()
    assert dialog._cycle is not None
    assert dialog._cycle.phase is PowerCyclePhase.OFF_READY
    monkeypatch.setattr(PoEQueryDialog, "_start", lambda *_args: None)
    dialog._start(dialog._groups, Credentials("operator", "test-password"))
    assert dialog._batch_plan is None
    assert dialog._cycle.phase is PowerCyclePhase.INTERRUPTED
    assert dialog.cycle_button.isEnabled()
    dialog.close()


def test_refresh_invalidates_on_plan_but_recovers_original_off_targets(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aruba2930f_backup.batch_port_control import create_batch_plan
    from aruba2930f_backup.models import Credentials
    from aruba2930f_backup.poe_cycle import PowerCyclePhase, PowerCycleSession
    from aruba2930f_backup.poe_query_gui import PoEQueryDialog

    dialog = setup_dialog()
    dialog.result_tree.setCurrentItem(dialog._items[(TARGET.endpoint, "1")])
    dialog._items[(TARGET.endpoint, "1")].setCheckState(0, Qt.CheckState.Checked)
    plan = create_batch_plan([queried("1")], PortAction.POE_OFF)
    session = PowerCycleSession(plan)
    session.off_verified = plan.targets
    session.on_plan = create_batch_plan([queried("1")], PortAction.POE_ON)
    session.phase = PowerCyclePhase.ON_READY
    dialog._cycle = session
    dialog._batch_plan = session.on_plan
    monkeypatch.setattr(PoEQueryDialog, "_start", lambda *_args: None)
    dialog._start(dialog._groups, Credentials("operator", "test-password"))
    assert dialog._batch_plan is None
    assert session.on_plan is None
    assert session.phase is PowerCyclePhase.REQUERY_OFF
    assert dialog.cycle_button.isEnabled()
    assert session.off_requery_groups()[0].interfaces == ("1",)
    dialog.close()


def test_row_focus_alone_never_authorizes_a_port_mutation(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *_args: QMessageBox.StandardButton.Ok,
    )
    dialog = setup_dialog()
    dialog.result_tree.setCurrentItem(dialog._items[(TARGET.endpoint, "1")])
    assert dialog._selected_rows() == ()
    dialog._prepare_batch()
    assert dialog._batch_plan is None
    assert not dialog.quick_off_button.isEnabled()
    dialog.close()


def test_child_and_parent_checkboxes_are_consistent(app: QApplication) -> None:
    dialog = setup_dialog()
    first = dialog._items[(TARGET.endpoint, "1")]
    second = dialog._items[(TARGET.endpoint, "2")]
    parent = dialog._parents[TARGET.endpoint]
    first.setCheckState(0, Qt.CheckState.Checked)
    assert parent.checkState(0) is Qt.CheckState.PartiallyChecked
    parent.setCheckState(0, Qt.CheckState.Checked)
    assert second.checkState(0) is Qt.CheckState.Checked
    assert len(dialog._checked_port_keys) == 2
    parent.setCheckState(0, Qt.CheckState.Unchecked)
    assert dialog._checked_port_keys == set()
    assert first.checkState(0) is Qt.CheckState.Unchecked
    dialog.close()


def test_checked_targets_survive_search_but_all_are_reviewed_before_execution(
    app: QApplication,
) -> None:
    dialog = setup_dialog()
    dialog._set_checked_keys({(TARGET.endpoint, "1"), (TARGET.endpoint, "2")})
    dialog.search_input.setText("AP-2")
    dialog._apply_search_filter()
    dialog._update_selection_summary()
    assert len(dialog._checked_port_keys) == 2
    assert "화면 밖 1개" in dialog.quick_summary.text()
    dialog._prepare_batch()
    assert dialog._batch_plan is not None
    assert {target.interface for target in dialog._batch_plan.targets} == {"1", "2"}
    dialog.close()


def test_rebuilding_targets_clears_checkboxes_and_prepared_plans(app: QApplication) -> None:
    dialog = setup_dialog()
    dialog._items[(TARGET.endpoint, "1")].setCheckState(0, Qt.CheckState.Checked)
    dialog._prepare_batch()
    assert dialog._batch_plan is not None
    dialog._build_results((PoETarget(TARGET, ("1", "3")),))
    assert not dialog._checked_port_keys
    assert dialog._batch_plan is None
    assert dialog._items[(TARGET.endpoint, "1")].checkState(0) is Qt.CheckState.Unchecked
    dialog.close()


def test_quick_off_starts_with_an_internal_plan_but_no_extra_plan_button(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = setup_dialog()
    dialog._items[(TARGET.endpoint, "1")].setCheckState(0, Qt.CheckState.Checked)
    dispatched: list[PortAction] = []

    def capture(*, quick: bool = False) -> None:
        assert quick
        if dialog._batch_plan is not None:
            dispatched.append(dialog._batch_plan.action)

    monkeypatch.setattr(dialog, "_run_batch", capture)
    monkeypatch.setattr(QuickPoEReviewDialog, "exec", lambda _self: QDialog.DialogCode.Accepted)
    dialog.quick_off_button.click()
    assert dispatched == [PortAction.POE_OFF]
    assert dialog._batch_plan is not None
    assert len(dialog._batch_plan.targets) == 1
    dialog.close()


def test_quick_bar_exposes_only_independent_poe_off_and_on(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = setup_dialog()
    bar_layout = dialog.quick_action_bar.layout()
    assert bar_layout is not None
    button_row_item = bar_layout.itemAt(0)
    assert button_row_item is not None
    button_row = button_row_item.layout()
    assert button_row is not None
    assert button_row.count() == 3  # selection summary + OFF + ON
    assert button_row.itemAt(1).widget() is dialog.quick_off_button
    assert button_row.itemAt(2).widget() is dialog.quick_on_button
    assert not dialog.quick_off_button.isHidden()
    assert not dialog.quick_on_button.isHidden()
    assert dialog.quick_cycle_button.isHidden()
    assert dialog.cycle_button.isHidden()
    assert dialog.cycle_status.isHidden()

    dialog._items[(TARGET.endpoint, "1")].setCheckState(0, Qt.CheckState.Checked)
    executed: list[PortAction] = []

    def capture(*, quick: bool = False) -> None:
        assert quick
        if dialog._batch_plan is not None:
            executed.append(dialog._batch_plan.action)

    monkeypatch.setattr(dialog, "_run_batch", capture)
    monkeypatch.setattr(QuickPoEReviewDialog, "exec", lambda _self: QDialog.DialogCode.Accepted)
    dialog.quick_off_button.click()
    dialog.quick_on_button.click()
    assert executed == [PortAction.POE_OFF, PortAction.POE_ON]
    dialog.close()


def test_bulk_checkbox_selection_keeps_workflow_bound_at_300_ports(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *_args: QMessageBox.StandardButton.Ok,
    )
    dialog = UnifiedPortManagementDialog(username="operator", password="test-password")
    groups = (PoETarget(TARGET, tuple(str(port) for port in range(1, 351))),)
    dialog._build_results(groups)
    dialog.select_all_button.click()
    assert len(dialog._checked_port_keys) == 350
    dialog._prepare_batch()
    assert dialog._batch_plan is None
    assert len(dialog._checked_port_keys) == 350
    dialog.close()


def test_checkbox_change_is_reverted_during_an_active_cycle(app: QApplication) -> None:
    from aruba2930f_backup.poe_cycle import PowerCyclePhase

    dialog = setup_dialog()
    first = dialog._items[(TARGET.endpoint, "1")]
    second = dialog._items[(TARGET.endpoint, "2")]
    first.setCheckState(0, Qt.CheckState.Checked)
    dialog._cycle_next()
    assert dialog._cycle is not None
    assert dialog._cycle.phase is PowerCyclePhase.OFF_READY
    second.setCheckState(0, Qt.CheckState.Checked)
    assert second.checkState(0) is Qt.CheckState.Unchecked
    assert len(dialog._checked_port_keys) == 1
    dialog.close()


def test_missing_checked_observation_blocks_plan_instead_of_dropping_port(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = setup_dialog()
    dialog._set_checked_keys({(TARGET.endpoint, "1"), (TARGET.endpoint, "2")})
    dialog._results.pop((TARGET.endpoint, "2"))
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message) or QMessageBox.StandardButton.Ok,
    )
    dialog._prepare_batch()
    assert dialog._batch_plan is None
    assert warnings and "임의로" in warnings[0]
    dialog.close()


def test_changed_target_input_clears_old_checks(app: QApplication) -> None:
    dialog = setup_dialog()
    dialog._items[(TARGET.endpoint, "1")].setCheckState(0, Qt.CheckState.Checked)
    assert dialog._checked_port_keys
    dialog.targets_table.item(0, 0).setText("192.0.2.99")
    assert not dialog._checked_port_keys
    assert (TARGET.endpoint, "1") not in dialog._items
    assert dialog._batch_plan is None
    dialog.close()


def test_quick_single_port_off_executes_without_typed_confirmation(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = setup_dialog()
    dialog._batch_controller = OfflineBatch()  # type: ignore[assignment]
    dialog._items[(TARGET.endpoint, "1")].setCheckState(0, Qt.CheckState.Checked)

    def forbid_typed_confirmation(_self: object) -> None:
        raise AssertionError("Quick action must not open EXECUTE N dialog")

    monkeypatch.setattr(BatchConfirmationDialog, "exec", forbid_typed_confirmation)
    monkeypatch.setattr(QuickPoEReviewDialog, "exec", lambda _self: QDialog.DialogCode.Accepted)
    dialog.quick_off_button.click()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and dialog._batch_thread is not None:
        app.processEvents()
        time.sleep(0.01)
    assert dialog._batch_thread is None
    assert dialog._batch_outcome is not None
    assert dialog._batch_outcome.plan.action is PortAction.POE_OFF
    assert len(dialog._batch_outcome.results) == 1
    dialog.close()


def test_quick_bulk_off_requires_one_cancellable_confirmation_without_target_review(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = setup_dialog()
    dialog._set_checked_keys({(TARGET.endpoint, "1"), (TARGET.endpoint, "2")})
    seen: list[str] = []

    def decline(_parent: object, _title: str, message: str, *_args: object) -> QMessageBox.StandardButton:
        seen.append(message)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", decline)
    monkeypatch.setattr(
        QuickPoEReviewDialog,
        "exec",
        lambda _self: (_ for _ in ()).throw(AssertionError("Target review must not appear")),
    )
    dialog.quick_off_button.click()
    assert len(seen) == 1 and "2개 포트" in seen[0]
    assert dialog._batch_thread is None
    assert dialog._batch_outcome is None
    dialog.close()

def test_quick_chunk_continues_without_extra_operator_dialog(
    app: QApplication,
) -> None:
    from aruba2930f_backup.batch_port_control import create_batch_plan
    from aruba2930f_backup.models import Credentials
    from aruba2930f_backup.unified_port_gui import _BatchWorker

    del app
    plan = create_batch_plan([queried("1")], PortAction.POE_OFF)
    worker = _BatchWorker(
        plan,
        Credentials("operator", "test-password"),
        auto_continue_chunks=True,
    )
    checkpoints: list[tuple[int, int]] = []
    worker.checkpoint.connect(lambda done, total: checkpoints.append((done, total)))
    assert worker._wait_for_checkpoint(10, 30)
    assert not checkpoints
    worker.cancel()
    assert not worker._wait_for_checkpoint(20, 30)


def test_quick_off_shows_unsafe_ap_inline_and_does_not_change_it(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = setup_dialog()
    dialog._on_result(queried("2", model="Aruba 2930F"))
    dialog._items[(TARGET.endpoint, "2")].setCheckState(0, Qt.CheckState.Checked)
    monkeypatch.setattr(
        QuickPoEReviewDialog,
        "exec",
        lambda _self: (_ for _ in ()).throw(AssertionError("No target-review popup")),
    )
    dialog.quick_off_button.click()
    assert dialog._batch_thread is None
    assert dialog._batch_plan is None
    assert "AP-535" in dialog.status_label.text()
    assert "AP-535" in dialog.quick_summary.toolTip()
    assert (TARGET.endpoint, "2") in dialog._checked_port_keys
    dialog.close()

def test_admin_disabled_filter_is_distinct_from_poe_off(
    app: QApplication,
) -> None:
    from dataclasses import replace

    dialog = setup_dialog()
    result = queried("2")
    assert result.snapshot is not None
    dialog._on_result(replace(result, snapshot=replace(result.snapshot, enabled=False)))
    dialog.filter_combo.setCurrentIndex(dialog.filter_combo.findData("port_disabled"))
    dialog._apply_search_filter()
    first = dialog._items[(TARGET.endpoint, "1")]
    second = dialog._items[(TARGET.endpoint, "2")]
    assert first.isHidden()
    assert not second.isHidden()
    assert second.text(3).startswith("포트 Disable")
    assert second.text(5) == "PoE 공급 중"
    dialog.close()


def test_switch_admin_disable_does_not_disable_poe_quick_buttons(
    app: QApplication,
) -> None:
    from dataclasses import replace

    dialog = setup_dialog()
    result = queried("1")
    assert result.snapshot is not None
    dialog._on_result(replace(result, snapshot=replace(result.snapshot, enabled=False)))
    item = dialog._items[(TARGET.endpoint, "1")]
    item.setCheckState(0, Qt.CheckState.Checked)
    assert item.text(3).startswith("포트 Disable")
    assert item.text(5) == "PoE 공급 중"
    assert dialog.quick_on_button.isEnabled()
    assert dialog.quick_off_button.isEnabled()
    dialog.close()


def test_quick_readonly_requery_requests_only_missing_checked_port(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = setup_dialog()
    dialog._set_checked_keys({(TARGET.endpoint, "1"), (TARGET.endpoint, "2")})
    dialog._results.pop((TARGET.endpoint, "2"))
    collected: list[tuple[PoETarget, ...]] = []

    def record_read(groups: tuple[PoETarget, ...], _credentials: object) -> None:
        collected.append(groups)

    monkeypatch.setattr(dialog, "_start", record_read)
    dialog.quick_off_button.click()
    assert len(collected) == 1
    assert collected[0][0].interfaces == ("2",)
    assert dialog._quick_requery_pending == (
        PortAction.POE_OFF,
        frozenset({(TARGET.endpoint, "1"), (TARGET.endpoint, "2")}),
    )
    assert dialog._batch_plan is None  # no mutation from a read request
    dialog.stop()  # user cancellation invalidates pending action
    assert dialog._quick_requery_pending is None
    dialog.close()


def test_failed_lldp_requeries_only_for_off_not_on(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = setup_dialog()
    from dataclasses import replace

    dialog._on_result(
        replace(queried("2"), lldp=LLDPObservation(LLDPStatus.FAILED, message="LLDP 오류"))
    )
    dialog._set_checked_keys({(TARGET.endpoint, "2")})
    requested: list[tuple[PoETarget, ...]] = []
    ready: list[PortAction] = []
    monkeypatch.setattr(dialog, "_start", lambda groups, _auth: requested.append(groups))

    def capture(*, quick: bool = False) -> None:
        assert quick
        assert dialog._batch_plan is not None
        ready.append(dialog._batch_plan.action)

    monkeypatch.setattr(dialog, "_run_batch", capture)
    dialog.quick_off_button.click()
    assert requested and requested[0][0].interfaces == ("2",)
    dialog.stop()
    requested.clear()
    dialog.quick_on_button.click()
    assert requested == []  # ON requires port state, but not AP LLDP identity
    assert ready == [PortAction.POE_ON]
    dialog.close()

def test_partial_review_cannot_execute_until_exclusion_is_acknowledged(
    app: QApplication,
) -> None:
    dialog = QuickPoEReviewDialog(
        PortAction.POE_OFF,
        [queried("1")],
        [(TARGET.endpoint, "2", "SSH 연결 실패")],
    )
    assert dialog.table.topLevelItemCount() == 2
    assert "제외 1포트" in dialog.summary.text()
    assert not dialog.approve_button.isEnabled()
    dialog._approve()
    assert dialog.result() != QDialog.DialogCode.Accepted
    dialog.exclusion_ack.setChecked(True)
    assert dialog.approve_button.isEnabled()
    dialog._approve()
    assert dialog.result() == QDialog.DialogCode.Accepted
    dialog.close()


def test_no_valid_targets_cannot_be_confirmed(
    app: QApplication,
) -> None:
    dialog = QuickPoEReviewDialog(
        PortAction.POE_ON,
        [],
        [(TARGET.endpoint, "2", "포트 상태 없음")],
    )
    dialog.exclusion_ack.setChecked(True)
    assert not dialog.approve_button.isEnabled()
    dialog._approve()
    assert dialog.result() != QDialog.DialogCode.Accepted
    dialog.close()


def test_requery_failure_blocks_entire_quick_action_with_inline_port_reason(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    class OfflineReadOnlyService:
        def collect(
            self,
            groups: tuple[PoETarget, ...],
            _credentials: object,
            *,
            approve_host_key: object,
            on_result: object = None,
            cancel_event: object = None,
        ) -> tuple[PoEQueryResult, ...]:
            del approve_host_key, cancel_event
            assert len(groups) == 1
            assert groups[0].interfaces == ("2",)
            row = PoEQueryResult(
                groups[0],
                "2",
                QueryStatus.FAILED,
                message="연결 오류 (가상)",
                complete=True,
            )
            if callable(on_result):
                on_result(row)
            return (row,)

    dialog = UnifiedPortManagementDialog(
        username="operator", password="test-password", service=OfflineReadOnlyService()
    )
    dialog._build_results((PoETarget(TARGET, ("1", "2")),))
    dialog._on_result(queried("1"))
    dialog._set_checked_keys({(TARGET.endpoint, "1"), (TARGET.endpoint, "2")})
    executed: list[PortAction] = []

    def capture(*, quick: bool = False) -> None:
        assert quick
        assert dialog._batch_plan is not None
        executed.append(dialog._batch_plan.action)

    monkeypatch.setattr(
        QuickPoEReviewDialog,
        "exec",
        lambda _self: (_ for _ in ()).throw(AssertionError("No target-review popup")),
    )
    monkeypatch.setattr(dialog, "_run_batch", capture)
    dialog.quick_off_button.click()
    assert dialog._batch_plan is None
    assert dialog._thread is not None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and dialog._thread is not None:
        app.processEvents()
        time.sleep(0.01)
    assert dialog._thread is None
    assert executed == []
    assert dialog._batch_plan is None
    assert len(dialog._checked_port_keys) == 2
    assert "192.0.2.10:22" in dialog.status_label.text()
    assert "2" in dialog.status_label.text()
    assert "연결 오류" in dialog.quick_summary.toolTip()
    dialog.close()

def test_bulk_off_uses_only_one_safety_confirmation_without_review(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = setup_dialog()
    dialog._batch_controller = OfflineBatch()  # type: ignore[assignment]
    dialog._set_checked_keys({(TARGET.endpoint, "1"), (TARGET.endpoint, "2")})
    approvals: list[str] = []

    def accept(_parent: object, _title: str, message: str, *_args: object) -> QMessageBox.StandardButton:
        approvals.append(message)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", accept)
    monkeypatch.setattr(
        QuickPoEReviewDialog,
        "exec",
        lambda _self: (_ for _ in ()).throw(AssertionError("No duplicate review")),
    )
    dialog.quick_off_button.click()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and dialog._batch_thread is not None:
        app.processEvents()
        time.sleep(0.01)
    assert dialog._batch_thread is None
    assert dialog._batch_outcome is not None
    assert len(dialog._batch_outcome.results) == 2
    assert len(approvals) == 1
    dialog.close()

def test_large_quick_review_requires_explicit_bulk_ack_without_repeated_prompts(
    app: QApplication,
) -> None:
    ready = [queried(str(port)) for port in range(1, 12)]
    dialog = QuickPoEReviewDialog(PortAction.POE_OFF, ready, [])
    try:
        assert dialog.large_action_ack.isVisible() is False or dialog.large_action_ack.isEnabled()
        assert dialog.approve_button.isEnabled() is False
        dialog.large_action_ack.setChecked(True)
        assert dialog.approve_button.isEnabled() is True
    finally:
        dialog.close()


def test_small_quick_review_keeps_existing_single_approval(app: QApplication) -> None:
    dialog = QuickPoEReviewDialog(PortAction.POE_OFF, [queried("1")], [])
    try:
        assert dialog.approve_button.isEnabled() is True
    finally:
        dialog.close()


def test_quick_poe_off_does_not_open_target_review_dialog(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = setup_dialog()
    dialog._set_checked_keys({(TARGET.endpoint, "1"), (TARGET.endpoint, "2")})
    dispatched: list[tuple[PortAction, int]] = []

    def forbid_review(_self: QuickPoEReviewDialog) -> None:
        raise AssertionError("Quick PoE controls must not open a target review window")

    def capture_execution(*, quick: bool = False) -> None:
        assert quick
        assert dialog._batch_plan is not None
        dispatched.append((dialog._batch_plan.action, len(dialog._batch_plan.targets)))

    monkeypatch.setattr(QuickPoEReviewDialog, "exec", forbid_review)
    monkeypatch.setattr(dialog, "_run_batch", capture_execution)
    dialog.quick_off_button.click()
    assert dispatched == [(PortAction.POE_OFF, 2)]
    dialog.close()


def test_quick_invalid_checked_port_does_not_silently_drop_or_open_review(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = setup_dialog()
    dialog._set_checked_keys({(TARGET.endpoint, "1"), (TARGET.endpoint, "2")})
    from dataclasses import replace

    dialog._on_result(
        replace(queried("2"), snapshot=None, status=QueryStatus.FAILED, message="SSH timeout")
    )
    triggered: list[tuple[PoETarget, ...]] = []
    monkeypatch.setattr(dialog, "_start", lambda groups, _auth: triggered.append(groups))
    monkeypatch.setattr(
        QuickPoEReviewDialog,
        "exec",
        lambda _self: (_ for _ in ()).throw(AssertionError("no target review dialog")),
    )
    dialog.quick_off_button.click()
    assert triggered and triggered[0][0].interfaces == ("2",)
    # The single read-only retry was started. It must never mutate by itself.
    assert dialog._batch_plan is None
    assert len(dialog._checked_port_keys) == 2
    dialog.stop()
    dialog.close()
