"""Local Windows folder actions and per-device backup progress."""

from __future__ import annotations

import base64
import os
import secrets
import subprocess
import threading
from pathlib import Path

from aruba2930f_backup.models import CollectionStage

from .backup_destination import selected_directory


class HistoryFolders:
    """Only paths found while reading an actual history report or journal are registered."""

    def __init__(self):
        self.sections = {}

    def register(self, section, rows, *, path_field, roots):
        mapping, handles = {}, {}
        trusted = [root.resolve() for root in roots]
        for row in rows:
            original = (
                row.pop(path_field, "") if path_field.startswith("_") else row.get(path_field, "")
            )
            if not original:
                continue
            path = Path(original)
            if path in handles:
                row["folder_id"] = handles[path]
                continue
            try:
                if (
                    path.resolve(strict=True) != path
                    or not path.is_file()
                    or not any(path.is_relative_to(root) for root in trusted)
                ):
                    continue
            except OSError, RuntimeError:
                continue
            key = secrets.token_hex(16)
            handles[path] = key
            mapping[key] = path
            row["folder_id"] = key
        self.sections[section] = mapping
        return rows

    def open(self, token):
        if not isinstance(token, str):
            raise ValueError("기록을 다시 불러온 뒤 폴더를 선택하세요.")
        path = next(
            (mapping[token] for mapping in self.sections.values() if token in mapping), None
        )
        if path is None or not path.is_file() or path.resolve() != path:
            raise ValueError("저장된 기록 파일을 찾을 수 없습니다. 목록을 새로고침하세요.")
        try:
            os.startfile(path.parent, "explore")
        except OSError as exc:
            raise ValueError("탐색기에서 기록 폴더를 열 수 없습니다.") from exc
        return {"opened": True}


_FOLDER_PICKER = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()
# A TopMost property on an unshown form does not reliably raise its
# owned modal dialog above the browser. Show an almost transparent, 1px
# owner on the monitor where the operator clicked before opening the picker.
$owner = New-Object System.Windows.Forms.Form
$owner.ShowInTaskbar = $false
$owner.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
$owner.Size = [System.Drawing.Size]::new(1, 1)
$owner.Opacity = 0.01
$monitor = [System.Windows.Forms.Screen]::FromPoint([System.Windows.Forms.Cursor]::Position)
$owner.Location = [System.Drawing.Point]::new(
    [int]($monitor.WorkingArea.Left + $monitor.WorkingArea.Width / 2),
    [int]($monitor.WorkingArea.Top + $monitor.WorkingArea.Height / 2)
)
$owner.TopMost = $true
$dlg = New-Object System.Windows.Forms.FolderBrowserDialog
$dlg.Description = '백업을 저장할 폴더를 선택하세요.'
try {
    $owner.Show()
    [System.Windows.Forms.Application]::DoEvents()
    $owner.Activate()
    $owner.BringToFront()
    if ($dlg.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
        [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
        [Console]::Write($dlg.SelectedPath)
    }
} finally { $dlg.Dispose(); $owner.Dispose() }
"""


def pick_directory(cancel):
    system = (
        Path(os.environ["SYSTEMROOT"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    command = base64.b64encode(_FOLDER_PICKER.encode("utf-16-le")).decode("ascii")
    try:
        with subprocess.Popen(
            [system, "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand", command],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) as dialog:
            while True:
                try:
                    output, _ = dialog.communicate(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    if cancel.is_set():
                        dialog.terminate()
                        dialog.communicate()
                        return {"directory": ""}
            if dialog.returncode:
                raise OSError("folder picker")
    except OSError as exc:
        raise ValueError(
            "Windows 폴더 선택기를 열 수 없습니다. 폴더의 전체 경로를 입력하세요."
        ) from exc
    selected = output.decode("utf-8").strip()
    return {"directory": str(selected_directory(selected)) if selected else ""}


STAGES = {
    "queued": "대기",
    "host_key_checking": "호스트 키 확인",
    "connecting": "SSH 접속",
    "enabling": "Enable 진입",
    "disabling_paging": "페이징 해제",
    "setting_terminal_width": "터미널 폭 설정",
    "reading_version": "버전 조회",
    "reading_modules": "모듈 조회",
    "validating_model": "모델 검증",
    "reading_config": "구성 수집",
    "verifying_prompt": "프롬프트 검증",
    "retrying": "다시 접속",
    "retry_queued": "재시도 대기",
    "retry_wait": "재시도 대기",
    "completed": "구성 수집 완료",
    "failed": "실패",
    "cancelled": "취소",
    "trusted": "지문 확인 완료",
    "unknown": "새 지문 승인 대기",
    "changed": "지문 변경",
    "rejected": "지문 확인 실패",
}


class BackupProgress:
    def __init__(self, operations, targets):
        self.operations = operations
        self.lock = threading.Lock()
        self.rows = {
            t: {
                "endpoint": t.endpoint,
                "phase": "host_key",
                "stage": "대기",
                "host_key_attempts": 0,
                "attempts": 0,
                "error_code": "",
            }
            for t in targets
        }
        self.finished = set()
        self.operations._backup_progress = self
        self.publish("호스트 키 확인 중")

    def publish(self, message):
        self.operations.progress = dict(
            message=message, completed=len(self.finished), total=len(self.rows), backup_detail=True
        )

    def snapshot(self):
        with self.lock:
            return dict(
                self.operations.progress, devices=[row.copy() for row in self.rows.values()]
            )

    def record(self, event, phase):
        with self.lock:
            row = self.rows[event.target]
            row.update(
                phase=phase,
                stage=STAGES.get(event.stage, event.stage.value),
                error_code=event.error_code.value if event.error_code else "",
            )
            attempts = "host_key_attempts" if phase == "host_key" else "attempts"
            if event.stage not in {
                CollectionStage.RETRY_WAIT,
                CollectionStage.RETRY_QUEUED,
                CollectionStage.RETRYING,
            }:
                row[attempts] = max(row[attempts], event.attempt)
            if phase == "backup" and event.stage in {
                CollectionStage.COMPLETED,
                CollectionStage.CANCELLED,
            }:
                self.finished.add(event.target)
            self.publish(row["stage"])

    def host_keys(self, checks):
        for check in checks:
            row = self.rows[check.target]
            row.update(
                host_key_attempts=check.attempts,
                stage=STAGES.get(check.state.value, check.state.value),
                error_code=check.error_code.value if check.error_code else "",
            )
            if check.state.value != "trusted":
                self.finished.add(check.target)
        self.publish("구성 수집 중")
