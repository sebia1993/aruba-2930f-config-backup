"""Preview an operator's device table without storing it or contacting switches."""

import csv
import io
import unicodedata

from .ping_analysis_nms import MAX_BYTES, MAX_COLS, MAX_ROWS
from .ping_tools import inventory_hostname, unicast_ipv4

_LABELS = dict(hostname="장비 Hostname", swIp="SW IP", gwIp="GW IP", bbIp="BB", nmsIp="NMS server")
_ALIASES = {
    "hostname": {"hostname", "hostanme", "장비hostname", "장비hostanme", "장비명", "호스트명"},
    "swIp": {"swip", "switchip", "switchipv4", "스위치ip", "장비ip", "관리ip"},
    "gwIp": {"gw", "gwip", "gateway", "gatewayip", "defaultgateway", "게이트웨이", "게이트웨이ip"},
    "bbIp": {"bb", "bbip", "backbone", "backboneip", "백본", "백본ip"},
    "nmsIp": {"nms", "nmsip", "nmsserver", "nmsserverip", "nms서버", "nms서버ip"},
}


class InventoryInputError(ValueError):
    """Safe operator feedback for an inventory preview request."""


def _paste_table(text):
    if not isinstance(text, str) or len(text) > MAX_BYTES:
        raise InventoryInputError("붙여넣을 표는 3 MiB 이하로 입력하세요.")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise InventoryInputError("붙여넣은 표의 문자 인코딩을 확인하세요.") from exc
    if size > MAX_BYTES:
        raise InventoryInputError("붙여넣을 표는 3 MiB 이하로 입력하세요.")
    rows = []
    try:
        source = io.StringIO(text.removeprefix("\ufeff"), newline="")
        for row in csv.reader(source, delimiter="\t", strict=True):
            if len(rows) > MAX_ROWS or len(row) > MAX_COLS:
                raise InventoryInputError("표는 머리글 외 5,000행, 80열까지 지원합니다.")
            rows.append(row)
    except csv.Error as exc:
        raise InventoryInputError("엑셀에서 열 이름을 포함한 표를 복사해 붙여넣으세요.") from exc
    if len(rows) < 2 or not rows[0]:
        raise InventoryInputError("첫 행에 열 이름이 있고 하나 이상의 데이터 행이 필요합니다.")
    return rows[0], rows[1:]


def _headers_mapping(headers, overrides):
    if not isinstance(overrides, dict) or set(overrides) - _LABELS.keys():
        raise InventoryInputError("열 매핑 항목을 확인하세요.")
    normalized = [
        "".join(c for c in unicodedata.normalize("NFKC", name).casefold() if c.isalnum())
        for name in headers
    ]
    mapping, errors = {}, []
    for key, aliases in _ALIASES.items():
        if key in overrides:
            index = overrides[key]
            if index is not None and (type(index) is not int or not 0 <= index < len(headers)):
                raise InventoryInputError(f"{_LABELS[key]}에 사용할 열을 다시 선택하세요.")
        else:
            matches = [i for i, name in enumerate(normalized) if name in aliases]
            index = matches[0] if len(matches) == 1 else None
            if len(matches) > 1:
                errors.append(
                    f"{_LABELS[key]}에 해당하는 열이 여러 개입니다. 사용할 열을 선택하세요."
                )
        mapping[key] = index
    indices = [index for index in mapping.values() if index is not None]
    if len(indices) != len(set(indices)):
        errors.append("같은 원본 열을 여러 항목에 지정했습니다. 열 매핑을 확인하세요.")
    if mapping["swIp"] is None:
        errors.append("SW IP 열을 선택하세요.")
    if all(mapping[key] is None for key in ("gwIp", "bbIp", "nmsIp")):
        errors.append("GW IP, BB, NMS server 중 하나 이상의 열을 선택하세요.")
    return mapping, errors


def _preview_rows(data, mapping):
    rows, rows_by_ip = [], {}
    for number, cells in enumerate(data, 2):
        if not any(cells):
            continue
        row = dict.fromkeys(_LABELS, "")
        row.update(rowNumber=number, errors=[])
        for key, column in mapping.items():
            if column is None:
                continue
            value = cells[column].strip(" ") if column < len(cells) else ""
            try:
                if value.startswith("="):
                    raise InventoryInputError("수식 대신 값을 입력하세요.")
                row[key] = value[:100]
                if key == "hostname":
                    row[key] = inventory_hostname(value)
                elif value or key == "swIp":
                    row[key] = unicast_ipv4(value)
                    if key == "swIp":
                        rows_by_ip.setdefault(row[key], []).append(row)
            except ValueError as exc:
                row["errors"].append(f"{number}행 {_LABELS[key]} · {exc}")
        rows.append(row)
    for devices in rows_by_ip.values():
        if len(devices) > 1:
            for device in devices:
                device["errors"].append(
                    f"{device['rowNumber']}행 SW IP가 중복됩니다. 장비마다 한 행을 사용하세요."
                )
    return rows


def preview_inventory(payload):
    """Map a header row and validate individual records for explicit operator selection."""
    if (
        not isinstance(payload, dict)
        or "text" not in payload
        or payload.keys() - {"text", "mapping"}
    ):
        raise InventoryInputError("열 이름과 장비 행을 함께 복사해 붙여넣으세요.")
    headers, data = _paste_table(payload["text"])
    headers = [name.strip()[:100] for name in headers]
    mapping, errors = _headers_mapping(headers, payload.get("mapping", {}))
    rows = _preview_rows(data, mapping)
    if not rows:
        errors.append("데이터 행이 비어 있습니다.")
    return dict(headers=headers, mapping=mapping, rows=rows, errors=errors)
