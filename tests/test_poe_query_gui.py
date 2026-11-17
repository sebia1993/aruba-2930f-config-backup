from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from aruba2930f_backup.models import Credentials, HostKeyCheck
from aruba2930f_backup.poe_query_gui import PoEQueryDialog
from aruba2930f_backup.poe_query_models import PoEQueryResult, PoETarget, QueryStatus
from aruba2930f_backup.port_control import PortSnapshot


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[PoETarget, ...]] = []
        self.release: threading.Event | None = None
        self.request_approval = False
        self.approved: bool | None = None

    def collect(
        self,
        groups: Sequence[PoETarget],
        credentials: Credentials,
        *,
        approve_host_key: Callable[[HostKeyCheck], bool],
        on_result: Callable[[PoEQueryResult], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[PoEQueryResult, ...]:
        del credentials
        self.calls.append(tuple(groups))
        if self.request_approval:
            self.approved = approve_host_key(object())  # type: ignore[arg-type]
        if self.release is not None:
            assert self.release.wait(5)
        results = []
        for group in groups:
            for interface in group.interfaces:
                cancelled = cancel_event is not None and cancel_event.is_set()
                snapshot = PortSnapshot(
                    group.target,
                    interface,
                    "lab-sw",
                    "Aruba 2930F",
                    True,
                    "Up",
                    True,
                    "Delivering",
                    0.0 if interface == "1" else None,
                    observed_at="2026-11-16T10:00:00+00:00",
                )
                from aruba2930f_backup.poe_power import LLDPNeighbor, LLDPObservation, LLDPStatus

                result = PoEQueryResult(
                    group,
                    interface,
                    QueryStatus.CANCELLED if cancelled else QueryStatus.OK,
                    None if cancelled else snapshot,
                    finished_at="2026-11-16T10:00:01+00:00",
                    lldp=LLDPObservation(
                        LLDPStatus.OK, (LLDPNeighbor(system_name="AP-" + interface),)
                    ),
                )
                results.append(result)
                if on_result:
                    thread = threading.Thread(target=on_result, args=(result,))
                    thread.start()
                    thread.join()
        return tuple(results)


def setup_dialog(qtbot: object) -> tuple[PoEQueryDialog, FakeService]:
    service = FakeService()
    dialog = PoEQueryDialog(service=service, username="operator", password="test-password")
    qtbot.addWidget(dialog)
    dialog._replace_targets_rows(
        (
            ("192.0.2.1", "22", "1,2", "A"),
            ("192.0.2.2", "2222", "3", "B"),
            ("192.0.2.1", "22", "2,4", "A"),
        )
    )
    return dialog, service


def test_target_pairing_merge_group_results_and_edit_invalidation(
    app: QApplication, qtbot: object
) -> None:
    dialog, service = setup_dialog(qtbot)
    dialog.collect_button.click()
    qtbot.waitUntil(lambda: not dialog.is_busy)
    assert [g.interfaces for g in service.calls[0]] == [("1", "2", "4"), ("3",)]
    assert service.calls[0][1].target.port == 2222
    assert dialog.result_tree.topLevelItemCount() == 2
    group = dialog.result_tree.topLevelItem(0)
    assert "lab-sw" in group.text(0)
    assert "3/3" in group.text(5)
    assert group.child(0).text(2) == "0 W"
    assert group.child(1).text(2) == "확인 안 됨"
    assert group.child(0).text(4)
    dialog.targets_table.item(0, 2).setText("5")
    assert dialog.result_tree.topLevelItemCount() == 0
    assert not dialog.export_button.isEnabled()


@pytest.mark.parametrize("scope", ["all", "group", "port"])
def test_refresh_clears_only_selected_stale_values(
    app: QApplication, qtbot: object, scope: str
) -> None:
    dialog, service = setup_dialog(qtbot)
    dialog.collect_button.click()
    qtbot.waitUntil(lambda: not dialog.is_busy)
    first = dialog.result_tree.topLevelItem(0)
    dialog.result_tree.setCurrentItem(first.child(0) if scope == "port" else first)
    assert first.child(0).text(1) == "AP-1"
    service.release = threading.Event()
    button = {
        "all": dialog.collect_button,
        "group": dialog.refresh_group_button,
        "port": dialog.refresh_port_button,
    }[scope]
    button.click()
    try:
        assert first.child(0).text(1) == ""
        assert first.child(0).toolTip(3) == ""
        assert first.child(0).toolTip(5) == ""
        assert first.child(0).text(2) == ""
        assert ("192.0.2.1:22", "1") not in dialog._results
        assert first.child(0).text(5) == "대기"
        if scope == "port":
            assert first.child(1).text(1) == "AP-2"
            assert first.child(1).text(2) == "확인 안 됨"
        if scope != "all":
            assert dialog.result_tree.topLevelItem(1).child(0).text(1) == "AP-3"
            assert dialog.result_tree.topLevelItem(1).child(0).text(2) == "확인 안 됨"
        dialog.reject()
        assert dialog.is_busy
    finally:
        service.release.set()
    qtbot.waitUntil(lambda: not dialog.is_busy)
    expected = 4 if scope == "all" else 3 if scope == "group" else 1
    assert sum(len(g.interfaces) for g in service.calls[-1]) == expected


def test_invalid_rows_do_not_collect(
    app: QApplication, qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog, service = setup_dialog(qtbot)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: QMessageBox.StandardButton.Ok)
    dialog.targets_table.item(0, 2).setText("1-24")
    dialog.collect_button.click()
    assert service.calls == []
    assert not dialog.is_busy


def test_cancel_releases_host_key_approval_and_defers_close(
    app: QApplication, qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aruba2930f_backup import gui

    dialog, service = setup_dialog(qtbot)
    service.request_approval = True

    class Approval(QDialog):
        approval_allowed = True

        def __init__(self, checks: object, parent: object) -> None:
            super().__init__(parent)
            QTimer.singleShot(10, dialog.reject)

    monkeypatch.setattr(gui, "HostKeyApprovalDialog", Approval)
    dialog.show()
    dialog.collect_button.click()
    qtbot.waitUntil(lambda: not dialog.is_busy)
    assert service.approved is False
    qtbot.waitUntil(lambda: not dialog.isVisible())


def test_profile_load_paste_are_atomic_and_never_connect(
    app: QApplication, qtbot: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog, service = setup_dialog(qtbot)
    from aruba2930f_backup.poe_targets import load_target_profile

    target_path = tmp_path / "targets.json"
    dialog.save_profile(target_path)
    assert "test-password" not in target_path.read_text()
    assert "operator" not in target_path.read_text()
    assert len(load_target_profile(target_path)) == 2
    before = dialog.targets_table.item(0, 0).text()
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.No)
    assert not dialog.load_profile(target_path)
    assert dialog.targets_table.item(0, 0).text() == before
    with pytest.raises(ValueError):
        dialog.paste_targets("192.0.2.9\t22\t1\ninvalid\t22\t2")
    assert dialog.targets_table.item(0, 0).text() == before
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    assert dialog.load_profile(target_path)
    assert service.calls == []


def test_backup_disables_query_and_modal_query_blocks_other_workflows(
    app: QApplication, qtbot: object
) -> None:
    from aruba2930f_backup.gui import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    assert window.poe_query_button.isEnabled()
    window._set_running(True)
    assert not window.poe_query_button.isEnabled()
    window._set_running(False)
    dialog, _service = setup_dialog(qtbot)
    window._poe_query_dialog = dialog
    window._launch_backup()
    window._open_port_control()
    assert window._thread is None
    assert window._port_control_dialog is None
    window._poe_query_dialog = None


def test_failure_rows_and_same_ip_different_ssh_port_stay_separate(
    app: QApplication, qtbot: object, tmp_path: Path
) -> None:
    from aruba2930f_backup.models import ErrorCode

    dialog, _service = setup_dialog(qtbot)
    dialog._replace_targets_rows((("192.0.2.1", "22", "1", "A"), ("192.0.2.1", "2222", "2", "B")))
    groups = dialog._validated_targets()
    dialog._build_results(groups)
    result = PoEQueryResult(
        groups[1],
        "2",
        QueryStatus.FAILED,
        error_code=ErrorCode.AUTH_FAILED,
        message="인증 실패",
        finished_at="2026-11-16T10:00:00+00:00",
    )
    dialog._on_result(result)
    assert dialog.result_tree.topLevelItemCount() == 2
    failed = dialog.result_tree.topLevelItem(1).child(0)
    assert failed.text(2) == "확인 안 됨"
    assert failed.text(5) == "실패"
    dialog.result_tree.setCurrentItem(failed)
    assert "인증 실패" in dialog.details_label.text()
    path = tmp_path / "results.csv"
    dialog.export_results(path)
    data = path.read_text(encoding="utf-8-sig")
    assert "test-password" not in data
    assert "operator" not in data
    assert "2222" in data


def test_stop_keeps_dialog_open_and_blocks_duplicate_collect(
    app: QApplication, qtbot: object
) -> None:
    dialog, service = setup_dialog(qtbot)
    service.release = threading.Event()
    dialog.show()
    dialog.collect_button.click()
    try:
        qtbot.waitUntil(lambda: len(service.calls) == 1)
        dialog._collect_all()
        dialog.stop_button.click()
        assert dialog.is_busy
        assert not dialog.stop_button.isEnabled()
        assert not dialog.targets_table.isEnabled()
        assert dialog.isVisible()
    finally:
        service.release.set()
    qtbot.waitUntil(lambda: not dialog.is_busy)
    assert len(service.calls) == 1
    assert dialog.isVisible()
    assert "취소" in dialog.result_tree.topLevelItem(0).child(0).text(5)


def test_parent_close_waits_for_query_thread(app: QApplication, qtbot: object) -> None:
    from aruba2930f_backup.gui import MainWindow

    parent = MainWindow()
    qtbot.addWidget(parent)
    parent.show()
    service = FakeService()
    service.release = threading.Event()
    dialog = PoEQueryDialog(
        parent,
        service=service,
        initial_ips=("192.0.2.1",),
        username="operator",
        password="test-password",
    )
    dialog.targets_table.item(0, 2).setText("1")
    parent._poe_query_dialog = dialog
    dialog.show()
    dialog.collect_button.click()
    try:
        assert not parent.close()
        assert parent.isVisible()
        assert dialog.is_busy
        assert dialog.isVisible()
    finally:
        service.release.set()
    qtbot.waitUntil(lambda: not dialog.is_busy)
    qtbot.waitUntil(lambda: not parent.isVisible())
    parent._poe_query_dialog = None


def test_add_remove_and_paste_valid_targets_preserve_pairing(
    app: QApplication, qtbot: object
) -> None:
    dialog = PoEQueryDialog(service=FakeService())
    qtbot.addWidget(dialog)
    dialog.add_button.click()
    assert dialog.targets_table.rowCount() == 2
    dialog.targets_table.selectRow(1)
    dialog.remove_button.click()
    assert dialog.targets_table.rowCount() == 1
    dialog.paste_targets("192.0.2.10\t22\t1,2\tA\n192.0.2.11\t2222\t24\tB")
    assert [g.interfaces for g in dialog._validated_targets()] == [("1", "2"), ("24",)]


def test_target_changes_clear_result_details_and_pending_metadata(
    app: QApplication, qtbot: object
) -> None:
    dialog, _service = setup_dialog(qtbot)
    dialog.collect_button.click()
    qtbot.waitUntil(lambda: not dialog.is_busy)
    dialog.targets_table.item(0, 0).setText("192.0.2.9")
    assert dialog._pending == set()
    assert dialog._run_total == 0


def test_parallel_approval_requests_are_queued_and_cancelled_together(
    app: QApplication, qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aruba2930f_backup import gui

    dialog, service = setup_dialog(qtbot)
    opened: list[QDialog] = []
    results: list[bool] = []
    checks_started = threading.Barrier(2)

    def parallel_collect(
        groups: object, credentials: object, *, approve_host_key: object, **kwargs: object
    ) -> tuple[()]:
        del groups, credentials, kwargs

        def ask() -> None:
            checks_started.wait(timeout=2)
            results.append(approve_host_key(object()))

        threads = [threading.Thread(target=ask) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        return ()

    class Approval(QDialog):
        approval_allowed = True

        def __init__(self, checks: object, parent: object) -> None:
            super().__init__(parent)
            opened.append(self)
            QTimer.singleShot(30, dialog.stop)

    monkeypatch.setattr(service, "collect", parallel_collect)
    monkeypatch.setattr(gui, "HostKeyApprovalDialog", Approval)
    watchdog = QTimer(dialog)
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: [approval.reject() for approval in opened])
    watchdog.start(150)
    dialog.collect_button.click()
    qtbot.waitUntil(lambda: not dialog.is_busy)
    watchdog.stop()
    assert results == [False, False]
    assert len(opened) == 1


def test_busy_state_waits_for_native_thread_cleanup_before_close(
    app: QApplication, qtbot: object
) -> None:
    from PySide6.QtCore import Qt

    dialog, service = setup_dialog(qtbot)
    service.release = threading.Event()
    cleanup_entered = threading.Event()
    cleanup_release = threading.Event()
    dialog.show()
    dialog.collect_button.click()
    thread = dialog._thread
    worker = dialog._worker
    assert thread is not None and worker is not None

    def delayed_native_cleanup() -> None:
        cleanup_entered.set()
        assert cleanup_release.wait(3)

    # Qt emits finished before all native cleanup completes. Hold that phase open
    # after the GUI has received finished, like a slow thread-local destructor.
    thread.finished.connect(delayed_native_cleanup, Qt.ConnectionType.DirectConnection)
    service.release.set()
    try:
        qtbot.waitUntil(cleanup_entered.is_set)
        qtbot.wait(130)
        assert dialog.is_busy
        assert not dialog.close()
        assert dialog.isVisible()
    finally:
        cleanup_release.set()
    qtbot.waitUntil(lambda: not dialog.is_busy)
    qtbot.waitUntil(lambda: not dialog.isVisible())


def test_basic_target_columns_keep_advanced_metadata_and_endpoints(
    app: QApplication, qtbot: object, tmp_path: Path
) -> None:
    dialog = PoEQueryDialog(service=FakeService())
    qtbot.addWidget(dialog)
    assert dialog.targets_table.isColumnHidden(1)
    assert dialog.targets_table.isColumnHidden(3)
    assert not dialog.targets_table.isColumnHidden(0)
    assert not dialog.targets_table.isColumnHidden(2)
    dialog.paste_targets("IP\tinterfaces\n192.0.2.1\t1,2")
    assert dialog._validated_targets()[0].target.port == 22
    dialog.paste_targets("192.0.2.1\t2222\t24\t별도")
    assert dialog.advanced_toggle.isChecked()
    assert not dialog.targets_table.isColumnHidden(1)
    assert [g.target.port for g in dialog._validated_targets()] == [22, 2222]
    dialog.advanced_toggle.setChecked(False)
    dialog.targets_table.item(1, 2).setText("24,1/24")
    path = tmp_path / "advanced.json"
    dialog.save_profile(path)
    from aruba2930f_backup.poe_targets import load_target_profile

    stored = load_target_profile(path)
    assert stored[1].target.port == 2222
    assert stored[1].label == "별도"
    assert stored[1].interfaces == ("24", "1/24")
    before = dialog._validated_targets()
    with pytest.raises(ValueError):
        dialog.paste_targets("192.0.2.9\t1\ninvalid\t2")
    assert dialog._validated_targets() == before


@pytest.mark.parametrize("label", ["bad\ud800name", "bad\udfffname"])
def test_invalid_unicode_import_keeps_existing_targets_and_results(
    app: QApplication, qtbot: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    import json

    dialog, service = setup_dialog(qtbot)
    dialog.collect_button.click()
    qtbot.waitUntil(lambda: not dialog.is_busy)
    before = dialog._validated_targets()
    results = dict(dialog._results)
    first_item = dialog.result_tree.topLevelItem(0)
    questions = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: questions.append(args) or QMessageBox.StandardButton.Yes,
    )
    path = tmp_path / "invalid-unicode.json"
    path.write_text(
        json.dumps(
            {
                "format": "aruba2930f-poe-targets",
                "version": 1,
                "targets": [
                    {"ip": "192.0.2.9", "ssh_port": 22, "interfaces": ["9"], "label": label}
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        dialog.load_profile(path)
    with pytest.raises(ValueError):
        dialog.paste_targets(f"192.0.2.9\t22\t9\t{label}")
    assert dialog._validated_targets() == before
    assert dialog._results == results
    assert dialog.result_tree.topLevelItem(0) is first_item
    assert dialog.export_button.isEnabled()
    assert questions == []
    assert len(service.calls) == 1


def test_inherited_nondefault_port_reveals_advanced_inputs(
    app: QApplication, qtbot: object
) -> None:
    dialog = PoEQueryDialog(initial_ips=("192.0.2.1",), ssh_port=2222)
    qtbot.addWidget(dialog)
    assert not dialog.targets_table.isColumnHidden(1)
    assert dialog.advanced_toggle.isChecked()


def phase_result(group: PoETarget, *, complete: bool = True) -> PoEQueryResult:
    from decimal import Decimal

    from aruba2930f_backup.models import DeviceIdentity
    from aruba2930f_backup.poe_power import (
        AssessmentStatus,
        LLDPNeighbor,
        LLDPObservation,
        LLDPStatus,
        PowerAssessment,
        PowerEvidence,
    )

    return PoEQueryResult(
        group,
        group.interfaces[0],
        QueryStatus.OK,
        PortSnapshot(
            group.target,
            group.interfaces[0],
            "real-sw",
            "Aruba 2930F",
            True,
            "Up",
            True,
            "Delivering",
            8.7,
            observed_at="2026-11-16T10:00:00+00:00",
        ),
        identity=DeviceIdentity("real-sw", "Aruba 2930F", None),
        power_evidence=PowerEvidence(
            requested_watts=Decimal("25.5"), allocated_watts=Decimal("25.5"), plc_class=4
        ),
        lldp=LLDPObservation(
            LLDPStatus.OK if complete else LLDPStatus.PENDING,
            (
                LLDPNeighbor(
                    system_name="<b>AP-east</b>",
                    model="AP-535",
                    system_description="Aruba AP-535",
                    chassis_id="aa:bb",
                    port_id="eth0",
                    port_description="uplink",
                    management_addresses=("192.0.2.44",),
                ),
            )
            if complete
            else (),
            observed_at="2026-11-16T10:00:02+00:00" if complete else "",
        ),
        assessment=PowerAssessment(
            AssessmentStatus.NORMAL if complete else AssessmentStatus.UNKNOWN,
            "공급·협상 정상" if complete else "확인 필요",
            "유효 요청·할당 충족",
            model_source="lldp" if complete else "configured",
            requested_watts=Decimal("25.5"),
            allocated_watts=Decimal("25.5"),
            negotiation="802.3at Class 4",
        ),
        poe_finished_at="2026-11-16T10:00:01+00:00",
        finished_at="2026-11-16T10:00:03+00:00" if complete else "",
        complete=complete,
    )


def test_lldp_columns_details_and_incremental_completion_are_idempotent(
    app: QApplication, qtbot: object
) -> None:
    from PySide6.QtCore import Qt

    dialog, _service = setup_dialog(qtbot)
    groups = dialog._validated_targets()
    dialog._build_results(groups)
    key = (groups[0].target.endpoint, "1")
    dialog._pending = {key}
    dialog._run_total = 1
    partial = phase_result(groups[0], complete=False)
    dialog._on_result(partial)
    dialog._on_result(partial)
    item = dialog._items[key]
    assert key in dialog._pending
    assert dialog.progress_bar.value() == 0
    assert item.text(1) == "조회 중"
    assert item.text(2) == "8.7 W"
    assert "LLDP" in item.text(5)
    final = phase_result(groups[0])
    dialog._on_result(final)
    dialog._on_result(final)
    dialog._on_result(partial)  # A delayed intermediate callback cannot undo completion.
    assert dialog.progress_bar.value() == 100
    assert not dialog._pending
    assert item.text(1) == "<b>AP-east</b>"
    assert item.text(3) == "공급·협상 정상"
    assert "충족" in item.toolTip(3)
    assert item.text(4)
    dialog.result_tree.setCurrentItem(item)
    details = dialog.details_label.text()
    for fact in (
        "Up",
        "Delivering",
        "lldp",
        "25.5",
        "Class 4",
        "AP-535",
        "aa:bb",
        "eth0",
        "uplink",
        "192.0.2.44",
        "HPE-530-QuickSpecs-V22-2026-08-17",
    ):
        assert fact in details
    from aruba2930f_backup.poe_query_gui import _local_time

    assert _local_time(final.poe_finished_at) in details
    assert _local_time(final.lldp.observed_at) in details
    assert dialog.details_label.textFormat() == Qt.TextFormat.PlainText
    parent = item.parent()
    assert parent.text(0) == "192.0.2.1 · real-sw"
    assert "A" in parent.toolTip(0)
    assert all(" W" not in parent.text(column) for column in range(6))
    assert [dialog.result_tree.headerItem().text(i) for i in range(6)] == [
        "스위치 / 포트",
        "AP 이름 (LLDP)",
        "현재 전력",
        "PoE 판정",
        "조회 시각 (로컬)",
        "조회 상태",
    ]


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("empty", "없음"),
        ("unnamed", "없음"),
        ("failed", "조회 실패"),
        ("cancelled", "조회 취소"),
        ("multiple", "AP-one / AP-two"),
    ],
)
def test_lldp_empty_and_error_remain_distinct(
    app: QApplication, qtbot: object, kind: str, expected: str
) -> None:
    from dataclasses import replace

    from aruba2930f_backup.poe_power import LLDPNeighbor, LLDPObservation, LLDPStatus

    dialog, _service = setup_dialog(qtbot)
    groups = dialog._validated_targets()
    dialog._build_results(groups)
    status = (
        LLDPStatus.FAILED
        if kind == "failed"
        else (LLDPStatus.CANCELLED if kind == "cancelled" else LLDPStatus.OK)
    )
    neighbors = (
        (LLDPNeighbor(),)
        if kind == "unnamed"
        else (
            (LLDPNeighbor(system_name="AP-one"), LLDPNeighbor(system_name="AP-two"))
            if kind == "multiple"
            else ()
        )
    )
    result = replace(
        phase_result(groups[0]), lldp=LLDPObservation(status, neighbors, message="LLDP detail")
    )
    dialog._on_result(result)
    item = dialog._items[(groups[0].target.endpoint, "1")]
    assert item.text(1) == expected
    dialog.result_tree.setCurrentItem(item)
    assert "LLDP detail" in dialog.details_label.text()


def test_hostname_survives_all_port_failures(app: QApplication, qtbot: object) -> None:
    from aruba2930f_backup.models import DeviceIdentity

    dialog, _service = setup_dialog(qtbot)
    groups = dialog._validated_targets()
    dialog._build_results(groups)
    for interface in groups[0].interfaces:
        dialog._on_result(
            PoEQueryResult(
                groups[0],
                interface,
                QueryStatus.FAILED,
                message="port failed",
                identity=DeviceIdentity("real-sw", "Aruba 2930F", None),
            )
        )
    assert dialog._parents[groups[0].target.endpoint].text(0) == "192.0.2.1 · real-sw"


def test_worker_fallback_preserves_partial_poe_and_identity(
    app: QApplication, qtbot: object
) -> None:
    from aruba2930f_backup.poe_power import LLDPStatus

    dialog, _service = setup_dialog(qtbot)
    groups = dialog._validated_targets()
    dialog._build_results(groups)
    partial = phase_result(groups[0], complete=False)
    key = (groups[0].target.endpoint, "1")
    dialog._pending = {key}
    dialog._run_total = 1
    dialog._on_result(partial)
    dialog._thread_finished()
    result = dialog._results[key]
    assert result.complete
    assert result.status == QueryStatus.OK
    assert result.snapshot == partial.snapshot
    assert result.identity == partial.identity
    assert result.power_evidence == partial.power_evidence
    assert result.poe_finished_at == partial.poe_finished_at
    assert result.lldp.status == LLDPStatus.FAILED
    assert result.finished_at
    assert dialog._items[key].text(2) == "8.7 W"
    assert dialog.progress_bar.value() == 100


def test_long_details_remain_scrollable_without_crowding_results(
    app: QApplication, qtbot: object
) -> None:
    from PySide6.QtWidgets import QScrollArea

    dialog, _service = setup_dialog(qtbot)
    groups = dialog._validated_targets()
    dialog._build_results(groups)
    dialog._on_result(phase_result(groups[0]))
    dialog.result_tree.setCurrentItem(dialog._items[(groups[0].target.endpoint, "1")])
    scroll = dialog.findChild(QScrollArea, "poeDetailsScroll")
    assert scroll is not None
    dialog.show()
    qtbot.wait(10)
    assert scroll.height() <= 180
    assert dialog.result_tree.height() >= 160
    assert scroll.verticalScrollBar().maximum() > 0


def test_device_text_is_inert_in_group_and_status_tooltips(
    app: QApplication, qtbot: object
) -> None:
    from dataclasses import replace

    from aruba2930f_backup.models import DeviceIdentity

    dialog, _service = setup_dialog(qtbot)
    groups = dialog._validated_targets()
    dialog._build_results(groups)
    result = replace(
        phase_result(groups[0]),
        identity=DeviceIdentity("<b>sw</b>", "Aruba 2930F", None),
        message='<img src="x">',
    )
    dialog._on_result(result)
    item = dialog._items[(groups[0].target.endpoint, "1")]
    assert "<b>sw</b>" in item.parent().text(0)
    assert "<b>sw</b>" not in item.parent().toolTip(0)
    assert "&lt;b&gt;sw&lt;/b&gt;" in item.parent().toolTip(0)
    assert "<img" not in item.toolTip(5)
    assert "&lt;img" in item.toolTip(5)


def test_cancelled_worker_retains_poe_phase_until_native_cleanup(
    app: QApplication, qtbot: object
) -> None:
    from aruba2930f_backup.poe_power import LLDPStatus

    dialog, service = setup_dialog(qtbot)
    release = threading.Event()

    def collect_partial(
        groups: Sequence[PoETarget],
        credentials: Credentials,
        *,
        on_result: Callable[[PoEQueryResult], None] | None = None,
        **kwargs: object,
    ) -> tuple[PoEQueryResult, ...]:
        del credentials, kwargs
        assert on_result is not None
        on_result(phase_result(groups[0], complete=False))
        assert release.wait(3)
        raise RuntimeError("LLDP transport failed")

    service.collect = collect_partial
    dialog.collect_button.click()
    try:
        key = ("192.0.2.1:22", "1")
        qtbot.waitUntil(lambda: key in dialog._results)
        assert dialog.progress_bar.value() == 0
        dialog.stop()
        assert dialog.is_busy
        assert dialog._results[key].snapshot.watts == 8.7
    finally:
        release.set()
    qtbot.waitUntil(lambda: not dialog.is_busy)
    result = dialog._results[key]
    assert result.complete
    assert result.status == QueryStatus.OK
    assert result.lldp.status == LLDPStatus.CANCELLED
    assert result.identity.hostname == "real-sw"
    assert result.snapshot.watts == 8.7
    assert dialog._items[key].text(1) == "조회 취소"
    assert dialog.progress_bar.value() == 100


@pytest.mark.parametrize("delimiter", [",", "\t"], ids=["csv", "tsv"])
@pytest.mark.parametrize(
    "rows",
    [
        (("ip", "ssh_port", "interfaces"), ("192.0.2.9", "22")),
        (("ip", "interfaces"), ("192.0.2.9", "24"), ("192.0.2.10", "24", "12")),
        (("192.0.2.9", "24"), ("192.0.2.10", "2222", "12")),
        (("192.0.2.9", "2222", "24", "별도"), ("192.0.2.10", "12")),
    ],
)
def test_cross_schema_paste_keeps_existing_targets_atomic(
    app: QApplication,
    qtbot: object,
    delimiter: str,
    rows: tuple[tuple[str, ...], ...],
) -> None:
    dialog, service = setup_dialog(qtbot)
    before = dialog._validated_targets()
    before_cells = tuple(
        tuple(dialog._cell_text(row, column) for column in range(4))
        for row in range(dialog.targets_table.rowCount())
    )
    with pytest.raises(ValueError):
        dialog.paste_targets("\n".join(delimiter.join(row) for row in rows))
    assert dialog._validated_targets() == before
    assert (
        tuple(
            tuple(dialog._cell_text(row, column) for column in range(4))
            for row in range(dialog.targets_table.rowCount())
        )
        == before_cells
    )
    assert service.calls == []


def test_large_query_gui_batches_result_signals_without_losing_final_state(
    app: QApplication, qtbot: object
) -> None:
    from aruba2930f_backup.models import DeviceTarget

    dialog = PoEQueryDialog(username="operator", password="test-password")
    qtbot.addWidget(dialog)
    group = PoETarget(DeviceTarget("192.0.2.10"), ("24",))
    dialog._build_results((group,))
    dialog._run_total = 1_000
    item = dialog._items[(group.target.endpoint, "24")]
    assert item.text(5) == ""
    row = PoEQueryResult(
        group,
        "24",
        QueryStatus.FAILED,
        message="synthetic query failure",
        complete=True,
    )
    dialog._on_result(row)
    assert len(dialog._render_queue) == 1
    assert item.text(5) == ""
    dialog._flush_result_queue()
    assert not dialog._render_queue
    assert item.text(5) == "실패"
    dialog.close()
