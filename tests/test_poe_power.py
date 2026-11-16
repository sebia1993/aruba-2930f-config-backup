"""Fictional manufacturer-shaped observations, not measurements of an AP-535."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path

import pytest

from aruba2930f_backup.models import CollectionFailure, ErrorCode
from aruba2930f_backup.poe_power import (
    AssessmentStatus,
    LLDPNeighbor,
    LLDPObservation,
    LLDPStatus,
    PowerEvidence,
    assess_ap535_power,
    parse_lldp_neighbors,
    parse_power_evidence,
)
from aruba2930f_backup.port_control import PoEState, parse_poe_status

POE = """Status and Configuration Information for port 13
 Power Enable :Yes                   PoE Port Status :Delivering
 PLC Class/Type :4/2                  Priority Config :low
 DLC Class/Type :4/2                  Pre-std Detect :off
 Alloc By Config :usage              Configured Type :AP-515
 Alloc By Actual :lldp               PoE Value Config :30 W
 PSE Allocated Power Value :25.5 W
 PD Requested Power Value :25.5 W
 MED LLDP Detect :Disabled            PD TLV Sent Type :dot3
 PSE Reserved Power :9.4 W
 PD Power Draw :8.7 W
"""
HEADER = "LLDP Remote Device Information Detail\n"
BLOCK = """ Local Port :13
 ChassisType :mac-address
 ChassisId :00 00 00 00 00 13
 PortType :mac-address
 PortId :00 00 00 00 00 14
 SysName :fictional-ap
 System Descr :ArubaOS (MODEL:535), Version fictional
 PortDescr :eth0
 Management Address :192.0.2.53
 Poe Device Type :Type2 PD
 Power Source :Only PSE
 PD Requested Power Value :25.5 Watts
 PSE Allocated Power Value :25.5 Watts
"""


def good() -> PowerEvidence:
    return PowerEvidence(
        requested_watts=Decimal("25.5"),
        allocated_watts=Decimal("25.5"),
        plc_class=4,
        plc_type=2,
        dlc_class=4,
        dlc_type=2,
        allocation_method="lldp",
        device_type="dot3",
        allocation_field="PSE Allocated Power Value",
    )


def assess(
    evidence: PowerEvidence | None = None,
    *,
    lldp: LLDPObservation | None = None,
    expected_model: str | None = "AP-535",
):
    return assess_ap535_power(
        PoEState(True, "Delivering", 8.7),
        evidence or good(),
        lldp,
        expected_model=expected_model,
    )


def test_low_draw_with_fulfilled_negotiation_is_normal_port_supply():
    result = assess()
    assert result.status is AssessmentStatus.NORMAL
    assert result.label == "공급·협상 정상"
    assert result.requested_watts == result.allocated_watts == Decimal("25.5")
    assert result.model_source == "configured"
    assert "AP-535" in result.reason
    assert "포트" in result.reason


def test_allocation_shortfall_is_warning():
    assert assess(replace(good(), allocated_watts=Decimal("20"))).status is AssessmentStatus.WARNING


@pytest.mark.parametrize("field", ["requested_watts", "allocated_watts"])
@pytest.mark.parametrize(
    "value", [None, Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")]
)
def test_unusable_negotiation_never_produces_normal(field, value):
    assert assess(replace(good(), **{field: value})).status is AssessmentStatus.UNKNOWN


@pytest.mark.parametrize(
    "poe,status",
    [
        (None, AssessmentStatus.UNKNOWN),
        (PoEState(False, "Fault", 0), AssessmentStatus.OFF),
        (PoEState(True, "Fault", 0), AssessmentStatus.ERROR),
        (PoEState(True, "Other Fault", 0), AssessmentStatus.ERROR),
        (PoEState(True, "Searching", 0), AssessmentStatus.SEARCHING),
        (PoEState(None, "PoE 미지원", None), AssessmentStatus.UNSUPPORTED),
        (PoEState(None, "PoE 조회 불가", None), AssessmentStatus.UNKNOWN),
        (PoEState(True, "Disabled", 0), AssessmentStatus.UNKNOWN),
    ],
)
def test_current_supply_state_has_priority(poe, status):
    assert assess_ap535_power(poe, good(), None).status is status


def test_class_four_alone_and_unknown_profile_are_not_confirmation():
    assert (
        assess(
            PowerEvidence(
                power_class=4, requested_watts=Decimal("25.5"), allocated_watts=Decimal("25.5")
            )
        ).status
        is AssessmentStatus.UNKNOWN
    )
    assert assess(expected_model=None).status is AssessmentStatus.UNKNOWN
    assert assess(expected_model="AP-515").status is AssessmentStatus.UNKNOWN


def test_explicit_type_one_supply_warns_but_class_three_alone_is_unknown():
    assert (
        assess(
            replace(
                good(), device_type="Type1 PD", plc_class=3, dlc_class=3, plc_type=1, dlc_type=1
            )
        ).status
        is AssessmentStatus.WARNING
    )
    assert assess(PowerEvidence(power_class=3)).status is AssessmentStatus.UNKNOWN


def test_bt_class_five_has_valid_supported_supply():
    assert (
        assess(
            replace(
                good(), plc_class=5, dlc_class=5, plc_type=3, dlc_type=3, device_type="Type3 PD"
            )
        ).status
        is AssessmentStatus.NORMAL
    )


def test_lldp_description_confirms_model_but_friendly_name_does_not():
    neighbors = parse_lldp_neighbors(HEADER + BLOCK, "13")
    confirmed = assess(lldp=LLDPObservation(LLDPStatus.OK, neighbors))
    assert confirmed.status is AssessmentStatus.NORMAL
    assert confirmed.model_source == "lldp"
    assert neighbors[0].model == "AP-535"
    named_only = replace(
        neighbors[0], system_name="AP535-friendly", system_description="", model=""
    )
    assert assess(lldp=LLDPObservation(LLDPStatus.OK, (named_only,))).model_source == "configured"


@pytest.mark.parametrize(
    "description,model",
    [
        ("ArubaOS (MODEL:515)", "AP-515"),
        ("Aruba AP-535", "AP-535"),
        ("Regulatory APIN0535", "AP-535"),
        ("ArubaOS (MODEL:205H)", "AP-205H"),
    ],
)
def test_model_comes_from_system_description(description, model):
    neighbor = parse_lldp_neighbors(
        HEADER + BLOCK.replace("ArubaOS (MODEL:535), Version fictional", description), "13"
    )[0]
    assert neighbor.model == model
    expected = AssessmentStatus.NORMAL if model == "AP-535" else AssessmentStatus.UNKNOWN
    assert assess(lldp=LLDPObservation(LLDPStatus.OK, (neighbor,))).status is expected


def test_multiple_neighbors_or_conflicting_sources_are_unknown():
    neighbor = parse_lldp_neighbors(HEADER + BLOCK, "13")[0]
    assert (
        assess(lldp=LLDPObservation(LLDPStatus.OK, (neighbor, neighbor))).status
        is AssessmentStatus.UNKNOWN
    )
    changed = replace(neighbor, power=replace(neighbor.power, allocated_watts=Decimal("20")))
    assert (
        assess(lldp=LLDPObservation(LLDPStatus.OK, (changed,))).status is AssessmentStatus.UNKNOWN
    )
    assert assess(replace(good(), plc_type=1)).status is AssessmentStatus.UNKNOWN
    assert (
        assess(replace(good(), issues=("duplicate requested field",))).status
        is AssessmentStatus.UNKNOWN
    )


def test_lldp_only_valid_pair_can_establish_negotiation():
    neighbor = parse_lldp_neighbors(HEADER + BLOCK, "13")[0]
    assert (
        assess_ap535_power(
            PoEState(True, "Delivering", 0), None, LLDPObservation(LLDPStatus.OK, (neighbor,))
        ).status
        is AssessmentStatus.NORMAL
    )


@pytest.mark.parametrize(
    "device_type",
    [
        "Type2 PSE",
        "802.3at Type2 PSE",
        "not Type2 PD",
        "Type2 PD trailing",
        "prefix 802.3at",
        "Type2 PD / Type2 PSE",
    ],
)
def test_remote_device_role_or_malformed_type_cannot_prove_ap_supply(device_type):
    neighbor = parse_lldp_neighbors(HEADER + BLOCK.replace("Type2 PD", device_type), "13")[0]
    assert neighbor.power is not None
    assert neighbor.power.device_type == device_type
    assert neighbor.power.requested_watts == Decimal("25.5")
    assert neighbor.power.allocated_watts == Decimal("25.5")
    assert neighbor.power.issues
    observation = LLDPObservation(LLDPStatus.OK, (neighbor,))
    for switch_evidence in (None, good()):
        result = assess_ap535_power(PoEState(True, "Delivering", 8.7), switch_evidence, observation)
        assert result.status is AssessmentStatus.UNKNOWN


@pytest.mark.parametrize("device_type", ["Type2 PSE", "not Type2 PD"])
def test_direct_remote_evidence_cannot_bypass_device_role_validation(device_type):
    neighbor = LLDPNeighbor(model="AP-535", power=replace(good(), device_type=device_type))
    result = assess_ap535_power(
        PoEState(True, "Delivering", 8.7),
        None,
        LLDPObservation(LLDPStatus.OK, (neighbor,)),
    )
    assert result.status is AssessmentStatus.UNKNOWN


@pytest.mark.parametrize(
    "device_type", ["Type2 PD", "Type 2 PD", "TYPE2 PD", "Type\t2\tPD", "802.3at Type2 PD"]
)
@pytest.mark.parametrize(
    "allocated,status", [("25.5", AssessmentStatus.NORMAL), ("20", AssessmentStatus.WARNING)]
)
def test_recognized_pd_type_retains_fulfilled_and_shortfall_assessment(
    device_type, allocated, status
):
    block = BLOCK.replace("Type2 PD", device_type).replace(
        "PSE Allocated Power Value :25.5 Watts", f"PSE Allocated Power Value :{allocated} Watts"
    )
    neighbor = parse_lldp_neighbors(HEADER + block, "13")[0]
    assert neighbor.power is not None and not neighbor.power.issues
    result = assess_ap535_power(
        PoEState(True, "Delivering", 8.7),
        None,
        LLDPObservation(LLDPStatus.OK, (neighbor,)),
    )
    assert result.status is status


def test_missing_remote_type_does_not_discard_verified_switch_evidence():
    neighbor = parse_lldp_neighbors(
        HEADER + BLOCK.replace(" Poe Device Type :Type2 PD\n", ""), "13"
    )[0]
    assert neighbor.power is not None and neighbor.power.device_type == ""
    assert (
        assess(lldp=LLDPObservation(LLDPStatus.OK, (neighbor,))).status is AssessmentStatus.NORMAL
    )


def test_bare_bt_marker_preserves_explicit_type_four_from_verified_fields():
    evidence = replace(
        good(), plc_class=6, dlc_class=6, plc_type=4, dlc_type=4, device_type="802.3bt"
    )
    assert assess(evidence).status is AssessmentStatus.NORMAL


def test_pair_cannot_be_combined_across_sources():
    neighbor = LLDPNeighbor(power=replace(good(), requested_watts=None))
    result = assess(
        replace(good(), allocated_watts=None), lldp=LLDPObservation(LLDPStatus.OK, (neighbor,))
    )
    assert result.status is AssessmentStatus.UNKNOWN


def test_power_evidence_uses_effective_fields_not_reserved_config_or_measured():
    evidence = parse_power_evidence(POE, "13")
    assert evidence.requested_watts == evidence.allocated_watts == Decimal("25.5")
    assert (evidence.plc_class, evidence.plc_type, evidence.dlc_class, evidence.dlc_type) == (
        4,
        2,
        4,
        2,
    )
    assert evidence.allocation_method == "lldp"
    assert evidence.device_type == "dot3"
    assert evidence.issues == ()
    assert assess(evidence).status is AssessmentStatus.NORMAL


@pytest.mark.parametrize(
    "value,expected",
    [("25500 mW", Decimal("25.500")), ("25.5 watts", Decimal("25.5")), ("25.5W", Decimal("25.5"))],
)
def test_power_units_and_spacing(value, expected):
    evidence = parse_power_evidence(POE.replace("25.5 W", value), "13")
    assert evidence.requested_watts == evidence.allocated_watts == expected


@pytest.mark.parametrize(
    "value", ["", "n/a", "0 W", "-3 W", "NaN W", "Infinity W", "25.5", "25.5 kW"]
)
def test_invalid_or_missing_power_retains_uncertainty(value):
    evidence = parse_power_evidence(
        POE.replace("PD Requested Power Value :25.5 W", f"PD Requested Power Value :{value}"), "13"
    )
    assert assess(evidence).status is AssessmentStatus.UNKNOWN


@pytest.mark.parametrize("value", ["25.5", "20"])
def test_duplicate_power_fields_are_ambiguous_even_if_equal(value):
    evidence = parse_power_evidence(POE + f"PD Requested Power Value :{value} W\n", "13")
    assert evidence.issues
    assert assess(evidence).status is AssessmentStatus.UNKNOWN


def test_legacy_actual_allocation_is_not_equated_to_measured_or_allocated():
    evidence = parse_power_evidence(
        POE.replace("PSE Allocated Power Value", "PSE Actual Power Value"), "13"
    )
    assert evidence.allocated_watts is None
    assert evidence.allocation_field == "PSE Actual Power Value"
    assert evidence.issues
    assert assess(evidence).status is AssessmentStatus.UNKNOWN


def test_lldp_retains_all_neighbors_names_addresses_and_remote_identity():
    second = BLOCK.replace("fictional-ap", "<b>inert</b>").replace("192.0.2.53", "2001:db8::53")
    neighbors = parse_lldp_neighbors(HEADER + BLOCK + second, "13")
    assert len(neighbors) == 2
    assert neighbors[0].system_name == "fictional-ap"
    assert neighbors[1].system_name == "<b>inert</b>"
    assert neighbors[0].management_addresses == ("192.0.2.53",)
    assert neighbors[1].management_addresses == ("2001:db8::53",)
    assert neighbors[0].port_id == "00 00 00 00 00 14"
    assert neighbors[0].power.requested_watts == Decimal("25.5")


def test_missing_optional_name_and_address_and_alias_labels():
    output = (
        (HEADER + BLOCK)
        .replace(" SysName :fictional-ap\n", "")
        .replace(" Management Address :192.0.2.53\n", "")
        .replace("System Descr", "System Description")
        .replace("PortDescr", "Port Description")
    )
    neighbor = parse_lldp_neighbors(output, "13")[0]
    assert neighbor.system_name == ""
    assert neighbor.management_addresses == ()
    assert neighbor.port_description == "eth0"


@pytest.mark.parametrize("output", [HEADER, HEADER + " No neighbors found.\n"])
def test_recognized_empty_lldp_is_successful_no_neighbor(output):
    assert parse_lldp_neighbors(output, "13") == ()


@pytest.mark.parametrize(
    "output",
    [
        "",
        "Timed out",
        "Invalid input: remote-device",
        HEADER + "Permission denied",
        HEADER + "garbage",
        HEADER + " Local Port :13\n",
        HEADER + BLOCK.replace("Local Port :13", "Local Port :1/13"),
        HEADER + BLOCK.replace(" ChassisId :00 00 00 00 00 13\n", ""),
    ],
)
def test_unrecognized_failed_malformed_or_wrong_port_is_not_empty(output):
    with pytest.raises(CollectionFailure) as caught:
        parse_lldp_neighbors(output, "13")
    assert caught.value.code is ErrorCode.COMMAND_REJECTED


@pytest.mark.parametrize("parser", [parse_power_evidence, parse_lldp_neighbors])
def test_parsers_enforce_output_bounds_and_validate_single_port(parser):
    with pytest.raises(CollectionFailure) as caught:
        parser("x" * (1024 * 1024 + 1), "13")
    assert caught.value.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
    with pytest.raises(ValueError):
        parser(POE, "13;reload")


def test_power_exact_vsf_port_binding():
    with pytest.raises(CollectionFailure):
        parse_power_evidence(POE, "1/13")
    assert parse_power_evidence(
        POE.replace("port 13", "port 1/13"), "1/13"
    ).requested_watts == Decimal("25.5")


def test_contracts_are_immutable():
    evidence = good()
    with pytest.raises(FrozenInstanceError):
        evidence.device_type = "changed"


def test_numeric_zero_is_preserved_distinct_from_a_missing_field():
    zero = parse_power_evidence(
        POE.replace("PD Requested Power Value :25.5 W", "PD Requested Power Value :0 W"), "13"
    )
    missing = parse_power_evidence(POE.replace(" PD Requested Power Value :25.5 W\n", ""), "13")
    assert zero.requested_watts == Decimal("0")
    assert missing.requested_watts is None
    assert assess(zero).status is AssessmentStatus.UNKNOWN


def test_unsupported_allocation_field_cannot_establish_normal_supply():
    assert (
        assess(replace(good(), allocation_field="PSE Actual Power Value")).status
        is AssessmentStatus.UNKNOWN
    )
    assert (
        assess(replace(good(), allocation_field="PoE Value Config")).status
        is AssessmentStatus.UNKNOWN
    )


def test_conflicting_classes_between_otherwise_valid_sources_are_unknown():
    switch = replace(
        good(), plc_class=5, dlc_class=5, plc_type=3, dlc_type=3, device_type="Type3 PD"
    )
    peer = replace(switch, plc_class=6, dlc_class=6)
    neighbor = LLDPNeighbor(model="AP-535", power=peer)
    assert (
        assess(switch, lldp=LLDPObservation(LLDPStatus.OK, (neighbor,))).status
        is AssessmentStatus.UNKNOWN
    )


def test_explicit_unsupported_class_type_combination_is_conflicting():
    assert (
        assess(replace(good(), plc_type=1, dlc_type=1, device_type="Type1 PD")).status
        is AssessmentStatus.UNKNOWN
    )


def test_missing_or_failed_lldp_does_not_reuse_supplied_stale_neighbors():
    stale = LLDPNeighbor(model="AP-515", system_name="old")
    result = assess(lldp=LLDPObservation(LLDPStatus.FAILED, (stale,)))
    assert result.model_source == "configured"
    assert result.status is AssessmentStatus.NORMAL


@pytest.mark.parametrize("parser", [parse_power_evidence, parse_lldp_neighbors])
def test_line_bound_covers_carriage_return_delimited_output(parser):
    with pytest.raises(CollectionFailure) as caught:
        parser("\r" * 10_001, "13")
    assert caught.value.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED


def test_non_lldp_effective_allocation_cannot_prove_current_negotiation():
    assert assess(replace(good(), allocation_method="usage")).status is AssessmentStatus.UNKNOWN


def test_signalling_nan_remains_unknown_even_with_another_source():
    neighbor = LLDPNeighbor(model="AP-535", power=good())
    result = assess(
        replace(good(), requested_watts=Decimal("sNaN")),
        lldp=LLDPObservation(LLDPStatus.OK, (neighbor,)),
    )
    assert result.status is AssessmentStatus.UNKNOWN


def test_power_looking_text_inside_neighbor_name_is_inert():
    block = "\n".join(
        line
        for line in BLOCK.splitlines()
        if not any(
            label in line
            for label in (
                "Poe Device Type",
                "PD Requested Power Value",
                "PSE Allocated Power Value",
            )
        )
    )
    spoof = "name  Poe Device Type :Type2 PD  PD Requested Power Value :25.5 W  PSE Allocated Power Value :25.5 W"
    neighbor = parse_lldp_neighbors(HEADER + block.replace("fictional-ap", spoof), "13")[0]
    assert neighbor.system_name == spoof
    assert neighbor.power is None
    assert (
        assess_ap535_power(
            PoEState(True, "Delivering", 8.7), None, LLDPObservation(LLDPStatus.OK, (neighbor,))
        ).status
        is AssessmentStatus.UNKNOWN
    )


def test_sufficient_lldp_source_is_selected_over_a_modeless_poe_pair():
    modeless = PowerEvidence(
        requested_watts=Decimal("25.5"),
        allocated_watts=Decimal("25.5"),
        allocation_method="lldp",
        allocation_field="PSE Allocated Power Value",
    )
    neighbors = parse_lldp_neighbors(HEADER + BLOCK, "13")
    result = assess(modeless, lldp=LLDPObservation(LLDPStatus.OK, neighbors))
    assert result.status is AssessmentStatus.NORMAL
    assert result.negotiation == "802.3at Type 2"
    assert result.requested_watts == result.allocated_watts == Decimal("25.5")


@pytest.mark.parametrize(
    "field,original",
    [
        ("SysName", "fictional-ap"),
        ("System Descr", "ArubaOS (MODEL:535), Version fictional"),
        ("PortDescr", "eth0"),
    ],
    ids=["name", "description", "port-description"],
)
@pytest.mark.parametrize(
    "value",
    ["error: east-wing", "Timed out laboratory", "permission denied classroom"],
    ids=["error-text", "timeout-text", "permission-text"],
)
def test_error_looking_lldp_text_fields_are_inert(field, original, value):
    neighbors = parse_lldp_neighbors(
        HEADER + BLOCK.replace(f"{field} :{original}", f"{field} :{value}"), "13"
    )
    attr = {
        "SysName": "system_name",
        "System Descr": "system_description",
        "PortDescr": "port_description",
    }[field]
    assert getattr(neighbors[0], attr) == value


@pytest.mark.parametrize(
    "line",
    [
        "error: command unavailable",
        "% Invalid input: remote-device",
        "Timed out waiting for command",
    ],
    ids=["error", "invalid-input", "timeout"],
)
def test_true_cli_failure_lines_are_rejected_even_after_a_valid_neighbor(line):
    with pytest.raises(CollectionFailure) as caught:
        parse_lldp_neighbors(HEADER + BLOCK + line + "\n", "13")
    assert caught.value.code is ErrorCode.COMMAND_REJECTED


def test_poe_configured_type_cannot_manufacture_negotiation():
    output = """Status and Configuration Information for port 13
 Power Enable :Yes  PoE Port Status :Delivering
 Configured Type :fictional  Poe Device Type :Type2 PD  PD Requested Power Value :25.5 W  PSE Allocated Power Value :25.5 W
"""
    evidence = parse_power_evidence(output, "13")
    assert evidence.requested_watts is None
    assert evidence.allocated_watts is None
    assert evidence.device_type == ""
    assert assess(evidence).status is AssessmentStatus.UNKNOWN


def test_second_column_configured_type_cannot_manufacture_negotiation():
    output = """Status and Configuration Information for port 13
 Alloc By Config :usage  Configured Type :fictional  Poe Device Type :Type2 PD  PD Requested Power Value :25.5 W  PSE Allocated Power Value :25.5 W
"""
    evidence = parse_power_evidence(output, "13")
    assert evidence == PowerEvidence()


def test_documented_poe_columns_remain_supported_with_opaque_configured_type():
    poisoned = POE.replace(
        "Configured Type :AP-515", "Configured Type :error: timeout  PD Requested Power Value :20 W"
    )
    evidence = parse_power_evidence(poisoned, "13")
    assert evidence == parse_power_evidence(POE, "13")
    assert assess(evidence).status is AssessmentStatus.NORMAL


def test_explicit_bt_marker_conflicting_with_type_two_is_unknown_after_parsing():
    neighbors = parse_lldp_neighbors(HEADER + BLOCK.replace("Type2 PD", "802.3bt Type2 PD"), "13")
    result = assess_ap535_power(
        PoEState(True, "Delivering", 8.7), None, LLDPObservation(LLDPStatus.OK, neighbors)
    )
    assert result.status is AssessmentStatus.UNKNOWN


def test_bounded_malformed_milliwatt_number_returns_an_issue():
    # Keep the giant value inside this test: never expand it into a pytest ID.
    output = (
        "Status and Configuration Information for port 13\n PD Requested Power Value :"
        + "9" * 1_000_010
        + " mW\n"
    )
    assert len(output.encode()) < 1024 * 1024
    evidence = parse_power_evidence(output, "13")
    assert evidence.requested_watts is None
    assert evidence.issues
    assert assess(evidence).status is AssessmentStatus.UNKNOWN


def full_fixture(name: str) -> str:
    """Full documented format, fictional IDs/AP-535 values; no actual AP measurements."""
    return (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")


def test_full_manufacturer_lldp_sections_preserve_neighbor_and_negotiation():
    output = full_fixture("lldp_full_manufacturer_shape.txt")
    neighbor = parse_lldp_neighbors(output, "13")[0]
    assert neighbor.system_name == "fictional-ap-535"
    assert neighbor.model == "AP-535"
    assert neighbor.management_addresses == ("192.0.2.53",)
    assert neighbor.power is not None
    assert neighbor.power.requested_watts == neighbor.power.allocated_watts == Decimal("25.5")
    assert (
        assess_ap535_power(
            PoEState(True, "Delivering", 8.7), None, LLDPObservation(LLDPStatus.OK, (neighbor,))
        ).status
        is AssessmentStatus.NORMAL
    )


def test_full_manufacturer_poe_numeric_tlv_columns_preserve_effective_pair():
    output = full_fixture("power_evidence_full_manufacturer_shape.txt")
    evidence = parse_power_evidence(output, "13")
    assert evidence.requested_watts == evidence.allocated_watts == Decimal("25.5")
    assert evidence.allocation_method == "lldp"
    assert evidence.device_type == "dot3"
    assert evidence.issues == ()
    poe = parse_poe_status(output, "13")
    assert poe.watts == 8.7
    assert assess_ap535_power(poe, evidence, None).status is AssessmentStatus.NORMAL


@pytest.mark.parametrize(
    "mode",
    ["missing", "heading-only", "empty-fields"],
    ids=["missing", "heading-only", "empty-fields"],
)
def test_full_optional_management_blocks_and_name_can_be_absent(mode):
    output = full_fixture("lldp_full_manufacturer_shape.txt")
    block = " Remote Management Address\n    Type    : ipv4\n    Address : 192.0.2.53\n"
    replacement = {
        "missing": "",
        "heading-only": " Remote Management Address\n",
        "empty-fields": " Remote Management Address\n    Type :\n    Address :\n",
    }[mode]
    output = output.replace(block, replacement).replace(" SysName      : fictional-ap-535\n", "")
    neighbor = parse_lldp_neighbors(output, "13")[0]
    assert neighbor.system_name == ""
    assert neighbor.management_addresses == ()
    assert neighbor.model == "AP-535"


def test_full_management_sections_preserve_all_addresses_and_neighbors():
    output = full_fixture("lldp_full_manufacturer_shape.txt")
    extra = "    Type    : ipv6\n    Address : 2001:db8::53\n"
    first = output.replace("    Address : 192.0.2.53\n", "    Address : 192.0.2.53\n" + extra)
    second = (
        output.split("\n", 1)[1]
        .replace("fictional-ap-535", "second-ap")
        .replace("192.0.2.53", "198.51.100.53")
    )
    neighbors = parse_lldp_neighbors(first + second, "13")
    assert len(neighbors) == 2
    assert neighbors[0].management_addresses == ("192.0.2.53", "2001:db8::53")
    assert neighbors[1].management_addresses == ("198.51.100.53",)
    assert assess(lldp=LLDPObservation(LLDPStatus.OK, neighbors)).status is AssessmentStatus.UNKNOWN


@pytest.mark.parametrize(
    "replacement",
    [
        " Remote Management Address Unexpected",
        " Poe Plus Information Detail Extra",
        " Random Optional Section",
    ],
    ids=["bad-management", "bad-poe", "unknown-section"],
)
def test_full_unknown_or_malformed_headings_are_rejected(replacement):
    output = full_fixture("lldp_full_manufacturer_shape.txt").replace(
        " Remote Management Address", replacement
    )
    with pytest.raises(CollectionFailure) as caught:
        parse_lldp_neighbors(output, "13")
    assert caught.value.code is ErrorCode.COMMAND_REJECTED


@pytest.mark.parametrize(
    "replacement",
    ["    Type : ipv4\n", "    Address : 192.0.2.53\n"],
    ids=["missing-address", "missing-type"],
)
def test_full_truncated_management_pairs_are_rejected(replacement):
    output = full_fixture("lldp_full_manufacturer_shape.txt").replace(
        "    Type    : ipv4\n    Address : 192.0.2.53\n", replacement
    )
    with pytest.raises(CollectionFailure) as caught:
        parse_lldp_neighbors(output, "13")
    assert caught.value.code is ErrorCode.COMMAND_REJECTED


def test_full_fixture_inert_configured_type_and_neighbor_name_defenses():
    poe_output = full_fixture("power_evidence_full_manufacturer_shape.txt")
    spoof = "fictional  PD Requested Power Value :20 W  Poe Device Type :Type1 PD"
    evidence = parse_power_evidence(
        poe_output.replace("Configured Type : fictional", "Configured Type : " + spoof), "13"
    )
    assert evidence == parse_power_evidence(poe_output, "13")
    lldp_output = full_fixture("lldp_full_manufacturer_shape.txt")
    neighbor = parse_lldp_neighbors(
        lldp_output.replace("fictional-ap-535", "error: east  PD Requested Power Value :20 W"), "13"
    )[0]
    assert neighbor.system_name == "error: east  PD Requested Power Value :20 W"
    assert neighbor.power.requested_watts == Decimal("25.5")
