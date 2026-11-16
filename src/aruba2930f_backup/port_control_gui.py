"""Single-port operator controls, separate from the read-only backup workflow."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol, cast

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .models import CollectionFailure, Credentials, DeviceTarget, HostKeyCheck
from .port_control import (
    PortAction,
    PortControlService,
    PortOperationResult,
    PortSnapshot,
)

ACTION_LABELS = {
    PortAction.POE_OFF: "PoE 전원 끄기",
    PortAction.POE_ON: "PoE 전원 켜기",
    PortAction.DISABLE: "포트 내리기 (Down)",
    PortAction.ENABLE: "포트 올리기 (Up)",
}


class PortServiceProtocol(Protocol):
    def inspect(
        self,
        target: DeviceTarget,
        interface: str,
        credentials: Credentials,
        *,
        approve_host_key: Callable[[HostKeyCheck], bool],
    ) -> PortSnapshot: ...

    def apply(
        self,
        snapshot: PortSnapshot,
        action: PortAction,
        credentials: Credentials,
        *,
        confirmation: str,
    ) -> PortOperationResult: ...


class PortConfirmationDialog(QDialog):
    """Bind a deliberate confirmation to the exact inspected endpoint and port."""

    def __init__(
        self, snapshot: PortSnapshot, action: PortAction, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("포트 제어 대상 확인")
        self.setMinimumWidth(520)
        self.expected_confirmation = snapshot.confirmation_text
        layout = QVBoxLayout(self)
        text = QLabel(
            f"작업: {ACTION_LABELS[action]}\n"
            f"장비: {snapshot.hostname} ({snapshot.target.endpoint})\n"
            f"인터페이스: {snapshot.interface}\n\n"
            "관리 경로나 상단 연결 포트를 내리면 스위치 접속이 끊길 수 있습니다.\n"
            "대상 장비와 연결된 기기를 확인한 뒤 아래 문자열을 그대로 입력하세요.",
            self,
        )
        text.setTextFormat(Qt.TextFormat.PlainText)
        text.setWordWrap(True)
        layout.addWidget(text)
        target_label = QLabel(self.expected_confirmation, self)
        target_label.setTextFormat(Qt.TextFormat.PlainText)
        target_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(target_label)
        self.confirmation_input = QLineEdit(self)
        self.confirmation_input.setObjectName("portTargetConfirmation")
        self.confirmation_input.setPlaceholderText(self.expected_confirmation)
        layout.addWidget(self.confirmation_input)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, parent=self)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        self.execute_button = QPushButton("이 포트에 실행", self)
        self.execute_button.setAutoDefault(False)
        self.execute_button.setEnabled(False)
        buttons.addButton(self.execute_button, QDialogButtonBox.ButtonRole.AcceptRole)
        self.confirmation_input.textChanged.connect(
            lambda value: self.execute_button.setEnabled(value == self.expected_confirmation)
        )
        buttons.accepted.connect(self._accept_target)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @Slot()
    def _accept_target(self) -> None:
        if self.confirmation_input.text() == self.expected_confirmation:
            self.accept()


class _PortWorker(QObject):
    result = Signal(object)
    failed = Signal(str)
    approval_requested = Signal(object)
    finished = Signal()

    def __init__(
        self,
        service: PortServiceProtocol,
        target: DeviceTarget,
        interface: str,
        credentials: Credentials,
        snapshot: PortSnapshot | None,
        action: PortAction | None,
        confirmation: str,
    ) -> None:
        super().__init__()
        self.service = service
        self.target = target
        self.interface = interface
        self.credentials: Credentials | None = credentials
        self.snapshot = snapshot
        self.action = action
        self.confirmation = confirmation
        self.approval_event = threading.Event()
        self.approved = False

    def request_approval(self, check: HostKeyCheck) -> bool:
        self.approval_event.clear()
        self.approval_requested.emit(check)
        self.approval_event.wait()
        return self.approved

    @Slot()
    def run(self) -> None:
        outcome: PortSnapshot | PortOperationResult
        try:
            if self.credentials is None:
                raise ValueError("Missing session credentials.")
            if self.action is None:
                outcome = self.service.inspect(
                    self.target,
                    self.interface,
                    self.credentials,
                    approve_host_key=self.request_approval,
                )
            else:
                if self.snapshot is None:
                    raise ValueError("Inspect the port first.")
                outcome = self.service.apply(
                    self.snapshot,
                    self.action,
                    self.credentials,
                    confirmation=self.confirmation,
                )
            self.result.emit(outcome)
        except CollectionFailure as exc:
            self.failed.emit(f"{exc.code.value}: {exc.safe_message}")
        except ValueError:
            self.failed.emit(
                "대상 입력 또는 조회 상태가 유효하지 않습니다. 현재 상태를 다시 조회하세요."
            )
        except Exception:
            self.failed.emit(
                "작업을 완료하지 못했습니다. 변경을 요청했다면 실행 여부를 단정할 수 없습니다. "
                "현재 상태를 다시 조회하세요. 변경 명령은 자동 재전송하지 않습니다."
            )
        finally:
            self.credentials = None
            self.finished.emit()


class PortControlDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        service: PortServiceProtocol | None = None,
        ip: str = "",
        ssh_port: int = 22,
        username: str = "",
        password: str = "",
        enable_password: str = "",
    ) -> None:
        super().__init__(parent)
        self._service = service if service is not None else PortControlService()
        self._snapshot: PortSnapshot | None = None
        self._thread: QThread | None = None
        self._worker: _PortWorker | None = None
        self._action: PortAction | None = None
        self.setWindowTitle("Aruba 2930F · 포트 제어")
        self.resize(760, 660)
        self.setMinimumWidth(640)
        layout = QVBoxLayout(self)
        introduction = QLabel(
            "스위치 IP와 인터페이스를 입력한 뒤 현재 상태를 조회하세요.\n"
            "PoE는 연결 기기의 전원을, 포트 Down/Up은 데이터 통신을 제어합니다.",
            self,
        )
        introduction.setWordWrap(True)
        layout.addWidget(introduction)
        inputs = QGroupBox("1. 대상과 SSH 접속 정보", self)
        form = QFormLayout(inputs)
        self.ip_input = QLineEdit(ip, inputs)
        self.ip_input.setPlaceholderText("예: 192.0.2.10")
        self.interface_input = QLineEdit(inputs)
        self.interface_input.setPlaceholderText("일반 스위치: 24 / VSF: 1/24")
        self.ssh_port_input = QSpinBox(inputs)
        self.ssh_port_input.setRange(1, 65535)
        self.ssh_port_input.setValue(ssh_port)
        self.username_input = QLineEdit(username, inputs)
        self.password_input = QLineEdit(password, inputs)
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.enable_password_input = QLineEdit(enable_password, inputs)
        self.enable_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("SW IP (IPv4)", self.ip_input)
        form.addRow("인터페이스 번호", self.interface_input)
        form.addRow("SSH 접속 포트", self.ssh_port_input)
        form.addRow("사용자 이름", self.username_input)
        form.addRow("비밀번호", self.password_input)
        form.addRow("Enable 암호 (선택)", self.enable_password_input)
        layout.addWidget(inputs)
        self.inspect_button = QPushButton("현재 상태 조회", self)
        self.inspect_button.setObjectName("inspectPortButton")
        self.inspect_button.setAutoDefault(False)
        self.inspect_button.clicked.connect(self._inspect)
        layout.addWidget(self.inspect_button)
        states = QGroupBox("2. 확인된 장비와 포트 상태", self)
        state_layout = QVBoxLayout(states)
        self.state_label = QLabel("아직 조회하지 않았습니다.", states)
        self.state_label.setObjectName("portStateLabel")
        self.state_label.setTextFormat(Qt.TextFormat.PlainText)
        self.state_label.setWordWrap(True)
        self.state_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        state_layout.addWidget(self.state_label)
        layout.addWidget(states)
        actions = QGroupBox("3. 실행할 작업", self)
        action_layout = QGridLayout(actions)
        self.action_buttons: dict[str, QPushButton] = {}
        for index, (action, label) in enumerate(ACTION_LABELS.items()):
            button = QPushButton(label, actions)
            button.setObjectName(f"portAction_{action.value}")
            button.setAutoDefault(False)
            button.clicked.connect(lambda _checked=False, selected=action: self._execute(selected))
            action_layout.addWidget(button, index // 2, index % 2)
            self.action_buttons[action.value] = button
        layout.addWidget(actions)
        self.status_label = QLabel("조회 후 대상 확인을 거쳐 실행합니다.", self)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        note = QLabel(
            "전원 끄기는 꺼진 상태를 유지합니다. 복구하려면 다시 조회한 뒤 전원 켜기를 실행하세요.\n"
            "시작 설정으로 자동 저장하지 않습니다. PoE 켜짐은 AP 서비스 복구를 뜻하지 않습니다.",
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        buttons.rejected.connect(self.reject)
        self.close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        self.close_button.setText("닫기")
        layout.addWidget(buttons)
        for widget in self._input_widgets():
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._invalidate)
            elif isinstance(widget, QSpinBox):
                widget.valueChanged.connect(self._invalidate)
        self._set_busy(False)

    @property
    def is_busy(self) -> bool:
        return self._thread is not None

    def _input_widgets(self) -> tuple[QWidget, ...]:
        return (
            self.ip_input,
            self.interface_input,
            self.ssh_port_input,
            self.username_input,
            self.password_input,
            self.enable_password_input,
        )

    @Slot()
    def _invalidate(self) -> None:
        self._snapshot = None
        self.state_label.setText("입력이 변경되었습니다. 현재 상태를 다시 조회하세요.")
        self._set_busy(self._thread is not None)

    def _set_busy(self, busy: bool) -> None:
        for widget in self._input_widgets():
            widget.setEnabled(not busy)
        self.inspect_button.setEnabled(not busy)
        self.close_button.setEnabled(not busy)
        for action, button in self.action_buttons.items():
            available = not busy and self._snapshot is not None
            if action in (PortAction.POE_OFF, PortAction.POE_ON):
                available = (
                    available
                    and self._snapshot is not None
                    and self._snapshot.poe_enabled is not None
                )
            button.setEnabled(available)

    def _credentials(self) -> Credentials:
        return Credentials(
            username=self.username_input.text().strip(),
            password=self.password_input.text(),
            enable_secret=self.enable_password_input.text() or None,
        )

    @Slot()
    def _inspect(self) -> None:
        if self._thread is not None:
            return
        self._snapshot = None
        self._start(None, "")

    def _execute(self, action: PortAction) -> None:
        if self._thread is not None or self._snapshot is None:
            return
        dialog = PortConfirmationDialog(self._snapshot, action, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        confirmation = dialog.confirmation_input.text()
        if confirmation != self._snapshot.confirmation_text:
            return
        self._start(action, confirmation)

    def _start(self, action: PortAction | None, confirmation: str) -> None:
        try:
            target = DeviceTarget(self.ip_input.text(), self.ssh_port_input.value())
            credentials = self._credentials()
            interface = self.interface_input.text().strip()
            if not interface:
                raise ValueError("An interface is required.")
        except ValueError:
            self._snapshot = None
            self._set_busy(False)
            QMessageBox.warning(
                self, "입력 확인", "SW IPv4, 인터페이스, 사용자 이름과 비밀번호를 확인하세요."
            )
            return
        self._action = action
        thread = QThread(self)
        worker = _PortWorker(
            self._service,
            target,
            interface,
            credentials,
            self._snapshot,
            action,
            confirmation,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.result.connect(self._on_result)
        worker.failed.connect(self._on_failed)
        worker.approval_requested.connect(self._approve_key)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._thread, self._worker = thread, worker
        self._set_busy(True)
        self.status_label.setText(
            "현재 상태 조회 중…" if action is None else "대상 재확인 및 변경 후 상태 확인 중…"
        )
        thread.start()

    @Slot(object)
    def _approve_key(self, value: object) -> None:
        from .gui import HostKeyApprovalDialog

        worker = self._worker
        if worker is None:
            return
        try:
            dialog = HostKeyApprovalDialog([cast(HostKeyCheck, value)], self)
            worker.approved = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            worker.approval_event.set()

    @Slot(object)
    def _on_result(self, value: object) -> None:
        if self._action is None:
            snapshot = cast(PortSnapshot, value)
            self._snapshot = snapshot
            self._show_snapshot(snapshot)
            self.status_label.setText(
                "조회 완료. 실행할 작업을 선택하세요. 조회는 120초 동안 유효합니다."
            )
        else:
            result = cast(PortOperationResult, value)
            self._snapshot = None
            if result.after is not None:
                self._show_snapshot(result.after)
            else:
                self.state_label.setText(
                    "변경 후 현재 상태를 확인하지 못했습니다. 다시 조회하세요."
                )
            message = result.message + "\n다음 작업 전 현재 상태를 다시 조회하세요."
            if result.audit_path is not None:
                message += f"\n작업 이력: {result.audit_path}"
            self.status_label.setText(message)

    def _show_snapshot(self, snapshot: PortSnapshot) -> None:
        poe = (
            "확인 불가 / 미지원"
            if snapshot.poe_enabled is None
            else ("켜짐" if snapshot.poe_enabled else "꺼짐")
        )
        watts = "확인 안 됨" if snapshot.watts is None else f"{snapshot.watts:g} W"
        self.state_label.setText(
            f"{snapshot.hostname} · {snapshot.model}\n"
            f"{snapshot.target.endpoint} / 인터페이스 {snapshot.interface}\n"
            f"포트 사용: {'켜짐' if snapshot.enabled else '꺼짐'}  |  물리 링크: {snapshot.link_status}\n"
            f"PoE 사용: {poe}  |  공급 상태: {snapshot.poe_status or '확인 안 됨'}  |  전력: {watts}"
        )

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self._snapshot = None
        self.state_label.setText("최신 상태를 확인하려면 다시 조회하세요.")
        self.status_label.setText(message)

    @Slot()
    def _thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._action = None
        self._set_busy(False)

    def reject(self) -> None:
        if self._thread is not None:
            self.status_label.setText("실행 중인 작업의 결과를 확인한 뒤 닫아주세요.")
            return
        self.password_input.clear()
        self.enable_password_input.clear()
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._thread is not None:
            event.ignore()
            return
        self.password_input.clear()
        self.enable_password_input.clear()
        super().closeEvent(event)
