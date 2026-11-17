"""One integrated window for grouped PoE/LLDP inspection and safe batch changes."""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from PySide6.QtCore import QSignalBlocker, QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .batch_port_control import (
    BatchControlError,
    BatchOutcome,
    BatchPlan,
    BatchPortController,
    BatchResult,
    BatchStatus,
    create_batch_plan,
    journal_states,
    validate_batch_target,
)
from .models import Credentials
from .poe_cycle import (
    PowerCyclePhase,
    PowerCycleSession,
    prepare_cycle_off,
)
from .poe_query_gui import PoEQueryDialog, PoEQueryServiceProtocol
from .poe_query_models import PoEQueryResult, PoETarget
from .poe_targets import merge_targets, parse_pasted_targets
from .port_paste_dialog import PortTargetPasteDialog
from .port_control import PortAction
from .storage import default_output_directory

ACTION_TITLES: dict[PortAction, str] = {
    PortAction.POE_ON: "PoE 전원 ON",
    PortAction.POE_OFF: "PoE 전원 OFF",
    PortAction.ENABLE: "포트 Up",
    PortAction.DISABLE: "포트 Down",
}


class _BatchWorker(QObject):
    progress = Signal(object)
    completed = Signal(object)
    failed = Signal(str)
    checkpoint = Signal(int, int)
    finished = Signal()

    def __init__(
        self,
        plan: BatchPlan,
        credentials: Credentials,
        *,
        controller: BatchPortController | None = None,
    ) -> None:
        super().__init__()
        self._plan = plan
        self._credentials: Credentials | None = credentials
        self._controller = controller or BatchPortController()
        self.cancel_event = threading.Event()
        self._continue_event = threading.Event()
        self._continue_allowed = False

    def cancel(self) -> None:
        self.cancel_event.set()
        self._continue_allowed = False
        self._continue_event.set()

    def proceed(self, accepted: bool) -> None:
        self._continue_allowed = accepted
        self._continue_event.set()

    def _wait_for_checkpoint(self, done: int, total: int) -> bool:
        self._continue_allowed = False
        self._continue_event.clear()
        self.checkpoint.emit(done, total)
        while not self._continue_event.wait(0.1):
            if self.cancel_event.is_set():
                return False
        return self._continue_allowed and not self.cancel_event.is_set()

    def _on_result(self, index: int, result: BatchResult) -> None:
        self.progress.emit((index, result))

    @Slot()
    def run(self) -> None:
        try:
            credentials = self._credentials
            if credentials is None:
                raise BatchControlError("접속 정보가 존재하지 않습니다.")
            outcome = self._controller.execute(
                self._plan,
                credentials,
                cancel_event=self.cancel_event,
                on_result=self._on_result,
                continue_after_chunk=self._wait_for_checkpoint,
            )
            self.completed.emit(outcome)
        except BatchControlError as exc:
            self.failed.emit(str(exc))
        except Exception:
            self.failed.emit(
                "일괄 작업을 완료하지 못했습니다. 마지막 작업의 변경 여부가 불확실할 수 있습니다. "
                "작업 이력과 실제 스위치 상태를 대조하세요. 자동 재시도하지 않습니다."
            )
        finally:
            self._credentials = None
            self.finished.emit()


class BatchConfirmationDialog(QDialog):
    """Explicit operator approval binds one action and its frozen target set."""

    def __init__(self, plan: BatchPlan, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("다중 포트 제어 최종 확인")
        self.setMinimumSize(600, 430)
        self.confirmation = f"EXECUTE {len(plan.targets)}"
        layout = QVBoxLayout(self)
        title = ACTION_TITLES[plan.action]
        message = QLabel(
            f"요청 작업: {title}\n대상: {len(plan.targets)}개 포트\n"
            "작업은 한 포트씩, 실행 직전 재확인 후 진행합니다.\n"
            "전원 OFF 또는 포트 Down은 AP 무선 서비스에 영향을 줄 수 있습니다.\n"
            "업링크/관리망 연결을 직접 확인하세요. 명령 전송 후 결과가 불확실하면 자동 반복하지 않습니다.",
            self,
        )
        message.setWordWrap(True)
        message.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(message)
        summary = QPlainTextEdit(self)
        summary.setReadOnly(True)
        summary.setObjectName("batchTargetPreview")
        summary.setPlainText(
            "\n".join(
                f"{idx:03d}. {item.target.endpoint} / {item.interface} · "
                f"{item.expected_hostname} · {item.ap_name or 'LLDP 이름 없음'}"
                for idx, item in enumerate(plan.targets, start=1)
            )
        )
        layout.addWidget(summary)
        layout.addWidget(QLabel(f"확인 문자열을 그대로 입력하세요: {self.confirmation}", self))
        self.confirm_input = QLineEdit(self)
        self.confirm_input.setObjectName("batchConfirmationText")
        self.confirm_input.setPlaceholderText(self.confirmation)
        layout.addWidget(self.confirm_input)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        self.execute_button = QPushButton("검증 후 순차 실행", self)
        self.execute_button.setObjectName("batchConfirmExecute")
        self.execute_button.setEnabled(False)
        buttons.addButton(self.execute_button, QDialogButtonBox.ButtonRole.AcceptRole)
        self.confirm_input.textChanged.connect(
            lambda text: self.execute_button.setEnabled(text == self.confirmation)
        )
        self.execute_button.clicked.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @Slot()
    def _accept(self) -> None:
        if self.confirm_input.text() == self.confirmation:
            self.accept()


class UnifiedPortManagementDialog(PoEQueryDialog):
    """Reuse the proven query UI; add an independent, fail-closed batch console."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        batch_controller: BatchPortController | None = None,
        service: PoEQueryServiceProtocol | None = None,
        initial_ips: Sequence[str] = (),
        ssh_port: int = 22,
        username: str = "",
        password: str = "",
        enable_password: str = "",
    ) -> None:
        super().__init__(
            parent,
            service=service,
            initial_ips=initial_ips,
            ssh_port=ssh_port,
            username=username,
            password=password,
            enable_password=enable_password,
        )
        self._batch_controller = batch_controller
        self._batch_thread: QThread | None = None
        self._batch_worker: _BatchWorker | None = None
        self._batch_plan: BatchPlan | None = None
        self._batch_outcome: BatchOutcome | None = None
        self._batch_count = 0
        self._close_after_batch = False
        self._cycle: PowerCycleSession | None = None
        self._cycle_query_stage = ""
        self._selection_summary_timer = QTimer(self)
        self._selection_summary_timer.setSingleShot(True)
        self._selection_summary_timer.setInterval(120)
        self._selection_summary_timer.timeout.connect(self._update_selection_summary)
        self.setWindowTitle("Aruba 2930F · 포트 통합 관리")
        self.resize(1200, 920)
        self.result_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.result_tree.itemSelectionChanged.connect(self._selection_changed_for_batch)
        self._build_search_panel()
        self._build_control_panel()
        self._set_busy(False)

    @property
    def is_busy(self) -> bool:
        return super().is_busy or getattr(self, "_batch_thread", None) is not None

    def _build_search_panel(self) -> None:
        """Search and visible, explicit scope-selection; never select parent rows."""
        container = QGroupBox("포트 결과 검색·필터·선택", self)
        layout = QVBoxLayout(container)
        row = QHBoxLayout()
        self.search_input = QLineEdit(container)
        self.search_input.setObjectName("unifiedSearch")
        self.search_input.setPlaceholderText("스위치 IP / 호스트네임 / 포트 / AP 이름 검색")
        row.addWidget(self.search_input, 3)
        self.filter_combo = QComboBox(container)
        self.filter_combo.setObjectName("unifiedStatusFilter")
        for label, code in (
            ("전체", "all"),
            ("조회 실패", "failed"),
            ("전력 확인 필요", "unknown"),
            ("PoE 꺼짐", "off"),
        ):
            self.filter_combo.addItem(label, code)
        row.addWidget(self.filter_combo, 1)
        layout.addLayout(row)

        selection_row = QHBoxLayout()
        self.selection_scope = QComboBox(container)
        self.selection_scope.setObjectName("unifiedSelectionScope")
        self.selection_scope.addItem("조회된 전체", "all")
        self.selection_scope.addItem("현재 스위치", "switch")
        self.selection_scope.addItem("검색 결과", "filtered")
        self.selection_scope.setToolTip(
            "전체는 현재 검색 조건을 해제하고 조회 대상의 포트 행을 선택합니다."
        )
        selection_row.addWidget(self.selection_scope, 1)
        self.select_all_button = QPushButton("범위 전체 선택", container)
        self.select_all_button.setObjectName("unifiedSelectAll")
        self.select_all_button.clicked.connect(self._select_scope_all)
        selection_row.addWidget(self.select_all_button)
        self.select_eligible_button = QPushButton("제어 가능만", container)
        self.select_eligible_button.setObjectName("unifiedSelectEligible")
        self.select_eligible_button.clicked.connect(self._select_scope_eligible)
        self.select_eligible_button.setToolTip(
            "현재 선택한 PoE/포트 명령의 안전 검증을 통과한 포트만 선택합니다."
        )
        selection_row.addWidget(self.select_eligible_button)
        self.clear_selection_button = QPushButton("선택 해제", container)
        self.clear_selection_button.setObjectName("unifiedClearSelection")
        self.clear_selection_button.clicked.connect(self._clear_selection)
        selection_row.addWidget(self.clear_selection_button)
        layout.addLayout(selection_row)
        self.selection_summary = QLabel("선택 0개 · 제어 대상은 조회 후 선택", container)
        self.selection_summary.setObjectName("unifiedSelectionSummary")
        self.selection_summary.setWordWrap(True)
        layout.addWidget(self.selection_summary)

        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(180)
        self._filter_timer.timeout.connect(self._apply_search_filter)
        self.search_input.textChanged.connect(self._filter_changed)
        self.filter_combo.currentIndexChanged.connect(self._filter_changed)
        parent = cast(QVBoxLayout, self.layout())
        parent.insertWidget(parent.indexOf(self.result_tree), container)

    def _scope_items(self) -> list[QTreeWidgetItem] | None:
        scope = str(self.selection_scope.currentData())
        if scope == "all":
            # An all-rows selection must not silently omit hidden filtered ports.
            if self.search_input.text():
                self.search_input.clear()
            if self.filter_combo.currentData() != "all":
                self.filter_combo.setCurrentIndex(self.filter_combo.findData("all"))
            self._filter_timer.stop()
            self._apply_search_filter()
        parent: QTreeWidgetItem | None = None
        if scope == "switch":
            current = self.result_tree.currentItem()
            if current is None:
                QMessageBox.information(
                    self, "스위치 선택", "먼저 결과 표에서 스위치 또는 포트를 선택하세요."
                )
                return None
            parent = current.parent() or current
        roots = (
            [parent]
            if parent is not None
            else [self.result_tree.topLevelItem(i) for i in range(self.result_tree.topLevelItemCount())]
        )
        return [
            row
            for root in roots
            if root is not None and not root.isHidden()
            for i in range(root.childCount())
            if not (row := root.child(i)).isHidden()
        ]

    @Slot()
    def _select_scope_all(self) -> None:
        self._select_scope(eligible_only=False)

    @Slot()
    def _select_scope_eligible(self) -> None:
        self._select_scope(eligible_only=True)

    def _select_scope(self, *, eligible_only: bool) -> None:
        if self.is_busy:
            return
        candidates = self._scope_items()
        if candidates is None:
            return
        chosen = candidates
        if eligible_only:
            action = PortAction(str(self.action_combo.currentData()))
            chosen = []
            for item in candidates:
                key = cast(tuple[str, str] | None, item.data(0, Qt.ItemDataRole.UserRole))
                result = self._results.get(key) if key is not None else None
                if result is None:
                    continue
                try:
                    validate_batch_target(result, action)
                except BatchControlError:
                    continue
                chosen.append(item)
        # One signal after the full selection prevents 14,400 repeated plan invalidations.
        with QSignalBlocker(self.result_tree):
            self.result_tree.clearSelection()
            for item in chosen:
                item.setSelected(True)
        self._selection_changed_for_batch()
        self._update_selection_summary()
        if len(chosen) > 300:
            self.selection_summary.setText(
                self.selection_summary.text()
                + " · 제어 1회 최대 300포트. 스위치/검색 범위로 나누세요."
            )

    @Slot()
    def _clear_selection(self) -> None:
        if self.is_busy:
            return
        self.result_tree.clearSelection()
        self._selection_changed_for_batch()
        self._update_selection_summary()

    @Slot()
    def _update_selection_summary(self) -> None:
        if not hasattr(self, "selection_summary"):
            return
        items = [
            item
            for item in self.result_tree.selectedItems()
            if item.parent() is not None
            and not item.isHidden()
            and not item.parent().isHidden()
        ]
        action = PortAction(str(self.action_combo.currentData()))
        eligible = 0
        for item in items:
            key = cast(tuple[str, str] | None, item.data(0, Qt.ItemDataRole.UserRole))
            result = self._results.get(key) if key is not None else None
            if result is None:
                continue
            try:
                validate_batch_target(result, action)
            except BatchControlError:
                continue
            eligible += 1
        selected = len(items)
        self.selection_summary.setText(
            f"선택 {selected}개 · {ACTION_TITLES[action]} 가능 {eligible}개 · "
            f"제외/확인 필요 {selected - eligible}개 · 일괄 제어 1회 최대 300포트"
        )

    def paste_targets(self, text: str, *, replace: bool = False) -> None:
        """Preserve old append API while offering a safe explicit replace choice."""
        if self.is_busy:
            raise ValueError("조회·포트 제어 중에는 대상 목록을 변경할 수 없습니다.")
        parsed = parse_pasted_targets(text)
        existing = (
            self._validated_targets()
            if not replace and self._has_target_text()
            else ()
        )
        groups = merge_targets((*existing, *parsed))
        self._replace_targets(groups)

    @Slot()
    def _paste_dialog(self) -> None:
        if self.is_busy:
            return
        dialog = PortTargetPasteDialog(
            self,
            existing_targets=lambda: (
                self._validated_targets() if self._has_target_text() else ()
            ),
        )
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                groups = dialog.proposed_targets
                if groups is not None:
                    self._replace_targets(groups)
                    self.status_label.setText(
                        f"붙여넣기 적용 · 스위치 {len(groups)}대 · "
                        f"포트 {sum(len(group.interfaces) for group in groups)}개. "
                        "전체 조회를 실행하세요."
                    )
        finally:
            dialog.deleteLater()

    def _clear_batch_plan(self, message: str) -> None:
        if self._batch_plan is not None and not self.is_busy:
            if self._cycle is not None and self._cycle.phase in (
                PowerCyclePhase.OFF_READY,
                PowerCyclePhase.ON_READY,
            ):
                self._cycle.interrupt()
            self._batch_plan = None
            self.plan_preview.setText(message)
        if hasattr(self, "plan_button"):
            self._update_control_buttons()

    def _selection_changed_for_batch(self) -> None:
        self._clear_batch_plan("포트 선택이 변경되었습니다. 작업 계획을 다시 작성하세요.")
        self._selection_summary_timer.start()

    def _filter_changed(self, _value: object = None) -> None:
        self._clear_batch_plan("검색·필터가 변경되었습니다. 작업 계획을 다시 작성하세요.")
        self._filter_timer.start()

    @Slot()
    def _apply_search_filter(self) -> None:
        term = self.search_input.text().strip().casefold()
        wanted = str(self.filter_combo.currentData())
        for group in self._groups:
            endpoint = group.target.endpoint
            parent = self._parents[endpoint]
            switch_name = parent.text(0)
            visible = False
            for interface in group.interfaces:
                key = (endpoint, interface)
                item = self._items[key]
                result = self._results.get(key)
                neighbors = result.lldp.neighbors if result and result.lldp else ()
                names = " ".join(neighbor.system_name for neighbor in neighbors)
                match_text = " ".join(
                    (
                        endpoint,
                        switch_name,
                        group.label,
                        interface,
                        names,
                        item.text(1),
                    )
                )
                matched_term = not term or term in match_text.casefold()
                if wanted == "failed":
                    matched_status = result is not None and (
                        result.status.value == "failed"
                        or (result.lldp is not None and result.lldp.status.value == "failed")
                    )
                elif wanted == "unknown":
                    matched_status = result is not None and (
                        result.assessment is None or result.assessment.label == "확인 필요"
                    )
                elif wanted == "off":
                    matched_status = (
                        result is not None
                        and result.snapshot is not None
                        and result.snapshot.poe_enabled is False
                    )
                else:
                    matched_status = True
                shown = matched_term and matched_status
                if not shown and item.isSelected():
                    item.setSelected(False)
                item.setHidden(not shown)
                visible = visible or shown
            parent.setHidden(not visible)
        self._selection_summary_timer.start()

    def _apply_result(self, value: object) -> None:
        super()._apply_result(value)
        self._selection_summary_timer.start()
        if hasattr(self, "search_input") and (
            self.search_input.text() or self.filter_combo.currentData() != "all"
        ):
            self._filter_timer.start()

    def _build_control_panel(self) -> None:
        group = QGroupBox("다중 포트 제어 · 작업 대기열", self)
        layout = QVBoxLayout(group)
        info = QLabel(
            "조회 결과에서 Ctrl/Shift로 포트를 여러 개 선택하세요. "
            "전원 OFF/포트 Down의 일괄 작업은 LLDP로 AP-535가 확인된 포트만 허용합니다. "
            "일반 포트나 LLDP 미확인 포트는 아래 '선택 포트 단일 제어'에서 개별 확인하세요.",
            group,
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        row = QHBoxLayout()
        self.action_combo = QComboBox(group)
        self.action_combo.setObjectName("unifiedBatchAction")
        for action, label in ACTION_TITLES.items():
            self.action_combo.addItem(label, action.value)
        row.addWidget(self.action_combo)
        self.action_combo.currentIndexChanged.connect(self._selection_changed_for_batch)
        self.plan_button = QPushButton("선택한 포트로 작업 계획", group)
        self.plan_button.setObjectName("unifiedBatchPrepare")
        self.plan_button.clicked.connect(self._prepare_batch)
        row.addWidget(self.plan_button)
        self.single_button = QPushButton("선택 포트 단일 제어…", group)
        self.single_button.setObjectName("unifiedSingleControl")
        self.single_button.clicked.connect(self._open_single_control)
        row.addWidget(self.single_button)
        layout.addLayout(row)
        self.plan_preview = QLabel("작업 계획 없음 · 먼저 포트를 선택하세요.", group)
        self.plan_preview.setObjectName("unifiedBatchPreview")
        self.plan_preview.setWordWrap(True)
        layout.addWidget(self.plan_preview)
        row2 = QHBoxLayout()
        self.run_button = QPushButton("계획 검토·승인 후 실행", group)
        self.run_button.setObjectName("unifiedBatchRun")
        self.run_button.clicked.connect(self._run_batch)
        row2.addWidget(self.run_button)
        self.cancel_batch_button = QPushButton("일괄 작업 중지", group)
        self.cancel_batch_button.setObjectName("unifiedBatchStop")
        self.cancel_batch_button.clicked.connect(self._cancel_batch)
        row2.addWidget(self.cancel_batch_button)
        self.history_button = QPushButton("작업 이력 확인…", group)
        self.history_button.setObjectName("unifiedBatchHistory")
        self.history_button.clicked.connect(self._show_history)
        row2.addWidget(self.history_button)
        layout.addLayout(row2)
        self.cycle_button = QPushButton("PoE 재가동 시작 (OFF → 확인 → ON)", group)
        self.cycle_button.setObjectName("unifiedPowerCycle")
        self.cycle_button.setToolTip("OFF와 ON을 자동 연속 실행하지 않습니다. 각 단계는 별도로 승인합니다.")
        self.cycle_button.clicked.connect(self._cycle_next)
        layout.addWidget(self.cycle_button)
        self.cycle_status = QLabel(
            "선택 포트를 OFF → 상태 재조회 → 별도 승인 후 ON → 복구 재조회합니다.",
            group,
        )
        self.cycle_status.setObjectName("unifiedPowerCycleStatus")
        self.cycle_status.setWordWrap(True)
        layout.addWidget(self.cycle_status)
        self.batch_progress = QProgressBar(group)
        self.batch_progress.setObjectName("unifiedBatchProgress")
        self.batch_progress.setRange(0, 100)
        self.batch_progress.setValue(0)
        layout.addWidget(self.batch_progress)
        self.batch_status = QLabel(
            "실행 전 대상별 재검증 · 10개마다 다음 묶음 승인 · 설정 저장(write memory) 없음",
            group,
        )
        self.batch_status.setObjectName("unifiedBatchStatus")
        self.batch_status.setWordWrap(True)
        layout.addWidget(self.batch_status)
        parent_layout = cast(QVBoxLayout, self.layout())
        parent_layout.insertWidget(parent_layout.count() - 2, group)

    def _set_busy(self, busy: bool) -> None:
        super()._set_busy(busy)
        if hasattr(self, "plan_button"):
            self._update_control_buttons()

    def _invalidate(self) -> None:
        if getattr(self, "_cycle", None) is not None:
            if self._cycle.phase not in (
                PowerCyclePhase.COMPLETE,
                PowerCyclePhase.INTERRUPTED,
            ):
                self._cycle.interrupt()
        if hasattr(self, "_batch_plan"):
            self._batch_plan = None
        super()._invalidate()
        if hasattr(self, "plan_preview"):
            self.plan_preview.setText("입력 대상이 변경되어 작업 계획을 초기화했습니다.")
        if hasattr(self, "cycle_button"):
            self._update_cycle_controls()

    def _start(self, groups: tuple[PoETarget, ...], credentials: Credentials) -> None:
        self._batch_plan = None
        if hasattr(self, "plan_preview"):
            self.plan_preview.setText("재조회 중 · 완료 후 포트 제어 계획을 다시 작성하세요.")
        super()._start(groups, credentials)

    def _selected_rows(self) -> tuple[PoEQueryResult, ...]:
        selected: set[tuple[str, str]] = set()
        for item in self.result_tree.selectedItems():
            if item.isHidden() or (item.parent() is not None and item.parent().isHidden()):
                continue
            key = cast(tuple[str, str] | None, item.data(0, Qt.ItemDataRole.UserRole))
            if key is not None and key[1]:
                selected.add(key)
        # One accidental parent selection must never select all child ports.
        return tuple(
            self._results[key]
            for group in self._groups
            for interface in group.interfaces
            if (key := (group.target.endpoint, interface)) in selected and key in self._results
        )

    @Slot()
    def _update_control_buttons(self) -> None:
        if not hasattr(self, "plan_button"):
            return
        busy = self.is_busy
        selected = self._selected_rows()
        self.plan_button.setEnabled(not busy and bool(selected))
        self.single_button.setEnabled(not busy and len(selected) == 1)
        self.run_button.setEnabled(not busy and self._batch_plan is not None)
        self.cancel_batch_button.setEnabled(self._batch_thread is not None)
        self.action_combo.setEnabled(not busy)
        self.history_button.setEnabled(not busy)
        self.search_input.setEnabled(not busy)
        self.filter_combo.setEnabled(not busy)
        if hasattr(self, "selection_scope"):
            locked = self._cycle is not None and self._cycle.phase not in (
                PowerCyclePhase.COMPLETE,
                PowerCyclePhase.INTERRUPTED,
            )
            for button in (
                self.selection_scope,
                self.select_all_button,
                self.select_eligible_button,
                self.clear_selection_button,
            ):
                button.setEnabled(not busy and not locked)
            self.plan_button.setEnabled(self.plan_button.isEnabled() and not locked)
            self.action_combo.setEnabled(not busy and not locked)
        if hasattr(self, "cycle_button"):
            self._update_cycle_controls()

    @Slot()
    def _prepare_batch(self) -> None:
        if self.is_busy:
            return
        selected = self._selected_rows()
        if not selected:
            QMessageBox.information(self, "작업 대상 선택", "조회 결과에서 포트 행을 선택하세요.")
            return
        try:
            action = PortAction(str(self.action_combo.currentData()))
            self._batch_plan = create_batch_plan(selected, action)
        except (BatchControlError, ValueError) as exc:
            self._batch_plan = None
            QMessageBox.warning(self, "제어 계획 확인", str(exc))
            self._update_control_buttons()
            return
        plan = self._batch_plan
        self.plan_preview.setText(
            f"계획 준비: {ACTION_TITLES[plan.action]} · {len(plan.targets)}개 포트 · "
            f"{len({item.target.endpoint for item in plan.targets})}개 스위치. "
            "실행 버튼을 눌러 전체 대상을 검토하고 확인 문자열을 입력하세요."
        )
        self._update_control_buttons()

    @Slot()
    def _run_batch(self) -> None:
        if self.is_busy or self._batch_plan is None:
            return
        plan = self._batch_plan
        confirmation = BatchConfirmationDialog(plan, self)
        if confirmation.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            credentials = self._credentials()
        except ValueError:
            QMessageBox.warning(self, "접속 정보 확인", "SSH 사용자 이름과 비밀번호를 확인하세요.")
            return
        session = self._cycle
        if session is not None and session.phase in (
            PowerCyclePhase.OFF_READY,
            PowerCyclePhase.ON_READY,
        ) and (
            plan.plan_id == session.off_plan.plan_id
            or (session.on_plan is not None and plan.plan_id == session.on_plan.plan_id)
        ):
            try:
                session.mark_running(plan)
            except BatchControlError as exc:
                QMessageBox.warning(self, "PoE 재가동 승인", str(exc))
                return
        thread = QThread(self)
        worker = _BatchWorker(plan, credentials, controller=self._batch_controller)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._batch_progress)
        worker.completed.connect(self._batch_completed)
        worker.failed.connect(self._batch_failed)
        worker.checkpoint.connect(self._approve_next_chunk)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._batch_thread_finished)
        self._batch_thread, self._batch_worker = thread, worker
        self._batch_count = 0
        self._batch_outcome = None
        self.batch_progress.setRange(0, len(plan.targets))
        self.batch_progress.setValue(0)
        self.batch_status.setText(f"일괄 작업 실행 중 · 0/{len(plan.targets)}개 완료")
        self._set_busy(True)
        thread.start()

    @Slot(object)
    def _batch_progress(self, value: object) -> None:
        index, result = cast(tuple[int, BatchResult], value)
        self._batch_count = index + 1
        self.batch_progress.setValue(self._batch_count)
        key = (result.target.target.endpoint, result.target.interface)
        if key in self._items:
            item: QTreeWidgetItem = self._items[key]
            state = {
                BatchStatus.VERIFIED: "제어 완료 · 재조회 필요",
                BatchStatus.UNCHANGED: "이미 요청한 설정",
                BatchStatus.PREFLIGHT_FAILED: "제어 전 검증 실패",
                BatchStatus.UNVERIFIED: "변경 결과 확인 불가",
                BatchStatus.CANCELLED: "제어 취소",
                BatchStatus.PAUSED: "제어 대기",
            }[result.status]
            item.setText(5, state)
            item.setToolTip(5, result.message)
            if result.status in (
                BatchStatus.VERIFIED,
                BatchStatus.PREFLIGHT_FAILED,
                BatchStatus.UNVERIFIED,
            ):
                # Never present a pre-mutation AP identity or power value as current.
                item.setText(1, "재조회 필요")
                item.setText(2, "—")
                item.setText(3, "확인 필요")
                self._results.pop(key, None)
        self.batch_status.setText(
            f"일괄 작업: {self._batch_count}/{len(self._batch_plan.targets) if self._batch_plan else '?'}개 처리 · "
            f"현재 결과 {result.status.value} · {result.message}"
        )

    @Slot(object)
    def _batch_completed(self, value: object) -> None:
        result = cast(BatchOutcome, value)
        if self._cycle is not None:
            try:
                if self._cycle.phase is PowerCyclePhase.OFF_RUNNING:
                    self._cycle.record_off(result)
                elif self._cycle.phase is PowerCyclePhase.ON_RUNNING:
                    self._cycle.record_on(result)
            except BatchControlError:
                self._cycle.interrupt()
            self._update_cycle_controls()
        self._batch_outcome = result
        counts = result.counts
        self.batch_status.setText(
            f"일괄 작업 종료 · 확인 성공 {counts['verified']} · 이미 설정 {counts['unchanged']} · "
            f"검증 실패 {counts['preflight_failed']} · 확인 불가 {counts['unverified']} · "
            f"미실행 {counts['paused'] + counts['cancelled']}\n"
            f"작업 이력: {result.journal_path}"
        )
        self._batch_plan = None

    @Slot(str)
    def _batch_failed(self, message: str) -> None:
        if self._cycle is not None and self._cycle.phase in (
            PowerCyclePhase.OFF_RUNNING,
            PowerCyclePhase.ON_RUNNING,
        ):
            self._cycle.interrupt()
            self._update_cycle_controls()
        # A result-journal write can fail *after* a switch mutation but
        # before on_result is emitted. Treat all planned rows as potentially
        # stale and require an explicit new query before another control.
        plan = self._batch_plan
        if plan is not None:
            for target in plan.targets:
                key = (target.target.endpoint, target.interface)
                self._results.pop(key, None)
                item = self._items.get(key)
                if item is not None:
                    item.setText(1, "재조회 필요")
                    item.setText(2, "—")
                    item.setText(3, "확인 필요")
                    item.setText(5, "일괄 작업 실패 · 결과 불확실")
                    item.setToolTip(5, "작업 이력을 확인하고 수동 재조회하세요.")
        self._batch_outcome = None
        self.batch_status.setText(
            message + "\n계획 대상의 이전 AP·전력 조회값을 무효화했습니다. "
            "실제 스위치 상태를 수동 재조회하세요. 자동 재실행하지 않습니다."
        )
        self._batch_plan = None
        self.plan_preview.setText("일괄 제어 실패 · 대상 포트를 다시 조회한 뒤 계획을 작성하세요.")
        self._update_control_buttons()
        if self.search_input.text() or self.filter_combo.currentData() != "all":
            self._filter_timer.start()

    @Slot(int, int)
    def _approve_next_chunk(self, processed: int, total: int) -> None:
        worker = self._batch_worker
        if worker is None or worker.cancel_event.is_set():
            if worker is not None:
                worker.proceed(False)
            return
        answer = QMessageBox.question(
            self,
            "다음 작업 묶음 승인",
            f"{processed}/{total}개 포트를 처리했습니다.\n"
            "작업 이력과 변경 결과를 확인한 후 다음 최대 10개 포트를 처리하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        worker.proceed(answer is QMessageBox.StandardButton.Yes)

    @Slot()
    def _cancel_batch(self) -> None:
        if self._batch_worker is not None:
            self._batch_worker.cancel()
            self.batch_status.setText(
                "일괄 작업 중지 요청 · 실행 중 명령이 끝나면 남은 대상은 수행하지 않습니다."
            )
            self.cancel_batch_button.setEnabled(False)

    @Slot()
    def _batch_thread_finished(self) -> None:
        thread = self._batch_thread
        if thread is not None and not thread.wait(0):
            QTimer.singleShot(25, self._batch_thread_finished)
            return
        self._batch_thread = None
        self._batch_worker = None
        if thread is not None:
            thread.deleteLater()
        self._set_busy(False)
        self._update_cycle_controls()
        if self._close_after_batch:
            self._close_after_batch = False
            self.reject()

    @Slot()
    def _cycle_next(self) -> None:
        """Require explicit human approval and a fresh query for each phase."""
        if self.is_busy:
            return
        session = self._cycle
        if session is None or session.phase in (
            PowerCyclePhase.COMPLETE,
            PowerCyclePhase.INTERRUPTED,
        ):
            if session is not None and session.phase is PowerCyclePhase.INTERRUPTED:
                answer = QMessageBox.question(
                    self,
                    "중단된 재가동 계획 종료",
                    "일부 포트가 OFF 상태로 남아 있을 수 있습니다.\n"
                    "이력과 실제 장비를 확인한 뒤 수동으로 복구하세요.\n"
                    "마법사만 초기화하시겠습니까?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
                self._cycle = None
                self._update_cycle_controls()
                return
            try:
                plan = prepare_cycle_off(self._selected_rows())
            except (BatchControlError, ValueError) as exc:
                QMessageBox.warning(self, "재가동 OFF 계획 확인", str(exc))
                return
            self._cycle = PowerCycleSession(plan)
            self._batch_plan = plan
            self.plan_preview.setText(
                f"재가동 1/2: {len(plan.targets)}포트 PoE OFF 계획. "
                "아래 실행 버튼에서 전체 대상과 확인 문자열을 승인하세요. "
                "OFF 이후 ON은 자동으로 실행되지 않습니다."
            )
            self._update_control_buttons()
            return

        if session.phase in (PowerCyclePhase.REQUERY_OFF, PowerCyclePhase.REQUERY_ON):
            try:
                targets = (
                    session.off_requery_groups()
                    if session.phase is PowerCyclePhase.REQUERY_OFF
                    else session.on_requery_groups()
                )
            except BatchControlError as exc:
                QMessageBox.warning(self, "복구 대상 확인", str(exc))
                return
            stage = "off" if session.phase is PowerCyclePhase.REQUERY_OFF else "on"
            self._refresh(targets)
            if self._thread is not None:
                self._cycle_query_stage = stage
            return

        if session.phase is PowerCyclePhase.ON_PREPARE:
            try:
                plan = session.prepare_on(self._results)
            except BatchControlError as exc:
                QMessageBox.warning(self, "재가동 ON 준비", str(exc))
                session.phase = PowerCyclePhase.REQUERY_OFF
                self._update_cycle_controls()
                return
            self._batch_plan = plan
            self.plan_preview.setText(
                f"재가동 2/2: OFF 확인 포트 {len(plan.targets)}개를 ON합니다. "
                "전원 상태와 스위치 식별이 재조회됐습니다. "
                "별도 확인 문자열과 작업 승인 후에만 실행합니다."
            )
            self._update_control_buttons()
            return

        if session.phase is PowerCyclePhase.VERIFY_ON:
            try:
                session.verify_on(self._results)
            except BatchControlError as exc:
                QMessageBox.warning(self, "PoE 복구 확인", str(exc))
                session.phase = PowerCyclePhase.REQUERY_ON
                self._update_cycle_controls()
                return
            self._update_cycle_controls()

    def _update_cycle_controls(self) -> None:
        if not hasattr(self, "cycle_button"):
            return
        session = self._cycle
        phase = session.phase if session is not None else None
        labels = {
            None: "PoE 재가동 시작 (OFF → 확인 → ON)",
            PowerCyclePhase.OFF_READY: "OFF 계획 준비 · 아래 승인 후 실행",
            PowerCyclePhase.OFF_RUNNING: "PoE OFF 실행 중",
            PowerCyclePhase.REQUERY_OFF: "OFF 완료 · 재조회",
            PowerCyclePhase.ON_PREPARE: "재조회 완료 · ON 계획 만들기",
            PowerCyclePhase.ON_READY: "ON 계획 준비 · 아래 승인 후 실행",
            PowerCyclePhase.ON_RUNNING: "PoE ON 실행 중",
            PowerCyclePhase.REQUERY_ON: "ON 완료 · 복구 재조회",
            PowerCyclePhase.VERIFY_ON: "복구 전력 확인",
            PowerCyclePhase.COMPLETE: "새 PoE 재가동 시작",
            PowerCyclePhase.INTERRUPTED: "중단된 계획 종료 · 수동복구 확인",
        }
        self.cycle_button.setText(labels[phase])
        self.cycle_button.setEnabled(
            not self.is_busy
            and phase not in (
                PowerCyclePhase.OFF_READY,
                PowerCyclePhase.OFF_RUNNING,
                PowerCyclePhase.ON_READY,
                PowerCyclePhase.ON_RUNNING,
            )
        )
        if session is None:
            self.cycle_status.setText(
                "포트를 선택한 뒤 OFF → 재조회 → 별도 승인 ON → 복구 재조회를 진행합니다."
            )
        elif phase is PowerCyclePhase.OFF_READY:
            self.cycle_status.setText(
                "1단계 PoE OFF 계획을 만들었습니다. 실행 전 포트 목록을 검토하세요."
            )
        elif phase is PowerCyclePhase.REQUERY_OFF:
            self.cycle_status.setText(
                f"OFF 검증 성공 {len(session.off_verified)}포트 · "
                f"제외/미실행 {session.excluded}포트. 성공한 대상만 다시 조회합니다."
            )
        elif phase is PowerCyclePhase.ON_PREPARE:
            self.cycle_status.setText(
                "새로 조회한 OFF 상태와 SSH 지문·호스트네임을 확인한 뒤 "
                "원래 대상으로 별도 PoE ON 계획을 작성하세요."
            )
        elif phase is PowerCyclePhase.ON_READY:
            self.cycle_status.setText(
                "2단계 PoE ON 계획 준비. OFF와 별개로 대상 목록과 확인 문자열을 승인해야 합니다."
            )
        elif phase is PowerCyclePhase.REQUERY_ON:
            self.cycle_status.setText(
                "PoE ON 실행 결과가 확인됐습니다. 마지막으로 실제 전력 상태를 재조회하세요."
            )
        elif phase is PowerCyclePhase.VERIFY_ON:
            self.cycle_status.setText(
                "ON 이후 다시 조회한 실제 PoE 설정을 확인하세요. "
                "AP 부팅·무선 서비스 정상 여부는 별도 확인이 필요합니다."
            )
        elif phase is PowerCyclePhase.COMPLETE:
            self.cycle_status.setText(
                f"재가동 전력 확인 완료 · {len(session.off_verified)}포트. "
                "AP 서비스 복구는 별도로 확인해야 합니다.\n"
                f"OFF 이력: {session.off_journal}\nON 이력: {session.on_journal}"
            )
        elif phase is PowerCyclePhase.INTERRUPTED:
            self.cycle_status.setText(
                "재가동이 중단됐습니다. 결과 불확실·미실행 포트는 작업 이력과 "
                "스위치 현재 상태를 확인하세요. 자동으로 OFF/ON을 반복하지 않습니다."
            )

    def _thread_finished(self) -> None:
        super()._thread_finished()
        if self._thread is not None or not self._cycle_query_stage:
            return
        stage = self._cycle_query_stage
        self._cycle_query_stage = ""
        session = self._cycle
        if session is None:
            return
        try:
            if stage == "off":
                session.mark_off_requeried()
            elif stage == "on":
                session.mark_on_requeried()
        except BatchControlError:
            session.interrupt()
        self._update_cycle_controls()

    @Slot()
    def _open_single_control(self) -> None:
        if self.is_busy:
            return
        selected = self._selected_rows()
        if len(selected) != 1 or selected[0].snapshot is None:
            return
        snapshot = selected[0].snapshot
        from .port_control_gui import PortControlDialog

        dialog = PortControlDialog(
            self,
            ip=snapshot.target.ip,
            ssh_port=snapshot.target.port,
            username=self.username_input.text(),
            password=self.password_input.text(),
            enable_password=self.enable_password_input.text(),
        )
        dialog.interface_input.setText(snapshot.interface)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()
            # A single-port action may have changed the device since query.
            key = (snapshot.target.endpoint, snapshot.interface)
            self._results.pop(key, None)
            if key in self._items:
                self._items[key].setText(1, "재조회 필요")
                self._items[key].setText(2, "—")
                self._items[key].setText(3, "확인 필요")
                self._items[key].setText(5, "단일 제어 종료 · 재조회 필요")
            self._batch_plan = None
            self._update_control_buttons()

    @Slot()
    def _show_history(self) -> None:
        if self.is_busy:
            return
        directory = default_output_directory() / "port-control" / "batch"
        path_text, _selected_filter = QFileDialog.getOpenFileName(
            self, "일괄 제어 작업 이력 열기", str(directory), "작업 이력 (*.jsonl)"
        )
        if not path_text:
            return
        path = Path(path_text)
        try:
            states = journal_states(path)
            header_line = path.read_text(encoding="utf-8").splitlines()[0]
            header = json.loads(header_line)
            if not isinstance(header, dict) or not isinstance(header.get("items"), list):
                raise BatchControlError("작업 대상 정보가 손상되었습니다.")
            items = header["items"]
            lines = []
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    raise BatchControlError("작업 이력의 대상 행이 올바르지 않습니다.")
                lines.append(
                    f"{index + 1:03d}. {item.get('endpoint', '')} / "
                    f"{item.get('interface', '')} · {item.get('hostname', '')} · "
                    f"{states.get(index, 'result_unknown')}"
                )
        except (BatchControlError, OSError, UnicodeError, ValueError) as exc:
            QMessageBox.warning(self, "작업 이력 읽기 실패", str(exc))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("일괄 포트 제어 결과·중단 이력")
        dialog.resize(720, 520)
        layout = QVBoxLayout(dialog)
        note = QLabel(
            "result_unknown은 변경 명령의 전송 여부를 확인하지 못한 상태입니다. "
            "자동 재시도하지 말고 장비를 직접 재조회하세요.",
            dialog,
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        display = QPlainTextEdit(dialog)
        display.setReadOnly(True)
        display.setPlainText("\n".join(lines))
        layout.addWidget(display)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def stop(self) -> None:
        if self._batch_thread is not None:
            self._cancel_batch()
        else:
            super().stop()

    def reject(self) -> None:
        if self._batch_thread is not None:
            self._close_after_batch = True
            self._cancel_batch()
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._batch_thread is not None:
            self._close_after_batch = True
            self._cancel_batch()
            event.ignore()
            return
        super().closeEvent(event)
