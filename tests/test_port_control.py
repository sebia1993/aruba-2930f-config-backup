from __future__ import annotations

import importlib
import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from aruba2930f_backup.hostkeys import HostKeyStore, sha256_fingerprint
from aruba2930f_backup.models import (
    CollectionFailure,
    CollectionOptions,
    Credentials,
    DeviceTarget,
    ErrorCode,
    HostKeyObservation,
)
from aruba2930f_backup.port_control import PortAction, parse_interface_status, parse_poe_status


@pytest.mark.parametrize(
    "value", ["", "0", "01", "1,2", "1-2", "all", "1/1/1", "1\nend", " 1", "1 "]
)
def test_unsafe_interface_never_becomes_a_command_target(value: str) -> None:
    module = importlib.import_module("aruba2930f_backup.port_control")
    with pytest.raises(ValueError):
        module.validate_interface(value)


def test_port_admin_state_is_independent_of_link_state() -> None:
    module = importlib.import_module("aruba2930f_backup.port_control")
    state = module.parse_interface_status(
        "Port Type | Alert Enabled Status Mode\n24 100/1000T | No Yes Down Auto\n", "24"
    )
    assert state.enabled is True
    assert state.link_status == "Down"


def test_poe_disabled_output_need_not_include_power_delivery_fields() -> None:
    module = importlib.import_module("aruba2930f_backup.port_control")
    state = module.parse_poe_status(
        "Status and Counters - Port Power Status for port 24\nPower Enable : No\n", "24"
    )
    assert state.enabled is False
    assert state.watts is None


TARGET = DeviceTarget("192.0.2.20")
CREDENTIALS = Credentials("operator", "test-password")


class Device:
    """Synthetic device whose state changes independently of the service."""

    def __init__(self) -> None:
        self.interface = "24"
        self.enabled = True
        self.link_status = "Down"
        self.poe_enabled = True
        self.poe_status = "Searching"
        self.poe_format = "legacy"
        self.sku = "JL255A"
        self.hostname = "edge-lab"
        self.fingerprint = sha256_fingerprint(b"synthetic-port-control-key")
        self.mutations: list[tuple[str, str]] = []
        self.sessions: list[Session] = []
        self.fail_after_change = False
        self.fail_post_read = False
        self.ignore_change = False
        self.malformed = False
        self.probes = 0
        self.lldp_chassis = "00:11:22:33:44:55"
        self.lldp_port = "eth0"
        self.lldp_model = "AP-535"
        self.lldp_failure = False

    def probe(self, target: DeviceTarget, *, timeout: float = 15.0) -> HostKeyObservation:
        self.probes += 1
        return HostKeyObservation(target, "ssh-ed25519", self.fingerprint)

    def session(
        self,
        target: DeviceTarget,
        credentials: Credentials,
        options: CollectionOptions,
        store: HostKeyStore,
    ) -> Session:
        session = Session(self)
        self.sessions.append(session)
        return session


class Session:
    def __init__(self, device: Device) -> None:
        self.device = device
        self.closed = False

    def connect(self) -> None:
        pass

    def enter_enable(self) -> None:
        pass

    def disable_paging(self) -> None:
        pass

    def set_terminal_width(self) -> None:
        pass

    def get_prompt(self) -> str:
        return f"{self.device.hostname}#"

    def send_show(self, command: str) -> str:
        if command == "show version":
            return f"Aruba {self.device.sku} 2930F Switch\nSoftware revision WC.16.11.0025"
        assert command == "show modules"
        return f"Chassis: Aruba 2930F {self.device.sku}"

    def read_interfaces(self) -> str:
        d = self.device
        if d.fail_post_read and d.mutations:
            raise TimeoutError("synthetic sensitive detail")
        if d.malformed:
            return "unparseable"
        enabled = "Yes" if d.enabled else "No"
        return f"Port Type | Alert Enabled Status Mode\n{d.interface} 100/1000T | No {enabled} {d.link_status} Auto\n"

    def read_poe(self, interface: str) -> str:
        d = self.device
        enabled = "Yes" if d.poe_enabled else "No"
        if d.poe_format == "configuration":
            text = f"Status and Configuration Information for port {d.interface}\nPower Enable : {enabled}\n"
            if d.poe_enabled:
                text += f"PoE Port Status : {d.poe_status}\nPSE Reserved Power : 60 W\nPD Power Draw : 0 W\n"
            return text
        text = f"Status and Counters - Port Power Status for port {d.interface}\nPower Enable : {enabled}\n"
        if d.poe_enabled:
            text += f"Detection Status : {d.poe_status}\nPower : 0 W\n"
        return text

    def read_lldp(self, interface: str) -> str:
        d = self.device
        if d.lldp_failure:
            raise TimeoutError("synthetic LLDP timeout")
        if d.lldp_model == "none":
            return "LLDP Remote Device Information Detail\nNo neighbors found.\n"
        return (
            "LLDP Remote Device Information Detail\n"
            f" Local Port :{interface}\n"
            " ChassisType :mac-address\n"
            f" ChassisId :{d.lldp_chassis}\n"
            " PortType :local\n"
            f" PortId :{d.lldp_port}\n"
            f" System Descr :Aruba {d.lldp_model}\n"
        )

    def change_port(self, interface: str, action: str) -> None:
        d = self.device
        d.mutations.append((interface, action))
        if not d.ignore_change:
            if action.startswith("poe_"):
                d.poe_enabled = action == "poe_on"
            else:
                d.enabled = action == "enable"
        if d.fail_after_change:
            raise TimeoutError("synthetic sensitive detail")

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Any, Device, list[float]]:
    module = importlib.import_module("aruba2930f_backup.port_control")
    d = Device()
    clock = [100.0]
    service = module.PortControlService(
        host_key_store=HostKeyStore(tmp_path / "known_hosts.json"),
        host_key_probe=d,
        session_factory=d.session,
        audit_directory=tmp_path / "audit",
        clock=lambda: clock[0],
    )
    return service, d, clock


def inspect(service: Any, device: Device) -> Any:
    return service.inspect(
        TARGET, device.interface, CREDENTIALS, approve_host_key=lambda check: True
    )


@pytest.mark.parametrize("action", list(PortAction))
@pytest.mark.parametrize("poe_format", ["legacy", "configuration"])
def test_each_action_changes_only_its_setting_and_is_verified(
    setup: Any, action: PortAction, poe_format: str
) -> None:
    service, d, _ = setup
    d.poe_format = poe_format
    if action.is_poe:
        d.poe_enabled = not action.desired_enabled
    else:
        d.enabled = not action.desired_enabled
    old_enabled, old_poe = d.enabled, d.poe_enabled
    snapshot = inspect(service, d)
    result = service.apply(snapshot, action, CREDENTIALS, confirmation=snapshot.confirmation_text)
    assert result.status == "verified"
    assert len(d.mutations) == 1
    assert d.poe_enabled is (action.desired_enabled if action.is_poe else old_poe)
    assert d.enabled is (old_enabled if action.is_poe else action.desired_enabled)
    assert result.after.link_status == "Down"  # Enabling does not imply link restoration.
    assert all(session.closed for session in d.sessions)
    records = [json.loads(line) for line in result.audit_path.read_text().splitlines()]
    assert [row["status"] for row in records] == ["pending", "verified"]
    assert records[0]["interface"] == "24"
    assert CREDENTIALS.password not in result.audit_path.read_text()


def test_port_preflight_uses_system_name_instead_of_prompt_alias(
    setup: Any,
) -> None:
    service, device, _clock = setup
    device.hostname = "alias-2930"
    device.system_name = "ACTUAL-SW-2930"
    device.poe_enabled = False
    snapshot = inspect(service, device)
    assert snapshot.hostname == "ACTUAL-SW-2930"
    device.hostname = "different-alias"
    result = service.apply(
        snapshot,
        PortAction.POE_ON,
        CREDENTIALS,
        confirmation=snapshot.confirmation_text,
    )
    assert result.status == "verified"
    assert result.after.hostname == "ACTUAL-SW-2930"
    assert device.mutations == [("24", "poe_on")]


def test_poe_on_accepts_unknown_hostname_when_fingerprint_and_settings_match(
    setup: Any,
) -> None:
    service, device, _clock = setup
    device.poe_enabled = False
    device.hostname = ""
    snapshot = inspect(service, device)
    assert snapshot.hostname == "확인 불가"
    # A subsequent SSH session may provide a parseable prompt hostname.
    device.hostname = "edge-lab"
    result = service.apply(
        snapshot,
        PortAction.POE_ON,
        CREDENTIALS,
        confirmation=snapshot.confirmation_text,
    )
    assert result.status == "verified"
    assert device.mutations == [("24", "poe_on")]


def test_poe_on_does_not_accept_changed_known_hostname_or_settings(
    setup: Any,
) -> None:
    service, device, _clock = setup
    device.poe_enabled = False
    snapshot = inspect(service, device)
    device.hostname = "unexpected-host"
    with pytest.raises((ValueError, CollectionFailure)):
        service.apply(
            snapshot,
            PortAction.POE_ON,
            CREDENTIALS,
            confirmation=snapshot.confirmation_text,
        )
    assert device.mutations == []


def test_poe_on_allows_missing_hostname_after_change_without_unverified(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, device, _clock = setup
    device.poe_enabled = False
    snapshot = inspect(service, device)
    original = Session.change_port

    def change_then_lose_hostname(self: Session, interface: str, action: str) -> None:
        original(self, interface, action)
        self.device.hostname = ""

    monkeypatch.setattr(Session, "change_port", change_then_lose_hostname)
    result = service.apply(
        snapshot,
        PortAction.POE_ON,
        CREDENTIALS,
        confirmation=snapshot.confirmation_text,
    )
    assert result.status == "verified"
    assert device.mutations == [("24", "poe_on")]


def test_already_desired_state_sends_no_change(setup: Any) -> None:
    service, d, _ = setup
    snapshot = inspect(service, d)
    result = service.apply(
        snapshot, PortAction.ENABLE, CREDENTIALS, confirmation=snapshot.confirmation_text
    )
    assert result.status == "unchanged"
    assert d.mutations == []


@pytest.mark.parametrize(
    "kind", ["wrong-confirmation", "expired", "copy", "state-changed", "hostname-changed"]
)
def test_stale_or_unconfirmed_targets_cannot_change(setup: Any, kind: str) -> None:
    service, d, clock = setup
    snapshot = inspect(service, d)
    confirmation = snapshot.confirmation_text
    if kind == "wrong-confirmation":
        confirmation = "192.0.2.21:22 / 24"
    elif kind == "expired":
        clock[0] += 121
    elif kind == "copy":
        snapshot = replace(snapshot, interface="25")
    elif kind == "state-changed":
        d.enabled = False
    else:
        d.hostname = "changed-lab"
    with pytest.raises((ValueError, CollectionFailure)):
        service.apply(snapshot, PortAction.DISABLE, CREDENTIALS, confirmation=confirmation)
    assert not d.mutations


@pytest.mark.parametrize("kind", ["send-timeout", "post-read-timeout", "ignored"])
def test_uncertain_results_never_replay_mutations(setup: Any, kind: str) -> None:
    service, d, _ = setup
    snapshot = inspect(service, d)
    d.fail_after_change = kind == "send-timeout"
    d.fail_post_read = kind == "post-read-timeout"
    d.ignore_change = kind == "ignored"
    result = service.apply(
        snapshot, PortAction.POE_OFF, CREDENTIALS, confirmation=snapshot.confirmation_text
    )
    assert result.status == "unverified"
    assert len(d.mutations) == 1
    assert "sensitive" not in result.message
    with pytest.raises(ValueError):
        service.apply(
            snapshot, PortAction.POE_OFF, CREDENTIALS, confirmation=snapshot.confirmation_text
        )
    assert len(d.mutations) == 1


def test_new_key_requires_approval_before_authenticated_connection(setup: Any) -> None:
    service, d, _ = setup
    with pytest.raises(CollectionFailure):
        service.inspect(TARGET, "24", CREDENTIALS, approve_host_key=lambda check: False)
    assert not d.sessions


def test_changed_host_key_blocks_apply_without_approval(setup: Any) -> None:
    service, d, _ = setup
    snapshot = inspect(service, d)
    d.fingerprint = sha256_fingerprint(b"different-synthetic-key")
    with pytest.raises(CollectionFailure) as error:
        service.apply(
            snapshot, PortAction.DISABLE, CREDENTIALS, confirmation=snapshot.confirmation_text
        )
    assert error.value.code is ErrorCode.HOST_KEY_CHANGED
    assert len(d.sessions) == 1
    assert not d.mutations


def test_non_poe_model_allows_port_control_but_blocks_poe(setup: Any) -> None:
    service, d, _ = setup
    d.sku = "JL253A"
    snapshot = inspect(service, d)
    assert snapshot.poe_enabled is None
    with pytest.raises(ValueError):
        service.apply(
            snapshot, PortAction.POE_OFF, CREDENTIALS, confirmation=snapshot.confirmation_text
        )
    assert not d.mutations
    snapshot = inspect(service, d)
    result = service.apply(
        snapshot, PortAction.DISABLE, CREDENTIALS, confirmation=snapshot.confirmation_text
    )
    assert result.status == "verified"


def test_audit_failure_blocks_command_before_transmission(setup: Any) -> None:
    service, d, _ = setup
    snapshot = inspect(service, d)
    service.audit_directory.write_text("this path is a file")
    with pytest.raises(CollectionFailure) as error:
        service.apply(
            snapshot, PortAction.DISABLE, CREDENTIALS, confirmation=snapshot.confirmation_text
        )
    assert error.value.code is ErrorCode.REPORT_WRITE_FAILED
    assert not d.mutations


@pytest.mark.parametrize(
    "output",
    [
        "Port Type | Alert Enabled Status Mode\n2 100/1000T | No Yes Up Auto\n",
        "Port Type | Alert Enabled Status Mode\n24 100/1000T | No Yes Up Auto\n24 100/1000T | No No Down Auto\n",
        "Port Type | Alert Enabled Status Mode\n24 100/1000T | nonsense\n",
    ],
)
def test_missing_ambiguous_or_malformed_port_is_rejected(output: str) -> None:
    with pytest.raises(CollectionFailure):
        parse_interface_status(output, "24")


@pytest.mark.parametrize("status", ["Searching", "Disabled", "Delivering", "Other Fault"])
def test_power_enable_is_not_inferred_from_detection_status(status: str) -> None:
    result = parse_poe_status(
        f"Status and Counters - Port Power Status for port 1/24\nPower Enable : Yes\nDetection Status : {status}\n",
        "1/24",
    )
    assert result.enabled is True
    assert result.status == status


def test_poe_response_for_another_port_is_rejected() -> None:
    with pytest.raises(CollectionFailure):
        parse_poe_status(
            "Status and Counters - Port Power Status for port 25\nPower Enable : No", "24"
        )


def test_duplicate_invalid_power_enable_field_is_rejected() -> None:
    with pytest.raises(CollectionFailure):
        parse_poe_status(
            "Status and Counters - Port Power Status for port 24\n"
            "Power Enable : No\nPower Enable : invalid\n",
            "24",
        )


@pytest.mark.parametrize(
    "extra",
    [
        "Detection Status : Unknown\n",
        "Detection Status : Disabled\nDetection Status : Unknown\n",
        "Power : -1 W\n",
        "Power : unreadable\n",
        "Power : 0 W\nPower : unreadable\n",
    ],
)
def test_present_but_invalid_poe_fields_are_not_silently_ignored(extra: str) -> None:
    with pytest.raises(CollectionFailure):
        parse_poe_status(
            "Status and Counters - Port Power Status for port 24\nPower Enable : No\n" + extra,
            "24",
        )


def test_unexpected_value_error_is_not_displayed(setup: Any, monkeypatch: Any) -> None:
    service, d, _ = setup

    def broken_read(self: Session) -> str:
        raise ValueError("synthetic sensitive detail")

    monkeypatch.setattr(Session, "read_interfaces", broken_read)
    with pytest.raises(CollectionFailure) as error:
        inspect(service, d)
    assert "sensitive" not in error.value.safe_message
    assert all(session.closed for session in d.sessions)


def test_concurrent_inspection_is_rejected_before_another_connection(
    setup: Any,
    monkeypatch: Any,
) -> None:
    service, d, _ = setup
    entered, release = threading.Event(), threading.Event()
    failures: list[BaseException] = []
    original_probe = d.probe

    def blocked_probe(target: DeviceTarget, *, timeout: float = 15.0) -> HostKeyObservation:
        entered.set()
        assert release.wait(3)
        return original_probe(target, timeout=timeout)

    monkeypatch.setattr(d, "probe", blocked_probe)

    def run() -> None:
        try:
            inspect(service, d)
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(3)
        with pytest.raises(ValueError, match="진행 중"):
            inspect(service, d)
        assert d.probes == 0
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert failures == []
    assert d.probes == 1


def test_failed_new_inspection_invalidates_old_snapshot(setup: Any) -> None:
    service, d, _ = setup
    snapshot = inspect(service, d)
    with pytest.raises(ValueError):
        service.inspect(TARGET, "1-2", CREDENTIALS, approve_host_key=lambda check: True)
    with pytest.raises(ValueError):
        service.apply(
            snapshot, PortAction.DISABLE, CREDENTIALS, confirmation=snapshot.confirmation_text
        )
    assert not d.mutations


@pytest.mark.parametrize("kind", ["model", "malformed"])
def test_prewrite_identity_or_output_failure_cannot_mutate(setup: Any, kind: str) -> None:
    service, d, _ = setup
    snapshot = inspect(service, d)
    if kind == "model":
        d.sku = "JL999A"
    else:
        d.malformed = True
    with pytest.raises(CollectionFailure):
        service.apply(
            snapshot, PortAction.DISABLE, CREDENTIALS, confirmation=snapshot.confirmation_text
        )
    assert not d.mutations


def documented_poe_status() -> str:
    # Official 2930F/2930M AOS-S 16.11 Management and Configuration Guide,
    # pp. 108-109: the 1/28 example, preserving both output columns.
    # https://arubanetworking.hpe.com/techdocs/AOS-Switch/16.11/Aruba%202930%20M%26F%20Management%20and%20Configuration%20Guide%20for%20AOS-S%2016.11.pdf
    return (Path(__file__).parent / "fixtures" / "poe_status_aos_s_16_11.txt").read_text(
        encoding="utf-8"
    )


def test_documented_configuration_poe_format_binds_vsf_port_and_power_draw() -> None:
    state = parse_poe_status(documented_poe_status(), "1/28")
    assert state.enabled is True
    assert state.status == "Delivering"
    assert state.watts == 3.2


def test_configuration_format_uses_pd_draw_not_pse_reserved_or_allocated_power() -> None:
    output = documented_poe_status().replace("for port 1/28", "for port 1")
    output = output.replace("PSE Reserved Power : 3.2 W", "PSE Reserved Power : 57.4 W")
    output = output.replace("PD Power Draw : 3.2 W", "PD Power Draw : 49.2 W")
    state = parse_poe_status(output, "1")
    assert state.watts == 49.2


def test_configuration_format_disabled_can_omit_delivery_and_draw() -> None:
    state = parse_poe_status(
        "Status and Configuration Information for port 1/28\nPower Enable : No\n", "1/28"
    )
    assert state.enabled is False
    assert state.watts is None


@pytest.mark.parametrize("interface", ["28", "1", "1/2", "2/28"])
def test_configuration_format_cannot_be_applied_to_another_port(interface: str) -> None:
    with pytest.raises(CollectionFailure):
        parse_poe_status(documented_poe_status(), interface)


@pytest.mark.parametrize(
    "extra",
    [
        "Status and Configuration Information for port 1/28\n",
        "Status and Configuration Information for port 2/28\n",
        "Status and Counters - Port Power Status for port 1/28\n",
        "PoE Port Status : Delivering\n",
        "PoE Port Status : Unknown\n",
        "Detection Status : Delivering\n",
        "PD Power Draw : 3.2 W\n",
        "PD Power Draw : Unknown\n",
        "Power : 3.2 W\n",
    ],
)
def test_configuration_format_rejects_ambiguous_and_mixed_fields(extra: str) -> None:
    with pytest.raises(CollectionFailure):
        parse_poe_status(documented_poe_status() + extra, "1/28")


@pytest.mark.parametrize(
    "old,new",
    [
        ("PoE Port Status : Delivering", "Detection Status : Delivering"),
        ("PD Power Draw : 3.2 W", "Power : 3.2 W"),
        ("PoE Port Status : Delivering", "PoE Port Status : Unknown"),
        ("PD Power Draw : 3.2 W", "PD Power Draw : -1 W"),
    ],
)
def test_configuration_format_rejects_replaced_or_invalid_fields(old: str, new: str) -> None:
    with pytest.raises(CollectionFailure):
        parse_poe_status(documented_poe_status().replace(old, new), "1/28")


@pytest.mark.parametrize("extra", ["PoE Port Status : Disabled\n", "PD Power Draw : 0 W\n"])
def test_legacy_format_rejects_configuration_format_fields(extra: str) -> None:
    with pytest.raises(CollectionFailure):
        parse_poe_status(
            "Status and Counters - Port Power Status for port 24\nPower Enable : No\n" + extra,
            "24",
        )


def test_bulk_off_requires_current_lldp_identity_in_the_write_session(setup: Any) -> None:
    service, d, _ = setup
    d.link_status = "Up"
    snapshot = inspect(service, d)
    identity = (d.lldp_chassis, d.lldp_port)
    result = service.apply(
        snapshot,
        PortAction.POE_OFF,
        CREDENTIALS,
        confirmation=snapshot.confirmation_text,
        expected_lldp_identity=identity,
    )
    assert result.status == "verified"
    assert d.mutations == [("24", "poe_off")]
    assert all(session.closed for session in d.sessions)


@pytest.mark.parametrize("change", ["chassis", "port", "model", "missing", "failed"])
def test_bulk_off_rejects_rewired_missing_or_unverified_lldp_before_write(
    setup: Any, change: str
) -> None:
    service, d, _ = setup
    d.link_status = "Up"
    snapshot = inspect(service, d)
    expected = (d.lldp_chassis, d.lldp_port)
    if change == "chassis":
        d.lldp_chassis = "66:77:88:99:aa:bb"
    elif change == "port":
        d.lldp_port = "eth9"
    elif change == "model":
        d.lldp_model = "AP-515"
    elif change == "missing":
        d.lldp_model = "none"
    else:
        d.lldp_failure = True
    with pytest.raises(CollectionFailure):
        service.apply(
            snapshot,
            PortAction.POE_OFF,
            CREDENTIALS,
            confirmation=snapshot.confirmation_text,
            expected_lldp_identity=expected,
        )
    assert d.mutations == []
    assert all(session.closed for session in d.sessions)


def test_lldp_expectation_is_not_accepted_for_on_operation(setup: Any) -> None:
    service, d, _ = setup
    snapshot = inspect(service, d)
    with pytest.raises(ValueError, match="LLDP"):
        service.apply(
            snapshot,
            PortAction.POE_ON,
            CREDENTIALS,
            confirmation=snapshot.confirmation_text,
            expected_lldp_identity=(d.lldp_chassis, d.lldp_port),
        )
    assert not d.mutations


def test_cached_lldp_on_link_down_is_rejected_before_any_change(setup: Any) -> None:
    from aruba2930f_backup.port_control import PreMutationRejected

    service, device, _ = setup
    device.link_status = "Up"
    snapshot = inspect(service, device)
    device.link_status = "Down"
    # The switch still returns the old AP-535 from its cached LLDP table.
    with pytest.raises(PreMutationRejected, match="Link Up"):
        service.apply(
            snapshot,
            PortAction.POE_OFF,
            CREDENTIALS,
            confirmation=snapshot.confirmation_text,
            expected_lldp_identity=(device.lldp_chassis, device.lldp_port),
        )
    assert device.mutations == []


def test_prewrite_lldp_identity_change_has_distinct_safe_failure_type(setup: Any) -> None:
    from aruba2930f_backup.port_control import PreMutationRejected

    service, device, _ = setup
    device.link_status = "Up"
    snapshot = inspect(service, device)
    expected = (device.lldp_chassis, device.lldp_port)
    device.lldp_chassis = "66:77:88:99:aa:bb"
    with pytest.raises(PreMutationRejected, match="LLDP"):
        service.apply(
            snapshot,
            PortAction.POE_OFF,
            CREDENTIALS,
            confirmation=snapshot.confirmation_text,
            expected_lldp_identity=expected,
        )
    assert not device.mutations
