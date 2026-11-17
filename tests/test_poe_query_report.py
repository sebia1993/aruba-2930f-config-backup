"""Exports preserve actual measurements and prevent spreadsheet formulas."""

from __future__ import annotations

import csv
import io
import json
import os
import tracemalloc
from decimal import Decimal
from pathlib import Path

import pytest

from aruba2930f_backup.models import DeviceIdentity, ErrorCode
from aruba2930f_backup.poe_power import (
    AP535_REFERENCE_ID,
    AP535_REFERENCE_URL,
    AssessmentStatus,
    LLDPNeighbor,
    LLDPObservation,
    LLDPStatus,
    PowerAssessment,
    PowerEvidence,
)
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


def test_csv_retains_switch_identity_when_every_port_query_fails(tmp_path: Path) -> None:
    group = make_target("192.0.2.20", 22, "1,2")
    identity = DeviceIdentity("LAB-SW-20", "Aruba 2930F", "JL256A")
    results = [
        PoEQueryResult(
            group,
            port,
            QueryStatus.FAILED,
            identity=identity,
            error_code=ErrorCode.COMMAND_REJECTED,
            message="포트 응답을 해석할 수 없습니다.",
        )
        for port in group.interfaces
    ]
    path = tmp_path / "failed.csv"
    write_query_csv(path, results)
    rows = read_rows(path)
    assert [row["hostname"] for row in rows] == ["LAB-SW-20", "LAB-SW-20"]
    assert [row["model"] for row in rows] == ["Aruba 2930F", "Aruba 2930F"]
    assert [row["watts"] for row in rows] == ["", ""]


@pytest.mark.parametrize("lldp_status", [LLDPStatus.FAILED, LLDPStatus.CANCELLED])
def test_csv_independent_lldp_failure_preserves_poe_and_phase_times(
    tmp_path: Path, lldp_status: LLDPStatus
) -> None:
    group = make_target("192.0.2.10", 22, "12")
    snapshot = PortSnapshot(
        group.target,
        "12",
        "LAB-SW-10",
        "Aruba 2930F",
        True,
        "Up",
        True,
        "Delivering",
        8.7,
        observed_at="2026-11-17T10:30:01+09:00",
    )
    code = ErrorCode.COMMAND_TIMEOUT if lldp_status is LLDPStatus.FAILED else ErrorCode.CANCELLED
    result = PoEQueryResult(
        group,
        "12",
        QueryStatus.OK,
        snapshot,
        poe_finished_at="2026-11-17T10:30:02+09:00",
        lldp=LLDPObservation(
            status=lldp_status,
            error_code=code,
            message="LLDP 단계가 완료되지 않았습니다.",
            observed_at="2026-11-17T10:30:22+09:00",
        ),
        finished_at="2026-11-17T10:30:23+09:00",
    )
    path = tmp_path / "partial.csv"
    write_query_csv(path, [result])
    row = read_rows(path)[0]
    assert row["status"] == "ok"
    assert row["watts"] == "8.7"
    assert row["observed_at"] == "2026-11-17T01:30:01+00:00"
    assert row["poe_finished_at"] == "2026-11-17T01:30:02+00:00"
    assert row["lldp_observed_at"] == "2026-11-17T01:30:22+00:00"
    assert row["finished_at"] == "2026-11-17T01:30:23+00:00"
    assert row["complete"] == "yes"
    assert row["lldp_status"] == lldp_status.value
    assert row["lldp_error_code"] == code.value
    assert row["lldp_message"] == "LLDP 단계가 완료되지 않았습니다."
    assert row["error_code"] == ""
    assert row["ap_names"] == ""


def test_csv_keeps_multiple_neighbors_and_decimal_provenance_in_one_port_row(
    tmp_path: Path,
) -> None:
    group = make_target("192.0.2.10", 22, "1/24")
    power = PowerEvidence(
        requested_watts=Decimal("25.50"),
        allocated_watts=Decimal("25.50"),
        plc_class=4,
        plc_type=2,
        allocation_method="lldp",
        allocation_field="PSE Allocated Power Value",
    )
    neighbors = (
        LLDPNeighbor(
            system_name="AP-01",
            system_description="ArubaOS (MODEL:535)",
            model="AP-535",
            chassis_id="00:00:00:00:00:01",
            port_id="eth0",
            management_addresses=("192.0.2.101",),
            power=power,
        ),
        LLDPNeighbor(system_name="AP-02", port_id="eth1"),
    )
    result = PoEQueryResult(
        group,
        "1/24",
        QueryStatus.OK,
        PortSnapshot(
            group.target, "1/24", "LAB-SW", "Aruba 2930F", True, "Up", True, "Delivering", 8.7
        ),
        power_evidence=power,
        lldp=LLDPObservation(status=LLDPStatus.OK, neighbors=neighbors),
        assessment=PowerAssessment(
            status=AssessmentStatus.UNKNOWN,
            label="확인 필요",
            reason="LLDP 이웃이 여러 개이므로 연결 모델을 하나로 확정할 수 없습니다.",
            model="AP-535",
            model_source="ambiguous",
            requested_watts=Decimal("25.50"),
            allocated_watts=Decimal("25.50"),
            negotiation="802.3at Class 4",
        ),
    )
    path = tmp_path / "neighbors.csv"
    write_query_csv(path, [result])
    rows = read_rows(path)
    assert len(rows) == 1
    row = rows[0]
    assert row["interface"] == "1/24"
    assert row["watts"] == "8.7"
    assert row["ap_names"] == "AP-01 | AP-02"
    parsed_neighbors = json.loads(row["lldp_neighbors"])
    assert [neighbor["system_name"] for neighbor in parsed_neighbors] == ["AP-01", "AP-02"]
    assert parsed_neighbors[0]["management_addresses"] == ["192.0.2.101"]
    assert parsed_neighbors[0]["power"]["requested_watts"] == "25.50"
    assert parsed_neighbors[1]["power"] is None
    assert json.loads(row["power_evidence"])["plc_type"] == 2
    assert row["requested_watts"] == "25.50"
    assert row["allocated_watts"] == "25.50"
    assert row["assessment_status"] == "unknown"
    assert row["assessment_label"] == "확인 필요"
    assert row["assessment_model"] == "AP-535"
    assert row["assessment_model_source"] == "ambiguous"
    assert row["negotiation"] == "802.3at Class 4"
    assert row["reference_id"] == AP535_REFERENCE_ID
    assert row["reference_url"] == AP535_REFERENCE_URL


@pytest.mark.parametrize("text", ["=HYPERLINK(1)", "\t+1", "\r@SUM(1)"])
def test_csv_new_device_text_fields_remain_spreadsheet_safe(tmp_path: Path, text: str) -> None:
    group = make_target("192.0.2.1", 22, "1")
    result = PoEQueryResult(
        group,
        "1",
        QueryStatus.FAILED,
        identity=DeviceIdentity(text, text, None),
        lldp=LLDPObservation(
            status=LLDPStatus.OK,
            neighbors=(LLDPNeighbor(system_name=text, system_description=text),),
            message=text,
        ),
        assessment=PowerAssessment(
            status=AssessmentStatus.UNKNOWN,
            label=text,
            reason=text,
            model=text,
            model_source=text,
            negotiation=text,
        ),
    )
    path = tmp_path / "safe-new-fields.csv"
    write_query_csv(path, [result])
    row = read_rows(path)[0]
    for field in (
        "hostname",
        "model",
        "ap_names",
        "lldp_message",
        "assessment_label",
        "assessment_reason",
        "assessment_model",
        "assessment_model_source",
        "negotiation",
    ):
        assert row[field] == "'" + text
    assert json.loads(row["lldp_neighbors"])[0]["system_description"] == text


def test_csv_partial_and_completed_empty_lldp_remain_distinct(tmp_path: Path) -> None:
    group = make_target("192.0.2.1", 22, "1,2")
    results = [
        PoEQueryResult(group, "1", QueryStatus.OK, complete=False, lldp=LLDPObservation()),
        PoEQueryResult(group, "2", QueryStatus.OK, lldp=LLDPObservation(status=LLDPStatus.OK)),
    ]
    path = tmp_path / "empty.csv"
    write_query_csv(path, results)
    rows = read_rows(path)
    assert [row["complete"] for row in rows] == ["no", "yes"]
    assert [row["lldp_status"] for row in rows] == ["pending", "ok"]
    assert [row["lldp_neighbors"] for row in rows] == ["[]", "[]"]
    assert [row["watts"] for row in rows] == ["", ""]
    assert [row["allocated_watts"] for row in rows] == ["", ""]


def test_csv_does_not_fill_ambiguous_assessment_pair_from_other_raw_evidence(
    tmp_path: Path,
) -> None:
    group = make_target("192.0.2.1", 22, "1")
    result = PoEQueryResult(
        group,
        "1",
        QueryStatus.OK,
        power_evidence=PowerEvidence(
            requested_watts=Decimal("25.5"), allocated_watts=Decimal("25.5")
        ),
        assessment=PowerAssessment(
            status=AssessmentStatus.UNKNOWN,
            label="확인 필요",
            reason="서로 충돌하는 협상 정보",
        ),
    )
    path = tmp_path / "ambiguous.csv"
    write_query_csv(path, [result])
    row = read_rows(path)[0]
    assert row["requested_watts"] == ""
    assert row["allocated_watts"] == ""
    assert json.loads(row["power_evidence"])["allocated_watts"] == "25.5"


@pytest.mark.parametrize("phase", ["poe", "lldp"])
def test_csv_rejects_naive_phase_time_without_replacing_existing_file(
    tmp_path: Path, phase: str
) -> None:
    group = make_target("192.0.2.1", 22, "1")
    result = PoEQueryResult(
        group,
        "1",
        QueryStatus.OK,
        poe_finished_at="2026-11-17T10:30:01" if phase == "poe" else "",
        lldp=LLDPObservation(
            status=LLDPStatus.OK,
            observed_at="2026-11-17T10:30:02" if phase == "lldp" else "",
        ),
    )
    path = tmp_path / "existing.csv"
    path.write_bytes(b"original")
    with pytest.raises(ValueError, match="시간대"):
        write_query_csv(path, [result])
    assert path.read_bytes() == b"original"


def test_csv_explains_admin_disabled_and_poe_off_separately(tmp_path: Path) -> None:
    group = make_target("192.0.2.10", 22, "12,24")
    administrative_disable = PortSnapshot(
        group.target, "12", "LAB-SW-10", "Aruba 2930F", False, "Down", True, "Searching", 0.0
    )
    manual_power_off = PortSnapshot(
        group.target, "24", "LAB-SW-10", "Aruba 2930F", True, "Down", False, "사용 안 함", None
    )
    out = tmp_path / "causes.csv"
    write_query_csv(
        out,
        [
            PoEQueryResult(group, "12", QueryStatus.OK, administrative_disable),
            PoEQueryResult(group, "24", QueryStatus.OK, manual_power_off),
        ],
    )
    entries = read_rows(out)
    assert entries[0]["enabled"] == "no"
    assert entries[0]["poe_enabled"] == "yes"
    assert entries[0]["port_poe_state"] == "포트 Disable · PoE 대기"
    assert entries[0]["watts"] == "0"
    assert "PoE OFF라고 단정" in entries[0]["port_poe_explanation"]
    assert entries[1]["enabled"] == "yes"
    assert entries[1]["poe_enabled"] == "no"
    assert entries[1]["port_poe_state"] == "PoE OFF(설정)"
    assert entries[1]["watts"] == ""


def test_large_csv_uses_bounded_heap_without_changing_bytes(tmp_path: Path) -> None:
    group = make_target("192.0.2.10", 22, "1")
    result = PoEQueryResult(group, "1", QueryStatus.FAILED, message="x" * 2048)
    destination = tmp_path / "large.csv"

    tracemalloc.start()
    try:
        write_query_csv(destination, [result] * 4096)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert destination.stat().st_size > 8 * 1024 * 1024
    assert peak < 8 * 1024 * 1024
    with destination.open("rb") as handle:
        assert handle.read(3) == b"\xef\xbb\xbf"
