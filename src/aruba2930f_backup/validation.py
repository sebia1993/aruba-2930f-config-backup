"""Input, device identity, prompt, and command-output validation."""

from __future__ import annotations

import re
from collections.abc import Callable
from ipaddress import IPv4Address

from .models import (
    CollectionFailure,
    DeviceIdentity,
    DeviceTarget,
    DiagnosticDetail,
    ErrorCode,
)


class InputValidationError(ValueError):
    """Raised after all IP input lines have been checked."""

    def __init__(self, issues: list[str]) -> None:
        self.issues = tuple(issues)
        super().__init__("\n".join(issues))


# Official AOS-S 16.10 applicable-product list for the Aruba 2930F series:
# https://arubanetworking.hpe.com/techdocs/AOS-S/16.10/FCI/content/
# fci/applicable%20products%20all%2016.10.htm
# Accessories (for example JL311A/JL312A) are intentionally not accepted.
ARUBA_2930F_MODELS: dict[str, str] = {
    "JL253A": "Aruba 2930F 24G 4SFP+",
    "JL254A": "Aruba 2930F 48G 4SFP+",
    "JL255A": "Aruba 2930F 24G PoE+ 4SFP+",
    "JL256A": "Aruba 2930F 48G PoE+ 4SFP+",
    "JL258A": "Aruba 2930F 8G PoE+ 2SFP+",
    "JL259A": "Aruba 2930F 24G 4SFP",
    "JL260A": "Aruba 2930F 48G 4SFP",
    "JL261A": "Aruba 2930F 24G PoE+ 4SFP",
    "JL262A": "Aruba 2930F 48G PoE+ 4SFP",
    "JL263A": "Aruba 2930F 24G PoE+ 4SFP+ TAA",
    "JL264A": "Aruba 2930F 48G PoE+ 4SFP+ TAA",
    "JL557A": "Aruba 2930F 48G PoE+ 4SFP 740W",
    "JL558A": "Aruba 2930F 48G PoE+ 4SFP+ 740W",
    "JL559A": "Aruba 2930F 48G PoE+ 4SFP+ 740W TAA",
    "JL692A": "Aruba 2930F 8G PoE+ 2SFP+ TAA",
    "JL693A": "Aruba 2930F 12G PoE+ 2G/2SFP+",
}

SUPPORTED_MODELS = {**ARUBA_2930F_MODELS, "JL320A": "Aruba 2930M 24G PoE+ 1-slot"}

_SKU_RE = re.compile(r"\b(?:J[A-Z]\d{3}[A-Z]|J\d{4}[A-Z]|R\d[A-Z]\d{2}[A-Z])\b", re.IGNORECASE)
_SOFTWARE_RE = re.compile(r"\b([A-Z]{2}\.\d{2}\.\d{2}\.\d{4})\b")
_BOOT_ROM_VERSION_RE = re.compile(
    r"\b(?:BOOT[ \t-]*ROM(?:[ \t]+(?:VERSION|REVISION))?|ROM[ \t]+VERSION)\s*[:=]?\s*"
    + _SOFTWARE_RE.pattern,
    re.IGNORECASE,
)
_CHASSIS_ROW_RE = re.compile(
    r"^\s*(?:(?:VSF\s+)?member\s+\d+\s*:?\s*|\*?\d+\s+[0-9a-f]{6}-[0-9a-f]{6}\s+)?"
    r"(?:chassis\b|model\b|product (?:name|number)\b|(?:Aruba|HPE|HP)\b|2930[FM]\b|JL\d{3}A\b)",
    re.IGNORECASE,
)
_STACK_ID_RE = re.compile(r"^[ \t]*Stack ID[ \t]*:[ \t]*([^\r\n]*)", re.IGNORECASE | re.MULTILINE)
_STACK_HEADER_RE = re.compile(
    r"^[ \t]*(?:Mbr[ \t]+)?ID[ \t]+Mac[ \t]+Address[ \t]+Model[ \t]+Pri(?:ority)?[ \t]+Status[ \t]*\r?$",
    re.IGNORECASE | re.MULTILINE,
)
_STACK_MEMBER_RE = re.compile(
    r"\*?\s*(10|[1-9])\s+([0-9a-f]{6}-[0-9a-f]{6})\s+(.+?)\s+\d{1,3}\s+(Commander|Standby|Member)",
    re.IGNORECASE,
)
_WRONG_JL320A_VARIANT_RE = re.compile(
    r"\b2930M[- ]+(?:(?!24G\b)\d+[A-Z]|24G[- ]+(?:Switch|Non-PoE)\b)", re.IGNORECASE
)
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_FAMILY_2930F_RE = re.compile(r"\b2930F\b", re.IGNORECASE)
_FAMILY_2930M_RE = re.compile(r"\b2930M\b", re.IGNORECASE)
_RUNNING_CONFIG_HOSTNAME_RE = re.compile(
    r'^hostname[ \t]+(?:"([^"\r\n]+)"|([^" \t\r\n]+))[ \t]*$',
    re.IGNORECASE,
)
_APPENDED_CLI_MODE_RE = re.compile(r"^.+\([^()\r\n]+\)[ \t]*[#>]$")
_MAX_PROMPT_LENGTH = 256
_MAX_HOSTNAME_LENGTH = 255
_CONFLICTING_FAMILY_RE = re.compile(
    r"\b(?:2530|2540|2920|3810M|5400R|6200F|6300[FM]|6400|CX)\b",
    re.IGNORECASE,
)
_CLI_ERROR_RE = re.compile(
    r"(?:^|\n)\s*(?:%\s*)?(?:invalid input|unknown command|incomplete input|"
    r"ambiguous input|command authorization failed|permission denied|"
    r"authorization failed|unrecognized command)\b",
    re.IGNORECASE,
)

PAGER_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^[ \t]*--\s*MORE\s*--,\s*next page:\s*Space,\s*"
        r"next line:\s*Enter,\s*quit:\s*Control-C[ \t]*$",
        re.IGNORECASE | re.MULTILINE,
    ),
)


def parse_ipv4_targets(text: str, *, port: int = 22) -> list[DeviceTarget]:
    """Parse one IPv4 address per non-blank line and reject the whole batch.

    All invalid and duplicate lines are reported together. No partial target list
    is returned when any issue exists.
    """

    issues: list[str] = []
    parsed: list[tuple[int, str]] = []
    seen: dict[str, int] = {}

    if not 1 <= port <= 65535:
        raise InputValidationError(["SSH port must be between 1 and 65535."])

    for line_number, raw in enumerate(text.splitlines(), start=1):
        value = raw.strip()
        if not value:
            continue
        try:
            address = IPv4Address(value)
        except ValueError:
            issues.append(f"Line {line_number}: invalid IPv4 address '{value}'.")
            continue
        normalized = str(address)
        if normalized in seen:
            issues.append(
                f"Line {line_number}: duplicate IPv4 address '{normalized}' "
                f"(first entered on line {seen[normalized]})."
            )
            continue
        seen[normalized] = line_number
        parsed.append((line_number, normalized))

    if not parsed and not issues:
        issues.append("Enter at least one IPv4 address.")
    if issues:
        raise InputValidationError(issues)
    return [DeviceTarget(ip=ip, port=port) for _, ip in parsed]


def validate_cli_response(command: str, output: str) -> None:
    if not isinstance(output, str):
        raise CollectionFailure(
            ErrorCode.COMMAND_REJECTED,
            f"{command} returned an invalid response.",
        )
    clean = _ANSI_RE.sub("", output)
    if command.strip().casefold() == "show running-config":
        # MOTD is arbitrary operator-written text, not CLI error output.
        # Only hide fully delimited banners; malformed output stays observable.
        outside = _config_lines_outside_banners(clean)
        if outside is not None:
            clean = "\n".join(outside)
    if _CLI_ERROR_RE.search(clean):
        raise CollectionFailure(
            ErrorCode.COMMAND_REJECTED,
            f"The device rejected '{command}'.",
        )


def contains_pager_marker(output: str) -> bool:
    clean = _ANSI_RE.sub("", output)
    return any(pattern.search(clean) for pattern in PAGER_MARKERS)


def validate_output_limits(output: str, *, max_bytes: int, max_lines: int) -> None:
    if len(output.encode("utf-8")) > max_bytes or _line_count(output) > max_lines:
        raise CollectionFailure(
            ErrorCode.OUTPUT_LIMIT_EXCEEDED,
            "Command output exceeded the configured safety limit.",
        )


def requires_stacking_inventory(modules_output: str) -> bool:
    """A backplane stack reports a Stack ID and accessory slots in show modules."""
    return _STACK_ID_RE.search(_ANSI_RE.sub("", modules_output)) is not None


def identify_connected_device(
    version_output: str,
    modules_output: str,
    read_stacking: Callable[[], str],
    *,
    hostname: str | None = None,
) -> DeviceIdentity:
    """Request the separate chassis inventory only on a backplane stack."""
    inventory = read_stacking() if requires_stacking_inventory(modules_output) else ""
    return validate_device_identity(
        version_output, modules_output, hostname=hostname, stacking_output=inventory
    )


def validate_device_identity(
    version_output: str,
    modules_output: str = "",
    *,
    hostname: str | None = None,
    stacking_output: str = "",
) -> DeviceIdentity:
    """Identify a 2930F or the supported JL320A chassis from switch CLI output."""

    version = _ANSI_RE.sub("", version_output or "")
    modules = _ANSI_RE.sub("", modules_output or "")
    if not version.strip():
        raise _unsupported(
            "The show version response was empty.",
            DiagnosticDetail.IDENTITY_EVIDENCE_MISSING,
        )
    module_rows = _chassis_rows(modules)
    stack_models = (
        _stack_member_models(modules, _ANSI_RE.sub("", stacking_output))
        if requires_stacking_inventory(modules)
        else []
    )
    evidence = "\n".join([*_chassis_rows(version), *module_rows, *stack_models])
    explicit_skus = _all_skus(evidence)
    candidates = explicit_skus & SUPPORTED_MODELS.keys()
    family_f = bool(_FAMILY_2930F_RE.search(evidence) or candidates & ARUBA_2930F_MODELS.keys())
    family_m = bool(_FAMILY_2930M_RE.search(evidence) or "JL320A" in candidates)
    if (
        _CONFLICTING_FAMILY_RE.search(version)
        or _CONFLICTING_FAMILY_RE.search(evidence)
        or (family_f and family_m)
    ):
        raise _unsupported(
            "A conflicting switch family was detected.",
            DiagnosticDetail.IDENTITY_FAMILY_CONFLICT,
        )

    unsupported_skus = explicit_skus - SUPPORTED_MODELS.keys()
    if unsupported_skus:
        detail = (
            DiagnosticDetail.IDENTITY_SKU_CONFLICT
            if candidates or len(unsupported_skus) > 1
            else DiagnosticDetail.IDENTITY_SKU_UNSUPPORTED
        )
        raise _unsupported("Unsupported or conflicting chassis SKU evidence was detected.", detail)

    if "JL320A" in candidates:
        # Do not advertise an unknown or different second chassis as another JL320A.
        chassis = [row for row in module_rows if re.search(r"\bchassis\b", row, re.IGNORECASE)]
        unverified = any(
            "JL320A" not in _all_skus(row)
            and (len(chassis) > 1 or not _FAMILY_2930M_RE.search(row))
            for row in chassis
        )
        if unverified or _WRONG_JL320A_VARIANT_RE.search(evidence):
            raise _unsupported(
                "Conflicting JL320A chassis information was detected.",
                DiagnosticDetail.IDENTITY_SKU_CONFLICT,
            )

    software_match = _SOFTWARE_RE.search(_BOOT_ROM_VERSION_RE.sub("", version.upper()))
    if len(candidates) == 1:
        sku = next(iter(candidates))
        model = SUPPORTED_MODELS[sku]
        display_sku: str | None = sku
    elif len(candidates) > 1:
        model = "Aruba 2930F VSF"
        display_sku = ", ".join(sorted(candidates))
    elif family_f:
        model = "Aruba 2930F"
        display_sku = None
    else:
        raise _unsupported(
            "The command responses did not identify a supported Aruba 2930F or JL320A chassis.",
            DiagnosticDetail.IDENTITY_EVIDENCE_MISSING,
        )

    return DeviceIdentity(
        hostname=hostname,
        model=model,
        sku=display_sku,
        software_version=software_match.group(1) if software_match else None,
    )


def hostname_from_prompt(prompt: str) -> str | None:
    clean = _ANSI_RE.sub("", prompt or "").strip()
    if not clean or "\n" in clean:
        return None
    match = re.fullmatch(r"([A-Za-z0-9_.-]+)[#>]", clean)
    if match is None:
        return None
    value = match.group(1)
    if len(value) > 255:
        return None
    return value


def hostname_from_running_config(config_text: str) -> str | None:
    """Extract one unambiguous top-level ArubaOS-Switch hostname directive.

    ArubaOS-Switch renders configured hostnames as either ``hostname value`` or
    ``hostname "value"``. Indented text is intentionally ignored so banner or
    nested configuration content cannot be promoted to device metadata.
    """

    candidates: list[str] = []
    # An unindented "hostname" inside a login banner is not a config directive.
    # On invalid/unclosed banners the safe result is no hostname metadata.
    outside = _config_lines_outside_banners(_ANSI_RE.sub("", config_text or ""))
    if outside is None:
        return None
    for line in outside:
        match = _RUNNING_CONFIG_HOSTNAME_RE.fullmatch(line)
        if match is None:
            continue
        value = next(group for group in match.groups() if group is not None)
        if (
            not value.strip()
            or len(value) > _MAX_HOSTNAME_LENGTH
            or any(not character.isprintable() for character in value)
        ):
            continue
        candidates.append(value)

    if not candidates or any(value != candidates[0] for value in candidates[1:]):
        return None
    return candidates[0]


def require_valid_prompt(prompt: str) -> str | None:
    """Validate an exact EXEC prompt while extracting only simple hostnames.

    Aruba prompts can contain display wrappers, spaces, and model characters
    that are not valid DNS hostname characters. Those prompts are safe to use
    as opaque command terminators, but they are not promoted to hostname
    metadata. Appended CLI modes such as ``switch(config)#`` remain blocked.
    """

    clean = _ANSI_RE.sub("", prompt or "").strip()
    if not clean:
        detail = DiagnosticDetail.PROMPT_EMPTY
    elif _APPENDED_CLI_MODE_RE.fullmatch(clean):
        detail = DiagnosticDetail.PROMPT_NON_EXEC_MODE
    elif (
        len(clean) > _MAX_PROMPT_LENGTH
        or "\n" in clean
        or "\r" in clean
        or clean[-1] not in "#>"
        or not clean[:-1].strip()
        or any(not character.isprintable() for character in clean)
    ):
        detail = DiagnosticDetail.PROMPT_FORMAT
    else:
        return hostname_from_prompt(clean)

    raise CollectionFailure(
        ErrorCode.PROMPT_PARSE_FAILED,
        "The final device prompt could not be verified.",
        transient=True,
        diagnostic_detail=detail,
    )


def normalize_config_text(output: str) -> str:
    """Normalize a verified config to the exact UTF-8/CRLF payload to be saved."""

    text = output.replace("\r\n", "\n").replace("\r", "\n")
    if not any(line.strip() for line in text.splitlines()):
        raise CollectionFailure(
            ErrorCode.PROMPT_PARSE_FAILED,
            "The running configuration output was empty.",
        )
    if not _config_banners_closed(text):
        raise CollectionFailure(
            ErrorCode.PROMPT_PARSE_FAILED,
            "The running configuration contains an incomplete banner.",
            transient=True,
        )
    if contains_pager_marker(text):
        raise CollectionFailure(
            ErrorCode.PROMPT_PARSE_FAILED,
            "A paging marker remained in the configuration output.",
            transient=True,
        )
    return text.rstrip("\n").replace("\n", "\r\n") + "\r\n"


def _config_lines_outside_banners(text: str) -> tuple[str, ...] | None:
    """Return true configuration lines, or None for an uncertain MOTD boundary.

    The ArubaOS-Switch MOTD delimiter is one character and may close inline or
    on a later line; an escaped delimiter is literal. Never promote banner
    content to a hostname or interpret its text as a command rejection.
    """
    outside: list[str] = []
    delimiter: str | None = None
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized.split("\n"):
        if delimiter is None:
            match = re.fullmatch(r"[ \t]*banner[ \t]+motd(?:[ \t]+(.*))?", line, re.IGNORECASE)
            if match is None:
                outside.append(line)
                continue
            value = (match.group(1) or "").lstrip(" \t")
            if not value:
                return None
            delimiter, value = value[0], value[1:]
        else:
            value = line
        escaped = False
        for index, character in enumerate(value):
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == delimiter:
                if value[index + 1 :].strip():
                    return None
                delimiter = None
                break
        # An escape cannot continue across a literal output line boundary.
    return tuple(outside) if delimiter is None else None


def _config_banners_closed(text: str) -> bool:
    """Reject incomplete or ambiguous AOS-S MOTD banner delimiters."""
    return _config_lines_outside_banners(text) is not None


def _chassis_rows(text: str) -> list[str]:
    """Keep chassis descriptions, omitting serial numbers and module/slot rows."""
    return [
        re.split(r"\bserial\s+number\s*:", line, maxsplit=1, flags=re.IGNORECASE)[0]
        for line in text.splitlines()
        if _CHASSIS_ROW_RE.match(line)
        and not re.search(r"\b(?:module|transceiver)\b", line, re.IGNORECASE)
    ]


def _all_skus(text: str) -> set[str]:
    return {match.upper() for match in _SKU_RE.findall(text)}


def _stack_member_models(modules: str, inventory: str) -> list[str]:
    """Read all members of the same backplane stack, including unrecognized rows."""
    ids = _STACK_ID_RE.findall(modules)
    reported_ids = _STACK_ID_RE.findall(inventory)
    table = _STACK_HEADER_RE.search(inventory)
    stack_id = ids[0].strip().casefold() if len(ids) == 1 else ""
    unformed = stack_id == "no id - will merge upon connectivity"
    if (
        len(reported_ids) != 1
        or stack_id != reported_ids[0].strip().casefold()
        or (not unformed and not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{8}", stack_id))
        or table is None
    ):
        raise _unsupported(
            "Stack member inventory was not verified.", DiagnosticDetail.IDENTITY_EVIDENCE_MISSING
        )

    if unformed:
        status = re.findall(
            r"^[ \t]*stack status[ \t]*:[ \t]*([^\n\r]*)", inventory.casefold(), re.MULTILINE
        )
        if [value.strip() for value in status] != ["no stack formed"]:
            raise _unsupported(
                "The stack formation state was not verified.",
                DiagnosticDetail.IDENTITY_EVIDENCE_MISSING,
            )

    members: dict[str, str] = {}
    for line in inventory[table.end() :].strip().splitlines():
        line = line.strip()
        if not line or re.fullmatch(r"[-\s]+", line):
            continue
        row = _STACK_MEMBER_RE.fullmatch(line)
        if row is None or row[1] in members:
            raise _unsupported(
                "Incomplete or ambiguous stack member inventory.",
                DiagnosticDetail.IDENTITY_EVIDENCE_MISSING,
            )
        if unformed and (members or row[4].casefold() != "commander"):
            raise _unsupported(
                "An unformed stack requires a single Commander.",
                DiagnosticDetail.IDENTITY_EVIDENCE_MISSING,
            )
        model = row[3]
        if _all_skus(model) != {"JL320A"}:
            raise _unsupported(
                "Unsupported or unidentified stack member.", DiagnosticDetail.IDENTITY_SKU_CONFLICT
            )
        members[row[1]] = model

    # Slot rows identify members with installed accessories, even though they do
    # not identify the chassis. Every such member must appear in the inventory.
    slots = set(
        re.findall(r"^[ \t]*(\d+)[ \t]+(?:STK|[A-Z])[ \t]+", modules, re.IGNORECASE | re.MULTILINE)
    )
    if not members or not slots.issubset(members):
        raise _unsupported(
            "The stack member list was incomplete.", DiagnosticDetail.IDENTITY_EVIDENCE_MISSING
        )
    return list(members.values())


def _unsupported(message: str, detail: DiagnosticDetail) -> CollectionFailure:
    return CollectionFailure(
        ErrorCode.MODEL_UNSUPPORTED,
        message,
        diagnostic_detail=detail,
    )


def _line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)
