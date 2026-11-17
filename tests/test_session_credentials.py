"""Session-only authentication for the integrated Aruba operator shell."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QFormLayout, QLineEdit

from aruba2930f_backup.port_control_gui import PortControlDialog
from aruba2930f_backup.session_credentials import (
    SessionCredentialDialog,
    SessionCredentialStore,
)


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def test_store_is_session_only_and_repr_does_not_leak_passwords() -> None:
    store = SessionCredentialStore()
    with pytest.raises(ValueError, match="공통 계정"):
        store.require()
    store.configure(" network-operator ", "super-secret", "enable-secret")
    assert store.configured
    assert store.username == "network-operator"
    credential = store.require()
    assert credential.username == "network-operator"
    assert credential.password == "super-secret"
    assert credential.enable_secret == "enable-secret"
    assert "super-secret" not in repr(credential)
    assert "enable-secret" not in repr(credential)
    with pytest.raises(ValueError):
        store.configure("", "bad-username")
    assert store.require() is credential
    store.clear()
    assert not store.configured
    assert store.username is None
    with pytest.raises(ValueError):
        store.require()
    # Creating a new process-local store cannot restore previous credentials.
    assert not SessionCredentialStore().configured


def test_dialog_accepts_once_and_clears_secret_fields(app: QApplication) -> None:
    dialog = SessionCredentialDialog(username="network-operator")
    assert dialog.username_input.text() == "network-operator"
    assert dialog.password_input.text() == ""
    assert dialog.enable_password_input.text() == ""
    assert dialog.password_input.echoMode() is QLineEdit.EchoMode.Password
    assert dialog.enable_password_input.echoMode() is QLineEdit.EchoMode.Password
    dialog.password_input.setText("super-secret")
    dialog.enable_password_input.setText("enable-secret")
    dialog.save_button.click()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.password_input.text() == ""
    assert dialog.enable_password_input.text() == ""
    credentials = dialog.take_credentials()
    assert credentials is not None
    assert credentials.username == "network-operator"
    assert credentials.password == "super-secret"
    assert credentials.enable_secret == "enable-secret"
    assert dialog.take_credentials() is None
    dialog.close()


def test_dialog_reject_discards_unsaved_password(app: QApplication) -> None:
    dialog = SessionCredentialDialog()
    dialog.password_input.setText("uncommitted-secret")
    dialog.enable_password_input.setText("uncommitted-enable")
    dialog.reject()
    assert dialog.password_input.text() == ""
    assert dialog.enable_password_input.text() == ""
    assert dialog.take_credentials() is None
    dialog.close()


def test_single_port_dialog_session_provider_has_no_credential_copies(
    app: QApplication,
) -> None:
    store = SessionCredentialStore()
    store.configure("operator", "session-password", "enable-secret")
    dialog = PortControlDialog(
        ip="192.0.2.10",
        credential_provider=store.require,
    )
    assert dialog._credentials() is store.require()
    assert dialog.username_input.text() == ""
    assert dialog.password_input.text() == ""
    assert dialog.enable_password_input.text() == ""
    form = dialog.username_input.parentWidget().layout()
    assert isinstance(form, QFormLayout)
    assert not form.isRowVisible(dialog.username_input)
    assert not form.isRowVisible(dialog.password_input)
    assert not form.isRowVisible(dialog.enable_password_input)
    store.clear()
    with pytest.raises(ValueError, match="공통 계정"):
        dialog._credentials()
    dialog.close()


def test_legacy_single_port_keeps_original_temporary_credentials(
    app: QApplication,
) -> None:
    dialog = PortControlDialog(
        ip="192.0.2.10",
        username="legacy-user",
        password="test-password",
        enable_password="legacy-enable",
    )
    credentials = dialog._credentials()
    assert credentials.username == "legacy-user"
    assert credentials.password == "test-password"
    assert credentials.enable_secret == "legacy-enable"
    form = dialog.username_input.parentWidget().layout()
    assert isinstance(form, QFormLayout)
    assert form.isRowVisible(dialog.username_input)
    dialog.close()
    assert dialog.password_input.text() == ""
