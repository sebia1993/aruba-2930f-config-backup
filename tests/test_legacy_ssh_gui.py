"""Offline Qt tests for explicit WC.16.01 legacy SSH authorization."""

from __future__ import annotations

import os

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from aruba2930f_backup.models import DeviceTarget
from aruba2930f_backup.poe_query_models import PoETarget
from aruba2930f_backup.ssh_legacy import session_legacy_ssh
from aruba2930f_backup.ssh_legacy_gui import LegacySSHConsentDialog
from aruba2930f_backup.unified_port_gui import UnifiedPortManagementDialog

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

TARGET = DeviceTarget("192.0.2.10", 22)


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def test_legacy_ssh_dialog_requires_exact_endpoint_confirmation(app: QApplication) -> None:
    dialog = LegacySSHConsentDialog((TARGET,))
    assert not dialog.confirm_button.isEnabled()
    dialog.confirm_input.setText("192.0.2.10")
    assert not dialog.confirm_button.isEnabled()
    dialog.confirm_input.setText(TARGET.endpoint)
    assert dialog.confirm_button.isEnabled()
    assert "ssh-rsa" in dialog.warning_label.text()
    assert dialog.selected_target == TARGET
    dialog.close()


def test_unified_gui_allows_only_confirmed_switch_without_mutation(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_legacy_ssh.clear()
    dialog = UnifiedPortManagementDialog(username="operator", password="test-password")
    dialog._build_results((PoETarget(TARGET, ("1", "2")),))
    refreshed: list[tuple[PoETarget, ...]] = []
    monkeypatch.setattr(dialog, "_refresh", lambda groups: refreshed.append(groups))
    monkeypatch.setattr(LegacySSHConsentDialog, "exec", lambda _self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(LegacySSHConsentDialog, "selected_target", property(lambda _self: TARGET))
    try:
        dialog.legacy_ssh_button.click()
        assert session_legacy_ssh.allowed(TARGET)
        assert len(refreshed) == 1
        assert refreshed[0][0].interfaces == ("1", "2")
        assert dialog._batch_plan is None
    finally:
        session_legacy_ssh.clear()
        dialog.close()
