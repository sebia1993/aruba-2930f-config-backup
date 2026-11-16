from __future__ import annotations

import os
import threading
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


class FakePortService:
    def __init__(self) -> None:
        self.applied: list[tuple[object, object, str]] = []
        self.wait: threading.Event | None = None
        self.snapshot: object | None = None

    def inspect(
        self, target: object, interface: str, credentials: object, **kwargs: object
    ) -> object:
        del credentials, kwargs
        if self.wait is not None:
            assert self.wait.wait(5)
        from aruba2930f_backup.port_control import PortSnapshot

        self.snapshot = PortSnapshot(
            target=target,
            interface=interface,
            hostname="lab-edge",
            model="Aruba 2930F",
            enabled=True,
            link_status="Down",
            poe_enabled=True,
            poe_status="Searching",
            watts=0.0,
        )
        return self.snapshot

    def apply(
        self, snapshot: object, action: object, credentials: object, *, confirmation: str
    ) -> object:
        del credentials
        self.applied.append((snapshot, action, confirmation))
        return SimpleNamespace(
            status="verified",
            message="요청한 설정을 확인했습니다.",
            before=snapshot,
            after=snapshot,
            audit_path=None,
        )


def dialog_with_inputs(app: QApplication, qtbot: object) -> tuple[object, FakePortService]:
    del app
    from aruba2930f_backup.port_control_gui import PortControlDialog

    service = FakePortService()
    dialog = PortControlDialog(service=service)
    qtbot.addWidget(dialog)
    dialog.ip_input.setText("192.0.2.10")
    dialog.interface_input.setText("1/24")
    dialog.username_input.setText("operator")
    dialog.password_input.setText("test-password")
    return dialog, service


def test_actions_require_inspection_and_input_changes_invalidate_it(
    app: QApplication, qtbot: object
) -> None:
    dialog, service = dialog_with_inputs(app, qtbot)
    assert not any(button.isEnabled() for button in dialog.action_buttons.values())
    dialog.inspect_button.click()
    qtbot.waitUntil(lambda: dialog._thread is None)
    assert dialog.action_buttons["poe_off"].isEnabled()
    assert "포트 사용: 켜짐" in dialog.state_label.text()
    assert "링크: Down" in dialog.state_label.text()
    assert "Searching" in dialog.state_label.text()
    dialog.interface_input.setText("1/25")
    assert not any(button.isEnabled() for button in dialog.action_buttons.values())
    assert service.applied == []


def test_cancelled_confirmation_sends_no_command(
    app: QApplication, qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aruba2930f_backup import port_control_gui

    dialog, service = dialog_with_inputs(app, qtbot)
    dialog.inspect_button.click()
    qtbot.waitUntil(lambda: dialog._thread is None)
    monkeypatch.setattr(
        port_control_gui.PortConfirmationDialog, "exec", lambda self: QDialog.DialogCode.Rejected
    )
    dialog.action_buttons["poe_off"].click()
    assert service.applied == []


def test_confirmed_action_consumes_snapshot_and_requires_fresh_inspection(
    app: QApplication, qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aruba2930f_backup import port_control_gui

    dialog, service = dialog_with_inputs(app, qtbot)
    dialog.inspect_button.click()
    qtbot.waitUntil(lambda: dialog._thread is None)

    def confirm(self: object) -> int:
        self.confirmation_input.setText(self.expected_confirmation)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(port_control_gui.PortConfirmationDialog, "exec", confirm)
    dialog.action_buttons["disable"].click()
    qtbot.waitUntil(lambda: dialog._thread is None)
    assert len(service.applied) == 1
    assert service.applied[0][2] == service.snapshot.confirmation_text
    assert not any(button.isEnabled() for button in dialog.action_buttons.values())


def test_confirmation_needs_exact_target(app: QApplication, qtbot: object) -> None:
    from aruba2930f_backup.models import DeviceTarget
    from aruba2930f_backup.port_control import PortAction
    from aruba2930f_backup.port_control_gui import PortConfirmationDialog

    snapshot = FakePortService().inspect(DeviceTarget("192.0.2.10"), "24", object())
    dialog = PortConfirmationDialog(snapshot, PortAction.POE_OFF)
    qtbot.addWidget(dialog)
    assert not dialog.execute_button.isEnabled()
    dialog.confirmation_input.setText("192.0.2.11:22 24")
    assert not dialog.execute_button.isEnabled()
    dialog.confirmation_input.setText(snapshot.confirmation_text)
    assert dialog.execute_button.isEnabled()


def test_busy_window_does_not_close_or_send_duplicate_requests(
    app: QApplication, qtbot: object
) -> None:
    dialog, service = dialog_with_inputs(app, qtbot)
    service.wait = threading.Event()
    dialog.show()
    dialog.inspect_button.click()
    try:
        assert not dialog.inspect_button.isEnabled()
        dialog.reject()
        assert dialog.isVisible()
    finally:
        service.wait.set()
    qtbot.waitUntil(lambda: dialog._thread is None)
    dialog.reject()
    assert not dialog.isVisible()


def test_backup_disables_port_control_entry(app: QApplication, qtbot: object) -> None:
    from aruba2930f_backup.gui import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    assert window.port_control_button.isEnabled()
    window._set_running(True)
    assert not window.port_control_button.isEnabled()
    window._set_running(False)
    assert window.port_control_button.isEnabled()


@pytest.mark.parametrize("changing", [False, True])
def test_parent_close_waits_for_active_modal_port_operation(
    app: QApplication,
    qtbot: object,
    monkeypatch: pytest.MonkeyPatch,
    changing: bool,
) -> None:
    from aruba2930f_backup import port_control_gui
    from aruba2930f_backup.gui import MainWindow
    from aruba2930f_backup.models import Credentials, DeviceTarget
    from aruba2930f_backup.port_control import PortAction

    parent = MainWindow()
    qtbot.addWidget(parent)
    parent.show()
    service = FakePortService()
    release = threading.Event()
    close_results: list[bool] = []
    original_dialog = port_control_gui.PortControlDialog
    original_apply = service.apply

    def blocked_apply(*args: object, **kwargs: object) -> object:
        assert release.wait(5)
        return original_apply(*args, **kwargs)

    if changing:
        monkeypatch.setattr(service, "apply", blocked_apply)
    else:
        service.wait = release

    def build_dialog(*args: object, **kwargs: object) -> object:
        dialog = original_dialog(*args, service=service, **kwargs)
        dialog.ip_input.setText("192.0.2.10")
        dialog.interface_input.setText("24")
        dialog.username_input.setText("operator")
        dialog.password_input.setText("test-password")

        def close_after_completion() -> None:
            if dialog._thread is None:
                dialog.reject()
            else:
                QTimer.singleShot(10, close_after_completion)

        def try_parent_close() -> None:
            try:
                close_results.append(parent.close())
            finally:
                release.set()
                QTimer.singleShot(10, close_after_completion)

        def begin() -> None:
            if changing:
                snapshot = service.inspect(
                    DeviceTarget("192.0.2.10"), "24", Credentials("operator", "test-password")
                )
                dialog._snapshot = snapshot
                dialog._start(PortAction.DISABLE, snapshot.confirmation_text)
            else:
                dialog.inspect_button.click()
            QTimer.singleShot(20, try_parent_close)

        QTimer.singleShot(0, begin)
        return dialog

    monkeypatch.setattr(port_control_gui, "PortControlDialog", build_dialog)
    parent._open_port_control()
    assert close_results == [False]
    assert parent.isVisible()
