"""Exercise scoped PoE OFF and lost PoE ON acknowledgements using loopback SSH only."""

from threading import Event
from unittest.mock import patch

from browser_qa import (
    click_quick_poe,
    edit_query_targets,
    enter_ssh_credentials,
    expect_power_result,
    open_port_controls,
    open_target_editor,
    run_browser_case,
)
from playwright.sync_api import expect
from verify_http_ssh import MonitorFixture, interface_status_table


class RangeSwitch(MonitorFixture):
    def __init__(self):
        super().__init__()
        self.power = {"1": False, "2": False}
        self.replacement_key = None
        self.auth_at_disconnect = 0
        self.stale_port = None
        self.linked = set()
        self.no_neighbor = {"2"}
        self.draws = {"1": 0, "2": 0}

    def _respond(self, channel, command):
        port = command.split()[-1]
        prompt = self.prompt
        if command == "show interfaces brief":
            lines = ["Port Type | Alert Enabled Status Mode"]
            lines.extend(f"{p} 100/1000T | No Yes Down Auto" for p in self.power)
        elif command == "show interfaces status":
            lines = interface_status_table(
                (p, "", "Up" if p in self.linked else "Down") for p in self.power
            )
        elif command.startswith("show power-over-ethernet ") and port in self.power:
            enabled = self.power[port]
            if port == self.stale_port:
                enabled = False
                self.stale_port = None
            watts = self.draws[port] if enabled else 0
            state = "Delivering" if watts else ("Searching" if enabled else "Disabled")
            lines = [
                f"Status and Configuration Information for port {port}",
                "Power Enable : " + ("Yes" if enabled else "No"),
                f"PoE Port Status : {state}",
                f"PD Power Draw : {watts} W",
            ]
        elif command.startswith("show lldp info remote-device ") and port in self.no_neighbor:
            lines = []
        elif command == "configure terminal":
            lines = []
            prompt = self.prompt[:-1] + "(config)#"
        elif command in (
            "no interface 1 power-over-ethernet",
            "no interface 2 power-over-ethernet",
        ):
            self.power[command.split()[2]] = False
            lines = []
            prompt = self.prompt[:-1] + "(config)#"
        elif command in ("interface 1 power-over-ethernet", "interface 2 power-over-ethernet"):
            port = command.split()[1]
            self.power[port] = True
            if port == "1":
                self.commands.append(command)
                self.auth_at_disconnect = len(self.auth_attempts)
                if self.replacement_key:
                    self.host_key = self.replacement_key
                channel.close()
                return False
            self.stale_port = port
            lines = []
            prompt = self.prompt[:-1] + "(config)#"
        else:
            return super()._respond(channel, command)
        self.commands.append(command)
        channel.sendall(("\r\n".join([command, *lines, prompt])).encode("utf-8"))
        return False


def cancel_before_off(page, switch, address):
    reading, resume = Event(), Event()
    respond = switch._respond

    def hold_status(channel, command):
        if command == "show power-over-ethernet 1":
            reading.set()
            assert resume.wait(30), "Test did not resume the SSH status read"
        return respond(channel, command)

    page.locator("#clearSelection").click()
    page.frame_locator("#matrixViewer").locator(
        f'#tbody tr[data-port-key="{address}/1"] input'
    ).check()
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    start = len(switch.commands)
    with (
        patch.object(switch, "_respond", hold_status),
        click_quick_poe(page, "poe_off", [(address, "1")]),
    ):
        try:
            assert reading.wait(30), "Quick OFF never reached the port state read"
            expect(page.locator("#quickOff")).to_be_disabled()
            with page.expect_response(lambda res: res.url.endswith("/cancel")) as cancellation:
                page.locator("#stopJob").click()
            assert cancellation.value.ok
        finally:
            resume.set()
        expect(page.locator("#stopJob")).to_be_hidden(timeout=90000)
    assert "configure terminal" not in switch.commands[start:]
    assert switch.power["1"]
    checkbox = page.frame_locator("#matrixViewer").locator(
        f'#tbody tr[data-port-key="{address}/1"] input'
    )
    expect(checkbox).to_be_enabled()
    expect(checkbox).to_be_checked()
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")


def assert_port_off(page, switch, address, port, before, since):
    expect(page.locator("#controlResult")).to_contain_text("검증 완료", timeout=90000)
    expect_power_result(
        page, address, f"{before} W", "0 W", port=port, delta=f"\N{MINUS SIGN}{before} W"
    )
    commands = switch.commands[since:]
    change = f"no interface {port} power-over-ethernet"
    writes = [cmd for cmd in commands if cmd.startswith(("interface ", "no interface "))]
    assert writes == [change], writes
    assert commands[commands.index(change) + 1 :].count(f"show power-over-ethernet {port}") >= 2
    assert not switch.power[port]


def exercise_port_off(page, switch, address):
    # Port 2 has no LLDP neighbor and is physically down while drawing power.
    switch.linked.add("1")
    switch.draws = {"1": 5, "2": 7}
    page.locator("#queryPorts").click()
    expect(page.locator("#statusText")).to_contain_text("포트 조회 완료", timeout=90000)
    page.locator("#listView").click()
    table = page.frame_locator("#matrixViewer").locator("#tbody")
    first = table.locator(f'tr[data-port-key="{address}/1"] input')
    second = table.locator(f'tr[data-port-key="{address}/2"] input')
    open_port_controls(page)
    page.locator("#operation").select_option("disable")
    page.locator("#selectEligible").click()
    expect(first).to_be_checked()
    expect(second).not_to_be_checked()

    page.locator("#operation").select_option("poe_off")
    cancel_before_off(page, switch, address)
    page.locator("#selectEligible").click()
    expect(page.locator("#selectedCount")).to_have_text("선택 2개")
    first.uncheck()
    expect(second).to_be_checked()
    since = len(switch.commands)
    with click_quick_poe(page, "poe_off", [(address, "2")]):
        assert_port_off(page, switch, address, "2", 7, since)
    assert switch.power["1"]

    # The other port now loses its neighbor and link too. Exercise the reviewed path.
    switch.no_neighbor.add("1")
    switch.linked.clear()
    page.locator("#queryPorts").click()
    expect(page.locator("#statusText")).to_contain_text("포트 조회 완료", timeout=90000)
    page.locator("#listView").click()
    open_port_controls(page)
    page.locator("#clearSelection").click()
    first.check()
    expect(second).not_to_be_checked()
    since = len(switch.commands)
    page.locator("#preview").click()
    expect(page.locator("#review")).to_be_visible(timeout=90000)
    assert switch.power["1"] and "configure terminal" not in switch.commands[since:]
    page.locator("#reviewBody input").fill(page.locator("#reviewBody code").inner_text())
    page.get_by_role("button", name="승인하고 명령 실행").click()
    assert_port_off(page, switch, address, "1", 5, since)


def run_range(page, url, switch, second, shots, output):
    page.goto(url)
    page.locator('nav a[href="#ports"]').click()
    # Registration is offline. The first quick action asks for the account in place.
    address = f"127.0.0.1:{switch.port}"
    page.locator("#queryTargets").fill(address + " 2")
    open_target_editor(page)
    page.locator("#verifyEditor").click()
    page.locator("#acceptTargets").click()
    open_port_controls(page)
    page.get_by_role("button", name="범위 전체 선택", exact=True).click()
    expect(page.locator("#selectedCount")).to_have_text("선택 1개")
    page.get_by_role("button", name="PoE OFF 즉시 실행").click()
    expect(page.locator("#queryCredentials")).to_be_visible()
    assert not switch.auth_attempts
    with page.expect_request(
        lambda req: (
            req.method == "POST"
            and req.url.endswith("/api/jobs")
            and req.post_data_json["action"] == "control.quick-execute"
        )
    ) as resumed:
        enter_ssh_credentials(page, switch)
    assert resumed.value.post_data_json["body"]["targets"] == [
        {"ip": "127.0.0.1", "sshPort": switch.port, "port": "2"}
    ]
    expect(page.locator("#quickResult")).to_contain_text("이미 OFF 1개", timeout=90000)
    expect(page.locator("#review")).to_be_hidden()
    assert "configure terminal" not in switch.commands
    for rotate_key in (False, True):
        # Simulate a pair of powered-off access ports without attached PDs.
        switch.linked.clear()
        switch.power = {"1": False, "2": False}
        if rotate_key:
            switch.replacement_key = second.host_key
            switch.prompt = "Aruba 2930F Edge#"
        start = len(switch.commands)
        edit_query_targets(page).fill(f"127.0.0.1:{switch.port} 1-2")
        page.locator("#queryForm .primary").click()
        expect(page.locator("#statusText")).to_contain_text("포트 조회 완료", timeout=90000)
        expect(page.locator("#matrixViewer")).to_be_visible()
        open_port_controls(page)
        page.locator("#selectionScope").select_option("all")
        page.get_by_role("button", name="범위 전체 선택", exact=True).click()
        expect(page.locator("#selectedCount")).to_have_text("선택 2개")
        address = f"127.0.0.1:{switch.port}"
        targets = [(address, "1"), (address, "2")]
        if not rotate_key:
            with click_quick_poe(page, "poe_off", targets):
                expect(page.locator("#quickResult")).to_contain_text("이미 OFF 2개", timeout=90000)
            assert "configure terminal" not in switch.commands[start:]
            page.get_by_role("button", name="범위 전체 선택", exact=True).click()
        with click_quick_poe(page, "poe_on", targets):
            rows = page.locator("#controlResult tbody tr")
            # The fixture drops an ACK and returns a stale setting. Allow the
            # serial SSH checks and both post-control power reads to finish.
            expect(rows).to_have_count(2, timeout=150000)
        results = rows.all_text_contents()
        writes = [cmd for cmd in switch.commands[start:] if cmd.endswith(" power-over-ethernet")]
        if rotate_key:
            assert "검증 미완료" in results[0] and "일시 중지" in results[1], results
            assert writes == ["interface 1 power-over-ethernet"], writes
            assert len(switch.auth_attempts) == switch.auth_at_disconnect
        else:
            assert all("검증 완료" in row for row in results), results
            for port in ("1", "2"):
                expect_power_result(page, address, "0 W", "0 W", port=port, delta="0 W")
            after_second_write = switch.commands.index("interface 2 power-over-ethernet", start) + 1
            assert switch.commands[after_second_write:].count("show power-over-ethernet 2") >= 2
            assert writes == ["interface 1 power-over-ethernet", "interface 2 power-over-ethernet"]
            exercise_port_off(page, switch, address)

    page.locator("#shutdown").click()
    page.locator("#reviewBody").get_by_role("button", name="서버 종료").click()
    expect(page.locator("#statusText")).to_contain_text("종료했습니다.")


if __name__ == "__main__":
    run_browser_case(run_range, switch_factory=RangeSwitch)
    print(
        "PASS quick and reviewed PoE OFF without link or LLDP, selected-port power refresh, "
        "and bulk PoE ON with lost acknowledgement, no replay and changed-key halt"
    )
