from __future__ import annotations

import pytest

from aruba2930f_backup.models import CollectionFailure, DiagnosticDetail, ErrorCode
from aruba2930f_backup.validation import (
    ARUBA_2930F_MODELS,
    InputValidationError,
    contains_pager_marker,
    hostname_from_prompt,
    hostname_from_running_config,
    normalize_config_text,
    parse_ipv4_targets,
    require_valid_prompt,
    validate_cli_response,
    validate_device_identity,
    validate_output_limits,
)


def test_parse_ipv4_targets_preserves_order_and_ignores_blank_lines() -> None:
    targets = parse_ipv4_targets(" 192.0.2.10\n\n198.51.100.4  \n", port=2222)

    assert [(target.ip, target.port) for target in targets] == [
        ("192.0.2.10", 2222),
        ("198.51.100.4", 2222),
    ]


def test_parse_ipv4_targets_blocks_entire_batch_and_reports_all_issues() -> None:
    with pytest.raises(InputValidationError) as captured:
        parse_ipv4_targets("192.0.2.1\nnot-an-ip\n192.0.2.1\n2001:db8::1")

    assert len(captured.value.issues) == 3
    assert "Line 2" in captured.value.issues[0]
    assert "duplicate" in captured.value.issues[1]
    assert "Line 4" in captured.value.issues[2]


@pytest.mark.parametrize("text", ["", "\n  \n"])
def test_parse_ipv4_targets_requires_at_least_one_address(text: str) -> None:
    with pytest.raises(InputValidationError, match="at least one"):
        parse_ipv4_targets(text)


@pytest.mark.parametrize("port", [0, 65536])
def test_parse_ipv4_targets_rejects_invalid_port(port: int) -> None:
    with pytest.raises(InputValidationError, match="SSH port"):
        parse_ipv4_targets("192.0.2.1", port=port)


@pytest.mark.parametrize(("sku", "model"), ARUBA_2930F_MODELS.items())
def test_all_official_2930f_skus_are_recognized(sku: str, model: str) -> None:
    identity = validate_device_identity(
        f"Aruba {sku} 2930F Switch\nSoftware revision WC.16.11.0025"
    )

    assert identity.sku == sku
    assert identity.model == model
    assert identity.software_version == "WC.16.11.0025"


def test_modules_can_supply_sku_when_version_identifies_family() -> None:
    identity = validate_device_identity(
        "Aruba 2930F Switch\nSoftware revision WC.16.10.0012",
        "Chassis: 2930F-48G-PoE+-4SFP+ JL256A",
    )

    assert identity.sku == "JL256A"


def test_modules_can_supply_jl255a_when_version_reports_only_software() -> None:
    identity = validate_device_identity(
        "Image stamp:\nAug 20 2026\nWC.16.10.0024\nBoot Image: Primary",
        "Status and Counters - Module Information\nChassis: Aruba 2930F-24G-PoE+-4SFP+ JL255A",
    )

    assert identity.model == "Aruba 2930F 24G PoE+ 4SFP+"
    assert identity.sku == "JL255A"
    assert identity.software_version == "WC.16.10.0024"


def test_show_version_exact_evidence_does_not_require_modules() -> None:
    identity = validate_device_identity("Aruba JL258A 2930F Switch")

    assert identity.sku == "JL258A"


@pytest.mark.parametrize(
    "modules",
    [
        "Chassis: 2930M-24G-PoE+ JL320A    Serial Number: 12345\n"
        "A Aruba 3810M/2930M 4-port SFP+ Module JL083A",
        "Member 1\nChassis: Aruba JL320A 2930M-24G-PoE+ Switch\n"
        "Member 2\nChassis: Aruba JL320A 2930M-24G-PoE+ Switch",
    ],
)
def test_jl320a_chassis_and_running_image_are_identified(modules: str) -> None:
    identity = validate_device_identity(
        "Boot ROM Version: WC.17.02.0006\nImage stamp:\nWC.16.10.0009\nBoot Image: Primary",
        modules,
    )

    assert identity.sku == "JL320A"
    assert identity.model == "Aruba 2930M 24G PoE+ 1-slot"
    assert identity.software_version == "WC.16.10.0009"


@pytest.mark.parametrize(
    "boot", ["Boot ROM Version: WC.17.02.0006", "Boot ROM Version:\nWC.17.02.0006"]
)
def test_jl320a_does_not_present_boot_rom_as_running_software(boot: str) -> None:
    identity = validate_device_identity(f"Aruba JL320A 2930M-24G-PoE+ Switch\n{boot}")
    assert identity.sku == "JL320A"
    assert identity.software_version is None


@pytest.mark.parametrize(
    ("version", "modules"),
    [
        ("Boot ROM Version: WC.17.02.0006", ""),
        ("WC.16.10.0025", "Chassis: Aruba 2930M-24G-PoE+"),
        ("WC.16.10.0025", "A Aruba 2930M Module JL320A"),
        ("WC.16.10.0025", "Aruba 2930M Switch Module JL320A"),
        ("WC.16.10.0025", "Chassis: 2930M-48G JL321A\nA Aruba 2930M Module JL320A"),
        ("Aruba 2930F Switch", "Chassis: 2930M-24G-PoE+ JL320A"),
        ("Aruba JL320A 2930M Switch", "Chassis: Aruba 2930F JL255A"),
        ("WC.16.10.0025", "Chassis: 2930F JL320A"),
        ("WC.16.10.0025", "Chassis: 2930M-48G-PoE+ JL322A"),
        ("WC.16.10.0025", "Chassis: 2930M-48G-PoE+ JL320A"),
        ("WC.16.10.0025", "Chassis: 2930M-24G Switch JL320A"),
        ("Aruba JL320A 2930M Switch", "Chassis: Unknown"),
        ("WC.16.10.0025", "Chassis: 2930M-24G-PoE+ JL320A\nChassis: 2930M-48G JL321A"),
        ("WC.16.10.0025", "Chassis: 2930M-24G-PoE+ JL320A\nChassis: 2930M-48G"),
        ("WC.16.10.0025", "Chassis: 2930M-24G-PoE+ JL320A\nChassis: 2930M R0M67A"),
    ],
)
def test_jl320a_requires_consistent_chassis_evidence(version: str, modules: str) -> None:
    with pytest.raises(CollectionFailure) as captured:
        validate_device_identity(version, modules)
    assert captured.value.code is ErrorCode.MODEL_UNSUPPORTED


_STACK_MODULES = (
    "Status and Counters - Module Information\nStack ID : 01000000-00000001\n"
    "Member\nID Slot Module Description Serial Number Status\n"
    "1 STK Aruba JL325A 2p Stacking Module Up\n2 A Aruba JL081A 4p 10GbE XGT Module Up"
)
_STACK_HEADER = (
    "Stack ID : 01000000-00000001\nStack Status : Active\nMbr\n"
    "ID Mac Address Model Pri Status\n--- ------------- ----------------------------------- --- ----------\n"
)
_STACK_FIRST = "*1 020000-000001 Aruba JL320A 2930M-24G-PoE+ Switch 128 Commander\n"
_STACK_SECOND = "2 020000-000002 Aruba JL320A 2930M-24G-PoE+ Switch 128 Standby"
_UNFORMED_ID = "NO ID - will merge upon connectivity"
_UNFORMED_MODULES = f"Stack ID : {_UNFORMED_ID}\n1 STK Aruba JL325A 2p Stacking Module Up"
_UNFORMED_HEADER = _STACK_HEADER.replace("01000000-00000001", _UNFORMED_ID).replace(
    "Active", "No Stack Formed"
)


def test_jl320a_unformed_stack_with_one_commander() -> None:
    identity = validate_device_identity(
        "Image stamp:\nWC.16.10.0025",
        _UNFORMED_MODULES,
        stacking_output=_UNFORMED_HEADER + _STACK_FIRST,
    )
    assert identity.sku == "JL320A"


@pytest.mark.parametrize(
    "inventory",
    [
        _UNFORMED_HEADER,
        _UNFORMED_HEADER + _STACK_FIRST + _STACK_SECOND,
        _UNFORMED_HEADER + _STACK_FIRST.replace("Commander", "Standby"),
        _UNFORMED_HEADER.replace("No Stack Formed", "Active") + _STACK_FIRST,
        _UNFORMED_HEADER.replace("Stack Status : No Stack Formed", "") + _STACK_FIRST,
        _UNFORMED_HEADER.replace(_UNFORMED_ID, "01000000-00000001") + _STACK_FIRST,
        _UNFORMED_HEADER + _STACK_FIRST.replace("JL320A", "JL321A"),
    ],
)
def test_unformed_stack_rejects_inconsistent_evidence(inventory: str) -> None:
    with pytest.raises(CollectionFailure) as captured:
        validate_device_identity("WC.16.10.0025", _UNFORMED_MODULES, stacking_output=inventory)
    assert captured.value.code is ErrorCode.MODEL_UNSUPPORTED


def test_jl320a_stack_identity_uses_all_members_instead_of_accessory_skus() -> None:
    identity = validate_device_identity(
        "Image stamp:\nWC.16.10.0025",
        _STACK_MODULES,
        stacking_output=_STACK_HEADER + _STACK_FIRST + _STACK_SECOND,
    )
    assert identity.sku == "JL320A"


@pytest.mark.parametrize(
    "inventory",
    [
        "",
        "Stacking is disabled.",
        _STACK_HEADER,
        _STACK_HEADER + _STACK_FIRST,
        _STACK_HEADER + _STACK_FIRST + _STACK_SECOND.replace("JL320A", "JL321A"),
        _STACK_HEADER + _STACK_FIRST + _STACK_SECOND.replace("JL320A", "R0M68A"),
        _STACK_HEADER + _STACK_FIRST + _STACK_SECOND.replace("JL320A", "Unknown"),
        _STACK_HEADER + _STACK_FIRST + "2 020000-000002 128 Missing",
        _STACK_HEADER + _STACK_FIRST + _STACK_SECOND.replace("Standby", "Not Joined"),
        _STACK_HEADER + _STACK_FIRST + _STACK_SECOND.replace("2 020000", "1 020000"),
        _STACK_HEADER.replace("00000001", "00000099") + _STACK_FIRST + _STACK_SECOND,
    ],
)
def test_partial_or_conflicting_stack_never_inherits_one_members_model(inventory: str) -> None:
    with pytest.raises(CollectionFailure) as captured:
        validate_device_identity(
            "Aruba JL320A 2930M-24G-PoE+ Switch\nImage stamp:\nWC.16.10.0025",
            _STACK_MODULES,
            stacking_output=inventory,
        )
    assert captured.value.code is ErrorCode.MODEL_UNSUPPORTED


@pytest.mark.parametrize(
    ("version", "modules", "expected_model"),
    [
        ("Aruba 2930F Switch", "", "Aruba 2930F"),
        ("ArubaOS-Switch WC.16.11.0025", "Chassis: Aruba 2930F", "Aruba 2930F"),
    ],
)
def test_family_marker_without_sku_is_accepted(
    version: str,
    modules: str,
    expected_model: str,
) -> None:
    identity = validate_device_identity(version, modules)

    assert identity.model == expected_model
    assert identity.sku is None


def test_mixed_official_skus_are_reported_as_vsf() -> None:
    identity = validate_device_identity(
        "ArubaOS-Switch WC.16.10.0024",
        "Aruba JL255A 2930F-24G-PoE+-4SFP+ Switch\n"
        "Aruba JL253A 2930F-24G-4SFP+ Switch\n"
        "Aruba JL256A 2930F-48G-PoE+-4SFP+ Switch",
    )

    assert identity.model == "Aruba 2930F VSF"
    assert identity.sku == "JL253A, JL255A, JL256A"


def test_module_and_transceiver_part_numbers_are_not_chassis_conflicts() -> None:
    identity = validate_device_identity(
        "ArubaOS-Switch WC.16.10.0024",
        "Chassis: Aruba 2930F-24G-PoE+-4SFP+ JL255A\n"
        "A JL083A 4-port SFP+ expansion module\n"
        "1 J9150D SFP+ transceiver",
    )

    assert identity.sku == "JL255A"


def test_2930f_vsf_members_keep_their_chassis_identity() -> None:
    identity = validate_device_identity(
        "Image stamp:\nWC.16.10.0025",
        "VSF Member 1: Aruba JL255A 2930F-24G-PoE+-4SFP+ Switch\n"
        "*2 020000-000002 Aruba JL256A 2930F-48G-PoE+-4SFP+ Switch 128 Commander",
    )
    assert identity.sku == "JL255A, JL256A"
    assert identity.model == "Aruba 2930F VSF"


@pytest.mark.parametrize(
    ("version", "modules", "detail"),
    [
        ("", "Chassis: Aruba 2930F JL255A", DiagnosticDetail.IDENTITY_EVIDENCE_MISSING),
        (
            "ArubaOS-Switch WC.16.11.0025",
            "",
            DiagnosticDetail.IDENTITY_EVIDENCE_MISSING,
        ),
        ("Aruba 2930F JL999A", "", DiagnosticDetail.IDENTITY_SKU_UNSUPPORTED),
        (
            "Aruba 2930F JL999A",
            "Chassis: Aruba 2930F JL253A",
            DiagnosticDetail.IDENTITY_SKU_CONFLICT,
        ),
        ("Aruba 2930M JL253A", "", DiagnosticDetail.IDENTITY_FAMILY_CONFLICT),
        (
            "Aruba CX 6200F JL724A",
            "Chassis: Aruba 2930F JL255A",
            DiagnosticDetail.IDENTITY_FAMILY_CONFLICT,
        ),
    ],
)
def test_unsupported_identity_evidence_has_stable_diagnostic_detail(
    version: str,
    modules: str,
    detail: DiagnosticDetail,
) -> None:
    with pytest.raises(CollectionFailure) as captured:
        validate_device_identity(version, modules)

    assert captured.value.code is ErrorCode.MODEL_UNSUPPORTED
    assert captured.value.transient is False
    assert captured.value.diagnostic_detail is detail


def test_cli_error_is_sanitized_and_rejected() -> None:
    with pytest.raises(CollectionFailure) as captured:
        validate_cli_response("show modules", "% Invalid input: modules")

    assert captured.value.code is ErrorCode.COMMAND_REJECTED
    assert "Invalid input" not in captured.value.safe_message


def test_pager_markers_and_windows_config_normalization() -> None:
    marker = "-- MORE --, next page: Space, next line: Enter, quit: Control-C"
    assert contains_pager_marker(f"output\n{marker}")
    with pytest.raises(CollectionFailure) as captured:
        normalize_config_text(f"hostname lab\n{marker}\n")
    assert captured.value.code is ErrorCode.PROMPT_PARSE_FAILED
    assert normalize_config_text("hostname lab\nvlan 1\n") == "hostname lab\r\nvlan 1\r\n"


def test_empty_running_config_is_rejected() -> None:
    with pytest.raises(CollectionFailure) as captured:
        normalize_config_text("\r\n  \r\n")

    assert captured.value.code is ErrorCode.PROMPT_PARSE_FAILED


def test_pager_words_embedded_in_config_are_not_control_markers() -> None:
    config = 'banner motd "Press any key to continue"\n; literal -- MORE -- text must remain\n'

    assert not contains_pager_marker(config)
    assert "Press any key" in normalize_config_text(config)
    assert "-- MORE --" in normalize_config_text(config)


def test_output_bounds_check_bytes_and_lines() -> None:
    validate_output_limits("a\nb", max_bytes=10, max_lines=2)
    with pytest.raises(CollectionFailure) as bytes_error:
        validate_output_limits("한글", max_bytes=5, max_lines=10)
    assert bytes_error.value.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
    with pytest.raises(CollectionFailure):
        validate_output_limits("a\nb\nc", max_bytes=100, max_lines=2)


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("System Name : SW-2930F-01\r\nSystem Contact : admin", "SW-2930F-01"),
        ("SYSTEM NAME  :  서울 RND SW 01", "서울 RND SW 01"),
        ("  System Name\t:\tSW_01  ", "SW_01"),
        ("\x1b[32mSystem Name : building-sw\x1b[0m", "building-sw"),
        ("System Name : VSF-2930F\nSystem Name : VSF-2930F", "VSF-2930F"),
        ('System Name : "branch with spaces"', "branch with spaces"),
        ("System Name : ", None),
        ("System Contact : SW-01", None),
        ("System Name : SW-1\nSystem Name : SW-2", None),
        ("System Name : SW-1\x00bad", None),
        ("System Name : " + "a" * 256, None),
    ),
)
def test_system_info_hostname_parser(text: str, expected: str | None) -> None:
    from aruba2930f_backup.validation import hostname_from_system_information

    assert hostname_from_system_information(text) == expected


def test_prompt_hostname_requires_single_exec_prompt() -> None:
    assert hostname_from_prompt("edge-lab#") == "edge-lab"
    assert hostname_from_prompt("edge-lab>") == "edge-lab"
    assert hostname_from_prompt("edge-lab(config)#") is None
    assert hostname_from_prompt("bad prompt#") is None
    assert require_valid_prompt("bad prompt#") is None
    assert require_valid_prompt("(Aruba 2930F) #") is None
    assert require_valid_prompt("Aruba-2930F-48G-PoE+ #") is None
    with pytest.raises(CollectionFailure) as captured:
        require_valid_prompt("not a prompt")
    assert captured.value.code is ErrorCode.PROMPT_PARSE_FAILED


@pytest.mark.parametrize(
    ("config_text", "expected"),
    (
        ('Running configuration:\r\nhostname "edge-lab"\r\n', "edge-lab"),
        ("hostname edge-lab\nvlan 1\n", "edge-lab"),
        ('hostname "floor 3 edge"\n', "floor 3 edge"),
        ('\x1b[32mhostname "edge-lab"\x1b[0m\n', "edge-lab"),
        ('hostname "edge-lab"\nhostname "edge-lab"\n', "edge-lab"),
    ),
)
def test_running_config_hostname_accepts_unambiguous_top_level_forms(
    config_text: str,
    expected: str,
) -> None:
    assert hostname_from_running_config(config_text) == expected


@pytest.mark.parametrize(
    "config_text",
    (
        "no hostname edge-lab\n",
        " hostname nested-value\n",
        'hostname "unterminated\n',
        "hostname unquoted value\n",
        'hostname ""\n',
        'hostname "bad\tvalue"\n',
        f'hostname "{"x" * 256}"\n',
        'hostname "edge-one"\nhostname "edge-two"\n',
    ),
)
def test_running_config_hostname_ignores_missing_malformed_or_ambiguous_values(
    config_text: str,
) -> None:
    assert hostname_from_running_config(config_text) is None


@pytest.mark.parametrize(
    ("prompt", "detail"),
    (
        ("", DiagnosticDetail.PROMPT_EMPTY),
        ("edge-lab(config)#", DiagnosticDetail.PROMPT_NON_EXEC_MODE),
        ("edge-lab(vlan-10)#", DiagnosticDetail.PROMPT_NON_EXEC_MODE),
        ("bad prompt", DiagnosticDetail.PROMPT_FORMAT),
        ("bad\nprompt#", DiagnosticDetail.PROMPT_FORMAT),
        ("bad\x00prompt#", DiagnosticDetail.PROMPT_FORMAT),
        ("x" * 256 + "#", DiagnosticDetail.PROMPT_FORMAT),
    ),
)
def test_prompt_failure_details_are_stable(prompt: str, detail: DiagnosticDetail) -> None:
    with pytest.raises(CollectionFailure) as captured:
        require_valid_prompt(prompt)

    assert captured.value.diagnostic_detail is detail


@pytest.mark.parametrize(
    "banner",
    [
        "banner motd ^\nedge-lab#",
        'banner motd "unfinished',
        'banner motd "escaped closing quote\\"',
        "banner motd",
    ],
)
def test_unclosed_banner_cannot_be_normalized_as_a_successful_backup(banner) -> None:
    with pytest.raises(CollectionFailure) as captured:
        normalize_config_text(f'hostname "edge-lab"\n{banner}\n')
    assert captured.value.code is ErrorCode.PROMPT_PARSE_FAILED


@pytest.mark.parametrize(
    "banner",
    [
        'banner motd "Welcome"',
        'banner motd "Welcome\\" operator\\nnext line"',
        'banner motd "Welcome\nedge-lab#\nclosing text"',
        "banner motd ^Welcome^",
        "banner motd ^\nWelcome\n^",
        "banner motd #Welcome#",
        "no banner motd",
    ],
)
def test_supported_banner_forms_are_preserved_by_normalization(banner) -> None:
    assert normalize_config_text(banner) == banner.replace("\n", "\r\n") + "\r\n"


@pytest.mark.parametrize(
    "banner",
    [
        "banner motd ^\nInvalid input: a training example\n^",
        "banner motd ^\nPermission denied (illustration)\n^",
        'banner motd "Invalid input: example"',
    ],
)
def test_running_config_cli_error_words_inside_complete_banner_are_literal(banner: str) -> None:
    config = f"hostname edge\n{banner}\ninterface 1\n"
    # The same words outside the banner still indicate a rejected command.
    assert validate_cli_response("show running-config", config) is None
    with pytest.raises(CollectionFailure) as caught:
        validate_cli_response("show running-config", config + "% Invalid input: actual error\n")
    assert caught.value.code is ErrorCode.COMMAND_REJECTED


def test_running_config_hostname_ignores_unindented_banner_content() -> None:
    assert (
        hostname_from_running_config(
            "Running configuration:\nbanner motd ^\nhostname forged-device\n^\n"
        )
        is None
    )
    assert (
        hostname_from_running_config(
            "hostname legitimate\nbanner motd ^\nhostname forged-device\n^\n"
        )
        == "legitimate"
    )
    assert (
        hostname_from_running_config('banner motd "hostname forged-device"\nhostname legitimate\n')
        == "legitimate"
    )
