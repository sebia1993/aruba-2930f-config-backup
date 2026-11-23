"""Bounded read-only PySide6 view of PoE query and guarded mutation progress."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


class PortProgressPanel(QFrame):
    """Operator feedback only; never initiates network operations."""

    MAX_DETAIL_ROWS = 300

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("portPinnedProgress")
        self.setStyleSheet(
            "QFrame#portPinnedProgress { border: 1px solid palette(mid); "
            "border-radius: 7px; background: palette(base); }"
        )
        self._mode = ""
        self._action = ""
        self._total = 0
        self._done = 0
        self._selected = 0
        self._already_on = 0
        self._statuses: dict[str, int] = {}
        self._completed: set[tuple[str, str]] = set()
        self._rows: dict[tuple[str, str], QTreeWidgetItem] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(9, 6, 9, 6)
        outer.setSpacing(4)
        top = QHBoxLayout()
        self.header = QLabel("작업 상태 · 대기", self)
        self.header.setObjectName("portProgressHeader")
        top.addWidget(self.header, 1)
        self.details_toggle = QToolButton(self)
        self.details_toggle.setText("포트별 상세 보기")
        self.details_toggle.setObjectName("portProgressDetailsToggle")
        self.details_toggle.setCheckable(True)
        top.addWidget(self.details_toggle)
        outer.addLayout(top)
        self._header_layout = top
        self._body_layout = outer

        self._compact = False
        self._summary = QWidget(self)
        summary = QVBoxLayout(self._summary)
        summary.setContentsMargins(0, 0, 0, 0)
        summary.setSpacing(4)
        self.stage = QLabel(
            "전체 조회 또는 PoE 작업을 시작하면 진행 상황이 표시됩니다.", self._summary
        )
        self.stage.setWordWrap(True)
        self.stage.setObjectName("portProgressStage")
        summary.addWidget(self.stage)
        self.progress = QProgressBar(self._summary)
        self.progress.setObjectName("portProgressBar")
        self.progress.setTextVisible(True)
        self.progress.hide()
        summary.addWidget(self.progress)
        outer.addWidget(self._summary)
        self.counts = QLabel("작업 대기", self)
        self.counts.setObjectName("portProgressCounts")
        self.counts.setWordWrap(True)
        outer.addWidget(self.counts)

        self.details_tree = QTreeWidget(self)
        self.details_tree.setObjectName("portProgressDetails")
        self.details_tree.setHeaderLabels(["스위치 / 포트", "현재 단계", "결과"])
        self.details_tree.setRootIsDecorated(False)
        self.details_tree.setMinimumHeight(100)
        self.details_tree.setMaximumHeight(200)
        self.details_tree.hide()
        outer.addWidget(self.details_tree)
        self.details_toggle.toggled.connect(self.details_tree.setVisible)
        self.details_toggle.toggled.connect(self._show_summary)

    def set_compact(self, compact: bool) -> None:
        self._compact = compact
        if compact:
            self._header_layout.insertWidget(1, self.counts, 3)
        else:
            self._body_layout.insertWidget(
                self._body_layout.indexOf(self.details_tree), self.counts
            )
        self._show_summary(self.details_toggle.isChecked())

    def _show_summary(self, expanded: bool) -> None:
        self._summary.setVisible(expanded or not self._compact)

    def _reset(self) -> None:
        self._mode = ""
        self._action = ""
        self._total = 0
        self._done = 0
        self._selected = 0
        self._already_on = 0
        self._statuses.clear()
        self._completed.clear()
        self._rows.clear()
        self.details_tree.clear()
        self.details_toggle.setChecked(False)
        self.progress.hide()

    def _detail(self, endpoint: str, interface: str, phase: str, result: str = "") -> None:
        key = (endpoint, interface)
        item = self._rows.get(key)
        if item is None:
            if len(self._rows) >= self.MAX_DETAIL_ROWS:
                return
            item = QTreeWidgetItem([f"{endpoint} / {interface}", phase, result])
            self.details_tree.addTopLevelItem(item)
            self._rows[key] = item
        else:
            item.setText(1, phase)
            item.setText(2, result)
        if result:
            item.setToolTip(2, result)

    def start_query(self, total: int, *, preflight: bool = False, action: str = "") -> None:
        self._reset()
        self._mode = "query"
        self._action = action
        self._total = total
        title = f"{action} · 사전 재조회 중" if preflight else "포트 전체 조회 중"
        self.header.setText(title)
        self.stage.setText("조회 중 · 포트 상태와 LLDP 결과를 확인하고 있습니다.")
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(0)
        self.progress.show()
        self.counts.setText(f"0/{total}포트 조회 완료 · 변경 명령 미전송")

    def update_query(self, done: int, total: int, endpoint: str = "", interface: str = "") -> None:
        if self._mode != "query":
            return
        self._done = min(max(done, 0), total)
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(self._done)
        self.counts.setText(f"{self._done}/{total}포트 조회 완료 · 변경 명령 미전송")
        if endpoint and interface:
            self.stage.setText(f"최근 조회: {endpoint} / {interface} · 상태 판별 중")
            self._detail(endpoint, interface, "조회 완료", "설정 변경 없음")

    def finish_query(self, done: int, total: int, failed: int = 0) -> None:
        if self._mode != "query":
            return
        self.update_query(done, total)
        self.header.setText(
            f"{self._action} · 사전 재조회 완료" if self._action else "전체 조회 완료"
        )
        self.stage.setText("조회가 종료됐습니다. 포트 변경 명령은 보내지 않았습니다.")
        self.counts.setText(f"조회 {done}/{total} · 실패/취소 {failed} · 변경 명령 미전송")

    def start_batch(
        self,
        action: str,
        total: int,
        *,
        selected: int,
        already_on: int = 0,
        skipped: Sequence[tuple[str, str]] = (),
    ) -> None:
        self._reset()
        self._mode = "batch"
        self._action = action
        self._total = total
        self._selected = selected
        self._already_on = already_on
        self.header.setText(f"{action} · 실행 중")
        self.stage.setText("첫 포트의 SSH 연결 및 실행 전 검증 대기 중")
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(0)
        self.progress.show()
        for endpoint, interface in skipped[: self.MAX_DETAIL_ROWS]:
            self._detail(endpoint, interface, "명령 생략", "이미 ON")
        self._update_batch_counts()

    def _update_batch_counts(self) -> None:
        self.counts.setText(
            f"선택 {self._selected} · 처리 {self._done}/{self._total} · "
            f"이미 ON {self._already_on} · 확인 성공 {self._statuses.get('verified', 0)} · "
            f"이미 설정 {self._statuses.get('unchanged', 0)} · "
            f"검증 실패 {self._statuses.get('preflight_failed', 0)} · "
            f"확인 불가 {self._statuses.get('unverified', 0)} · "
            f"미실행 {self._statuses.get('paused', 0) + self._statuses.get('cancelled', 0)}"
        )

    def mark_stage(self, endpoint: str, interface: str, phase: str) -> None:
        if self._mode != "batch":
            return
        label = {
            "preflight": "SSH 접속·사전 검증 중",
            "apply": "장비 재검증·설정 적용 중",
        }.get(phase, "상태 확인 중")
        self.stage.setText(f"현재 처리: {endpoint} / {interface} · {label}")
        self._detail(endpoint, interface, label)

    def record_result(self, endpoint: str, interface: str, status: str, message: str) -> None:
        if self._mode != "batch":
            return
        key = (endpoint, interface)
        if key in self._completed:
            return
        self._completed.add(key)
        self._done += 1
        self._statuses[status] = self._statuses.get(status, 0) + 1
        labels = {
            "verified": "변경 확인",
            "unchanged": "이미 설정",
            "preflight_failed": "검증 실패",
            "unverified": "확인 불가",
            "paused": "미실행",
            "cancelled": "취소",
        }
        label = labels.get(status, "결과 확인")
        self.progress.setValue(min(self._done, self._total))
        self.stage.setText(f"최근 결과: {endpoint} / {interface} · {label}")
        self._detail(endpoint, interface, label, message)
        self._update_batch_counts()

    def finish_batch(self, summary: str, *, failed: bool = False) -> None:
        if self._mode != "batch":
            self._mode = "batch"
        self.header.setText(f"{self._action} · {'작업 보류' if failed else '작업 완료'}")
        self.stage.setText(summary)

    def notice(
        self,
        text: str,
        *,
        blocked: bool = False,
        details: Sequence[tuple[str, str, str]] = (),
    ) -> None:
        self._reset()
        self.header.setText("PoE 실행 보류" if blocked else "PoE 확인 완료 · 변경 없음")
        self.stage.setText(text)
        self.counts.setText(
            f"확인 필요 {len(details)}포트 · 변경 명령 미전송" if blocked else "변경 명령 미전송"
        )
        for endpoint, interface, message in details[: self.MAX_DETAIL_ROWS]:
            self._detail(endpoint, interface, "실행 보류", message)
