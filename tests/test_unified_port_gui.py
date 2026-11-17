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
    dialog = UnifiedPortManagementDialog(username="operator", password="fixture")
    groups = tuple(
        PoETarget(
            DeviceTarget(
                f"{'192.0.2' if i < 250 else '198.51.100'}.{i % 250 + 1}", 22
            ),
            tuple(str(number) for number in range(1, 49)),
        )
        for i in range(300)
    )
    dialog._build_results(groups)
    assert len(dialog._groups) == 300
    assert len(dialog._parents) == 300
    assert len(dialog._items) == 14_400
    dialog.close()
