"""README용 문서 화면을 실제 PySide6 MainWindow에서 생성합니다.

실제 네트워크에 접속하지 않으며 RFC 5737 문서용 IP와 가상 장비명만 사용합니다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFont, QFontDatabase, QFontMetrics, QImage
from PySide6.QtWidgets import QApplication, QTableWidgetItem

from aruba2930f_backup import __version__
from aruba2930f_backup.gui import HostKeyApprovalDialog, MainWindow
from aruba2930f_backup.models import (
    DeviceTarget,
    ErrorCode,
    HostKeyCheck,
    HostKeyObservation,
    HostKeyTrustState,
)


def _apply_documentation_font(app: QApplication) -> None:
    font_path = os.environ.get("DOCS_FONT_PATH", "").strip()
    if not font_path:
        return

    font_id = QFontDatabase.addApplicationFont(font_path)
    if font_id < 0:
        raise RuntimeError(f"문서 화면용 글꼴을 불러올 수 없습니다: {font_path}")
    families = QFontDatabase.applicationFontFamilies(font_id)
    if not families:
        raise RuntimeError("문서 화면용 글꼴에서 font family를 확인할 수 없습니다.")

    font = QFont(families[0], 9)
    if not QFontMetrics(font).inFontUcs4(ord("한")):
        raise RuntimeError(f"문서 화면용 글꼴이 한글을 지원하지 않습니다: {families[0]}")
    app.setFont(font)


def _prepare_window() -> tuple[QApplication, MainWindow]:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Aruba 2930F 설정 백업")
    app.setStyle("Fusion")
    _apply_documentation_font(app)

    window = MainWindow(service=None)
    window.resize(1040, 760)
    window.ip_input.setPlainText("192.0.2.10\n192.0.2.11\n192.0.2.12")
    window.port_input.setValue(22)
    window.username_input.setText("netops-demo")
    window.password_input.setText("documentation-only")
    window.enable_password_input.clear()
    window.concurrency_input.setValue(10)
    window.output_input.setText(r"C:\NetworkBackup\Aruba2930F")
    window.show()
    app.processEvents()
    return app, window


def _save_main_window(output: Path) -> None:
    app, window = _prepare_window()
    window.status_label.setText("대기 중 — 문서용 가상 데이터")
    window.progress_bar.setValue(0)
    app.processEvents()
    if not window.grab().save(str(output / "main-window.png"), "PNG"):
        raise RuntimeError("메인 화면 PNG 저장에 실패했습니다.")
    window.close()
    app.processEvents()


def _save_result_example(output: Path) -> None:
    app, window = _prepare_window()
    window.resize(1040, 880)
    rows = (
        ("192.0.2.10", "LAB-2930F-01", "Aruba 2930F / JL255A", "성공", "지문 1/4 · 백업 1/4", ""),
        ("192.0.2.11", "LAB-2930F-02", "Aruba 2930F / JL256A", "성공", "지문 1/4 · 백업 1/4", ""),
        ("192.0.2.12", "LAB-2930F-03", "Aruba 2930F", "성공", "지문 1/4 · 백업 2/4", ""),
    )
    window.result_table.setRowCount(len(rows))
    for row_index, values in enumerate(rows):
        for column_index, value in enumerate(values):
            window.result_table.setItem(row_index, column_index, QTableWidgetItem(value))
    window.status_label.setText("완료 — 성공 3대 / 실패 0대 · 문서용 가상 결과")
    window.progress_bar.setValue(100)
    window.open_result_button.setEnabled(True)
    app.processEvents()
    if not window.grab().save(str(output / "result-example.png"), "PNG"):
        raise RuntimeError("결과 화면 PNG 저장에 실패했습니다.")
    window.close()
    app.processEvents()


def _save_host_key_review(output: Path) -> None:
    app, window = _prepare_window()
    check = HostKeyCheck(
        observation=HostKeyObservation(
            DeviceTarget("192.0.2.10"),
            "ssh-ed25519",
            "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        ),
        state=HostKeyTrustState.UNKNOWN,
    )
    dialog = HostKeyApprovalDialog([check], parent=window)
    dialog.resize(1040, 640)
    dialog.show()
    app.processEvents()
    if not dialog.grab().save(str(output / "host-key-review.png"), "PNG"):
        raise RuntimeError("SSH 지문 확인 화면 PNG 저장에 실패했습니다.")
    dialog.reject()
    window.close()
    app.processEvents()


def _save_mixed_result(output: Path) -> None:
    app, window = _prepare_window()
    window.resize(1200, 880)
    rows = (
        ("192.0.2.10", "LAB-2930F-01", "Aruba 2930F / JL255A", "성공", "지문 1/4 · 백업 1/4", ""),
        (
            "192.0.2.11",
            "LAB-OTHER-02",
            "합성 비대상 모델",
            "실패",
            "지문 1/4 · 백업 1/4",
            "MODEL_UNSUPPORTED",
        ),
        (
            "192.0.2.12",
            "LAB-2930F-03",
            "Aruba 2930F",
            "재시도 소진",
            "지문 1/4 · 백업 4/4",
            "COMMAND_TIMEOUT",
        ),
    )
    window.result_table.setRowCount(len(rows))
    for row_index, values in enumerate(rows):
        for column_index, value in enumerate(values):
            window.result_table.setItem(row_index, column_index, QTableWidgetItem(value))
    window.status_label.setText("완료 — 성공 1대 / 실패 2대 · 문서용 합성 상태 (실제 수집 없음)")
    window.progress_bar.setValue(100)
    app.processEvents()
    if not window.grab().save(str(output / "mixed-result.png"), "PNG"):
        raise RuntimeError("혼합 결과 화면 PNG 저장에 실패했습니다.")
    window.close()
    app.processEvents()


def _block_network(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("Documentation capture forbids network connections")


def _save_poe_query(output: Path) -> None:
    from aruba2930f_backup.poe_query_gui import PoEQueryDialog
    from aruba2930f_backup.poe_query_models import PoEQueryResult, PoETarget, QueryStatus
    from aruba2930f_backup.port_control import PortSnapshot

    app, window = _prepare_window()
    groups = (
        PoETarget(DeviceTarget("192.0.2.10"), ("12", "24"), "점검 A"),
        PoETarget(DeviceTarget("192.0.2.20", 2222), ("3", "7"), "점검 B"),
    )
    dialog = PoEQueryDialog(window, username="netops-demo")
    dialog.resize(1240, 900)
    dialog._replace_targets(groups)
    dialog._build_results(groups)
    examples = (
        (groups[0], "12", "Up", "Delivering", 14.8, "2026-11-16T12:30:01+00:00"),
        (groups[0], "24", "Down", "Searching", 0.0, "2026-11-16T12:30:02+00:00"),
        (groups[1], "3", "Up", "Delivering", 17.2, "2026-11-16T12:30:01+00:00"),
    )
    for group, interface, link, supply, watts, observed in examples:
        snapshot = PortSnapshot(
            group.target,
            interface,
            f"LAB-2930F-{group.target.ip.rsplit('.', 1)[1]}",
            "Aruba 2930F",
            True,
            link,
            True,
            supply,
            watts,
            observed_at=observed,
        )
        dialog._on_result(
            PoEQueryResult(
                group,
                interface,
                QueryStatus.OK,
                snapshot=snapshot,
                finished_at=observed,
            )
        )
    dialog._on_result(
        PoEQueryResult(
            groups[1],
            "7",
            QueryStatus.FAILED,
            error_code=ErrorCode.COMMAND_TIMEOUT,
            message="문서용 가상 시간 초과 — 실제 장비 조회 없음",
            finished_at="2026-11-16T12:30:22+00:00",
        )
    )
    dialog.result_tree.setCurrentItem(dialog.result_tree.topLevelItem(0).child(0))
    dialog.progress_bar.setValue(100)
    dialog.status_label.setText("문서용 가상 결과 · 스위치 2대 / 포트 4개 · 실제 장비 접속 없음")
    dialog.show()
    app.processEvents()
    if not dialog.grab().save(str(output / "poe-query.png"), "PNG"):
        raise RuntimeError("전력 조회 화면 PNG 저장에 실패했습니다.")
    dialog.reject()
    window.close()
    app.processEvents()


def _verify_image(path: Path) -> None:
    if not path.is_file():
        raise RuntimeError(f"문서 화면 파일이 생성되지 않았습니다: {path}")

    image = QImage(str(path))
    if image.isNull():
        raise RuntimeError(f"생성된 문서 화면을 PNG로 다시 읽을 수 없습니다: {path}")
    if image.width() < 800 or image.height() < 600:
        raise RuntimeError(
            f"문서 화면 해상도가 예상보다 작습니다: {path} ({image.width()}x{image.height()})"
        )
    if path.stat().st_size < 1_024:
        raise RuntimeError(f"문서 화면 파일이 비정상적으로 작습니다: {path}")

    print(f"created: {path} ({image.width()}x{image.height()}, {path.stat().st_size} bytes)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    socket.create_connection = _block_network
    socket.socket.connect = _block_network
    socket.socket.connect_ex = _block_network
    _save_main_window(output)
    _save_result_example(output)
    _save_host_key_review(output)
    _save_mixed_result(output)
    _save_poe_query(output)

    for path in sorted(output.glob("*.png")):
        _verify_image(path)
    manifest = {
        "application": "Aruba 2930F Config Backup",
        "application_version": __version__,
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "capture_os": platform.platform(),
        "python": platform.python_version(),
        "method": "Actual PySide6 MainWindow, HostKeyApprovalDialog and PoEQueryDialog; Qt offscreen; synthetic widget state",
        "network": "socket connection attempts blocked; no collection or device access",
        "workflow_run": os.environ.get("GITHUB_RUN_ID", "local"),
        "images": [
            {"file": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(output.glob("*.png"))
        ],
    }
    (output / "capture-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
