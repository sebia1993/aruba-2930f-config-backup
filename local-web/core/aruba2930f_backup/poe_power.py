"""Immutable, port-bound LLDP evidence and AP-535 supply assessment."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from enum import StrEnum

from .models import CollectionFailure, ErrorCode
from .port_control import PoEState, validate_query_interface

AP535_REFERENCE_ID = "HPE-530-QuickSpecs-V22-2026-08-17"
AP535_REFERENCE_URL = "https://www.hpe.com/au/en/collaterals/collateral.a00060238enw.html"


class LLDPStatus(StrEnum):
    PENDING = "pending"
    OK = "ok"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AssessmentStatus(StrEnum):
    NORMAL = "normal"
    WARNING = "warning"
    OFF = "off"
    SEARCHING = "searching"
    ERROR = "error"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class PowerEvidence:
    requested_watts: Decimal | None = None
    allocated_watts: Decimal | None = None
    power_class: int | None = None
    plc_class: int | None = None
    plc_type: int | None = None
    dlc_class: int | None = None
    dlc_type: int | None = None
    allocation_method: str = ""
    device_type: str = ""
    allocation_field: str = ""
    issues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LLDPNeighbor:
    system_name: str = ""
    system_description: str = ""
    model: str = ""
    chassis_type: str = ""
    chassis_id: str = ""
    port_type: str = ""
    port_id: str = ""
    port_description: str = ""
    management_addresses: tuple[str, ...] = ()
    power: PowerEvidence | None = None
    system_capabilities_enabled: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LLDPObservation:
    status: LLDPStatus = LLDPStatus.PENDING
    neighbors: tuple[LLDPNeighbor, ...] = ()
    error_code: ErrorCode | None = None
    message: str = ""
    observed_at: str = ""


@dataclass(frozen=True, slots=True)
class PowerAssessment:
    status: AssessmentStatus
    label: str
    reason: str
    model: str = "AP-535"
    model_source: str = "configured"
    requested_watts: Decimal | None = None
    allocated_watts: Decimal | None = None
    negotiation: str = ""
    reference_id: str = AP535_REFERENCE_ID


# The pure parsers enforce the same bounded per-command envelope as queries.
_MAX_OUTPUT_BYTES = 1024 * 1024
_MAX_OUTPUT_LINES = 10_000
_POE_HEADER = re.compile(
    r"(?:Port Power Status|Status and Configuration Information)"
    r"[ \t]+for port[ \t]+([^\r\n]+)",
    re.IGNORECASE,
)
_LLDP_HEADER = re.compile(r"^\s*LLDP Remote Device Information Detail\s*$", re.IGNORECASE)
_LOCAL_PORT = re.compile(r"^\s*Local Port\s*:\s*(\S+)\s*$", re.IGNORECASE)
_CLI_ERROR = re.compile(
    r"(?:invalid input|unknown command|incomplete command|ambiguous command|"
    r"permission denied|access denied|not authorized|command not supported|"
    r"unrecognized command|error\s*:|timed?\s*out)",
    re.IGNORECASE,
)
# Power extraction consumes normalized single fields. Column boundaries are
# decoded separately from the documented PoE layout, never from arbitrary
# Configured Type text or neighbor descriptions.
_FIELD_LABELS = (
    "PD Requested Power Value",
    "PD Requested Power",
    "PSE Allocated Power Value",
    "PSE Allocated Power",
    "PSE Actual Power Value",
    "PLC Class/Type",
    "DLC Class/Type",
    "Power Class",
    "Alloc By Actual",
    "Poe Device Type",
    "PD TLV Sent Type",
)
_POWER_LABELS = frozenset(label.casefold() for label in _FIELD_LABELS)
_FIELD_RE = re.compile(
    r"^[ \t]*(" + "|".join(re.escape(label) for label in _FIELD_LABELS) + r")[ \t]*:[ \t]*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)
# The order of each documented two-column row establishes its boundary. The
# second field is the last field on that row, even if its value contains colons.
_POE_COLUMNS = {
    "power enable": "PoE Port Status",
    "plc class/type": "Priority Config",
    "dlc class/type": "Pre-std Detect",
    "alloc by config": "Configured Type",
    "alloc by actual": "PoE Value Config",
    "med lldp detect": "PD TLV Sent Type",
    "pse allocated power value": "PSE TLV Configured",
    "pd requested power value": "PSE TLV Sent Type",
    "pse reserved power": "PD Power Draw",
}


def _poe_power_lines(output: str) -> str:
    fields: list[str] = []
    for line in output.splitlines():
        match = re.fullmatch(r"[ \t]*([^:]+?)[ \t]*:[ \t]*(.*)", line)
        if match is None:
            continue
        label, value = match[1].strip(), match[2]
        second_label = _POE_COLUMNS.get(label.casefold())
        if second_label is not None:
            boundary = re.search(r"\b" + re.escape(second_label) + r"[ \t]*:", value, re.IGNORECASE)
            if (
                boundary is not None
                and boundary.start() >= 2
                and value[boundary.start() - 2 : boundary.start()].isspace()
            ):
                if second_label.casefold() in _POWER_LABELS:
                    fields.append(f"{second_label} :{value[boundary.end() :].strip()}")
                value = value[: boundary.start()].rstrip()
        if label.casefold() in _POWER_LABELS:
            fields.append(f"{label} :{value}")
    return "\n".join(fields)


def _failure(message: str) -> CollectionFailure:
    return CollectionFailure(ErrorCode.COMMAND_REJECTED, message)


def _validate_output(output: str, interface: str) -> None:
    validate_query_interface(interface)
    if (
        len(output) > _MAX_OUTPUT_BYTES
        or len(output.encode("utf-8")) > _MAX_OUTPUT_BYTES
        or len(output.splitlines()) > _MAX_OUTPUT_LINES
    ):
        raise CollectionFailure(
            ErrorCode.OUTPUT_LIMIT_EXCEEDED, "조회 출력이 허용 크기를 초과했습니다."
        )
    if any(_CLI_ERROR.match(line.lstrip(" \t%")) for line in output.splitlines()):
        raise _failure("장비가 조회 명령을 거절했거나 정상 응답을 완료하지 못했습니다.")


def _device_type_info(value: str) -> tuple[tuple[int, ...], str] | None:
    """Recognize a complete type/IEEE token and its optional PD/PSE role."""
    value = value.strip()
    if value.casefold() in ("", "dot3"):
        # dot3 identifies the TLV format, not a negotiated power type.
        return (), ""
    typed = re.fullmatch(
        r"(?:(802\.3(?:af|at|bt))[ \t]+)?Type[ \t]*([1-4])(?:[ \t]+(PD|PSE))?",
        value,
        re.IGNORECASE,
    )
    if typed:
        ieee, supply_type, role = (typed[1] or "").casefold(), int(typed[2]), typed[3] or ""
        compatible = {"802.3af": {1}, "802.3at": {2}, "802.3bt": {3, 4}}
        if ieee and supply_type not in compatible[ieee]:
            return None
        return (supply_type,), role.casefold()
    ieee_only = re.fullmatch(r"(802\.3(?:af|at|bt))(?:[ \t]+(PD|PSE))?", value, re.IGNORECASE)
    if ieee_only:
        return {"802.3af": (1,), "802.3at": (2,), "802.3bt": (3, 4)}[ieee_only[1].casefold()], (
            ieee_only[2] or ""
        ).casefold()
    return None


def _power_fields(output: str, *, remote: bool = False) -> PowerEvidence:
    fields: dict[str, list[str]] = {}
    issues: list[str] = []
    for match in _FIELD_RE.finditer(output):
        label = match[1].casefold()
        # Optional 'Value' is a label variation, never a second source.
        if label in ("pd requested power", "pse allocated power"):
            label += " value"
        fields.setdefault(label, []).append(match[2].strip())

    def single(label: str) -> str:
        values = fields.get(label.casefold(), [])
        if len(values) > 1:
            issues.append(f"중복/충돌 필드: {label}")
            return ""
        return values[0] if values else ""

    def watts(label: str) -> Decimal | None:
        value = single(label)
        if not value:
            if label.casefold() in fields:
                issues.append(f"유효 전력 값 없음: {label}")
            return None
        match = re.fullmatch(
            r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(mW|W|Watts?)", value, re.IGNORECASE
        )
        if match is None:
            issues.append(f"전력 값/단위 해석 불가: {label}")
            return None
        try:
            amount = Decimal(match[1])
            if match[2].casefold() == "mw":
                amount /= 1000
        except DecimalException:
            issues.append(f"전력 값 해석 불가: {label}")
            return None
        if not amount.is_finite() or amount < 0:
            issues.append(f"유효한 비음수 전력 값 아님: {label}")
            return None
        return amount

    def class_type(label: str) -> tuple[int | None, int | None]:
        value = single(label)
        if not value:
            return None, None
        match = re.fullmatch(r"([0-8])\s*/\s*([1-4])", value)
        if match is None:
            issues.append(f"Class/Type 해석 불가: {label}")
            return None, None
        return int(match[1]), int(match[2])

    requested = watts("PD Requested Power Value")
    allocated = watts("PSE Allocated Power Value")
    plc_class, plc_type = class_type("PLC Class/Type")
    dlc_class, dlc_type = class_type("DLC Class/Type")
    power_class_value = single("Power Class")
    power_class = int(power_class_value) if re.fullmatch(r"[0-8]", power_class_value) else None
    if power_class_value and power_class is None:
        issues.append("Power Class 해석 불가")
    allocation_field = "PSE Allocated Power Value" if "pse allocated power value" in fields else ""
    if "pse actual power value" in fields:
        allocation_field = allocation_field or "PSE Actual Power Value"
        issues.append("PSE Actual Power Value는 할당/실측 전력과 대응이 확인되지 않은 필드입니다.")
    device_type = single("Poe Device Type")
    tlv_type = single("PD TLV Sent Type")
    device_info = _device_type_info(device_type)
    if device_type and (device_info is None or device_type.casefold() == "dot3"):
        issues.append("PoE Device Type의 전체 형식을 해석할 수 없습니다.")
    elif remote and device_info is not None and device_info[1] == "pse":
        issues.append("LLDP 원격 장치가 PD가 아닌 PSE 역할을 보고했습니다.")
    if device_type and tlv_type and tlv_type.casefold() != "dot3":
        issues.append("PoE Device Type과 PD TLV Sent Type의 대응 확인 필요")
    return PowerEvidence(
        requested,
        allocated,
        power_class,
        plc_class,
        plc_type,
        dlc_class,
        dlc_type,
        single("Alloc By Actual"),
        device_type or tlv_type,
        allocation_field,
        tuple(issues),
    )


def parse_power_evidence(output: str, interface: str) -> PowerEvidence:
    """Extract effective negotiation fields, never configured/reserved/drawn watts."""
    _validate_output(output, interface)
    headers = _POE_HEADER.findall(output)
    if len(headers) != 1 or headers[0].strip() != interface:
        raise _failure("PoE 전력 근거의 정확한 대상 포트를 확인하지 못했습니다.")
    return _power_fields(_poe_power_lines(output))


_NEIGHBOR_FIELDS = {
    "sysname": "system_name",
    "system name": "system_name",
    "system descr": "system_description",
    "system description": "system_description",
    "chassistype": "chassis_type",
    "chassis type": "chassis_type",
    "chassisid": "chassis_id",
    "chassis id": "chassis_id",
    "porttype": "port_type",
    "port type": "port_type",
    "portid": "port_id",
    "port id": "port_id",
    "portdescr": "port_description",
    "port description": "port_description",
}


_AP_PRODUCT_DESCRIPTION = re.compile(
    r"^(?:ArubaOS\s*\(\s*MODEL\s*:\s*\d{3}[A-Z]?\s*\)|"
    r"(?:(?:HPE\s+)?Aruba(?:\s+Networking|\s+Networks)?\s+)?(?:Regulatory\s+)?"
    r"(?:I?AP[- ]\d{3}[A-Z]?|APIN0\d{3})"
    r"(?=$|\s*[,/;(]|\s+(?:(?:wireless|campus)\s+)?access point\b|\s+version\b))",
    re.IGNORECASE,
)


def _model_from_description(description: str) -> str:
    # A product or model number mentioned by a different device is not its identity.
    if not _AP_PRODUCT_DESCRIPTION.match(description):
        return ""
    models: set[str] = set()
    for pattern in (
        r"\bMODEL\s*:\s*(\d{3}[A-Z]?)\b",
        r"\bI?AP[- ](\d{3}[A-Z]?)\b",
        r"\bAPIN0(\d{3})\b",
    ):
        models.update(
            "AP-" + match.upper() for match in re.findall(pattern, description, re.IGNORECASE)
        )
    return (
        next(iter(models)) if len(models) == 1 else (" / ".join(sorted(models)) if models else "")
    )


_LLDP_SECTION_HEADINGS = frozenset(
    {
        "remote management address",
        "poe plus information detail",
        "med information detail",
    }
)


def _neighbor(block: list[str]) -> LLDPNeighbor:
    fields: dict[str, str] = {}
    addresses: list[str] = []
    power_lines: list[str] = []
    enabled_capabilities: tuple[str, ...] | None = None
    in_management = False
    management_type: str | None = None
    for line in block[1:]:
        if not line.strip() or re.fullmatch(r"[\s=-]+", line):
            continue
        heading = line.strip().casefold()
        if heading in _LLDP_SECTION_HEADINGS:
            if management_type is not None:
                raise _failure("LLDP 관리 주소의 Type/Address 쌍이 잘렸습니다.")
            in_management = heading == "remote management address"
            continue
        match = re.fullmatch(r"\s*([^:]+?)\s*:\s*(.*?)\s*", line)
        if match is None:
            raise _failure("LLDP 이웃 블록의 잘린 필드 또는 알 수 없는 행을 확인했습니다.")
        label, value = match[1].strip().casefold(), match[2]
        if in_management and label in ("type", "address"):
            if label == "type":
                if management_type is not None:
                    raise _failure("LLDP 관리 주소 Type이 중복되었거나 주소가 누락되었습니다.")
                management_type = value
            else:
                if management_type is None or (value and not management_type):
                    raise _failure("LLDP 관리 주소의 Type/Address 쌍을 확인하지 못했습니다.")
                if value:
                    addresses.append(value)
                management_type = None
            continue
        if in_management:
            if management_type is not None:
                raise _failure("LLDP 관리 주소의 Type/Address 쌍이 잘렸습니다.")
            in_management = False
        if label in _NEIGHBOR_FIELDS:
            key = _NEIGHBOR_FIELDS[label]
            if key in fields:
                raise _failure("LLDP 이웃 식별 필드가 중복되어 연결할 수 없습니다.")
            fields[key] = value
        elif label == "system capabilities enabled":
            if enabled_capabilities is not None:
                raise _failure("LLDP 이웃의 활성 역할 필드가 중복되었습니다.")
            enabled_capabilities = tuple(role.strip() for role in value.split(",") if role.strip())
        elif label in (
            "management address",
            "management addresses",
            "mgmt address",
            "management ip address",
        ):
            if value:
                addresses.append(value)
        elif label in _POWER_LABELS:
            power_lines.append(line)
        # Other key/value LLDP TLVs are optional, with no identity authority.
    if management_type is not None:
        raise _failure("LLDP 관리 주소의 Type/Address 쌍이 잘렸습니다.")
    if not fields.get("chassis_id") or not fields.get("port_id"):
        raise _failure("LLDP 이웃의 Chassis ID와 Port ID를 확인하지 못했습니다.")
    power = _power_fields("\n".join(power_lines), remote=True)
    has_power = power != PowerEvidence()
    return LLDPNeighbor(
        **fields,
        model=_model_from_description(fields.get("system_description", "")),
        management_addresses=tuple(addresses),
        power=power if has_power else None,
        system_capabilities_enabled=enabled_capabilities or (),
    )


def parse_lldp_neighbors(output: str, interface: str) -> tuple[LLDPNeighbor, ...]:
    """Bind every neighbor to the exact local port; reject failures as failures."""
    _validate_output(output, interface)
    lines = output.splitlines()
    headers = [index for index, line in enumerate(lines) if _LLDP_HEADER.fullmatch(line)]
    if len(headers) != 1:
        raise _failure("정상 LLDP 상세 응답 헤더를 확인하지 못했습니다.")
    # Allow a standard command echo, but no arbitrary text before the header.
    for line in lines[: headers[0]]:
        if line.strip() and not re.fullmatch(
            r"(?:\S+[>#]\s*)?show lldp info remote-device\s+" + re.escape(interface),
            line.strip(),
            re.IGNORECASE,
        ):
            raise _failure("LLDP 응답 앞의 예상하지 못한 텍스트를 확인했습니다.")
    blocks: list[list[str]] = []
    empty = False
    for line in lines[headers[0] + 1 :]:
        local = _LOCAL_PORT.fullmatch(line)
        if local:
            if local[1] != interface:
                raise _failure("LLDP Local Port가 요청한 포트와 정확히 일치하지 않습니다.")
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
        elif line.strip() and not re.fullmatch(r"[\s=-]+", line):
            if re.fullmatch(
                r"No (?:LLDP )?(?:neighbors?(?: found)?|remote devices?(?: found)?)[.!]?",
                line.strip(),
                re.IGNORECASE,
            ):
                empty = True
            else:
                raise _failure("LLDP 이웃 없음으로 해석할 수 없는 응답입니다.")
    if empty and blocks:
        raise _failure("LLDP 이웃 없음 표시와 이웃 블록이 충돌합니다.")
    return tuple(_neighbor(block) for block in blocks)


def _valid_watts(value: Decimal | None) -> bool:
    return value is not None and value.is_finite() and value > 0


def _mode(evidence: PowerEvidence, *, remote: bool = False) -> str:
    """Return only explicit IEEE/type evidence; class alone proves no mode."""
    types = {value for value in (evidence.plc_type, evidence.dlc_type) if value is not None}
    device_info = _device_type_info(evidence.device_type)
    if device_info is None or (remote and device_info[1] == "pse"):
        return "conflict"
    device_types = device_info[0]
    if device_types:
        if types and not types.issubset(device_types):
            return "conflict"
        if not types:
            types.add(min(device_types))
    if len(types) > 1:
        return "conflict"
    if not types:
        return ""
    supply_type = next(iter(types))
    classes = {
        value
        for value in (evidence.power_class, evidence.plc_class, evidence.dlc_class)
        if value is not None
    }
    # PLC and DLC may describe different discovery stages. Do not infer a final
    # supported mode from inconsistent class/type observations.
    if len(classes) > 1:
        return "conflict"
    if supply_type == 1:
        return "802.3af Type 1" if not classes or max(classes) <= 3 else "conflict"
    if supply_type == 2:
        return "802.3at Type 2" if not classes or classes == {4} else "conflict"
    return "802.3bt Type " + str(supply_type) if classes and min(classes) >= 5 else ""


def assess_ap535_power(
    poe: PoEState | None,
    evidence: PowerEvidence | None,
    lldp: LLDPObservation | None,
    *,
    expected_model: str | None = "AP-535",
) -> PowerAssessment:
    """Assess one PSE port, with no consumption threshold or AP health claim."""
    neighbors = lldp.neighbors if lldp is not None and lldp.status is LLDPStatus.OK else ()
    neighbor = neighbors[0] if len(neighbors) == 1 else None
    model = neighbor.model if neighbor is not None and neighbor.model else (expected_model or "")
    model_source = "lldp" if neighbor is not None and neighbor.model else "configured"
    source_modes = [(evidence, _mode(evidence))] if evidence is not None else []
    if neighbor is not None and neighbor.power is not None:
        source_modes.append((neighbor.power, _mode(neighbor.power, remote=True)))
    sources = [value for value, _ in source_modes]
    paired = [
        (value, mode)
        for value, mode in source_modes
        if _valid_watts(value.requested_watts) and _valid_watts(value.allocated_watts)
    ]
    eligible = [(value, mode) for value, mode in paired if mode not in ("", "conflict")]
    chosen, negotiation = (
        eligible[0]
        if eligible
        else (paired[0] if paired else (source_modes[0] if source_modes else (PowerEvidence(), "")))
    )

    def result(status: AssessmentStatus, label: str, reason: str) -> PowerAssessment:
        provenance = "LLDP 설명에서 확인한" if model_source == "lldp" else "사용자가 지정한"
        return PowerAssessment(
            status,
            label,
            f"{provenance} {model or '모델 미지정'} 기준. {reason}",
            model,
            model_source,
            chosen.requested_watts,
            chosen.allocated_watts,
            negotiation,
        )

    if poe is not None:
        state = poe.status.strip().casefold()
        if poe.enabled is False:
            return result(
                AssessmentStatus.OFF, "공급 꺼짐", "포트의 PoE 사용 설정이 꺼져 있습니다."
            )
        if state in ("fault", "other fault"):
            return result(AssessmentStatus.ERROR, "공급 오류", "포트의 현재 PoE 공급 오류입니다.")
        if state == "searching":
            return result(
                AssessmentStatus.SEARCHING, "공급 대기", "포트가 전원 장치를 검색 중입니다."
            )
        if "미지원" in state or state in ("unsupported", "not supported"):
            return result(AssessmentStatus.UNSUPPORTED, "미지원", "PoE를 지원하지 않는 포트입니다.")
    if len(neighbors) > 1:
        return result(
            AssessmentStatus.UNKNOWN,
            "확인 필요",
            "LLDP 이웃이 여러 개여서 하나의 AP 전원 협상으로 연결할 수 없습니다.",
        )
    if model != "AP-535":
        return result(
            AssessmentStatus.UNKNOWN,
            "확인 필요",
            "AP-535 모델 기준을 적용할 근거가 없거나 다른 모델입니다.",
        )
    if any(
        value.issues
        or mode == "conflict"
        or value.allocation_method.strip().casefold() not in ("", "lldp")
        or any(
            watts is not None and not _valid_watts(watts)
            for watts in (value.requested_watts, value.allocated_watts)
        )
        or value.allocation_field not in ("", "PSE Allocated Power Value")
        for value, mode in source_modes
    ):
        return result(
            AssessmentStatus.UNKNOWN,
            "확인 필요",
            "전력 근거에 해석 불가 또는 서로 충돌하는 필드가 있습니다.",
        )
    for field in (
        "requested_watts",
        "allocated_watts",
        "power_class",
        "plc_class",
        "dlc_class",
        "plc_type",
        "dlc_type",
    ):
        values = {getattr(value, field) for value in sources if getattr(value, field) is not None}
        if len(values) > 1:
            return result(
                AssessmentStatus.UNKNOWN, "확인 필요", "PoE와 LLDP 전원 협상 근거가 서로 다릅니다."
            )
    modes = {mode for _, mode in source_modes if mode}
    if len(modes) > 1:
        return result(
            AssessmentStatus.UNKNOWN, "확인 필요", "전원 공급 방식 근거가 서로 충돌합니다."
        )
    if poe is None or poe.enabled is not True or poe.status.casefold() != "delivering":
        return result(
            AssessmentStatus.UNKNOWN, "확인 필요", "포트의 현재 전력 공급을 확인하지 못했습니다."
        )
    if negotiation == "802.3af Type 1":
        return result(
            AssessmentStatus.WARNING,
            "주의",
            "명시적인 802.3af 공급은 AP-535의 지원 전원 조건이 아닙니다.",
        )
    if not paired or not negotiation:
        return result(
            AssessmentStatus.UNKNOWN,
            "확인 필요",
            "동일 출처의 유효 요청/할당 쌍과 지원 전원 협상 근거가 필요합니다. 낮은 실측 W만으로 부족을 판정하지 않습니다.",
        )
    # Both are valid finite Decimals from the same source by construction.
    assert chosen.requested_watts is not None and chosen.allocated_watts is not None
    if chosen.allocated_watts < chosen.requested_watts:
        return result(
            AssessmentStatus.WARNING,
            "주의",
            "포트 할당 전력이 같은 협상 출처의 PD 요청 전력보다 낮습니다.",
        )
    return result(
        AssessmentStatus.NORMAL,
        "공급·협상 정상",
        "포트가 전력을 공급하며 유효한 PD 요청을 할당 전력이 충족합니다. AP 내부 IPM·라디오·서비스 상태는 확인 범위 밖입니다.",
    )
