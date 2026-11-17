"""Operator-friendly, fail-closed Excel two-column paste preview dialog."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .poe_query_models import PoETarget
from .poe_targets import merge_targets, parse_pasted_targets


class PortTargetPasteDialog(QDialog):
    """Preview a complete 2-column clipboard block before changing any target."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        existing_targets: Callable[[], tuple[PoETarget, ...]],
    ) -> None:
        super().__init__(parent)
        self._existing_targets = existing_targets
        self.proposed_targets: tuple[PoETarget, ...] | None = None
        self.setWindowTitle("스위치 IP · 포트 2열 붙여넣기")
        self.resize(1120, 750)
        self.setMinimumSize(720, 480)
        layout = QVBoxLayout(self)
        explanation = QLabel(
            "엑셀의 '스위치 IP / 포트' 두 열을 선택하고 Ctrl+C → Ctrl+V 하세요.\n"
            "예: 192.0.2.10 [TAB] 12 또는 같은 IP의 12,24 · 기본 SSH 포트 22.\n"
            "기존 3~4열 형식도 지원하며, 입력 오류 시 기존 목록은 변경되지 않습니다.",
            self,
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("적용 방식:", self))
        self.mode = QComboBox(self)
        self.mode.setObjectName("pasteTargetMode")
        self.mode.addItem("기존 목록 교체 (기본)", "replace")
        self.mode.addItem("기존 목록에 추가", "append")
        toolbar.addWidget(self.mode, 1)
        clipboard_button = QPushButton("클립보드에서 가져오기", self)
        clipboard_button.setObjectName("pasteFromClipboard")
        clipboard_button.clicked.connect(self._from_clipboard)
        toolbar.addWidget(clipboard_button)
        layout.addLayout(toolbar)

        self.pasted_text = QPlainTextEdit(self)
        self.pasted_text.setObjectName("pasteTwoColumnText")
        self.pasted_text.setPlaceholderText(
            "스위치 IP\t포트\n192.0.2.10\t12\n192.0.2.10\t24\n192.0.2.20\t1/12,1/24"
        )
        self.pasted_text.setMinimumWidth(270)
        self.pasted_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.preview_status = QLabel("표를 붙여넣으면 대상 개수와 오류를 확인합니다.", self)
        self.preview_status.setObjectName("pastePreviewStatus")
        self.preview_status.setWordWrap(True)
        self.preview_table = QPlainTextEdit(self)
        self.preview_table.setObjectName("pastePreviewTable")
        self.preview_table.setReadOnly(True)
        self.preview_table.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.preview_table.setPlaceholderText("스위치 IP | 포트 목록 미리보기")
        self.preview_table.setMinimumWidth(270)

        # Side-by-side editors make large Excel paste blocks and the validated
        # preview visible simultaneously; the splitter is user-resizable.
        input_panel = QWidget(self)
        input_layout = QVBoxLayout(input_panel)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.addWidget(QLabel("① 엑셀 IP · 포트 입력", input_panel))
        input_layout.addWidget(self.pasted_text, 1)
        preview_panel = QWidget(self)
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.addWidget(QLabel("② 검증된 대상 미리보기", preview_panel))
        preview_layout.addWidget(self.preview_table, 1)
        self.paste_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.paste_splitter.setObjectName("pastePreviewSplitter")
        self.paste_splitter.setChildrenCollapsible(False)
        self.paste_splitter.addWidget(input_panel)
        self.paste_splitter.addWidget(preview_panel)
        self.paste_splitter.setSizes([560, 560])
        layout.addWidget(self.paste_splitter, 1)
        layout.addWidget(self.preview_status)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        self.apply_button = QPushButton("검증된 대상 적용", self)
        self.apply_button.setObjectName("pasteApplyTargets")
        self.apply_button.setEnabled(False)
        buttons.addButton(self.apply_button, QDialogButtonBox.ButtonRole.AcceptRole)
        self.apply_button.clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._validation_timer = QTimer(self)
        self._validation_timer.setSingleShot(True)
        self._validation_timer.setInterval(160)
        self._validation_timer.timeout.connect(self._update_preview)
        self.pasted_text.textChanged.connect(self._schedule_preview)
        self.mode.currentIndexChanged.connect(self._schedule_preview)

    @Slot()
    def _from_clipboard(self) -> None:
        clipboard = QApplication.clipboard()
        self.pasted_text.setPlainText(clipboard.text())
        self._update_preview()

    @Slot()
    def _schedule_preview(self) -> None:
        self.proposed_targets = None
        self.apply_button.setEnabled(False)
        self._validation_timer.start()

    def evaluate(self) -> tuple[PoETarget, ...]:
        parsed = parse_pasted_targets(self.pasted_text.toPlainText())
        if self.mode.currentData() == "append":
            return merge_targets((*self._existing_targets(), *parsed))
        return parsed

    @Slot()
    def _update_preview(self) -> None:
        try:
            groups = self.evaluate()
        except ValueError as exc:
            self.proposed_targets = None
            self.apply_button.setEnabled(False)
            self.preview_status.setText(f"입력 확인 필요: {exc}")
            self.preview_table.clear()
            return
        self.proposed_targets = groups
        self.apply_button.setEnabled(True)
        total = sum(len(group.interfaces) for group in groups)
        self.preview_status.setText(
            f"검증 완료 · 스위치 {len(groups)}대 · 조회할 포트 {total}개 · "
            f"적용 방식: {self.mode.currentText()}\n"
            "확인 버튼을 눌러야 현재 대상 목록에 반영됩니다."
        )
        preview = [
            f"{group.target.ip}:{group.target.port}\t{','.join(group.interfaces)}"
            for group in groups
        ]
        self.preview_table.setPlainText("\n".join(preview))

    def accept(self) -> None:
        self._validation_timer.stop()
        self._update_preview()
        if self.proposed_targets is not None:
            super().accept()
