from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from runpy import run_path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_release_notes_are_operator_focused_and_korean_first(tmp_path: Path) -> None:
    output = tmp_path / "release-notes.md"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "release_notes.py"),
            "--version",
            "0.1.8",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0, result.stderr
    notes = output.read_text(encoding="utf-8")

    assert "# Aruba 2930F 설정 백업 v0.1.8" in notes
    assert "## 릴리즈 요약" in notes
    assert "## 운영 영향" in notes
    assert "## 검증 결과" in notes
    assert "## 다운로드 파일" in notes
    assert "## 알려진 제한" in notes
    assert "## v0.1.8 변경 내역" in notes
    assert "Windows x64 portable onedir ZIP" not in notes
    assert "Prerelease" not in notes


def test_release_notes_highlights_do_not_lead_with_security_repetition(tmp_path: Path) -> None:
    output = tmp_path / "release-notes.md"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "release_notes.py"),
            "--version",
            "0.1.8",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    notes = output.read_text(encoding="utf-8")
    summary = notes.split("## 운영 영향", maxsplit=1)[0]

    assert "파일 이름" in summary
    assert "SSH 명령, 진단 코드와 운영 로그의 비민감정보 경계" not in summary


@pytest.mark.parametrize("version", ["0.2.0", "0.2.1", "0.10.0", "1.0.0"])
def test_port_control_release_notes_explain_configuration_changes(version: str) -> None:
    render = run_path(str(ROOT / "tools" / "release_notes.py"))["_render"]
    notes = render(version, "### 추가\n\n- 단일 포트 제어\n")

    assert "백업은 **읽기 전용 설정 수집**" in notes
    assert "별도 **포트 제어**" in notes
    assert "PoE OFF/ON" in notes
    assert "포트 Down/Up" in notes
    assert "running-config" in notes
    assert "`write memory`" in notes
    assert "결과 확인 불가" in notes
    assert "자동 재전송" in notes
    assert "설정 모드에 진입하거나 장비 구성을 변경하는 명령은 실행하지 않습니다." not in notes
    assert "설정 복원 및 장비 구성 변경 기능은 제공하지 않습니다." not in notes
    assert "설정 복원 및 포트 제어 이외의 장비 구성 변경" in notes
    assert "docs/PORT_CONTROL_KO.md" in notes
    assert f"Aruba2930FConfigBackup_v{version}_windows_x64.zip" in notes
    assert "배포 상태: 사전릴리즈" in notes


def test_legacy_release_notes_keep_the_original_read_only_scope() -> None:
    render = run_path(str(ROOT / "tools" / "release_notes.py"))["_render"]
    notes = render("0.1.8", "### 추가\n\n- 파일 이름 선택\n")

    assert "장비 측 기본 동작은 **읽기 전용 설정 수집**" in notes
    assert "설정 모드에 진입하거나 장비 구성을 변경하는 명령은 실행하지 않습니다." in notes
    assert "설정 복원 및 장비 구성 변경 기능은 제공하지 않습니다." in notes
    assert "포트 제어" not in notes
