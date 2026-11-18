"""Five-page operator shell for Aruba 2930F backup and port operations.

The existing collectors, query workers and guarded control paths remain the
only execution engines. This module rearranges their Qt widgets, not their
SSH commands or authorization logic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from zipfile import BadZipFile

from openpyxl import load_workbook
from PySide6.QtCore import Qt, QTimer, QUrl, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .batch_port_control import BatchControlError, journal_states
from .gui import MainWindow
from .home_shortcuts import HomeShortcutButton
from .models import Credentials
from .session_credentials import SessionCredentialDialog, SessionCredentialStore
from .storage import default_output_directory
from .unified_port_gui import UnifiedPortManagementDialog

if TYPE_CHECKING:
    from .developer_inspector import DeveloperInspectorController
    from .gui import BackupServiceProtocol

_PAGE_TITLES = (
    ("홈", "오늘의 운영 작업을 선택하세요."),
    ("컨피그 백업", "running-config 수집과 스위치별 백업 결과 확인"),
    ("포트 통합 관리", "LLDP·PoE 전력 조회 후 해당 포트 제어 및 복구 확인"),
    ("작업 이력", "저장된 백업 및 포트 변경 이력"),
)


def _attach(layout: QVBoxLayout, value: QWidget | object) -> None:
    if isinstance(value, QWidget):
        layout.addWidget(value)
    else:
        # Original Qt layout items are detached before being reparented.
        from PySide6.QtWidgets import QLayout

        if not isinstance(value, QLayout):
            raise TypeError("Unsupported Qt layout item")
        layout.addLayout(value)


def _take_layout_with(layout: QVBoxLayout, button: QWidget) -> QLayout:
    from PySide6.QtWidgets import QLayout

    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item is None:
            continue
        child = item.layout()
        if isinstance(child, QLayout):
            for number in range(child.count()):
                inner = child.itemAt(number)
                if inner is not None and inner.widget() is button:
                    layout.takeAt(index)
                    return child
    raise RuntimeError("Existing operator controls could not be found")


def recent_backup_reports(root: Path, *, limit: int = 30) -> list[Path]:
    """Bounded enumeration of recent result workbooks; never crawl entire disks."""
    results: list[Path] = []
    try:
        dates = sorted(
            (directory for directory in root.iterdir() if directory.is_dir()),
            reverse=True,
        )[:20]
        for day in dates:
            runs = sorted(
                (directory for directory in day.iterdir() if directory.is_dir()),
                reverse=True,
            )[:30]
            for run in runs:
                candidate = run / "result.xlsx"
                if candidate.is_file():
                    results.append(candidate)
                if len(results) >= limit:
                    return results
    except OSError:
        return results
    return results


def report_device_rows(path: Path, *, limit: int = 200) -> list[tuple[str, str, str]]:
    """Read only the existing device report and never modify any saved result."""
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except (BadZipFile, OSError) as exc:
        raise ValueError("저장된 백업 보고서를 읽을 수 없습니다.") from exc
    try:
        if "Devices" not in workbook.sheetnames:
            return []
        sheet = workbook["Devices"]
        iterator = sheet.iter_rows(values_only=True)
        headers = tuple(str(value or "") for value in next(iterator, ()))
        if not {"IP", "Hostname", "Status"}.issubset(headers):
            return []
        positions = {key: headers.index(key) for key in ("IP", "Hostname", "Status")}
        output: list[tuple[str, str, str]] = []
        for number, values in enumerate(iterator):
            if number >= limit:
                break
            ip = str(values[positions["IP"]] or "")
            hostname = str(values[positions["Hostname"]] or "확인 불가")
            status = str(values[positions["Status"]] or "확인 불가")
            if ip:
                output.append((hostname, ip, status))
        return output
    finally:
        workbook.close()


class _EmbeddedPortManagementDialog(UnifiedPortManagementDialog):
    """Do not let Escape or a hidden QDialog Close action discard a workspace."""

    def __init__(self, parent: QWidget) -> None:
        self._shell_allows_close = False
        super().__init__(parent)

    def reject(self) -> None:
        if self._shell_allows_close:
            super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._shell_allows_close:
            super().closeEvent(event)
        else:
            event.ignore()


class NetworkManagerWindow(QMainWindow):
    """Single top-level window; the old GUI modules stay usable independently."""

    def __init__(
        self,
        *,
        service: BackupServiceProtocol | None = None,
        developer_inspector: DeveloperInspectorController | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("networkManagerWindow")
        self.setWindowTitle("Aruba 2930F · 네트워크 통합 관리")
        self.resize(1320, 840)
        self.setMinimumSize(820, 540)
        self._navigation_buttons: list[QPushButton] = []
        self._page_index = 0
        self._history_paths: dict[int, Path] = {}
        self._ssh_credentials = SessionCredentialStore()

        central = QWidget(self)
        root = QHBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        sidebar = QFrame(central)
        sidebar.setObjectName("networkManagerSidebar")
        sidebar.setFixedWidth(178)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(8, 12, 8, 12)
        sidebar_layout.setSpacing(8)
        brand = QLabel("Aruba 2930F\nNETWORK MANAGER", sidebar)
        brand.setObjectName("managerBrand")
        brand.setWordWrap(True)
        sidebar_layout.addWidget(brand)
        sidebar_layout.addSpacing(12)
        for index, (title, _) in enumerate(_PAGE_TITLES):
            button = QPushButton(title, sidebar)
            button.setCheckable(True)
            button.setObjectName(("navHome", "navBackup", "navPorts", "navHistory")[index])
            button.setMinimumHeight(42)
            button.clicked.connect(lambda checked=False, target=index: self.navigate(target))
            sidebar_layout.addWidget(button)
            self._navigation_buttons.append(button)
        sidebar_layout.addStretch(1)
        self.sidebar_status = QLabel("작업 상태 · 대기", sidebar)
        self.sidebar_status.setWordWrap(True)
        sidebar_layout.addWidget(self.sidebar_status)
        root.addWidget(sidebar)

        content = QWidget(central)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(4, 4, 4, 4)
        content_layout.setSpacing(8)
        self.page_title = QLabel(content)
        self.page_title.setObjectName("managerPageTitle")
        title_font = self.page_title.font()
        title_font.setPointSize(max(12, title_font.pointSize() + 4))
        title_font.setBold(True)
        self.page_title.setFont(title_font)
        content_layout.addWidget(self.page_title)
        self.page_description = QLabel(content)
        self.page_description.setWordWrap(True)
        content_layout.addWidget(self.page_description)

        # One place to set/revoke SSH credentials, shared across every action.
        account_row = QHBoxLayout()
        self.ssh_session_status = QLabel("SSH 계정 미설정", content)
        self.ssh_session_status.setObjectName("sshSessionStatus")
        self.ssh_session_status.setTextFormat(Qt.TextFormat.PlainText)
        account_row.addWidget(self.ssh_session_status, 1)
        self.ssh_session_button = QPushButton("SSH 계정 설정…", content)
        self.ssh_session_button.setObjectName("sshSessionConfigure")
        self.ssh_session_button.clicked.connect(self._configure_ssh_session)
        account_row.addWidget(self.ssh_session_button)
        self.ssh_session_clear_button = QPushButton("세션 해제", content)
        self.ssh_session_clear_button.setObjectName("sshSessionClear")
        self.ssh_session_clear_button.setEnabled(False)
        self.ssh_session_clear_button.clicked.connect(self._clear_ssh_session)
        account_row.addWidget(self.ssh_session_clear_button)
        content_layout.addLayout(account_row)

        self.pages = QStackedWidget(content)
        self.pages.setObjectName("networkManagerPages")
        content_layout.addWidget(self.pages, 1)
        root.addWidget(content, 1)
        self.setCentralWidget(central)

        self.home_page = self._create_home()
        self.pages.addWidget(self.home_page)
        self.backup_page = MainWindow(
            service=service,
            developer_inspector=developer_inspector,
            parent=self.pages,
        )
        self.backup_page.setWindowFlags(Qt.WindowType.Widget)
        self.backup_page.setMinimumSize(0, 0)
        self.backup_page.session_credential_provider = self._require_ssh_credentials
        self._layout_backup()
        self.pages.addWidget(self.backup_page)

        self.port_page = _EmbeddedPortManagementDialog(self.pages)
        self.port_page.setWindowFlags(Qt.WindowType.Widget)
        self.port_page.setMinimumSize(0, 0)
        self.port_page.session_credential_provider = self._require_ssh_credentials
        self._layout_port()
        self.pages.addWidget(self.port_page)

        self.history_page = self._create_history()
        self.pages.addWidget(self.history_page)
        self.navigate(0)

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(650)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start()
        self.setStyleSheet(
            "QFrame#networkManagerSidebar { border: 1px solid palette(mid); border-radius: 8px; }"
            " QPushButton:checked { background: palette(highlight);"
            " color: palette(highlighted-text); font-weight: 600; }"
            " QLabel#managerPageTitle { padding: 2px 0; }"
        )

    def _create_home(self) -> QWidget:
        page = QWidget(self.pages)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        heading = QLabel("작업 바로가기", page)
        layout.addWidget(heading)
        shortcuts = (
            ("컨피그 백업", "running-config 수집 / 결과", "backup", 1),
            ("포트 통합 관리", "LLDP·PoE 조회 / 포트 제어", "ports", 2),
            ("작업 이력", "백업·포트 변경 기록", "history", 3),
        )
        # Advanced SSH trust management and backup folder settings remain
        # accessible through the sidebar, not the daily-task shortcuts.
        cards = QGridLayout()
        cards.setSpacing(8)
        for position, (title, description, icon_name, index) in enumerate(shortcuts):
            button = HomeShortcutButton(title, description, icon_name, page)
            button.setObjectName(f"homeShortcut{index}")
            button.clicked.connect(lambda checked=False, target=index: self.navigate(target))
            cards.addWidget(button, 0, position)
            cards.setColumnStretch(position, 1)
        layout.addLayout(cards)
        layout.addSpacing(12)
        self.home_backup_status = QLabel("백업 작업 · 실행 전", page)
        self.home_port_status = QLabel("포트 작업 · 실행 전", page)
        for label in (self.home_backup_status, self.home_port_status):
            label.setWordWrap(True)
            layout.addWidget(label)
        layout.addStretch(1)
        return page

    def _layout_backup(self) -> None:
        backup = self.backup_page
        central = backup.centralWidget()
        assert central is not None
        root = central.layout()
        assert isinstance(root, QVBoxLayout)
        target = backup.ip_input.parentWidget()
        access = backup.username_input.parentWidget()
        options = backup.output_input.parentWidget()
        assert target is not None and access is not None and options is not None
        action = _take_layout_with(root, backup.start_button)
        for widget in (
            target,
            access,
            options,
            backup.status_label,
            backup.progress_bar,
            backup.result_table,
        ):
            root.removeWidget(widget)
        backup.poe_query_button.hide()
        backup.trust_keys_button.hide()
        backup.port_control_button.hide()

        settings = QWidget(central)
        settings_layout = QVBoxLayout(settings)
        settings_layout.setContentsMargins(7, 7, 7, 7)
        settings_layout.addWidget(QLabel("대상 장비 · 접속 · 저장 옵션", settings))
        for group in (target, access, options):
            settings_layout.addWidget(group)
        # The common SSH account is configured in the global header only.
        # Existing inputs stay connected for standalone GUI compatibility.
        access.hide()
        settings_layout.addStretch(1)
        scroll = QScrollArea(central)
        scroll.setObjectName("backupConfigurationScroll")
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(270)
        scroll.setWidget(settings)
        results = QWidget(central)
        results_layout = QVBoxLayout(results)
        results_layout.setContentsMargins(6, 6, 6, 6)
        results_layout.addWidget(QLabel("백업 결과 · IP + Hostname", results))
        _attach(results_layout, action)
        results_layout.addWidget(backup.status_label)
        results_layout.addWidget(backup.progress_bar)
        backup.result_table.setMinimumHeight(180)
        results_layout.addWidget(backup.result_table, 1)
        splitter = QSplitter(Qt.Orientation.Horizontal, central)
        splitter.setObjectName("backupMainSplitter")
        splitter.addWidget(scroll)
        splitter.addWidget(results)
        splitter.setChildrenCollapsible(False)
        splitter.setSizes([340, 740])
        root.addWidget(splitter, 1)

    def _layout_port(self) -> None:
        port = self.port_page
        root = port.layout()
        assert isinstance(root, QVBoxLayout)
        target_action = _take_layout_with(root, port.add_button)
        query_action = _take_layout_with(root, port.collect_button)
        # Reuse the existing credential form without replacing its live inputs.
        credential_form = None
        from PySide6.QtWidgets import QFormLayout

        for index in range(root.count()):
            item = root.itemAt(index)
            if item is None:
                continue
            candidate = item.layout()
            if isinstance(candidate, QFormLayout):
                credential_form = candidate
                root.takeAt(index)
                break
        if credential_form is None:
            raise RuntimeError("Could not relocate the existing SSH credential form")
        search_group = port.search_input.parentWidget()
        control_group = port.batch_progress.parentWidget()
        quick_bar = port.quick_action_bar
        assert search_group is not None and control_group is not None
        details_scroll = port.findChild(QScrollArea, "poeDetailsScroll")
        if details_scroll is None:
            raise RuntimeError("Missing existing per-port details widget")
        header_items = (
            port.targets_table,
            port.advanced_toggle,
            port.result_tree,
            details_scroll,
            port.progress_bar,
            port.status_label,
            search_group,
            control_group,
            quick_bar,
        )
        for widget in header_items:
            if widget is not None:
                root.removeWidget(widget)
        # Remove explanatory labels and the legacy Close button: navigation is
        # owned by the shell, with active operations protected by navigate().
        while root.count():
            item = root.takeAt(0)
            if item is None:
                continue
            leftover_widget = item.widget()
            if leftover_widget is not None:
                leftover_widget.hide()

        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(7)
        self.port_target_toggle = QToolButton(port)
        self.port_target_toggle.setObjectName("portTargetsToggle")
        self.port_target_toggle.setText("조회 대상 · SSH 접속 설정 접기")
        self.port_target_toggle.setCheckable(True)
        self.port_target_toggle.setChecked(True)
        self.port_target_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.port_target_toggle.setArrowType(Qt.ArrowType.DownArrow)
        # Excel IP/port paste stays accessible while target/SSH settings are collapsed.
        target_action.removeWidget(port.paste_button)
        port.paste_button.setText("IP·포트 2열 붙여넣기…")
        port.paste_button.setToolTip(
            "엑셀의 스위치 IP / 포트 두 열을 복사하고 붙여넣기 결과를 미리 확인하세요."
        )
        quick_actions = QHBoxLayout()
        quick_actions.addWidget(self.port_target_toggle, 1)
        self.port_input_focus_button = QToolButton(port)
        self.port_input_focus_button.setObjectName("portInputFocusToggle")
        self.port_input_focus_button.setCheckable(True)
        self.port_input_focus_button.setText("입력 크게 보기")
        self.port_input_focus_button.setToolTip(
            "대상 IP·포트 검토 시 결과 영역을 잠시 접어 입력 공간을 크게 사용합니다."
        )
        self.port_input_focus_button.toggled.connect(self._toggle_port_input_focus)
        quick_actions.addWidget(self.port_input_focus_button)
        quick_actions.addWidget(port.paste_button)
        root.addLayout(quick_actions)

        target_container = QWidget(port)
        target_layout = QVBoxLayout(target_container)
        target_layout.setContentsMargins(8, 8, 8, 8)
        # Replace the old 150px fixed table with a vertically growing editor.
        port.targets_table.setMinimumHeight(180)
        port.targets_table.setMaximumHeight(16777215)
        port.targets_table.setAlternatingRowColors(True)
        header = port.targets_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        port.targets_table.setColumnWidth(0, 210)
        port.targets_table.setColumnWidth(1, 90)
        port.targets_table.setColumnWidth(3, 160)
        target_layout.addWidget(port.targets_table, 1)
        target_layout.addWidget(port.advanced_toggle)
        _attach(target_layout, target_action)
        target_layout.addLayout(credential_form)
        # Standalone PoEQueryDialog retains the form, but this embedded view
        # never copies or exposes the shared secret in duplicate QLineEdits.
        for field in (
            port.username_input,
            port.password_input,
            port.enable_password_input,
        ):
            credential_form.setRowVisible(field, False)
        self.port_target_scroll = QScrollArea(port)
        self.port_target_scroll.setObjectName("portTargetSettings")
        self.port_target_scroll.setWidgetResizable(True)
        self.port_target_scroll.setMinimumHeight(160)
        self.port_target_scroll.setWidget(target_container)
        self.port_target_scroll.setVisible(True)
        self._last_port_target_height = 290
        self.port_target_toggle.toggled.connect(self._toggle_port_targets)

        _attach(root, query_action)
        for button in (
            port.collect_button,
            port.refresh_group_button,
            port.refresh_port_button,
        ):
            button.clicked.connect(lambda: self.port_input_focus_button.setChecked(False))
        # The operator requested that the per-port detail pane stop consuming
        # horizontal space. Keep the original read-only detail widget alive for
        # older standalone APIs, but never display it in this embedded view.
        details_scroll.hide()
        # A안: a resizable vertical input/result stack; focus mode temporarily
        # hides only the visual result pane, without discarding checked ports.
        self.port_result_panel = QWidget(port)
        self.port_result_panel.setObjectName("portResultPanel")
        result_layout = QVBoxLayout(self.port_result_panel)
        result_layout.setContentsMargins(0, 0, 0, 0)
        result_layout.setSpacing(6)
        result_layout.addWidget(search_group)
        port.result_tree.setColumnHidden(4, True)  # timestamps stay in CSV/history
        port.result_tree.setColumnWidth(0, 235)  # hostname and IP together
        port.result_tree.setColumnWidth(1, 190)  # LLDP AP
        port.result_tree.setColumnWidth(2, 105)  # watts
        port.result_tree.setColumnWidth(3, 220)  # port administration and link
        port.result_tree.setColumnWidth(5, 190)  # PoE setting and delivery
        result_layout.addWidget(port.result_tree, 1)
        self.port_input_result_splitter = QSplitter(Qt.Orientation.Vertical, port)
        self.port_input_result_splitter.setObjectName("portVerticalSplitter")
        # Preserve v0.8.0 integrations/tests that reference this splitter name.
        self.port_vertical_splitter = self.port_input_result_splitter
        self.port_input_result_splitter.addWidget(self.port_target_scroll)
        self.port_input_result_splitter.addWidget(self.port_result_panel)
        self.port_input_result_splitter.setChildrenCollapsible(False)
        self.port_input_result_splitter.setStretchFactor(0, 0)
        self.port_input_result_splitter.setStretchFactor(1, 1)
        self.port_input_result_splitter.setSizes([290, 520])
        root.addWidget(self.port_input_result_splitter, 1)

        self._layout_port_control_buttons(control_group)
        self.port_advanced_toggle = QToolButton(port)
        self.port_advanced_toggle.setObjectName("portAdvancedControlToggle")
        self.port_advanced_toggle.setText("고급 제어 · 기존 포트 Up/Down 및 계획 기능")
        self.port_advanced_toggle.setToolTip(
            "일반 PoE 작업은 아래 OFF/ON 두 버튼을 사용하세요. "
            "포트 Up/Down 및 기존 작업 계획은 필요할 때만 펼칩니다."
        )
        self.port_advanced_toggle.setCheckable(True)
        self.port_advanced_toggle.setChecked(False)
        self.port_advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.port_advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.port_advanced_toggle.toggled.connect(self._toggle_port_advanced)
        root.addWidget(self.port_advanced_toggle)
        self.port_advanced_scroll = QScrollArea(port)
        self.port_advanced_scroll.setObjectName("portAdvancedControls")
        self.port_advanced_scroll.setWidgetResizable(True)
        self.port_advanced_scroll.setMaximumHeight(270)
        self.port_advanced_scroll.setWidget(control_group)
        self.port_advanced_scroll.setVisible(False)
        root.addWidget(self.port_advanced_scroll)

        # Fixed OFF/ON actions, progress and stop remain visible at the bottom.
        root.addWidget(quick_bar)
        root.addWidget(port.progress_bar)
        root.addWidget(port.status_label)

    def _layout_port_control_buttons(self, group: QWidget) -> None:
        """Reflow wide legacy button rows into a readable narrow control rail."""
        panel = group.layout()
        assert isinstance(panel, QVBoxLayout)
        port = self.port_page
        first = _take_layout_with(panel, port.action_combo)
        second = _take_layout_with(panel, port.run_button)
        # Preserve all signal connections and control policies while moving
        # the actual widgets, not rebuilding new control actions.
        from PySide6.QtWidgets import QLayout

        assert isinstance(first, QLayout) and isinstance(second, QLayout)
        for widget in (port.action_combo, port.plan_button, port.single_button):
            first.removeWidget(widget)
        for widget in (port.run_button, port.cancel_batch_button, port.history_button):
            second.removeWidget(widget)
        rows = QGridLayout()
        rows.setSpacing(5)
        rows.addWidget(port.action_combo, 0, 0, 1, 2)
        rows.addWidget(port.plan_button, 1, 0)
        rows.addWidget(port.single_button, 1, 1)
        port.plan_button.setToolTip(port.plan_button.text())
        port.single_button.setToolTip(port.single_button.text())
        port.plan_button.setText("일괄 계획")
        port.single_button.setText("단일 제어…")
        panel.insertLayout(1, rows)
        actions = QGridLayout()
        actions.setSpacing(5)
        actions.addWidget(port.run_button, 0, 0, 1, 2)
        actions.addWidget(port.cancel_batch_button, 1, 0)
        actions.addWidget(port.history_button, 1, 1)
        for widget, label in (
            (port.run_button, "승인 후 실행…"),
            (port.cancel_batch_button, "작업 중지"),
            (port.history_button, "이력 확인…"),
        ):
            widget.setToolTip(widget.text())
            widget.setText(label)
        panel.insertLayout(3, actions)
        explanation = panel.itemAt(0)
        if explanation is not None:
            explanation_label = explanation.widget()
            if isinstance(explanation_label, QLabel):
                explanation_label.setToolTip(explanation_label.text())
                explanation_label.setText(
                    "포트 체크박스로 전체·스위치별 대상을 선택하세요. "
                    "일괄 OFF/Down은 LLDP로 확인된 AP-535만 허용합니다."
                )

    @Slot(bool)
    def _toggle_port_advanced(self, expanded: bool) -> None:
        self.port_advanced_scroll.setVisible(expanded)
        self.port_advanced_toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )

    @Slot(bool)
    def _toggle_port_targets(self, expanded: bool) -> None:
        if expanded:
            self.port_target_scroll.setVisible(True)
            self.port_input_result_splitter.setSizes([self._last_port_target_height, 520])
        else:
            current_height = self.port_target_scroll.height()
            if current_height >= 160:
                self._last_port_target_height = current_height
            # Leave input focus gracefully rather than hiding both panes.
            if self.port_input_focus_button.isChecked():
                self.port_input_focus_button.setChecked(False)
            self.port_target_scroll.setVisible(False)
        self.port_target_toggle.setText(
            "조회 대상 · SSH 접속 설정 접기" if expanded else "조회 대상 · SSH 접속 설정 펼치기"
        )
        self.port_target_toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )

    @Slot(bool)
    def _toggle_port_input_focus(self, focused: bool) -> None:
        if focused and not self.port_target_toggle.isChecked():
            self.port_target_toggle.setChecked(True)
        self.port_result_panel.setVisible(not focused)
        self.port_input_focus_button.setText("조회 결과 함께 보기" if focused else "입력 크게 보기")
        if not focused and self.port_target_toggle.isChecked():
            self.port_input_result_splitter.setSizes([self._last_port_target_height, 520])

    def _create_history(self) -> QWidget:
        page = QWidget(self.pages)
        layout = QVBoxLayout(page)
        toolbar = QHBoxLayout()
        button = QPushButton("저장된 작업 이력 새로고침", page)
        button.setObjectName("refreshHistory")
        button.clicked.connect(self.refresh_history)
        toolbar.addWidget(button)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)
        self.history_table = QTreeWidget(page)
        self.history_table.setObjectName("managerHistoryTable")
        self.history_table.setHeaderLabels(("유형", "Hostname", "스위치 IP", "상태", "저장 파일"))
        self.history_table.setAlternatingRowColors(True)
        self.history_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(self.history_table, 1)
        open_button = QPushButton("선택 이력 파일 위치 열기", page)
        open_button.setObjectName("openHistoryLocation")
        open_button.clicked.connect(self._open_history_location)
        layout.addWidget(open_button)
        note = QLabel(
            "기존 result.xlsx·일괄 제어 JSONL만 읽습니다. 설정 파일과 암호는 표시하지 않습니다.",
            page,
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        return page

    def refresh_history(self) -> None:
        self.history_table.clear()
        self._history_paths.clear()
        backup_root = Path(self.backup_page.output_input.text()).expanduser()
        files = recent_backup_reports(backup_root, limit=12)
        row_count = 0
        for report in files:
            try:
                records = report_device_rows(report, limit=100)
            except OSError, ValueError, KeyError:
                continue
            for hostname, ip, status in records:
                QTreeWidgetItem(
                    self.history_table, ["컨피그 백업", hostname, ip, status, str(report)]
                )
                self._history_paths[row_count] = report
                row_count += 1
        journal_root = default_output_directory() / "port-control" / "batch"
        dated_journals: list[tuple[int, Path]] = []
        try:
            for candidate in journal_root.glob("*.jsonl"):
                try:
                    dated_journals.append((candidate.stat().st_mtime_ns, candidate))
                except OSError:
                    continue
        except OSError:
            pass
        journals = [path for _, path in sorted(dated_journals, reverse=True)[:30]]
        for journal in journals:
            try:
                if journal.stat().st_size > 2_000_000:
                    continue
                with journal.open(encoding="utf-8") as handle:
                    record = json.loads(handle.readline())
                if not isinstance(record, dict) or not isinstance(record.get("items"), list):
                    continue
                states = journal_states(journal)
                for index, entry in enumerate(record["items"][:300]):
                    if not isinstance(entry, dict):
                        continue
                    endpoint = str(entry.get("endpoint", ""))
                    ip = endpoint.rsplit(":", 1)[0]
                    hostname = str(entry.get("hostname", "") or "확인 불가")
                    state = states.get(index, "queued")
                    item = QTreeWidgetItem(
                        self.history_table,
                        [
                            "포트 제어 이력",
                            hostname,
                            ip,
                            f"{record.get('action', '')} · {state}",
                            str(journal),
                        ],
                    )
                    item.setToolTip(3, "confirmed/unknown/queued states come from saved journal")
                    item.setToolTip(4, str(journal))
                    self._history_paths[row_count] = journal
                    row_count += 1
            except OSError, ValueError, UnicodeError, BatchControlError:
                continue
        if row_count == 0:
            QTreeWidgetItem(
                self.history_table,
                [
                    "기록 없음",
                    "—",
                    "—",
                    "저장된 작업 파일이 없습니다.",
                    "",
                ],
            )
        self.history_table.resizeColumnToContents(0)
        self.history_table.resizeColumnToContents(1)
        self.history_table.resizeColumnToContents(2)

    @Slot()
    def _open_history_location(self) -> None:
        current = self.history_table.currentItem()
        if current is None:
            return
        path = Path(current.text(4))
        if path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    @Slot()
    def _configure_ssh_session(self) -> bool:
        if self._is_busy():
            QMessageBox.information(
                self, "SSH 계정 변경 불가", "작업 진행 중에는 SSH 계정을 변경할 수 없습니다."
            )
            return False
        dialog = SessionCredentialDialog(self, username=self._ssh_credentials.username or "")
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            credentials = dialog.take_credentials()
            if credentials is None:
                return False
            was_configured = self._ssh_credentials.configured
            self._ssh_credentials.configure(
                credentials.username, credentials.password, credentials.enable_secret
            )
            if was_configured:
                # Account changes must invalidate any previously inspected
                # switch/port state before further destructive operations.
                self.port_page._invalidate()
            self._refresh_ssh_session_status()
            return True
        finally:
            dialog.deleteLater()

    @Slot()
    def _clear_ssh_session(self) -> None:
        if self._is_busy():
            QMessageBox.information(
                self, "SSH 세션 해제 불가", "작업 진행 중에는 SSH 세션을 해제할 수 없습니다."
            )
            return
        if self._ssh_credentials.configured:
            self._ssh_credentials.clear()
            self.port_page._invalidate()
        self._refresh_ssh_session_status()

    def _require_ssh_credentials(self) -> Credentials:
        if not self._ssh_credentials.configured and not self._configure_ssh_session():
            raise ValueError("SSH 공통 계정 설정을 취소했습니다.")
        return self._ssh_credentials.require()

    def _refresh_ssh_session_status(self) -> None:
        username = self._ssh_credentials.username
        self.ssh_session_status.setText(
            f"SSH 계정: {username} · 현재 실행에만 사용" if username else "SSH 계정 미설정"
        )
        self.ssh_session_button.setText("계정 변경…" if username else "SSH 계정 설정…")
        self.ssh_session_clear_button.setEnabled(username is not None and not self._is_busy())

    def _is_busy(self) -> bool:
        backup = self.backup_page
        return (
            backup._thread is not None
            or backup._cancel_thread_is_alive()
            or backup._run_finalize_pending
            or self.port_page.is_busy
        )

    def navigate(self, index: int) -> bool:
        if index < 0 or index >= self.pages.count():
            return False
        if self._is_busy() and index != self._page_index:
            QMessageBox.information(
                self,
                "작업 진행 중",
                "현재 작업이 실행 중입니다. 작업 상태와 안전한 종료를 확인한 후 이동하세요.",
            )
            for number, button in enumerate(self._navigation_buttons):
                button.setChecked(number == self._page_index)
            return False
        if index == 2 and self._page_index != 2:
            # Carry only target IPs and keep separately confirmed SSH defaults.
            # Never overwrite a saved port target profile or existing result set.
            port = self.port_page
            if not port._has_target_text():
                ips = tuple(
                    line.strip()
                    for line in self.backup_page.ip_input.toPlainText().splitlines()
                    if line.strip()
                )
                if ips:
                    port._replace_targets_rows(tuple((ip, "22", "", "") for ip in ips))
            # Port actions read one common session; never copy passwords
            # between the hidden legacy backup/port input widgets.
            if not port.isVisible():
                port.show()
        if index == 3:
            self.refresh_history()
        self._page_index = index
        self.pages.setCurrentIndex(index)
        self.page_title.setText(_PAGE_TITLES[index][0])
        self.page_description.setText(_PAGE_TITLES[index][1])
        for number, button in enumerate(self._navigation_buttons):
            button.setChecked(number == index)
        self._refresh_status()
        return True

    @Slot()
    def _refresh_status(self) -> None:
        backup = self.backup_page
        self.home_backup_status.setText("컨피그 백업 · " + backup.status_label.text())
        self.home_port_status.setText("포트 통합 관리 · " + self.port_page.status_label.text())
        busy = self._is_busy()
        self.ssh_session_button.setEnabled(not busy)
        self.ssh_session_clear_button.setEnabled(self._ssh_credentials.configured and not busy)
        if busy:
            self.sidebar_status.setText("작업 진행 중 · 현재 화면에서 확인")
        else:
            self.sidebar_status.setText("작업 상태 · 대기")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._is_busy():
            QMessageBox.warning(
                self,
                "작업 진행 중",
                "실행 중인 작업을 먼저 중지하고 종료를 확인하세요. "
                "명령 결과가 불확실하면 자동 재실행하지 않습니다.",
            )
            event.ignore()
            return
        self._status_timer.stop()
        # The old controllers clear sensitive inputs in their own close paths.
        self.backup_page.close()
        self.port_page._shell_allows_close = True
        self.port_page.close()
        self._ssh_credentials.clear()
        self._refresh_ssh_session_status()
        super().closeEvent(event)
