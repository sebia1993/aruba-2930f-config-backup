"""Conservative Source IPv4 evidence from completed AOS-S read-only tables.

Supported grammar: documented 2930M/F 'show vlans' and 'show ip' pipe
tables (AOS-S 16.10). Other formats require a separately verified parser.
The caller must verify the same device/session, command echo, final prompt
and output completeness for both responses. This module never issues CLI.
"""

from __future__ import annotations

import re
from ipaddress import IPv4Network

from .ping_tools import MAX_OUTPUT, unicast_ipv4

_VLAN_HEADER = re.compile(r"VLAN ID\s+Name\s*\|\s*Status\s+Voice\s+Jumbo", re.I)
_IP_HEADER = re.compile(
    r"VLAN\s*\|\s*IP Config\s+IP Address\s+Subnet Mask"
    r"(?:\s+(?:Proxy ARP|Std\s+Local))?",
    re.I,
)
_DIVIDER = re.compile(r"[-+ ]+")
_VLAN_ROW = re.compile(
    r"([0-9]{1,4})\s+([^|]+?)\s*\|\s*"
    r"(?:Port-based|Protocol|Dynamic)\s+(?:Yes|No)\s+(?:Yes|No)",
    re.I,
)
_IP_ROW = re.compile(
    r"([^|]+?)\s*\|\s*(Manual|DHCP/Bootp|Disabled)(?:\s+(.*))?",
    re.I,
)
_ADDRESS_ROW = re.compile(r"([0-9.]{7,15})\s+([0-9.]{7,15})(?:\s+(?:Yes|No)){0,2}", re.I)
_ERROR = "Source 주소의 VLAN 및 활성 IPv4 정보를 확인할 수 없습니다."


def _rows(output: str, header: re.Pattern[str]) -> list[str]:
    if not isinstance(output, str) or not output or len(output) > MAX_OUTPUT:
        raise ValueError(_ERROR)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    headers = [i for i, line in enumerate(lines) if header.fullmatch(line)]
    if len(headers) != 1:
        raise ValueError(_ERROR)
    offset = headers[0] + 1
    if offset >= len(lines):
        raise ValueError(_ERROR)
    divider = lines[offset]
    if not _DIVIDER.fullmatch(divider) or "+" not in divider or "-" not in divider:
        raise ValueError(_ERROR)
    rows = lines[offset + 1 :]
    if not rows:
        raise ValueError(_ERROR)
    return rows


def parse_owned_ipv4(
    vlan_output: str, ip_output: str, *, completed: bool
) -> tuple[tuple[int, str], ...]:
    """Join exact VLAN names to VIDs and active interface addresses.

    Fail closed for truncated, ambiguous, unsupported or inconsistent output.
    Disabled Layer 3 interfaces never count, even when an IP is displayed.
    A default gateway, DNS address or SSH peer address is not interface proof.
    """
    if completed is not True:
        raise ValueError(_ERROR)
    vlans: dict[str, int] = {}
    ids: set[int] = set()
    for row in _rows(vlan_output, _VLAN_HEADER):
        match = _VLAN_ROW.fullmatch(row)
        if match is None:
            raise ValueError(_ERROR)
        vid, name = int(match[1]), match[2].strip()
        if not 1 <= vid <= 4094 or not name or name in vlans or vid in ids:
            raise ValueError(_ERROR)
        vlans[name] = vid
        ids.add(vid)

    owned: list[tuple[int, str]] = []
    seen_ips: set[str] = set()
    seen_names: set[str] = set()
    for row in _rows(ip_output, _IP_HEADER):
        match = _IP_ROW.fullmatch(row)
        if match is None:
            raise ValueError(_ERROR)
        name, mode, detail = match[1].strip(), match[2].casefold(), match[3]
        if name not in vlans or name in seen_names:
            # Repeated/secondary/unnamed address rows need version-specific
            # evidence before they can be used for Source verification.
            raise ValueError(_ERROR)
        seen_names.add(name)
        if mode == "disabled":
            if detail and not _ADDRESS_ROW.fullmatch(detail):
                raise ValueError(_ERROR)
            continue
        if not detail:
            if mode == "dhcp/bootp":
                continue
            raise ValueError(_ERROR)
        address = _ADDRESS_ROW.fullmatch(detail)
        if address is None:
            raise ValueError(_ERROR)
        try:
            ip = unicast_ipv4(address[1])
            network = IPv4Network((ip, address[2]), strict=False)
            if str(network.netmask) != address[2]:
                raise ValueError(_ERROR)
            if network.prefixlen <= 30 and ip in (
                str(network.network_address),
                str(network.broadcast_address),
            ):
                raise ValueError(_ERROR)
        except ValueError as exc:
            raise ValueError(_ERROR) from exc
        if ip in seen_ips:
            raise ValueError(_ERROR)
        seen_ips.add(ip)
        owned.append((vlans[name], ip))
    # Do not assume omitted VLAN rows are disabled or invent their addresses.
    if seen_names != set(vlans):
        raise ValueError(_ERROR)
    return tuple(owned)
