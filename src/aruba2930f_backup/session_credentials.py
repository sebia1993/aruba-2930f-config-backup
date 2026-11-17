"""One process-local SSH credential source for the integrated operator shell.

This module deliberately has no filesystem, keychain or persistence API.
Existing standalone GUIs retain their own temporary credential fields.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from .models import Credentials


class SessionCredentialStore:
    """Hold credentials only for the lifetime of this application instance."""

    def __init__(self) -> None:
        self._credentials: Credentials | None = None

    @property
    def configured(self) -> bool:
        return self._credentials is not None

    @property
    def username(self) -> str | None:
        credentials = self._credentials
        return credentials.username if credentials is not None else None

    def configure(
        self,
        username: str,
        password: str,
        enable_secret: str | None = None,
    ) -> None:
        # Validate a new complete value before replacing the existing session.
        candidate = Credentials(username.strip(), password, enable_secret or None)
        self._credentials = candidate

    def require(self) -> Credentials:
        credentials = self._credentials
        if credentials is None:
            raise ValueError("SSH 공통 계정을 설정한 뒤 작업을 다시 시작하세요.")
        return credentials

    def clear(self) -> None:
        # Python cannot guarantee zeroization of immutable string contents.
        # Never serialize/store the session, and release our reference on exit.
        self._credentials = None


class SessionCredentialDialog(QDialog):
    """Collect one SSH account without ever displaying the stored password."""

    def __init__(self, parent: QWidget | None = None, *, username: str = "") -> None:
        super().__init__(parent)
        self._accepted_credentials: Credentials | None = None
        self.setWindowTitle("SSH 공통 계정 설정")
        self.setObjectName("sshSessionCredentialDialog")
        self.setModal(True)
        self.setMinimumWidth(390)
        layout = QVBoxLayout(self)
        explanation = QLabel(
            "이 계정은 현재 실행 중인 프로그램에서만 사용됩니다. "
            "컨피그 백업·포트 조회·PoE 제어에 공통으로 적용되며 파일에는 저장하지 않습니다.",
            self,
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        form = QFormLayout()
        self.username_input = QLineEdit(username, self)
        self.username_input.setObjectName("sessionSshUsername")
        self.password_input = QLineEdit(self)
        self.password_input.setObjectName("sessionSshPassword")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.enable_password_input = QLineEdit(self)
        self.enable_password_input.setObjectName("sessionEnablePassword")
        self.enable_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.enable_password_input.setPlaceholderText("필요한 경우에만 입력")
        form.addRow("사용자 이름", self.username_input)
        form.addRow("SSH 비밀번호", self.password_input)
        form.addRow("Enable 암호", self.enable_password_input)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.save_button.setText("현재 실행에 적용")
        buttons.accepted.connect(self._accept_credentials)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept_credentials(self) -> None:
        try:
            candidate = Credentials(
                username=self.username_input.text().strip(),
                password=self.password_input.text(),
                enable_secret=self.enable_password_input.text() or None,
            )
        except ValueError:
            QMessageBox.warning(self, "SSH 계정 확인", "사용자 이름과 비밀번호를 입력하세요.")
            return
        self._accepted_credentials = candidate
        self._clear_secret_fields()
        super().accept()

    def take_credentials(self) -> Credentials | None:
        credentials = self._accepted_credentials
        self._accepted_credentials = None
        return credentials

    def _clear_secret_fields(self) -> None:
        self.password_input.clear()
        self.enable_password_input.clear()

    def reject(self) -> None:
        self._accepted_credentials = None
        self._clear_secret_fields()
        super().reject()

    def closeEvent(self, event: object) -> None:
        self._clear_secret_fields()
        super().closeEvent(event)  # type: ignore[arg-type]
