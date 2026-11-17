"""Offline Qt coverage for the integrated five-page operator application."""

from __future__ import annotations

import json
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
        "홈",
        "컨피그 백업",
        "포트 통합 관리",
        "작업 이력",
        "설정",
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
    assert not window.backup_page.username_input.isVisible()
    assert not window.backup_page.password_input.isVisible()
    assert window.ssh_session_button.isVisible()
    window.close()


def test_port_page_keeps_query_and_control_together_and_collapses_settings(
    app: QApplication,
) -> None:
    window = make_window(app)
    assert window.navigate(2)
    port = window.port_page
    # No side panel steals width from the switch/port status table.
    assert port.findChild(QSplitter, "portResultDetailSplitter") is None
    assert port.findChild(QScrollArea, "portDetailsAndControl") is None
    assert port.result_tree.isVisible()
    assert port.result_tree.parentWidget() is not port
    assert window.port_vertical_splitter.count() == 2
    assert port.result_tree.width() >= 700
    assert port.search_input.isVisible()
    assert port.findChild(QScrollArea, "poeDetailsScroll").isHidden()
    assert not port.action_combo.isVisible()
    assert not port.run_button.isVisible()
    assert window.port_advanced_toggle.isVisible()
    assert port.quick_off_button.isVisible()
    assert port.quick_on_button.isVisible()
    assert window.port_target_scroll.isVisible()
    window.port_target_toggle.click()
    assert not window.port_target_scroll.isVisible()
    window.port_target_toggle.click()
    assert window.port_target_scroll.isVisible()
    assert port.targets_table.isVisible()
    assert not port.username_input.isVisible()
    assert not port.password_input.isVisible()
    assert not window.port_advanced_scroll.isVisible()
    window.port_advanced_toggle.click()
    assert window.port_advanced_scroll.isVisible()
    assert port.action_combo.isVisible()
    assert port.run_button.isVisible()
    window.port_advanced_toggle.click()
    assert not window.port_advanced_scroll.isVisible()
    assert not port.run_button.isVisible()
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
        target,
        "12",
        "SEOUL-F1-SW-01",
        "Aruba 2930F",
        True,
        "Up",
        True,
        "Delivering",
        15.0,
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
        QMessageBox,
        "information",
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


def test_esc_and_direct_reject_cannot_hide_embedded_port_workspace(
    app: QApplication,
) -> None:
    window = make_window(app)
    assert window.navigate(2)
    port = window.port_page
    assert port.isVisible()
    port.reject()
    QApplication.processEvents()
    assert port.isVisible()
    assert window.pages.currentIndex() == 2
    port.close()
    QApplication.processEvents()
    assert port.isVisible()
    window.close()


def test_port_journals_use_recorded_state_and_latest_file_mtime(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aruba2930f_backup.manager_gui as manager_gui

    root = tmp_path / "backups"
    journal_dir = root / "port-control" / "batch"
    journal_dir.mkdir(parents=True)
    monkeypatch.setattr(manager_gui, "default_output_directory", lambda: root)
    header = {
        "event": "plan",
        "schema": 1,
        "action": "poe_off",
        "items": [{"endpoint": "192.0.2.10:22", "hostname": "SEOUL-SW-01", "interface": "12"}],
    }
    for number in range(31):
        path = journal_dir / f"z{number:02d}.jsonl"
        path.write_text(
            json.dumps(header)
            + "\n"
            + json.dumps({"event": "result", "index": 0, "status": "verified"})
            + "\n",
            encoding="utf-8",
        )
        os.utime(path, (100 + number, 100 + number))
    latest = journal_dir / "a-latest.jsonl"
    latest.write_text(
        json.dumps(header) + "\n" + json.dumps({"event": "intent", "index": 0}) + "\n",
        encoding="utf-8",
    )
    os.utime(latest, (500, 500))

    window = make_window(app)
    window.backup_page.output_input.setText(str(root))
    window.navigate(3)
    first = window.history_table.topLevelItem(0)
    assert first.text(1) == "SEOUL-SW-01"
    assert first.text(2) == "192.0.2.10"
    assert "result_unknown" in first.text(3)
    assert first.text(4).endswith("a-latest.jsonl")
    assert window.history_table.topLevelItemCount() == 30
    window.close()


def test_port_paste_entry_is_visible_without_expanding_ssh_settings(
    app: QApplication,
) -> None:
    window = make_window(app)
    window.navigate(2)
    assert window.port_page.paste_button.isVisible()
    assert window.port_target_scroll.isVisible()
    assert "2열" in window.port_page.paste_button.text()
    window.port_target_toggle.click()
    assert not window.port_target_scroll.isVisible()
    assert window.port_page.paste_button.isVisible()
    assert window.port_page.quick_off_button.isVisible()
    assert window.port_page.quick_on_button.isVisible()
    assert not window.port_page.quick_cycle_button.isVisible()
    assert not window.port_page.cycle_button.isVisible()
    assert not window.port_page.cycle_button.isVisible()
    window.close()


def test_ssh_account_is_shared_without_copying_passwords_to_feature_views(
    app: QApplication,
) -> None:
    window = make_window(app)
    session = window._ssh_credentials
    assert not session.configured
    assert "미설정" in window.ssh_session_status.text()
    session.configure("operator", "session-password", "enable-secret")
    window._refresh_ssh_session_status()
    assert window.ssh_session_status.text().startswith("SSH 계정: operator")
    assert "session-password" not in window.ssh_session_status.text()

    backup = window.backup_page
    backup.ip_input.setPlainText("192.0.2.10")
    request = backup.build_request()
    assert request.username == "operator"
    assert request.password == "session-password"
    assert request.enable_password == "enable-secret"
    assert backup.password_input.text() == ""
    assert backup.username_input.text() == ""

    # Backup's legacy per-run clear must not revoke the integrated session.
    backup._finalize_completed_run()
    # Even if a stale legacy input was populated, navigation must not copy it.
    backup.password_input.setText("stale-legacy-password")
    assert window.navigate(2)
    port = window.port_page
    assert port._credentials() is session.require()
    assert port.password_input.text() == ""
    assert port.enable_password_input.text() == ""
    assert window.ssh_session_clear_button.isEnabled()
    window.close()
    assert not session.configured


def test_ssh_session_change_revokes_stale_port_rows(app: QApplication) -> None:
    window = make_window(app)
    window._ssh_credentials.configure("first", "first-pass")
    window.port_page._build_results((PoETarget(DeviceTarget("192.0.2.10"), ("12",)),))
    assert window.port_page.result_tree.topLevelItemCount() == 1
    # Changing account must invalidate old query state, but preserve target input.
    window._ssh_credentials.configure("second", "second-pass")
    window.port_page._invalidate()
    assert window.port_page.result_tree.topLevelItemCount() == 0
    assert window.port_page._batch_plan is None
    window.close()


def test_ssh_session_clear_and_missing_account_block_actions(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = make_window(app)
    window._ssh_credentials.configure("operator", "demo-password")
    window._refresh_ssh_session_status()
    window._clear_ssh_session()
    assert not window._ssh_credentials.configured
    assert not window.ssh_session_clear_button.isEnabled()
    assert "미설정" in window.ssh_session_status.text()

    monkeypatch.setattr(window, "_configure_ssh_session", lambda: False)
    with pytest.raises(ValueError, match="취소"):
        window._require_ssh_credentials()
    window.close()


def test_ssh_session_controls_reject_changes_during_active_backup(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtCore import QThread

    window = make_window(app)
    window._ssh_credentials.configure("operator", "session-secret")
    window._refresh_ssh_session_status()
    notices: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, _title, message: notices.append(message) or QMessageBox.StandardButton.Ok,
    )
    fake_thread = QThread(window)
    window.backup_page._thread = fake_thread
    try:
        assert not window._configure_ssh_session()
        window._clear_ssh_session()
        assert window._ssh_credentials.configured
        assert len(notices) == 2
        window._refresh_status()
        assert not window.ssh_session_button.isEnabled()
        assert not window.ssh_session_clear_button.isEnabled()
    finally:
        window.backup_page._thread = None
        fake_thread.deleteLater()
    window.close()


def test_common_ssh_account_dialog_is_shared_and_reprompts_after_clear(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aruba2930f_backup.manager_gui as manager_gui
    from aruba2930f_backup.models import Credentials

    window = make_window(app)
    calls: list[str] = []

    class FakeCredentialDialog:
        def __init__(self, _parent: object, *, username: str = "") -> None:
            calls.append(username)

        def exec(self) -> object:
            return manager_gui.QDialog.DialogCode.Accepted

        def take_credentials(self) -> Credentials:
            return Credentials("network-operator", "shared-secret", "shared-enable")

        def deleteLater(self) -> None:
            return

    monkeypatch.setattr(manager_gui, "SessionCredentialDialog", FakeCredentialDialog)
    credential = window._require_ssh_credentials()
    assert credential.username == "network-operator"
    assert credential.password == "shared-secret"
    assert window._require_ssh_credentials() is credential
    assert calls == [""]
    assert window.ssh_session_status.text().startswith("SSH 계정: network-operator")
    assert "shared-secret" not in window.ssh_session_status.text()

    window.port_page._build_results((PoETarget(DeviceTarget("192.0.2.10"), ("12",)),))
    assert window.port_page.result_tree.topLevelItemCount() == 1
    assert window._configure_ssh_session()
    assert calls == ["", "network-operator"]
    assert window.port_page.result_tree.topLevelItemCount() == 0
    window._clear_ssh_session()
    assert not window._ssh_credentials.configured
    assert window._require_ssh_credentials().username == "network-operator"
    assert len(calls) == 3
    window.close()


def test_port_vertical_splitter_expands_input_and_preserves_results(app: QApplication) -> None:
    from PySide6.QtCore import Qt

    window = make_window(app)
    window.navigate(2)
    port = window.port_page
    assert window.port_target_toggle.isChecked()
    assert window.port_target_scroll.isVisible()
    assert window.port_vertical_splitter.orientation() == Qt.Orientation.Vertical
    assert port.result_tree.parentWidget() is not None
    window.port_target_toggle.setChecked(False)
    assert not window.port_target_scroll.isVisible()
    assert port.result_tree.isVisible()
    window.port_target_toggle.setChecked(True)
    assert window.port_target_scroll.isVisible()
    assert port.result_tree.isVisible()
    window.close()
