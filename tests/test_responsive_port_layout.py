"""Exercise the embedded Windows port workspace at logical window sizes."""

import os

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QRegion
from PySide6.QtWidgets import QApplication, QTabBar, QWidget

from aruba2930f_backup.manager_gui import NetworkManagerWindow
from aruba2930f_backup.models import DeviceTarget
from aruba2930f_backup.poe_query_models import PoETarget

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytestmark = pytest.mark.gui


@pytest.fixture
def window():
    app = QApplication.instance() or QApplication([])
    manager = NetworkManagerWindow()
    manager.resize(1093, 614)
    manager.show()
    manager.navigate(2)
    group = PoETarget(DeviceTarget("192.0.2.10"), tuple(str(n) for n in range(1, 49)))
    manager.port_page._build_results((group,))
    app.processEvents()
    yield manager
    manager.close()
    app.processEvents()


@pytest.mark.parametrize(("height", "minimum_rows"), [(550, 5), (614, 8)])
def test_compact_list_has_complete_rows_and_reachable_poe_buttons(window, height, minimum_rows):
    window.resize(1093, height)
    port = window.port_page
    # A finished query keeps the progress bar and terminal summary populated.
    port.progress_panel.start_query(48)
    port.progress_panel.finish_query(48, 48)
    port.full_query_succeeded.emit()
    QApplication.processEvents()
    viewport = port.result_tree.viewport()
    switch = port.result_tree.topLevelItem(0)
    fully_visible_rows = 0
    for index in range(switch.childCount()):
        rect = port.result_tree.visualItemRect(switch.child(index))
        band = QRect(3, rect.top(), 1, rect.height())
        if band.height() > 0 and QRegion(band).subtracted(viewport.visibleRegion()).isEmpty():
            fully_visible_rows += 1
    assert fully_visible_rows >= minimum_rows
    last_port = switch.child(47)
    port.result_tree.scrollToItem(last_port)
    QApplication.processEvents()
    last_rect = port.result_tree.visualItemRect(last_port)
    assert (
        QRegion(QRect(3, last_rect.y(), 1, last_rect.height()))
        .subtracted(viewport.visibleRegion())
        .isEmpty()
    )
    assert not window.port_target_toggle.isChecked()
    for button in (port.quick_on_button, port.quick_off_button):
        point = button.mapTo(window, button.rect().center())
        assert window.childAt(point) is button
    window.port_target_toggle.click()
    assert window.port_target_scroll.isVisible()
    window.resize(1024, 615)
    QApplication.processEvents()
    assert window.port_target_scroll.isVisible()
    port.full_query_succeeded.emit()
    assert window.port_target_scroll.isVisible()


def test_wide_completion_leaves_settings_open_until_a_later_compact_query(window):
    window.resize(1680, 1080)
    window.port_page.full_query_succeeded.emit()
    assert window.port_target_scroll.isVisible()
    window.resize(1093, 614)
    assert window.port_target_scroll.isVisible()
    window.port_page.full_query_succeeded.emit()
    assert not window.port_target_scroll.isVisible()


def test_matrix_and_list_share_filter_focus_and_checked_targets_across_resize(window):
    port = window.port_page
    window.port_target_toggle.setChecked(False)
    tabs = port.findChild(QTabBar, "portResultTabs")
    assert tabs is not None and tabs.isVisible()
    tree = port.result_tree
    endpoint = port._groups[0].target.endpoint
    checked = (endpoint, "24")
    port._items[checked].setCheckState(0, Qt.CheckState.Checked)
    port.search_input.setText("48")
    port._apply_search_filter()
    matrix = port.findChild(QWidget, "portStatusMatrix")
    assert matrix is not None
    assert matrix.model.shown == 1
    tabs.setCurrentIndex(next(i for i in range(tabs.count()) if tabs.tabText(i) == "매트릭스"))
    QApplication.processEvents()
    assert matrix.isVisible() and not tree.isVisible()
    matrix._tile_clicked(matrix.model.index_for((endpoint, "48")))
    assert tree.currentItem() is port._items[(endpoint, "48")]
    assert port._checked_port_keys == {checked}
    window.resize(1680, 1080)
    QApplication.processEvents()
    assert matrix.isVisible() and tree.isVisible() and not tabs.isVisible()
    assert port._checked_port_keys == {checked}
    window.resize(1093, 614)
    QApplication.processEvents()
    assert matrix.isVisible() and not tree.isVisible()
    assert tree.currentItem() is port._items[(endpoint, "48")]
    assert port.search_input.text() == "48"


def test_laptop_columns_fit_and_restore_user_widths(window):
    tree = window.port_page.result_tree
    window.port_target_toggle.setChecked(False)
    QApplication.processEvents()
    assert tree.horizontalScrollBar().maximum() == 0
    tree.setColumnWidth(1, 143)
    window.resize(1680, 1080)
    tree.setColumnWidth(1, 251)
    window.resize(1093, 614)
    assert tree.columnWidth(1) == 143
    window.resize(1680, 1080)
    assert tree.columnWidth(1) == 251


@pytest.mark.parametrize(("work_area", "preferred"), [((1093, 614), False), ((1920, 1080), True)])
def test_startup_fits_work_area_and_keeps_preferred_large_size(monkeypatch, work_area, preferred):
    app = QApplication.instance() or QApplication([])
    bounds = QRect(0, 0, *work_area)
    monkeypatch.setattr(
        "aruba2930f_backup.developer_inspector._available_screen_geometry",
        lambda *args: bounds,
    )
    manager = NetworkManagerWindow()
    try:
        assert bounds.contains(manager.geometry())
        if preferred:
            assert manager.size().toTuple() == (1320, 840)
        else:
            assert manager.width() < bounds.width()
            assert manager.height() < bounds.height()
    finally:
        manager.close()
        app.processEvents()
