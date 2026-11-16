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
                result = PoEQueryResult(
                    group,
                    interface,
                    QueryStatus.CANCELLED if cancelled else QueryStatus.OK,
                    None if cancelled else snapshot,
                    finished_at="2026-11-16T10:00:01+00:00",
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
    assert "3/3" in group.text(1)
    assert group.child(0).text(4) == "0 W"
    assert group.child(1).text(4) == "확인 안 됨"
    assert group.child(0).text(5)
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
    service.release = threading.Event()
    button = {
        "all": dialog.collect_button,
        "group": dialog.refresh_group_button,
        "port": dialog.refresh_port_button,
    }[scope]
    button.click()
    try:
        assert first.child(0).text(4) == ""
        assert first.child(0).text(6) == "대기"
        if scope == "port":
            assert first.child(1).text(4) == "확인 안 됨"
        if scope != "all":
            assert dialog.result_tree.topLevelItem(1).child(0).text(4) == "확인 안 됨"
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
    assert failed.text(4) == "확인 안 됨"
    assert failed.text(6) == "실패"
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
    assert "취소" in dialog.result_tree.topLevelItem(0).child(0).text(6)


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
