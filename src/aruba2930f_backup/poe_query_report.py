"""Atomic, spreadsheet-safe CSV output for switch-specific PoE query results."""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from .poe_query_models import PoEQueryResult, QueryStatus
from .poe_targets import _atomic_write_bytes

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


def write_query_csv(path: Path, results: Sequence[PoEQueryResult]) -> None:
    """Write one row per result, preserving missing watts and original row times."""
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(_FIELDS)
    for result in results:
        snapshot = result.snapshot
        watts = snapshot.watts if snapshot and result.status is QueryStatus.OK else None
        row: tuple[str | int, ...] = (
            result.group.target.ip,
            result.group.target.port,
            result.group.label,
            snapshot.hostname if snapshot else "",
            snapshot.model if snapshot else "",
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
        )
        writer.writerow(_safe_string(cell) if isinstance(cell, str) else cell for cell in row)
    _atomic_write_bytes(path, output.getvalue().encode("utf-8-sig"))
