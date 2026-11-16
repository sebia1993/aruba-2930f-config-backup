"""Exports preserve actual measurements and prevent spreadsheet formulas."""

from __future__ import annotations

import csv
import io
import os
from pathlib import Path

import pytest

from aruba2930f_backup.models import ErrorCode
from aruba2930f_backup.poe_query_models import PoEQueryResult, QueryStatus
from aruba2930f_backup.poe_query_report import write_query_csv
from aruba2930f_backup.poe_targets import make_target
from aruba2930f_backup.port_control import PortSnapshot


def read_rows(path: Path) -> list[dict[str, str]]:
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    return list(csv.DictReader(io.StringIO(path.read_bytes().decode("utf-8-sig"), newline="")))


def test_csv_zero_missing_and_per_row_timestamps(tmp_path: Path) -> None:
    group = make_target("192.0.2.1", 2222, "1,2,3", '동관, "서버"\n1층')
    zero = PortSnapshot(
        group.target,
        "1",
        "SW-01",
        "Aruba 2930F",
        True,
        "Up",
        True,
        "Searching",
        0.0,
        observed_at="2026-11-16T09:00:00+09:00",
    )
    unsupported = PortSnapshot(
        group.target,
        "2",
        "SW-01",
        "Aruba 2930F",
        True,
        "Down",
        None,
        "PoE 미지원",
        None,
        observed_at="2026-11-16T00:00:01+00:00",
    )
    results = [
        PoEQueryResult(group, "1", QueryStatus.OK, zero, finished_at="2026-11-16T09:00:02+09:00"),
        PoEQueryResult(
            group, "2", QueryStatus.UNSUPPORTED, unsupported, finished_at="2026-11-16T00:00:03Z"
        ),
        PoEQueryResult(
            group,
            "3",
            QueryStatus.FAILED,
            error_code=ErrorCode.COMMAND_TIMEOUT,
            message="시간 초과",
            finished_at="2026-11-16T00:00:04+00:00",
        ),
    ]
    path = tmp_path / "results.csv"
    write_query_csv(path, results)
    rows = read_rows(path)
    assert [(row["ip"], row["ssh_port"], row["interface"]) for row in rows] == [
        ("192.0.2.1", "2222", "1"),
        ("192.0.2.1", "2222", "2"),
        ("192.0.2.1", "2222", "3"),
    ]
    assert [row["watts"] for row in rows] == ["0", "", ""]
    assert rows[0]["label"] == '동관, "서버"\n1층'
    assert rows[0]["hostname"] == "SW-01"
    assert rows[0]["observed_at"] == "2026-11-16T00:00:00+00:00"
    assert [row["finished_at"] for row in rows] == [
        "2026-11-16T00:00:02+00:00",
        "2026-11-16T00:00:03+00:00",
        "2026-11-16T00:00:04+00:00",
    ]
    assert rows[2]["error_code"] == "COMMAND_TIMEOUT"
    assert rows[2]["message"] == "시간 초과"
    assert rows[2]["observed_at"] == ""


@pytest.mark.parametrize(
    "text", ["=1+1", "+CMD", "-CMD", "@SUM(1)", "  =1", "\t+1", "\r@1", "\n-1"]
)
def test_csv_formula_prefixes_in_all_text_fields_are_inert(tmp_path: Path, text: str) -> None:
    group = make_target("192.0.2.1", 22, "1", text)
    snapshot = PortSnapshot(group.target, "1", text, text, True, text, True, text, 1.25)
    result = PoEQueryResult(group, "1", QueryStatus.OK, snapshot, message=text)
    path = tmp_path / "safe.csv"
    write_query_csv(path, [result])
    row = read_rows(path)[0]
    for field in ("label", "hostname", "model", "link_status", "poe_status", "message"):
        assert row[field] == "'" + text
    assert row["watts"] == "1.25"


def test_csv_replace_failure_keeps_old_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "results.csv"
    path.write_bytes(b"previous report")
    group = make_target("192.0.2.1", 22, "1")

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        write_query_csv(path, [PoEQueryResult(group, "1", QueryStatus.CANCELLED)])
    assert path.read_bytes() == b"previous report"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["results.csv"]
