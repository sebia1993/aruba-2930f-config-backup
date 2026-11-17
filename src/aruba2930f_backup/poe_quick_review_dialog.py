"""Operator review for guarded quick PoE ON/OFF after read-only requery."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .poe_query_models import PoEQueryResult
from .port_control import PortAction


class QuickPoEReviewDialog(QDialog):
    """Require an explicit action-specific approval, including skipped targets."""

    def __init__(
        self,
        action: PortAction,
        ready: Sequence[PoEQueryResult],
        excluded: Sequence[tuple[str, str, str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if action not in (PortAction.POE_ON, PortAction.POE_OFF):
            raise ValueError("Quick review is only for PoE ON/OFF")
        self.setObjectName("quickPoEReviewDialog")
        self.setWindowTitle("PoE 일괄 제어 · 실행 대상 검토")
        self.resize(980, 620)
        self.setMinimumSize(690, 420)

        title = "PoE OFF" if action is PortAction.POE_OFF else "PoE ON"
        selected = len(ready) + len(excluded)
        layout = QVBoxLayout(self)
        self.summary = QLabel(
            f"요청: {title} · 전체 {selected}포트 · 실행 가능 {len(ready)}포트 · "
            f"제외 {len(excluded)}포트",
            self,
        )
        self.summary.setObjectName("quickPoEReviewSummary")
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        message = QLabel(
            "표의 실행 가능 포트에만 변경 명령이 전송됩니다. 제외 포트는 변경하지 않습니다. "
            "실행 직전 SSH 지문·호스트네임·포트 설정을 다시 검증하며, "
            "결과가 불확실하면 자동 재전송하지 않습니다. "
            + (
                "PoE OFF는 연결된 AP 서비스를 중단시킬 수 있습니다."
                if action is PortAction.POE_OFF
                else "PoE ON 후에도 AP 등록 및 무선 서비스 정상화 여부를 별도 확인해야 합니다."
            ),
            self,
        )
        message.setTextFormat(Qt.TextFormat.PlainText)
        message.setWordWrap(True)
        layout.addWidget(message)

        self.table = QTreeWidget(self)
        self.table.setObjectName("quickPoEReviewTargets")
        self.table.setHeaderLabels(["스위치 IP:SSH", "포트", "계획 대상", "근거 / 제외 사유"])
        self.table.setRootIsDecorated(False)
        self.table.setAlternatingRowColors(True)
        self.table.setUniformRowHeights(True)
        self.table.setColumnWidth(0, 210)
        self.table.setColumnWidth(1, 95)
        self.table.setColumnWidth(2, 110)
        self.table.setColumnWidth(3, 480)
        for result in ready:
            QTreeWidgetItem(
                self.table,
                [
                    result.group.target.endpoint,
                    result.interface,
                    "실행 가능",
                    "조회 완료 / 실행 직전 상태 재검증",
                ],
            )
        for endpoint, interface, reason in excluded:
            item = QTreeWidgetItem(self.table, [endpoint, interface, "제외", reason])
            item.setToolTip(3, reason)
        layout.addWidget(self.table, 1)

        self.exclusion_ack = QCheckBox(
            f"제외된 {len(excluded)}포트는 변경하지 않고 "
            f"실행 가능한 {len(ready)}포트만 처리하는 데 동의합니다.",
            self,
        )
        self.exclusion_ack.setObjectName("quickPoEAcknowledgeExcluded")
        self.exclusion_ack.setVisible(bool(excluded))
        self.exclusion_ack.toggled.connect(self._update_approval)
        layout.addWidget(self.exclusion_ack)

        # Quick actions intentionally have no repeated 10-port popups. For
        # larger plans require an explicit second acknowledgment *in this
        # single target-review dialog* before automatic chunk continuation.
        self.large_action_ack = QCheckBox(
            f"{len(ready)}개 포트가 순차적으로 실행되며 10개마다 재확인 창이 "
            "나타나지 않는다는 점을 확인했습니다.",
            self,
        )
        self.large_action_ack.setObjectName("quickPoEAcknowledgeLargeAction")
        self.large_action_ack.setVisible(len(ready) > 10)
        self.large_action_ack.toggled.connect(self._update_approval)
        layout.addWidget(self.large_action_ack)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        self.approve_button = QPushButton(f"확인 후 {len(ready)}포트 {title} 실행", self)
        self.approve_button.setObjectName("quickPoEApprove")
        buttons.addButton(self.approve_button, QDialogButtonBox.ButtonRole.AcceptRole)
        self.approve_button.clicked.connect(self._approve)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._ready_count = len(ready)
        self._blocked_count = len(excluded)
        self._update_approval()

    def _update_approval(self, _checked: bool = False) -> None:
        self.approve_button.setEnabled(
            self._ready_count > 0
            and (self._blocked_count == 0 or self.exclusion_ack.isChecked())
            and (self._ready_count <= 10 or self.large_action_ack.isChecked())
        )

    def _approve(self) -> None:
        if self.approve_button.isEnabled():
            self.accept()
