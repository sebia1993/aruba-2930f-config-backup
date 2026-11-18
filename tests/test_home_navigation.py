"""Focused UI regression tests for home shortcuts, isolated from port-management tests."""

from __future__ import annotations

import os

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from aruba2930f_backup.home_shortcuts import HomeShortcutButton
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
        assert not any("SSH 지문" in button.accessibleName() for button in shortcuts)
        for button, expected_title in zip(
            shortcuts,
            ("컨피그 백업", "포트 통합 관리", "작업 이력"),
            strict=True,
        ):
            assert isinstance(button, HomeShortcutButton)
            assert button.accessibleName() == expected_title
            assert button.toolTip()
            assert button.height() == 64
            title = button.findChild(QLabel, "homeShortcutTitle")
            icon = button.findChild(QLabel, "homeShortcutIcon")
            assert title is not None and title.text() == expected_title
            assert icon is not None and not icon.pixmap().isNull()

        # The cards are actual clickable buttons, not a decorative mock-up.
        for index in (1, 2, 3):
            window.findChild(QPushButton, f"homeShortcut{index}").click()
            assert window.pages.currentIndex() == index
            assert window.navigate(0)

        # No settings menu, sidebar entry or hidden fifth page in the app.
        assert window.pages.count() == 4
        assert {button.text() for button in window._navigation_buttons} == {
            "홈",
            "컨피그 백업",
            "포트 통합 관리",
            "작업 이력",
        }
        assert window.findChild(QPushButton, "navSettings") is None
        assert window.findChild(QPushButton, "managerTrustedKeys") is None
        assert not hasattr(window, "settings_page")
        assert not window.navigate(4)
        assert window.pages.currentIndex() == 0

        # The protected first-use/changed-key SSH flow stays in the backend.
        assert callable(window.backup_page._manage_trusted_keys)
        assert window.navigate(1)
        assert window.backup_page.output_input.isVisible()
        assert window.backup_page.output_input.text()
    finally:
        window.close()


@pytest.mark.gui
def test_four_page_navigation_does_not_expose_a_fifth_settings_page() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    window = NetworkManagerWindow()
    window.show()
    app.processEvents()
    try:
        assert window.pages.count() == 4
        assert window._navigation_buttons[-1].objectName() == "navHistory"
        assert all(
            button.objectName() != "navSettings" for button in window.findChildren(QPushButton)
        )
        assert not window.navigate(4)
        assert not hasattr(window, "settings_page")
        assert window.backup_page.output_input.text()
        assert callable(window.backup_page._manage_trusted_keys)
    finally:
        window.close()
