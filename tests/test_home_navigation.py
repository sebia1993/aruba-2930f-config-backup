"""Focused UI regression tests for home shortcuts, isolated from port-management tests."""

from __future__ import annotations

import os

import pytest
from PySide6.QtWidgets import QApplication, QPushButton

from aruba2930f_backup.manager_gui import NetworkManagerWindow


@pytest.mark.gui
def test_home_hides_settings_shortcut_but_preserves_ssh_trust_management() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    window = NetworkManagerWindow()
    window.show()
    app.processEvents()
    try:
        shortcuts = window.home_page.findChildren(QPushButton)
        assert len(shortcuts) == 3
        assert {button.objectName() for button in shortcuts} == {
            "homeShortcut1",
            "homeShortcut2",
            "homeShortcut3",
        }
        assert not any("SSH 지문" in button.text() for button in shortcuts)

        settings_nav = window.findChild(QPushButton, "navSettings")
        assert settings_nav is not None
        settings_nav.click()
        app.processEvents()
        assert window.pages.currentIndex() == 4

        # A changed key is never silently re-approved; the operator still has
        # access to existing trust records from the advanced settings page.
        trust_button = window.findChild(QPushButton, "managerTrustedKeys")
        assert trust_button is not None
        assert trust_button.isVisible()

        # Result output is a different path from the internal host-key store.
        assert window.backup_page.output_input.text()
    finally:
        window.close()
