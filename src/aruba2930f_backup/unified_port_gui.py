"""One integrated window for grouped PoE/LLDP inspection and safe batch changes."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import cast

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
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
)
from .models import Credentials
from .poe_query_gui import PoEQueryDialog, PoEQueryServiceProtocol
from .poe_query_models import PoEQueryResult, PoETarget
from .port_control import PortAction

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
        self.setWindowTitle("Aruba 2930F · 포트 통합 관리")
        self.resize(1200, 920)
        self.result_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.result_tree.itemSelectionChanged.connect(self._update_control_buttons)
        self._build_control_panel()
        self._set_busy(False)

    @property
    def is_busy(self) -> bool:
        return super().is_busy or getattr(self, "_batch_thread", None) is not None

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
        layout.addLayout(row2)
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
        if hasattr(self, "_batch_plan"):
            self._batch_plan = None
        super()._invalidate()
        if hasattr(self, "plan_preview"):
            self.plan_preview.setText("입력 대상이 변경되어 작업 계획을 초기화했습니다.")

    def _start(self, groups: tuple[PoETarget, ...], credentials: Credentials) -> None:
        self._batch_plan = None
        if hasattr(self, "plan_preview"):
            self.plan_preview.setText("재조회 중 · 완료 후 포트 제어 계획을 다시 작성하세요.")
        super()._start(groups, credentials)

    def _selected_rows(self) -> tuple[PoEQueryResult, ...]:
        selected: set[tuple[str, str]] = set()
        for item in self.result_tree.selectedItems():
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
        self.batch_status.setText(f"일괄 작업 실행 중 · 0/{len(plan.targets)}개 완료")
        self._set_busy(True)
        thread.start()

    @Slot(object)
    def _batch_progress(self, value: object) -> None:
        index, result = cast(tuple[int, BatchResult], value)
        self._batch_count = index + 1
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
        self.batch_status.setText(
            message + "\n명령 전송 여부가 불확실한 항목은 자동 재실행하지 않습니다."
        )
        self._batch_plan = None

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
        if self._close_after_batch:
            self._close_after_batch = False
            self.reject()

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
