"""Read the desktop Devices sheet from dated backup directories, without opening configurations."""

from __future__ import annotations

import re
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook


def _directories(root: Path, name_pattern: str) -> list[Path]:
    return sorted(
        (
            path
            for path in root.iterdir()
            if re.fullmatch(name_pattern, path.name)
            and path.is_dir()
            and path.resolve().parent == root
        ),
        reverse=True,
    )


def _reports(root: Path):
    count = 0
    for day in _directories(root, r"\d{4}-\d{2}-\d{2}"):
        for run in _directories(day, r"\d{6}"):
            report = run / "result.xlsx"
            if report.is_file() and report.resolve().parent == run:
                yield report
                count += 1
                if count >= 30:
                    return


def _device_rows(path: Path) -> list[dict]:
    if path.stat().st_size > 8 * 1024 * 1024:
        return []
    with ZipFile(path) as archive:
        if sum(info.file_size for info in archive.infolist()) > 32 * 1024 * 1024:
            return []
    workbook = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    try:
        if "Devices" not in workbook.sheetnames:
            return []
        # Existing EXE export uses the English headers regardless of the UI language.
        records = workbook["Devices"].iter_rows(max_row=301, max_col=40, values_only=True)
        headers = next(records, ())
        if not {"Hostname", "IP", "Status"}.issubset(headers):
            return []
        indices = {key.lower(): headers.index(key) for key in ("Hostname", "IP", "Status")}
        return [
            {key: str(row[index] or "")[:255] for key, index in indices.items()}
            for row in records
            if row[indices["ip"]]
        ]
    finally:
        workbook.close()


def backup_history(desktop: Path, web: Path, *, extra: Path | None = None) -> list[dict]:
    rows = []
    sources = [("web", web), ("desktop", desktop)]
    if extra is not None and extra not in (web.resolve(), desktop.resolve()):
        sources.append(("selected", extra))
    for source, base in sources:
        root = base.resolve()
        if not root.is_dir():
            continue
        try:
            for report in _reports(root):
                try:
                    for row in _device_rows(report):
                        rows.append(
                            dict(
                                row,
                                source=source,
                                run=report.parent.relative_to(root).as_posix(),
                                path=str(report),
                            )
                        )
                except BadZipFile, OSError, KeyError, ValueError, SyntaxError:
                    continue
        except OSError:
            continue
    return sorted(rows, key=lambda row: row["run"], reverse=True)
