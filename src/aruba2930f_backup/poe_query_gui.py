"""Read-only grouped PoE collection with session-only credentials."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Protocol, cast

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .models import Credentials, HostKeyCheck
from .poe_power import (
    AP535_REFERENCE_URL,
    LLDPObservation,
    LLDPStatus,
    PowerEvidence,
)
from .poe_query_models import PoEQueryResult, PoETarget, QueryStatus
from .poe_query_report import write_query_csv
from .poe_targets import (
    load_target_profile,
    make_target,
    merge_targets,
    parse_pasted_targets,
    save_target_profile,
)


class PoEQueryServiceProtocol(Protocol):
    def collect(
        self,
        groups: Sequence[PoETarget],
        credentials: Credentials,
        *,
        approve_host_key: Callable[[HostKeyCheck], bool],
        on_result: Callable[[PoEQueryResult], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[PoEQueryResult, ...]: ...


@dataclass(eq=False)
class _Approval:
    check: HostKeyCheck
    completed: threading.Event = field(default_factory=threading.Event)
    accepted: bool = False


class _QueryWorker(QObject):
    result = Signal(object)
    approval_requested = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        service: PoEQueryServiceProtocol,
        groups: tuple[PoETarget, ...],
        credentials: Credentials,
    ) -> None:
        super().__init__()
        self.service = service
        self.groups = groups
        self.credentials: Credentials | None = credentials
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._approvals: set[_Approval] = set()

    def cancel(self) -> None:
        self.cancel_event.set()
        with self._lock:
            for approval in self._approvals:
                approval.accepted = False
                approval.completed.set()

    def request_approval(self, check: HostKeyCheck) -> bool:
        approval = _Approval(check)
        with self._lock:
            if self.cancel_event.is_set():
                return False
            self._approvals.add(approval)
        self.approval_requested.emit(approval)
        while not approval.completed.wait(0.05):
            if self.cancel_event.is_set():
                break
        with self._lock:
            self._approvals.discard(approval)
        return approval.accepted and not self.cancel_event.is_set()

    @Slot()
    def run(self) -> None:
        try:
            if self.credentials is None:
                raise ValueError("Missing credentials")
            results = self.service.collect(
                self.groups,
                self.credentials,
                approve_host_key=self.request_approval,
                on_result=self.result.emit,
                cancel_event=self.cancel_event,
            )
            # Also support services which return results without calling on_result.
            for result in results:
                self.result.emit(result)
        except Exception:
            self.failed.emit("조회를 완료하지 못했습니다. 대상과 접속 상태를 확인하세요.")
        finally:
            self.credentials = None
            self.finished.emit()


def _local_time(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except ValueError:
        return "확인 안 됨"


def _watts(value: Decimal | None) -> str:
    return "확인 안 됨" if value is None else f"{value:g} W"


def _tooltip(value: str) -> str:
    # Qt tooltips auto-detect rich text; escape every device-derived string.
    return "<qt>" + escape(value).replace("\n", "<br>") + "</qt>"


def _power_details(power: PowerEvidence) -> str:
    return (
        f"요청 {_watts(power.requested_watts)} · 할당 {_watts(power.allocated_watts)}"
        f" · Class {power.power_class} · PLC {power.plc_class}/{power.plc_type}"
        f" · DLC {power.dlc_class}/{power.dlc_type} · 할당 방식 {power.allocation_method}"
        f" · 장치 유형 {power.device_type} · 할당 필드 {power.allocation_field}"
        f" · 해석 문제 {', '.join(power.issues) or '없음'}"
    )


class PoEQueryDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        service: PoEQueryServiceProtocol | None = None,
        initial_ips: Sequence[str] = (),
        ssh_port: int = 22,
        username: str = "",
        password: str = "",
        enable_password: str = "",
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._thread: QThread | None = None
        self._worker: _QueryWorker | None = None
        self._approval_dialog: QDialog | None = None
        self._approval_queue: list[_Approval] = []
        self._close_when_done = False
        self._groups: tuple[PoETarget, ...] = ()
        self._results: dict[tuple[str, str], PoEQueryResult] = {}
        self._items: dict[tuple[str, str], QTreeWidgetItem] = {}
        self._parents: dict[str, QTreeWidgetItem] = {}
        self._pending: set[tuple[str, str]] = set()
        self._run_total = 0
        self._render_queue: list[PoEQueryResult] = []
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(35)
        self._render_timer.timeout.connect(self._flush_result_queue)
        self.setWindowTitle("Aruba 2930F · 포트 전력 조회")
        self.resize(1080, 800)
        self.setMinimumSize(820, 650)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "스위치별 포트 목록을 입력하고 전체 조회를 누르세요. 전력은 조회 시점의 값입니다.", self
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.targets_table = QTableWidget(0, 4, self)
        self.targets_table.setObjectName("poeTargetsTable")
        self.targets_table.setHorizontalHeaderLabels(
            ["SW IP (IPv4)", "SSH 포트", "인터페이스 목록", "이름 (선택)"]
        )
        self.targets_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.targets_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.targets_table.setMinimumHeight(130)
        self.targets_table.setMaximumHeight(150)
        layout.addWidget(self.targets_table)
        self.advanced_toggle = QCheckBox("고급 설정 · SSH 포트 / 별칭", self)
        self.advanced_toggle.setObjectName("poeAdvancedTargets")
        self.advanced_toggle.toggled.connect(self._show_advanced)
        self._show_advanced(False)
        layout.addWidget(self.advanced_toggle)
        actions = QHBoxLayout()
        self.add_button = self._button("스위치 추가", "poeAddTarget", self._add_row, actions)
        self.remove_button = self._button("행 삭제", "poeRemoveTarget", self._remove_rows, actions)
        self.paste_button = self._button(
            "표 붙여넣기…", "poePasteTargets", self._paste_dialog, actions
        )
        self.save_button = self._button("목록 저장…", "poeSaveTargets", self._save_dialog, actions)
        self.load_button = self._button(
            "목록 불러오기…", "poeLoadTargets", self._load_dialog, actions
        )
        layout.addLayout(actions)
        note = QLabel(
            "포트는 쉼표로 구분: 24,1/24 · 범위/all 제외 · IP / 포트 목록 두 열 붙여넣기 (SSH 22). 기존 3~4열과 고급 설정도 지원합니다.",
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.username_input = QLineEdit(username, self)
        self.username_input.setObjectName("poeUsername")
        self.password_input = QLineEdit(password, self)
        self.password_input.setObjectName("poePassword")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.enable_password_input = QLineEdit(enable_password, self)
        self.enable_password_input.setObjectName("poeEnablePassword")
        self.enable_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("공통 사용자 이름", self.username_input)
        form.addRow("비밀번호", self.password_input)
        form.addRow("Enable 암호 (선택)", self.enable_password_input)
        layout.addLayout(form)
        actions = QHBoxLayout()
        self.collect_button = self._button("전체 조회", "poeCollectAll", self._collect_all, actions)
        self.refresh_group_button = self._button(
            "선택 스위치 재조회", "poeRefreshGroup", self._refresh_group, actions
        )
        self.refresh_port_button = self._button(
            "선택 포트 재조회", "poeRefreshPort", self._refresh_port, actions
        )
        self.stop_button = self._button("조회 중지", "poeStop", self.stop, actions)
        self.export_button = self._button("CSV 저장…", "poeExportCsv", self._export_dialog, actions)
        layout.addLayout(actions)
        self.result_tree = QTreeWidget(self)
        self.result_tree.setObjectName("poeResultTree")
        self.result_tree.setHeaderLabels(
            [
                "스위치 / 포트",
                "AP 이름 (LLDP)",
                "현재 전력",
                "PoE 판정",
                "조회 시각 (로컬)",
                "조회 상태",
            ]
        )
        self.result_tree.setColumnWidth(0, 230)
        self.result_tree.setColumnWidth(1, 180)
        self.result_tree.setColumnWidth(2, 95)
        self.result_tree.setColumnWidth(3, 135)
        self.result_tree.setColumnWidth(4, 185)
        self.result_tree.setAlternatingRowColors(True)
        self.result_tree.setMinimumHeight(160)
        self.result_tree.currentItemChanged.connect(self._selection_changed)
        layout.addWidget(self.result_tree, 2)
        self.details_label = QLabel(
            "포트를 선택하면 관리 설정과 모델 등 상세 정보를 표시합니다.", self
        )
        self.details_label.setObjectName("poeResultDetails")
        self.details_label.setTextFormat(Qt.TextFormat.PlainText)
        self.details_label.setWordWrap(True)
        self.details_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        details_scroll = QScrollArea(self)
        details_scroll.setObjectName("poeDetailsScroll")
        details_scroll.setWidgetResizable(True)
        details_scroll.setMinimumHeight(70)
        details_scroll.setMaximumHeight(140)
        details_scroll.setWidget(self.details_label)
        layout.addWidget(details_scroll)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setObjectName("poeProgress")
        layout.addWidget(self.progress_bar)
        self.status_label = QLabel("조회 대상 목록에는 계정이나 암호를 저장하지 않습니다.", self)
        self.status_label.setObjectName("poeStatus")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        self.close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        self.close_button.setText("닫기")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._replace_targets_rows(
            tuple((ip, str(ssh_port), "", "") for ip in initial_ips)
            or (("", str(ssh_port), "", ""),)
        )
        self.targets_table.itemChanged.connect(self._invalidate)
        self._set_busy(False)

    def _button(
        self, text: str, name: str, slot: Callable[[], None], layout: QHBoxLayout
    ) -> QPushButton:
        button = QPushButton(text, self)
        button.setObjectName(name)
        button.setAutoDefault(False)
        button.clicked.connect(slot)
        layout.addWidget(button)
        return button

    @property
    def is_busy(self) -> bool:
        return self._thread is not None

    def _replace_targets_rows(self, rows: Sequence[Sequence[str]]) -> None:
        self.targets_table.blockSignals(True)
        self.targets_table.setRowCount(len(rows))
        for row, cells in enumerate(rows):
            for column, value in enumerate(cells):
                self.targets_table.setItem(row, column, QTableWidgetItem(value))
        self.targets_table.blockSignals(False)
        if any(len(cells) > 1 and cells[1].strip() not in ("", "22") for cells in rows):
            self.advanced_toggle.setChecked(True)
        self._invalidate()

    @Slot(bool)
    def _show_advanced(self, visible: bool) -> None:
        for column in (1, 3):
            self.targets_table.setColumnHidden(column, not visible)

    def _replace_targets(self, groups: Sequence[PoETarget]) -> None:
        self._replace_targets_rows(
            tuple(
                (g.target.ip, str(g.target.port), ",".join(g.interfaces), g.label) for g in groups
            )
        )

    def _validated_targets(self) -> tuple[PoETarget, ...]:
        groups = []
        for row in range(self.targets_table.rowCount()):
            values = [self._cell_text(row, column).strip() for column in range(4)]
            # A newly added empty row can be ignored, but incomplete nonempty rows cannot.
            if not any(values[index] for index in (0, 2, 3)) and values[1] in ("", "22"):
                continue
            try:
                groups.append(make_target(*values))
            except ValueError as exc:
                raise ValueError(f"{row + 1}행: {exc}") from exc
        if not groups:
            raise ValueError("조회할 스위치와 포트를 입력하세요.")
        return merge_targets(groups)

    @Slot()
    def _invalidate(self) -> None:
        self._groups = ()
        self._pending.clear()
        self._run_total = 0
        self._results.clear()
        self._items.clear()
        self._parents.clear()
        self.result_tree.clear()
        self.details_label.setText("대상이 변경되었습니다. 전체 조회를 실행하세요.")
        self.progress_bar.setValue(0)
        self._set_busy(self.is_busy)

    @Slot()
    def _add_row(self) -> None:
        row = self.targets_table.rowCount()
        self.targets_table.insertRow(row)
        for column, text in enumerate(("", "22", "", "")):
            self.targets_table.setItem(row, column, QTableWidgetItem(text))
        self.targets_table.setCurrentCell(row, 0)
        self._invalidate()

    @Slot()
    def _remove_rows(self) -> None:
        for row in sorted(
            {item.row() for item in self.targets_table.selectedItems()}, reverse=True
        ):
            self.targets_table.removeRow(row)
        self._invalidate()

    def paste_targets(self, text: str) -> None:
        parsed = parse_pasted_targets(text)
        # Merge only after both the pasted block and current rows validate.
        existing = self._validated_targets() if self._has_target_text() else ()
        self._replace_targets(merge_targets((*existing, *parsed)))

    def _cell_text(self, row: int, column: int) -> str:
        item = self.targets_table.item(row, column)
        return item.text() if item is not None else ""

    def _has_target_text(self) -> bool:
        return any(
            self._cell_text(row, column).strip()
            for row in range(self.targets_table.rowCount())
            for column in (0, 2, 3)
        )

    @Slot()
    def _paste_dialog(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("엑셀 / CSV 표 붙여넣기")
        dialog.resize(700, 330)
        layout = QVBoxLayout(dialog)
        layout.addWidget(
            QLabel(
                "IP / 인터페이스 목록 두 열의 표를 붙여넣으세요 (SSH 22).\n기존 IP / SSH 포트 / 인터페이스 목록 / 이름(선택) 3~4열도 지원합니다.\n탭 구분 또는 CSV를 받습니다. 쉼표가 있는 CSV 포트 목록은 큰따옴표로 감싸세요.",
                dialog,
            )
        )
        text = QPlainTextEdit(dialog)
        text.setObjectName("poePasteText")
        layout.addWidget(text)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, dialog
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.paste_targets(text.toPlainText())
            except ValueError as exc:
                QMessageBox.warning(self, "표 입력 확인", str(exc))

    def save_profile(self, path: Path) -> None:
        save_target_profile(path, self._validated_targets())

    def load_profile(self, path: Path) -> bool:
        groups = load_target_profile(path)
        if (
            self._has_target_text()
            and QMessageBox.question(
                self,
                "조회 대상 바꾸기",
                "현재 대상 목록을 파일의 목록으로 바꾸시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return False
        self._replace_targets(groups)
        self.status_label.setText("목록을 불러왔습니다. 접속하려면 전체 조회를 누르세요.")
        return True

    @Slot()
    def _save_dialog(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "조회 대상 목록 저장", "poe-targets.json", "JSON (*.json)"
        )
        if path:
            try:
                self.save_profile(Path(path))
                self.status_label.setText("조회 대상 목록을 저장했습니다.")
            except (ValueError, OSError) as exc:
                QMessageBox.warning(self, "목록 저장 실패", str(exc))

    @Slot()
    def _load_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "조회 대상 목록 불러오기", "", "JSON (*.json)")
        if path:
            try:
                self.load_profile(Path(path))
            except (ValueError, OSError) as exc:
                QMessageBox.warning(self, "목록 불러오기 실패", str(exc))

    def export_results(self, path: Path) -> None:
        results = tuple(self._results[key] for key in self._items if key in self._results)
        write_query_csv(path, results)

    @Slot()
    def _export_dialog(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "조회 결과 CSV 저장", "poe-results.csv", "CSV (*.csv)"
        )
        if path:
            try:
                self.export_results(Path(path))
                self.status_label.setText("조회 결과를 CSV로 저장했습니다.")
            except (ValueError, OSError) as exc:
                QMessageBox.warning(self, "CSV 저장 실패", str(exc))

    def _build_results(self, groups: tuple[PoETarget, ...]) -> None:
        self._render_timer.stop()
        self._render_queue.clear()
        self.result_tree.clear()
        self._items.clear()
        self._parents.clear()
        self._results.clear()
        self._groups = groups
        for group in groups:
            parent = QTreeWidgetItem(self.result_tree)
            parent.setData(0, Qt.ItemDataRole.UserRole, (group.target.endpoint, ""))
            self._parents[group.target.endpoint] = parent
            for interface in group.interfaces:
                item = QTreeWidgetItem(parent, [interface])
                key = (group.target.endpoint, interface)
                item.setData(0, Qt.ItemDataRole.UserRole, key)
                self._items[key] = item
            parent.setExpanded(True)
            self._update_group(group)

    @Slot()
    def _collect_all(self) -> None:
        if self.is_busy:
            return
        try:
            groups = self._validated_targets()
            credentials = self._credentials()
        except ValueError as exc:
            QMessageBox.warning(self, "입력 확인", str(exc))
            return
        if groups != self._groups:
            self._build_results(groups)
        self._start(groups, credentials)

    def _credentials(self) -> Credentials:
        return Credentials(
            self.username_input.text().strip(),
            self.password_input.text(),
            self.enable_password_input.text() or None,
        )

    def _selected_key(self) -> tuple[str, str] | None:
        item = self.result_tree.currentItem()
        return (
            cast(tuple[str, str] | None, item.data(0, Qt.ItemDataRole.UserRole)) if item else None
        )

    @Slot()
    def _refresh_group(self) -> None:
        key = self._selected_key()
        if key:
            self._refresh(tuple(group for group in self._groups if group.target.endpoint == key[0]))

    @Slot()
    def _refresh_port(self) -> None:
        key = self._selected_key()
        if key and key[1]:
            self._refresh(
                tuple(
                    PoETarget(group.target, (key[1],), group.label)
                    for group in self._groups
                    if group.target.endpoint == key[0]
                )
            )

    def _refresh(self, groups: tuple[PoETarget, ...]) -> None:
        if self.is_busy or not groups:
            return
        try:
            self._start(groups, self._credentials())
        except ValueError as exc:
            QMessageBox.warning(self, "입력 확인", str(exc))

    def _start(self, groups: tuple[PoETarget, ...], credentials: Credentials) -> None:
        if self._service is None:
            from .poe_query import PoEQueryService

            self._service = PoEQueryService()
        self._pending = {
            (group.target.endpoint, interface) for group in groups for interface in group.interfaces
        }
        self._run_total = len(self._pending)
        for key in self._pending:
            self._results.pop(key, None)
            item = self._items[key]
            for column in range(1, 6):
                item.setText(column, "")
                item.setToolTip(column, "")
            item.setText(5, "대기")
        for group in self._groups:
            self._update_group(group)
        self.details_label.setText("조회 중입니다. 이전 측정값은 재조회 대상에서 지웠습니다.")
        self.progress_bar.setValue(0)
        thread = QThread(self)
        worker = _QueryWorker(self._service, groups, credentials)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.result.connect(self._on_result)
        worker.approval_requested.connect(self._approve_key)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._thread_finished)
        self._thread, self._worker = thread, worker
        self._set_busy(True)
        self.status_label.setText(f"조회 중: 0/{self._run_total}개 포트 · 최대 3대 동시 접속")
        thread.start()

    @Slot(object)
    def _approve_key(self, value: object) -> None:
        from .gui import HostKeyApprovalDialog

        approval = cast(_Approval, value)
        worker = self._worker
        if worker is None or worker.cancel_event.is_set() or approval.completed.is_set():
            approval.completed.set()
            return
        if self._approval_dialog is not None:
            self._approval_queue.append(approval)
            return
        dialog = HostKeyApprovalDialog([approval.check], self)
        self._approval_dialog = dialog
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            approval.accepted = (
                accepted and dialog.approval_allowed and not worker.cancel_event.is_set()
            )
        finally:
            approval.completed.set()
            self._approval_dialog = None
            dialog.deleteLater()
            if self._approval_queue:
                next_approval = self._approval_queue.pop(0)
                QTimer.singleShot(0, lambda: self._approve_key(next_approval))

    @Slot(object)
    def _on_result(self, value: object) -> None:
        # Large jobs deliver up to two observations per port. Coalescing GUI
        # writes keeps the main event loop responsive even at 14,400 ports.
        if self._run_total > 750:
            self._render_queue.append(cast(PoEQueryResult, value))
            if not self._render_timer.isActive():
                self._render_timer.start()
            return
        self._apply_result(value)

    @Slot()
    def _flush_result_queue(self) -> None:
        batch = self._render_queue[:120]
        del self._render_queue[:120]
        for result in batch:
            self._apply_result(result)
        if not self._render_queue:
            self._render_timer.stop()

    def _apply_result(self, value: object) -> None:
        result = cast(PoEQueryResult, value)
        key = (result.group.target.endpoint, result.interface)
        if key not in self._items:
            return
        previous = self._results.get(key)
        if previous is not None and previous.complete and not result.complete:
            return
        self._results[key] = result
        if result.complete:
            self._pending.discard(key)
        item = self._items[key]
        snapshot = result.snapshot
        lldp = result.lldp
        if lldp is None:
            ap_name = "확인 안 됨" if result.complete else "조회 중"
        elif lldp.status == LLDPStatus.OK:
            ap_name = " / ".join(n.system_name or "없음" for n in lldp.neighbors) or "없음"
        else:
            ap_name = {
                LLDPStatus.PENDING: "조회 중",
                LLDPStatus.FAILED: "조회 실패",
                LLDPStatus.CANCELLED: "조회 취소",
            }[lldp.status]
        item.setText(1, ap_name)
        item.setText(
            2,
            "확인 안 됨" if snapshot is None or snapshot.watts is None else f"{snapshot.watts:g} W",
        )
        assessment = result.assessment
        item.setText(3, assessment.label if assessment else "확인 필요")
        item.setToolTip(
            3, _tooltip(assessment.reason if assessment else "전력 판정 근거를 받지 못했습니다.")
        )
        item.setText(4, _local_time(snapshot.observed_at if snapshot else result.finished_at))
        status = {
            QueryStatus.OK: "완료",
            QueryStatus.UNSUPPORTED: "미지원",
            QueryStatus.FAILED: "실패",
            QueryStatus.CANCELLED: "취소",
        }[result.status]
        if not result.complete:
            status = "PoE 수집 · LLDP 조회 중"
        elif lldp is not None and lldp.status in (LLDPStatus.FAILED, LLDPStatus.CANCELLED):
            status += " · LLDP " + ("실패" if lldp.status == LLDPStatus.FAILED else "취소")
        item.setText(5, status)
        item.setToolTip(
            5, _tooltip(" · ".join(filter(None, (result.message, lldp.message if lldp else ""))))
        )
        for group in self._groups:
            if group.target.endpoint == key[0]:
                self._update_group(group)
                break
        completed = self._run_total - len(self._pending)
        if self._run_total:
            self.progress_bar.setValue(round(completed * 100 / self._run_total))
        self.status_label.setText(f"조회 중: {completed}/{self._run_total}개 포트 처리")
        if self._selected_key() == key:
            self._selection_changed()

    def _update_group(self, group: PoETarget) -> None:
        results = [
            self._results[(group.target.endpoint, p)]
            for p in group.interfaces
            if (group.target.endpoint, p) in self._results
        ]
        hostname = next(
            (r.identity.hostname for r in results if r.identity and r.identity.hostname),
            next((r.snapshot.hostname for r in results if r.snapshot), ""),
        )
        name = (
            f"{hostname} · {group.target.ip}"
            if hostname
            else f"확인 전 · {group.target.ip}"
        )
        if group.target.port != 22:
            name += f" (SSH {group.target.port})"
        item = self._parents[group.target.endpoint]
        item.setText(0, name)
        completed = sum(r.complete for r in results)
        failures = sum(
            r.complete
            and (
                r.status in (QueryStatus.FAILED, QueryStatus.UNSUPPORTED, QueryStatus.CANCELLED)
                or (
                    r.lldp is not None
                    and r.lldp.status in (LLDPStatus.FAILED, LLDPStatus.CANCELLED)
                )
            )
            for r in results
        )
        item.setText(5, f"처리 {completed}/{len(group.interfaces)} · 실패/취소 {failures}")
        item.setToolTip(
            0, _tooltip(f"{name}\nSSH: {group.target.endpoint}\n별칭: {group.label or '없음'}")
        )
        item.setToolTip(5, item.text(5))

    @Slot()
    def _selection_changed(self) -> None:
        key = self._selected_key()
        result = self._results.get(key) if key else None
        if result:
            snapshot = result.snapshot
            details = f"{result.group.target.endpoint} / {result.interface} · {result.message}"
            details += f"\n별칭: {result.group.label or '없음'}"
            identity = result.identity
            if identity:
                details += f"\n장비명: {identity.hostname or '확인 안 됨'} · 스위치 모델: {identity.model} · SKU: {identity.sku or '확인 안 됨'} · 소프트웨어: {identity.software_version or '확인 안 됨'}"
            if snapshot:
                poe_setting = (
                    "확인 안 됨"
                    if snapshot.poe_enabled is None
                    else ("켜짐" if snapshot.poe_enabled else "꺼짐")
                )
                details += f"\n장비명: {snapshot.hostname} · 스위치 모델: {snapshot.model} · 관리 설정: {'켜짐' if snapshot.enabled else '꺼짐'}"
                details += f"\n링크: {snapshot.link_status} · PoE 설정: {poe_setting} · 공급 상태: {snapshot.poe_status}"
                details += f"\n실측 전력: {'확인 안 됨' if snapshot.watts is None else f'{snapshot.watts:g} W'} · 측정: {_local_time(snapshot.observed_at)}"
            details += f"\nPoE 수집 시각: {_local_time(result.poe_finished_at) or '확인 안 됨'} · 레코드 완료: {_local_time(result.finished_at) or '대기'}"
            assessment = result.assessment
            if assessment:
                details += f"\nPoE 판정: {assessment.label} · {assessment.reason}"
                details += f"\n판정 모델: {assessment.model} · 모델 출처: {assessment.model_source}"
                details += f"\n판정 요청: {_watts(assessment.requested_watts)} · 할당: {_watts(assessment.allocated_watts)} · 협상: {assessment.negotiation or '확인 안 됨'}"
                details += f"\n규칙 기준: {assessment.reference_id} · {AP535_REFERENCE_URL}"
            if result.power_evidence:
                details += "\nPoE 협상 근거: " + _power_details(result.power_evidence)
            lldp = result.lldp
            if lldp:
                details += f"\nLLDP: {lldp.status.value} · {lldp.message} · 관측 시각: {_local_time(lldp.observed_at) or '확인 안 됨'}"
                if lldp.error_code:
                    details += f" · 오류: {lldp.error_code.value}"
                for index, neighbor in enumerate(lldp.neighbors, 1):
                    details += f"\n이웃 {index}: 이름 {neighbor.system_name or '없음'} · 모델 {neighbor.model or '확인 안 됨'} · 설명 {neighbor.system_description}"
                    details += f"\nChassis: {neighbor.chassis_type} / {neighbor.chassis_id} · 상대 포트: {neighbor.port_type} / {neighbor.port_id} · 포트 설명: {neighbor.port_description} · 관리 주소: {', '.join(neighbor.management_addresses) or '없음'}"
                    if neighbor.power:
                        details += "\nLLDP 협상 근거: " + _power_details(neighbor.power)
            if result.error_code:
                details += f"\nPoE 오류: {result.error_code.value}"
            self.details_label.setText(details)
        elif key:
            self.details_label.setText("선택한 대상의 새 조회 결과를 기다리는 중입니다.")
        self._set_busy(self.is_busy)

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self.status_label.setText(message)

    @Slot()
    def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.stop_button.setEnabled(False)
            self.status_label.setText("중지 요청 중… 현재 제한된 작업이 반환되면 연결을 닫습니다.")
        for approval in self._approval_queue:
            approval.completed.set()
        self._approval_queue.clear()
        if self._approval_dialog is not None:
            self._approval_dialog.reject()

    @Slot()
    def _thread_finished(self) -> None:
        # QThread.finished precedes native thread-local/deferred-delete cleanup.
        # Keep both Python wrappers alive until that cleanup has completed.
        thread = self._thread
        if thread is not None and not thread.wait(100):
            QTimer.singleShot(20, self._thread_finished)
            return
        self._render_timer.stop()
        while self._render_queue:
            self._flush_result_queue()
        cancelled = self._worker is not None and self._worker.cancel_event.is_set()
        # A broken service must never leave pending rows looking measured.
        for endpoint, interface in tuple(self._pending):
            group = next(g for g in self._groups if g.target.endpoint == endpoint)
            now = datetime.now().astimezone().isoformat()
            message = "조회가 중지되었습니다." if cancelled else "조회 결과를 받지 못했습니다."
            previous = self._results.get((endpoint, interface))
            if previous is not None:
                from .poe_power import assess_ap535_power
                from .port_control import PoEState

                lldp = LLDPObservation(
                    LLDPStatus.CANCELLED if cancelled else LLDPStatus.FAILED,
                    message=message,
                    observed_at=now,
                )
                snapshot = previous.snapshot
                poe = (
                    PoEState(snapshot.poe_enabled, snapshot.poe_status, snapshot.watts)
                    if snapshot
                    else None
                )
                result = replace(
                    previous,
                    lldp=lldp,
                    complete=True,
                    finished_at=now,
                    assessment=assess_ap535_power(poe, previous.power_evidence, lldp),
                )
            else:
                result = PoEQueryResult(
                    group,
                    interface,
                    QueryStatus.CANCELLED if cancelled else QueryStatus.FAILED,
                    message=message,
                    finished_at=now,
                )
            self._apply_result(result)

        self._thread = None
        self._worker = None
        if thread is not None:
            thread.deleteLater()
        self.status_label.setText(
            f"{'조회 중지' if cancelled else '조회 완료'} · {len(self._results)}개 포트 결과 · 각 행의 시각을 확인하세요."
        )
        self._set_busy(False)
        if self._close_when_done:
            self.reject()

    def _set_busy(self, busy: bool) -> None:
        for widget in (
            self.targets_table,
            self.advanced_toggle,
            self.add_button,
            self.remove_button,
            self.paste_button,
            self.save_button,
            self.load_button,
            self.username_input,
            self.password_input,
            self.enable_password_input,
            self.collect_button,
        ):
            widget.setEnabled(not busy)
        key = self._selected_key()
        self.refresh_group_button.setEnabled(not busy and key is not None)
        self.refresh_port_button.setEnabled(not busy and key is not None and bool(key[1]))
        self.stop_button.setEnabled(
            busy and self._worker is not None and not self._worker.cancel_event.is_set()
        )
        self.export_button.setEnabled(not busy and bool(self._results))

    def reject(self) -> None:
        if self.is_busy:
            self._close_when_done = True
            self.stop()
            return
        self.password_input.clear()
        self.enable_password_input.clear()
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.is_busy:
            self._close_when_done = True
            self.stop()
            event.ignore()
            return
        self.password_input.clear()
        self.enable_password_input.clear()
        super().closeEvent(event)
