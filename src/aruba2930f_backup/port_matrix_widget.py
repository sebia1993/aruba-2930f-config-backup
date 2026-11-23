"""Virtualized, read-only port status matrix for the integrated Qt operator UI.

A cell is one *registered interface*, never an entire switch or a physical
48-port front panel. No mutation action is connected to this widget.
"""

from __future__ import annotations

from collections import Counter
from math import ceil
from typing import cast

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QStyle,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .poe_query_models import PoEQueryResult, PoETarget
from .port_status_mapper import (
    STATE_LABELS,
    MatrixPort,
    MatrixState,
    PortKey,
    group_sort_key,
    make_matrix_port,
    port_sort_key,
)

_COLORS_DARK: dict[MatrixState, str] = {
    MatrixState.NORMAL: "#3ca58d",
    MatrixState.ATTENTION: "#c15d67",
    MatrixState.WARNING: "#c6a056",
    MatrixState.OFF: "#528ebc",
    MatrixState.UNKNOWN: "#7d8d9b",
    MatrixState.LOADING: "#8d79b8",
}
_COLORS_LIGHT: dict[MatrixState, str] = {
    MatrixState.NORMAL: "#24947c",
    MatrixState.ATTENTION: "#b34b5a",
    MatrixState.WARNING: "#b18335",
    MatrixState.OFF: "#4f84b5",
    MatrixState.UNKNOWN: "#83919d",
    MatrixState.LOADING: "#826db0",
}


class PortMatrixModel(QAbstractTableModel):
    """Flat keys stored once; reshape only indices for visible grid columns."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._columns = 20
        self._groups: dict[str, PoETarget] = {}
        self._ordered: list[PortKey] = []
        self._visible: list[PortKey] = []
        self._row_by_key: dict[PortKey, int] = {}
        self._ports: dict[PortKey, MatrixPort] = {}

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        if parent.isValid():
            return 0
        return ceil(len(self._visible) / self._columns)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else self._columns

    def key_at(self, index: QModelIndex | QPersistentModelIndex) -> PortKey | None:
        if not index.isValid() or index.row() < 0 or index.column() < 0:
            return None
        offset = index.row() * self._columns + index.column()
        return self._visible[offset] if offset < len(self._visible) else None

    def index_for(self, key: PortKey) -> QModelIndex:
        offset = self._row_by_key.get(key)
        if offset is None:
            return QModelIndex()
        return self.index(offset // self._columns, offset % self._columns)

    def data(
        self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        key = self.key_at(index)
        if key is None:
            return None
        value = self._ports[key]
        if role == Qt.ItemDataRole.UserRole:
            return value
        if role == Qt.ItemDataRole.ToolTipRole:
            return value.tooltip
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{value.title}, {STATE_LABELS[value.state]}"
        return None

    def set_columns(self, value: int) -> None:
        value = max(1, value)
        if value == self._columns:
            return
        self.beginResetModel()
        self._columns = value
        self.endResetModel()

    def set_targets(self, groups: tuple[PoETarget, ...]) -> None:
        self.beginResetModel()
        self._groups = {g.target.endpoint: g for g in groups}
        self._ordered = [
            (g.target.endpoint, interface)
            for g in sorted(groups, key=group_sort_key)
            for interface in sorted(g.interfaces, key=port_sort_key)
        ]
        self._ports = {key: make_matrix_port(self._groups[key[0]], key[1]) for key in self._ordered}
        self._visible = list(self._ordered)
        self._row_by_key = {key: i for i, key in enumerate(self._visible)}
        self.endResetModel()

    def set_visible_keys(self, keys: set[PortKey]) -> None:
        self.beginResetModel()
        self._visible = [key for key in self._ordered if key in keys]
        self._row_by_key = {key: i for i, key in enumerate(self._visible)}
        self.endResetModel()

    def set_loading(self, keys: tuple[PortKey, ...]) -> None:
        for key in keys:
            group = self._groups.get(key[0])
            if group is not None and key in self._ports:
                self._put(key, make_matrix_port(group, key[1], loading=True))

    def update_observation(self, result: PoEQueryResult) -> None:
        key = (result.group.target.endpoint, result.interface)
        group = self._groups.get(key[0])
        if group is not None and key in self._ports:
            self._put(key, make_matrix_port(group, key[1], result))

    def _put(self, key: PortKey, entry: MatrixPort) -> None:
        self._ports[key] = entry
        index = self.index_for(key)
        if index.isValid():
            self.dataChanged.emit(index, index)

    def port(self, key: PortKey) -> MatrixPort | None:
        return self._ports.get(key)

    def counts(self) -> Counter[MatrixState]:
        return Counter(port.state for port in self._ports.values())

    def attention_keys(self, *, limit: int = 12) -> list[PortKey]:
        priorities = (MatrixState.ATTENTION, MatrixState.WARNING)
        found = [key for key in self._ordered if self._ports[key].state in priorities]
        return sorted(
            found, key=lambda key: 0 if self._ports[key].state is MatrixState.ATTENTION else 1
        )[:limit]

    @property
    def total(self) -> int:
        return len(self._ordered)

    @property
    def shown(self) -> int:
        return len(self._visible)


class PortMatrixDelegate(QStyledItemDelegate):
    """Paint tiles on demand instead of allocating a QPushButton per port."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._dark = True

    def set_dark(self, enabled: bool) -> None:
        self._dark = enabled

    def paint(
        self, painter: QPainter, option: object, index: QModelIndex | QPersistentModelIndex
    ) -> None:
        # QStyleOptionViewItem arrives through the PySide6 virtual interface.
        from PySide6.QtWidgets import QStyleOptionViewItem

        if not isinstance(option, QStyleOptionViewItem):
            return
        entry = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry, MatrixPort):
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(option.rect.adjusted(2, 2, -2, -2))
        colors = _COLORS_DARK if self._dark else _COLORS_LIGHT
        painter.setBrush(QColor(colors[entry.state]))
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.setPen(QColor("#f9fafb") if selected else Qt.PenStyle.NoPen)
        if selected:
            pen = painter.pen()
            pen.setWidth(2)
            painter.setPen(pen)
        painter.drawRoundedRect(rect, 3, 3)
        painter.restore()


class PortMatrixView(QTableView):
    def __init__(self, model: PortMatrixModel, parent: QWidget) -> None:
        super().__init__(parent)
        self._tile_px = 28
        self.setObjectName("portStatusMatrixView")
        self.setModel(model)
        self._delegate = PortMatrixDelegate(self)
        self.setItemDelegate(self._delegate)
        self.setShowGrid(False)
        for header in (self.horizontalHeader(), self.verticalHeader()):
            header.hide()
            # The headers have no text. A font-based minimum would enlarge
            # the cells beyond the tile width used to fit the grid.
            header.setMinimumSectionSize(1)
            header.setDefaultSectionSize(self._tile_px)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.setMouseTracking(True)
        self.setMinimumHeight(135)
        self._fit_columns()

    def set_tile_size(self, value: int) -> None:
        self._tile_px = value
        self.horizontalHeader().setDefaultSectionSize(value)
        self.verticalHeader().setDefaultSectionSize(value)
        self._fit_columns()

    def set_dark(self, enabled: bool) -> None:
        self._delegate.set_dark(enabled)
        self.viewport().update()

    def resizeEvent(self, event: object) -> None:
        from PySide6.QtGui import QResizeEvent

        if isinstance(event, QResizeEvent):
            super().resizeEvent(event)
            self._fit_columns()

    def _fit_columns(self) -> None:
        model = self.model()
        if isinstance(model, PortMatrixModel):
            columns = max(1, self.viewport().width() // self._tile_px)
            if columns != model.columnCount():
                focused_port = model.key_at(self.currentIndex())
                model.set_columns(columns)
                if focused_port is not None:
                    self.setCurrentIndex(model.index_for(focused_port))


class PortMatrixPanel(QWidget):
    """Queryable matrix and on-demand details, explicitly disconnected from writes."""

    portActivated = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("portStatusMatrix")
        self.model = PortMatrixModel(self)
        self._current: PortKey | None = None
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(120)
        self._refresh_timer.timeout.connect(self._update_summary)
        root = QHBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(8)
        left = QWidget(self)
        layout = QVBoxLayout(left)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(5)
        header = QHBoxLayout()
        heading = QLabel("포트 상태 매트릭스 · 1블록 = 1지정 포트", left)
        heading.setWordWrap(True)
        header.addWidget(heading, 1)
        self.summary = QLabel("대상 포트 없음", left)
        self.summary.setObjectName("portMatrixSummary")
        self.summary.setWordWrap(True)
        header.addWidget(self.summary, 2)
        self.priority_button = QPushButton("우선 확인", left)
        self.priority_button.setObjectName("portMatrixAttention")
        self.priority_button.clicked.connect(self.show_attention)
        header.addWidget(self.priority_button)
        self.zoom = QComboBox(left)
        self.zoom.setObjectName("portMatrixZoom")
        for text, size in (("작게", 22), ("보통", 28), ("크게", 38)):
            self.zoom.addItem(text, size)
        self.zoom.setCurrentIndex(1)
        header.addWidget(self.zoom)
        layout.addLayout(header)

        self.view = PortMatrixView(self.model, left)
        self.zoom.currentIndexChanged.connect(self._change_zoom)
        self.view.clicked.connect(self._tile_clicked)
        layout.addWidget(self.view, 1)
        self.legend = QLabel(
            "■ 링크 Up   ■ 링크 Down·확인 필요   ■ 일부 확인 필요   "
            "■ 설정 OFF   ■ 미수집   ■ 조회 중",
            left,
        )
        self.legend.setObjectName("portMatrixLegend")
        self.legend.setWordWrap(True)
        layout.addWidget(self.legend)
        root.addWidget(left, 1)

        self.details = QFrame(self)
        self.details.setObjectName("portMatrixDetails")
        self.details.setFixedWidth(285)
        side = QVBoxLayout(self.details)
        side.setContentsMargins(10, 8, 10, 8)
        side.setSpacing(6)
        title = QHBoxLayout()
        title.addWidget(QLabel("선택 포트 상세", self.details), 1)
        close = QPushButton("닫기", self.details)
        close.setObjectName("portMatrixDetailsClose")
        close.clicked.connect(self.details.hide)
        title.addWidget(close)
        side.addLayout(title)
        self.details_text = QLabel("포트 블록을 클릭하세요.", self.details)
        self.details_text.setTextFormat(Qt.TextFormat.PlainText)
        self.details_text.setWordWrap(True)
        self.details_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_scroll = QScrollArea(self.details)
        detail_scroll.setObjectName("portMatrixDetailScroll")
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setWidget(self.details_text)
        side.addWidget(detail_scroll, 1)
        side.addWidget(QLabel("우선 확인 포트 (링크 Down / 일부 확인 필요)", self.details))
        self.priorities = QListWidget(self.details)
        self.priorities.setObjectName("portMatrixPriorities")
        self.priorities.setMaximumHeight(120)
        self.priorities.itemClicked.connect(self._priority_clicked)
        side.addWidget(self.priorities)
        root.addWidget(self.details)
        self.details.hide()
        self._update_summary()

    def set_theme(self, mode: str) -> None:
        self.view.set_dark(mode == "dark")
        colors = _COLORS_DARK if mode == "dark" else _COLORS_LIGHT
        spans = [
            f'<span style="color:{colors[state]}">■</span> {STATE_LABELS[state]}'
            for state in MatrixState
        ]
        self.legend.setText("　".join(spans))
        self.legend.setTextFormat(Qt.TextFormat.RichText)

    def set_targets(self, groups: tuple[PoETarget, ...]) -> None:
        self.model.set_targets(groups)
        self._current = None
        self.details.hide()
        self._refresh_timer.start()

    def mark_loading(self, keys: tuple[PortKey, ...]) -> None:
        self.model.set_loading(keys)
        self._refresh_timer.start()

    def update_result(self, result: PoEQueryResult) -> None:
        self.model.update_observation(result)
        if self._current == (result.group.target.endpoint, result.interface):
            self._show_details()
        self._refresh_timer.start()

    def filter_to(self, visible: set[PortKey]) -> None:
        self.model.set_visible_keys(visible)
        if self._current is not None:
            index = self.model.index_for(self._current)
            if index.isValid():
                self.view.setCurrentIndex(index)
        self._refresh_timer.start()

    def _change_zoom(self, _index: int) -> None:
        self.view.set_tile_size(int(self.zoom.currentData()))

    def _tile_clicked(self, index: QModelIndex) -> None:
        key = self.model.key_at(index)
        if key is not None:
            self.select_port(key, open_details=True)
            self.portActivated.emit(key)

    def select_port(self, key: PortKey, *, open_details: bool = False) -> None:
        if self.model.port(key) is None:
            return
        self._current = key
        index = self.model.index_for(key)
        if index.isValid():
            self.view.setCurrentIndex(index)
        if open_details:
            self.details.show()
        self._show_details()

    def _show_details(self) -> None:
        entry = self.model.port(self._current) if self._current else None
        if entry is None:
            self.details_text.setText("지정 포트 블록을 선택하세요.")
            return
        self.details_text.setText(
            f"스위치: {entry.switch}\n"
            f"IP: {entry.ip}\n포트: {entry.interface}\n\n"
            f"분류: {STATE_LABELS[entry.state]}\n"
            f"Link: {entry.link}\nPoE 설정: {entry.poe}\n"
            f"포트 실측 전력: {entry.watts}\n"
            f"LLDP AP: {entry.ap}\n"
            f"관측 시각: {entry.observed_at or '미확인'}\n\n"
            f"{entry.note or '참고: 이 화면은 조회 전용입니다.'}"
        )

    def show_attention(self) -> None:
        keys = self.model.attention_keys()
        if keys:
            self.select_port(keys[0], open_details=True)
            self.portActivated.emit(keys[0])
        else:
            self.details.show()
            self.details_text.setText(
                "현재 조회 결과에서 링크 Down 또는 일부 확인 필요로 분류된 포트가 없습니다. "
                "미수집 및 설정 OFF 항목은 위 상태 요약에서 따로 확인하세요."
            )

    def _priority_clicked(self, item: QListWidgetItem) -> None:
        key = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(key, tuple) and len(key) == 2:
            self.select_port(cast(PortKey, key), open_details=True)
            self.portActivated.emit(key)

    def _update_summary(self) -> None:
        counts = self.model.counts()
        self.summary.setText(
            f"전체 {self.model.total:,} · 표시 {self.model.shown:,} · "
            f"Up {counts[MatrixState.NORMAL]:,} · "
            f"Down {counts[MatrixState.ATTENTION]:,} · "
            f"주의 {counts[MatrixState.WARNING]:,} · "
            f"OFF {counts[MatrixState.OFF]:,} · "
            f"미수집 {counts[MatrixState.UNKNOWN]:,}"
        )
        self.priorities.blockSignals(True)
        try:
            self.priorities.clear()
            for key in self.model.attention_keys():
                entry = self.model.port(key)
                if entry is None:
                    continue
                item = QListWidgetItem(
                    f"{entry.switch} · {entry.ip} / {entry.interface} · {STATE_LABELS[entry.state]}"
                )
                item.setData(Qt.ItemDataRole.UserRole, key)
                self.priorities.addItem(item)
        finally:
            self.priorities.blockSignals(False)
