"""Validate switch-specific interface lists and reusable target profiles."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from .models import DeviceTarget
from .poe_query_models import (
    MAX_INTERFACES_PER_TARGET,
    MAX_TARGETS,
    MAX_TOTAL_INTERFACES,
    PoETarget,
)
from .port_control import validate_interface

MAX_PASTE_CHARACTERS = 100_000
MAX_PROFILE_BYTES = 1024 * 1024
_PROFILE_FORMAT = "aruba2930f-poe-targets"


def parse_interfaces(text: str) -> tuple[str, ...]:
    """Accept explicit single ports only, retaining first occurrence order."""
    if not isinstance(text, str) or len(text) > MAX_PASTE_CHARACTERS:
        raise ValueError("인터페이스 입력이 올바르지 않거나 너무 큽니다.")
    ports = tuple(dict.fromkeys(validate_interface(part.strip()) for part in text.split(",")))
    if len(ports) > MAX_INTERFACES_PER_TARGET:
        raise ValueError("스위치당 인터페이스는 최대 384개입니다.")
    return ports


def make_target(ip: str, ssh_port: str | int, interfaces: str, label: str = "") -> PoETarget:
    if isinstance(ssh_port, str):
        if not re.fullmatch(r"[0-9]{1,5}", ssh_port.strip()):
            raise ValueError("SSH 포트는 1~65535 사이의 정수로 입력하세요.")
        port = int(ssh_port.strip())
    elif type(ssh_port) is int:
        port = ssh_port
    else:
        raise ValueError("SSH 포트는 정수로 입력하세요.")
    return PoETarget(DeviceTarget(ip, port), parse_interfaces(interfaces), label)


def merge_targets(targets: Sequence[PoETarget]) -> tuple[PoETarget, ...]:
    """Merge matching endpoints only; reject conflicting nonempty labels."""
    merged: dict[DeviceTarget, PoETarget] = {}
    for group in targets:
        if not isinstance(group, PoETarget):
            raise ValueError("올바른 조회 대상이 필요합니다.")
        previous = merged.get(group.target)
        if previous is not None:
            if previous.label and group.label and previous.label != group.label:
                raise ValueError("같은 접속 대상의 이름이 서로 다릅니다.")
            group = PoETarget(
                group.target,
                tuple(dict.fromkeys((*previous.interfaces, *group.interfaces))),
                previous.label or group.label,
            )
        merged[group.target] = group
        if len(merged) > MAX_TARGETS:
            raise ValueError("접속 대상은 최대 300개입니다.")
        if sum(len(item.interfaces) for item in merged.values()) > MAX_TOTAL_INTERFACES:
            raise ValueError("전체 인터페이스는 최대 14,400개입니다.")
    return tuple(merged.values())


def _check_quotes(text: str, delimiter: str) -> None:
    # csv.reader(strict=True) still permits a bare quote in an unquoted cell.
    # Reject it rather than silently changing a pasted target/label.
    state = "start"
    for character in text:
        if state == "quoted":
            if character == '"':
                state = "closed"
        elif state == "closed":
            if character == '"':
                state = "quoted"
            elif character == delimiter or character in "\r\n":
                state = "start"
            else:
                raise ValueError("표의 따옴표 형식이 올바르지 않습니다.")
        elif character == '"':
            if state != "start":
                raise ValueError("표의 따옴표 형식이 올바르지 않습니다.")
            state = "quoted"
        elif character == delimiter or character in "\r\n":
            state = "start"
        else:
            state = "unquoted"
    if state == "quoted":
        raise ValueError("표의 따옴표를 닫아 주세요.")


def _is_header(row: list[str]) -> bool:
    aliases = (
        {"ip", "sw ip", "switch ip", "ip 주소"},
        {"ssh port", "ssh_port", "ssh 포트"},
        {"interfaces", "interface", "interface list", "ports", "인터페이스", "인터페이스 목록"},
        {"label", "name", "이름", "이름(선택)"},
    )
    selected = (aliases[0], aliases[2]) if len(row) == 2 else aliases[: len(row)]
    return all(cell.strip().casefold() in names for cell, names in zip(row, selected, strict=True))


def _parse_table(text: str, delimiter: str) -> tuple[PoETarget, ...]:
    _check_quotes(text, delimiter)
    try:
        rows = [
            row
            for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
            if row
        ]
    except csv.Error as exc:
        raise ValueError("붙여넣은 표 형식이 올바르지 않습니다.") from exc
    if not rows or any(len(row) not in (2, 3, 4) for row in rows):
        raise ValueError(
            "표는 IP / 인터페이스 목록의 2열 또는 기존 SSH 포트를 포함한 3~4열이어야 합니다."
        )
    # A header declares column meaning for the whole block. Without a header,
    # the first row establishes the same schema; never reinterpret later rows.
    basic_schema = len(rows[0]) == 2
    if _is_header(rows[0]):
        rows = rows[1:]
    if not rows:
        raise ValueError("조회할 대상을 입력하세요.")
    allowed_widths = (2,) if basic_schema else (3, 4)
    if any(len(row) not in allowed_widths for row in rows):
        raise ValueError(
            "한 표에는 IP / 인터페이스 2열과 SSH 포트를 포함한 3~4열을 섞을 수 없습니다."
        )
    return merge_targets(
        [
            make_target(row[0], 22, row[1])
            if basic_schema
            else make_target(row[0], row[1], row[2], row[3] if len(row) == 4 else "")
            for row in rows
        ]
    )


def parse_pasted_targets(text: str) -> tuple[PoETarget, ...]:
    """Validate the complete TSV or quoted CSV before returning any targets."""
    if not isinstance(text, str) or len(text) > MAX_PASTE_CHARACTERS:
        raise ValueError("붙여넣기는 최대 100,000자입니다.")
    text = text.removeprefix("\ufeff")
    error: ValueError | None = None
    for delimiter in ("\t", ","):
        try:
            return _parse_table(text, delimiter)
        except ValueError as exc:
            error = exc
    assert error is not None
    raise error


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """Replace only after the complete same-directory temporary file is durable."""
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


def save_target_profile(path: Path, targets: Sequence[PoETarget]) -> None:
    groups = merge_targets(targets)
    document = {
        "format": _PROFILE_FORMAT,
        "version": 1,
        "targets": [
            {
                "ip": group.target.ip,
                "ssh_port": group.target.port,
                "interfaces": list(group.interfaces),
                "label": group.label,
            }
            for group in groups
        ],
    }
    content = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(content) > MAX_PROFILE_BYTES:
        raise ValueError("대상 목록 파일은 최대 1 MiB입니다.")
    _atomic_write_bytes(path, content)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("대상 목록에 중복 JSON 키가 있습니다.")
        result[key] = value
    return result


def load_target_profile(path: Path) -> tuple[PoETarget, ...]:
    with path.open("rb") as handle:
        content = handle.read(MAX_PROFILE_BYTES + 1)
    if len(content) > MAX_PROFILE_BYTES:
        raise ValueError("대상 목록 파일은 최대 1 MiB입니다.")
    try:
        document = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("UTF-8 JSON 대상 목록을 확인하세요.") from exc
    if not isinstance(document, dict) or set(document) != {"format", "version", "targets"}:
        raise ValueError("대상 목록의 최상위 항목이 올바르지 않습니다.")
    if (
        document["format"] != _PROFILE_FORMAT
        or type(document["version"]) is not int
        or document["version"] != 1
    ):
        raise ValueError("지원하지 않는 대상 목록 형식 또는 버전입니다.")
    entries = document["targets"]
    if not isinstance(entries, list) or len(entries) > MAX_TARGETS:
        raise ValueError("대상 목록의 접속 대상 배열을 확인하세요.")
    groups = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"ip", "ssh_port", "interfaces", "label"}:
            raise ValueError("대상 목록의 대상 항목이 올바르지 않습니다.")
        if (
            not isinstance(entry["ip"], str)
            or type(entry["ssh_port"]) is not int
            or not isinstance(entry["label"], str)
        ):
            raise ValueError("대상 목록의 IP, SSH 포트, 이름 타입을 확인하세요.")
        interfaces = entry["interfaces"]
        if not isinstance(interfaces, list) or not all(
            isinstance(port, str) for port in interfaces
        ):
            raise ValueError("대상 목록의 인터페이스 문자열 배열을 확인하세요.")
        groups.append(
            PoETarget(
                DeviceTarget(entry["ip"], entry["ssh_port"]), tuple(interfaces), entry["label"]
            )
        )
    return merge_targets(groups)
