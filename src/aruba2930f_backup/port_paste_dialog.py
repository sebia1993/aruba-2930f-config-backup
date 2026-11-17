"""Operator-friendly, fail-closed Excel two-column paste preview dialog."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .poe_query_models import PoETarget
from .poe_targets import merge_targets, parse_pasted_targets


class PortTargetPasteDialog(QDialog):
    """Review the entire parsed target list before applying it atomically."""

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
        self.resize(1100, 720)
        self.setMinimumSize(760, 500)
        layout = QVBoxLayout(self)
        explanation = QLabel(
            "엑셀의 '스위치 IP / 포트' 두 열을 선택해 Ctrl+C → Ctrl+V 하세요.\n"
            "왼쪽에 입력하고 오른쪽에서 전체 스위치·포트를 검증합니다. "
            "입력 오류가 있으면 기존 대상은 변경되지 않습니다.",
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

        # Side-by-side input and preview use the available height rather than
        # competing for a short stacked region. The divider is user-resizable.
        self.paste_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.paste_splitter.setObjectName("portPasteReviewSplitter")
        self.paste_splitter.setChildrenCollapsible(False)

        input_panel = QWidget(self.paste_splitter)
        input_layout = QVBoxLayout(input_panel)
        input_layout.setContentsMargins(4, 4, 4, 4)
        input_layout.addWidget(QLabel("① 엑셀 원본 · IP / 포트 목록", input_panel))
        self.pasted_text = QPlainTextEdit(input_panel)
        self.pasted_text.setObjectName("pasteTwoColumnText")
        self.pasted_text.setPlaceholderText(
            "스위치 IP\t포트\n192.0.2.10\t12\n192.0.2.10\t24\n192.0.2.20\t1/12,1/24"
        )
        self.pasted_text.setTabStopDistance(145.0)
        input_layout.addWidget(self.pasted_text, 1)
        self.paste_splitter.addWidget(input_panel)

        preview_panel = QWidget(self.paste_splitter)
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(4, 4, 4, 4)
        preview_layout.addWidget(QLabel("② 검증된 스위치 · 포트 미리보기", preview_panel))
        self.preview_status = QLabel(
            "표를 붙여넣으면 대상 개수와 오류를 확인합니다.", preview_panel
        )
        self.preview_status.setObjectName("pastePreviewStatus")
        self.preview_status.setWordWrap(True)
        preview_layout.addWidget(self.preview_status)
        self.preview_table = QTableWidget(0, 4, preview_panel)
        self.preview_table.setObjectName("pastePreviewTable")
        self.preview_table.setHorizontalHeaderLabels(
            ("스위치 IP", "SSH", "인터페이스 목록", "포트 수")
        )
        self.preview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.preview_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.preview_table.setAlternatingRowColors(True)
        self.preview_table.setWordWrap(False)
        preview_header = self.preview_table.horizontalHeader()
        preview_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        preview_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.preview_table.setColumnWidth(0, 145)
        self.preview_table.setColumnWidth(1, 70)
        self.preview_table.setColumnWidth(3, 70)
        preview_layout.addWidget(self.preview_table, 1)
        self.paste_splitter.addWidget(preview_panel)
        self.paste_splitter.setSizes([460, 600])
        layout.addWidget(self.paste_splitter, 1)

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
        self.pasted_text.setPlainText(QApplication.clipboard().text())
        self._update_preview()

    @Slot()
    def _schedule_preview(self) -> None:
        # Never leave a previously valid plan actionable after the user edits.
        self.proposed_targets = None
        self.apply_button.setEnabled(False)
        self.preview_table.setRowCount(0)
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
            self.preview_table.setRowCount(0)
            return

        total = sum(len(group.interfaces) for group in groups)
        self.preview_status.setText(
            f"검증 완료 · 스위치 {len(groups)}대 · 조회할 포트 {total}개 · "
            f"적용 방식: {self.mode.currentText()}\n"
            "모든 대상은 아래 표에서 스크롤로 확인할 수 있으며, 적용 전에는 기존 목록이 유지됩니다."
        )
        self.preview_table.setUpdatesEnabled(False)
        try:
            # Show every validated switch instead of truncating at 40 groups.
            self.preview_table.setRowCount(len(groups))
            for row, group in enumerate(groups):
                interfaces = ",".join(group.interfaces)
                fields = (
                    group.target.ip,
                    str(group.target.port),
                    interfaces,
                    str(len(group.interfaces)),
                )
                for column, value in enumerate(fields):
                    item = QTableWidgetItem(value)
                    if column == 2:
                        item.setToolTip(interfaces)
                    if column == 0 and group.label:
                        item.setToolTip(f"이름: {group.label}")
                    self.preview_table.setItem(row, column, item)
        finally:
            self.preview_table.setUpdatesEnabled(True)
        self.proposed_targets = groups
        self.apply_button.setEnabled(True)

    def accept(self) -> None:
        self._validation_timer.stop()
        self._update_preview()
        if self.proposed_targets is not None:
            super().accept()
