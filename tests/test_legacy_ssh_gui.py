"""The automatic compatibility path must not require an extra GUI approval."""

from __future__ import annotations

import os

import pytest
from PySide6.QtWidgets import QApplication, QPushButton

from aruba2930f_backup.unified_port_gui import UnifiedPortManagementDialog

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def test_auto_ssh_has_no_operator_compatibility_approval_button(app: QApplication) -> None:
    dialog = UnifiedPortManagementDialog()
    try:
        assert not hasattr(dialog, "legacy_ssh_button")
        assert dialog.findChild(QPushButton, "unifiedLegacySsh") is None
    finally:
        dialog.close()
