"""Explicit, endpoint-scoped operator consent for legacy ArubaOS-S SSH."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from .models import DeviceTarget
from .ssh_legacy import session_legacy_ssh


class LegacySSHConsentDialog(QDialog):
    """Never approve a legacy SSH endpoint without exact operator input."""

    def __init__(
        self, targets: Sequence[DeviceTarget], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        if not targets:
            raise ValueError("Legacy SSH approval requires a known target.")
        self.setWindowTitle("레거시 SSH · 장비별 예외 승인")
        self.setObjectName("legacySshConsentDialog")
        self.setModal(True)
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        self.warning_label = QLabel(
            "주의: WC.16.01 등 구형 장비와 연결하기 위해 선택한 IP:포트의 "
            "SSH 서버 호스트 키 서명 ssh-rsa(RSA/SHA-1)만 현재 실행 중에 허용합니다. "
            "기본 보안 정책은 유지되며 암호 협상 위험이 증가할 수 있습니다. "
            "가능한 경우 장비 펌웨어를 업그레이드하세요. "
            "최초 SSH 지문은 별도의 신뢰 가능한 경로로 확인해야 합니다.",
            self,
        )
        self.warning_label.setObjectName("legacySshWarning")
        self.warning_label.setWordWrap(True)
        layout.addWidget(self.warning_label)

        self.target_combo = QComboBox(self)
        self.target_combo.setObjectName("legacySshEndpoint")
        for target in targets:
            self.target_combo.addItem(target.endpoint, target)
        layout.addWidget(self.target_combo)

        help_text = QLabel(
            "대상 스위치 IP:SSH포트를 아래에 정확히 입력해야 허용 또는 해제할 수 있습니다. "
            "예외는 파일에 저장되지 않고 계정 변경/해제·프로그램 종료 시 소멸합니다.",
            self,
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)

        self.confirm_input = QLineEdit(self)
        self.confirm_input.setObjectName("legacySshExactConfirm")
        self.confirm_input.setPlaceholderText(self.selected_target.endpoint)
        layout.addWidget(self.confirm_input)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        self.confirm_button = buttons.addButton(
            "선택 장비만 예외 허용", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.confirm_button.setObjectName("legacySshConfirm")
        self.confirm_button.setEnabled(False)
        self.confirm_button.clicked.connect(self._approve)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.target_combo.currentIndexChanged.connect(self._update_confirmation)
        self.confirm_input.textChanged.connect(self._update_confirmation)
        self._update_confirmation()

    @property
    def selected_target(self) -> DeviceTarget:
        target = self.target_combo.currentData()
        if not isinstance(target, DeviceTarget):
            raise ValueError("Select a known switch.")
        return target

    def select_endpoint(self, endpoint: str) -> None:
        for index in range(self.target_combo.count()):
            target = self.target_combo.itemData(index)
            if isinstance(target, DeviceTarget) and target.endpoint == endpoint:
                self.target_combo.setCurrentIndex(index)
                return

    def _update_confirmation(self, _value: object = None) -> None:
        target = self.selected_target
        active = session_legacy_ssh.allowed(target)
        self.confirm_button.setText(
            "선택 장비의 예외 해제" if active else "선택 장비만 예외 허용"
        )
        self.confirm_input.setPlaceholderText(target.endpoint)
        self.confirm_button.setEnabled(self.confirm_input.text() == target.endpoint)

    def _approve(self) -> None:
        if self.confirm_input.text() == self.selected_target.endpoint:
            self.accept()
