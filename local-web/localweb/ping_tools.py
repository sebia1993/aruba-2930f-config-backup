"""Validated, read-only AOS-Switch IPv4 Ping request and observation models.

This module does not open SSH sessions or dispatch CLI commands. A future
transport must verify the model, host key, source ownership and final prompt
before interpreting a response. Never use partial/deadline output as evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from ipaddress import IPv4Address
from math import isfinite

from aruba2930f_backup.models import DeviceTarget

MAX_SWITCHES = 300
MAX_DESTINATIONS = 10
MAX_PAIRS = 3000
MAX_INPUT = 100_000
MAX_OUTPUT = 1_000_000

_SOURCE_MODES = frozenset({"ssh_ip", "auto", "ip", "vlan"})
_PRESETS = {
    "quick": (3, 1),
    "normal": (5, 2),
    "detail": (20, 3),
}


def unicast_ipv4(value: object) -> str:
    """Accept only literal IPv4 unicast addresses, never a CLI fragment."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9.]{7,15}", value):
        raise ValueError("IPv4 유니캐스트 주소를 입력하세요.")
    try:
        ip = IPv4Address(value)
    except ValueError as exc:
        raise ValueError("IPv4 유니캐스트 주소를 입력하세요.") from exc
    if int(ip) < (1 << 24) or int(ip) >= (224 << 24) or ip.is_loopback:
        raise ValueError("유니캐스트 목적지 또는 Source 주소가 아닙니다.")
    return str(ip)


def _integer(value: object, lower: int, upper: int, name: str) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{name}: {lower}~{upper} 범위의 정수만 입력하세요.")
    return value


def _object(value: object, allowed: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("지원하지 않는 입력 항목이 있습니다.")
    return value


@dataclass(frozen=True)
class PingOptions:
    repetitions: int = 5
    timeout: int = 2
    source_mode: str = "ssh_ip"
    data_size: int | None = None
    vlan: int | None = None
    concurrency: int = 5

    def __post_init__(self):
        _integer(self.repetitions, 1, 100, "반복 횟수")
        _integer(self.timeout, 1, 10, "응답 대기 시간")
        _integer(self.concurrency, 1, 10, "동시 SSH")
        if not isinstance(self.source_mode, str) or self.source_mode not in _SOURCE_MODES:
            raise ValueError("지원하지 않는 Source 모드입니다.")
        if self.data_size is not None:
            _integer(self.data_size, 0, 1472, "Data Size")
        if self.source_mode == "vlan":
            _integer(self.vlan, 1, 4094, "Source VLAN")
        elif self.vlan is not None:
            raise ValueError("VLAN Source 모드에서만 VLAN을 지정할 수 있습니다.")

    @classmethod
    def parse(cls, value: object) -> PingOptions:
        obj = _object(
            value,
            {"repetitions", "timeout", "source_mode", "data_size", "vlan", "concurrency"},
        )
        return cls(**obj)

    @classmethod
    def preset(cls, name: str) -> PingOptions:
        if not isinstance(name, str) or name not in _PRESETS:
            raise ValueError("지원하지 않는 프리셋입니다.")
        repetitions, timeout = _PRESETS[name]
        return cls(repetitions=repetitions, timeout=timeout)

    @property
    def command_timeout(self) -> int:
        # Not an ICMP RTT or a guaranteed wall-clock duration.
        # Leave headroom for command echo, inter-request gaps and prompt recovery.
        return 15 + self.repetitions * (self.timeout + 2)


@dataclass(frozen=True)
class Destination:
    ip: str
    label: str = ""

    def __post_init__(self):
        unicast_ipv4(self.ip)
        if (
            not isinstance(self.label, str)
            or len(self.label) > 80
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in self.label)
            or any(0xD800 <= ord(ch) <= 0xDFFF for ch in self.label)
        ):
            raise ValueError("서버 이름은 제어문자 없는 80자 이하여야 합니다.")


@dataclass(frozen=True)
class PingPlan:
    targets: tuple[DeviceTarget, ...]
    destinations: tuple[Destination, ...]
    options: PingOptions
    source_ips: tuple[tuple[DeviceTarget, str], ...] = ()

    def __post_init__(self):
        if not 1 <= len(self.targets) <= MAX_SWITCHES:
            raise ValueError("스위치는 1~300대를 지정하세요.")
        if not 1 <= len(self.destinations) <= MAX_DESTINATIONS:
            raise ValueError("서버는 1~10대를 지정하세요.")
        if self.total_pairs > MAX_PAIRS:
            raise ValueError("테스트 조합은 최대 3,000개입니다.")
        for target in self.targets:
            unicast_ipv4(target.ip)
            _integer(target.port, 1, 65535, "SSH 포트")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("스위치 IP/SSH 포트가 중복됩니다.")
        if len({d.ip for d in self.destinations}) != len(self.destinations):
            raise ValueError("목적지 IP가 중복됩니다.")
        mapping = dict(self.source_ips)
        if self.options.source_mode == "ip":
            if len(mapping) != len(self.source_ips) or set(mapping) != set(self.targets):
                raise ValueError("모든 스위치에 별도의 Source IP를 지정하세요.")
            for ip in mapping.values():
                unicast_ipv4(ip)
        elif mapping:
            raise ValueError("직접 IP Source 모드에서만 Source 매핑을 사용할 수 있습니다.")

    @property
    def total_pairs(self) -> int:
        return len(self.targets) * len(self.destinations)

    @property
    def total_requests(self) -> int:
        return self.total_pairs * self.options.repetitions

    def requested_source(self, target: DeviceTarget) -> str | None:
        if target not in self.targets:
            raise ValueError("등록된 스위치가 아닙니다.")
        mode = self.options.source_mode
        if mode == "auto":
            return None
        if mode == "ssh_ip":
            return target.ip
        if mode == "ip":
            return dict(self.source_ips)[target]
        return str(self.options.vlan)

    @classmethod
    def parse(cls, value: object) -> PingPlan:
        body = _object(value, {"targets", "destinations", "options", "source_ips"})
        raw = body.get("targets")
        if not isinstance(raw, str) or not 1 <= len(raw) <= MAX_INPUT:
            raise ValueError("스위치 목록을 입력하세요.")
        # Only CR/LF are line delimiters; never silently normalize other controls.
        if any(ord(ch) < 32 and ch not in "\r\n" for ch in raw):
            raise ValueError("스위치 목록에 허용하지 않는 제어문자가 있습니다.")
        targets = []
        for line in raw.split("\n"):
            value = line.removesuffix("\r").strip(" ")
            if not value:
                continue
            ip, sep, port = value.partition(":")
            if sep and not re.fullmatch(r"[0-9]{1,5}", port):
                raise ValueError("SSH 포트를 확인하세요.")
            targets.append(
                DeviceTarget(
                    unicast_ipv4(ip),
                    _integer(int(port), 1, 65535, "SSH 포트") if sep else 22,
                )
            )
            if len(targets) > MAX_SWITCHES:
                raise ValueError("스위치는 최대 300대입니다.")
        raw_destinations = body.get("destinations")
        if not isinstance(raw_destinations, list) or not 1 <= len(raw_destinations) <= 10:
            raise ValueError("서버 목록을 1~10대 입력하세요.")
        destinations = []
        for item in raw_destinations:
            entry = _object(item, {"ip", "label"})
            if "ip" not in entry:
                raise ValueError("목적지 IPv4 주소를 입력하세요.")
            destinations.append(Destination(**entry))
        options = PingOptions.parse(body.get("options", {}))
        sources = body.get("source_ips", {})
        if not isinstance(sources, dict):
            raise ValueError("Source IP 매핑을 확인하세요.")
        target_by_endpoint = {target.endpoint: target for target in targets}
        if set(sources) - set(target_by_endpoint):
            raise ValueError("등록되지 않은 스위치 Source 매핑입니다.")
        return cls(
            tuple(targets),
            tuple(destinations),
            options,
            tuple((target_by_endpoint[key], unicast_ipv4(ip)) for key, ip in sources.items()),
        )


def verify_source(
    plan: PingPlan, target: DeviceTarget, owned: tuple[tuple[int, str], ...]
) -> str | None:
    """Check normalized, independently acquired interface evidence.

    This does not parse show-ip output or infer an address from the SSH peer.
    The adapter must pass only verified interface rows from that device.
    """
    requested = plan.requested_source(target)
    if requested is None:
        return None
    evidence = {(_integer(vlan, 1, 4094, "VLAN"), unicast_ipv4(ip)) for vlan, ip in owned}
    if plan.options.source_mode == "vlan":
        candidates = {ip for vlan, ip in evidence if vlan == plan.options.vlan}
        if len(candidates) != 1:
            raise ValueError("Source VLAN의 단일 IPv4 주소를 확인하지 못했습니다.")
    elif requested not in {ip for _, ip in evidence}:
        raise ValueError("지정 Source IP가 장비에 할당되었는지 확인하지 못했습니다.")
    return requested


def preview_command(plan: PingPlan, target: DeviceTarget, destination: Destination) -> str:
    """Preview only: ownership and SSH model verification are required before use."""
    if destination not in plan.destinations:
        raise ValueError("등록된 목적지가 아닙니다.")
    options = plan.options
    command = f"ping {destination.ip} repetitions {options.repetitions} timeout {options.timeout}"
    source = plan.requested_source(target)
    if source is not None:
        command += f" source {source}"
    if options.data_size is not None:
        command += f" data-size {options.data_size}"
    return command


def preview_plan(value: object) -> dict:
    """Validate the same plan as admission without accessing any device."""
    plan = PingPlan.parse(value)
    commands = []
    for target in plan.targets:
        for destination in plan.destinations:
            if len(commands) == 20:
                break
            commands.append(
                {
                    "endpoint": target.endpoint,
                    "destination": destination.ip,
                    "command": preview_command(plan, target, destination),
                }
            )
        if len(commands) == 20:
            break
    return {
        "switches": len(plan.targets),
        "destinations": len(plan.destinations),
        "totalPairs": plan.total_pairs,
        "totalRequests": plan.total_requests,
        "requestedConcurrency": plan.options.concurrency,
        "effectiveConcurrency": 1,
        "commands": commands,
        "truncated": len(commands) < plan.total_pairs,
        "sourceVerified": False,
    }


@dataclass(frozen=True)
class PingObservation:
    status: str
    sent: int | None = None
    received: int | None = None
    loss_pct: float | None = None
    rtt_min_ms: float | None = None
    rtt_avg_ms: float | None = None
    rtt_max_ms: float | None = None
    rtt_evidence: str | None = None
    rtt_origin: str | None = None


_SUMMARY = re.compile(
    r"^(\d{1,3}) packets? transmitted,\s*(\d{1,3}) packets? received,\s*"
    r"(\d{1,3}(?:\.\d{1,6})?)% packet loss\.?$",
    re.IGNORECASE,
)
_RTT = re.compile(
    r"^round[-\u2013]trip \(ms\) min/avg/max\s*=\s*"
    r"(<\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?)\s*/\s*"
    r"(<\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?)\s*/\s*"
    r"(<\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?)\.?$",
    re.IGNORECASE,
)
_REPLY = re.compile(
    r"^(\d{1,3}(?:\.\d{1,3}){3}) is alive,\s*"
    r"(?:iteration (\d{1,3}),\s*)?time\s*=\s*(<\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?)\s*ms\.?$",
    re.IGNORECASE,
)
_TIMEOUT = re.compile(r"^(?:request timed out|no response from destination)\.?$", re.I)
_UNREACHABLE = re.compile(r"^(?:the )?destination (?:address |host )?is unreachable\.?$", re.I)
_SOURCE_ERROR = re.compile(
    r"^(?:invalid source (?:ip )?address|source (?:ip )?address is not valid|"
    r"invalid source vlan|source vlan has no ip address)\.?$",
    re.I,
)
_CLI_ERROR = re.compile(
    r"^(?:%\s*)?(?:invalid input|invalid command|unknown command|incomplete command|"
    r"incomplete input|ambiguous command|ambiguous input|permission denied|not authorized|"
    r"access denied|command authorization failed|authorization failed|"
    r"unrecognized command)(?:[.:].*)?$",
    re.I,
)


def _measured(
    sent: int, received: int, rtt: tuple[str, ...] = (), *, origin: str | None = None
) -> PingObservation:
    status = "normal" if sent == received else "partial_loss" if received else "no_reply"
    loss = 100.0 * (sent - received) / sent
    values = None
    evidence = None
    if received and rtt:
        # Preserve bound notation, never replace <1 with 0ms or a fabricated number.
        evidence = "/".join(rtt)
        if all(not value.startswith("<") for value in rtt):
            candidate = tuple(float(value) for value in rtt)
            if all(isfinite(value) and value >= 0 for value in candidate):
                if candidate[0] <= candidate[1] <= candidate[2]:
                    values = candidate
                else:
                    evidence = None
    return PingObservation(
        status,
        sent,
        received,
        loss,
        *(values or (None, None, None)),
        evidence,
        origin if evidence else None,
    )


def _numbered_replies(replies, repetitions: int) -> bool:
    if not replies or any(match[2] is None for match in replies):
        return False
    indices = [int(match[2]) for match in replies]
    return len(set(indices)) == len(indices) and all(1 <= n <= repetitions for n in indices)


def _from_replies(sent: int, received: int, replies) -> PingObservation:
    # Only derive statistics when all successful reply times were collected.
    if not received or len(replies) != received:
        return _measured(sent, received)
    times = tuple(match[3] for match in replies)
    if any(value.startswith("<") for value in times):
        # The samples themselves are useful evidence, but their mean is unknown.
        return PingObservation(
            "normal" if sent == received else "partial_loss",
            sent,
            received,
            100.0 * (sent - received) / sent,
            rtt_evidence=", ".join(times),
            rtt_origin="reply_bounds",
        )
    values = [float(value) for value in times]
    if not all(isfinite(value) and value >= 0 for value in values):
        return _measured(sent, received)
    return _measured(
        sent,
        received,
        tuple(str(value) for value in (min(values), sum(values) / len(values), max(values))),
        origin="replies",
    )


def parse_ping_output(
    output: str, destination: str, repetitions: int, *, completed: bool
) -> PingObservation:
    """Classify only bounded CLI output from a verified complete command.

    An incomplete command, malformed count, contradictory summary or unknown
    format must remain unknown; SSH or missing measurement is not packet loss.
    """
    destination = unicast_ipv4(destination)
    _integer(repetitions, 1, 100, "반복 횟수")
    unknown = PingObservation("unknown")
    if completed is not True or not isinstance(output, str) or len(output) > MAX_OUTPUT:
        return unknown
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return unknown
    summaries = [m for line in lines if (m := _SUMMARY.fullmatch(line))]
    statistics = [m for line in lines if (m := _RTT.fullmatch(line))]
    replies = [m for line in lines if (m := _REPLY.fullmatch(line))]
    if any(m[1] != destination for m in replies):
        return unknown
    errors = {
        status
        for regex, status in (
            (_SOURCE_ERROR, "source_error"),
            (_CLI_ERROR, "cli_error"),
            (_UNREACHABLE, "unreachable"),
        )
        if any(regex.fullmatch(line) for line in lines)
    }
    if errors:
        if len(errors) != 1 or summaries or replies:
            return unknown
        return PingObservation(errors.pop())
    if summaries:
        if len(summaries) != 1 or len(statistics) > 1:
            return unknown
        sent, received = int(summaries[0][1]), int(summaries[0][2])
        if sent != repetitions or not 0 <= received <= sent:
            return unknown
        reported_loss = float(summaries[0][3])
        if not 0 <= reported_loss <= 100 or abs(reported_loss - 100 * (sent - received) / sent) > 1:
            return unknown
        if len(replies) > received:
            return unknown
        if any(match[2] is not None for match in replies) and not _numbered_replies(
            replies, repetitions
        ):
            return unknown
        if not received and statistics:
            return unknown
        if statistics:
            return _measured(sent, received, statistics[0].groups(), origin="summary")
        if replies and _numbered_replies(replies, repetitions):
            return _from_replies(sent, received, replies)
        return _measured(sent, received)
    # The documented one-shot response has no summary.
    if repetitions == 1 and len(lines) == 1:
        if len(replies) == 1 and replies[0][2] in (None, "1"):
            return _measured(1, 1, (replies[0][3],) * 3, origin="single_reply")
        if _TIMEOUT.fullmatch(lines[0]):
            return _measured(1, 0)
    # Some AOS-S builds return one line per successful repetition but omit
    # a summary. Accept only a complete set of unique matching reply numbers.
    if len(replies) == repetitions == len(lines) and _numbered_replies(replies, repetitions):
        return _from_replies(repetitions, repetitions, replies)
    # Without trustworthy full transmission evidence do not infer batch loss.
    return unknown
