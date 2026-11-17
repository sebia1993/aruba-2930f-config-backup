"""Exercise the unified Qt controls without contacting a real switch."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from aruba2930f_backup.batch_port_control import BatchOutcome, BatchResult, BatchStatus
from aruba2930f_backup.models import DeviceTarget
from aruba2930f_backup.poe_power import LLDPNeighbor, LLDPObservation, LLDPStatus
from aruba2930f_backup.poe_query_models import PoEQueryResult, PoETarget, QueryStatus
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
    lldp = LLDPObservation(LLDPStatus.OK, (LLDPNeighbor(system_name=f"AP-{port}", model=model),))
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
    assert dialog.result_tree.selectionMode() == dialog.result_tree.SelectionMode.ExtendedSelection
    first = dialog._items[(TARGET.endpoint, "1")]
    second = dialog._items[(TARGET.endpoint, "2")]
    dialog.result_tree.setCurrentItem(first)
    first.setSelected(True)
    second.setSelected(True)
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
    second.setSelected(True)
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


def test_filter_hides_and_deselects_ports_and_invalidates_batch_plan(app: QApplication) -> None:
    dialog = setup_dialog()
    first = dialog._items[(TARGET.endpoint, "1")]
    second = dialog._items[(TARGET.endpoint, "2")]
    dialog.result_tree.setCurrentItem(first)
    first.setSelected(True)
    second.setSelected(True)
    dialog._prepare_batch()
    assert dialog._batch_plan is not None
    assert len(dialog._batch_plan.targets) == 2
    dialog.search_input.setText("AP-2")
    assert dialog._batch_plan is None
    dialog._apply_search_filter()
    assert first.isHidden()
    assert not first.isSelected()
    assert not second.isHidden()
    assert tuple(row.interface for row in dialog._selected_rows()) == ("2",)
    assert not dialog.run_button.isEnabled()
    dialog.close()


def test_change_action_after_plan_forces_new_explicit_approval(app: QApplication) -> None:
    dialog = setup_dialog()
    dialog.result_tree.setCurrentItem(dialog._items[(TARGET.endpoint, "1")])
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
    first.setSelected(True)
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
    assert first.isSelected() and second.isSelected()
    assert "선택 2개" in dialog.selection_summary.text()
    assert "가능 1개" in dialog.selection_summary.text()
    dialog.select_eligible_button.click()
    assert first.isSelected() and not second.isSelected()
    assert len(dialog._selected_rows()) == 1
    dialog.clear_selection_button.click()
    assert not first.isSelected() and not second.isSelected()
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
    assert first.isSelected() and second.isSelected()
    assert not dialog._parents[TARGET.endpoint].isSelected()
    dialog.search_input.setText("AP-2")
    dialog._apply_search_filter()
    dialog.selection_scope.setCurrentIndex(dialog.selection_scope.findData("filtered"))
    dialog.select_all_button.click()
    assert not first.isSelected() and second.isSelected()
    dialog.selection_scope.setCurrentIndex(dialog.selection_scope.findData("all"))
    dialog.select_all_button.click()
    assert dialog.search_input.text() == ""
    assert first.isSelected() and second.isSelected()
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
    assert len(dialog.result_tree.selectedItems()) == 400
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
    assert dialog._items[(TARGET.endpoint, "1")].isSelected()
    assert not dialog._items[(TARGET.endpoint, "2")].isSelected()
    assert len(dialog._selected_rows()) == 1
    dialog.close()
