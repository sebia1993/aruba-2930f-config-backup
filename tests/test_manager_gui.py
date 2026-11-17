"""Offline Qt coverage for the integrated five-page operator application."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from openpyxl import Workbook
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QMessageBox, QScrollArea, QSplitter

from aruba2930f_backup.manager_gui import (
    NetworkManagerWindow,
    recent_backup_reports,
    report_device_rows,
)
from aruba2930f_backup.models import DeviceTarget
from aruba2930f_backup.poe_query_models import PoEQueryResult, PoETarget, QueryStatus
from aruba2930f_backup.port_control import PortSnapshot

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def make_window(app: QApplication) -> NetworkManagerWindow:
    del app
    window = NetworkManagerWindow()
    window.show()
    QApplication.processEvents()
    return window


def test_navigation_has_five_pages_and_keeps_existing_engines(app: QApplication) -> None:
    window = make_window(app)
    assert window.pages.count() == 5
    assert window.windowTitle() == "Aruba 2930F · 네트워크 통합 관리"
    assert [button.text() for button in window._navigation_buttons] == [
        "홈", "컨피그 백업", "포트 통합 관리", "작업 이력", "설정"
    ]
    for index in range(5):
        assert window.navigate(index)
        assert window.pages.currentIndex() == index
        assert window._navigation_buttons[index].isChecked()
    assert window.backup_page.start_button is not None
    assert window.port_page.collect_button is not None
    assert window.port_page.run_button is not None
    window.close()


def test_backup_settings_and_results_are_in_distinct_splitter_panes(
    app: QApplication,
) -> None:
    window = make_window(app)
    window.navigate(1)
    splitter = window.backup_page.findChild(QSplitter, "backupMainSplitter")
    assert splitter is not None
    assert splitter.count() == 2
    assert window.backup_page.ip_input.isVisible()
    assert window.backup_page.result_table.isVisible()
    assert window.backup_page.start_button.isVisible()
    assert window.backup_page.poe_query_button.isHidden()
    window.close()


def test_port_page_keeps_query_and_control_together_and_collapses_settings(
    app: QApplication,
) -> None:
    window = make_window(app)
    assert window.navigate(2)
    port = window.port_page
    splitter = port.findChild(QSplitter, "portResultDetailSplitter")
    assert splitter is not None
    assert splitter.count() == 2
    assert port.result_tree.isVisible()
    assert port.search_input.isVisible()
    assert port.action_combo.isVisible()
    assert port.run_button.isVisible()
    assert not window.port_target_scroll.isVisible()
    window.port_target_toggle.click()
    assert window.port_target_scroll.isVisible()
    assert port.targets_table.isVisible()
    assert port.username_input.isVisible()
    window.close()


def test_backup_ip_list_seeds_empty_port_targets_without_clobbering_profiles(
    app: QApplication,
) -> None:
    window = make_window(app)
    window.backup_page.ip_input.setPlainText("192.0.2.10\n192.0.2.20")
    window.navigate(2)
    assert window.port_page.targets_table.rowCount() == 2
    assert window.port_page.targets_table.item(0, 0).text() == "192.0.2.10"
    window.port_page._replace_targets_rows((("192.0.2.30", "2222", "12", "현장 A"),))
    window.navigate(1)
    window.navigate(2)
    assert window.port_page.targets_table.rowCount() == 1
    assert window.port_page.targets_table.item(0, 0).text() == "192.0.2.30"
    assert window.port_page.targets_table.item(0, 1).text() == "2222"
    assert window.port_page.targets_table.item(0, 3).text() == "현장 A"
    window.close()


def test_hostname_and_ip_are_shown_together_after_query(app: QApplication) -> None:
    window = make_window(app)
    window.navigate(2)
    target = DeviceTarget("192.0.2.10", 22)
    group = PoETarget(target, ("12",))
    window.port_page._build_results((group,))
    first = window.port_page.result_tree.topLevelItem(0)
    assert "192.0.2.10" in first.text(0)
    snapshot = PortSnapshot(
        target, "12", "SEOUL-F1-SW-01", "Aruba 2930F",
        True, "Up", True, "Delivering", 15.0,
    )
    window.port_page._apply_result(
        PoEQueryResult(group, "12", QueryStatus.OK, snapshot=snapshot, complete=True)
    )
    display = first.text(0)
    assert display.startswith("SEOUL-F1-SW-01")
    assert "192.0.2.10" in display
    window.close()


def test_busy_navigation_is_blocked_without_interrupting_current_job(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = make_window(app)
    window.navigate(1)
    fake_thread = QThread(window)
    window.backup_page._thread = fake_thread
    messages: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "information",
        lambda _parent, _title, message: messages.append(message) or QMessageBox.StandardButton.Ok,
    )
    try:
        assert not window.navigate(2)
        assert window.pages.currentIndex() == 1
        assert messages and "실행 중" in messages[0]
    finally:
        window.backup_page._thread = None
    window.close()


def test_history_displays_actual_hostname_ip_from_saved_xlsx(
    app: QApplication, tmp_path: Path
) -> None:
    run = tmp_path / "2026-11-17" / "130000"
    run.mkdir(parents=True)
    report = run / "result.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Devices"
    sheet.append(("IP", "Hostname", "Status"))
    sheet.append(("192.0.2.10", "SEOUL-F1-SW-01", "SUCCESS"))
    workbook.save(report)
    workbook.close()

    assert recent_backup_reports(tmp_path) == [report]
    assert report_device_rows(report) == [("SEOUL-F1-SW-01", "192.0.2.10", "SUCCESS")]

    window = make_window(app)
    window.backup_page.output_input.setText(str(tmp_path))
    window.navigate(3)
    first = window.history_table.topLevelItem(0)
    assert first.text(1) == "SEOUL-F1-SW-01"
    assert first.text(2) == "192.0.2.10"
    assert first.text(3) == "SUCCESS"
    window.close()


def test_history_missing_files_and_malformed_reports_fail_safely(tmp_path: Path) -> None:
    assert recent_backup_reports(tmp_path / "missing") == []
    broken = tmp_path / "broken.xlsx"
    broken.write_bytes(b"not an Excel workbook")
    with pytest.raises((OSError, ValueError)):
        report_device_rows(broken)


def test_credential_fields_are_not_persisted_in_view_settings(app: QApplication) -> None:
    window = make_window(app)
    window.backup_page.password_input.setText("sensitive-test")
    window.port_page.password_input.setText("another-sensitive-test")
    window.navigate(4)
    assert "sensitive-test" not in window.settings_status.text()
    assert "another-sensitive-test" not in window.settings_status.text()
    window.close()
