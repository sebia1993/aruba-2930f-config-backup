"""Explain AOS-S interface administration separately from PoE power delivery.

Never infer an administrative power-off from zero measured watts or a down link.
This module does not authorize port changes or modify controller safety checks.
"""

from __future__ import annotations

from dataclasses import dataclass

from .port_control import PortSnapshot


@dataclass(frozen=True, slots=True)
class PortPowerDiagnosis:
    """One operator-facing state with evidence and an explicit reason."""

    label: str
    reason: str


def diagnose_port_power(snapshot: PortSnapshot | None) -> PortPowerDiagnosis:
    """Use explicit interface/PoE settings before interpreting measured watts."""
    if snapshot is None:
        return PortPowerDiagnosis("조회 확인 필요", "장비에서 포트/PoE 설정을 확인하지 못했습니다.")

    disabled_port = not snapshot.enabled
    prefix = "포트 Disable · " if disabled_port else ""
    port_reason = (
        "포트는 네트워크 통신상 Disable입니다. "
        "AOS-S에서 인터페이스 Disable만으로 PoE OFF라고 단정할 수 없습니다. "
        if disabled_port
        else ""
    )

    if snapshot.poe_enabled is False:
        return PortPowerDiagnosis(
            prefix + "PoE OFF(설정)",
            port_reason
            + "Power Enable=No로 확인됐습니다. 장비의 PoE 설정에서 전원 공급을 껐습니다. "
            "포트 자체의 Enable/Disable과는 별개입니다.",
        )
    if snapshot.poe_enabled is None:
        state = snapshot.poe_status.casefold()
        if "미지원" in state or "unsupported" in state:
            return PortPowerDiagnosis(
                prefix + "PoE 미지원",
                port_reason + "PoE를 지원하지 않는 포트로 확인됐습니다.",
            )
        return PortPowerDiagnosis(
            prefix + "PoE 설정 미확인",
            port_reason + "Power Enable을 확인하지 못해 전원 차단 여부를 판정할 수 없습니다.",
        )

    state = snapshot.poe_status.strip().casefold()
    if state == "disabled":
        supply = "PoE 공급 불가"
        explanation = (
            "Power Enable=Yes지만 Detection Status=Disabled입니다. "
            "AOS-S에서 가용 PoE 전력 부족일 수 있으므로 전원 예산/장애를 확인하세요. "
            "관리자가 PoE를 OFF한 상태로 판정하지 않습니다."
        )
    elif state in ("fault", "other fault"):
        supply = "PoE 공급 오류"
        explanation = (
            f"Power Enable=Yes지만 공급 상태가 {snapshot.poe_status}입니다. "
            "스위치와 연결 장비의 PoE 오류를 확인하세요."
        )
    elif state == "searching":
        supply = "PoE 대기"
        explanation = (
            "Power Enable=Yes이며 전원 장치를 검색 중입니다. "
            "현재 0W이더라도 PoE를 OFF한 설정은 아닙니다."
        )
    elif state == "delivering":
        if snapshot.watts == 0:
            supply = "PoE 공급 중(0W)"
            explanation = (
                "Power Enable=Yes, Detection Status=Delivering이지만 실측은 0W입니다. "
                "상태·측정값이 일치하는지 재조회가 필요합니다."
            )
        else:
            supply = "PoE 공급 중"
            explanation = (
                "Power Enable=Yes, Detection Status=Delivering입니다. "
                "실측 W는 설정의 ON/OFF 여부와 구분해 판단합니다."
            )
    else:
        supply = "PoE 공급 확인 필요"
        explanation = (
            f"Power Enable=Yes지만 공급 상태({snapshot.poe_status})가 명확하지 않습니다. "
            "실제 PoE 상태를 확인하세요."
        )

    return PortPowerDiagnosis(prefix + supply, port_reason + explanation)



def port_admin_link_label(snapshot: PortSnapshot | None) -> str:
    """The port admin setting is not interchangeable with the link state."""
    if snapshot is None:
        return "포트 상태 미확인"
    admin = "Disable" if not snapshot.enabled else "Enable"
    return f"포트 {admin} · Link {snapshot.link_status}"


def poe_supply_label(snapshot: PortSnapshot | None) -> str:
    """Show PoE configuration/delivery alone, separate from port administration."""
    diagnosis = diagnose_port_power(snapshot)
    prefix = "포트 Disable · "
    if snapshot is not None and not snapshot.enabled and diagnosis.label.startswith(prefix):
        return diagnosis.label[len(prefix) :]
    return diagnosis.label
