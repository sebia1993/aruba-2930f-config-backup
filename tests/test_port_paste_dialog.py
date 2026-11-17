"""Offline two-column Excel paste preview and atomic apply checks."""

from __future__ import annotations

import os

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from aruba2930f_backup.poe_targets import make_target
from aruba2930f_backup.port_paste_dialog import PortTargetPasteDialog

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def test_default_replace_ignores_invalid_existing_rows(
    app: QApplication,
) -> None:
    def malformed_existing() -> tuple:
        raise ValueError("기존 행에 포트가 없습니다.")

    dialog = PortTargetPasteDialog(existing_targets=malformed_existing)
    dialog.pasted_text.setPlainText(
        "스위치 IP\t포트\n192.0.2.10\t12\n192.0.2.10\t24\n192.0.2.20\t1/12"
    )
    dialog._update_preview()
    assert dialog.apply_button.isEnabled()
    assert "스위치 2대" in dialog.preview_status.text()
    assert "포트 3개" in dialog.preview_status.text()
    assert dialog.proposed_targets is not None
    assert dialog.proposed_targets[0].interfaces == ("12", "24")
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    dialog.close()


def test_explicit_append_preserves_existing_ssh_port_and_profile(
    app: QApplication,
) -> None:
    existing = make_target("192.0.2.10", "2222", "1/12", "기존 이름")
    dialog = PortTargetPasteDialog(existing_targets=lambda: (existing,))
    dialog.mode.setCurrentIndex(dialog.mode.findData("append"))
    dialog.pasted_text.setPlainText("192.0.2.10\t24")
    dialog._update_preview()
    groups = dialog.proposed_targets
    assert groups is not None
    assert [(g.target.port, g.interfaces, g.label) for g in groups] == [
        (2222, ("1/12",), "기존 이름"),
        (22, ("24",), ""),
    ]
    dialog.close()


def test_invalid_paste_cannot_be_accepted_or_mutate_existing_plan(
    app: QApplication,
) -> None:
    existing = make_target("192.0.2.10", 22, "12")
    dialog = PortTargetPasteDialog(existing_targets=lambda: (existing,))
    dialog.pasted_text.setPlainText("192.0.2.10\t24\n192.0.2.20\tall")
    dialog._update_preview()
    assert not dialog.apply_button.isEnabled()
    assert dialog.proposed_targets is None
    assert "2행" in dialog.preview_status.text()
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert existing.interfaces == ("12",)
    dialog.close()


def test_append_with_invalid_existing_requires_replace_not_silent_discard(
    app: QApplication,
) -> None:
    dialog = PortTargetPasteDialog(
        existing_targets=lambda: (_ for _ in ()).throw(ValueError("기존 목록 오류"))
    )
    dialog.mode.setCurrentIndex(dialog.mode.findData("append"))
    dialog.pasted_text.setPlainText("192.0.2.10\t12")
    dialog._update_preview()
    assert not dialog.apply_button.isEnabled()
    assert "기존 목록 오류" in dialog.preview_status.text()
    dialog.mode.setCurrentIndex(dialog.mode.findData("replace"))
    dialog._update_preview()
    assert dialog.apply_button.isEnabled()
    dialog.close()


def test_enlarged_paste_window_and_complete_large_preview(app: QApplication) -> None:
    from PySide6.QtCore import Qt

    dialog = PortTargetPasteDialog(existing_targets=lambda: ())
    assert dialog.width() >= 1000
    assert dialog.paste_splitter.orientation() == Qt.Orientation.Horizontal
    assert dialog.pasted_text.parentWidget() is not None
    source = "\n".join(f"192.0.2.{ip}\t12" for ip in range(1, 61))
    dialog.pasted_text.setPlainText(source)
    dialog._update_preview()
    assert dialog.apply_button.isEnabled()
    assert "스위치 60대" in dialog.preview_status.text()
    assert "192.0.2.60:22" in dialog.preview_table.toPlainText()
    assert "나머지" not in dialog.preview_table.toPlainText()
    dialog.close()
