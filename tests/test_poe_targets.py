"""Targets retain endpoint/port pairings before any SSH session is created."""

from pathlib import Path

import pytest

from aruba2930f_backup.models import DeviceTarget
from aruba2930f_backup.poe_query_models import PoETarget
from aruba2930f_backup.poe_targets import (
    make_target,
    merge_targets,
    parse_interfaces,
    parse_pasted_targets,
)


def test_independent_endpoints_merge_only_their_own_ports() -> None:
    targets = parse_pasted_targets(
        "IP\tSSH port\tinterfaces\tlabel\n"
        "192.0.2.1\t22\t1,2\t동관\n"
        "192.0.2.2\t22\t24\t서관\n"
        "192.0.2.1\t22\t2,3\t동관\n"
        "192.0.2.1\t2222\t48\t별도\n"
    )
    assert [(g.target.endpoint, g.interfaces, g.label) for g in targets] == [
        ("192.0.2.1:22", ("1", "2", "3"), "동관"),
        ("192.0.2.2:22", ("24",), "서관"),
        ("192.0.2.1:2222", ("48",), "별도"),
    ]


def test_quoted_csv_and_korean_header_preserve_label() -> None:
    groups = parse_pasted_targets(
        'SW IP,SSH 포트,인터페이스 목록,이름(선택)\r\n192.0.2.1,22,"24,1/24","동관, 1층"\r\n'
    )
    assert groups[0].interfaces == ("24", "1/24")
    assert groups[0].label == "동관, 1층"


def test_single_port_list_deduplicates_in_order() -> None:
    assert parse_interfaces(" 24, 1/24,24 ") == ("24", "1/24")


@pytest.mark.parametrize(
    "value", ["", "1,", ",1", "1,,2", "all", "1-24", "0", "01", "1/1/1", "1;show run", "1\n2"]
)
def test_invalid_interface_lists_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        parse_interfaces(value)


@pytest.mark.parametrize("port", [True, False, 0, 65536, "22.0", "1e2", "+22", ""])
def test_invalid_ssh_ports_rejected(port: object) -> None:
    with pytest.raises(ValueError):
        make_target("192.0.2.1", port, "1")  # type: ignore[arg-type]


def test_conflicting_labels_reject_whole_merge() -> None:
    first = make_target("192.0.2.1", 22, "1", "동관")
    with pytest.raises(ValueError):
        merge_targets([first, make_target("192.0.2.1", 22, "2", "서관")])
    assert first.interfaces == ("1",)
    assert merge_targets([make_target("192.0.2.1", 22, "2"), first])[0].label == "동관"


@pytest.mark.parametrize(
    "text",
    [
        "192.0.2.1\t22\t1\nwrong\t22\t2",
        "192.0.2.1,22,1,extra,unknown",
        '192.0.2.1,22,"1,2',
        '192.0.2.1,22,1"broken',
        "192.0.2.1,22,1\nIP,SSH port,interfaces",
        " ",
        pytest.param("x" * 100_001, id="oversized-paste"),
    ],
)
def test_malformed_or_oversized_paste_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        parse_pasted_targets(text)


def test_target_limits_apply_after_merge() -> None:
    ports = ",".join(str(i) for i in range(1, 385))
    assert len(parse_interfaces(ports)) == 384
    with pytest.raises(ValueError):
        parse_interfaces(ports + ",385")
    with pytest.raises(ValueError):
        make_target("192.0.2.1", 22, "1", "x" * 81)

    targets = [make_target(f"192.0.2.{i}", 22, "1") for i in range(1, 251)]
    targets += [make_target(f"198.51.100.{i}", 22, "1") for i in range(1, 51)]
    assert len(merge_targets(targets)) == 300
    with pytest.raises(ValueError, match="300"):
        merge_targets([*targets, make_target("203.0.113.1", 22, "1")])

    full = [make_target(f"192.0.2.{i}", 22, ports) for i in range(1, 38)]
    remainder = ",".join(str(i) for i in range(1, 193))
    groups = merge_targets([*full, make_target("192.0.2.38", 22, remainder)])
    assert sum(len(g.interfaces) for g in groups) == 14_400
    with pytest.raises(ValueError, match="14,400"):
        merge_targets([*full, make_target("192.0.2.38", 22, remainder + ",193")])


def test_direct_target_construction_enforces_interface_and_port_safety() -> None:
    with pytest.raises(ValueError):
        PoETarget(DeviceTarget("192.0.2.1"), ("all",))
    with pytest.raises(ValueError):
        PoETarget(DeviceTarget("192.0.2.1", True), ("1",))
    with pytest.raises(ValueError):
        PoETarget(DeviceTarget("192.0.2.1"), ())


def test_profile_roundtrip_stores_only_target_fields(tmp_path: Path) -> None:
    import json

    from aruba2930f_backup.poe_targets import load_target_profile, save_target_profile

    path = tmp_path / "targets.json"
    groups = [make_target("192.0.2.1", 2222, "24,1/24", "동관"), make_target("192.0.2.2", 22, "2")]
    save_target_profile(path, groups)
    assert load_target_profile(path) == tuple(groups)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document == {
        "format": "aruba2930f-poe-targets",
        "version": 1,
        "targets": [
            {"ip": "192.0.2.1", "ssh_port": 2222, "interfaces": ["24", "1/24"], "label": "동관"},
            {"ip": "192.0.2.2", "ssh_port": 22, "interfaces": ["2"], "label": ""},
        ],
    }


@pytest.mark.parametrize("label", ["bad\ud800name", "bad\udfffname"])
def test_unpaired_surrogate_label_is_rejected_at_all_input_boundaries(
    tmp_path: Path, label: str
) -> None:
    import json

    from aruba2930f_backup.poe_targets import load_target_profile

    with pytest.raises(ValueError):
        PoETarget(DeviceTarget("192.0.2.1"), ("1",), label)
    with pytest.raises(ValueError):
        make_target("192.0.2.1", 22, "1", label)
    with pytest.raises(ValueError):
        parse_pasted_targets(f"192.0.2.1\t22\t1\t{label}")
    path = tmp_path / "invalid-label.json"
    path.write_text(
        json.dumps(
            {
                "format": "aruba2930f-poe-targets",
                "version": 1,
                "targets": [
                    {"ip": "192.0.2.1", "ssh_port": 22, "interfaces": ["1"], "label": label}
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_target_profile(path)


def test_korean_and_non_bmp_target_names_roundtrip_without_conversion(tmp_path: Path) -> None:
    import json

    from aruba2930f_backup.poe_targets import load_target_profile, save_target_profile

    label = "동관 📡 \U00020000"
    groups = (make_target("192.0.2.1", 22, "1,2", label),)
    assert parse_pasted_targets(f"192.0.2.1\t22\t1,2\t{label}") == groups
    path = tmp_path / "unicode.json"
    save_target_profile(path, groups)
    assert load_target_profile(path) == groups
    document = json.loads(path.read_text(encoding="utf-8"))
    # JSON's valid escaped surrogate pair must decode into one Unicode scalar.
    path.write_text(json.dumps(document, ensure_ascii=True), encoding="utf-8")
    assert load_target_profile(path) == groups


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_top",
        "secret",
        "bool_version",
        "float_version",
        "version",
        "format",
        "bool_port",
        "float_port",
        "string_port",
        "interfaces_string",
        "interfaces_number",
        "missing_label",
        "label_number",
        "invalid_ip",
        "too_many_targets",
        "too_many_ports",
        "duplicate_top",
        "duplicate_target",
        "bad_utf8",
        "bad_json",
        "oversized",
    ],
)
def test_profile_rejects_invalid_or_secret_bearing_content(tmp_path: Path, mutation: str) -> None:
    import json

    from aruba2930f_backup.poe_targets import load_target_profile

    entry: dict[str, object] = {"ip": "192.0.2.1", "ssh_port": 22, "interfaces": ["1"], "label": ""}
    document: dict[str, object] = {
        "format": "aruba2930f-poe-targets",
        "version": 1,
        "targets": [entry],
    }
    if mutation == "unknown_top":
        document["results"] = []
    elif mutation == "secret":
        entry["password"] = "never accepted"
    elif mutation == "bool_version":
        document["version"] = True
    elif mutation == "float_version":
        document["version"] = 1.0
    elif mutation == "version":
        document["version"] = 2
    elif mutation == "format":
        document["format"] = "other"
    elif mutation == "bool_port":
        entry["ssh_port"] = True
    elif mutation == "float_port":
        entry["ssh_port"] = 22.0
    elif mutation == "string_port":
        entry["ssh_port"] = "22"
    elif mutation == "interfaces_string":
        entry["interfaces"] = "1,2"
    elif mutation == "interfaces_number":
        entry["interfaces"] = [1]
    elif mutation == "missing_label":
        del entry["label"]
    elif mutation == "label_number":
        entry["label"] = 5
    elif mutation == "invalid_ip":
        entry["ip"] = "example.com"
    elif mutation == "too_many_targets":
        document["targets"] = [entry] * 301
    elif mutation == "too_many_ports":
        entry["interfaces"] = [str(i) for i in range(1, 386)]
    raw = json.dumps(document).encode()
    if mutation == "duplicate_top":
        raw = raw.replace(b'"version": 1', b'"version": 1, "version": 1')
    elif mutation == "duplicate_target":
        raw = raw.replace(b'"ssh_port": 22', b'"ssh_port": 22, "ssh_port": 22')
    elif mutation == "bad_utf8":
        raw = b"\xff"
    elif mutation == "bad_json":
        raw = b"{"
    elif mutation == "oversized":
        raw = b" " * (1024 * 1024 + 1)
    path = tmp_path / "invalid.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        load_target_profile(path)


def test_profile_replace_failure_preserves_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from aruba2930f_backup.poe_targets import save_target_profile

    path = tmp_path / "targets.json"
    path.write_bytes(b"original")

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        save_target_profile(path, [make_target("192.0.2.1", 22, "1")])
    assert path.read_bytes() == b"original"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["targets.json"]


def test_profile_validation_failure_preserves_existing_file(tmp_path: Path) -> None:
    from aruba2930f_backup.poe_targets import save_target_profile

    path = tmp_path / "targets.json"
    path.write_bytes(b"original")
    with pytest.raises(ValueError):
        save_target_profile(
            path, [make_target("192.0.2.1", 22, "1", "a"), make_target("192.0.2.1", 22, "2", "b")]
        )
    assert path.read_bytes() == b"original"


@pytest.mark.parametrize(
    "text",
    [
        "192.0.2.1\t24,1/24\n192.0.2.2\t2",
        "IP\tinterfaces\n192.0.2.1\t24,1/24\n192.0.2.2\t2",
        'SW IP,인터페이스 목록\r\n192.0.2.1,"24,1/24"\r\n192.0.2.2,2',
    ],
)
def test_two_column_paste_defaults_to_ssh22(text: str) -> None:
    groups = parse_pasted_targets(text)
    assert [(g.target.endpoint, g.interfaces, g.label) for g in groups] == [
        ("192.0.2.1:22", ("24", "1/24"), ""),
        ("192.0.2.2:22", ("2",), ""),
    ]


def test_two_column_paste_rejects_whole_invalid_block() -> None:
    with pytest.raises(ValueError):
        parse_pasted_targets("192.0.2.1\t1\n192.0.2.2\tall")


@pytest.mark.parametrize("delimiter", [",", "\t"], ids=["csv", "tsv"])
@pytest.mark.parametrize("valid_first", [False, True], ids=["immediate", "after-valid-row"])
@pytest.mark.parametrize(
    "header,valid_row,wrong_row",
    [
        (("ip", "ssh_port", "interfaces"), ("192.0.2.9", "2222", "1"), ("192.0.2.1", "22")),
        (
            ("SW IP", "SSH port", "ports", "label"),
            ("192.0.2.9", "2222", "1", "A"),
            ("192.0.2.1", "22"),
        ),
        (
            ("IP 주소", "SSH 포트", "인터페이스 목록", "이름(선택)"),
            ("192.0.2.9", "2222", "1", "A"),
            ("192.0.2.1", "22"),
        ),
        (("ip", "interfaces"), ("192.0.2.9", "1"), ("192.0.2.1", "24", "12")),
        (("Switch IP", "interface list"), ("192.0.2.9", "1"), ("192.0.2.1", "24", "12")),
        (("IP 주소", "인터페이스"), ("192.0.2.9", "1"), ("192.0.2.1", "24", "12", "A")),
    ],
)
def test_declared_header_schema_rejects_rows_from_other_family(
    delimiter: str,
    valid_first: bool,
    header: tuple[str, ...],
    valid_row: tuple[str, ...],
    wrong_row: tuple[str, ...],
) -> None:
    rows = [header, *([valid_row] if valid_first else []), wrong_row]
    text = "\n".join(delimiter.join(row) for row in rows)
    with pytest.raises(ValueError):
        parse_pasted_targets(text)


@pytest.mark.parametrize("delimiter", [",", "\t"], ids=["csv", "tsv"])
@pytest.mark.parametrize("legacy_first", [False, True])
def test_headerless_block_cannot_mix_two_column_and_legacy_families(
    delimiter: str, legacy_first: bool
) -> None:
    basic = ("192.0.2.1", "24")
    legacy = ("192.0.2.2", "2222", "12", "B")
    rows = (legacy, basic) if legacy_first else (basic, legacy)
    with pytest.raises(ValueError):
        parse_pasted_targets("\n".join(delimiter.join(row) for row in rows))


@pytest.mark.parametrize("delimiter", [",", "\t"], ids=["csv", "tsv"])
@pytest.mark.parametrize("header_columns", [0, 3, 4])
def test_legacy_schema_keeps_optional_alias_rows_and_nondefault_profile_roundtrip(
    tmp_path: Path, delimiter: str, header_columns: int
) -> None:
    from aruba2930f_backup.poe_targets import load_target_profile, save_target_profile

    header = ("ip", "ssh_port", "interfaces", "label")[:header_columns]
    rows = ([header] if header else []) + [
        ("192.0.2.1", "2222", "24", "별도"),
        ("192.0.2.1", "22", "12"),
    ]
    groups = parse_pasted_targets("\n".join(delimiter.join(row) for row in rows))
    assert [(g.target.endpoint, g.interfaces, g.label) for g in groups] == [
        ("192.0.2.1:2222", ("24",), "별도"),
        ("192.0.2.1:22", ("12",), ""),
    ]
    path = tmp_path / "legacy-targets.json"
    save_target_profile(path, groups)
    assert load_target_profile(path) == groups


@pytest.mark.parametrize(
    "text",
    [
        "스위치 IP\t포트\n192.0.2.10\t12\n192.0.2.10\t24\n192.0.2.20\t1/12,1/24",
        "스위치IP\t포트번호\n192.0.2.10\t12\n192.0.2.10\t24\n192.0.2.20\t1/12,1/24",
        '스위치 IP,포트\n192.0.2.10,12\n192.0.2.10,24\n192.0.2.20,"1/12,1/24"',
    ],
)
def test_excel_two_columns_with_common_korean_headers_merge_identical_switches(
    text: str,
) -> None:
    groups = parse_pasted_targets(text)
    assert [(g.target.endpoint, g.interfaces) for g in groups] == [
        ("192.0.2.10:22", ("12", "24")),
        ("192.0.2.20:22", ("1/12", "1/24")),
    ]


def test_bad_excel_row_has_line_number_and_never_uses_csv_fallback() -> None:
    with pytest.raises(ValueError, match="2행"):
        parse_pasted_targets("192.0.2.10\t1\n192.0.2.20\tall,24")


def test_quoted_csv_alias_with_embedded_tab_remains_supported() -> None:
    groups = parse_pasted_targets('192.0.2.10,22,12,"floor\tA"')
    assert len(groups) == 1
    assert groups[0].target.endpoint == "192.0.2.10:22"
    assert groups[0].interfaces == ("12",)
    assert groups[0].label == "floor\tA"


def test_reported_error_line_accounts_for_excel_header() -> None:
    with pytest.raises(ValueError, match="3행"):
        parse_pasted_targets("스위치 IP\t포트\n192.0.2.10\t12\n192.0.2.20\tall")


def test_many_repeated_switch_rows_keep_existing_merge_order_and_limits() -> None:
    rows = []
    for index in range(300):
        ip = f"{'192.0.2' if index < 250 else '198.51.100'}.{index % 250 + 1}"
        rows.extend(
            (
                make_target(ip, 22, "1,2"),
                make_target(ip, 22, "2,3", "단말"),
                make_target(ip, 22, "3"),
            )
        )
    merged = merge_targets(rows)
    assert len(merged) == 300
    assert all(group.interfaces == ("1", "2", "3") for group in merged)
    assert all(group.label == "단말" for group in merged)
    assert merged[0].target.ip == "192.0.2.1"
    assert merged[-1].target.ip == "198.51.100.50"


def test_unique_interface_cap_is_not_consumed_by_repeated_rows() -> None:
    initial = make_target("192.0.2.1", 22, ",".join(str(i) for i in range(1, 385)))
    repeats = [initial] * 100
    assert merge_targets(repeats) == (initial,)
    remaining = [
        make_target(f"198.51.100.{index}", 22, ",".join(str(i) for i in range(1, 385)))
        for index in range(1, 37)
    ]
    assert sum(len(item.interfaces) for item in merge_targets([*repeats, *remaining])) == 14_208
    with pytest.raises(ValueError, match="14,400"):
        merge_targets(
            [
                *repeats,
                *remaining,
                make_target("203.0.113.1", 22, ",".join(str(i) for i in range(1, 194))),
            ]
        )
