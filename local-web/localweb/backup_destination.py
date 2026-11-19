"""Place completed backup artifacts into the Windows folder selected by the operator."""

from __future__ import annotations

import os
import re
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from shutil import copyfileobj

from aruba2930f_backup.storage import create_run_directory


def _valid_drive(path: Path) -> bool:
    if os.name != "nt":
        return True
    import ctypes

    if path.drive.startswith("\\\\"):
        return bool(re.fullmatch(r"\\\\[^\\/?*:]+\\[^\\/?*:]+", path.drive))
    return bool(re.fullmatch(r"[A-Za-z]:", path.drive)) and ctypes.windll.kernel32.GetDriveTypeW(
        path.anchor
    ) in {2, 3, 4, 6}


def selected_directory(value: object) -> Path | None:
    if value is None or value == "":
        return None
    if (
        not isinstance(value, str)
        or "\0" in value
        or value.replace("/", "\\").startswith(("\\\\?\\", "\\\\.\\", "\\??\\"))
    ):
        raise ValueError("백업 폴더의 전체 경로를 입력하세요.")
    path = Path(value)
    if not path.is_absolute() or not _valid_drive(path):
        raise ValueError("드라이브 경로 또는 공유 폴더의 전체 경로를 입력하세요.")
    if os.name == "nt" and any(os.path.isreserved(part) for part in path.parts[1:]):
        raise ValueError("장치 이름이나 특수 경로는 백업 폴더로 사용할 수 없습니다.")
    try:
        resolved = path.resolve()
        if resolved.exists() and not resolved.is_dir():
            raise ValueError("백업 폴더에 파일 경로가 입력되었습니다.")
    except (OSError, ValueError) as exc:
        raise ValueError("선택한 백업 폴더를 찾을 수 없습니다.") from exc
    if not _valid_drive(resolved):
        raise ValueError("백업 폴더의 드라이브 경로를 확인하세요.")
    return resolved


def copy_backup_files(directory: Path, canonical: Path, names: list[str]) -> tuple[Path, list[str]]:
    """Copy only this run's known output filenames. Canonical copies remain downloadable."""
    if directory == canonical.parent.parent:
        return canonical, []
    now = datetime.strptime(f"{canonical.parent.name}/{canonical.name}", "%Y-%m-%d/%H%M%S")
    try:
        if selected_directory(str(directory)) != directory:
            raise ValueError("백업 폴더 경로가 변경되었습니다.")
        for day in (now, now + timedelta(days=1)):
            expected = directory / day.strftime("%Y-%m-%d")
            if expected.resolve() != expected:
                raise ValueError("지정한 날짜 폴더 경로가 변경되었습니다.")
        copy_dir = create_run_directory(directory, now=now)
        if copy_dir.resolve().parent.parent != directory:
            raise ValueError("선택한 폴더 밖에는 백업할 수 없습니다.")
    except OSError, ValueError:
        return canonical, [
            "선택한 폴더에 실행 폴더를 만들 수 없어 기본 데이터 폴더에 백업했습니다."
        ]

    failed = []
    for name in names:
        source = canonical / name
        destination = copy_dir / name
        try:
            if source.resolve().parent != canonical or source.resolve().name != name:
                raise ValueError("실행 폴더의 백업 파일 경로가 변경되었습니다.")
            with source.open("rb") as input_stream:
                output_stream = destination.open("xb")
                try:
                    with output_stream:
                        copyfileobj(input_stream, output_stream)
                        output_stream.flush()
                        os.fsync(output_stream.fileno())
                except OSError:
                    with suppress(OSError):
                        destination.unlink()
                    raise
        except OSError, ValueError:
            failed.append(name)
    warnings = []
    if failed:
        sample = ", ".join(failed[:5])
        if len(failed) > 5:
            sample += " 외 " + str(len(failed) - 5) + "개"
        warnings.append(
            f"선택한 폴더에 총 {len(failed)}개 파일을 복사하지 못했습니다 ({sample}). 기본 데이터 폴더에서 다운로드할 수 있습니다."
        )
    return (canonical if len(failed) == len(names) else copy_dir), warnings
