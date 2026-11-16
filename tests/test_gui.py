from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from openpyxl import load_workbook

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from aruba2930f_backup.collector import ArubaCollector
from aruba2930f_backup.diagnostics import decode_diagnostic_code, diagnostic_code_for_exception
from aruba2930f_backup.gui import (
    BackupCallbacks,
    BackupOutcome,
    BackupRequest,
    CollectorBackupService,
    DiagnosticCodesDialog,
    HostKeyApprovalDialog,
    MainWindow,
    TrustedKeysDialog,
)
from aruba2930f_backup.hostkeys import HostKeyStore, sha256_fingerprint
from aruba2930f_backup.models import (
    CollectionFailure,
    CollectionOptions,
    DeviceResult,
    DeviceStatus,
    DeviceTarget,
    DiagnosticDetail,
    DiagnosticPhase,
    ErrorCode,
    HostKeyCheck,
    HostKeyObservation,
    HostKeyTrustState,
)
from aruba2930f_backup.reporting import SanitizedJsonlLogger
from aruba2930f_backup.storage import FilenameMode, StoredConfig, write_config_atomic
from tests.fakes import ScriptedFactory


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


class FakeService:
    def __init__(self, run_directory: Path) -> None:
        self.run_directory = run_directory
        self.request: BackupRequest | None = None
        self.cancelled = False

    def run(self, request: BackupRequest, callbacks: BackupCallbacks) -> BackupOutcome:
        self.request = request
        callbacks.on_event(
            {
                "target": {"ip": request.targets[0]},
                "stage": "completed",
                "attempt": 1,
            }
        )
        result = {
            "target": {"ip": request.targets[0]},
            "hostname": "lab-edge",
            "model": "Aruba 2930F",
            "sku": "JL253A",
            "status": "success",
            "host_key_attempts": 1,
            "attempts": 1,
        }
        return BackupOutcome(
            run_directory=self.run_directory,
            results=(result,),
            report_path=self.run_directory / "result.xlsx",
        )

    def cancel(self) -> None:
        self.cancelled = True


class FakeCollector:
    def __init__(self) -> None:
        self.cancelled = False
        self.begin_runs = 0
        self.result: DeviceResult | None = None

    def begin_run(self) -> None:
        self.begin_runs += 1
        self.cancelled = False

    def probe_host_keys_round(
        self,
        targets: Sequence[DeviceTarget],
        *,
        attempt: int,
        options: CollectionOptions | None = None,
        cancel_event: threading.Event | None = None,
        on_event: Callable[[object], None] | None = None,
    ) -> list[HostKeyCheck]:
        del options, cancel_event
        if on_event is not None:
            for target in targets:
                on_event(
                    {
                        "target": target,
                        "stage": "host_key_checking",
                        "round": attempt,
                        "attempt": attempt,
                    }
                )
        return [
            HostKeyCheck(
                observation=HostKeyObservation(
                    target=target,
                    key_type="ssh-ed25519",
                    fingerprint="SHA256:fixture",
                ),
                state=HostKeyTrustState.TRUSTED,
                attempts=attempt,
            )
            for target in targets
        ]

    def wait_for_retry_delay(
        self,
        delay_seconds: float,
        *,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        del delay_seconds
        return not bool(cancel_event and cancel_event.is_set())

    def approve_host_keys(self, checks: Iterable[HostKeyCheck]) -> None:
        raise AssertionError("Trusted fixture keys must not require approval")

    def collect_many(
        self,
        targets: Sequence[DeviceTarget],
        credentials: object,
        options: CollectionOptions | None = None,
        *,
        cancel_event: threading.Event | None = None,
        on_event: Callable[[object], None] | None = None,
    ) -> list[DeviceResult]:
        del credentials, options, cancel_event
        target = targets[0]
        if on_event is not None:
            on_event(
                {
                    "target": {"ip": target.ip},
                    "stage": "completed",
                    "attempt": 1,
                    "message": f"completed {target.ip}",
                }
            )
        now = datetime.now(UTC)
        self.result = DeviceResult(
            target=target,
            status=DeviceStatus.SUCCESS,
            attempts=1,
            started_at=now,
            finished_at=now,
            duration_seconds=0.1,
            hostname="fixture-edge",
            model="Aruba 2930F",
            sku="JL253A",
            config_text="hostname fixture-edge\nno telnet-server\n",
        )
        return [self.result]

    def cancel(self) -> None:
        self.cancelled = True


def _wait_until(app: QApplication, predicate: object, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if callable(predicate) and predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Qt condition was not reached before timeout")


@pytest.mark.gui
def test_main_window_defaults_and_request_are_session_only(
    app: QApplication, tmp_path: Path
) -> None:
    window = MainWindow(service=FakeService(tmp_path))
    window.ip_input.setPlainText("192.0.2.10\n198.51.100.8")
    window.username_input.setText("operator")
    window.password_input.setText("secret")
    window.enable_password_input.setText("enable-secret")
    window.output_input.setText(str(tmp_path))

    request = window.build_request()

    assert request.targets == ("192.0.2.10", "198.51.100.8")
    assert request.port == 22
    assert request.concurrency == 10
    assert [
        window.filename_mode_input.itemText(index)
        for index in range(window.filename_mode_input.count())
    ] == ["장비 이름", "IP", "장비 이름(IP)"]
    assert window.filename_mode_input.currentData() == FilenameMode.HOSTNAME_IP.value
    assert request.filename_mode is FilenameMode.HOSTNAME_IP
    assert request.username == "operator"
    assert request.password == "secret"
    assert request.enable_password == "enable-secret"
    window.close()


@pytest.mark.gui
def test_filename_mode_selection_is_session_only(app: QApplication, tmp_path: Path) -> None:
    window = MainWindow(service=FakeService(tmp_path))
    window.ip_input.setPlainText("192.0.2.10")
    window.username_input.setText("operator")
    window.password_input.setText("secret")
    window.output_input.setText(str(tmp_path))
    window.filename_mode_input.setCurrentIndex(
        window.filename_mode_input.findData(FilenameMode.IP.value)
    )

    assert window.build_request().filename_mode is FilenameMode.IP
    window._set_running(True)
    assert not window.filename_mode_input.isEnabled()
    window._set_running(False)
    assert window.filename_mode_input.isEnabled()
    assert window.filename_mode_input.currentData() == FilenameMode.IP.value
    window.close()

    replacement = MainWindow(service=FakeService(tmp_path))
    assert replacement.filename_mode_input.currentData() == FilenameMode.HOSTNAME_IP.value
    replacement.close()


@pytest.mark.gui
def test_invalid_or_duplicate_ipv4_blocks_entire_request(app: QApplication) -> None:
    window = MainWindow(service=FakeService(Path.cwd()))

    with pytest.raises(ValueError, match="중복"):
        window.parse_targets("192.0.2.1\n192.0.2.1")
    with pytest.raises(ValueError, match="올바른 IPv4"):
        window.parse_targets("192.0.2.1\nnot-a-device")

    window.close()


@pytest.mark.gui
def test_changed_host_key_can_never_be_approved(app: QApplication) -> None:
    dialog = HostKeyApprovalDialog(
        [
            {
                "target": {"endpoint": "192.0.2.10:22"},
                "key_type": "ssh-ed25519",
                "fingerprint": "SHA256:example",
                "state": "changed",
            }
        ]
    )

    assert dialog.approval_allowed is False
    assert dialog.approve_button.isEnabled() is False
    assert dialog.windowTitle() == "SSH 장비 지문 확인"
    assert dialog.approve_button.text() == "표시된 지문 모두 승인"
    dialog.close()


@pytest.mark.gui
def test_host_key_controls_use_device_fingerprint_wording(app: QApplication) -> None:
    window = MainWindow(service=FakeService(Path.cwd()))
    dialog = TrustedKeysDialog((), None)

    assert window.trust_keys_button.text() == "SSH 장비 지문 관리…"
    assert dialog.windowTitle() == "SSH 장비 지문 관리"
    assert dialog.remove_button.text() == "선택 지문 제거"

    dialog.close()
    window.close()


@pytest.mark.gui
def test_injected_service_runs_off_ui_thread_and_updates_results(
    app: QApplication,
    tmp_path: Path,
) -> None:
    service = FakeService(tmp_path)
    window = MainWindow(service=service)
    window.ip_input.setPlainText("192.0.2.10")
    window.username_input.setText("operator")
    window.password_input.setText("secret")
    window.output_input.setText(str(tmp_path))

    window.start_button.click()
    _wait_until(app, lambda: window.start_button.isEnabled())

    assert service.request is not None
    assert window.progress_bar.value() == 100
    assert window.result_table.rowCount() == 1
    assert window.result_table.item(0, 1).text() == "lab-edge"
    assert window.password_input.text() == ""
    assert window.open_result_button.isEnabled()
    window.close()


@pytest.mark.gui
def test_retry_wait_status_does_not_advance_attempt_before_next_connection(
    app: QApplication,
) -> None:
    window = MainWindow(service=FakeService(Path.cwd()))
    window._target_count = 1
    target = {"ip": "192.0.2.20"}

    window._on_collection_event(
        {
            "target": target,
            "stage": "host_key_checking",
            "phase": "host_key",
            "round": 1,
            "attempt": 1,
        }
    )
    window._on_collection_event(
        {
            "target": target,
            "stage": "retry_wait",
            "phase": "host_key",
            "round": 2,
            "attempt": 2,
            "delay_seconds": 5.0,
        }
    )

    row = window._row_by_target["192.0.2.20"]
    assert window.result_table.item(row, 3).text() == "재시도 대기"
    assert window.result_table.item(row, 4).text() == "지문 1/4 · 백업 0/4"
    assert window.result_table.item(row, 3).foreground().color().name() == "#9a6700"
    window.close()


@pytest.mark.gui
def test_family_only_model_does_not_render_missing_sku_as_none(
    app: QApplication,
    tmp_path: Path,
) -> None:
    window = MainWindow(service=FakeService(tmp_path))

    window._on_worker_success(
        {
            "run_directory": tmp_path,
            "results": (
                {
                    "target": {"ip": "192.0.2.22"},
                    "status": "success",
                    "model": "Aruba 2930F",
                    "sku": None,
                    "host_key_attempts": 1,
                    "attempts": 1,
                },
            ),
        }
    )

    row = window._row_by_target["192.0.2.22"]
    assert window.result_table.item(row, 2).text() == "Aruba 2930F"
    window.close()


@pytest.mark.gui
def test_terminal_failed_event_counts_as_completed_progress(app: QApplication) -> None:
    window = MainWindow(service=FakeService(Path.cwd()))
    window._target_count = 1

    window._on_collection_event(
        {
            "target": {"ip": "192.0.2.21"},
            "stage": "failed",
            "phase": "backup",
            "round": 1,
            "attempt": 1,
            "error_code": "AUTH_FAILED",
        }
    )

    assert window.progress_bar.value() == 100
    assert "192.0.2.21" in window._completed_targets
    window.close()


def test_collector_service_writes_config_report_and_sanitized_log(tmp_path: Path) -> None:
    collector = FakeCollector()
    service = CollectorBackupService(collector)
    request = BackupRequest(
        targets=("192.0.2.10",),
        port=22,
        username="operator",
        password="secret-password",
        enable_password="enable-secret",
        concurrency=10,
        output_directory=tmp_path,
    )

    outcome = service.run(
        request,
        BackupCallbacks(
            on_event=lambda _event: None,
            request_host_key_approval=lambda _checks: False,
            cancel_event=threading.Event(),
        ),
    )

    assert collector.result is not None
    assert collector.begin_runs == 1
    assert collector.result.config_text is None
    assert collector.result.config_path is not None
    assert collector.result.config_path.name == "fixture-edge(192.0.2.10).txt"
    assert collector.result.config_path.read_bytes().endswith(b"\r\n")
    assert collector.result.config_sha256
    assert outcome.report_path.exists()
    workbook = load_workbook(outcome.report_path, data_only=True)
    assert workbook["Devices"]["B2"].value == "fixture-edge"
    assert Path(workbook["Devices"]["K2"].value).name == "fixture-edge(192.0.2.10).txt"
    workbook.close()
    log_text = (outcome.run_directory / "operation.jsonl").read_text(encoding="utf-8")
    assert "192.0.2.10" not in log_text
    assert "fixture-edge" not in log_text
    assert "secret-password" not in log_text
    assert "enable-secret" not in log_text


@pytest.mark.parametrize(
    ("filename_mode", "expected_name"),
    (
        (FilenameMode.HOSTNAME, "fixture-edge.txt"),
        (FilenameMode.IP, "192.0.2.10.txt"),
    ),
)
def test_collector_service_applies_selected_filename_mode(
    tmp_path: Path,
    filename_mode: FilenameMode,
    expected_name: str,
) -> None:
    collector = FakeCollector()
    outcome = CollectorBackupService(collector).run(
        BackupRequest(
            targets=("192.0.2.10",),
            port=22,
            username="operator",
            password="secret-password",
            enable_password=None,
            concurrency=1,
            output_directory=tmp_path,
            filename_mode=filename_mode,
        ),
        BackupCallbacks(
            on_event=lambda _event: None,
            request_host_key_approval=lambda _checks: False,
            cancel_event=threading.Event(),
        ),
    )

    assert collector.result is not None
    assert collector.result.config_path is not None
    assert collector.result.config_path.name == expected_name
    workbook = load_workbook(outcome.report_path, data_only=True)
    assert Path(workbook["Devices"]["K2"].value).name == expected_name
    workbook.close()


def test_workbook_write_failure_has_report_diagnostic_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_workbook(*args: object, **kwargs: object) -> Path:
        del args, kwargs
        raise OSError("/sensitive/output/path")

    monkeypatch.setattr("aruba2930f_backup.gui.write_result_workbook", fail_workbook)
    service = CollectorBackupService(FakeCollector())

    with pytest.raises(CollectionFailure) as captured:
        service.run(
            BackupRequest(
                targets=("192.0.2.10",),
                port=22,
                username="operator",
                password="secret-password",
                enable_password=None,
                concurrency=1,
                output_directory=tmp_path,
            ),
            BackupCallbacks(
                on_event=lambda _event: None,
                request_host_key_approval=lambda _checks: False,
                cancel_event=threading.Event(),
            ),
        )

    assert captured.value.code is ErrorCode.REPORT_WRITE_FAILED
    assert captured.value.diagnostic_phase is DiagnosticPhase.REPORT_STORAGE
    log_path = next(tmp_path.rglob("operation.jsonl"))
    log_text = log_path.read_text(encoding="utf-8")
    assert "/sensitive/output/path" not in log_text
    fatal_record = next(
        json.loads(line)
        for line in log_text.splitlines()
        if json.loads(line).get("status") == "fatal_app"
    )
    assert fatal_record["diagnostic_code"]


def test_config_write_failure_updates_device_result_and_diagnostic_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_config(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("/sensitive/device-config/path")

    monkeypatch.setattr("aruba2930f_backup.gui.write_config_atomic", fail_config)
    outcome = CollectorBackupService(FakeCollector()).run(
        BackupRequest(
            targets=("192.0.2.10",),
            port=22,
            username="operator",
            password="secret-password",
            enable_password=None,
            concurrency=1,
            output_directory=tmp_path,
        ),
        BackupCallbacks(
            on_event=lambda _event: None,
            request_host_key_approval=lambda _checks: False,
            cancel_event=threading.Event(),
        ),
    )

    result = outcome.results[0]
    assert result["status"] is DeviceStatus.FAILED
    assert result["error_code"] is ErrorCode.REPORT_WRITE_FAILED
    assert result["failure_phase"] is DiagnosticPhase.REPORT_STORAGE
    assert result["diagnostic_detail"] is DiagnosticDetail.OS_ERROR
    code = str(result["diagnostic_code"])
    decoded = decode_diagnostic_code(code)
    assert decoded.phase is DiagnosticPhase.REPORT_STORAGE
    assert decoded.error_code is ErrorCode.REPORT_WRITE_FAILED
    assert "/sensitive/device-config/path" not in code
    log_text = (outcome.run_directory / "operation.jsonl").read_text(encoding="utf-8")
    assert "/sensitive/device-config/path" not in log_text
    assert code in log_text


def test_backup_request_repr_never_contains_secrets(tmp_path: Path) -> None:
    request = BackupRequest(
        targets=("192.0.2.10",),
        port=22,
        username="operator",
        password="secret-password",
        enable_password="enable-secret",
        concurrency=10,
        output_directory=tmp_path,
    )

    rendered = repr(request)
    assert "secret-password" not in rendered
    assert "enable-secret" not in rendered


def test_host_key_probe_failure_is_per_device_and_preserves_other_backup(tmp_path: Path) -> None:
    class PartiallyReachableCollector(FakeCollector):
        def __init__(self) -> None:
            super().__init__()
            self.call_log: list[str] = []

        def probe_host_keys_round(
            self,
            targets: Sequence[DeviceTarget],
            *,
            attempt: int,
            options: CollectionOptions | None = None,
            cancel_event: threading.Event | None = None,
            on_event: Callable[[object], None] | None = None,
        ) -> list[HostKeyCheck]:
            del options, cancel_event, on_event
            self.call_log.append(f"probe-{attempt}:" + ",".join(target.ip for target in targets))
            checks: list[HostKeyCheck] = []
            for target in targets:
                if target.ip == "192.0.2.10":
                    checks.append(
                        HostKeyCheck(
                            observation=HostKeyObservation(target, "", ""),
                            state=HostKeyTrustState.REJECTED,
                            message="The SSH endpoint timed out.",
                            error_code=ErrorCode.TCP_TIMEOUT,
                            attempts=attempt,
                            retryable=True,
                            retry_exhausted=attempt >= 4,
                        )
                    )
                else:
                    checks.append(
                        HostKeyCheck(
                            observation=HostKeyObservation(
                                target, "ssh-ed25519", "SHA256:trusted-fixture"
                            ),
                            state=HostKeyTrustState.TRUSTED,
                            attempts=attempt,
                        )
                    )
            return checks

        def collect_many(
            self,
            targets: Sequence[DeviceTarget],
            credentials: object,
            options: CollectionOptions | None = None,
            *,
            cancel_event: threading.Event | None = None,
            on_event: Callable[[object], None] | None = None,
        ) -> list[DeviceResult]:
            self.call_log.append("collect:" + ",".join(target.ip for target in targets))
            return super().collect_many(
                targets,
                credentials,
                options,
                cancel_event=cancel_event,
                on_event=on_event,
            )

    collector = PartiallyReachableCollector()
    outcome = CollectorBackupService(
        collector,
        retry_delays_seconds=(0.0, 0.0, 0.0),
    ).run(
        BackupRequest(
            targets=("192.0.2.10", "192.0.2.11"),
            port=22,
            username="operator",
            password="secret-password",
            enable_password=None,
            concurrency=10,
            output_directory=tmp_path,
        ),
        BackupCallbacks(
            on_event=lambda _event: None,
            request_host_key_approval=lambda _checks: False,
            cancel_event=threading.Event(),
        ),
    )

    first, second = outcome.results
    assert first["target"]["ip"] == "192.0.2.10"
    assert first["status"] == DeviceStatus.RETRY_EXHAUSTED
    assert first["error_code"] == ErrorCode.TCP_TIMEOUT
    assert first["host_key_attempts"] == 4
    assert first["attempts"] == 0
    assert first["diagnostic_code"]
    decoded = decode_diagnostic_code(str(first["diagnostic_code"]))
    assert decoded.phase is DiagnosticPhase.HOST_KEY
    assert decoded.host_key_attempts == 4
    assert decoded.backup_attempts == 0
    assert second["target"]["ip"] == "192.0.2.11"
    assert second["status"] == DeviceStatus.SUCCESS
    assert second["host_key_attempts"] == 1
    assert collector.call_log[:3] == [
        "probe-1:192.0.2.10,192.0.2.11",
        "collect:192.0.2.11",
        "probe-2:192.0.2.10",
    ]
    retry_log = next(
        record
        for record in (
            json.loads(line)
            for line in (outcome.run_directory / "operation.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        if record.get("stage") == "retry_wait"
    )
    assert retry_log["round"] == 2
    assert retry_log["attempt"] == 1
    assert retry_log["delay_seconds"] == 0.0
    assert retry_log["error_code"] == ErrorCode.TCP_TIMEOUT
    diagnostic_log = next(
        record
        for record in (
            json.loads(line)
            for line in (outcome.run_directory / "operation.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        if record.get("diagnostic_code") == first["diagnostic_code"]
    )
    assert diagnostic_log["count"] == 1


@pytest.mark.gui
def test_diagnostic_dialog_aggregates_and_copies_codes(
    app: QApplication,
    tmp_path: Path,
) -> None:
    window = MainWindow(service=FakeService(tmp_path))
    window.show()
    code = diagnostic_code_for_exception(RuntimeError("must not be shown"), version="0.1.3")

    window._pending_diagnostic_counts = {code: 2}
    window._finalize_completed_run()
    app.processEvents()

    dialog = window._diagnostic_dialog
    assert isinstance(dialog, DiagnosticCodesDialog)
    expected = f"{code} \N{MULTIPLICATION SIGN} 2"
    assert dialog.copy_text == expected
    dialog.copy_button.click()
    assert QApplication.clipboard().text() == expected
    dialog.close()
    window.close()


@pytest.mark.gui
def test_fatal_worker_code_remains_in_status_and_opens_dialog(
    app: QApplication,
    tmp_path: Path,
) -> None:
    window = MainWindow(service=FakeService(tmp_path))
    window.show()
    code = diagnostic_code_for_exception(RuntimeError("not serialized"), version="0.1.3")

    window._on_worker_failure({"exception_name": "RuntimeError", "diagnostic_code": code})
    window._finalize_completed_run()
    app.processEvents()

    assert code in window.status_label.text()
    assert isinstance(window._diagnostic_dialog, DiagnosticCodesDialog)
    window._diagnostic_dialog.close()
    window.close()


@pytest.mark.gui
def test_retry_exhausted_button_runs_only_captured_subset_with_new_password(
    app: QApplication,
    tmp_path: Path,
) -> None:
    class RetrySubsetService:
        def __init__(self) -> None:
            self.requests: list[BackupRequest] = []

        def run(self, request: BackupRequest, callbacks: BackupCallbacks) -> BackupOutcome:
            del callbacks
            self.requests.append(request)
            run_number = len(self.requests)
            run_directory = tmp_path / f"run-{run_number}"
            run_directory.mkdir()
            results: list[dict[str, object]] = []
            for target in request.targets:
                exhausted = target == "192.0.2.11"
                results.append(
                    {
                        "target": {"ip": target},
                        "status": "retry_exhausted" if exhausted else "success",
                        "host_key_attempts": 1,
                        "attempts": 4 if exhausted else 1,
                        "error_code": "TCP_TIMEOUT" if exhausted else "",
                    }
                )
            return BackupOutcome(
                run_directory=run_directory,
                results=tuple(results),
                report_path=run_directory / "result.xlsx",
            )

        def cancel(self) -> None:
            pass

    service = RetrySubsetService()
    window = MainWindow(service=service)
    window.ip_input.setPlainText("192.0.2.10\n192.0.2.11")
    window.username_input.setText("operator")
    window.password_input.setText("first-password")
    window.output_input.setText(str(tmp_path))

    window.start_button.click()
    _wait_until(app, lambda: window.start_button.isEnabled())

    assert window.retry_exhausted_button.isEnabled()
    exhausted_row = window._row_by_target["192.0.2.11"]
    assert window.result_table.item(exhausted_row, 3).text() == "재시도 소진"
    assert window.result_table.item(exhausted_row, 4).text() == "지문 1/4 · 백업 4/4"
    assert window.password_input.text() == ""
    assert window.ip_input.toPlainText() == "192.0.2.10\n192.0.2.11"

    window.port_input.setValue(2222)
    window.username_input.setText("retry-operator")
    window.concurrency_input.setValue(3)
    window.filename_mode_input.setCurrentIndex(
        window.filename_mode_input.findData(FilenameMode.IP.value)
    )
    window.password_input.setText("new-password")
    window.retry_exhausted_button.click()
    _wait_until(app, lambda: len(service.requests) == 2 and window.start_button.isEnabled())

    assert service.requests[1].targets == ("192.0.2.11",)
    assert service.requests[1].port == 2222
    assert service.requests[1].username == "retry-operator"
    assert service.requests[1].concurrency == 3
    assert service.requests[0].filename_mode is FilenameMode.HOSTNAME_IP
    assert service.requests[1].filename_mode is FilenameMode.IP
    assert service.requests[1].password == "new-password"
    assert window.result_table.rowCount() == 1
    assert service.requests[0].output_directory == service.requests[1].output_directory
    assert (tmp_path / "run-1") != (tmp_path / "run-2")
    window.close()


def test_same_service_runs_successfully_after_prior_cancel(tmp_path: Path) -> None:
    target = DeviceTarget("192.0.2.50")
    observation = HostKeyObservation(
        target,
        "ssh-ed25519",
        sha256_fingerprint(b"reusable-fixture-key"),
    )

    class Probe:
        def probe(self, requested: DeviceTarget, *, timeout: float = 15.0) -> HostKeyObservation:
            del timeout
            assert requested == target
            return observation

    collector = ArubaCollector(
        ScriptedFactory(),
        host_key_store=HostKeyStore(tmp_path / "known_hosts.json"),
        host_key_probe=Probe(),
    )
    service = CollectorBackupService(collector)
    request = BackupRequest(
        targets=(target.ip,),
        port=target.port,
        username="operator",
        password="test-password",
        enable_password=None,
        concurrency=1,
        output_directory=tmp_path / "backups",
    )

    def run_once() -> BackupOutcome:
        return service.run(
            request,
            BackupCallbacks(
                on_event=lambda _event: None,
                request_host_key_approval=lambda _checks: True,
                cancel_event=threading.Event(),
            ),
        )

    first_outcome = run_once()
    assert first_outcome.results[0]["status"] is DeviceStatus.SUCCESS
    service.cancel()
    second_outcome = run_once()
    assert second_outcome.results[0]["status"] is DeviceStatus.SUCCESS
    assert first_outcome.run_directory != second_outcome.run_directory
    assert first_outcome.report_path.exists()
    assert second_outcome.report_path.exists()


def test_cancel_during_preflight_retry_wait_writes_cancelled_report(tmp_path: Path) -> None:
    class CancelDuringWaitCollector(FakeCollector):
        def probe_host_keys_round(
            self,
            targets: Sequence[DeviceTarget],
            *,
            attempt: int,
            options: CollectionOptions | None = None,
            cancel_event: threading.Event | None = None,
            on_event: Callable[[object], None] | None = None,
        ) -> list[HostKeyCheck]:
            del options, cancel_event, on_event
            assert attempt == 1
            return [
                HostKeyCheck(
                    observation=HostKeyObservation(target, "", ""),
                    state=HostKeyTrustState.REJECTED,
                    message="The SSH endpoint timed out.",
                    error_code=ErrorCode.TCP_TIMEOUT,
                    attempts=attempt,
                    retryable=True,
                )
                for target in targets
            ]

        def wait_for_retry_delay(
            self,
            delay_seconds: float,
            *,
            cancel_event: threading.Event | None = None,
        ) -> bool:
            assert delay_seconds >= 0
            assert cancel_event is not None
            cancel_event.set()
            return False

    events: list[object] = []
    outcome = CollectorBackupService(CancelDuringWaitCollector()).run(
        BackupRequest(
            targets=("192.0.2.75",),
            port=22,
            username="operator",
            password="test-password",
            enable_password=None,
            concurrency=1,
            output_directory=tmp_path,
        ),
        BackupCallbacks(
            on_event=events.append,
            request_host_key_approval=lambda _checks: False,
            cancel_event=threading.Event(),
        ),
    )

    assert outcome.cancelled is True
    assert outcome.report_path.exists()
    assert outcome.results[0]["status"] is DeviceStatus.CANCELLED
    assert outcome.results[0]["host_key_attempts"] == 1
    assert outcome.results[0]["attempts"] == 0
    assert any(
        str(getattr(event.get("stage"), "value", event.get("stage"))) == "retry_wait"
        for event in cast(list[dict[str, object]], events)
    )


def test_cancel_mid_host_key_round_preserves_only_started_attempts(tmp_path: Path) -> None:
    class CancelMidRoundCollector(FakeCollector):
        def probe_host_keys_round(
            self,
            targets: Sequence[DeviceTarget],
            *,
            attempt: int,
            options: CollectionOptions | None = None,
            cancel_event: threading.Event | None = None,
            on_event: Callable[[object], None] | None = None,
        ) -> list[HostKeyCheck]:
            del options
            assert attempt == 1
            assert cancel_event is not None
            assert on_event is not None
            for target in targets[:2]:
                on_event(
                    {
                        "target": target,
                        "stage": "host_key_checking",
                        "attempt": attempt,
                    }
                )
            cancel_event.set()
            raise CollectionFailure(
                ErrorCode.CANCELLED,
                "Host-key review was cancelled.",
            )

    outcome = CollectorBackupService(CancelMidRoundCollector()).run(
        BackupRequest(
            targets=("192.0.2.75", "192.0.2.76", "192.0.2.77"),
            port=22,
            username="operator",
            password="test-password",
            enable_password=None,
            concurrency=2,
            output_directory=tmp_path,
        ),
        BackupCallbacks(
            on_event=lambda _event: None,
            request_host_key_approval=lambda _checks: False,
            cancel_event=threading.Event(),
        ),
    )

    assert outcome.cancelled is True
    assert [result["status"] for result in outcome.results] == [
        DeviceStatus.CANCELLED,
        DeviceStatus.CANCELLED,
        DeviceStatus.CANCELLED,
    ]
    assert [result["host_key_attempts"] for result in outcome.results] == [1, 1, 0]
    assert [result["attempts"] for result in outcome.results] == [0, 0, 0]


@pytest.mark.gui
def test_cancel_handler_does_not_block_on_slow_transport_close(app: QApplication) -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class SlowCancelService:
        def run(self, request: BackupRequest, callbacks: BackupCallbacks) -> BackupOutcome:
            del request, callbacks
            raise AssertionError("run is not used by this cancellation test")

        def cancel(self) -> None:
            entered.set()
            try:
                release.wait(timeout=2)
            finally:
                finished.set()

    class ThreadSentinel:
        def wait(self, timeout: int) -> bool:
            return True

        def deleteLater(self) -> None:
            pass

    window = MainWindow(service=SlowCancelService())
    window.show()
    window._set_running(True)
    assert not window.filename_mode_input.isEnabled()
    window._thread = cast(QThread, ThreadSentinel())
    window._cancel_event = threading.Event()

    started = time.monotonic()
    window._cancel_backup()
    elapsed = time.monotonic() - started

    assert elapsed < 0.1
    assert window._cancel_event.is_set()
    assert entered.wait(timeout=1)
    cancel_thread = window._cancel_thread
    assert cancel_thread is not None
    window._on_thread_finished()
    assert not window.start_button.isEnabled()
    window.close()
    app.processEvents()
    assert window.isVisible()

    release.set()
    assert finished.wait(timeout=1)
    deadline = time.monotonic() + 1
    while window.isVisible() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    cancel_thread.join(timeout=1)
    assert not cancel_thread.is_alive()
    assert not window.isVisible()


class RetryAfterSuccessCollector(FakeCollector):
    """Finish one batch before a separate endpoint's second preflight round."""

    fatal_second_round = False

    def probe_host_keys_round(
        self,
        targets: Sequence[DeviceTarget],
        *,
        attempt: int,
        options: CollectionOptions | None = None,
        cancel_event: threading.Event | None = None,
        on_event: Callable[[object], None] | None = None,
    ) -> list[HostKeyCheck]:
        del options, cancel_event, on_event
        if attempt > 1 and self.fatal_second_round:
            raise CollectionFailure(
                ErrorCode.HOST_KEY_REJECTED,
                "Controlled store failure with secret-password /private/store",
                diagnostic_phase=DiagnosticPhase.HOST_KEY,
            )
        return [
            HostKeyCheck(
                HostKeyObservation(target, "ssh-ed25519", "SHA256:fixture"),
                HostKeyTrustState.TRUSTED
                if target.ip == "192.0.2.10"
                else HostKeyTrustState.REJECTED,
                attempts=attempt,
                error_code=None if target.ip == "192.0.2.10" else ErrorCode.TCP_TIMEOUT,
                retryable=target.ip != "192.0.2.10" and attempt == 1,
            )
            for target in targets
        ]


def _mixed_backup_request(tmp_path: Path) -> BackupRequest:
    return BackupRequest(
        ("192.0.2.10", "192.0.2.20"), 22, "operator", "secret-password", None, 2, tmp_path
    )


def test_log_permission_error_preserves_completed_batch_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = SanitizedJsonlLogger.log
    failed_calls = 0

    def fail_retry_log(
        self: SanitizedJsonlLogger, event: object = None, **fields: object
    ) -> object:
        nonlocal failed_calls
        assert failed_calls == 0, "Diagnostic I/O must stop after its first failure"
        if isinstance(event, dict) and event.get("stage") == "retry_wait":
            failed_calls += 1
            raise PermissionError("secret-password /private/log")
        return original(self, event, **fields)

    monkeypatch.setattr(SanitizedJsonlLogger, "log", fail_retry_log)
    collector = RetryAfterSuccessCollector()
    events: list[object] = []
    outcome = CollectorBackupService(collector, retry_delays_seconds=(0, 0, 0)).run(
        _mixed_backup_request(tmp_path),
        BackupCallbacks(events.append, lambda _: False, threading.Event()),
    )
    assert len(list(tmp_path.rglob("*.txt"))) == 1
    assert outcome.report_path.exists()
    assert len(outcome.warnings) == 1
    assert "로그" in outcome.warnings[0]
    assert "secret-password" not in outcome.warnings[0]
    assert "/private" not in outcome.warnings[0]
    assert failed_calls == 1
    assert any(isinstance(event, dict) and event.get("stage") == "warning" for event in events)
    saved = outcome.results[0]
    assert saved["config_path"]
    assert saved["config_sha256"]
    workbook = load_workbook(outcome.report_path, data_only=True)
    assert workbook["Devices"]["K2"].value == saved["config_path"]
    assert workbook["Devices"]["L2"].value == saved["config_sha256"]
    assert outcome.warnings[0] in str(list(workbook["Summary"].values))
    workbook.close()


def test_later_fatal_service_error_preserves_prior_config(tmp_path: Path) -> None:
    collector = RetryAfterSuccessCollector()
    collector.fatal_second_round = True
    with pytest.raises(CollectionFailure) as caught:
        CollectorBackupService(collector, retry_delays_seconds=(0, 0, 0)).run(
            _mixed_backup_request(tmp_path),
            BackupCallbacks(lambda _: None, lambda _: False, threading.Event()),
        )
    assert caught.value.code is ErrorCode.HOST_KEY_REJECTED
    assert collector.result is not None
    assert collector.result.config_path is not None
    assert collector.result.config_path.read_bytes() == (
        b"hostname fixture-edge\r\nno telnet-server\r\n"
    )
    assert collector.result.config_sha256
    assert collector.result.config_text is None
    assert not list(tmp_path.rglob("result.xlsx"))


@pytest.mark.gui
@pytest.mark.parametrize("fatal_second_round", [True, False])
def test_early_txt_failure_reaches_gui_and_survives_later_failure(
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fatal_second_round: bool,
) -> None:
    second_round = threading.Event()
    release_second_round = threading.Event()

    class MixedStorageCollector(RetryAfterSuccessCollector):
        saved_results: list[DeviceResult]

        def probe_host_keys_round(
            self,
            targets: Sequence[DeviceTarget],
            *,
            attempt: int,
            options: CollectionOptions | None = None,
            cancel_event: threading.Event | None = None,
            on_event: Callable[[object], None] | None = None,
        ) -> list[HostKeyCheck]:
            if attempt > 1:
                second_round.set()
                assert release_second_round.wait(3)
            return super().probe_host_keys_round(
                [target for target in targets if target.ip != "192.0.2.30"],
                attempt=attempt,
            ) + FakeCollector.probe_host_keys_round(
                self,
                [target for target in targets if target.ip == "192.0.2.30"],
                attempt=attempt,
            )

        def collect_many(
            self,
            targets: Sequence[DeviceTarget],
            credentials: object,
            options: CollectionOptions | None = None,
            *,
            cancel_event: threading.Event | None = None,
            on_event: Callable[[object], None] | None = None,
        ) -> list[DeviceResult]:
            self.saved_results = []
            for target in targets:
                results = super().collect_many([target], credentials, on_event=on_event)
                results[0].hostname = "failed-edge" if target.ip.endswith(".10") else "saved-edge"
                self.saved_results.extend(results)
            return self.saved_results

    def fail_one_txt(path: Path, text: str) -> StoredConfig:
        if "failed-edge" in path.name:
            raise PermissionError("secret-password /private/config-write")
        return write_config_atomic(path, text)

    monkeypatch.setattr("aruba2930f_backup.gui.write_config_atomic", fail_one_txt)
    collector = MixedStorageCollector()
    collector.fatal_second_round = fatal_second_round
    window = MainWindow(service=CollectorBackupService(collector, retry_delays_seconds=(0, 0, 0)))
    window.ip_input.setPlainText("192.0.2.10\n192.0.2.20\n192.0.2.30")
    window.username_input.setText("operator")
    window.password_input.setText("secret-password")
    window.output_input.setText(str(tmp_path))
    window.start_button.click()
    try:
        _wait_until(
            app,
            lambda: (
                second_round.is_set()
                and "192.0.2.20" in window._row_by_target
                and window.result_table.item(window._row_by_target["192.0.2.20"], 3).data(
                    Qt.ItemDataRole.UserRole
                )
                == "retry_queued"
            ),
        )
        failed_row = window._row_by_target["192.0.2.10"]
        early_status = window.result_table.item(failed_row, 3).data(Qt.ItemDataRole.UserRole)
        early_error = window.result_table.item(failed_row, 5).text()
        release_second_round.set()
        _wait_until(app, lambda: window.start_button.isEnabled())

        assert early_status == "failed"
        assert "REPORT_WRITE_FAILED" in early_error
        assert window.result_table.item(failed_row, 3).data(Qt.ItemDataRole.UserRole) == "failed"
        assert "REPORT_WRITE_FAILED" in window.result_table.item(failed_row, 5).text()
        dialog = window._diagnostic_dialog
        assert dialog is not None
        decoded = []
        for line in dialog.copy_text.splitlines():
            code, count = line.split(" \N{MULTIPLICATION SIGN} ")
            assert count == "1", "Early events and final reports must not double-count failures"
            decoded.append(decode_diagnostic_code(code))
        assert {(item.phase, item.error_code) for item in decoded} == {
            (DiagnosticPhase.REPORT_STORAGE, ErrorCode.REPORT_WRITE_FAILED),
            (DiagnosticPhase.HOST_KEY, ErrorCode.TCP_TIMEOUT)
            if not fatal_second_round
            else (DiagnosticPhase.HOST_KEY, ErrorCode.HOST_KEY_REJECTED),
        }
        storage_diagnostic = next(
            item for item in decoded if item.phase is DiagnosticPhase.REPORT_STORAGE
        )
        assert storage_diagnostic.detail is DiagnosticDetail.OS_ERROR
        visible = (
            window.status_label.text()
            + dialog.copy_text
            + " ".join(
                window.result_table.item(row, column).text()
                for row in range(window.result_table.rowCount())
                for column in range(window.result_table.columnCount())
            )
        )
        assert "secret-password" not in visible
        assert "/private/" not in visible
        failed, saved = collector.saved_results
        assert failed.config_path is None
        assert saved.config_path is not None
        assert list(tmp_path.rglob("*.txt")) == [saved.config_path]
        assert saved.config_path.read_bytes() == b"hostname fixture-edge\r\nno telnet-server\r\n"
        assert saved.config_sha256 == hashlib.sha256(saved.config_path.read_bytes()).hexdigest()
        assert failed.config_text is saved.config_text is None
        assert window._thread is None and window._worker is None
    finally:
        release_second_round.set()
        _wait_until(app, lambda: window.start_button.isEnabled())
        if window._diagnostic_dialog is not None:
            window._diagnostic_dialog.close()
        window.close()


@pytest.mark.gui
@pytest.mark.parametrize("cancel_lags", [False, True])
def test_main_backup_retains_busy_state_until_native_cleanup_joins(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_lags: bool
) -> None:
    service_release = threading.Event()
    cleanup_entered = threading.Event()
    cleanup_release = threading.Event()
    cleanup_done = threading.Event()
    cancel_entered = threading.Event()
    cancel_release = threading.Event()

    class DelayedService(FakeService):
        def run(self, request: BackupRequest, callbacks: BackupCallbacks) -> BackupOutcome:
            assert service_release.wait(3)
            return super().run(request, callbacks)

        def cancel(self) -> None:
            cancel_entered.set()
            if cancel_lags:
                cancel_release.wait(3)

    window = MainWindow(service=DelayedService(tmp_path))
    window.ip_input.setPlainText("192.0.2.10")
    window.username_input.setText("operator")
    window.password_input.setText("secret-password")
    window.output_input.setText(str(tmp_path))
    window.show()
    window.start_button.click()
    thread = window._thread
    worker = window._worker
    assert thread is not None
    original_delete = thread.deleteLater
    # Avoid process abort on the unfixed implementation deleting a still-running QThread.
    monkeypatch.setattr(thread, "deleteLater", lambda: None)

    def native_cleanup() -> None:
        cleanup_entered.set()
        cleanup_release.wait(3)
        cleanup_done.set()

    thread.finished.connect(native_cleanup, Qt.ConnectionType.DirectConnection)
    ticks: list[float] = []
    timer = QTimer(window)
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    try:
        if cancel_lags:
            # A helper can still be closing transport after the worker delivers
            # its terminal outcome; initiate that cancellation before completion.
            window.cancel_button.click()
            _wait_until(app, cancel_entered.is_set)
        service_release.set()
        _wait_until(app, cleanup_entered.is_set)
        tick_count = len(ticks)
        _wait_until(app, lambda: len(ticks) > tick_count + 1)
        assert not cleanup_done.is_set()
        assert window._thread is thread
        assert window._worker is worker
        assert not window.start_button.isEnabled()
        assert window._cancel_event is not None
        window.close()
        app.processEvents()
        assert window.isVisible()
        cleanup_release.set()
        if cancel_lags:
            _wait_until(app, lambda: window._thread is None)
            assert cancel_entered.is_set()
            assert not window.start_button.isEnabled()
            assert window._cancel_thread is not None
            assert window._cancel_thread.is_alive()
            assert window.isVisible()
            cancel_release.set()
        _wait_until(app, lambda: window._thread is None and not window.isVisible())
        assert cleanup_done.is_set()
        assert thread.wait(0)
    finally:
        cleanup_release.set()
        cancel_release.set()
        service_release.set()
        thread.wait(3000)
        _wait_until(app, lambda: window._thread is None)
        window.close()
        original_delete()
        app.processEvents()


@pytest.mark.gui
def test_trust_manage_button_contains_malformed_store_error(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "known_hosts.json"
    path.write_text("{broken", encoding="utf-8")
    collector = FakeCollector()
    collector.host_key_store = HostKeyStore(path)  # type: ignore[attr-defined]
    errors: list[object] = []
    messages: list[str] = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: errors.append(args))
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: messages.append(str(args[2])))
    window = MainWindow(service=CollectorBackupService(collector))
    window.trust_keys_button.click()
    app.processEvents()
    assert not errors
    assert messages and "저장소" in messages[0]
    assert "{broken" not in messages[0]
    assert path.read_text(encoding="utf-8") == "{broken"
    window.close()


@pytest.mark.gui
@pytest.mark.parametrize("partial", [False, True])
def test_trust_remove_button_refreshes_actual_store_after_failure(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partial: bool
) -> None:
    store = HostKeyStore(tmp_path / "known_hosts.json")
    checks = [
        HostKeyCheck(
            HostKeyObservation(DeviceTarget(ip), "ssh-ed25519", sha256_fingerprint(ip.encode())),
            HostKeyTrustState.UNKNOWN,
        )
        for ip in ("192.0.2.10", "192.0.2.20")
    ]
    store.approve(checks)
    collector = FakeCollector()
    collector.host_key_store = store  # type: ignore[attr-defined]
    service = CollectorBackupService(collector)
    original_remove = store.remove
    calls = 0

    def fail_removal(endpoint: object) -> None:
        nonlocal calls
        calls += 1
        if partial and calls == 1:
            original_remove(str(endpoint))
            return
        raise PermissionError("secret-password /private/known_hosts")

    monkeypatch.setattr(store, "remove", fail_removal)
    errors: list[object] = []
    messages: list[str] = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: errors.append(args))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(str(args[2])))
    observed: list[tuple[str, ...]] = []

    def exercise_dialog(dialog: TrustedKeysDialog) -> int:
        dialog.table.selectAll()
        dialog.remove_button.click()
        observed.append(
            tuple(dialog.table.item(row, 0).text() for row in range(dialog.table.rowCount()))
        )
        return 0

    monkeypatch.setattr(TrustedKeysDialog, "exec", exercise_dialog)
    window = MainWindow(service=service)
    window.trust_keys_button.click()
    assert not errors
    assert messages and "제거" in messages[0]
    assert "secret-password" not in messages[0]
    assert "/private" not in messages[0]
    expected = ("192.0.2.20:22",) if partial else ("192.0.2.10:22", "192.0.2.20:22")
    assert observed == [expected]
    assert tuple(entry.endpoint for entry in store.list_approved()) == expected
    window.close()


@pytest.mark.parametrize("primary_storage", ["config", "workbook"])
def test_log_failure_does_not_hide_primary_storage_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, primary_storage: str
) -> None:
    def fail_write(*args: object, **kwargs: object) -> None:
        raise PermissionError("secret-password /private/output")

    monkeypatch.setattr(SanitizedJsonlLogger, "log", fail_write)
    monkeypatch.setattr(
        "aruba2930f_backup.gui."
        + ("write_config_atomic" if primary_storage == "config" else "write_result_workbook"),
        fail_write,
    )
    service = CollectorBackupService(FakeCollector())
    callbacks = BackupCallbacks(lambda _: None, lambda _: False, threading.Event())
    request = _mixed_backup_request(tmp_path)
    if primary_storage == "workbook":
        with pytest.raises(CollectionFailure) as caught:
            service.run(request, callbacks)
        assert caught.value.code is ErrorCode.REPORT_WRITE_FAILED
        assert caught.value.diagnostic_phase is DiagnosticPhase.REPORT_STORAGE
        assert "secret-password" not in str(caught.value)
        assert len(list(tmp_path.rglob("*.txt"))) == 1
    else:
        outcome = service.run(request, callbacks)
        result = outcome.results[0]
        assert result["status"] is DeviceStatus.FAILED
        assert result["error_code"] is ErrorCode.REPORT_WRITE_FAILED
        assert result["failure_phase"] is DiagnosticPhase.REPORT_STORAGE
        assert result["config_path"] == ""
        assert len(outcome.warnings) == 1
        workbook = load_workbook(outcome.report_path, data_only=True)
        assert workbook["Devices"]["D2"].value == "failed"
        assert workbook["Devices"]["N2"].value == "REPORT_WRITE_FAILED"
        workbook.close()


@pytest.mark.gui
@pytest.mark.parametrize("fatal", [False, True])
def test_backup_window_shows_log_warning_with_success_or_fatal_diagnostic(
    app: QApplication, tmp_path: Path, fatal: bool
) -> None:
    warning = "진단 로그를 저장하지 못했습니다. 출력 경로를 확인하세요."

    class WarningService(FakeService):
        def run(self, request: BackupRequest, callbacks: BackupCallbacks) -> BackupOutcome:
            callbacks.on_event({"stage": "warning", "message": warning})
            if fatal:
                raise CollectionFailure(
                    ErrorCode.REPORT_WRITE_FAILED,
                    "secret-password /private/output",
                    diagnostic_phase=DiagnosticPhase.REPORT_STORAGE,
                )
            outcome = super().run(request, callbacks)
            return BackupOutcome(
                outcome.run_directory, outcome.results, outcome.report_path, warnings=(warning,)
            )

    window = MainWindow(service=WarningService(tmp_path))
    window.ip_input.setPlainText("192.0.2.10")
    window.username_input.setText("operator")
    window.password_input.setText("secret-password")
    window.output_input.setText(str(tmp_path))
    window.start_button.click()
    _wait_until(app, lambda: window.start_button.isEnabled())
    assert warning in window.status_label.text()
    assert window.status_label.text().count(warning) == 1
    assert "secret-password" not in window.status_label.text()
    if fatal:
        assert "오류" in window.status_label.text()
        assert "진단 코드" in window.status_label.text()
        assert window._diagnostic_dialog is not None
        window._diagnostic_dialog.close()
    else:
        assert "백업 완료" in window.status_label.text()
        assert window.result_table.rowCount() == 1
    window.close()


@pytest.mark.gui
def test_trust_remove_button_disables_unverifiable_list_after_store_corruption(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "known_hosts.json"
    store = HostKeyStore(path)
    store.approve(
        [
            HostKeyCheck(
                HostKeyObservation(
                    DeviceTarget("192.0.2.10"), "ssh-ed25519", sha256_fingerprint(b"fixture")
                ),
                HostKeyTrustState.UNKNOWN,
            )
        ]
    )
    collector = FakeCollector()
    collector.host_key_store = store  # type: ignore[attr-defined]
    errors: list[object] = []
    messages: list[str] = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: errors.append(args))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(str(args[2])))

    def exercise_dialog(dialog: TrustedKeysDialog) -> int:
        dialog.table.selectAll()
        path.write_text("{corrupt", encoding="utf-8")
        dialog.remove_button.click()
        assert dialog.table.rowCount() == 0
        assert not dialog.remove_button.isEnabled()
        return 0

    monkeypatch.setattr(TrustedKeysDialog, "exec", exercise_dialog)
    window = MainWindow(service=CollectorBackupService(collector))
    window.trust_keys_button.click()
    assert not errors
    assert messages and "현재 저장소 상태를 확인할 수 없습니다" in messages[0]
    assert path.read_text(encoding="utf-8") == "{corrupt"
    window.close()


@pytest.mark.gui
def test_visible_log_warning_fits_existing_window_width(app: QApplication, tmp_path: Path) -> None:
    window = MainWindow(service=FakeService(tmp_path))
    window.resize(840, 640)
    window.show()
    app.processEvents()
    initial_width = window.width()
    warning = (
        "진단 로그를 저장하지 못했습니다. 설정 파일과 결과 보고서는 계속 저장합니다. "
        "출력 경로의 권한과 여유 공간을 확인하세요."
    )
    window._on_worker_success(
        BackupOutcome(tmp_path, (), tmp_path / "result.xlsx", warnings=(warning,))
    )
    app.processEvents()
    assert warning in window.status_label.text()
    assert window.width() == initial_width
    required_height = (
        window.status_label.fontMetrics()
        .boundingRect(
            0,
            0,
            window.status_label.width(),
            10000,
            Qt.TextFlag.TextWordWrap,
            window.status_label.text(),
        )
        .height()
    )
    assert window.status_label.height() >= required_height
    window.close()


@pytest.mark.gui
def test_terminal_success_warning_survives_cancel_during_native_cleanup(
    app: QApplication, tmp_path: Path
) -> None:
    service_release = threading.Event()
    cleanup_entered = threading.Event()
    cleanup_release = threading.Event()
    cleanup_done = threading.Event()
    warning = "진단 로그를 저장하지 못했습니다. 출력 경로를 확인하세요."

    class WarningService(FakeService):
        def run(self, request: BackupRequest, callbacks: BackupCallbacks) -> BackupOutcome:
            assert service_release.wait(3)
            outcome = super().run(request, callbacks)
            return BackupOutcome(
                outcome.run_directory, outcome.results, outcome.report_path, warnings=(warning,)
            )

    service = WarningService(tmp_path)
    window = MainWindow(service=service)
    window.ip_input.setPlainText("192.0.2.10")
    window.username_input.setText("operator")
    window.password_input.setText("secret-password")
    window.output_input.setText(str(tmp_path))
    window.show()
    window.start_button.click()
    thread = window._thread
    assert thread is not None

    def native_cleanup() -> None:
        cleanup_entered.set()
        cleanup_release.wait(3)
        cleanup_done.set()

    thread.finished.connect(native_cleanup, Qt.ConnectionType.DirectConnection)
    try:
        service_release.set()
        _wait_until(app, lambda: cleanup_entered.is_set() and warning in window.status_label.text())
        terminal_status = window.status_label.text()
        assert "백업 완료" in terminal_status
        assert not cleanup_done.is_set()
        assert window._thread is thread
        assert not window.start_button.isEnabled()
        window.cancel_button.click()
        window._cancel_backup()  # Closing also reaches this handler during cleanup.
        assert window._cancel_event is not None
        assert not window._cancel_event.is_set()
        cleanup_release.set()
        _wait_until(app, lambda: window.start_button.isEnabled())
        assert cleanup_done.is_set()
        assert window.status_label.text() == terminal_status
        assert not service.cancelled
        # A later run must become cancellable again before its terminal outcome.
        service_release.clear()
        window.password_input.setText("secret-password")
        window.start_button.click()
        window.cancel_button.click()
        _wait_until(app, lambda: service.cancelled)
        assert window._cancel_event is not None
        assert window._cancel_event.is_set()
        service_release.set()
        _wait_until(app, lambda: window.start_button.isEnabled())
    finally:
        service_release.set()
        cleanup_release.set()
        # Process events until production has joined/deleted its thread; avoid
        # using a QThread wrapper after deleteLater has already disposed of it.
        _wait_until(app, lambda: window._thread is None)
        window.close()
        app.processEvents()
