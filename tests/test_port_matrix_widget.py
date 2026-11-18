"""Offline read-only port matrix and one-click theme regression tests."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QModelIndex, QSettings, Qt
from PySide6.QtWidgets import QApplication

from aruba2930f_backup.manager_gui import NetworkManagerWindow
from aruba2930f_backup.models import DeviceTarget
from aruba2930f_backup.poe_power import LLDPObservation, LLDPStatus
from aruba2930f_backup.poe_query_models import PoEQueryResult, PoETarget, QueryStatus
from aruba2930f_backup.port_control import PortSnapshot
from aruba2930f_backup.port_matrix_widget import PortMatrixModel, PortMatrixPanel
from aruba2930f_backup.port_status_mapper import MatrixState, make_matrix_port, port_sort_key
from aruba2930f_backup.ui_theme import selected_theme, set_theme


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def sample(
    group: PoETarget,
    port: str,
    *,
    link: str = "Up",
    poe: bool | None = True,
    enabled: bool = True,
    watts: float | None = 0.0,
    power_status: str | None = None,
    complete: bool = True,
    lldp: LLDPObservation | None = None,
) -> PoEQueryResult:
    snapshot = PortSnapshot(
        group.target,
        port,
        "SW-TEST",
        "Aruba 2930F",
        enabled,
        link,
        poe,
        power_status or ("Delivering" if poe else "Disabled"),
        watts,
    )
    return PoEQueryResult(
        group, port, QueryStatus.OK, snapshot=snapshot, lldp=lldp, complete=complete
    )


def test_status_does_not_invent_down_from_missing_data_or_zero_watts() -> None:
    group = PoETarget(DeviceTarget("192.0.2.10", 22), ("1",))
    assert make_matrix_port(group, "1").state is MatrixState.UNKNOWN
    assert make_matrix_port(group, "1", sample(group, "1")).state is MatrixState.NORMAL
    assert make_matrix_port(group, "1", sample(group, "1")).watts == "0 W"
    assert (
        make_matrix_port(group, "1", sample(group, "1", link="Down")).state is MatrixState.ATTENTION
    )
    assert make_matrix_port(group, "1", sample(group, "1", poe=False)).state is MatrixState.OFF
    assert (
        make_matrix_port(group, "1", sample(group, "1", power_status="Fault")).state
        is MatrixState.WARNING
    )
    assert make_matrix_port(group, "1", sample(group, "1", poe=None)).state is MatrixState.WARNING
    assert make_matrix_port(group, "1", sample(group, "1", enabled=False)).state is MatrixState.OFF
    assert (
        make_matrix_port(group, "1", sample(group, "1", complete=False)).state
        is MatrixState.LOADING
    )
    assert (
        make_matrix_port(
            group, "1", PoEQueryResult(group, "1", QueryStatus.FAILED, message="SSH")
        ).state
        is MatrixState.UNKNOWN
    )
    assert (
        make_matrix_port(
            group, "1", sample(group, "1", lldp=LLDPObservation(LLDPStatus.FAILED, ()))
        ).state
        is MatrixState.WARNING
    )


def test_numeric_ports_and_ip_order_are_stable() -> None:
    assert sorted(("10", "2", "1/12", "1/2"), key=port_sort_key) == ["1/2", "1/12", "2", "10"]
    m = PortMatrixModel()
    groups = (
        PoETarget(DeviceTarget("192.0.2.10", 22), ("24", "2")),
        PoETarget(DeviceTarget("192.0.2.2", 22), ("12",)),
    )
    m.set_targets(groups)
    assert m._ordered == [
        ("192.0.2.2:22", "12"),
        ("192.0.2.10:22", "2"),
        ("192.0.2.10:22", "24"),
    ]


def test_matrix_renders_14400_ports_without_creating_cell_widgets(app: QApplication) -> None:
    groups = tuple(
        PoETarget(
            DeviceTarget(f"192.0.{i // 254}.{i % 254 + 1}", 22),
            tuple(str(port) for port in range(1, 49)),
        )
        for i in range(300)
    )
    panel = PortMatrixPanel()
    panel.set_targets(groups)
    assert panel.model.total == 14_400
    assert panel.model.shown == 14_400
    assert panel.model.columnCount() >= 1
    assert panel.model.rowCount() > 0
    assert panel.model.data(QModelIndex(), Qt.ItemDataRole.UserRole) is None
    assert panel.view.model() is panel.model
    assert panel.view.findChildren(QApplication) == []
    key = panel.model._ordered[25]
    panel.model.update_observation(sample(groups[0], key[1], link="Down"))
    assert panel.model.port(key) is not None
    assert panel.model.port(key).state is MatrixState.ATTENTION  # type: ignore[union-attr]
    panel.filter_to({key})
    assert panel.model.shown == 1
    assert panel.model.key_at(panel.model.index(0, 0)) == key
    panel.close()


def test_tile_click_is_inspection_only_and_keeps_existing_checked_keys(
    app: QApplication,
) -> None:
    window = NetworkManagerWindow()
    window.show()
    assert window.navigate(2)
    group = PoETarget(DeviceTarget("192.0.2.10", 22), ("1", "2", "24"))
    window.port_page._build_results((group,))
    key = (group.target.endpoint, "2")
    window.port_page._apply_result(sample(group, "2", link="Down"))
    assert window.port_matrix.model.total == 3
    assert window.port_matrix.model.port(key) is not None
    index = window.port_matrix.model.index_for(key)
    assert index.isValid()
    window.port_matrix._tile_clicked(index)
    assert window.port_page._selected_key() == key
    assert window.port_page._checked_port_keys == set()
    assert window.port_matrix.details.isVisible()
    assert "SW-TEST" in window.port_matrix.details_text.text()
    assert "2" in window.port_matrix.details_text.text()
    window.close()


def test_target_edit_clears_stale_matrix_observations(app: QApplication) -> None:
    window = NetworkManagerWindow()
    window.navigate(2)
    group = PoETarget(DeviceTarget("192.0.2.10", 22), ("1", "2"))
    window.port_page._build_results((group,))
    window.port_page._apply_result(sample(group, "1"))
    assert window.port_matrix.model.total == 2
    window.port_page._invalidate()
    assert window.port_matrix.model.total == 0
    assert window.port_matrix.model.shown == 0
    window.close()


def test_theme_buttons_apply_in_one_click_and_keep_settings(app: QApplication) -> None:
    settings = QSettings("sebia1993", "Aruba2930FNetworkManager")
    previous = settings.value("appearance/theme", "dark")
    window = NetworkManagerWindow()
    window.show()
    try:
        assert window.pages.count() == 4
        assert "설정" not in (button.text() for button in window._navigation_buttons)
        window.theme_light_button.click()
        assert window.theme_light_button.isChecked()
        assert not window.theme_dark_button.isChecked()
        assert selected_theme() == "light"
        window.theme_dark_button.click()
        assert not window.theme_light_button.isChecked()
        assert window.theme_dark_button.isChecked()
        assert selected_theme() == "dark"
    finally:
        window.close()
        value = str(previous)
        if value not in ("light", "dark"):
            value = "dark"
        set_theme(value)
