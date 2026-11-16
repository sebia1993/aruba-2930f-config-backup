"""Immutable grouped query contracts; no credentials or persistence state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from .models import DeviceIdentity, DeviceTarget, ErrorCode
from .port_control import PortSnapshot, validate_interface

if TYPE_CHECKING:
    from .poe_power import LLDPObservation, PowerAssessment, PowerEvidence

MAX_TARGETS = 100
MAX_INTERFACES_PER_TARGET = 384
MAX_TOTAL_INTERFACES = 2_000
MAX_LABEL_LENGTH = 80


class QueryStatus(StrEnum):
    OK = "ok"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class PoETarget:
    target: DeviceTarget
    interfaces: tuple[str, ...]
    label: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.target, DeviceTarget) or type(self.target.port) is not int:
            raise ValueError("올바른 IPv4/SSH 포트 대상을 입력하세요.")
        if not isinstance(self.interfaces, tuple) or not self.interfaces:
            raise ValueError("조회할 인터페이스를 입력하세요.")
        ports = tuple(dict.fromkeys(validate_interface(port) for port in self.interfaces))
        if len(ports) > MAX_INTERFACES_PER_TARGET:
            raise ValueError("스위치당 인터페이스는 최대 384개입니다.")
        if not isinstance(self.label, str) or len(self.label) > MAX_LABEL_LENGTH:
            raise ValueError("이름은 80자 이하로 입력하세요.")
        try:
            self.label.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("이름에 올바르지 않은 Unicode 문자가 있습니다.") from exc
        object.__setattr__(self, "interfaces", ports)


@dataclass(frozen=True, slots=True)
class PoEQueryResult:
    group: PoETarget
    interface: str
    status: QueryStatus
    snapshot: PortSnapshot | None = None
    error_code: ErrorCode | None = None
    message: str = ""
    finished_at: str = ""
    identity: DeviceIdentity | None = None
    power_evidence: PowerEvidence | None = None
    lldp: LLDPObservation | None = None
    assessment: PowerAssessment | None = None
    poe_finished_at: str = ""
    complete: bool = True
