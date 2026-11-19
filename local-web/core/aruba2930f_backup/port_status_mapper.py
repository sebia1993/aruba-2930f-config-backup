"""Read-only presentation of explicitly registered 2930F port observations.

Never infer a service outage from absent telemetry, zero measured watts, or
missing LLDP neighbors. The existing query and mutation engines own truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv4Address

from .poe_power import LLDPStatus
from .poe_query_models import PoEQueryResult, PoETarget, QueryStatus
from .port_power_diagnosis import diagnose_port_power

PortKey = tuple[str, str]


class MatrixState(StrEnum):
    NORMAL = "normal"
    ATTENTION = "attention"
    WARNING = "warning"
    OFF = "off"
    UNKNOWN = "unknown"
    LOADING = "loading"


STATE_LABELS: dict[MatrixState, str] = {
    MatrixState.NORMAL: "링크 Up",
    MatrixState.ATTENTION: "링크 Down · 확인 필요",
    MatrixState.WARNING: "일부 확인 필요",
    MatrixState.OFF: "설정 OFF",
    MatrixState.UNKNOWN: "미수집 / 확인 불가",
    MatrixState.LOADING: "조회 중",
}


@dataclass(frozen=True, slots=True)
class MatrixPort:
    key: PortKey
    switch: str
    ip: str
    interface: str
    state: MatrixState
    link: str
    poe: str
    watts: str
    ap: str
    note: str
    observed_at: str

    @property
    def title(self) -> str:
        return f"{self.switch} · {self.ip} / {self.interface}"

    @property
    def tooltip(self) -> str:
        return (
            f"{self.title}\n{STATE_LABELS[self.state]}\n"
            f"링크: {self.link} | PoE: {self.poe} | 실측: {self.watts}\n"
            f"LLDP: {self.ap}\n{self.note}"
        )


def port_sort_key(interface: str) -> tuple[int, ...]:
    """Numeric order for AOS-S ports such as 2, 10, 1/2 and 1/10."""
    return tuple(int(part) for part in interface.split("/"))


def group_sort_key(group: PoETarget) -> tuple[int, int]:
    return int(IPv4Address(group.target.ip)), group.target.port


def make_matrix_port(
    group: PoETarget,
    interface: str,
    result: PoEQueryResult | None = None,
    *,
    loading: bool = False,
) -> MatrixPort:
    key = (group.target.endpoint, interface)
    snapshot = result.snapshot if result else None
    identity = result.identity if result else None
    hostname = (
        (snapshot.hostname if snapshot else "")
        or (identity.hostname if identity else "")
        or group.label
        or "확인 전"
    )
    link = (result.observed_link if result else "") or "확인 불가"
    poe = (
        "OFF"
        if snapshot and snapshot.poe_enabled is False
        else "ON"
        if snapshot and snapshot.poe_enabled is True
        else "확인 불가"
    )
    watts = (
        f"{snapshot.watts:g} W" if snapshot is not None and snapshot.watts is not None else "미수집"
    )
    lldp = result.lldp if result else None
    if lldp and lldp.status is LLDPStatus.OK:
        ap = " / ".join(n.system_name or "이름 없음" for n in lldp.neighbors) or "이웃 없음"
    elif lldp:
        ap = f"LLDP {lldp.status.value}"
    else:
        ap = "미수집"
    note = result.message if result and result.message else ""
    if loading or (result is not None and not result.complete):
        state = MatrixState.LOADING
    elif result is None or snapshot is None or result.status is not QueryStatus.OK:
        state = MatrixState.UNKNOWN
        note = note or "최신 포트 측정값이 없습니다."
    elif not snapshot.enabled:
        state = MatrixState.OFF
        note = "포트 관리 설정 Disable. PoE 설정과는 별개입니다."
    elif snapshot.poe_enabled is False:
        state = MatrixState.OFF
        note = "PoE 설정 OFF. 장애 여부는 운영 의도와 비교해야 합니다."
    elif link.casefold() == "down":
        state = MatrixState.ATTENTION
        note = "링크 Down 관측. 서비스 장애 여부는 별도 확인이 필요합니다."
    elif snapshot.poe_enabled is None or snapshot.poe_status.strip().casefold() in (
        "fault",
        "other fault",
        "disabled",
    ):
        state = MatrixState.WARNING
        note = diagnose_port_power(snapshot).reason
    elif lldp and lldp.status in (LLDPStatus.FAILED, LLDPStatus.CANCELLED):
        state = MatrixState.WARNING
        note = "LLDP 확인 실패. 이미 수집한 링크/PoE 정보는 유효합니다."
    elif result.assessment is not None and result.assessment.label == "확인 필요":
        state = MatrixState.WARNING
        note = result.assessment.reason
    elif link.casefold() == "up":
        state = MatrixState.NORMAL
    else:
        state = MatrixState.UNKNOWN
        note = "링크의 Up/Down 상태를 확정할 수 없습니다."
    return MatrixPort(
        key=key,
        switch=hostname,
        ip=group.target.ip,
        interface=interface,
        state=state,
        link=link,
        poe=poe,
        watts=watts,
        ap=ap,
        note=note,
        observed_at=(snapshot.observed_at if snapshot else "")
        or (result.finished_at if result else ""),
    )
