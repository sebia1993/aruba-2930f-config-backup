"""Operator-facing port/PoE status is based on distinct switch observations."""

from __future__ import annotations

from dataclasses import replace

import pytest

from aruba2930f_backup.models import DeviceTarget
from aruba2930f_backup.port_control import PortSnapshot
from aruba2930f_backup.port_power_diagnosis import (
    diagnose_port_power,
    poe_supply_label,
    port_admin_link_label,
)


BASE = PortSnapshot(
    DeviceTarget("192.0.2.10", 22),
    "24",
    "LAB-SW-01",
    "Aruba 2930F",
    True,
    "Up",
    True,
    "Delivering",
    13.2,
)


@pytest.mark.parametrize(
    "values,expected,reason",
    [
        ({}, "PoE 공급 중", "Power Enable=Yes"),
        (
            {"enabled": False, "link_status": "Down", "poe_status": "Searching", "watts": 0.0},
            "포트 Disable · PoE 대기",
            "인터페이스 Disable만으로 PoE OFF라고 단정할 수 없습니다",
        ),
        (
            {"enabled": True, "poe_enabled": False, "poe_status": "사용 안 함", "watts": None},
            "PoE OFF(설정)",
            "Power Enable=No",
        ),
        (
            {"enabled": False, "poe_enabled": False, "poe_status": "사용 안 함", "watts": None},
            "포트 Disable · PoE OFF(설정)",
            "Power Enable=No",
        ),
        (
            {"enabled": True, "link_status": "Down", "poe_status": "Searching", "watts": 0.0},
            "PoE 대기",
            "PoE를 OFF한 설정은 아닙니다",
        ),
        (
            {"enabled": True, "poe_status": "Disabled", "watts": 0.0},
            "PoE 공급 불가",
            "가용 PoE 전력 부족",
        ),
        (
            {"enabled": True, "poe_status": "Fault", "watts": 0.0},
            "PoE 공급 오류",
            "Fault",
        ),
        (
            {"enabled": True, "poe_status": "Delivering", "watts": 0.0},
            "PoE 공급 중(0W)",
            "재조회",
        ),
        (
            {"enabled": True, "poe_enabled": None, "poe_status": "PoE 미지원", "watts": None},
            "PoE 미지원",
            "미지원",
        ),
        (
            {"enabled": False, "poe_enabled": None, "poe_status": "PoE 미지원", "watts": None},
            "포트 Disable · PoE 미지원",
            "인터페이스 Disable만으로",
        ),
    ],
)
def test_distinguish_interface_admin_from_poe_configuration(
    values: dict[str, object], expected: str, reason: str
) -> None:
    snapshot = replace(BASE, **values)
    result = diagnose_port_power(snapshot)
    assert result.label == expected
    assert reason in result.reason


def test_missing_query_never_invents_zero_watts_or_off() -> None:
    result = diagnose_port_power(None)
    assert result.label == "조회 확인 필요"
    assert "설정을 확인하지 못했습니다" in result.reason


def test_link_down_does_not_claim_poe_off() -> None:
    result = diagnose_port_power(
        replace(BASE, enabled=True, link_status="Down", poe_status="Searching", watts=0.0)
    )
    assert "OFF(설정)" not in result.label
    assert "대기" in result.label


def test_disabled_interface_can_still_deliver_power() -> None:
    result = diagnose_port_power(replace(BASE, enabled=False, link_status="Down"))
    assert result.label == "포트 Disable · PoE 공급 중"
    assert "PoE OFF라고 단정" in result.reason



def test_short_operator_labels_preserve_independent_port_and_poe_states() -> None:
    admin_disabled = replace(
        BASE, enabled=False, link_status="Down", poe_status="Delivering", watts=13.2
    )
    assert port_admin_link_label(admin_disabled) == "포트 Disable · Link Down"
    assert poe_supply_label(admin_disabled) == "PoE 공급 중"
    poe_off = replace(BASE, enabled=True, poe_enabled=False, poe_status="사용 안 함", watts=None)
    assert port_admin_link_label(poe_off) == "포트 Enable · Link Up"
    assert poe_supply_label(poe_off) == "PoE OFF(설정)"
    assert poe_supply_label(None) == "조회 확인 필요"
    assert port_admin_link_label(None) == "포트 상태 미확인"
