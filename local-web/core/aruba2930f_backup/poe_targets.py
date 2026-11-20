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
from .port_control import validate_query_interface

MAX_PASTE_CHARACTERS = 100_000
MAX_PROFILE_BYTES = 1024 * 1024
_PROFILE_FORMAT = "aruba2930f-poe-targets"


def parse_interfaces(text: str) -> tuple[str, ...]:
    """Accept explicit single ports only, retaining first occurrence order."""
    if not isinstance(text, str) or len(text) > MAX_PASTE_CHARACTERS:
        raise ValueError("인터페이스 입력이 올바르지 않거나 너무 큽니다.")
    ports = tuple(dict.fromkeys(validate_query_interface(part.strip()) for part in text.split(",")))
    if len(ports) > MAX_INTERFACES_PER_TARGET:
        raise ValueError("스위치당 인터페이스는 최대 384개입니다.")
    return ports


def make_target(
    ip: str, ssh_port: str | int, interfaces: str, label: str = "", *, allow_all_ports: bool = False
) -> PoETarget:
    if isinstance(ssh_port, str):
        if not re.fullmatch(r"[0-9]{1,5}", ssh_port.strip()):
            raise ValueError("SSH 포트는 1~65535 사이의 정수로 입력하세요.")
        port = int(ssh_port.strip())
    elif type(ssh_port) is int:
        port = ssh_port
    else:
        raise ValueError("SSH 포트는 정수로 입력하세요.")
    if allow_all_ports and isinstance(interfaces, str) and not interfaces.strip():
        return PoETarget(DeviceTarget(ip, port), (), label, all_ports=True)
    return PoETarget(DeviceTarget(ip, port), parse_interfaces(interfaces), label)


def merge_targets(targets: Sequence[PoETarget]) -> tuple[PoETarget, ...]:
    """Merge matching endpoints only; reject conflicting nonempty labels."""
    merged: dict[DeviceTarget, PoETarget] = {}
    total_interfaces = 0
    for group in targets:
        if not isinstance(group, PoETarget):
            raise ValueError("올바른 조회 대상이 필요합니다.")
        previous = merged.get(group.target)
        if previous is not None:
            if previous.label and group.label and previous.label != group.label:
                raise ValueError("같은 접속 대상의 이름이 서로 다릅니다.")
            all_ports = previous.all_ports or group.all_ports
            merged_interfaces = (
                () if all_ports else tuple(dict.fromkeys((*previous.interfaces, *group.interfaces)))
            )
            total_interfaces += len(merged_interfaces) - len(previous.interfaces)
            group = PoETarget(
                group.target, merged_interfaces, previous.label or group.label, all_ports
            )
        else:
            total_interfaces += len(group.interfaces)
        merged[group.target] = group
        if len(merged) > MAX_TARGETS:
            raise ValueError("접속 대상은 최대 300개입니다.")
        if total_interfaces > MAX_TOTAL_INTERFACES:
            raise ValueError("전체 인터페이스는 최대 14,400개입니다.")
    return tuple(merged.values())


def _check_quotes(text: str) -> None:
    # The spreadsheet dialect still permits a bare quote in an unquoted cell.
    # Reject it rather than silently changing a pasted target/label.
    state = "start"
    for character in text:
        if state == "quoted":
            if character == '"':
                state = "closed"
        elif state == "closed":
            if character == '"':
                state = "quoted"
            elif character in "\t\r\n":
                state = "start"
            else:
                raise ValueError("표의 따옴표 형식이 올바르지 않습니다.")
        elif character == '"':
            if state != "start":
                raise ValueError("표의 따옴표 형식이 올바르지 않습니다.")
            state = "quoted"
        elif character in "\t\r\n":
            state = "start"
        else:
            state = "unquoted"
    if state == "quoted":
        raise ValueError("표의 따옴표를 닫아 주세요.")


def _is_header(row: list[str]) -> bool:
    aliases = (
        {"ip", "sw ip", "switch ip", "ip 주소", "스위치 ip", "스위치ip", "스위치 ip 주소"},
        {"ssh port", "ssh_port", "ssh 포트"},
        {
            "interfaces",
            "interface",
            "interface list",
            "ports",
            "port",
            "포트",
            "포트 번호",
            "포트번호",
            "인터페이스",
            "인터페이스 목록",
        },
        {"label", "name", "이름", "이름(선택)"},
    )
    selected = (aliases[0], aliases[2]) if len(row) == 2 else aliases[: len(row)]
    return all(cell.strip().casefold() in names for cell, names in zip(row, selected, strict=True))


def parse_pasted_targets(text: str, *, allow_all_ports: bool = False) -> tuple[PoETarget, ...]:
    """Parse tab-separated cells copied directly from a spreadsheet."""
    if not isinstance(text, str) or len(text) > MAX_PASTE_CHARACTERS:
        raise ValueError("붙여넣기는 최대 100,000자입니다.")
    text = text.removeprefix("\ufeff")
    _check_quotes(text)
    try:
        rows = [
            row
            for row in csv.reader(io.StringIO(text, newline=""), dialect="excel-tab", strict=True)
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
    has_header = _is_header(rows[0])
    if has_header:
        rows = rows[1:]
    if not rows:
        raise ValueError("조회할 대상을 입력하세요.")
    allowed_widths = (2,) if basic_schema else (3, 4)
    if any(len(row) not in allowed_widths for row in rows):
        raise ValueError(
            "한 표에는 IP / 인터페이스 2열과 SSH 포트를 포함한 3~4열을 섞을 수 없습니다."
        )
    parsed: list[PoETarget] = []
    for line_number, row in enumerate(rows, start=2 if has_header else 1):
        try:
            target = (
                make_target(row[0], 22, row[1], allow_all_ports=allow_all_ports)
                if basic_schema
                else make_target(
                    row[0],
                    row[1],
                    row[2],
                    row[3] if len(row) == 4 else "",
                    allow_all_ports=allow_all_ports,
                )
            )
        except ValueError as exc:
            raise ValueError(f"{line_number}행: {exc}") from exc
        parsed.append(target)
    return merge_targets(parsed)


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
        "version": 2 if any(group.all_ports for group in groups) else 1,
        "targets": [
            {
                "ip": group.target.ip,
                "ssh_port": group.target.port,
                "interfaces": list(group.interfaces),
                "label": group.label,
                **({"all_ports": True} if group.all_ports else {}),
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
        or document["version"] not in (1, 2)
    ):
        raise ValueError("지원하지 않는 대상 목록 형식 또는 버전입니다.")
    entries = document["targets"]
    if not isinstance(entries, list) or len(entries) > MAX_TARGETS:
        raise ValueError("대상 목록의 접속 대상 배열을 확인하세요.")
    groups = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("대상 목록의 대상 항목이 올바르지 않습니다.")
        fields = {"ip", "ssh_port", "interfaces", "label"}
        if document["version"] == 2 and "all_ports" in entry:
            fields.add("all_ports")
        if set(entry) != fields:
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
                DeviceTarget(entry["ip"], entry["ssh_port"]),
                tuple(interfaces),
                entry["label"],
                all_ports=entry.get("all_ports", False),
            )
        )
    return merge_targets(groups)
