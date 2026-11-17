"""Atomic, spreadsheet-safe CSV output for switch-specific PoE query results."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .poe_power import AP535_REFERENCE_ID, AP535_REFERENCE_URL
from .poe_query_models import PoEQueryResult, QueryStatus
from .poe_targets import _atomic_write_bytes
from .port_power_diagnosis import diagnose_port_power

_FIELDS = (
    "ip",
    "ssh_port",
    "label",
    "hostname",
    "model",
    "interface",
    "status",
    "enabled",
    "link_status",
    "poe_enabled",
    "poe_status",
    "watts",
    "observed_at",
    "finished_at",
    "error_code",
    "message",
    "poe_finished_at",
    "complete",
    "lldp_status",
    "lldp_observed_at",
    "lldp_error_code",
    "lldp_message",
    "ap_names",
    "lldp_neighbors",
    "power_evidence",
    "assessment_status",
    "assessment_label",
    "assessment_reason",
    "assessment_model",
    "assessment_model_source",
    "requested_watts",
    "allocated_watts",
    "negotiation",
    "reference_id",
    "reference_url",
    "port_poe_state",
    "port_poe_explanation",
)


def _safe_string(value: str) -> str:
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def _utc_timestamp(value: str) -> str:
    if not value:
        return ""
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("조회 시각에는 시간대가 필요합니다.")
    return timestamp.astimezone(UTC).isoformat()


def _setting(value: bool | None) -> str:
    return "" if value is None else "yes" if value else "no"


def _decimal(value: Decimal | None) -> str:
    return "" if value is None else format(value, "f")


def _json_decimal(value: object) -> str:
    if isinstance(value, Decimal):
        return _decimal(value)
    raise TypeError(f"Cannot serialize {type(value).__name__} as power evidence")


def _detail_cells(result: PoEQueryResult) -> tuple[str, ...]:
    lldp = result.lldp
    assessment = result.assessment
    return (
        _utc_timestamp(result.poe_finished_at),
        _setting(result.complete),
        lldp.status.value if lldp else "",
        _utc_timestamp(lldp.observed_at) if lldp else "",
        lldp.error_code.value if lldp and lldp.error_code else "",
        lldp.message if lldp else "",
        " | ".join(neighbor.system_name for neighbor in lldp.neighbors if neighbor.system_name)
        if lldp
        else "",
        json.dumps(
            [asdict(neighbor) for neighbor in lldp.neighbors],
            ensure_ascii=False,
            default=_json_decimal,
        )
        if lldp
        else "",
        json.dumps(asdict(result.power_evidence), ensure_ascii=False, default=_json_decimal)
        if result.power_evidence
        else "",
        assessment.status.value if assessment else "",
        assessment.label if assessment else "",
        assessment.reason if assessment else "",
        assessment.model if assessment else "",
        assessment.model_source if assessment else "",
        _decimal(assessment.requested_watts) if assessment else "",
        _decimal(assessment.allocated_watts) if assessment else "",
        assessment.negotiation if assessment else "",
        assessment.reference_id if assessment else "",
        AP535_REFERENCE_URL if assessment and assessment.reference_id == AP535_REFERENCE_ID else "",
    )


def write_query_csv(path: Path, results: Sequence[PoEQueryResult]) -> None:
    """Write one row per result, preserving missing watts and original row times."""
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(_FIELDS)
    for result in results:
        snapshot = result.snapshot
        identity = result.identity
        watts = snapshot.watts if snapshot and result.status is QueryStatus.OK else None
        row: tuple[str | int, ...] = (
            result.group.target.ip,
            result.group.target.port,
            result.group.label,
            (snapshot.hostname if snapshot else "")
            or (identity.hostname or "" if identity else ""),
            (snapshot.model if snapshot else "") or (identity.model if identity else ""),
            result.interface,
            result.status.value,
            _setting(snapshot.enabled) if snapshot else "",
            snapshot.link_status if snapshot else "",
            _setting(snapshot.poe_enabled) if snapshot else "",
            snapshot.poe_status if snapshot else "",
            "" if watts is None else "0" if watts == 0 else str(watts),
            _utc_timestamp(snapshot.observed_at) if snapshot else "",
            _utc_timestamp(result.finished_at),
            result.error_code.value if result.error_code else "",
            result.message,
            *_detail_cells(result),
            diagnose_port_power(snapshot).label,
            diagnose_port_power(snapshot).reason,
        )
        writer.writerow(_safe_string(cell) if isinstance(cell, str) else cell for cell in row)
    _atomic_write_bytes(path, output.getvalue().encode("utf-8-sig"))
